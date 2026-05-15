"""
Per-viewer media tracks for live streaming and DVR playback.

WebcamTrack wraps CameraSource for live mode and opens recorded MP4 segment
files directly for playback mode. Segment boundary crossing is handled
transparently so the viewer sees a continuous stream.

DvrAudioTrack follows the paired WebcamTrack's mode:
  - live     → streams live mic audio from MicrophoneSource
  - playback → decodes recorded AAC audio from the same segment file
  - gap      → outputs silence
"""

import asyncio
import collections
import fractions
import logging
import time

import av
import cv2
import numpy as np
from aiortc import AudioStreamTrack, VideoStreamTrack

from audio import MicrophoneSource
from camera import CameraSource
from config import AUDIO_CHANNELS, AUDIO_FRAME_SAMPLES, AUDIO_SAMPLE_RATE
from recording import RecordingManager

log = logging.getLogger("streamer")


class WebcamTrack(VideoStreamTrack):
    """Per-viewer video track that supports live streaming and DVR playback.

    One instance is created per connected peer. In live mode frames come
    directly from the shared CameraSource. In playback mode the track opens
    the recorded MP4 segment files and advances through segment boundaries
    automatically as each file ends.

    Mode transitions:
        seek(offset_seconds) — enter playback mode at a specific position
        gap                  — black-frame playback for missing/offline time
        go_live()            — return to live mode, release playback state
    """

    kind = "video"

    def __init__(self, source: CameraSource, recorder: RecordingManager):
        super().__init__()
        self.source = source
        self.recorder = recorder

        self._last_frame_sequence: int = -1

        # "live"      → camera feed + live mic
        # "playback"  → MP4 file + recorded audio from same file
        # "gap"       → black frame + silence  (recording gap in time)
        self._current_mode: str = "live"

        self._playback_capture: cv2.VideoCapture | None = None
        self._playback_file_path: str | None = None
        self._playback_segment_duration: float = 0.0
        self._playback_frame_count: int = 0
        self._playback_started_at: float = 0.0
        self._playback_start_in_file_offset: float = 0.0
        self._playback_last_frame_index: int = -1
        self._playback_last_frame: np.ndarray | None = None
        # Tracks the absolute offset (from recording window start) at which the
        # current segment file begins. Added to the in-file position to produce
        # the global scrubber offset reported back to the peer.
        self._playback_base_offset: float = 0.0
        self._gap_base_offset: float = 0.0
        self._gap_started_at: float = 0.0
        self._gap_next_segment: dict | None = None

    @property
    def mode(self) -> str:
        """Current viewer mode: ``"live"``, ``"playback"``, or ``"gap"``."""
        return self._current_mode

    def playback_audio_position(self) -> tuple[str | None, float]:
        """Return (file_path, seek_seconds) for DvrAudioTrack to sync to.

        Encapsulates the seek-position calculation so DvrAudioTrack doesn't
        need to reach into private state. Returns (None, 0.0) when not in
        playback mode.
        """
        if self._current_mode != "playback" or not self._playback_file_path:
            return None, 0.0
        elapsed = max(0.0, time.monotonic() - self._playback_started_at)
        seek_pos = self._playback_start_in_file_offset + elapsed
        return self._playback_file_path, seek_pos

    def current_playback_offset(self) -> float | None:
        """Seconds into the recording window while in playback mode, else None."""
        if self._current_mode == "gap":
            return self._current_gap_offset()
        if self._current_mode != "playback" or not self._playback_capture:
            return None
        elapsed = max(0.0, time.monotonic() - self._playback_started_at)
        in_file_offset = min(
            self._playback_segment_duration,
            self._playback_start_in_file_offset + elapsed,
        )
        return self._playback_base_offset + in_file_offset

    def _current_gap_offset(self) -> float:
        if self._current_mode != "gap":
            return 0.0
        return self._gap_base_offset + max(0.0, time.monotonic() - self._gap_started_at)

    def _open_playback_segment(
        self, segment: dict, in_file_offset: float = 0.0
    ) -> bool:
        if self._playback_capture:
            self._playback_capture.release()

        self._playback_capture = cv2.VideoCapture(segment["path"])
        if not self._playback_capture.isOpened():
            self._playback_capture = None
            return False

        self._playback_file_path = segment["path"]
        self._playback_base_offset = segment["start_offset"]
        self._playback_segment_duration = max(0.0, float(segment["duration"]))
        self._playback_frame_count = max(
            1,
            int(self._playback_capture.get(cv2.CAP_PROP_FRAME_COUNT) or 1),
        )
        self._playback_started_at = time.monotonic()
        self._playback_start_in_file_offset = max(
            0.0,
            min(float(in_file_offset), self._playback_segment_duration),
        )
        self._playback_last_frame_index = -1
        self._playback_last_frame = None
        self._gap_next_segment = None
        self._current_mode = "playback"
        return True

    def seek(self, offset_seconds: float) -> bool:
        """Seek this viewer to a position in the recording window.

        Opens the appropriate segment file and positions the reader at the
        correct in-file byte offset. Returns False if no recordings exist yet.
        """
        resolved = self.recorder.resolve_playback_offset(offset_seconds)

        # No recordings yet or offset is out of range
        if not resolved:
            timeline = self.recorder.get_timeline()
            if not timeline["segments"]:
                return False
            total_duration = float(timeline["duration"])
            clamped_offset = max(0.0, min(float(offset_seconds), total_duration))

            if self._playback_capture:
                self._playback_capture.release()
                self._playback_capture = None
            self._playback_file_path = None
            self._playback_base_offset = 0.0
            self._gap_base_offset = clamped_offset
            self._gap_started_at = time.monotonic()
            self._gap_next_segment = self.recorder.get_next_segment_after_offset(
                clamped_offset
            )
            self._current_mode = "gap"
            log.info("Seek to recording gap @ %.1fs", clamped_offset)
            return True

        segment, in_file_offset, global_offset = resolved

        if not self._open_playback_segment(segment, in_file_offset):
            return False

        # global_offset - in_file_offset gives the absolute position of this
        # segment's start in the full recording window.
        self._playback_base_offset = global_offset - in_file_offset
        self._gap_next_segment = None
        log.info("Seek to playback: %s @ %.1fs", segment["path"], in_file_offset)
        return True

    def go_live(self) -> None:
        """Switch back to live mode and release all playback resources."""
        if self._playback_capture:
            self._playback_capture.release()
            self._playback_capture = None
        self._playback_file_path = None
        self._playback_segment_duration = 0.0
        self._playback_frame_count = 0
        self._playback_started_at = 0.0
        self._playback_start_in_file_offset = 0.0
        self._playback_last_frame_index = -1
        self._playback_last_frame = None
        self._playback_base_offset = 0.0
        self._gap_base_offset = 0.0
        self._gap_started_at = 0.0
        self._gap_next_segment = None
        self._current_mode = "live"
        log.info("Switched to live mode")

    async def _read_next_playback_frame(self) -> np.ndarray | None:
        """Read the next frame from the active playback file.

        When the current file ends, automatically opens the next segment so
        the viewer sees a continuous stream across segment boundaries.
        Returns None only when all segments have been exhausted.
        """
        if not self._playback_capture or not self._playback_file_path:
            return None

        elapsed = max(0.0, time.monotonic() - self._playback_started_at)
        in_file_offset = self._playback_start_in_file_offset + elapsed
        if in_file_offset < self._playback_segment_duration:
            target_frame_index = int(
                (in_file_offset / self._playback_segment_duration)
                * self._playback_frame_count
            )
            target_frame_index = max(
                0,
                min(target_frame_index, self._playback_frame_count - 1),
            )

            if (
                target_frame_index == self._playback_last_frame_index
                and self._playback_last_frame is not None
            ):
                return self._playback_last_frame.copy()

            if target_frame_index != self._playback_last_frame_index + 1:
                self._playback_capture.set(cv2.CAP_PROP_POS_FRAMES, target_frame_index)
            ret, frame = await asyncio.to_thread(self._playback_capture.read)
            if ret:
                self._playback_last_frame_index = target_frame_index
                self._playback_last_frame = frame
                return frame

        # Current segment exhausted — try to continue with the next one.
        next_segment = self.recorder.get_next_segment(self._playback_file_path)
        if not next_segment:
            return None

        current_offset = self.current_playback_offset()
        current_segment_end = next_segment["start_offset"]
        timeline = self.recorder.get_timeline()
        for segment in timeline["segments"]:
            if segment["path"] == self._playback_file_path:
                current_segment_end = segment["end_offset"]
                break

        if next_segment["start_offset"] > current_segment_end + 0.25:
            self._playback_capture.release()
            self._playback_capture = None
            self._playback_file_path = None
            self._playback_segment_duration = 0.0
            self._playback_frame_count = 0
            self._playback_started_at = 0.0
            self._playback_start_in_file_offset = 0.0
            self._playback_last_frame_index = -1
            self._playback_last_frame = None
            self._playback_base_offset = 0.0
            self._gap_base_offset = current_offset or current_segment_end
            self._gap_started_at = time.monotonic()
            self._gap_next_segment = next_segment
            self._current_mode = "gap"
            log.info("Playback entered recording gap @ %.1fs", self._gap_base_offset)
            return self._gap_frame()

        if not self._open_playback_segment(next_segment, 0.0):
            return None

        return await self._read_next_playback_frame()

    def _gap_frame(self) -> np.ndarray:
        frame = np.zeros(
            (self.source.capture_height, self.source.capture_width, 3),
            dtype=np.uint8,
        )
        message = "No recording available"
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = max(0.8, self.source.capture_width / 1600)
        thickness = 2
        (text_w, text_h), _ = cv2.getTextSize(message, font, scale, thickness)
        x = max(20, (self.source.capture_width - text_w) // 2)
        y = max(40, (self.source.capture_height + text_h) // 2)
        cv2.putText(
            frame,
            message,
            (x, y),
            font,
            scale,
            (220, 220, 220),
            thickness,
            cv2.LINE_AA,
        )
        return frame

    async def _read_next_gap_frame(self) -> np.ndarray:
        current_offset = self._current_gap_offset()
        next_segment = self._gap_next_segment
        if next_segment and current_offset >= next_segment["start_offset"]:
            if self._open_playback_segment(next_segment, 0.0):
                frame = await self._read_next_playback_frame()
                if frame is not None:
                    return frame
            self._gap_next_segment = self.recorder.get_next_segment_after_offset(
                current_offset
            )
        return self._gap_frame()

    async def recv(self) -> av.VideoFrame:
        """Return the next video frame to aiortc.

        Called continuously by aiortc's media engine. Falls back to live mode
        automatically when playback reaches the end of all available segments.
        """
        # next_timestamp() must be called every recv() regardless of mode —
        # aiortc uses it to drive the RTP packetizer clock. Skipping it
        # causes timestamp discontinuities that break playback on the peer.
        pts, time_base = await self.next_timestamp()

        if self._current_mode == "gap":
            frame = await self._read_next_gap_frame()
        elif self._current_mode == "playback" and self._playback_capture:
            frame = await self._read_next_playback_frame()
            if frame is None:
                # Reached end of all recorded segments — fall back to live.
                self.go_live()
                self._last_frame_sequence, frame, _ = await self.source.get_next_frame(
                    self._last_frame_sequence
                )
        else:  # live
            self._last_frame_sequence, frame, _ = await self.source.get_next_frame(
                self._last_frame_sequence  # blocks until newer frame arrives
            )

        # Pass BGR directly - av/FFmpeg converts to YUV in one step, skipping an extra copy.
        video_frame = av.VideoFrame.from_ndarray(frame, format="bgr24")  # type: ignore[arg-type]
        video_frame.pts = pts
        video_frame.time_base = time_base
        return video_frame

    def stop(self) -> None:
        """Release playback resources when the peer disconnects."""
        self.go_live()


class DvrAudioTrack(AudioStreamTrack):
    """Per-peer audio track that follows the paired WebcamTrack's playback mode.

    - live     → streams live mic audio from MicrophoneSource queue
    - playback → decodes recorded AAC audio from the current segment file
    - gap      → outputs silence at the correct cadence

    Segment boundary crossings are detected by watching
    video_track._playback_file_path; when it changes the audio container
    is re-opened at the correct in-file seek position so audio stays in sync.

    Old recordings without an audio stream are handled gracefully by returning
    silence, so existing files never cause errors.
    """

    kind = "audio"

    def __init__(self, source: "MicrophoneSource", video_track: WebcamTrack):
        super().__init__()
        self._source = source
        self._video_track = video_track
        self._subscriber_id, self._mic_queue = source.subscribe()
        self._pts = 0

        # Playback pacing — wall-clock time when the next frame should be sent.
        # Zero means "not yet initialised" (reset on every non-playback mode).
        self._pb_wake_time: float = 0.0

        # Playback audio state
        self._pb_container: av.container.InputContainer | None = None
        self._pb_resampler: av.audio.resampler.AudioResampler | None = None
        self._pb_decoder = None  # iterator from container.decode(audio=0)
        self._pb_fifo: collections.deque = collections.deque()
        self._pb_path: str | None = None  # path of currently open container

    def _open_audio_segment(self, path: str, seek_seconds: float) -> None:
        """Open an av input container for audio playback. Called in a thread."""
        self._close_audio_segment()
        try:
            container = av.open(path, "r")
        except Exception as exc:
            log.warning("Cannot open audio segment %s: %s", path, exc)
            self._pb_path = path  # mark so we don't retry on every recv()
            return

        audio_streams = [s for s in container.streams if s.type == "audio"]
        if not audio_streams:
            # Recording has no audio track (e.g. pre-audio legacy files).
            container.close()
            self._pb_path = path
            return

        if seek_seconds > 0.05:
            try:
                # av.open uses AV_TIME_BASE (microseconds) for the default seek.
                seek_us = int(seek_seconds * 1_000_000)
                container.seek(seek_us)
            except Exception as exc:
                log.debug(
                    "Audio seek failed for %s at %.2fs: %s", path, seek_seconds, exc
                )

        layout = "mono" if AUDIO_CHANNELS == 1 else "stereo"
        resampler = av.audio.resampler.AudioResampler(
            format="s16p",
            layout=layout,
            rate=AUDIO_SAMPLE_RATE,
            frame_size=AUDIO_FRAME_SAMPLES,
        )

        self._pb_container = container
        self._pb_resampler = resampler
        self._pb_decoder = container.decode(audio=0)
        self._pb_fifo.clear()
        self._pb_path = path
        log.debug("Audio playback opened: %s @ %.2fs", path, seek_seconds)

    def _close_audio_segment(self) -> None:
        if self._pb_container is not None:
            try:
                self._pb_container.close()
            except Exception:
                pass
        self._pb_container = None
        self._pb_resampler = None
        self._pb_decoder = None
        self._pb_fifo.clear()
        self._pb_path = None

    def _make_frame(self, pcm_t: np.ndarray) -> av.AudioFrame:
        """Wrap a (channels, samples) int16 array into a timestamped AudioFrame."""
        frame = av.AudioFrame.from_ndarray(
            pcm_t,
            format="s16",
            layout="mono" if AUDIO_CHANNELS == 1 else "stereo",
        )
        frame.pts = self._pts
        frame.sample_rate = AUDIO_SAMPLE_RATE
        frame.time_base = fractions.Fraction(1, AUDIO_SAMPLE_RATE)
        self._pts += AUDIO_FRAME_SAMPLES
        return frame

    def _silence_frame(self) -> av.AudioFrame:
        return self._make_frame(
            np.zeros((AUDIO_CHANNELS, AUDIO_FRAME_SAMPLES), dtype=np.int16)
        )

    def _decode_next_into_fifo(self) -> bool:
        """Decode one packet and push resampled frames into _pb_fifo.

        Returns True if frames were produced, False on EOF or error.
        Runs synchronously — caller must use asyncio.to_thread.
        """
        if self._pb_decoder is None or self._pb_resampler is None:
            return False
        try:
            raw = next(self._pb_decoder, None)
            if raw is None:
                return False
            for rf in self._pb_resampler.resample(raw):
                self._pb_fifo.append(rf.to_ndarray())
            return True
        except Exception as exc:
            log.debug("Audio decode error: %s", exc)
            return False

    async def _recv_playback(self) -> av.AudioFrame:
        current_path, seek_pos = self._video_track.playback_audio_position()

        # Re-open if the video track switched to a different segment.
        if current_path != self._pb_path:
            if current_path:
                await asyncio.to_thread(
                    self._open_audio_segment, current_path, seek_pos
                )
            else:
                self._close_audio_segment()
                return self._silence_frame()

        # No usable container (e.g. legacy file without audio).
        if self._pb_container is None:
            return self._silence_frame()

        # Fill fifo if needed, then pop one frame.
        while not self._pb_fifo:
            produced = await asyncio.to_thread(self._decode_next_into_fifo)
            if not produced:
                return self._silence_frame()

        return self._make_frame(self._pb_fifo.popleft())

    async def recv(self) -> av.AudioFrame:
        """Return the next audio frame to aiortc based on the video track's mode."""
        mode = self._video_track.mode  # reads video track's mode every call

        if mode == "live":
            # pull from mic queue
            self._pb_wake_time = 0.0
            if self._pb_container is not None:
                self._close_audio_segment()
            frame_duration = AUDIO_FRAME_SAMPLES / AUDIO_SAMPLE_RATE
            try:
                pcm_data = await asyncio.wait_for(
                    self._mic_queue.get(),
                    timeout=max(0.1, frame_duration * 3),
                )
            except asyncio.TimeoutError:
                pcm_data = np.zeros(
                    (AUDIO_FRAME_SAMPLES, AUDIO_CHANNELS), dtype=np.int16
                )
            # sounddevice gives (frames, channels); _make_frame wants (channels, frames)
            return self._make_frame(pcm_data.T.astype(np.int16))

        elif mode == "playback":
            # Pace to 20 ms per frame so we don't flood the peer's jitter buffer.
            # Without this, recv() returns instantly and the sender emits hundreds
            # of packets/second — the receiver drops them all and hears silence.
            now = time.monotonic()
            if self._pb_wake_time == 0.0:
                self._pb_wake_time = now
            sleep_for = self._pb_wake_time - now
            if sleep_for > 0.001:
                await asyncio.sleep(sleep_for)
            # If we ran late (segment open, seek, etc.) reset to now to avoid
            # accumulating debt that would produce a burst of frames later.
            self._pb_wake_time = (
                max(time.monotonic(), self._pb_wake_time)
                + AUDIO_FRAME_SAMPLES / AUDIO_SAMPLE_RATE
            )
            return await self._recv_playback()

        else:  # gap
            self._pb_wake_time = 0.0
            if self._pb_container is not None:
                self._close_audio_segment()
            # Pace silence to match the WebRTC audio cadence (20 ms per frame).
            await asyncio.sleep(AUDIO_FRAME_SAMPLES / AUDIO_SAMPLE_RATE)
            return self._silence_frame()

    def release(self) -> None:
        """Detach from mic source and release playback resources."""
        self._source.unsubscribe(self._subscriber_id)
        self._close_audio_segment()
        self.stop()


