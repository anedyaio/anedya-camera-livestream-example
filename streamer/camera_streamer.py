"""
CameraStreamer — top-level coordinator for WebRTC signaling and media delivery.

Responsibilities:
  - Connect to the Anedya MQTT broker and subscribe to inbound command events.
  - Detect incoming WebRTC offers sent as the ``webrtc_offer`` command.
  - For each offer: create a peer connection, attach video and audio tracks, and
    publish the answer as the command's ack data (status ``processing``).
  - Handle DataChannel commands from each peer: ``timeline``, ``seek``, ``live``.
  - Start the shared CameraSource and RecordingManager at process startup.
  - Gracefully clean up all peers, the camera, and MQTT on shutdown.

Signaling path (Anedya Commands — same as the ESP-CAM project):
  Peer sends command "webrtc_offer"  (data = base64(zstd(offer JSON)))
    → Anedya MQTT command event
    → _handle_command()
    → _handle_offer()
    → device publishes status "processing" with ack data = base64(zstd(answer SDP))
    → Peer polls the command status, reads the answer, completes the handshake
    → device concludes the command "success" / "failure" (terminal)

The command id assigned by Anedya is the correlation key. The SDP is zstd-
compressed with a shared trained dictionary (see _compress_signaling), which is
what makes a full media SDP fit inside a command payload.
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
    HEARTBEAT_INTERVAL_SECONDS,
    MOTION_ANALYSIS_HEIGHT,
    MOTION_ANALYSIS_WIDTH,
    MOTION_COOLDOWN_SECONDS,
    MOTION_ROI_TOP_FRACTION,
    MOTION_THRESHOLD_PX,
    MQTT_BROKER,
    MQTT_KEEPALIVE,
    MQTT_PORT,
    RECORDING_RETENTION_SECONDS,
    RECORDING_SEGMENT_SECONDS,
    RECORDING_MIN_FREE_MB,
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

# Shared zstd dictionary for compressing WebRTC signaling SDP payloads before
# they go into the command payload. A trained dictionary holds the large boilerplate
# that repeats in every SDP (codec menu, extmaps, rtcp-fb), so each payload only
# carries its deltas. This file MUST be byte-identical to the dictionary embedded
# in the browser/app peers, or decompression fails.
_SIGNALING_DICT = zstd.ZstdCompressionDict(
    Path(__file__).with_name("signaling_dict.bin").read_bytes()
)
_SIGNALING_ZSTD_LEVEL = 19


def _compress_signaling(text: str) -> str:
    """zstd-compress (with the shared dict) then base64 a JSON signaling payload."""
    raw = zstd.ZstdCompressor(
        level=_SIGNALING_ZSTD_LEVEL, dict_data=_SIGNALING_DICT
    ).compress(text.encode("utf-8"))
    return base64.b64encode(raw).decode("ascii")


def _decompress_signaling(data: str) -> str:
    """Inverse of :func:`_compress_signaling`: base64-decode then zstd-decompress."""
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
        enable_audio: bool = True,
        record_path: str = "recordings",
        enable_motion_detection: bool = False,
    ):
        self.camera_index = camera_index
        self.enable_audio = enable_audio
        self.enable_motion_detection = enable_motion_detection

        # command_id -> {"pc", "video", "audio", "concluded"}
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
                    # else:
                    #     log.debug("Heartbeat published")
                except Exception as e:
                    log.warning("Heartbeat failed: %s", e)

            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

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
        # connect_async() returns immediately; loop_start()'s background thread
        # handles DNS resolution and the TCP/TLS handshake without blocking the
        # asyncio event loop.
        client.connect_async(MQTT_BROKER, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
        # loop_start() spins the MQTT network I/O in a daemon thread.
        # All paho callbacks (_on_connect, _on_message, etc.) run in that thread,
        # NOT in the asyncio event loop thread — keep that distinction in mind.
        client.loop_start()
        self._mqtt_client = client

    def _on_mqtt_connect(self, client, _userdata, _flags, rc) -> None:
        if rc == 0:
            log.info("Connected to Anedya broker — subscribing to commands")
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

    def _handle_command(self, payload: dict) -> None:
        """Dispatch an async offer-handling coroutine when a webrtc_offer arrives.

        Inbound command JSON::

            {"commandId": "<uuid>", "command": "webrtc_offer",
             "datatype": "string", "data": "<base64(zstd(offer JSON))>",
             "exp": <int>}

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

        # Acknowledge receipt before doing any work (received -> processing -> ...).
        self._publish_command_status(command_id, CMD_STATUS_RECEIVED)

        # Offer data arrives as base64 either way; "binary" gives more headroom
        # than "string" (1023 chars) because Anedya counts the decoded bytes.
        if payload.get("datatype") not in ("string", "binary"):
            log.error("webrtc_offer has unexpected datatype %r", payload.get("datatype"))
            self._publish_command_status(command_id, CMD_STATUS_FAILED, "bad datatype")
            return

        log.info("Incoming WebRTC offer (cmd=%s)", command_id)
        assert self._event_loop is not None

        future = asyncio.run_coroutine_threadsafe(
            self._handle_offer(command_id, payload.get("data", "")),
            self._event_loop,
        )
        future.add_done_callback(
            lambda f: (
                log.error("_handle_offer raised: %s", f.exception())
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
        """Publish a command status update over MQTT.

        ``ack_data`` is the compressed answer SDP for ``processing`` (sent as
        ``binary`` so Anedya counts the decoded bytes, not the 1023-char string
        limit) or a short failure reason (``string``). ``received`` and terminal
        ``success`` updates carry no ack data. ``ack_data`` is always a base64
        string on the wire either way; the datatype only changes how Anedya
        measures/stores it.
        """
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

    async def _handle_offer(self, command_id: str, offer_data: str) -> None:
        """Create a peer connection, attach tracks, and publish the WebRTC answer.

        Steps:
          1. Decompress + parse the offer SDP and TURN credentials from the
             command's ``data`` field.
          2. Build the ICE server list from the provided TURN credentials.
          3. Create an RTCPeerConnection and attach video and audio tracks.
          4. Register a DataChannel handler for seek / live / timeline commands.
          5. Create and apply the local answer SDP.
          6. Wait for ICE gathering to complete (15 s timeout).
          7. Publish the compressed answer via a "processing" status update.
        """
        try:
            # The peer sends the offer zstd-compressed (shared dict) + base64.
            data = json.loads(_decompress_signaling(offer_data))
            offer_sdp = data["offer"]
        except Exception as exc:
            log.error("Malformed offer payload (cmd=%s): %s", command_id, exc)
            self._publish_command_status(command_id, CMD_STATUS_FAILED, "malformed offer")
            return

        log.info("Processing offer (cmd=%s)", command_id)

        turn_data = data.get("turn")
        if not turn_data:
            log.error("No TURN credentials in offer (cmd=%s)", command_id)
            self._publish_command_status(command_id, CMD_STATUS_FAILED, "no turn creds")
            return

        # Build the ICE server list from the offer data
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

        # Check if the camera source is ready
        if self.source is None:
            log.error(
                "Camera source not ready — cannot handle offer (cmd=%s)", command_id
            )
            self._publish_command_status(command_id, CMD_STATUS_FAILED, "camera not ready")
            return

        # Each command id is a distinct session; a duplicate means a retry, so
        # close the stale connection before rebuilding it.
        if command_id in self._active_peers:
            log.warning(
                "Command %s already active — closing stale connection", command_id
            )
            await self._close_peer_session(command_id)

        # Create the peer connection
        peer_connection = RTCPeerConnection(
            configuration=RTCConfiguration(iceServers=ice_servers)
        )

        # Create video + audio tracks for THIS viewer
        video_track = WebcamTrack(self.source, self.recorder)
        # DvrAudioTrack` gets a reference to `video_track`. Audio never decides its own mode — 
        # it just reads `video_track.mode` every frame and follows it. Video is the authority.
        audio_track = (
            DvrAudioTrack(self.audio_source, video_track)
            if self.enable_audio and self.audio_source
            else None
        )

        # Store and attach tracks to connection. ``concluded`` guards the terminal
        # command status so success/failure is reported to the peer only once.
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
        @peer_connection.on("datachannel")
        def on_data_channel(channel):
            log.info(
                "DataChannel opened (cmd=%s, label=%s)", command_id, channel.label
            )
            command_lock = asyncio.Lock()

            def push_timeline_to_peer() -> None:
                timeline = self.recorder.get_timeline()
                playback_offset = video_track.current_playback_offset()
                # In live mode report the slider at the far-right (end of recording).
                # The peer UI uses this to position the scrubber at "now".
                if video_track.mode == "live":
                    playback_offset = timeline["duration"]
                channel.send(
                    json.dumps(
                        {
                            "type": "timeline",
                            "mode": video_track.mode,
                            "playback_offset": playback_offset,
                            **timeline,
                        }
                    )
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
                                    json.dumps(
                                        {
                                            "type": "error",
                                            "message": "No recording available at selected time",
                                        }
                                    )
                                )
                        elif action == "live":
                            await video_track.go_live()
                            push_timeline_to_peer()
                    except Exception:
                        log.exception("DataChannel command failed")

            # Send the current timeline immediately so the peer UI can render
            # the scrubber without waiting for the first user interaction.
            push_timeline_to_peer()

        @peer_connection.on("connectionstatechange")
        async def on_connection_state_change():
            state = peer_connection.connectionState
            log.info("Peer connection state: %s (cmd=%s)", state, command_id)
            # Conclude the command with a terminal status the first time the
            # connection resolves. success/failure are terminal in Anedya, so the
            # peer stops polling once we report one.
            if state == "connected":
                self._conclude_command(command_id, True)
            elif state in ("failed", "closed"):
                self._conclude_command(command_id, False, "connection failed")
                await self._close_peer_session(command_id)

        # SDP handshake.
        # SDP = Session Description Protocol.
        # A text blob that describes "I can send H.264 video, Opus audio, here are my network candidates."
        await peer_connection.setRemoteDescription(
            RTCSessionDescription(sdp=offer_sdp["sdp"], type=offer_sdp["type"])
        )
        answer = await peer_connection.createAnswer()
        await peer_connection.setLocalDescription(answer)

        # Wait until ICE gathering is complete before publishing the answer.
        # Publishing early would result in an SDP without TURN relay candidates,
        # which breaks connectivity when both peers are behind NAT.
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
                    "ICE gathering timed out (cmd=%s) — proceeding with available candidates",
                    command_id,
                )

        # Publish the answer as the command's ack data with status "processing",
        # zstd-compressed (shared dict) + base64 to fit the command payload budget.
        # success/failure are sent later, once the connection actually resolves.
        answer_payload = json.dumps(
            {
                "sdp": peer_connection.localDescription.sdp,
                "type": peer_connection.localDescription.type,
            }
        )
        ack_data = _compress_signaling(answer_payload)
        log.debug("Answer sizes (cmd=%s): b64 ack=%dB", command_id, len(ack_data))
        # Send as "binary": the ~1.1 KB base64 exceeds the 1023-char "string"
        # cap, but its decoded byte count is well under, and Anedya measures
        # binary by decoded bytes.
        self._publish_command_status(
            command_id, CMD_STATUS_PROCESSING, ack_data, ack_data_type="binary"
        )
        log.info("Answer published via command status (cmd=%s)", command_id)

    def _conclude_command(self, command_id: str, success: bool, reason: str = "") -> None:
        """Send the terminal command status (success/failure) at most once.

        Guarded by the peer's ``concluded`` flag so a later state change (e.g.
        connected -> closed on teardown) does not re-conclude a terminal command.
        """
        peer = self._active_peers.get(command_id)
        if peer is None or peer.get("concluded"):
            return
        peer["concluded"] = True
        if success:
            self._publish_command_status(command_id, CMD_STATUS_SUCCESS)
        else:
            self._publish_command_status(command_id, CMD_STATUS_FAILED, reason or None)

    async def _close_peer_session(self, command_id: str) -> None:
        """Release all resources owned by one viewer session."""
        peer = self._active_peers.pop(command_id, None)
        if not peer:
            return

        peer["video"].stop()
        if peer["audio"]:
            peer["audio"].release()
        await peer["pc"].close()
        log.info("Peer session closed (cmd=%s)", command_id)

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
        await (
            self.recorder.wait_until_ready()
        )  # camera can't start before recorder is ready

        # Create the camera source
        source = CameraSource(
            self.camera_index,
            self.recorder,
            analysis_width=MOTION_ANALYSIS_WIDTH,
            analysis_height=MOTION_ANALYSIS_HEIGHT,
            enable_motion_detection=self.enable_motion_detection,
            motion_threshold_px=MOTION_THRESHOLD_PX,
            motion_cooldown=float(MOTION_COOLDOWN_SECONDS),
            motion_roi_top_fraction=MOTION_ROI_TOP_FRACTION,
        )
        await source.start()  # start the camera source capture loop in background

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
        log.info("Streamer running — recording started, waiting for peers")

        # Main loop: wait for peers to connect and handle them
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    async def shutdown(self) -> None:
        """Gracefully close all peers, the camera, the recorder, and MQTT."""
        for command_id in list(self._active_peers):
            await self._close_peer_session(command_id)

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
