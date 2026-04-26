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

import av
import cv2
import numpy as np
import sounddevice as sd
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
        # Tracks the absolute offset (from recording window start) at which the
        # current segment file begins. Added to the in-file position to produce
        # the global scrubber offset reported back to the peer.
        self._playback_base_offset: float                  = 0.0

    @property
    def mode(self) -> str:
        """Current viewer mode: ``"live"`` or ``"playback"``."""
        return self._current_mode

    def current_playback_offset(self) -> float | None:
        """Seconds into the recording window while in playback mode, else None."""
        if self._current_mode != "playback" or not self._playback_capture:
            return None
        in_file_ms = self._playback_capture.get(cv2.CAP_PROP_POS_MSEC)
        return self._playback_base_offset + max(0.0, in_file_ms / 1000.0)

    def seek(self, offset_seconds: float) -> bool:
        """Seek this viewer to a position in the recording window.

        Opens the appropriate segment file and positions the reader at the
        correct in-file byte offset. Returns False if no recordings exist yet.
        """
        resolved = self.recorder.resolve_playback_offset(offset_seconds)
        if not resolved:
            return False

        segment, in_file_offset, global_offset = resolved

        if self._playback_capture:
            self._playback_capture.release()

        self._playback_capture = cv2.VideoCapture(segment["path"])
        if not self._playback_capture.isOpened():
            self._playback_capture = None
            return False

        if in_file_offset > 0:
            self._playback_capture.set(cv2.CAP_PROP_POS_MSEC, in_file_offset * 1000.0)

        self._playback_file_path   = segment["path"]
        # global_offset - in_file_offset gives the absolute position of this
        # segment's start in the full recording window.
        self._playback_base_offset = global_offset - in_file_offset
        self._current_mode         = "playback"
        log.info("Seek to playback: %s @ %.1fs", segment["path"], in_file_offset)
        return True

    def go_live(self) -> None:
        """Switch back to live mode and release all playback resources."""
        if self._playback_capture:
            self._playback_capture.release()
            self._playback_capture = None
        self._playback_file_path   = None
        self._playback_base_offset = 0.0
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

        ret, frame = await asyncio.to_thread(self._playback_capture.read)
        if ret:
            return frame

        # Current segment exhausted — try to continue with the next one.
        next_segment = self.recorder.get_next_segment(self._playback_file_path)
        if not next_segment:
            return None

        self._playback_capture.release()
        self._playback_capture = cv2.VideoCapture(next_segment["path"])
        if not self._playback_capture.isOpened():
            self._playback_capture = None
            self._playback_file_path = None
            return None

        self._playback_file_path   = next_segment["path"]
        self._playback_base_offset = next_segment["start_offset"]
        ret, frame = await asyncio.to_thread(self._playback_capture.read)
        return frame if ret else None

    async def recv(self) -> av.VideoFrame:
        """Return the next video frame to aiortc.

        Called continuously by aiortc's media engine. Falls back to live mode
        automatically when playback reaches the end of all available segments.
        """
        # next_timestamp() must be called every recv() regardless of mode —
        # aiortc uses it to drive the RTP packetizer clock. Skipping it
        # causes timestamp discontinuities that break playback on the peer.
        pts, time_base = await self.next_timestamp()

        if self._current_mode == "playback" and self._playback_capture:
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

    async def stop(self) -> None:
        """Release playback resources when the peer disconnects."""
        self.go_live()


class MicrophoneAudioTrack(AudioStreamTrack):
    """Live microphone audio track.

    Opens the system default input device via sounddevice and pushes raw PCM
    samples into an asyncio queue from the sounddevice callback thread.
    aiortc calls recv() to pull one 20 ms frame at a time, which is wrapped
    in an av.AudioFrame and delivered to the connected peer.
    """

    kind = "audio"

    def __init__(self):
        super().__init__()
        self._audio_queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=50)
        self._event_loop  = asyncio.get_event_loop()
        self._pts         = 0

        self._input_stream = sd.InputStream(
            samplerate = AUDIO_SAMPLE_RATE,
            channels   = AUDIO_CHANNELS,
            dtype      = "int16",
            blocksize  = AUDIO_FRAME_SAMPLES,  # one callback = one 20 ms WebRTC frame
            callback   = self._sounddevice_callback,
        )
        self._input_stream.start()
        log.info("Microphone opened (%d Hz, %d ch)", AUDIO_SAMPLE_RATE, AUDIO_CHANNELS)

    def _enqueue_audio_samples(self, samples: np.ndarray) -> None:
        """Push captured PCM samples into the asyncio queue (called from the sounddevice thread)."""
        try:
            self._audio_queue.put_nowait(samples)
        except asyncio.QueueFull:
            # Drop the frame rather than block the audio capture thread.
            pass

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
            log.warning("Audio capture status: %s", status)
        self._event_loop.call_soon_threadsafe(self._enqueue_audio_samples, indata.copy())

    async def recv(self) -> av.AudioFrame:
        """Deliver one 20 ms PCM frame to aiortc."""
        pcm_data    = await self._audio_queue.get()
        # sounddevice gives shape (frames, channels); av.AudioFrame expects (channels, frames).
        audio_frame = av.AudioFrame.from_ndarray(
            pcm_data.T.astype(np.int16),
            format = "s16",
            layout = "mono" if AUDIO_CHANNELS == 1 else "stereo",
        )
        audio_frame.pts         = self._pts
        audio_frame.sample_rate = AUDIO_SAMPLE_RATE
        audio_frame.time_base   = fractions.Fraction(1, AUDIO_SAMPLE_RATE)
        self._pts += AUDIO_FRAME_SAMPLES
        return audio_frame

    def release(self) -> None:
        """Stop the microphone stream when the peer disconnects."""
        self._input_stream.stop()
        self._input_stream.close()
        log.info("Microphone released")
