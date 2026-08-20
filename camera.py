"""
Camera capture pipeline shared across all viewer sessions.

CameraSource opens a camera once and runs a continuous capture loop.
It feeds frames to two consumers simultaneously:
  1. RecordingManager — for writing rolling MP4 segments to disk.
  2. WebcamTrack instances — one per active viewer, for live streaming.

Frames are shared via an asyncio.Condition so multiple tracks can wait
for the next frame without blocking the capture loop.

Supports two capture backends selected via CAMERA_SOURCE:
  "usb"  — PyAV direct V4L2/DShow capture (high performance, codec negotiation)
  "rtsp" — OpenCV cv2.VideoCapture with FFmpeg backend (RTSP/IP cameras)
"""

import asyncio
import logging
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime

import av
import cv2
import numpy as np

from config import (
    CAPTURE_RESOLUTION_CANDIDATES,
    MOTION_ANALYSIS_HEIGHT,
    MOTION_ANALYSIS_WIDTH,
)
from recording import RecordingManager

log = logging.getLogger("streamer")


@dataclass(frozen=True)
class CameraMode:
    width: int
    height: int
    fps: float

    # Four-character code identifying the video compression format (codec)
    fourcc: str


def _normalize_fourcc(value: object) -> str:
    text = str(value).upper().strip()
    if text in {"MJPEG", "MJPG"}:
        return "MJPG"
    if text in {"YUYV", "YUYV422", "YUY2"}:
        return "YUYV"
    return text[:4]


# TODO : Choose what CPU can handle.
def _choose_best_mode(modes: list[CameraMode], target_fps: float) -> CameraMode | None:
    # Prefer modes that meet the requested FPS, then maximize captured pixels.
    # This keeps live streaming and recording on the same highest usable source.
    candidates = [
        mode for mode in modes if target_fps <= 0 or mode.fps >= target_fps
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
        for match in re.finditer(
            r'"([^"]+)"\s+\(video\)', result.stderr + result.stdout
        )
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


def discover_best_camera_mode(
    camera_index: int, target_fps: float
) -> CameraMode | None:
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
            mode.width,
            mode.height,
            mode.fps,
            mode.fourcc,
        )
    return mode


def _fallback_camera_modes(target_fps: float) -> list[CameraMode]:
    """Highest-first fallback modes used when capability discovery is unavailable."""
    fps = target_fps if target_fps > 0 else 30.0
    return [
        CameraMode(width, height, fps, "MJPG")
        for width, height in CAPTURE_RESOLUTION_CANDIDATES
    ]


def _camera_input_name(camera_index: int) -> tuple[str, str]:
    system = platform.system()
    if system == "Windows":
        ffmpeg = _ffmpeg_exe()
        camera_name = _windows_camera_name(ffmpeg, camera_index) if ffmpeg else None
        if not camera_name:
            raise RuntimeError(f"Cannot resolve DirectShow camera index {camera_index}")
        return f"video={camera_name}", "dshow"
    if system == "Linux":
        return f"/dev/video{camera_index}", "v4l2"
    raise RuntimeError(f"Unsupported camera platform for PyAV capture: {system}")


def _pyav_options_for_mode(mode: CameraMode, input_format: str) -> dict[str, str]:
    options = {
        "video_size": f"{mode.width}x{mode.height}",
        "framerate": str(mode.fps),
    }
    fourcc = _normalize_fourcc(mode.fourcc)
    if input_format == "v4l2":
        options["input_format"] = "mjpeg" if fourcc == "MJPG" else fourcc.lower()
    elif input_format == "dshow" and fourcc == "MJPG":
        options["vcodec"] = "mjpeg"
    return options


def open_best_pyav_camera(
    camera_index: int, target_fps: float
) -> tuple[av.container.InputContainer, CameraMode]:
    """Open the best available camera mode with PyAV."""
    input_name, input_format = _camera_input_name(camera_index)
    discovered_mode = discover_best_camera_mode(camera_index, target_fps)
    candidates = ([discovered_mode] if discovered_mode else []) + _fallback_camera_modes(
        target_fps
    )

    last_error: Exception | None = None
    seen: set[tuple[int, int, int, str]] = set()
    for candidate in candidates:
        key = (
            candidate.width,
            candidate.height,
            int(round(candidate.fps)),
            _normalize_fourcc(candidate.fourcc),
        )
        if key in seen:
            continue
        seen.add(key)
        try:
            container = av.open(
                input_name,
                format=input_format,
                options=_pyav_options_for_mode(candidate, input_format),
            )
            log.info(
                "Camera %d opened with PyAV: %dx%d @ %.1f fps %s",
                camera_index,
                candidate.width,
                candidate.height,
                candidate.fps,
                candidate.fourcc,
            )
            stream = container.streams.video[0] if container.streams.video else None
            actual_mode = CameraMode(
                int(getattr(stream, "width", 0) or candidate.width),
                int(getattr(stream, "height", 0) or candidate.height),
                _stream_fps(stream) or candidate.fps,
                candidate.fourcc,
            )
            return container, actual_mode
        except Exception as exc:
            last_error = exc
            log.debug(
                "PyAV camera open failed for %dx%d @ %.1f fps %s: %s",
                candidate.width,
                candidate.height,
                candidate.fps,
                candidate.fourcc,
                exc,
            )

    raise RuntimeError(
        f"Cannot open camera index {camera_index} with PyAV"
        + (f": {last_error}" if last_error else "")
    )

def _stream_fps(stream) -> float | None:
    if stream is None:
        return None
    for attr in ("average_rate", "base_rate", "guessed_rate"):
        rate = getattr(stream, attr, None)
        if rate:
            try:
                value = float(rate)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if value > 0:
                return value
    return None


def configure_camera_max_resolution(
    cap: cv2.VideoCapture,
    target_fps: float,
    camera_index: int | None = None,
) -> tuple[int, int, float]:
    """Select the highest camera mode for OpenCV, falling back to probing.

    Used by the RTSP path when the source is a local camera (fallback).
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
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.55
    thickness = 1

    margin_x = max(12, int(frame.shape[1] * 0.015))
    margin_y = max(18, int(frame.shape[0] * 0.04))

    (text_w, text_h), baseline = cv2.getTextSize(timestamp_text, font, scale, thickness)
    x = margin_x
    y = frame.shape[0] - margin_y

    # black background
    cv2.rectangle(
        frame,
        (x - 6, y - text_h - 6),
        (x + text_w + 6, y + baseline + 6),
        (0, 0, 0),
        -1,
    )
    # white text
    cv2.putText(
        frame,
        timestamp_text,
        (x, y),
        font,
        scale,
        (255, 255, 255),
        thickness,
        cv2.LINE_AA,
    )

    if measured_fps is not None:
        height, width = frame.shape[:2]
        fps_text = f"{width}x{height} {measured_fps:.1f} FPS"
        (fps_w, fps_h), fps_baseline = cv2.getTextSize(fps_text, font, scale, thickness)
        fps_x = max(12, frame.shape[1] - fps_w - margin_x)
        fps_y = margin_y + fps_h

        cv2.rectangle(
            frame,
            (fps_x - 6, fps_y - fps_h - 6),
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

    Supports two capture backends:
      - "usb"  — PyAV direct V4L2/DShow capture
      - "rtsp" — OpenCV cv2.VideoCapture (RTSP URL or local fallback)

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
        source_mode: str = "usb",
        source_url: str = "",
        analysis_width: int = MOTION_ANALYSIS_WIDTH,
        analysis_height: int = MOTION_ANALYSIS_HEIGHT,
        fps: float = 60.0,
        enable_motion_detection: bool = False,
        motion_threshold_px: int = 1200,
        motion_cooldown: float = 5.0,
        motion_roi_top_fraction: float = 0.5,
    ):
        self.camera_index = camera_index
        self.source_mode = source_mode.strip().lower()
        self.source_url = source_url.strip()
        self.recorder = recorder
        self.analysis_width = analysis_width
        self.analysis_height = analysis_height
        self.fps = fps
        self.enable_motion_detection = enable_motion_detection
        self._motion_threshold_px = motion_threshold_px
        self._motion_cooldown_init = motion_cooldown
        # Clamp to [0.0, 0.95] — 0.95 leaves at least 5% of frame for analysis.
        self._motion_roi_top_fraction = max(0.0, min(0.95, motion_roi_top_fraction))

        # ── Backend-specific initialisation ──
        self._av_container = None
        self._av_decoder = None
        self._cv_cap = None

        if self.source_mode == "rtsp":
            self._init_rtsp_capture()
        else:
            self._init_usb_capture()

        if self.capture_fps > 0:
            self.fps = self.capture_fps
            self.recorder.fps = self.capture_fps
        log.info(
            "Camera source opened [%s]: %dx%d @ %.1f fps",
            self.source_mode.upper(),
            self.capture_width,
            self.capture_height,
            self.capture_fps,
        )

        # detectShadows=False improves performance and avoids shadows being
        # classified as foreground, which would cause false motion events.
        self._background_subtractor = (
            cv2.createBackgroundSubtractorMOG2(detectShadows=False)
            if self.enable_motion_detection
            else None
        )
        self._motion_cooldown = self._motion_cooldown_init
        self._last_motion_trigger = 0.0

        # asyncio.Condition lets N viewer tracks all wait for the same new frame
        # without consuming it. A queue would require one queue per viewer.
        self._frame_condition = asyncio.Condition()
        self._frame_sequence = 0
        self._latest_frame: np.ndarray | None = None
        self._latest_timestamp: float = 0.0
        self._measured_fps: float | None = None
        self._actual_frame_shape_logged = False
        self._fps_window_started_at = time.monotonic()
        self._fps_window_frames = 0
        self._last_frame_monotonic = time.monotonic()
        self._last_capture_stall_log = 0.0

        self._is_running = False
        self._capture_task: asyncio.Task | None = None

    # ── USB capture init (PyAV) ───────────────────────────────────

    def _init_usb_capture(self) -> None:
        """Open the camera using PyAV for direct V4L2/DShow capture."""
        self._av_container, selected_mode = open_best_pyav_camera(
            self.camera_index, self.fps
        )
        self._av_decoder = self._av_container.decode(video=0)
        self.capture_width = selected_mode.width
        self.capture_height = selected_mode.height
        self.capture_fps = selected_mode.fps

    # ── RTSP capture init (OpenCV) ────────────────────────────────

    def _init_rtsp_capture(self) -> None:
        """Open the camera using OpenCV VideoCapture for RTSP streams."""
        if self.source_url:
            self._cv_cap = cv2.VideoCapture(self.source_url, cv2.CAP_FFMPEG)
            self._source_label = self.source_url
        else:
            system = platform.system()
            backend = cv2.CAP_DSHOW if system == "Windows" else cv2.CAP_V4L2
            self._cv_cap = cv2.VideoCapture(self.camera_index, backend)
            self._cv_cap.set(
                cv2.CAP_PROP_FOURCC,
                cv2.VideoWriter_fourcc(*"MJPG"),
            )
            self._source_label = f"camera index {self.camera_index}"
        self._cv_cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not self._cv_cap.isOpened():
            raise RuntimeError(f"Cannot open camera source {self._source_label}")

        if self.source_url:
            self.capture_width = int(self._cv_cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            self.capture_height = int(self._cv_cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            self.capture_fps = float(self._cv_cap.get(cv2.CAP_PROP_FPS) or self.fps)
        else:
            self.capture_width, self.capture_height, self.capture_fps = (
                configure_camera_max_resolution(
                    self._cv_cap, self.fps, self.camera_index
                )
            )

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background capture loop."""
        if self._capture_task is None:
            self._is_running = True
            self._last_frame_monotonic = time.monotonic()
            self._capture_task = asyncio.create_task(self._capture_loop())
            self._capture_task.add_done_callback(self._on_capture_done)
            log.info("Camera capture task started [%s]", self.source_mode.upper())

    def _on_capture_done(self, task: asyncio.Task) -> None:
        """Log camera capture exits so heartbeat cannot hide a dead camera."""
        if task.cancelled():
            return
        was_running = self._is_running
        self._is_running = False
        try:
            task.result()
        except Exception:
            log.exception("Camera capture task crashed")
        else:
            if was_running:
                log.error("Camera capture task stopped unexpectedly")

    # ── Frame processing (shared by both backends) ────────────────

    def _process_raw_frame(self, raw_frame: np.ndarray) -> tuple[np.ndarray, float]:
        """Motion detection + annotation. Runs in a thread via asyncio.to_thread."""
        now = time.time()

        # Return early if motion detection is disabled.
        if not self.enable_motion_detection:
            return draw_status_overlay(raw_frame, now, self._measured_fps), now

        # Assert that the background subtractor is initialized.
        assert self._background_subtractor is not None

        analysis_frame = cv2.resize(
            raw_frame, (self.analysis_width, self.analysis_height)
        )  # shrink for speed

        # Skip the top fraction of the frame for motion detection.
        # Sky/ceiling brightness shifts cause false positives; configurable via
        # MOTION_ROI_TOP_FRACTION (0.0 = full frame, 0.5 = lower half only).
        roi_y_start = int(self.analysis_height * self._motion_roi_top_fraction)
        roi = analysis_frame[roi_y_start:, :]

        gray = cv2.GaussianBlur(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), (3, 3), 0)
        mask = self._background_subtractor.apply(gray)
        # Threshold the raw subtractor output to a clean binary mask.
        # Values below 200 are uncertain background; keep only high-confidence foreground.
        _, binary_mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)

        # Morphological open removes isolated noise pixels (salt-and-pepper)
        # that would otherwise produce tiny spurious contours.
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        binary_mask = cv2.morphologyEx(binary_mask, cv2.MORPH_OPEN, kernel)

        contours, _ = cv2.findContours(
            binary_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        # 1200 px² threshold on the downscaled analysis frame filters out small
        # noise blobs. Adjust this value to tune sensitivity (lower = more sensitive).
        motion_contours = [
            c for c in contours if cv2.contourArea(c) > self._motion_threshold_px
        ]

        now = time.time()

        if motion_contours:
            if now - self._last_motion_trigger > self._motion_cooldown:
                log.info("Motion detected")
                self._last_motion_trigger = now

            # Scale bounding boxes back from analysis-ROI coordinates to
            # full-resolution frame coordinates before drawing.
            scale_x = raw_frame.shape[1] / self.analysis_width
            scale_y = raw_frame.shape[0] / self.analysis_height
            roi_y_offset = int(self.analysis_height * self._motion_roi_top_fraction)

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

    # ── USB frame reading (PyAV) ──────────────────────────────────

    def _read_pyav_frame(self) -> np.ndarray | None:
        try:
            frame = next(self._av_decoder)
        except StopIteration:
            return None
        raw_frame = frame.to_ndarray(format="bgr24")
        if not self._actual_frame_shape_logged:
            height, width = raw_frame.shape[:2]
            if (width, height) != (self.capture_width, self.capture_height):
                log.info(
                    "Camera actual decoded size: %dx%d (requested %dx%d)",
                    width,
                    height,
                    self.capture_width,
                    self.capture_height,
                )
                self.capture_width = width
                self.capture_height = height
            self._actual_frame_shape_logged = True
        return raw_frame

    # ── RTSP frame reading (OpenCV) ───────────────────────────────

    def _read_opencv_frame(self) -> np.ndarray | None:
        assert self._cv_cap is not None
        ret, frame = self._cv_cap.read()
        if not ret:
            return None
        return frame

    async def _reopen_camera(self) -> bool:
        """Safely close existing camera handles and attempt to re-initialize capture."""
        log.info("Attempting to reconnect camera source [%s]...", self.source_mode.upper())

        def _close_handles():
            if self._cv_cap is not None:
                try:
                    self._cv_cap.release()
                except Exception as exc:
                    log.debug("Error releasing cv_cap: %s", exc)
                self._cv_cap = None
            if self._av_container is not None:
                try:
                    self._av_container.close()
                except Exception as exc:
                    log.debug("Error closing av_container: %s", exc)
                self._av_container = None
                self._av_decoder = None

        await asyncio.to_thread(_close_handles)

        try:
            if self.source_mode == "rtsp":
                await asyncio.to_thread(self._init_rtsp_capture)
            else:
                await asyncio.to_thread(self._init_usb_capture)
            self._last_frame_monotonic = time.monotonic()
            self._last_capture_stall_log = 0.0
            log.info("Camera source successfully reconnected [%s]", self.source_mode.upper())
            return True
        except Exception as exc:
            log.warning(
                "Camera reconnect failed [%s]: %s (will retry in 5s)",
                self.source_mode.upper(),
                exc,
            )
            return False

    # ── Capture loop (dispatches to the right reader) ─────────────

    async def _capture_loop(self) -> None:
        """Main capture loop: read → analyse → annotate → record → publish."""
        frame_interval = 1 / self.fps if self.fps > 0 else 0
        consecutive_none_count = 0
        STALL_NONE_THRESHOLD = 30  # ~1.5s - 3s of consecutive empty reads

        while self._is_running:
            loop_start = time.monotonic()

            if self.source_mode == "rtsp":
                read_frame = self._read_opencv_frame
            else:
                read_frame = self._read_pyav_frame

            raw_frame = await asyncio.to_thread(read_frame)
            if raw_frame is None:
                consecutive_none_count += 1
                now_monotonic = time.monotonic()
                seconds_without_frames = now_monotonic - self._last_frame_monotonic
                if (
                    seconds_without_frames >= 5.0
                    and now_monotonic - self._last_capture_stall_log >= 10.0
                ):
                    log.warning(
                        "Camera capture stalled (no frames for %.1fs, %d consecutive empty reads). Triggering reconnect...",
                        seconds_without_frames,
                        consecutive_none_count,
                    )
                    self._last_capture_stall_log = now_monotonic

                if seconds_without_frames >= 5.0 or consecutive_none_count >= STALL_NONE_THRESHOLD:
                    reconnected = await self._reopen_camera()
                    if reconnected:
                        consecutive_none_count = 0
                    else:
                        await asyncio.sleep(5.0)
                else:
                    await asyncio.sleep(0.05)
                continue

            consecutive_none_count = 0
            self._last_frame_monotonic = time.monotonic()
            self._update_measured_fps()

            # CPU-intensive OpenCV processing runs in a thread to keep event loop free.
            annotated_frame, now = await asyncio.to_thread(
                self._process_raw_frame, raw_frame
            )

            # copy() so the recorder and the Condition share independent buffers —
            # if the live viewer modifies the frame later it won't corrupt the recording.
            self.recorder.enqueue_frame(annotated_frame.copy(), now)

            async with self._frame_condition:
                self._latest_frame = annotated_frame
                self._latest_timestamp = now
                self._frame_sequence += 1
                self._frame_condition.notify_all()  # wake ALL waiting viewers

            elapsed = time.monotonic() - loop_start
            sleep_time = max(0.0, frame_interval - elapsed)

    async def get_next_frame(
        self, last_known_sequence: int
    ) -> tuple[int, np.ndarray, float]:
        """Wait until a frame newer than last_known_sequence is available.

        Returns:
            (new_sequence, frame_copy, captured_at)
        """
        async with self._frame_condition:
            while (
                self._latest_frame is None
                or self._frame_sequence <= last_known_sequence
            ):
                await self._frame_condition.wait()
            return (
                self._frame_sequence,
                self._latest_frame.copy(),
                self._latest_timestamp,
            )

    async def stop(self) -> None:
        """Stop the capture loop and release the camera device."""
        self._is_running = False
        if self._capture_task:
            self._capture_task.remove_done_callback(self._on_capture_done)
            try:
                await self._capture_task
            except Exception:
                log.exception("Camera capture task failed during shutdown")
            self._capture_task = None

        # Release the right backend
        if self._av_container is not None:
            self._av_container.close()
            log.info("Camera %d released [USB/PyAV]", self.camera_index)
        if self._cv_cap is not None and self._cv_cap.isOpened():
            self._cv_cap.release()
            log.info("Camera source released [RTSP/OpenCV]: %s", getattr(self, '_source_label', ''))
