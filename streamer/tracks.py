"""
Per-viewer media tracks for live streaming and DVR playback.

WebcamTrack wraps CameraSource for live mode and opens recorded MP4 segment
files directly for playback mode. Segment boundary crossing is handled
transparently so the viewer sees a continuous stream.

MicrophoneAudioTrack reads from the system default microphone via sounddevice
and delivers PCM samples to aiortc at the standard 48 kHz / 20 ms cadence.
"""

import asyncio
import fractions
import logging
import time

import av
import cv2
import numpy as np
from aiortc import AudioStreamTrack, VideoStreamTrack

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
        self.source   = source
        self.recorder = recorder

        self._last_frame_sequence:  int                    = -1
        self._current_mode:         str                    = "live"
        self._playback_capture:     cv2.VideoCapture | None = None
        self._playback_file_path:   str | None             = None
        self._playback_segment_duration: float              = 0.0
        self._playback_frame_count: int                     = 0
        self._playback_started_at:  float                   = 0.0
        self._playback_start_in_file_offset: float          = 0.0
        self._playback_last_frame_index: int                = -1
        self._playback_last_frame: np.ndarray | None        = None
        # Tracks the absolute offset (from recording window start) at which the
        # current segment file begins. Added to the in-file position to produce
        # the global scrubber offset reported back to the peer.
        self._playback_base_offset: float                  = 0.0
        self._gap_base_offset:      float                  = 0.0
        self._gap_started_at:       float                  = 0.0
        self._gap_next_segment:     dict | None            = None

    @property
    def mode(self) -> str:
        """Current viewer mode: ``"live"``, ``"playback"``, or ``"gap"``."""
        return self._current_mode

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

    def _open_playback_segment(self, segment: dict, in_file_offset: float = 0.0) -> bool:
        if self._playback_capture:
            self._playback_capture.release()

        self._playback_capture = cv2.VideoCapture(segment["path"])
        if not self._playback_capture.isOpened():
            self._playback_capture = None
            return False

        self._playback_file_path   = segment["path"]
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
        self._gap_next_segment     = None
        self._current_mode         = "playback"
        return True

    def seek(self, offset_seconds: float) -> bool:
        """Seek this viewer to a position in the recording window.

        Opens the appropriate segment file and positions the reader at the
        correct in-file byte offset. Returns False if no recordings exist yet.
        """
        resolved = self.recorder.resolve_playback_offset(offset_seconds)
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
        self._gap_next_segment     = None
        log.info("Seek to playback: %s @ %.1fs", segment["path"], in_file_offset)
        return True

    def go_live(self) -> None:
        """Switch back to live mode and release all playback resources."""
        if self._playback_capture:
            self._playback_capture.release()
            self._playback_capture = None
        self._playback_file_path   = None
        self._playback_segment_duration = 0.0
        self._playback_frame_count = 0
        self._playback_started_at = 0.0
        self._playback_start_in_file_offset = 0.0
        self._playback_last_frame_index = -1
        self._playback_last_frame = None
        self._playback_base_offset = 0.0
        self._gap_base_offset      = 0.0
        self._gap_started_at       = 0.0
        self._gap_next_segment     = None
        self._current_mode         = "live"
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
        else:
            self._last_frame_sequence, frame, _ = await self.source.get_next_frame(
                self._last_frame_sequence
            )

        # Pass BGR directly - av/FFmpeg converts to YUV in one step, skipping an extra copy.
        video_frame = av.VideoFrame.from_ndarray(frame, format="bgr24")  # type: ignore[arg-type]
        video_frame.pts       = pts
        video_frame.time_base = time_base
        return video_frame

    def stop(self) -> None:
        """Release playback resources when the peer disconnects."""
        self.go_live()


class MicrophoneAudioTrack(AudioStreamTrack):
    """Per-peer audio track fed by the shared microphone source."""

    kind = "audio"

    def __init__(self, source: "MicrophoneSource"):
        super().__init__()
        self._source = source
        self._subscriber_id, self._audio_queue = source.subscribe()
        self._pts = 0

    async def recv(self) -> av.AudioFrame:
        """Deliver one 20 ms PCM frame to aiortc."""
        frame_duration = AUDIO_FRAME_SAMPLES / AUDIO_SAMPLE_RATE
        try:
            pcm_data = await asyncio.wait_for(
                self._audio_queue.get(),
                timeout=max(0.1, frame_duration * 3),
            )
        except asyncio.TimeoutError:
            pcm_data = np.zeros((AUDIO_FRAME_SAMPLES, AUDIO_CHANNELS), dtype=np.int16)

        # sounddevice gives shape (frames, channels); av.AudioFrame expects (channels, frames).
        audio_frame = av.AudioFrame.from_ndarray(
            pcm_data.T.astype(np.int16),
            format="s16",
            layout="mono" if AUDIO_CHANNELS == 1 else "stereo",
        )
        audio_frame.pts         = self._pts
        audio_frame.sample_rate = AUDIO_SAMPLE_RATE
        audio_frame.time_base   = fractions.Fraction(1, AUDIO_SAMPLE_RATE)
        self._pts += AUDIO_FRAME_SAMPLES
        return audio_frame

    def release(self) -> None:
        """Detach this peer from the shared microphone source."""
        self._source.unsubscribe(self._subscriber_id)
        self.stop()


class MicrophoneSource:
    """Single system microphone capture with per-peer fan-out queues.

    PortAudio cannot reliably handle one input stream per WebRTC peer. One
    shared input stream keeps capture load constant while each peer gets its own
    small queue.
    """

    def __init__(self):
        self._event_loop = asyncio.get_event_loop()
        self._subscribers: dict[int, asyncio.Queue[np.ndarray]] = {}
        self._next_subscriber_id = 1
        self._input_stream = None
        self._last_status_log_at = 0.0

    def start(self) -> None:
        """Open the system default microphone once for the whole streamer."""
        if self._input_stream is not None:
            return

        try:
            import sounddevice as sd
        except (ImportError, OSError) as exc:
            raise RuntimeError(
                "Microphone audio requires PortAudio. Install it with "
                "`sudo apt install libportaudio2`, or run with `--no-audio`."
            ) from exc

        self._input_stream = sd.InputStream(
            samplerate = AUDIO_SAMPLE_RATE,
            channels   = AUDIO_CHANNELS,
            dtype      = "int16",
            blocksize  = AUDIO_FRAME_SAMPLES,  # one callback = one 20 ms WebRTC frame
            callback   = self._sounddevice_callback,
        )
        self._input_stream.start()
        log.info("Microphone opened (%d Hz, %d ch)", AUDIO_SAMPLE_RATE, AUDIO_CHANNELS)

    def subscribe(self) -> tuple[int, asyncio.Queue[np.ndarray]]:
        """Create a bounded audio queue for one peer."""
        subscriber_id = self._next_subscriber_id
        self._next_subscriber_id += 1
        queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=10)
        self._subscribers[subscriber_id] = queue
        log.info("Audio subscriber added (count=%d)", len(self._subscribers))
        return subscriber_id, queue

    def unsubscribe(self, subscriber_id: int) -> None:
        """Remove one peer audio queue."""
        self._subscribers.pop(subscriber_id, None)
        log.info("Audio subscriber removed (count=%d)", len(self._subscribers))

    @staticmethod
    def _put_latest(queue: asyncio.Queue[np.ndarray], samples: np.ndarray) -> None:
        """Keep latest audio; drop stale frames when one peer falls behind."""
        try:
            queue.put_nowait(samples)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                queue.put_nowait(samples)
            except asyncio.QueueFull:
                pass

    def _fan_out_audio_samples(self, samples: np.ndarray) -> None:
        """Push captured PCM samples to all peer queues on the asyncio loop."""
        for queue in list(self._subscribers.values()):
            self._put_latest(queue, samples)

    def _sounddevice_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info,
        status,
    ) -> None:
        """sounddevice input callback — executes in a background C thread.

        asyncio queues are not thread-safe; call_soon_threadsafe schedules the
        enqueue onto the event loop thread where the queue lives.
        """
        _ = frames, time_info
        if status:
            now = time.monotonic()
            if now - self._last_status_log_at >= 5.0:
                log.warning("Audio capture status: %s", status)
                self._last_status_log_at = now
        self._event_loop.call_soon_threadsafe(self._fan_out_audio_samples, indata.copy())

    def release(self) -> None:
        """Stop the shared microphone stream on streamer shutdown."""
        self._subscribers.clear()
        if self._input_stream is None:
            return
        self._input_stream.stop()
        self._input_stream.close()
        self._input_stream = None
        log.info("Microphone released")
