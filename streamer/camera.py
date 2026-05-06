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
  discover_best_camera_mode       — enumerate platform camera capabilities first.
  draw_timestamp                  — burn a date/time stamp onto frame pixels.
"""

import asyncio
from dataclasses import dataclass
import logging
import platform
import re
import shutil
import subprocess
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


@dataclass(frozen=True)
class CameraMode:
    width: int
    height: int
    fps: float
    fourcc: str


def _normalize_fourcc(value: object) -> str:
    text = str(value).upper().strip()
    if text in {"MJPEG", "MJPG"}:
        return "MJPG"
    if text in {"YUYV", "YUYV422", "YUY2"}:
        return "YUYV"
    return text[:4]


def _choose_best_mode(modes: list[CameraMode], target_fps: float) -> CameraMode | None:
    # Prefer modes that meet the requested FPS, then maximize captured pixels.
    # This keeps live streaming and recording on the same highest usable source.
    candidates = [
        mode for mode in modes
        if target_fps <= 0 or mode.fps >= target_fps
    ] or modes
    if not candidates:
        return None

    return max(
        candidates,
        key=lambda mode: (
            mode.width * mode.height,
            mode.fourcc == "MJPG",
            mode.fps,
        ),
    )


def _linux_camera_modes(camera_index: int) -> list[CameraMode]:
    """Read V4L2 modes via linuxpy on Linux/Pi. Empty list means use fallback."""
    try:
        from linuxpy.video.device import Device
    except Exception:
        return []

    modes: list[CameraMode] = []
    try:
        with Device.from_id(camera_index) as camera:
            for frame_type in camera.info.frame_types:
                pixel_format = frame_type.pixel_format
                fourcc = _normalize_fourcc(
                    pixel_format.human_str()
                    if hasattr(pixel_format, "human_str")
                    else getattr(pixel_format, "name", pixel_format)
                )
                fps = float(frame_type.max_fps or frame_type.min_fps or 0)
                if fps > 0:
                    modes.append(
                        CameraMode(frame_type.width, frame_type.height, fps, fourcc)
                    )
    except Exception as exc:
        log.debug("linuxpy camera capability discovery failed: %s", exc)
    return modes


def _ffmpeg_exe() -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _windows_camera_name(ffmpeg: str, camera_index: int) -> str | None:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    devices = [
        match.group(1)
        for match in re.finditer(r'"([^"]+)"\s+\(video\)', result.stderr + result.stdout)
    ]
    return devices[camera_index] if 0 <= camera_index < len(devices) else None


def _windows_camera_modes(camera_index: int) -> list[CameraMode]:
    """Read DirectShow modes via FFmpeg on Windows. Empty list means use fallback."""
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        return []

    try:
        camera_name = _windows_camera_name(ffmpeg, camera_index)
        if not camera_name:
            return []

        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-f",
                "dshow",
                "-list_options",
                "true",
                "-i",
                f"video={camera_name}",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except Exception as exc:
        log.debug("FFmpeg camera capability discovery failed: %s", exc)
        return []

    modes: list[CameraMode] = []
    for line in (result.stderr + result.stdout).splitlines():
        format_match = re.search(r"(?:pixel_format|vcodec)=([^\s]+)", line)
        sizes = re.findall(r"s=(\d+)x(\d+)", line)
        fps_values = re.findall(r"fps=([0-9.]+)", line)
        if not format_match or not sizes or not fps_values:
            continue

        width, height = map(int, sizes[-1])
        fps = float(fps_values[-1])
        modes.append(
            CameraMode(width, height, fps, _normalize_fourcc(format_match.group(1)))
        )
    return modes


def discover_best_camera_mode(camera_index: int, target_fps: float) -> CameraMode | None:
    """Discover the best camera mode before falling back to OpenCV probing."""
    system = platform.system()
    if system == "Linux":
        modes = _linux_camera_modes(camera_index)
    elif system == "Windows":
        modes = _windows_camera_modes(camera_index)
    else:
        modes = []

    mode = _choose_best_mode(modes, target_fps)
    if mode:
        log.info(
            "Selected camera mode from capabilities: %dx%d @ %.1f fps %s",
            mode.width, mode.height, mode.fps, mode.fourcc,
        )
    return mode


def configure_camera_max_resolution(
    cap: cv2.VideoCapture,
    target_fps: float,
    camera_index: int | None = None,
) -> tuple[int, int, float]:
    """Select the highest camera mode, falling back to OpenCV probing.

    Uses platform capability discovery when available, otherwise iterates
    CAPTURE_RESOLUTION_CANDIDATES (highest first) and asks the
    driver for each one. Drivers typically clamp unsupported modes to the
    best available, so the loop stops as soon as the returned size matches
    the requested size.

    Returns:
        (actual_width, actual_height, actual_fps)
    """
    mode = (
        discover_best_camera_mode(camera_index, target_fps)
        if camera_index is not None
        else None
    )
    if mode:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, mode.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, mode.height)
        cap.set(cv2.CAP_PROP_FPS, mode.fps)
        # Some backends reset pixel format when size/FPS changes. Apply FOURCC
        # last so USB webcams stay on compressed MJPG instead of slow YUYV/YUY2.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*mode.fourcc))
        actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or mode.width)
        actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or mode.height)
        actual_fps = float(cap.get(cv2.CAP_PROP_FPS) or mode.fps)
        return actual_width, actual_height, actual_fps

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


def draw_status_overlay(
    frame: np.ndarray,
    captured_at: float,
    measured_fps: float | None = None,
) -> np.ndarray:
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

    if measured_fps is not None:
        height, width = frame.shape[:2]
        fps_text = f"{width}x{height} {measured_fps:.1f} FPS"
        (fps_w, fps_h), fps_baseline = cv2.getTextSize(fps_text, font, scale, thickness)
        fps_x = max(12, frame.shape[1] - fps_w - margin_x)
        fps_y = margin_y + fps_h

        cv2.rectangle(
            frame,
            (fps_x - 6,         fps_y - fps_h - 6),
            (fps_x + fps_w + 6, fps_y + fps_baseline + 6),
            (0, 0, 0),
            -1,
        )
        cv2.putText(
            frame,
            fps_text,
            (fps_x, fps_y),
            font,
            scale,
            (255, 255, 255),
            thickness,
            cv2.LINE_AA,
        )
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
        fps:             float = 60.0,
        enable_motion_detection: bool = False,
    ):
        self.camera_index    = camera_index
        self.recorder        = recorder
        self.analysis_width  = analysis_width
        self.analysis_height = analysis_height
        self.fps             = fps
        self.enable_motion_detection = enable_motion_detection

        system = platform.system()
        if system == "Windows":
            backend = cv2.CAP_DSHOW
        else:
            backend = cv2.CAP_V4L2

        self.cap = cv2.VideoCapture(camera_index, backend)
        self.cap.set(
            cv2.CAP_PROP_FOURCC,
            cv2.VideoWriter_fourcc(*"MJPG"),
        )

        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open camera index {camera_index}")

        self.capture_width, self.capture_height, self.capture_fps = (
            configure_camera_max_resolution(self.cap, fps, camera_index)
        )
        if self.capture_fps > 0:
            self.fps = self.capture_fps
            self.recorder.fps = self.capture_fps
        log.info(
            "Camera %d opened: %dx%d @ %.1f fps",
            camera_index, self.capture_width, self.capture_height, self.capture_fps,
        )

        # detectShadows=False improves performance and avoids shadows being
        # classified as foreground, which would cause false motion events.
        self._background_subtractor = (
            cv2.createBackgroundSubtractorMOG2(detectShadows=False)
            if self.enable_motion_detection
            else None
        )
        self._motion_cooldown       = 5.0
        self._last_motion_trigger   = 0.0

        # asyncio.Condition lets N viewer tracks all wait for the same new frame
        # without consuming it. A queue would require one queue per viewer.
        self._frame_condition   = asyncio.Condition()
        self._frame_sequence    = 0
        self._latest_frame:     np.ndarray | None = None
        self._latest_timestamp: float             = 0.0
        self._measured_fps:     float | None      = None
        self._fps_window_started_at = time.monotonic()
        self._fps_window_frames     = 0

        self._is_running = False
        self._capture_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background capture loop."""
        if self._capture_task is None:
            self._is_running   = True
            self._capture_task = asyncio.create_task(self._capture_loop())

    def _process_raw_frame(self, raw_frame: np.ndarray) -> tuple[np.ndarray, float]:
        """Motion detection + annotation. Runs in a thread via asyncio.to_thread."""
        now = time.time()

        # Return early if motion detection is disabled.
        if not self.enable_motion_detection:
            return draw_status_overlay(raw_frame, now, self._measured_fps), now

        # Assert that the background subtractor is initialized.
        assert self._background_subtractor is not None
        
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

        now = time.time()

        if motion_contours:
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

        return draw_status_overlay(raw_frame, now, self._measured_fps), now

    def _update_measured_fps(self) -> None:
        self._fps_window_frames += 1
        now = time.monotonic()
        elapsed = now - self._fps_window_started_at
        if elapsed >= 1.0:
            self._measured_fps = self._fps_window_frames / elapsed
            self._fps_window_started_at = now
            self._fps_window_frames = 0

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

            self._update_measured_fps()

            # CPU-intensive OpenCV processing runs in a thread to keep event loop free.
            annotated_frame, now = await asyncio.to_thread(self._process_raw_frame, raw_frame)

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
