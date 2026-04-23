"""
Pi Cam device process.

This module does four jobs:
1. Capture frames from camera continuously.
2. Record those frames into rolling MP4 segments.
3. Answer WebRTC offers from peers.
4. Serve live video or recorded playback to each peer.

Signaling is handled by Anedya value store + MQTT:
1. Peer creates offer and writes it to `offer_<sessionId>`.
2. Device receives value-store update over MQTT.
3. Device creates answer and writes it to `answer_<sessionId>`.
4. Peer receives the answer and completes WebRTC connection.
"""

import argparse
import asyncio
import fractions
import json
import logging
import os
import ssl
import time
from datetime import datetime
from pathlib import Path

import av
import cv2
import numpy as np
import paho.mqtt.client as mqtt_lib
import qrcode
import sounddevice as sd
from aiortc import (
    AudioStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)


# Keep logs focused on project behavior rather than low-level ICE noise.
logging.getLogger("aioice").setLevel(logging.WARNING)
logging.getLogger("aiortc").setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("streamer")


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs without adding a runtime dependency."""
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(Path(__file__).with_name(".env"))
load_env_file(Path.cwd() / ".env")


# Device identity and signaling configuration from Anedya.
ANEDYA_DEVICE_ID = os.environ.get("ANEDYA_DEVICE_ID", "")
ANEDYA_NODE_ID = os.environ.get("ANEDYA_NODE_ID", "")
ANEDYA_CONNECTION_KEY = os.environ.get("ANEDYA_CONNECTION_KEY", "")
ANEDYA_REGION = os.environ.get("ANEDYA_REGION", "ap-in-1")

# MQTT settings
MQTT_BROKER = f"mqtt.{ANEDYA_REGION}.anedya.io"
MQTT_PORT = 8883
MQTT_KEEPALIVE = 60

# Anedya Root CA 3 (ECC-256) — https://docs.anedya.io/device/mqtt-endpoints/#tls
ANEDYA_CA_CERT = """\
-----BEGIN CERTIFICATE-----
MIICDDCCAbOgAwIBAgITQxd3Dqj4u/74GrImxc0M4EbUvDAKBggqhkjOPQQDAjBL
MQswCQYDVQQGEwJJTjEQMA4GA1UECBMHR3VqYXJhdDEPMA0GA1UEChMGQW5lZHlh
MRkwFwYDVQQDExBBbmVkeWEgUm9vdCBDQSAzMB4XDTI0MDEwMTAwMDAwMFoXDTQz
MTIzMTIzNTk1OVowSzELMAkGA1UEBhMCSU4xEDAOBgNVBAgTB0d1amFyYXQxDzAN
BgNVBAoTBkFuZWR5YTEZMBcGA1UEAxMQQW5lZHlhIFJvb3QgQ0EgMzBZMBMGByqG
SM49AgEGCCqGSM49AwEHA0IABKsxf0vpbjShIOIGweak0/meIYS0AmXaujinCjFk
BFShcaf2MdMeYBPPFwz4p5I8KOCopgshSTUFRCXiiKwgYPKjdjB0MA8GA1UdEwEB
/wQFMAMBAf8wHQYDVR0OBBYEFNz1PBRXdRsYQNVsd3eYVNdRDcH4MB8GA1UdIwQY
MBaAFNz1PBRXdRsYQNVsd3eYVNdRDcH4MA4GA1UdDwEB/wQEAwIBhjARBgNVHSAE
CjAIMAYGBFUdIAAwCgYIKoZIzj0EAwIDRwAwRAIgR/rWSG8+L4XtFLces0JYS7bY
5NH1diiFk54/E5xmSaICIEYYbhvjrdR0GVLjoay6gFspiRZ7GtDDr9xF91WbsK0P
-----END CERTIFICATE-----"""

# MQTT topics
TOPIC_VS_UPDATES = f"$anedya/device/{ANEDYA_DEVICE_ID}/valuestore/updates/json"
TOPIC_VS_SET = f"$anedya/device/{ANEDYA_DEVICE_ID}/valuestore/setValue/json"
TOPIC_RESPONSES = f"$anedya/device/{ANEDYA_DEVICE_ID}/response"
TOPIC_ERRORS = f"$anedya/device/{ANEDYA_DEVICE_ID}/errors"

AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 1
AUDIO_FRAME_SAMPLES = 960
CAPTURE_RESOLUTION_CANDIDATES = [
    (7680, 4320),
    (3840, 2160),
    (2592, 1944),
    (2560, 1440),
    (2304, 1296),
    (1920, 1080),
    (1600, 1200),
    (1280, 720),
]
ANALYSIS_WIDTH = 320
ANALYSIS_HEIGHT = 240


def validate_anedya_config() -> None:
    """Fail fast if required Anedya credentials are not configured."""
    missing = [
        name
        for name, value in {
            "ANEDYA_DEVICE_ID": ANEDYA_DEVICE_ID,
            "ANEDYA_NODE_ID": ANEDYA_NODE_ID,
            "ANEDYA_CONNECTION_KEY": ANEDYA_CONNECTION_KEY,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required Anedya config: "
            + ", ".join(missing)
            + ". Set environment variables or create streamer/.env from streamer/.env.example."
        )


def configure_camera_max_resolution(cap: cv2.VideoCapture, fps: float) -> tuple[int, int, float]:
    """Ask OpenCV for the highest usable camera mode from a small candidate list."""
    best_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    best_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    best_area = best_width * best_height

    cap.set(cv2.CAP_PROP_FPS, fps)
    for width, height in CAPTURE_RESOLUTION_CANDIDATES:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        actual_area = actual_width * actual_height
        if actual_area > best_area:
            best_width = actual_width
            best_height = actual_height
            best_area = actual_area

        # Drivers often clamp unsupported modes down to the best available.
        if actual_width >= width and actual_height >= height:
            best_width = actual_width
            best_height = actual_height
            best_area = actual_area
            break

    actual_fps = float(cap.get(cv2.CAP_PROP_FPS) or fps)
    return best_width, best_height, actual_fps


def display_qr() -> None:
    """Print a QR code with identifiers used by browser-side app."""
    payload = json.dumps({
        "node_id": ANEDYA_NODE_ID,
        "device_id": ANEDYA_DEVICE_ID,
    })

    qr = qrcode.QRCode(border=2)
    qr.add_data(payload)
    qr.make(fit=True)

    print("\nScan this QR to connect:\n")
    qr.print_ascii(invert=True)
    print("\nPayload:", payload, "\n")


def build_turn_ice_servers(turn_url_or_host: str, username: str, credential: str) -> list[RTCIceServer]:
    """Return ICE server list used by aiortc on device side.

    TODO : `turn_url_or_host` is provided by Anedya. Current implementation still uses
    fixed regional endpoint strings, but all TURN-related config stays inside
    this helper so students can change it in one place later.
    """
    _ = turn_url_or_host
    return [
        RTCIceServer(urls=["stun:turn1.ap-in-1.anedya.io:3478"]),
        RTCIceServer(
            urls=["turn:turn1.ap-in-1.anedya.io:3478"],
            username=username,
            credential=credential,
        ),
    ]


def draw_timestamp(frame: np.ndarray, captured_at: float) -> np.ndarray:
    """Burn capture date/time into pixel data before recording and streaming."""
    stamp = datetime.fromtimestamp(captured_at).strftime("%Y-%m-%d %H:%M:%S")
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.55
    thickness = 1
    margin_x = max(12, int(frame.shape[1] * 0.015))
    margin_y = max(18, int(frame.shape[0] * 0.04))
    (text_w, text_h), baseline = cv2.getTextSize(stamp, font, scale, thickness)
    x = margin_x
    y = frame.shape[0] - margin_y
    cv2.rectangle(
        frame,
        (x - 6, y - text_h - 6),
        (x + text_w + 6, y + baseline + 6),
        (0, 0, 0),
        -1,
    )
    cv2.putText(frame, stamp, (x, y), font, scale,
                (255, 255, 255), thickness, cv2.LINE_AA)
    return frame


class RecordingManager:
    """Write rolling MP4 segments and expose timeline metadata."""

    SEGMENT_SECONDS = 5

    def __init__(self, record_path: str = "recordings", fps: float = 30.0):
        self.record_path = record_path
        self.width: int | None = None
        self.height: int | None = None
        self.fps = fps
        self._writer: cv2.VideoWriter | None = None
        self._segment_start_ts: float | None = None
        self._segment_path: str | None = None
        self._queue: asyncio.Queue | None = None
        self._running = False
        self._ready = asyncio.Event()
        self._segments: list[dict] = []

    async def wait_ready(self) -> None:
        """Wait until background recorder queue has been created."""
        await self._ready.wait()

    def enqueue(self, frame: np.ndarray, captured_at: float) -> None:
        """Queue a frame for disk writing.

        Called by `CameraSource`. This is intentionally independent of WebRTC,
        as recording starts when script starts.
        """
        if self._queue is None:
            return
        try:
            self._queue.put_nowait((frame, captured_at))
        except asyncio.QueueFull:
            pass

    def _finalize_segment(self, end_ts: float) -> None:
        """Close current MP4 file and store metadata used by playback."""
        if not self._writer or self._segment_start_ts is None or self._segment_path is None:
            return

        self._writer.release()
        self._writer = None

        duration = max(0.0, end_ts - self._segment_start_ts)
        if duration <= 0:
            return

        path = self._segment_path
        self._segments.append({
            "name": os.path.basename(path),
            "path": path,
            "size": os.path.getsize(path) if os.path.exists(path) else 0,
            "start_ts": self._segment_start_ts,
            "end_ts": end_ts,
            "duration": duration,
        })
        self._segments.sort(key=lambda item: item["start_ts"])

    def _rotate(self, start_ts: float, frame_size: tuple[int, int]) -> None:
        """Finish old segment and start a new one."""
        if self._writer:
            self._finalize_segment(start_ts)

        os.makedirs(self.record_path, exist_ok=True)
        self.width, self.height = frame_size
        ts = datetime.fromtimestamp(start_ts).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.record_path, f"{ts}.mp4")
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")  # type: ignore[attr-defined]
        self._writer = cv2.VideoWriter(
            path, fourcc, self.fps, frame_size)
        self._segment_start_ts = start_ts
        self._segment_path = path
        log.info("Recording segment: %s", path)

    def _build_timeline(self) -> dict:
        """Build slider-friendly timeline data from finalized segments."""
        if not self._segments:
            return {
                "available": False,
                "duration": 0.0,
                "window_start_ts": None,
                "window_end_ts": None,
                "segments": [],
            }

        window_start = self._segments[0]["start_ts"]
        window_end = self._segments[-1]["end_ts"]
        segments = []
        for segment in self._segments:
            segments.append({
                **segment,
                "start_offset": segment["start_ts"] - window_start,
                "end_offset": segment["end_ts"] - window_start,
            })

        return {
            "available": True,
            "duration": max(0.0, window_end - window_start),
            "window_start_ts": window_start,
            "window_end_ts": window_end,
            "segments": segments,
        }

    def get_timeline(self) -> dict:
        """Return current timeline for peer data channel."""
        return self._build_timeline()

    def resolve_offset(self, offset_seconds: float) -> tuple[dict, float, float] | None:
        """Translate slider offset into segment file + in-file seek position."""
        timeline = self._build_timeline()
        segments = timeline["segments"]
        if not segments:
            return None

        duration = float(timeline["duration"])
        clamped = max(0.0, min(float(offset_seconds), duration))
        for index, segment in enumerate(segments):
            is_last = index == len(segments) - 1
            if clamped < segment["end_offset"] or is_last:
                segment_offset = max(
                    0.0, min(clamped - segment["start_offset"], segment["duration"]))
                return segment, segment_offset, clamped
        return None

    def get_next_segment(self, current_path: str) -> dict | None:
        """Return next finalized segment when playback reaches file end."""
        timeline = self._build_timeline()
        segments = timeline["segments"]
        for index, segment in enumerate(segments):
            if segment["path"] == current_path and index + 1 < len(segments):
                return segments[index + 1]
        return None

    async def run(self) -> None:
        """Background loop that consumes queued frames and writes MP4 files."""
        self._queue = asyncio.Queue(maxsize=240)
        self._running = True
        self._ready.set()

        while self._running:
            try:
                frame, captured_at = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                frame_size = (frame.shape[1], frame.shape[0])
                if self._writer is None:
                    self._rotate(captured_at, frame_size)
                elif self._segment_start_ts is not None and captured_at - self._segment_start_ts >= self.SEGMENT_SECONDS:
                    self._rotate(captured_at, frame_size)
                elif frame_size != (self.width, self.height):
                    log.info(
                        "Recording size changed from %sx%s to %sx%s; rotating segment",
                        self.width,
                        self.height,
                        frame.shape[1],
                        frame.shape[0],
                    )
                    self._rotate(captured_at, frame_size)

                if self._writer:
                    self._writer.write(frame)
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        """Flush open file and stop recording loop."""
        self._running = False
        if self._writer:
            self._finalize_segment(time.time())
        self._writer = None
        self._segment_start_ts = None
        self._segment_path = None


class CameraSource:
    """Always-on camera capture pipeline shared by all peers.

    Responsibilities:
    - open camera source once
    - capture continuously
    - run motion detection
    - burn timestamp overlay
    - push frames to recorder
    - keep latest live frame ready for viewers
    """

    def __init__(
        self,
        camera_index: int,
        recorder: RecordingManager,
        analysis_width: int = ANALYSIS_WIDTH,
        analysis_height: int = ANALYSIS_HEIGHT,
        fps: float = 30.0,
    ):
        self.camera_index = camera_index
        self.recorder = recorder
        self.analysis_width = analysis_width
        self.analysis_height = analysis_height
        self.fps = fps
        self.cap = cv2.VideoCapture(camera_index)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open camera index {camera_index}")

        self.capture_width, self.capture_height, self.capture_fps = configure_camera_max_resolution(
            self.cap, fps)

        self.fgbg = cv2.createBackgroundSubtractorMOG2(detectShadows=False)
        self.last_trigger = 0.0
        self.cooldown = 5.0
        self._frame_condition = asyncio.Condition()
        self._frame_seq = 0
        self._latest_frame: np.ndarray | None = None
        self._latest_ts = 0.0
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start background capture task."""
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        """Capture loop used for both live streaming and recording."""
        frame_interval = 1 / self.fps if self.fps > 0 else 0

        while self._running:
            ret, frame = self.cap.read()
            if not ret:
                await asyncio.sleep(0.05)
                continue

            analysis_frame = cv2.resize(
                frame, (self.analysis_width, self.analysis_height))

            # Motion detection works on lower half of the reduced analysis frame.
            roi = analysis_frame[self.analysis_height // 2:self.analysis_height, 0:self.analysis_width]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (3, 3), 0)
            mask = self.fgbg.apply(gray)  # Background Subtraction

            # Threshold to get binary motion areas
            _, thresh = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
            thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)

            # Contours
            contours, _ = cv2.findContours(
                thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            # Motion detected if any contour is large enough i.e. area > 1200
            motion_detected = any(cv2.contourArea(
                contour) > 1200 for contour in contours)

            # Draw rectangles around detected motion areas
            for contour in contours:
                if cv2.contourArea(contour) > 1200:
                    x, y, w, h = cv2.boundingRect(contour)
                    cv2.rectangle(roi, (x, y), (x + w, y + h), (0, 255, 0), 2)

            # Trigger event if motion detected and cooldown has passed
            now = time.time()
            if motion_detected and (now - self.last_trigger > self.cooldown):
                print("Motion Detected!")
                self.last_trigger = now

            if motion_detected:
                scale_x = frame.shape[1] / self.analysis_width
                scale_y = frame.shape[0] / self.analysis_height
                roi_y_offset = self.analysis_height // 2
                for contour in contours:
                    if cv2.contourArea(contour) > 1200:
                        x, y, w, h = cv2.boundingRect(contour)
                        x1 = int(x * scale_x)
                        y1 = int((y + roi_y_offset) * scale_y)
                        x2 = int((x + w) * scale_x)
                        y2 = int((y + h + roi_y_offset) * scale_y)
                        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

            # Draw timestamp and enqueue full-resolution frame for recording and streaming
            frame = draw_timestamp(frame, now)
            self.recorder.enqueue(frame.copy(), now)

            # Wake any live viewers waiting for a newer frame.
            async with self._frame_condition:
                self._latest_frame = frame
                self._latest_ts = now
                self._frame_seq += 1
                self._frame_condition.notify_all()

            await asyncio.sleep(frame_interval)

    async def get_frame(self, last_seq: int) -> tuple[int, np.ndarray, float]:
        """Wait until a frame newer than `last_seq` is available."""
        async with self._frame_condition:
            while self._latest_frame is None or self._frame_seq <= last_seq:
                await self._frame_condition.wait()
            return self._frame_seq, self._latest_frame.copy(), self._latest_ts

    async def stop(self) -> None:
        """Stop capture task and release webcam device."""
        self._running = False
        if self._task:
            await self._task
            self._task = None
        if self.cap.isOpened():
            self.cap.release()
            log.info("Webcam released")


# ------------------------------------------------------------------ Stream tracks
class WebcamTrack(VideoStreamTrack):
    """Per-viewer video track.

    One peer session gets one `WebcamTrack`. This keeps playback state
    isolated per viewer.
    """

    kind = "video"

    def __init__(self, source: CameraSource, recorder: RecordingManager):
        super().__init__()
        self.source = source
        self.recorder = recorder
        self._last_seq = -1
        self._mode = "live"
        self._playback_cap: cv2.VideoCapture | None = None
        self._playback_path: str | None = None
        self._playback_base_offset = 0.0

    @property
    def mode(self) -> str:
        """Expose current viewer mode for browser UI."""
        return self._mode

    def current_offset(self) -> float | None:
        """Return current offset while in playback mode."""
        if self._mode != "playback" or not self._playback_cap:
            return None
        return self._playback_base_offset + max(0.0, self._playback_cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0)

    def seek(self, offset_seconds: float) -> bool:
        """Seek this viewer to a time inside recording window."""
        resolved = self.recorder.resolve_offset(offset_seconds)
        if not resolved:
            return False

        segment, segment_offset, clamped_offset = resolved
        if self._playback_cap:
            self._playback_cap.release()

        self._playback_cap = cv2.VideoCapture(segment["path"])
        if not self._playback_cap.isOpened():
            self._playback_cap = None
            return False

        if segment_offset > 0:
            self._playback_cap.set(cv2.CAP_PROP_POS_MSEC,
                                   segment_offset * 1000.0)

        self._playback_path = segment["path"]
        self._playback_base_offset = clamped_offset - segment_offset
        self._mode = "playback"
        log.info("Switched to playback: %s @ %.1fs",
                 segment["path"], segment_offset)
        return True

    def go_live(self) -> None:
        """Return this viewer from playback mode to live mode."""
        if self._playback_cap:
            self._playback_cap.release()
            self._playback_cap = None
        self._playback_path = None
        self._playback_base_offset = 0.0
        self._mode = "live"
        log.info("Switched to live")

    def _read_playback_frame(self) -> np.ndarray | None:
        """Read playback frame and cross segment boundaries automatically."""
        if not self._playback_cap or not self._playback_path:
            return None

        ret, frame = self._playback_cap.read()
        if ret:
            return frame

        next_segment = self.recorder.get_next_segment(self._playback_path)
        if not next_segment:
            return None

        self._playback_cap.release()
        self._playback_cap = cv2.VideoCapture(next_segment["path"])
        if not self._playback_cap.isOpened():
            self._playback_cap = None
            self._playback_path = None
            return None

        self._playback_path = next_segment["path"]
        self._playback_base_offset = next_segment["start_offset"]
        ret, frame = self._playback_cap.read()
        return frame if ret else None

    async def recv(self) -> av.VideoFrame:
        """Provide next video frame requested by aiortc."""
        pts, time_base = await self.next_timestamp()

        if self._mode == "playback" and self._playback_cap:
            frame = self._read_playback_frame()
            if frame is None:
                self.go_live()
                self._last_seq, frame, _ = await self.source.get_frame(self._last_seq)
        else:
            self._last_seq, frame, _ = await self.source.get_frame(self._last_seq)

        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        video_frame = av.VideoFrame.from_ndarray(
            frame_rgb, format="rgb24")  # type: ignore[arg-type]
        video_frame.pts = pts
        video_frame.time_base = time_base
        return video_frame

    async def stop(self) -> None:
        """Release any playback state owned by this viewer."""
        self.go_live()


class MicrophoneAudioTrack(AudioStreamTrack):
    """Live microphone audio track."""

    kind = "audio"

    def __init__(self):
        super().__init__()
        self._queue: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=50)
        self._loop = asyncio.get_event_loop()
        self._pts = 0
        self._stream = sd.InputStream(
            samplerate=AUDIO_SAMPLE_RATE,
            channels=AUDIO_CHANNELS,
            dtype="int16",
            blocksize=AUDIO_FRAME_SAMPLES,
            callback=self._callback,
        )
        self._stream.start()
        log.info("Microphone opened (%d Hz, %d ch)",
                 AUDIO_SAMPLE_RATE, AUDIO_CHANNELS)

    def _enqueue(self, data: np.ndarray) -> None:
        """Push microphone samples from callback thread into asyncio queue."""
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            pass

    def _callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """sounddevice callback used to collect microphone input."""
        _ = frames, time_info
        if status:
            log.warning("Audio status: %s", status)
        self._loop.call_soon_threadsafe(self._enqueue, indata.copy())

    async def recv(self) -> av.AudioFrame:
        """Convert PCM samples into aiortc audio frame."""
        data = await self._queue.get()
        frame = av.AudioFrame.from_ndarray(
            data.T.astype(np.int16),
            format="s16",
            layout="mono" if AUDIO_CHANNELS == 1 else "stereo",
        )
        frame.pts = self._pts
        frame.sample_rate = AUDIO_SAMPLE_RATE
        frame.time_base = fractions.Fraction(1, AUDIO_SAMPLE_RATE)
        self._pts += AUDIO_FRAME_SAMPLES
        return frame

    def release(self) -> None:
        """Stop microphone stream on disconnect or shutdown."""
        self._stream.stop()
        self._stream.close()
        log.info("Microphone released")


# ── Streamer ──────────────────────────────────────────────────────────────────
class CameraStreamer:
    """Top-level coordinator for signaling, camera source, and peers."""

    def __init__(self, camera_index: int, enable_audio: bool = True, record_path: str = "recordings"):
        self.camera_index = camera_index
        self.enable_audio = enable_audio
        self.peers: dict[str, dict] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mqtt: mqtt_lib.Client | None = None
        self.recorder = RecordingManager(record_path=record_path)
        self.source: CameraSource | None = None

    def _mqtt_connect(self) -> None:
        """Connect to MQTT broker used for Anedya value-store updates."""
        try:
            api_version = getattr(mqtt_lib, "CallbackAPIVersion").VERSION1
            client = mqtt_lib.Client(api_version, client_id=ANEDYA_DEVICE_ID)
        except AttributeError:
            client = mqtt_lib.Client(client_id=ANEDYA_DEVICE_ID)

        client.username_pw_set(ANEDYA_DEVICE_ID, ANEDYA_CONNECTION_KEY)

        ssl_ctx = ssl.create_default_context()
        ssl_ctx.load_verify_locations(cadata=ANEDYA_CA_CERT)
        client.tls_set_context(ssl_ctx)

        # Back off between reconnect attempts (1 s → 30 s)
        client.reconnect_delay_set(min_delay=1, max_delay=30)

        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.on_subscribe = lambda _c, _u, mid, granted_qos: log.info(
            "Subscribed to topic (mid=%d, qos=%s)", mid, granted_qos
        )

        log.info("Connecting to Anedya MQTT broker %s:%d...",
                 MQTT_BROKER, MQTT_PORT)
        client.connect(MQTT_BROKER, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        client.loop_start()   # runs MQTT network loop in a background thread
        self._mqtt = client

    _MQTT_RC = {
        1: "unacceptable protocol version",
        2: "client ID rejected",
        3: "broker unavailable",
        4: "bad username or password",
        5: "not authorised",
    }

    def _on_connect(self, client, _userdata, _flags, rc) -> None:
        """Subscribe to device topics after MQTT connection succeeds."""
        if rc == 0:
            log.info(
                "Connected to Anedya broker - subscribing to value store updates...")
            client.subscribe(TOPIC_VS_UPDATES)
            client.subscribe(TOPIC_RESPONSES)
            client.subscribe(TOPIC_ERRORS)
        else:
            reason = self._MQTT_RC.get(rc, f"unknown (rc={rc})")
            log.error("MQTT connect refused: %s - check credentials", reason)

    def _on_disconnect(self, _client, _userdata, rc) -> None:
        """Log unexpected disconnects. paho handles reconnect attempts."""
        if rc != 0:
            log.warning(
                "MQTT disconnected (rc=%d) - paho will reconnect with backoff", rc)

    def _on_message(self, _client, _userdata, message) -> None:
        """Route MQTT payloads to correct app-level handler."""
        try:
            payload = json.loads(message.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.warning("Unparseable MQTT message on %s: %s",
                        message.topic, exc)
            return

        if message.topic == TOPIC_VS_UPDATES:
            self._handle_vs_update(payload)
        elif message.topic == TOPIC_RESPONSES:
            log.info("MQTT response: %s", payload)
        elif message.topic == TOPIC_ERRORS:
            log.error("MQTT error: %s", payload)

    def _handle_vs_update(self, payload: dict) -> None:
        """Start async offer-handling task when an `offer_*` key changes."""
        log.debug("VS update payload: %s", payload)
        key = payload.get("key", "")
        if not key.startswith("offer_"):
            log.info("VS update ignored (key=%r)", key)
            return

        session_id = key[len("offer_"):]
        log.info("Value store offer received (session=%s)", session_id)
        assert self._loop is not None

        future = asyncio.run_coroutine_threadsafe(
            self._handle_offer(session_id, payload.get("value", "")),
            self._loop,
        )
        future.add_done_callback(
            lambda result: log.error(
                "_handle_offer error: %s", result.exception())
            if result.exception() else None
        )

    def _vs_set(self, key: str, value: str) -> None:
        """Write value into Anedya value store over MQTT."""
        assert self._mqtt is not None
        msg = json.dumps({
            "reqId": "",
            "key": key,
            "value": value,
            "type": "string",
        })
        self._mqtt.publish(TOPIC_VS_SET, msg, qos=1)
        log.debug("Value store set: namespace=node/%s key=%s",
                  ANEDYA_NODE_ID, key)

    async def _handle_offer(self, session_id: str, raw_value: str) -> None:
        """Create peer connection, media tracks, and answer for one viewer."""
        try:
            data = json.loads(raw_value)
            offer_sdp = data["offer"]
        except Exception as exc:
            log.error("Bad offer value (session=%s): %s", session_id, exc)
            return

        log.info("Handling offer (session=%s)", session_id)

        # Use TURN credentials forwarded by the browser so the Pi also gets
        # a relay (public) candidate — guarantees ICE connectivity through NAT.
        turn_data = data.get("turn")
        if not turn_data:
            log.error("No TURN creds in offer")
            return

        try:
            turn_urls = build_turn_ice_servers(
                turn_data["endpoint"],
                turn_data["username"],
                turn_data["credential"],
            )
        except (KeyError, ValueError) as exc:
            log.error("Invalid TURN data in offer (session=%s): %s",
                      session_id, exc)
            return

        if self.source is None:
            log.error("Camera source not ready")
            return

        pc = RTCPeerConnection(
            configuration=RTCConfiguration(iceServers=turn_urls))
        video_track = WebcamTrack(self.source, self.recorder)
        audio_track = MicrophoneAudioTrack() if self.enable_audio else None

        self.peers[session_id] = {
            "pc": pc,
            "track": video_track,
            "audio": audio_track,
        }

        pc.addTrack(video_track)
        if audio_track:
            pc.addTrack(audio_track)

        @pc.on("datachannel")
        def on_datachannel(channel):
            """Handle control channel from browser.

            Commands supported:
            - `timeline`: return current recording window
            - `seek`: move viewer into recorded playback
            - `live`: return viewer to live mode
            """
            log.info("DataChannel opened: label=%s", channel.label)

            def send_timeline() -> None:
                timeline = self.recorder.get_timeline()
                playback_offset = video_track.current_offset()
                if video_track.mode == "live":
                    playback_offset = timeline["duration"]

                channel.send(json.dumps({
                    "type": "timeline",
                    "mode": video_track.mode,
                    "playback_offset": playback_offset,
                    **timeline,
                }))

            @channel.on("message")
            def on_channel_message(msg):
                """Handle one JSON control message from browser."""
                try:
                    cmd = json.loads(msg)
                except json.JSONDecodeError:
                    return

                action = cmd.get("cmd")
                if action in ("list", "timeline"):
                    send_timeline()
                elif action == "seek":
                    offset = float(cmd.get("offset", 0))
                    if video_track.seek(offset):
                        send_timeline()
                    else:
                        channel.send(json.dumps({
                            "type": "error",
                            "message": "No finalized recording available yet",
                        }))
                elif action == "live":
                    video_track.go_live()
                    send_timeline()

            # Send initial timeline on channel open
            send_timeline()

        @pc.on("connectionstatechange")
        async def on_state():
            """Clean up if this peer fails or closes."""
            log.info("session=%s connection: %s",
                     session_id, pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                await self._close_peer(session_id)

        # Device is answerer in WebRTC handshake.
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=offer_sdp["sdp"], type=offer_sdp["type"])
        )
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        # Wait until local ICE candidates are gathered before publishing answer.
        gather_done = asyncio.Event()

        @pc.on("icegatheringstatechange")
        def on_gather():
            if pc.iceGatheringState == "complete":
                gather_done.set()

        if pc.iceGatheringState != "complete":
            try:
                await asyncio.wait_for(gather_done.wait(), timeout=15)
            except asyncio.TimeoutError:
                log.error("ICE gathering timed out (session=%s)", session_id)
                await self._close_peer(session_id)
                return

        answer_json = json.dumps({
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        })

        # Write answer to value store
        self._vs_set(f"answer_{session_id}", answer_json)
        log.info("Answer written to value store (session=%s)", session_id)

    async def _close_peer(self, session_id: str) -> None:
        """Release all objects owned by one viewer session."""
        peer = self.peers.pop(session_id, None)
        if not peer:
            return

        await peer["track"].stop()
        if peer["audio"]:
            peer["audio"].release()
        await peer["pc"].close()
        log.info("Peer closed (session=%s)", session_id)

    async def run(self) -> None:
        """Start signaling, recorder, and shared camera source."""
        self._loop = asyncio.get_event_loop()
        self._mqtt_connect()

        # Start recorder before camera source so early frames are not dropped.
        asyncio.create_task(self.recorder.run())
        await self.recorder.wait_ready()

        self.source = CameraSource(self.camera_index, self.recorder)
        await self.source.start()

        log.info("Streamer running - recording started immediately")
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    async def shutdown(self) -> None:
        """Gracefully close all peers, camera, recorder, and MQTT."""
        for session_id in list(self.peers):
            await self._close_peer(session_id)

        if self.source:
            await self.source.stop()
            self.source = None

        self.recorder.stop()

        if self._mqtt:
            self._mqtt.loop_stop()
            self._mqtt.disconnect()
            log.info("MQTT disconnected")


async def main(camera_index: int, enable_audio: bool, record_path: str = "recordings") -> None:
    """Async entrypoint used by `asyncio.run(...)`."""
    streamer = CameraStreamer(camera_index, enable_audio, record_path)
    try:
        await streamer.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutting down...")
    finally:
        await streamer.shutdown()


def cli() -> None:
    """Synchronous console entrypoint for `uv run streamer`."""
    parser = argparse.ArgumentParser(
        description="Pi Cam WebRTC streamer (Anedya MQTT signaling)")
    parser.add_argument("--camera", type=int, default=0,
                        help="Camera device index (default: 0)")
    parser.add_argument("--no-audio", action="store_true",
                        help="Disable microphone audio")
    parser.add_argument(
        "--record-path",
        default="recordings",
        help="Directory to store recording segments (default: recordings)",
    )
    args = parser.parse_args()

    log.info(
        "Starting (camera=%d, audio=%s, record-path=%s)",
        args.camera,
        "off" if args.no_audio else "on",
        args.record_path,
    )
    validate_anedya_config()
    display_qr()
    asyncio.run(main(args.camera, not args.no_audio, args.record_path))


if __name__ == "__main__":
    cli()
