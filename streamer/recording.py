"""
Rolling MP4 segment recorder.

RecordingManager runs as a background asyncio task. It consumes frames
queued by CameraSource and writes them into fixed-length MP4 files on disk.
It also maintains a list of finalized segments that the playback system
uses to serve DVR scrubbing to viewers.

Segment lifecycle:
    1. CameraSource calls enqueue_frame() with each captured frame.
    2. The run() loop pulls frames off the queue and writes them to the
       current segment file via OpenCV VideoWriter.
    3. Every SEGMENT_DURATION_SECONDS the writer is flushed and a new file
       is opened. The closed file is added to _finalized_segments.
    4. Peers call get_timeline() and resolve_playback_offset() to map slider
       positions back to segment files and in-file seek positions.
"""

import asyncio
import logging
import os
import time
from datetime import datetime

import cv2
import numpy as np

log = logging.getLogger("streamer")


class RecordingManager:
    """Write rolling MP4 segments to disk and expose timeline metadata."""

    SEGMENT_DURATION_SECONDS = 5

    def __init__(self, record_path: str = "recordings", fps: float = 30.0):
        self.record_path = record_path
        self.fps         = fps

        self.frame_width:  int | None = None
        self.frame_height: int | None = None

        self._video_writer:     cv2.VideoWriter | None = None
        self._segment_start_ts: float | None           = None
        self._segment_path:     str | None             = None

        self._frame_queue: asyncio.Queue | None = None
        self._is_running   = False

        self._ready_event = asyncio.Event()

        # Only closed (fully written) segments are listed here. The in-progress
        # segment is not added until _close_current_segment() is called because
        # partially-written MP4 files cannot be seeked reliably.
        self._finalized_segments: list[dict] = []

    async def wait_until_ready(self) -> None:
        """Block until the background recording loop has started."""
        await self._ready_event.wait()

    def enqueue_frame(self, frame: np.ndarray, captured_at: float) -> None:
        """Queue a frame for disk writing (called from CameraSource).

        Non-blocking — frames are dropped if the queue is full rather than
        stalling the live capture pipeline.
        """
        if self._frame_queue is None:
            return
        try:
            self._frame_queue.put_nowait((frame, captured_at))
        except asyncio.QueueFull:
            pass

    def _close_current_segment(self, closed_at: float) -> None:
        """Flush the current VideoWriter and register the segment metadata."""
        if not self._video_writer or self._segment_start_ts is None or self._segment_path is None:
            return

        self._video_writer.release()
        self._video_writer = None

        duration = max(0.0, closed_at - self._segment_start_ts)
        if duration <= 0:
            return

        path = self._segment_path
        self._finalized_segments.append({
            "name":     os.path.basename(path),
            "path":     path,
            "size":     os.path.getsize(path) if os.path.exists(path) else 0,
            "start_ts": self._segment_start_ts,
            "end_ts":   closed_at,
            "duration": duration,
        })
        self._finalized_segments.sort(key=lambda seg: seg["start_ts"])

    def _start_new_segment(self, started_at: float, frame_size: tuple[int, int]) -> None:
        """Close the old segment (if any) and open a new VideoWriter."""
        if self._video_writer:
            self._close_current_segment(started_at)

        os.makedirs(self.record_path, exist_ok=True)
        self.frame_width, self.frame_height = frame_size

        timestamp_str = datetime.fromtimestamp(started_at).strftime("%Y%m%d_%H%M%S")
        path          = os.path.join(self.record_path, f"{timestamp_str}.mp4")
        # mp4v is universally supported by OpenCV on all platforms including
        # Raspberry Pi OS. H.264 (avc1) requires a separate codec license on Pi.
        fourcc        = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore[attr-defined]

        self._video_writer     = cv2.VideoWriter(path, fourcc, self.fps, frame_size)
        self._segment_start_ts = started_at
        self._segment_path     = path
        log.info("Recording segment: %s", path)

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
                "available":       False,
                "duration":        0.0,
                "window_start_ts": None,
                "window_end_ts":   None,
                "segments":        [],
            }

        window_start = self._finalized_segments[0]["start_ts"]
        window_end   = self._finalized_segments[-1]["end_ts"]

        # start_offset / end_offset are seconds from the start of the full
        # recording window, not from epoch. The peer scrubber works in these
        # relative offsets so it doesn't need to know the wall-clock time.
        segments_with_offsets = [
            {
                **segment,
                "start_offset": segment["start_ts"] - window_start,
                "end_offset":   segment["end_ts"]   - window_start,
            }
            for segment in self._finalized_segments
        ]

        return {
            "available":       True,
            "duration":        max(0.0, window_end - window_start),
            "window_start_ts": window_start,
            "window_end_ts":   window_end,
            "segments":        segments_with_offsets,
        }

    def resolve_playback_offset(self, offset_seconds: float) -> tuple[dict, float, float] | None:
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
        clamped_offset = max(0.0, min(float(offset_seconds), total_duration))

        for index, segment in enumerate(segments):
            is_last_segment = index == len(segments) - 1
            if clamped_offset < segment["end_offset"] or is_last_segment:
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
            if segment["path"] == current_segment_path and index + 1 < len(timeline["segments"]):
                return timeline["segments"][index + 1]
        return None

    async def run(self) -> None:
        """Consume queued frames and write them to rolling MP4 files.

        Must be started as an asyncio task before enqueue_frame() is called.
        """
        # maxsize=240 provides ~8 seconds of buffer at 30 fps.
        # If the disk can't keep up, frames are dropped by enqueue_frame() rather
        # than blocking the live camera pipeline.
        self._frame_queue = asyncio.Queue(maxsize=240)
        self._is_running  = True
        self._ready_event.set()

        while self._is_running:
            try:
                # timeout=1.0 allows the loop to check _is_running periodically
                # even when no frames arrive (e.g. camera stalled or stopping).
                frame, captured_at = await asyncio.wait_for(
                    self._frame_queue.get(), timeout=1.0
                )
                frame_size = (frame.shape[1], frame.shape[0])

                if self._video_writer is None:
                    self._start_new_segment(captured_at, frame_size)
                elif (
                    self._segment_start_ts is not None
                    and captured_at - self._segment_start_ts >= self.SEGMENT_DURATION_SECONDS
                ):
                    self._start_new_segment(captured_at, frame_size)
                elif frame_size != (self.frame_width, self.frame_height):
                    # Resolution changed mid-stream (e.g. camera reconnected at
                    # different mode). Rotate so the new file has a consistent size.
                    log.info(
                        "Frame size changed (%sx%s → %sx%s); rotating segment",
                        self.frame_width, self.frame_height,
                        frame.shape[1], frame.shape[0],
                    )
                    self._start_new_segment(captured_at, frame_size)

                if self._video_writer:
                    self._video_writer.write(frame)

            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        """Flush the open segment file and stop the recording loop."""
        self._is_running = False
        if self._video_writer:
            self._close_current_segment(time.time())
        self._video_writer     = None
        self._segment_start_ts = None
        self._segment_path     = None
