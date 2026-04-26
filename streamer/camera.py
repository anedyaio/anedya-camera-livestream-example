"""
Camera capture pipeline shared across all viewer sessions.

CameraSource opens the webcam once and runs a continuous capture loop.
It feeds frames to two consumers simultaneously:
  1. RecordingManager — for writing rolling MP4 segments to disk.
  2. WebcamTrack instances — one per active viewer, for live streaming.

Frames are shared via an asyncio.Condition so multiple tracks can wait
for the next frame without blocking the capture loop.

Also contains:
  configure_camera_max_resolution — probe and select the highest camera mode.
  draw_timestamp                  — burn a date/time stamp onto frame pixels.
"""

import asyncio
import logging
import time
from datetime import datetime

import cv2
import numpy as np

from config import (
    CAPTURE_RESOLUTION_CANDIDATES,
    MOTION_ANALYSIS_WIDTH,
    MOTION_ANALYSIS_HEIGHT,
)
from recording import RecordingManager

log = logging.getLogger("streamer")


def configure_camera_max_resolution(
    cap: cv2.VideoCapture,
    target_fps: float,
) -> tuple[int, int, float]:
    """Probe the camera driver for the highest supported resolution.

    Iterates CAPTURE_RESOLUTION_CANDIDATES (highest first) and asks the
    driver for each one. Drivers typically clamp unsupported modes to the
    best available, so the loop stops as soon as the returned size matches
    the requested size.

    Returns:
        (actual_width, actual_height, actual_fps)
    """
    best_width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)  or 0)
    best_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    best_area   = best_width * best_height

    cap.set(cv2.CAP_PROP_FPS, target_fps)

    for requested_width, requested_height in CAPTURE_RESOLUTION_CANDIDATES:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  requested_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, requested_height)

        actual_width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)  or 0)
        actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        actual_area   = actual_width * actual_height

        if actual_area > best_area:
            best_width  = actual_width
            best_height = actual_height
            best_area   = actual_area

        # Driver returned at least the requested size — this is the highest
        # supported mode. Higher candidates in the list would just be clamped
        # back down to this, so stop here.
        if actual_width >= requested_width and actual_height >= requested_height:
            best_width  = actual_width
            best_height = actual_height
            break

    actual_fps = float(cap.get(cv2.CAP_PROP_FPS) or target_fps)
    return best_width, best_height, actual_fps


def draw_timestamp(frame: np.ndarray, captured_at: float) -> np.ndarray:
    """Burn the capture date and time into the bottom-left corner of the frame.

    Draws a semi-transparent black background behind the text so it remains
    readable on both light and dark scenes.
    """
    timestamp_text = datetime.fromtimestamp(captured_at).strftime("%Y-%m-%d %H:%M:%S")
    font      = cv2.FONT_HERSHEY_SIMPLEX
    scale     = 0.55
    thickness = 1

    margin_x = max(12, int(frame.shape[1] * 0.015))
    margin_y = max(18, int(frame.shape[0] * 0.04))

    (text_w, text_h), baseline = cv2.getTextSize(timestamp_text, font, scale, thickness)
    x = margin_x
    y = frame.shape[0] - margin_y

    cv2.rectangle(
        frame,
        (x - 6,          y - text_h - 6),
        (x + text_w + 6, y + baseline + 6),
        (0, 0, 0),
        -1,
    )
    cv2.putText(frame, timestamp_text, (x, y), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)
    return frame


class CameraSource:
    """Always-on camera pipeline shared by all viewer peers.

    Responsibilities:
    - Open the camera device once at startup.
    - Capture frames continuously in a background asyncio task.
    - Run lightweight motion detection on a downscaled analysis frame.
    - Draw detected motion bounding boxes and a timestamp onto the full frame.
    - Push each frame to RecordingManager for disk writing.
    - Publish each frame via asyncio.Condition so WebcamTrack instances
      can await the next frame without polling.
    """

    def __init__(
        self,
        camera_index: int,
        recorder: RecordingManager,
        analysis_width:  int   = MOTION_ANALYSIS_WIDTH,
        analysis_height: int   = MOTION_ANALYSIS_HEIGHT,
        fps:             float = 30.0,
    ):
        self.camera_index    = camera_index
        self.recorder        = recorder
        self.analysis_width  = analysis_width
        self.analysis_height = analysis_height
        self.fps             = fps

        self.cap = cv2.VideoCapture(camera_index)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open camera index {camera_index}")

        self.capture_width, self.capture_height, self.capture_fps = (
            configure_camera_max_resolution(self.cap, fps)
        )
        log.info(
            "Camera %d opened: %dx%d @ %.1f fps",
            camera_index, self.capture_width, self.capture_height, self.capture_fps,
        )

        # detectShadows=False improves performance and avoids shadows being
        # classified as foreground, which would cause false motion events.
        self._background_subtractor = cv2.createBackgroundSubtractorMOG2(detectShadows=False)
        self._motion_cooldown       = 5.0
        self._last_motion_trigger   = 0.0

        # asyncio.Condition lets N viewer tracks all wait for the same new frame
        # without consuming it. A queue would require one queue per viewer.
        self._frame_condition   = asyncio.Condition()
        self._frame_sequence    = 0
        self._latest_frame:     np.ndarray | None = None
        self._latest_timestamp: float             = 0.0

        self._is_running = False
        self._capture_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background capture loop."""
        if self._capture_task is None:
            self._is_running   = True
            self._capture_task = asyncio.create_task(self._capture_loop())

    async def _capture_loop(self) -> None:
        """Main capture loop: read → analyse → annotate → record → publish."""
        frame_interval = 1 / self.fps if self.fps > 0 else 0

        while self._is_running:
            loop_start = time.monotonic()
            # cap.read() blocks waiting for hardware — run in thread to keep event loop free.
            ret, raw_frame = await asyncio.to_thread(self.cap.read)
            if not ret:
                await asyncio.sleep(0.05)
                continue

            # Downscale once for motion analysis to keep CPU usage low on Pi.
            analysis_frame = cv2.resize(raw_frame, (self.analysis_width, self.analysis_height))

            # Use only the lower half of the frame for motion detection.
            # The upper half often contains sky or ceiling whose brightness changes
            # due to lighting conditions, causing false-positive motion events.
            roi_y_start = self.analysis_height // 2
            roi         = analysis_frame[roi_y_start:self.analysis_height, 0:self.analysis_width]

            gray   = cv2.GaussianBlur(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), (3, 3), 0)
            mask   = self._background_subtractor.apply(gray)
            # Threshold the raw subtractor output to a clean binary mask.
            # Values below 200 are uncertain background; keep only high-confidence foreground.
            _, binary_mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)

            # Morphological open removes isolated noise pixels (salt-and-pepper)
            # that would otherwise produce tiny spurious contours.
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
            binary_mask = cv2.morphologyEx(binary_mask, cv2.MORPH_OPEN, kernel)

            contours, _ = cv2.findContours(binary_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            # 1200 px² threshold on the downscaled analysis frame filters out small
            # noise blobs. Adjust this value to tune sensitivity (lower = more sensitive).
            motion_contours = [c for c in contours if cv2.contourArea(c) > 1200]
            motion_detected = len(motion_contours) > 0

            now = time.time()

            if motion_detected:
                if now - self._last_motion_trigger > self._motion_cooldown:
                    log.info("Motion detected")
                    self._last_motion_trigger = now

                # Scale bounding boxes back from analysis-ROI coordinates to
                # full-resolution frame coordinates before drawing.
                scale_x      = raw_frame.shape[1] / self.analysis_width
                scale_y      = raw_frame.shape[0] / self.analysis_height
                roi_y_offset = self.analysis_height // 2

                for contour in motion_contours:
                    x, y, w, h = cv2.boundingRect(contour)
                    x1 = int(x * scale_x)
                    y1 = int((y + roi_y_offset) * scale_y)
                    x2 = int((x + w) * scale_x)
                    y2 = int((y + h + roi_y_offset) * scale_y)
                    cv2.rectangle(raw_frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

            annotated_frame = draw_timestamp(raw_frame, now)

            # copy() so the recorder and the Condition share independent buffers —
            # if the live viewer modifies the frame later it won't corrupt the recording.
            self.recorder.enqueue_frame(annotated_frame.copy(), now)

            async with self._frame_condition:
                self._latest_frame     = annotated_frame
                self._latest_timestamp = now
                self._frame_sequence  += 1
                self._frame_condition.notify_all()

            elapsed    = time.monotonic() - loop_start
            sleep_time = max(0.0, frame_interval - elapsed)
            await asyncio.sleep(sleep_time)

    async def get_next_frame(self, last_known_sequence: int) -> tuple[int, np.ndarray, float]:
        """Wait until a frame newer than last_known_sequence is available.

        Returns:
            (new_sequence, frame_copy, captured_at)
        """
        async with self._frame_condition:
            while self._latest_frame is None or self._frame_sequence <= last_known_sequence:
                await self._frame_condition.wait()
            return self._frame_sequence, self._latest_frame.copy(), self._latest_timestamp

    async def stop(self) -> None:
        """Stop the capture loop and release the camera device."""
        self._is_running = False
        if self._capture_task:
            await self._capture_task
            self._capture_task = None
        if self.cap.isOpened():
            self.cap.release()
            log.info("Camera %d released", self.camera_index)
