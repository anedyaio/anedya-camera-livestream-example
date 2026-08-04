"""
Microphone capture pipeline shared across all viewer sessions.

MicrophoneSource opens the system microphone once and fans out PCM audio
to two consumers simultaneously:
  1. RecordingManager — for writing audio into rolling MP4 segments on disk.
  2. Per-peer asyncio queues — one per active viewer, for live streaming.

Audio is captured via sounddevice (PortAudio bindings) in a background C
thread. Samples are forwarded to the asyncio event loop via
call_soon_threadsafe so queue operations stay thread-safe.
"""

import asyncio
import logging
import time

import numpy as np

from config import AUDIO_CHANNELS, AUDIO_FRAME_SAMPLES, AUDIO_SAMPLE_RATE
from recording import RecordingManager

log = logging.getLogger("streamer")


class MicrophoneSource:
    """Single system microphone capture with per-peer fan-out queues.

    PortAudio cannot reliably handle one input stream per WebRTC peer. One
    shared input stream keeps capture load constant while each peer gets its own
    small queue.

    Optionally accepts a RecordingManager so the same PCM blocks are pushed
    to disk alongside the video frames.
    """

    def __init__(self, recorder: RecordingManager | None = None):
        self._event_loop = asyncio.get_event_loop()

        # int = subscriber ID, `Queue` = that viewer's audio buffer.
        self._subscribers: dict[int, asyncio.Queue[np.ndarray]] = {}

        self._next_subscriber_id = 1
        self._input_stream = None
        self._last_status_log_at = 0.0
        self._recorder = recorder

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

        try:
            self._input_stream = sd.InputStream(
                samplerate=AUDIO_SAMPLE_RATE,
                channels=AUDIO_CHANNELS,
                dtype="int16",
                blocksize=AUDIO_FRAME_SAMPLES,  # exactly one 20ms WebRTC frame per callback
                callback=self._sounddevice_callback,
            )
            self._input_stream.start()
            log.info("Microphone opened (%d Hz, %d ch)", AUDIO_SAMPLE_RATE, AUDIO_CHANNELS)
        except Exception as exc:
            raise RuntimeError(
                f"No default audio input device (microphone) found on host: {exc}"
            ) from exc

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
            queue.put_nowait(samples)  # normal case: queue has space
        except asyncio.QueueFull:
            try:
                queue.get_nowait()  # drop the oldest frame
            except asyncio.QueueEmpty:
                return
            try:
                queue.put_nowait(samples)  # put the new one in
            except asyncio.QueueFull:
                pass

    def _fan_out_audio_samples(self, samples: np.ndarray) -> None:
        """Push captured PCM samples to all peer queues and the recorder."""
        for queue in list(self._subscribers.values()):
            self._put_latest(queue, samples)
        if self._recorder is not None:
            self._recorder.enqueue_audio(samples)

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
        self._event_loop.call_soon_threadsafe(
            self._fan_out_audio_samples, indata.copy()
        )

    def release(self) -> None:
        """Stop the shared microphone stream on streamer shutdown."""
        self._subscribers.clear()
        if self._input_stream is None:
            return
        self._input_stream.stop()
        self._input_stream.close()
        self._input_stream = None
        log.info("Microphone released")
