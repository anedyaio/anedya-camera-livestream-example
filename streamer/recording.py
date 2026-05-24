"""
Rolling MP4 segment recorder with audio.

RecordingManager runs as a background asyncio task. It consumes video frames
queued by CameraSource and audio PCM frames queued by MicrophoneSource, writing
them into fixed-length MP4 files on disk using PyAV (mpeg4 video + AAC audio).

Segment lifecycle:
    1. CameraSource calls enqueue_frame() with each captured frame.
    2. MicrophoneSource calls enqueue_audio() with each 20 ms PCM block.
    3. The run() loop pulls video frames and drains the audio queue,
       writing both to the active av container.
    4. Every SEGMENT_DURATION_SECONDS the container is flushed/closed and a
       new one is opened. The closed file is added to _finalized_segments.
    5. Peers call get_timeline() and resolve_playback_offset() to map slider
       positions back to segment files and in-file seek positions.
"""

import asyncio
import logging
import os
import time
from datetime import datetime
from fractions import Fraction

import av
import numpy as np

from config import AUDIO_CHANNELS, AUDIO_SAMPLE_RATE

log = logging.getLogger("streamer")

VIDEO_TIME_BASE = Fraction(1, 90_000)


class RecordingManager:
    """Write rolling MP4 segments (video + audio) to disk and expose timeline metadata."""

    def __init__(
        self,
        record_path: str = "recordings",
        fps: float = 30.0,
        segment_duration_seconds: int = 60,
        retention_seconds: int = 24 * 60 * 60,
    ):
        self.record_path = record_path
        self.fps = fps
        self.segment_duration_seconds = segment_duration_seconds
        self.retention_seconds = retention_seconds

        self.frame_width: int | None = None
        self.frame_height: int | None = None

        # PyAV output container and streams (replace cv2.VideoWriter)
        self._av_container = None  # current open MP4 file
        self._av_video_stream = None  # video track inside current open MP4 file
        self._av_audio_stream = None  # audio track inside current open MP4 file

        # PTS = Presentation Time Stamp. Video uses a high-resolution time base
        # so recordings preserve real capture timing instead of FPS guesses.
        self._video_pts: int = 0
        self._audio_pts: int = 0

        self._segment_start_ts: float | None = None
        self._segment_path: str | None = None

        self._frame_queue: asyncio.Queue | None = (
            None  # video frames waiting to be written
        )
        self._audio_queue: asyncio.Queue | None = (
            None  # audio frames waiting to be written
        )
        self._is_running = False
        self._write_failures = 0
        self._max_write_failures = 3
        self._dropped_frames = 0
        self._last_drop_log_at = 0.0

        self._ready_event = asyncio.Event()

        # Only closed (fully written) segments are listed here. The in-progress
        # segment is not added until _close_current_segment() is called because
        # partially-written MP4 files cannot be seeked reliably.
        self._finalized_segments: list[dict] = []  # list of closed segments

    async def wait_until_ready(self) -> None:
        """Block until the background recording loop has started."""
        await self._ready_event.wait()

    def enqueue_frame(self, frame: np.ndarray, captured_at: float) -> None:
        """Queue a video frame for disk writing (called from CameraSource).

        Non-blocking — frames are dropped if the queue is full rather than
        stalling the live capture pipeline.
        """
        if not self._is_running or self._frame_queue is None:
            return
        try:
            self._frame_queue.put_nowait((frame, captured_at))
        except asyncio.QueueFull:
            self._dropped_frames += 1
            now = time.monotonic()
            if now - self._last_drop_log_at >= 5.0:
                log.warning(
                    "Recording queue full; dropped_frames=%d",
                    self._dropped_frames,
                )
                self._last_drop_log_at = now

    def enqueue_audio(self, pcm: np.ndarray) -> None:
        """Queue a PCM audio frame for recording (called from MicrophoneSource).

        Non-blocking — silently drops if queue full. Audio drops in recording
        are acceptable; they create a brief silence in the file rather than
        stalling capture.
        """
        if not self._is_running or self._audio_queue is None:
            return
        try:
            self._audio_queue.put_nowait(pcm.copy())
        except asyncio.QueueFull:
            pass

    @staticmethod
    def _flush_container(container, video_stream, audio_stream) -> bool:
        """Flush encoder buffers and close the av container. Runs in a thread."""
        try:
            if video_stream:
                for pkt in video_stream.encode(None):
                    container.mux(pkt)
            if audio_stream:
                for pkt in audio_stream.encode(None):
                    container.mux(pkt)
            container.close()
            return True
        except Exception as exc:
            log.warning("Error flushing recording segment: %s", exc)
            return False

    @staticmethod
    def _close_container_without_flush(container) -> None:
        """Close a failed container without asking encoders for more packets."""
        try:
            container.close()
        except Exception as exc:
            log.debug("Error closing failed recording segment: %s", exc)

    async def _close_current_segment(self, closed_at: float) -> None:
        """Flush the av container off the event loop and register the segment metadata."""
        if (
            not self._av_container
            or self._segment_start_ts is None
            or self._segment_path is None
        ):
            return

        # Capture state before clearing so the thread call gets stable references.
        container    = self._av_container
        video_stream = self._av_video_stream
        audio_stream = self._av_audio_stream
        segment_start = self._segment_start_ts
        path          = self._segment_path

        # Clear immediately so new frames don't try to write into a closing container.
        self._av_container    = None
        self._av_video_stream = None
        self._av_audio_stream = None
        self._video_pts       = 0
        self._audio_pts       = 0
        self._segment_start_ts = None
        self._segment_path     = None

        # Blocking flush happens in a thread — event loop stays free.
        flushed = await asyncio.to_thread(
            self._flush_container, container, video_stream, audio_stream
        )
        if not flushed:
            log.warning("Skipping unfinalized recording segment after flush failure: %s", path)
            self._delete_file(path, "failed recording segment")
            return

        duration = self._read_mp4_duration(path)
        if duration is None or duration <= 0:
            log.warning("Skipping recording segment with unreadable duration: %s", path)
            self._delete_file(path, "unreadable recording segment")
            return
        segment_end = segment_start + duration

        self._finalized_segments.append(
            {
                "name":     os.path.basename(path),
                "path":     path,
                "size":     os.path.getsize(path) if os.path.exists(path) else 0,
                "start_ts": segment_start,
                "end_ts":   segment_end,
                "duration": duration,
            }
        )
        self._finalized_segments.sort(key=lambda seg: seg["start_ts"])
        self._prune_expired_segments(closed_at)

    async def _abort_current_segment(self) -> None:
        """Drop the current partially-written segment after an encoder/muxer error."""
        if not self._av_container:
            return

        container = self._av_container
        path = self._segment_path

        self._av_container = None
        self._av_video_stream = None
        self._av_audio_stream = None
        self._video_pts = 0
        self._audio_pts = 0
        self._segment_start_ts = None
        self._segment_path = None

        await asyncio.to_thread(self._close_container_without_flush, container)
        self._delete_file(path, "failed recording segment")

    @staticmethod
    def _delete_file(path: str | None, reason: str) -> None:
        if not path or not os.path.exists(path):
            return
        try:
            os.remove(path)
            log.warning("Discarded %s: %s", reason, path)
        except OSError as exc:
            log.warning("Could not delete %s %s: %s", reason, path, exc)

    def _prune_expired_segments(self, now: float | None = None) -> None:
        """Delete finalized recordings fully outside the retention window."""
        cutoff = (now if now is not None else time.time()) - self.retention_seconds
        retained_segments: list[dict] = []
        deleted_count = 0
        freed_bytes = 0

        for segment in self._finalized_segments:
            if segment["end_ts"] > cutoff:
                retained_segments.append(segment)
                continue

            path = segment["path"]
            try:
                size = os.path.getsize(path) if os.path.exists(path) else 0
                if os.path.exists(path):
                    os.remove(path)
                deleted_count += 1
                freed_bytes += size
            except OSError as exc:
                log.warning("Could not delete expired recording %s: %s", path, exc)
                retained_segments.append(segment)

        if deleted_count:
            log.info(
                "Pruned %d expired recording segment(s), freed %.2f MB",
                deleted_count,
                freed_bytes / (1024 * 1024),
            )

        self._finalized_segments = retained_segments

    @staticmethod
    def _read_mp4_duration(path: str) -> float | None:
        """Return MP4 duration in seconds using container/stream timestamps."""
        try:
            with av.open(path, "r") as container:
                if container.duration:
                    return float(container.duration) / av.time_base

                durations: list[float] = []
                for stream in container.streams:
                    if stream.duration is not None and stream.time_base is not None:
                        durations.append(float(stream.duration * stream.time_base))
                return max(durations) if durations else None
        except Exception as exc:
            log.warning("Could not read MP4 duration with PyAV (%s): %s", path, exc)
        return None

    def _load_existing_segments(self) -> None:
        """Rebuild the DVR timeline from existing MP4 files after restart."""
        os.makedirs(self.record_path, exist_ok=True)
        loaded_segments: list[dict] = []

        for name in os.listdir(self.record_path):
            if not name.lower().endswith(".mp4"):
                continue

            path = os.path.join(self.record_path, name)
            try:
                started_at = datetime.strptime(
                    os.path.splitext(name)[0],
                    "%Y%m%d_%H%M%S",
                ).timestamp()
            except ValueError:
                log.warning("Skipping recording with unexpected filename: %s", path)
                continue

            duration = self._read_mp4_duration(path)
            if duration is None or duration <= 0:
                log.warning("Skipping unreadable recording: %s", path)
                continue

            loaded_segments.append(
                {
                    "name": name,
                    "path": path,
                    "size": os.path.getsize(path) if os.path.exists(path) else 0,
                    "start_ts": started_at,
                    "end_ts": started_at + duration,
                    "duration": duration,
                }
            )

        self._finalized_segments = sorted(
            loaded_segments,
            key=lambda seg: seg["start_ts"],
        )
        if self._finalized_segments:
            log.info(
                "Loaded %d existing recording segment(s)", len(self._finalized_segments)
            )
        self._prune_expired_segments()

    async def _start_new_segment(
        self, started_at: float, frame_size: tuple[int, int]
    ) -> None:
        """Close the old segment (if any) and open a new av container."""
        if self._av_container:
            await self._close_current_segment(started_at)

        os.makedirs(self.record_path, exist_ok=True)
        self.frame_width, self.frame_height = frame_size

        timestamp_str = datetime.fromtimestamp(started_at).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.record_path, f"{timestamp_str}.mp4")

        container = av.open(path, "w", format="mp4")

        # mjpeg (Motion JPEG) encodes each frame independently — no inter-frame
        # prediction, no motion estimation. This is ~10× faster than mpeg4 at
        # high resolutions and matches the camera's native MJPG capture format.
        # Files are larger than mpeg4 but seek perfectly since every frame is a
        # keyframe. OpenCV and PyAV can both read mjpeg-in-mp4 on all platforms.
        fps_int = max(1, int(round(self.fps)))
        vstream = container.add_stream("mjpeg", rate=fps_int)  # video track
        vstream.width = frame_size[0]
        vstream.height = frame_size[1]
        vstream.pix_fmt = "yuvj420p"
        vstream.time_base = VIDEO_TIME_BASE
        vstream.codec_context.time_base = VIDEO_TIME_BASE

        astream = container.add_stream("aac", rate=AUDIO_SAMPLE_RATE)  # audio track
        astream.layout = "mono" if AUDIO_CHANNELS == 1 else "stereo"

        self._av_container = container
        self._av_video_stream = vstream
        self._av_audio_stream = astream
        self._video_pts = 0
        self._audio_pts = 0
        self._segment_start_ts = started_at
        self._segment_path = path
        log.info("Recording segment: %s", path)

    def _write_video_frame(self, bgr_frame: np.ndarray, captured_at: float) -> None:
        if not self._av_container or not self._av_video_stream or self._segment_start_ts is None:
            return
        vframe = av.VideoFrame.from_ndarray(bgr_frame, format="bgr24")
        vframe = vframe.reformat(format="yuvj420p")
        # Store capture time at 90 kHz precision so playback follows actual
        # frame timing even when capture is irregular.
        elapsed = max(0.0, captured_at - self._segment_start_ts)
        pts = round(elapsed / float(VIDEO_TIME_BASE))
        if pts <= self._video_pts:
            pts = self._video_pts + 1
        self._video_pts = pts
        vframe.pts = pts
        vframe.time_base = VIDEO_TIME_BASE
        for pkt in self._av_video_stream.encode(vframe):
            self._av_container.mux(pkt)

    def _write_audio_frame(self, pcm: np.ndarray) -> None:
        """Write one PCM block (shape: frames×channels) to the av container."""
        if not self._av_container or not self._av_audio_stream:
            return
        # AAC encoder requires fltp (float planar). Convert from int16 directly.
        # Normalise to [-1.0, 1.0] range that float PCM expects.
        samples = pcm.shape[0]
        pcm_float = pcm.astype(np.float32) / 32768.0  # (frames, channels)
        aframe = av.AudioFrame.from_ndarray(
            pcm_float.T,  # (channels, frames) — planar layout
            format="fltp",
            layout="mono" if AUDIO_CHANNELS == 1 else "stereo",
        )
        aframe.pts = self._audio_pts
        aframe.sample_rate = AUDIO_SAMPLE_RATE
        aframe.time_base = Fraction(1, AUDIO_SAMPLE_RATE)
        self._audio_pts += samples
        for pkt in self._av_audio_stream.encode(aframe):
            self._av_container.mux(pkt)

    def _write_frame_and_audio(
        self, frame: np.ndarray, captured_at: float, audio_frames: list[np.ndarray]
    ) -> None:
        """Write one video frame and any batched audio frames. Runs in a thread."""
        self._write_video_frame(frame, captured_at)
        for pcm in audio_frames:
            self._write_audio_frame(pcm)

    def _flush_audio_frames(self, audio_frames: list[np.ndarray]) -> None:
        """Write audio-only batch (used on timeout ticks). Runs in a thread."""
        for pcm in audio_frames:
            self._write_audio_frame(pcm)

    def get_timeline(self) -> dict:
        """Return slider-friendly timeline data built from finalized segments.

        Returned dict shape:
            available         bool    — false until at least one segment exists
            duration          float   — total scrub window in seconds
            window_start_ts   float   — absolute timestamp of earliest segment
            window_end_ts     float   — absolute timestamp of latest segment end
            segments          list    — per-segment metadata with start/end offsets
        """
        if not self._finalized_segments:
            return {
                "available": False,
                "duration": 0.0,
                "window_start_ts": None,
                "window_end_ts": None,
                "segments": [],
            }

        window_start = self._finalized_segments[0]["start_ts"]
        window_end = self._finalized_segments[-1]["end_ts"]

        # start_offset / end_offset are seconds from the start of the full
        # recording window, not from epoch. The peer scrubber works in these
        # relative offsets so it doesn't need to know the wall-clock time.
        segments_with_offsets = [
            {
                **segment,
                "start_offset": segment["start_ts"] - window_start,
                "end_offset": segment["end_ts"] - window_start,
            }
            for segment in self._finalized_segments
        ]

        return {
            "available": True,
            "duration": max(0.0, window_end - window_start),
            "window_start_ts": window_start,
            "window_end_ts": window_end,
            "segments": segments_with_offsets,
        }

    def resolve_playback_offset(
        self, offset_seconds: float
    ) -> tuple[dict, float, float] | None:
        """Translate a slider offset (seconds) into a segment and in-file position.

        Returns:
            (segment_metadata, in_file_offset_seconds, clamped_global_offset)
            or None if no finalized segments exist yet.
        """
        timeline = self.get_timeline()
        segments = timeline["segments"]
        if not segments:
            return None

        total_duration = float(timeline["duration"])
        clamped_offset = max(
            0.0, min(float(offset_seconds), total_duration)
        )  # pattern: max(low, min(value, high))

        for segment in segments:
            if segment["start_offset"] <= clamped_offset <= segment["end_offset"]:
                in_file_offset = max(
                    0.0,
                    min(clamped_offset - segment["start_offset"], segment["duration"]),
                )
                return segment, in_file_offset, clamped_offset

        return None

    def get_next_segment(self, current_segment_path: str) -> dict | None:
        """Return the segment that follows the given file path, if one exists.

        Used by WebcamTrack to cross segment boundaries during playback.
        """
        timeline = self.get_timeline()
        for index, segment in enumerate(timeline["segments"]):
            if segment["path"] == current_segment_path and index + 1 < len(
                timeline["segments"]
            ):
                return timeline["segments"][index + 1]
        return None

    def get_next_segment_after_offset(self, offset_seconds: float) -> dict | None:
        """Return the first segment that starts after the given timeline offset."""
        timeline = self.get_timeline()
        for segment in timeline["segments"]:
            if segment["start_offset"] > offset_seconds:
                return segment
        return None

    async def run(self) -> None:
        """Consume queued frames/audio and write them to rolling MP4 files.

        Must be started as an asyncio task before enqueue_frame() is called.
        """
        self._load_existing_segments()  # rebuild timeline from files on disk

        # maxsize=240 provides ~8 seconds of video buffer at 30 fps.
        self._frame_queue = asyncio.Queue(maxsize=240)
        # maxsize=480 provides ~10 seconds of audio buffer at 48 kHz / 960 samples.
        self._audio_queue = asyncio.Queue(maxsize=480)

        self._is_running = True
        self._ready_event.set()

        while self._is_running:
            try:
                frame, captured_at = await asyncio.wait_for(
                    self._frame_queue.get(), timeout=1.0
                )
                frame_size = (frame.shape[1], frame.shape[0])

                # write frame to disk
                if self._av_container is None:
                    await self._start_new_segment(captured_at, frame_size)
                elif (
                    self._segment_start_ts is not None
                    and captured_at - self._segment_start_ts
                    >= self.segment_duration_seconds
                ):
                    await self._start_new_segment(captured_at, frame_size)
                elif frame_size != (self.frame_width, self.frame_height):
                    log.info(
                        "Frame size changed (%sx%s → %sx%s); rotating segment",
                        self.frame_width,
                        self.frame_height,
                        frame.shape[1],
                        frame.shape[0],
                    )
                    await self._start_new_segment(captured_at, frame_size)

                # Drain all pending audio before writing so audio/video stay
                # as close as possible to wall-clock order in the container.
                audio_frames: list[np.ndarray] = []
                while True:
                    try:
                        audio_frames.append(self._audio_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                if self._av_container:
                    try:
                        await asyncio.to_thread(
                            self._write_frame_and_audio, frame, captured_at, audio_frames
                        )
                        self._write_failures = 0
                    except Exception as exc:
                        self._write_failures += 1
                        log.exception(
                            "Failed to write recording frame (%d/%d): %s",
                            self._write_failures,
                            self._max_write_failures,
                            exc,
                        )
                        await self._abort_current_segment()
                        if self._write_failures >= self._max_write_failures:
                            log.error("Stopping recorder after repeated write failures")
                            self._is_running = False

            except asyncio.TimeoutError:
                # No video frame arrived — still flush any queued audio so
                # the recording stays in sync when the camera briefly stalls.
                audio_frames = []
                while True:
                    try:
                        audio_frames.append(self._audio_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                if audio_frames and self._av_container:
                    try:
                        await asyncio.to_thread(self._flush_audio_frames, audio_frames)
                    except Exception as exc:
                        log.warning("Audio flush error: %s", exc)

    async def stop(self) -> None:
        """Flush the open segment file and stop the recording loop."""
        self._is_running = False
        if self._av_container:
            await self._close_current_segment(time.time())
