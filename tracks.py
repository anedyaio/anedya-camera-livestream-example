"""
Per-viewer media tracks for live streaming and DVR playback.

WebcamTrack wraps CameraSource for live mode and opens recorded MP4 segment
files with PyAV for playback mode. Segment boundary crossing is handled
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


class PeerPlaybackSession:
    """Per-peer PyAV playback state for recorded video and audio.

    One object owns the active segment, seek position, video decoder, audio
    decoder, and segment transitions for a viewer. Video and audio use separate
    PyAV containers because aiortc calls their recv() methods independently.
    """

    def __init__(self, recorder: RecordingManager) -> None:
        self._recorder = recorder
        self._lock = asyncio.Lock()

        self._video_container: av.container.InputContainer | None = None
        self._video_decoder = None
        self._audio_container: av.container.InputContainer | None = None
        self._audio_decoder = None
        self._audio_resampler: av.audio.resampler.AudioResampler | None = None
        self._audio_fifo: collections.deque = collections.deque()

        self._segment: dict | None = None
        self._path: str | None = None
        self._duration: float = 0.0
        self._target_time: float = 0.0
        self._base_offset: float = 0.0
        self._last_media_time: float | None = None
        self._clock_wall_start: float | None = None
        self._clock_media_start: float | None = None
        self._audio_seek_seconds: float = 0.0
        self._audio_discard_until_seek: bool = False
        self._audio_available: bool = False

    @property
    def path(self) -> str | None:
        return self._path

    @property
    def base_offset(self) -> float:
        return self._base_offset

    async def open(self, segment: dict, in_file_offset: float = 0.0) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._open_sync, segment, in_file_offset)

    def _open_sync(self, segment: dict, in_file_offset: float = 0.0) -> bool:
        self._close_sync()
        path = segment["path"]
        try:
            video_container = av.open(path, "r")
        except Exception as exc:
            log.warning("Cannot open video segment %s: %s", path, exc)
            return False

        video_streams = [
            stream for stream in video_container.streams if stream.type == "video"
        ]
        if not video_streams:
            video_container.close()
            log.warning("Recording segment has no video stream: %s", path)
            return False

        video_stream = video_streams[0]
        duration = max(0.0, float(segment.get("duration") or 0.0))
        target_time = max(0.0, min(float(in_file_offset), duration))
        if target_time > 0.001:
            try:
                seek_pts = int(target_time / float(video_stream.time_base))
                video_container.seek(
                    seek_pts,
                    stream=video_stream,
                    backward=True,
                    any_frame=False,
                )
            except Exception as exc:
                log.debug(
                    "Video seek failed for %s at %.2fs: %s",
                    path,
                    target_time,
                    exc,
                )

        self._video_container = video_container
        self._video_decoder = video_container.decode(video=0)
        self._open_audio_sync(path, target_time)
        self._segment = segment
        self._path = path
        self._duration = duration
        self._target_time = target_time
        self._base_offset = float(segment.get("start_offset") or 0.0)
        self._last_media_time = None
        self._clock_wall_start = None
        self._clock_media_start = None
        log.debug("Video playback opened: %s @ %.2fs", path, target_time)
        return True

    def _open_audio_sync(self, path: str, seek_seconds: float) -> None:
        try:
            audio_container = av.open(path, "r")
        except Exception as exc:
            log.warning("Cannot open audio segment %s: %s", path, exc)
            return

        audio_streams = [
            stream for stream in audio_container.streams if stream.type == "audio"
        ]
        if not audio_streams:
            audio_container.close()
            log.debug("Recording segment has no audio stream: %s", path)
            return

        audio_stream = audio_streams[0]
        seek_seconds = max(0.0, float(seek_seconds))
        if seek_seconds > 0.05:
            try:
                seek_pts = int(seek_seconds / float(audio_stream.time_base))
                audio_container.seek(
                    seek_pts,
                    stream=audio_stream,
                    backward=True,
                    any_frame=False,
                )
            except Exception as exc:
                log.debug(
                    "Audio seek failed for %s at %.2fs: %s", path, seek_seconds, exc
                )

        layout = "mono" if AUDIO_CHANNELS == 1 else "stereo"
        self._audio_container = audio_container
        self._audio_decoder = audio_container.decode(audio=0)
        self._audio_resampler = av.audio.resampler.AudioResampler(
            format="s16p",
            layout=layout,
            rate=AUDIO_SAMPLE_RATE,
            frame_size=AUDIO_FRAME_SAMPLES,
        )
        self._audio_fifo.clear()
        self._audio_seek_seconds = seek_seconds
        self._audio_discard_until_seek = seek_seconds > 0.05
        self._audio_available = True
        log.debug("Audio playback opened: %s @ %.2fs", path, seek_seconds)

    async def close(self) -> None:
        async with self._lock:
            self._close_sync()

    def close_now(self) -> None:
        """Synchronous close for aiortc stop()/peer teardown paths."""
        self._close_sync()

    def _close_sync(self) -> None:
        if self._video_container is not None:
            try:
                self._video_container.close()
            except Exception:
                pass
        if self._audio_container is not None:
            try:
                self._audio_container.close()
            except Exception:
                pass
        self._video_container = None
        self._video_decoder = None
        self._audio_container = None
        self._audio_decoder = None
        self._audio_resampler = None
        self._audio_fifo.clear()
        self._segment = None
        self._path = None
        self._duration = 0.0
        self._target_time = 0.0
        self._base_offset = 0.0
        self._last_media_time = None
        self._clock_wall_start = None
        self._clock_media_start = None
        self._audio_seek_seconds = 0.0
        self._audio_discard_until_seek = False
        self._audio_available = False

    def current_media_time(self) -> float:
        if self._last_media_time is not None:
            return min(self._duration, self._last_media_time)
        return self._target_time

    def current_playback_offset(self) -> float | None:
        if not self._path:
            return None
        return self._base_offset + self.current_media_time()

    @staticmethod
    def _frame_time(frame: av.VideoFrame) -> float | None:
        if frame.time is not None:
            return float(frame.time)
        if frame.pts is not None and frame.time_base is not None:
            return float(frame.pts * frame.time_base)
        return None

    def _decode_next_frame(self) -> tuple[av.VideoFrame, float] | None:
        if self._video_decoder is None:
            return None

        while True:
            try:
                frame = next(self._video_decoder)
            except StopIteration:
                return None
            except Exception as exc:
                log.debug("Video decode error: %s", exc)
                return None

            media_time = self._frame_time(frame)
            if media_time is None:
                media_time = self._last_media_time or self._target_time

            # Timestamp seek can land before the target keyframe. Decode forward
            # until the first frame that belongs to the requested position.
            if (
                self._last_media_time is None
                and media_time + 0.001 < self._target_time
            ):
                continue
            return frame, media_time

    async def next_video_frame(
        self,
    ) -> tuple[str, np.ndarray | None, float | None, dict | None]:
        """Return the next video frame, or signal gap/eof after segment end."""
        while True:
            async with self._lock:
                decoded = await asyncio.to_thread(self._decode_next_frame)
                if decoded is None:
                    result = await asyncio.to_thread(self._advance_after_video_eof_sync)
                    if result[0] == "retry":
                        continue
                    return result

                frame, media_time = decoded
                sleep_for = 0.0
                if self._clock_wall_start is None or self._clock_media_start is None:
                    self._clock_wall_start = time.monotonic()
                    self._clock_media_start = media_time
                else:
                    sleep_until = self._clock_wall_start + (
                        media_time - self._clock_media_start
                    )
                    sleep_for = sleep_until - time.monotonic()

                self._last_media_time = media_time

            if sleep_for > 0.001:
                await asyncio.sleep(sleep_for)
            return "frame", frame.to_ndarray(format="bgr24"), None, None

    def _advance_after_video_eof_sync(
        self,
    ) -> tuple[str, np.ndarray | None, float | None, dict | None]:
        current_path = self._path
        current_segment = self._segment
        if not current_path or not current_segment:
            return "eof", None, None, None

        next_segment = self._recorder.get_next_segment(current_path)
        if not next_segment:
            return "eof", None, None, None

        current_offset = self.current_playback_offset()
        current_segment_end = float(current_segment.get("end_offset") or 0.0)
        next_start = float(next_segment.get("start_offset") or 0.0)
        if next_start > current_segment_end + 0.25:
            gap_offset = current_offset if current_offset is not None else current_segment_end
            self._close_sync()
            return "gap", None, gap_offset, next_segment

        if not self._open_sync(next_segment, 0.0):
            return "eof", None, None, None

        return "retry", None, None, None

    def _decode_next_audio_pcm(self) -> np.ndarray | None:
        if not self._audio_available or self._audio_decoder is None:
            return None
        if self._audio_resampler is None:
            return None

        while not self._audio_fifo:
            try:
                raw = next(self._audio_decoder, None)
            except Exception as exc:
                log.debug("Audio decode error: %s", exc)
                return None
            if raw is None:
                return None

            for rf in self._audio_resampler.resample(raw):
                frame_time = (
                    float(rf.pts * rf.time_base) if rf.pts is not None else None
                )
                if (
                    self._audio_discard_until_seek
                    and frame_time is not None
                    and frame_time + rf.samples / rf.sample_rate < self._audio_seek_seconds
                ):
                    continue
                self._audio_discard_until_seek = False
                self._audio_fifo.append(rf.to_ndarray())

        return self._audio_fifo.popleft()

    async def next_audio_pcm(self) -> np.ndarray | None:
        """Return one resampled playback audio frame, or None for silence."""
        async with self._lock:
            return await asyncio.to_thread(self._decode_next_audio_pcm)


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

        self._playback = PeerPlaybackSession(recorder)
        self._gap_base_offset: float = 0.0
        self._gap_started_at: float = 0.0
        self._gap_next_segment: dict | None = None

    @property
    def mode(self) -> str:
        """Current viewer mode: ``"live"``, ``"playback"``, or ``"gap"``."""
        return self._current_mode

    @property
    def playback_session(self) -> PeerPlaybackSession:
        """Shared recorded playback session used by paired audio/video tracks."""
        return self._playback

    def current_playback_offset(self) -> float | None:
        """Seconds into the recording window while in playback mode, else None."""
        if self._current_mode == "gap":
            return self._current_gap_offset()
        if self._current_mode != "playback":
            return None
        return self._playback.current_playback_offset()

    def _current_gap_offset(self) -> float:
        if self._current_mode != "gap":
            return 0.0
        return self._gap_base_offset + max(0.0, time.monotonic() - self._gap_started_at)

    async def _open_playback_segment(
        self, segment: dict, in_file_offset: float = 0.0
    ) -> bool:
        if not await self._playback.open(segment, in_file_offset):
            return False

        self._gap_next_segment = None
        self._current_mode = "playback"
        return True

    async def seek(self, offset_seconds: float) -> bool:
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

            await self._playback.close()
            self._gap_base_offset = clamped_offset
            self._gap_started_at = time.monotonic()
            self._gap_next_segment = self.recorder.get_next_segment_after_offset(
                clamped_offset
            )
            self._current_mode = "gap"
            log.info("Seek to recording gap @ %.1fs", clamped_offset)
            return True

        segment, in_file_offset, _global_offset = resolved

        if not await self._open_playback_segment(segment, in_file_offset):
            return False

        self._gap_next_segment = None
        log.info("Seek to playback: %s @ %.1fs", segment["path"], in_file_offset)
        return True

    async def go_live(self) -> None:
        """Switch back to live mode and release all playback resources."""
        await self._playback.close()
        self._gap_base_offset = 0.0
        self._gap_started_at = 0.0
        self._gap_next_segment = None
        self._current_mode = "live"
        log.info("Switched to live mode")

    async def _read_next_playback_frame(self) -> np.ndarray | None:
        """Read the next frame from the active playback session."""
        status, frame, gap_offset, next_segment = await self._playback.next_video_frame()
        if status == "frame" and frame is not None:
            return frame

        if status == "gap":
            await self._playback.close()
            self._gap_base_offset = gap_offset or 0.0
            self._gap_started_at = time.monotonic()
            self._gap_next_segment = next_segment
            self._current_mode = "gap"
            log.info("Playback entered recording gap @ %.1fs", self._gap_base_offset)
            return self._gap_frame()

        return None

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
            if await self._open_playback_segment(next_segment, 0.0):
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
        elif self._current_mode == "playback" and self._playback.path:
            frame = await self._read_next_playback_frame()
            if frame is None:
                # Reached end of all recorded segments — fall back to live.
                await self.go_live()
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
        self._playback.close_now()
        self._current_mode = "live"
        super().stop()


class DvrAudioTrack(AudioStreamTrack):
    """Per-peer audio track that follows the paired WebcamTrack mode.

    Live mode reads from the shared microphone queue. Playback mode delegates to
    the same PeerPlaybackSession used by video, so segment path, seek position,
    and segment crossing are owned in one place. Segments without audio return
    silence instead of failing.
    """

    kind = "audio"

    def __init__(self, source: "MicrophoneSource", video_track: WebcamTrack):
        super().__init__()
        self._source = source
        self._video_track = video_track
        self._subscriber_id, self._mic_queue = source.subscribe()
        self._pts = 0

        # Playback pacing: wall-clock time when the next frame should be sent.
        self._pb_wake_time: float = 0.0

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

    async def _recv_playback(self) -> av.AudioFrame:
        pcm = await self._video_track.playback_session.next_audio_pcm()
        if pcm is None:
            return self._silence_frame()
        return self._make_frame(pcm.astype(np.int16, copy=False))

    async def recv(self) -> av.AudioFrame:
        """Return the next audio frame to aiortc based on the video track mode."""
        mode = self._video_track.mode

        if mode == "live":
            self._pb_wake_time = 0.0
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
            return self._make_frame(pcm_data.T.astype(np.int16))

        if mode == "playback":
            # Pace to 20 ms per frame so we don't flood the peer jitter buffer.
            now = time.monotonic()
            if self._pb_wake_time == 0.0:
                self._pb_wake_time = now
            sleep_for = self._pb_wake_time - now
            if sleep_for > 0.001:
                await asyncio.sleep(sleep_for)
            self._pb_wake_time = (
                max(time.monotonic(), self._pb_wake_time)
                + AUDIO_FRAME_SAMPLES / AUDIO_SAMPLE_RATE
            )
            return await self._recv_playback()

        self._pb_wake_time = 0.0
        await asyncio.sleep(AUDIO_FRAME_SAMPLES / AUDIO_SAMPLE_RATE)
        return self._silence_frame()

    def release(self) -> None:
        """Detach from mic source and release track resources."""
        self._source.unsubscribe(self._subscriber_id)
        self.stop()
