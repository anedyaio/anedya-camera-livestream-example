"""
CameraStreamer — top-level coordinator for WebRTC signaling and media delivery.

Signaling via Anedya Commands (zstd-compressed SDP, lifecycle tracking):
  Peer sends command "webrtc_offer"  (data = base64(zstd(offer JSON)))
    → Anedya MQTT command event
    → _handle_command()
    → _handle_offer_commands()
    → device publishes status "processing" with ack data = base64(zstd(answer SDP))
    → Peer polls the command status, reads the answer, completes the handshake
    → device concludes the command "success" / "failure" (terminal)

Responsibilities:
  - Connect to the Anedya MQTT broker and subscribe to relevant topics.
  - For each offer: create a peer connection, attach video and audio tracks,
    and publish the answer.
  - Handle DataChannel commands from each peer: timeline, seek, live.
  - Start the shared CameraSource and RecordingManager at process startup.
  - Gracefully clean up all peers, the camera, and MQTT on shutdown.
"""

import asyncio
import base64
import json
import logging
import ssl
from concurrent.futures import Future
from pathlib import Path

import paho.mqtt.client as mqtt_lib
import zstandard as zstd
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)

from camera import CameraSource
from config import (
    ANEDYA_CA_CERT,
    ANEDYA_CONNECTION_KEY,
    ANEDYA_DEVICE_ID,
    CAMERA_SOURCE,
    CAMERA_SOURCE_URL,
    HEARTBEAT_INTERVAL_SECONDS,
    MOTION_ANALYSIS_HEIGHT,
    MOTION_ANALYSIS_WIDTH,
    MOTION_COOLDOWN_SECONDS,
    MOTION_ROI_TOP_FRACTION,
    MOTION_THRESHOLD_PX,
    MQTT_BROKER,
    MQTT_KEEPALIVE,
    MQTT_PORT,
    RECORDING_MIN_FREE_MB,
    RECORDING_RETENTION_SECONDS,
    RECORDING_SEGMENT_SECONDS,
    TOPIC_COMMANDS,
    TOPIC_COMMAND_STATUS,
    TOPIC_ERRORS,
    TOPIC_HEARTBEAT,
    TOPIC_LOGS,
    TOPIC_RESPONSES,
)
from audio import MicrophoneSource
from recording import RecordingManager
from tracks import DvrAudioTrack, WebcamTrack

log = logging.getLogger("streamer")

_LOG_MAX_CHARS = 1000

# ── Signaling compression (Commands mode) ─────────────────────────
# Shared zstd dictionary for compressing WebRTC signaling SDP payloads before
# they go into the command payload. A trained dictionary holds the large boilerplate
# that repeats in every SDP (codec menu, extmaps, rtcp-fb), so each payload only
# carries its deltas. This file MUST be byte-identical to the dictionary embedded
# in the browser/app peers, or decompression fails.
_SIGNALING_DICT_PATH = Path(__file__).with_name("signaling_dict.bin")
_SIGNALING_DICT = (
    zstd.ZstdCompressionDict(_SIGNALING_DICT_PATH.read_bytes())
    if _SIGNALING_DICT_PATH.exists()
    else None
)
_SIGNALING_ZSTD_LEVEL = 19


def _compress_signaling(text: str) -> str:
    """zstd-compress (with the shared dict) then base64 a JSON signaling payload."""
    assert _SIGNALING_DICT is not None, (
        "signaling_dict.bin not found — required for Commands signaling mode"
    )
    raw = zstd.ZstdCompressor(
        level=_SIGNALING_ZSTD_LEVEL, dict_data=_SIGNALING_DICT
    ).compress(text.encode("utf-8"))
    return base64.b64encode(raw).decode("ascii")


def _decompress_signaling(data: str) -> str:
    """Inverse of :func:`_compress_signaling`: base64-decode then zstd-decompress."""
    assert _SIGNALING_DICT is not None, (
        "signaling_dict.bin not found — required for Commands signaling mode"
    )
    raw = base64.b64decode(data)
    return zstd.ZstdDecompressor(dict_data=_SIGNALING_DICT).decompress(raw).decode("utf-8")


# Anedya command status strings (must match the platform / SDK exactly).
# received -> processing(+answer) -> success | failure. success/failure terminal.
CMD_STATUS_RECEIVED = "received"
CMD_STATUS_PROCESSING = "processing"
CMD_STATUS_SUCCESS = "success"
CMD_STATUS_FAILED = "failure"


class AnedyaLogHandler(logging.Handler):
    """Forwards log records to Anedya via MQTT after connection is established."""

    def __init__(self, mqtt_client: mqtt_lib.Client) -> None:
        super().__init__()
        self._mqtt_client = mqtt_client

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            if len(message) > _LOG_MAX_CHARS:
                message = message[:_LOG_MAX_CHARS]
            payload = json.dumps({
                "reqId": "",
                "data": [{"timestamp": int(record.created * 1000), "log": message}],
            })
            self._mqtt_client.publish(TOPIC_LOGS, payload, qos=0)
        except Exception:
            self.handleError(record)


def build_turn_ice_servers(
    turn_endpoint: str,
    username: str,
    credential: str,
) -> list[RTCIceServer]:
    """Build the ICE server list for a peer connection.

    Both STUN and TURN entries point to the Anedya regional relay. The
    ``turn_endpoint`` parameter is accepted for forward compatibility.

    Why TURN credentials come from the peer and not the device:
    The peer app fetches short-lived TURN credentials from the Anedya REST API
    and bundles them into the offer payload. The device reuses them so both
    sides share the same relay session, which is required for the TURN server
    to allow traffic between them.
    """
    _ = turn_endpoint  # TODO: use this instead of the hardcoded URL
    return [
        RTCIceServer(urls=["stun:turn1.ap-in-1.anedya.io:3478"]),
        RTCIceServer(
            urls=["turn:turn1.ap-in-1.anedya.io:3478"],
            username=username,
            credential=credential,
        ),
    ]


class CameraStreamer:
    """Top-level coordinator: MQTT signaling, camera pipeline, and peer sessions.

    One instance runs for the lifetime of the device process. It owns:
      - RecordingManager — rolling MP4 writer shared across all viewers
      - CameraSource     — always-on camera capture pipeline
      - MQTT client      — Anedya broker connection for signaling
      - _active_peers    — one entry per live WebRTC session
    """

    _MQTT_RETURN_CODES = {
        1: "unacceptable protocol version",
        2: "client ID rejected",
        3: "broker unavailable",
        4: "bad username or password",
        5: "not authorised",
    }

    def __init__(
        self,
        camera_index: int,
        source_mode: str = "usb",
        source_url: str = "",
        enable_audio: bool = True,
        record_path: str = "recordings",
        enable_motion_detection: bool = False,
    ):
        self.camera_index = camera_index
        self.source_mode = source_mode
        self.source_url = source_url
        self.enable_audio = enable_audio
        self.enable_motion_detection = enable_motion_detection

        # command_id / session_id -> {"pc", "video", "audio", "concluded"}
        self._active_peers: dict[str, dict] = {}

        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._mqtt_client: mqtt_lib.Client | None = None

        self.recorder = RecordingManager(
            record_path=record_path,
            segment_duration_seconds=RECORDING_SEGMENT_SECONDS,
            retention_seconds=RECORDING_RETENTION_SECONDS,
            min_free_mb=RECORDING_MIN_FREE_MB,
        )
        self.source: CameraSource | None = None  # Camera source
        self.audio_source: MicrophoneSource | None = None  # Audio source

        self._recorder_task: asyncio.Task | None = None
        self._heartbeat_task: Future | None = None
        self._anedya_log_handler: AnedyaLogHandler | None = None
        self._mqtt_connected_event: asyncio.Event | None = None

    def _on_recorder_done(self, task: asyncio.Task) -> None:
        """Log unexpected recorder exits instead of failing silently."""
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            log.exception("Recorder task crashed")
        else:
            log.error("Recorder task stopped unexpectedly")

    async def _heartbeat_loop(self) -> None:
        """Periodically publish device heartbeat to Anedya."""
        while True:
            if self._mqtt_client:
                try:
                    result = self._mqtt_client.publish(
                        TOPIC_HEARTBEAT, json.dumps({}), qos=1
                    )
                    if result.rc != mqtt_lib.MQTT_ERR_SUCCESS:
                        log.warning("Heartbeat publish failed rc=%s", result.rc)
                except Exception as e:
                    log.warning("Heartbeat failed: %s", e)

            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

    # ── MQTT connection ───────────────────────────────────────────

    def _connect_to_mqtt_broker(self) -> None:
        """Create, configure, and start the Paho MQTT client.

        Uses TLS with the embedded Anedya Root CA 3 certificate. Paho's
        automatic reconnect is enabled with exponential back-off (1 s → 30 s).
        The network loop runs in a background thread started by loop_start().
        """
        # CallbackAPIVersion.VERSION1 was introduced in paho-mqtt 2.x.
        # The try/except keeps compatibility with paho-mqtt 1.x installs.
        try:
            api_version = getattr(mqtt_lib, "CallbackAPIVersion").VERSION1
            client = mqtt_lib.Client(api_version, client_id=ANEDYA_DEVICE_ID)
        except AttributeError:
            client = mqtt_lib.Client(client_id=ANEDYA_DEVICE_ID)

        # Anedya uses the device ID as both username and the connection key as password.
        log.info("MQTT client configured: username=%s", ANEDYA_DEVICE_ID)
        client.username_pw_set(ANEDYA_DEVICE_ID, ANEDYA_CONNECTION_KEY)

        tls_context = ssl.create_default_context()
        tls_context.load_verify_locations(cadata=ANEDYA_CA_CERT)

        client.tls_set_context(tls_context)

        client.reconnect_delay_set(min_delay=1, max_delay=30)

        client.on_connect = self._on_mqtt_connect
        client.on_message = self._on_mqtt_message
        client.on_disconnect = self._on_mqtt_disconnect
        client.on_subscribe = lambda _c, _u, mid, granted_qos: log.info(
            "Subscribed (mid=%d, qos=%s)", mid, granted_qos
        )

        log.info("Connecting to Anedya MQTT broker %s:%d...", MQTT_BROKER, MQTT_PORT)
        client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        client.loop_start()
        self._mqtt_client = client

    def _on_mqtt_connect(self, client, _userdata, _flags, rc) -> None:
        if rc == 0:
            log.info("Connected to Anedya broker — signaling mode: COMMANDS")

            client.subscribe(TOPIC_COMMANDS)
            client.subscribe(TOPIC_RESPONSES)
            client.subscribe(TOPIC_ERRORS)

            # Install log forwarding handler (idempotent on reconnect)
            if self._anedya_log_handler is None:
                handler = AnedyaLogHandler(client)
                handler.setFormatter(
                    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
                )
                logging.getLogger().addHandler(handler)
                self._anedya_log_handler = handler

            # Signal run() to start camera/recorder/audio
            if self._event_loop and self._mqtt_connected_event:
                self._event_loop.call_soon_threadsafe(self._mqtt_connected_event.set)

            # Start heartbeat once connection is live
            if (
                self._heartbeat_task is None or self._heartbeat_task.done()
            ) and self._event_loop:
                self._heartbeat_task = asyncio.run_coroutine_threadsafe(
                    self._heartbeat_loop(), self._event_loop
                )
        else:
            reason = self._MQTT_RETURN_CODES.get(rc, f"unknown (rc={rc})")
            log.error("MQTT connection refused: %s — check credentials", reason)

    def _on_mqtt_disconnect(self, _client, _userdata, rc) -> None:
        if rc != 0:
            log.warning(
                "MQTT disconnected (rc=%d) — paho will reconnect with backoff", rc
            )

    def _on_mqtt_message(self, _client, _userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.warning("Unparseable MQTT message on %s: %s", message.topic, exc)
            return

        if message.topic == TOPIC_COMMANDS:
            self._handle_command(payload)
        elif message.topic == TOPIC_RESPONSES:
            if not payload.get("success", True):
                log.warning("MQTT response error: %s", payload)
        elif message.topic == TOPIC_ERRORS:
            log.error("MQTT error: %s", payload)

    # ══════════════════════════════════════════════════════════════
    #  COMMANDS-BASED SIGNALING
    # ══════════════════════════════════════════════════════════════

    def _handle_command(self, payload: dict) -> None:
        """Dispatch an async offer-handling coroutine when a webrtc_offer arrives.

        MQTT callbacks run in paho's background thread, so the coroutine is
        scheduled onto the asyncio event loop via run_coroutine_threadsafe.
        """
        command_name = payload.get("command", "")
        command_id = payload.get("commandId", "")
        if command_name != "webrtc_offer":
            log.debug("Ignoring command: %r", command_name)
            return
        if not command_id:
            log.warning("webrtc_offer command missing commandId — ignoring")
            return

        # Acknowledge receipt before doing any work.
        self._publish_command_status(command_id, CMD_STATUS_RECEIVED)

        if payload.get("datatype") not in ("string", "binary"):
            log.error("webrtc_offer has unexpected datatype %r", payload.get("datatype"))
            self._publish_command_status(command_id, CMD_STATUS_FAILED, "bad datatype")
            return

        log.info("Incoming WebRTC offer [commands] (cmd=%s)", command_id)
        assert self._event_loop is not None

        future = asyncio.run_coroutine_threadsafe(
            self._handle_offer_commands(command_id, payload.get("data", "")),
            self._event_loop,
        )
        future.add_done_callback(
            lambda f: (
                log.error("_handle_offer_commands raised: %s", f.exception())
                if f.exception()
                else None
            )
        )

    def _publish_command_status(
        self,
        command_id: str,
        status: str,
        ack_data: str | None = None,
        ack_data_type: str = "string",
    ) -> None:
        """Publish a command status update over MQTT."""
        assert self._mqtt_client is not None
        message: dict = {
            "reqId": "",
            "commandId": command_id,
            "status": status,
        }
        if ack_data is not None:
            message["ackdata"] = ack_data
            message["ackdatatype"] = ack_data_type
        self._mqtt_client.publish(TOPIC_COMMAND_STATUS, json.dumps(message), qos=1)
        log.debug("Command status=%s published (cmd=%s)", status, command_id)

    async def _handle_offer_commands(self, command_id: str, offer_data: str) -> None:
        """Handle an offer via Commands signaling (zstd-compressed SDP)."""
        try:
            data = json.loads(_decompress_signaling(offer_data))
            offer_sdp = data["offer"]
        except Exception as exc:
            log.error("Malformed offer payload (cmd=%s): %s", command_id, exc)
            self._publish_command_status(command_id, CMD_STATUS_FAILED, "malformed offer")
            return

        log.info("Processing offer [commands] (cmd=%s)", command_id)

        turn_data = data.get("turn")
        if not turn_data:
            log.error("No TURN credentials in offer (cmd=%s)", command_id)
            self._publish_command_status(command_id, CMD_STATUS_FAILED, "no turn creds")
            return

        try:
            ice_servers = build_turn_ice_servers(
                turn_data["endpoint"],
                turn_data["username"],
                turn_data["credential"],
            )
        except (KeyError, ValueError) as exc:
            log.error("Invalid TURN data in offer (cmd=%s): %s", command_id, exc)
            self._publish_command_status(command_id, CMD_STATUS_FAILED, "bad turn creds")
            return

        if self.source is None:
            log.error("Camera source not ready — cannot handle offer (cmd=%s)", command_id)
            self._publish_command_status(command_id, CMD_STATUS_FAILED, "camera not ready")
            return

        if command_id in self._active_peers:
            log.warning("Command %s already active — closing stale connection", command_id)
            await self._close_peer_session(command_id)

        # Create peer connection + tracks
        peer_connection, video_track, audio_track = self._create_peer(ice_servers)

        self._active_peers[command_id] = {
            "pc": peer_connection,
            "video": video_track,
            "audio": audio_track,
            "concluded": False,
        }
        peer_connection.addTrack(video_track)
        if audio_track:
            peer_connection.addTrack(audio_track)

        # Register event handlers
        self._setup_data_channel_handler(peer_connection, video_track, command_id)

        @peer_connection.on("connectionstatechange")
        async def on_connection_state_change():
            state = peer_connection.connectionState
            log.info("Peer connection state: %s (cmd=%s)", state, command_id)
            if state == "connected":
                self._conclude_command(command_id, True)
            elif state in ("failed", "closed"):
                self._conclude_command(command_id, False, "connection failed")
                await self._close_peer_session(command_id)

        # SDP handshake
        await peer_connection.setRemoteDescription(
            RTCSessionDescription(sdp=offer_sdp["sdp"], type=offer_sdp["type"])
        )
        answer = await peer_connection.createAnswer()
        await peer_connection.setLocalDescription(answer)

        # Wait for ICE gathering
        await self._wait_for_ice_gathering(peer_connection, command_id)

        # Publish answer via command status (zstd-compressed)
        answer_payload = json.dumps({
            "sdp": peer_connection.localDescription.sdp,
            "type": peer_connection.localDescription.type,
        })
        ack_data = _compress_signaling(answer_payload)
        log.debug("Answer sizes (cmd=%s): b64 ack=%dB", command_id, len(ack_data))
        self._publish_command_status(
            command_id, CMD_STATUS_PROCESSING, ack_data, ack_data_type="binary"
        )
        log.info("Answer published via command status (cmd=%s)", command_id)

    def _conclude_command(self, command_id: str, success: bool, reason: str = "") -> None:
        """Send the terminal command status (success/failure) at most once."""
        peer = self._active_peers.get(command_id)
        if peer is None or peer.get("concluded"):
            return
        peer["concluded"] = True
        if success:
            self._publish_command_status(command_id, CMD_STATUS_SUCCESS)
        else:
            self._publish_command_status(command_id, CMD_STATUS_FAILED, reason or None)



    # ══════════════════════════════════════════════════════════════
    #  SHARED HELPERS
    # ══════════════════════════════════════════════════════════════

    def _create_peer(
        self, ice_servers: list[RTCIceServer]
    ) -> tuple[RTCPeerConnection, WebcamTrack, DvrAudioTrack | None]:
        """Create a peer connection with video and optional audio tracks."""
        assert self.source is not None
        peer_connection = RTCPeerConnection(
            configuration=RTCConfiguration(iceServers=ice_servers)
        )
        video_track = WebcamTrack(self.source, self.recorder)
        audio_track = (
            DvrAudioTrack(self.audio_source, video_track)
            if self.enable_audio and self.audio_source
            else None
        )
        return peer_connection, video_track, audio_track

    def _setup_data_channel_handler(
        self,
        peer_connection: RTCPeerConnection,
        video_track: WebcamTrack,
        peer_id: str,
    ) -> None:
        """Register the DataChannel handler for timeline/seek/live commands."""

        @peer_connection.on("datachannel")
        def on_data_channel(channel):
            log.info("DataChannel opened (peer=%s, label=%s)", peer_id, channel.label)
            command_lock = asyncio.Lock()

            def push_timeline_to_peer() -> None:
                timeline = self.recorder.get_timeline()
                playback_offset = video_track.current_playback_offset()
                if video_track.mode == "live":
                    playback_offset = timeline["duration"]
                channel.send(
                    json.dumps({
                        "type": "timeline",
                        "mode": video_track.mode,
                        "playback_offset": playback_offset,
                        **timeline,
                    })
                )

            @channel.on("message")
            def on_channel_message(raw_message):
                asyncio.create_task(handle_channel_message(raw_message))

            async def handle_channel_message(raw_message):
                async with command_lock:
                    try:
                        command = json.loads(raw_message)
                    except json.JSONDecodeError:
                        return

                    try:
                        action = command.get("cmd")
                        if action in ("list", "timeline"):
                            push_timeline_to_peer()
                        elif action == "seek":
                            offset = float(command.get("offset", 0))
                            if await video_track.seek(offset):
                                push_timeline_to_peer()
                            else:
                                channel.send(
                                    json.dumps({
                                        "type": "error",
                                        "message": "No recording available at selected time",
                                    })
                                )
                        elif action == "live":
                            await video_track.go_live()
                            push_timeline_to_peer()
                    except Exception:
                        log.exception("DataChannel command failed")

            push_timeline_to_peer()

    async def _wait_for_ice_gathering(
        self, peer_connection: RTCPeerConnection, peer_id: str
    ) -> None:
        """Wait until ICE gathering is complete (15s timeout)."""
        ice_gathering_done = asyncio.Event()

        @peer_connection.on("icegatheringstatechange")
        def on_ice_gathering_state_change():
            if peer_connection.iceGatheringState == "complete":
                ice_gathering_done.set()

        if peer_connection.iceGatheringState != "complete":
            try:
                await asyncio.wait_for(ice_gathering_done.wait(), timeout=15)
            except asyncio.TimeoutError:
                log.warning(
                    "ICE gathering timed out (peer=%s) — proceeding with available candidates",
                    peer_id,
                )

    async def _close_peer_session(self, peer_id: str) -> None:
        """Release all resources owned by one viewer session."""
        peer = self._active_peers.pop(peer_id, None)
        if not peer:
            return

        peer["video"].stop()
        if peer["audio"]:
            peer["audio"].release()
        await peer["pc"].close()
        log.info("Peer session closed (peer=%s)", peer_id)

    # ── Main run loop ─────────────────────────────────────────────

    async def run(self) -> None:
        """Start all subsystems and block until a shutdown signal is received."""
        self._event_loop = asyncio.get_event_loop()
        self._mqtt_connected_event = asyncio.Event()
        self._connect_to_mqtt_broker()

        log.info("Waiting for MQTT connection before starting subsystems...")
        await self._mqtt_connected_event.wait()

        # Recorder must be running before the camera source starts so that
        # the very first frames are not dropped while the queue is being created.
        self._recorder_task = asyncio.create_task(self.recorder.run())
        self._recorder_task.add_done_callback(self._on_recorder_done)
        await self.recorder.wait_until_ready()

        # Create the camera source with the configured backend
        source = CameraSource(
            self.camera_index,
            self.recorder,
            source_mode=self.source_mode,
            source_url=self.source_url,
            analysis_width=MOTION_ANALYSIS_WIDTH,
            analysis_height=MOTION_ANALYSIS_HEIGHT,
            enable_motion_detection=self.enable_motion_detection,
            motion_threshold_px=MOTION_THRESHOLD_PX,
            motion_cooldown=float(MOTION_COOLDOWN_SECONDS),
            motion_roi_top_fraction=MOTION_ROI_TOP_FRACTION,
        )
        await source.start()

        # Create and start audio source if enabled
        if self.enable_audio:
            try:
                self.audio_source = MicrophoneSource(recorder=self.recorder)
                self.audio_source.start()
            except Exception as exc:
                self.enable_audio = False
                self.audio_source = None
                log.warning("Audio disabled: %s", exc)

        self.source = source
        log.info(
            "Streamer running — camera=%s, signaling=COMMANDS, recording started, waiting for peers",
            self.source_mode.upper(),
        )

        # Main loop: wait for peers to connect and handle them
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    async def shutdown(self) -> None:
        """Gracefully close all peers, the camera, the recorder, and MQTT."""
        for peer_id in list(self._active_peers):
            await self._close_peer_session(peer_id)

        if self.source:
            await self.source.stop()
            self.source = None

        if self.audio_source:
            self.audio_source.release()
            self.audio_source = None

        await self.recorder.stop()
        if self._recorder_task:
            self._recorder_task.remove_done_callback(self._on_recorder_done)
            try:
                await self._recorder_task
            except asyncio.CancelledError:
                pass
            self._recorder_task = None

        if self._heartbeat_task:
            canceled = self._heartbeat_task.cancel()
            log.info("Heartbeat task canceled: %s", canceled)
            self._heartbeat_task = None

        if self._anedya_log_handler:
            logging.getLogger().removeHandler(self._anedya_log_handler)
            self._anedya_log_handler = None

        if self._mqtt_client:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
            log.info("MQTT disconnected")
