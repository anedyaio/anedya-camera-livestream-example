"""
CameraStreamer — top-level coordinator for WebRTC signaling and media delivery.

Responsibilities:
  - Connect to the Anedya MQTT broker and subscribe to value-store update events.
  - Detect incoming WebRTC offers published under the key ``offer_<sessionId>``.
  - For each offer: create a peer connection, attach video and audio tracks, and
    publish the answer under ``answer_<sessionId>``.
  - Handle DataChannel commands from each peer: ``timeline``, ``seek``, ``live``.
  - Start the shared CameraSource and RecordingManager at process startup.
  - Gracefully clean up all peers, the camera, and MQTT on shutdown.

Signaling path:
  Peer writes offer_<id>
    → Anedya MQTT VS_UPDATES event
    → _handle_valuestore_update()
    → _handle_offer()
    → device writes answer_<id>
    → Peer reads answer and completes the WebRTC handshake
"""

import asyncio
import json
import logging
import ssl

import paho.mqtt.client as mqtt_lib
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

from camera import CameraSource
from config import (
    ANEDYA_CA_CERT,
    ANEDYA_CONNECTION_KEY,
    ANEDYA_DEVICE_ID,
    ANEDYA_NODE_ID,
    MQTT_BROKER,
    MQTT_KEEPALIVE,
    MQTT_PORT,
    TOPIC_ERRORS,
    TOPIC_RESPONSES,
    TOPIC_VALUESTORE_SET,
    TOPIC_VALUESTORE_UPDATES,
    TOPIC_HEARTBEAT,
    HEARTBEAT_INTERVAL_SECONDS,
)
from recording import RecordingManager
from tracks import MicrophoneAudioTrack, MicrophoneSource, WebcamTrack
from concurrent.futures import Future

log = logging.getLogger("streamer")


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
    _ = turn_endpoint  # TODO: use to support multiple Anedya regions
    return [
        RTCIceServer(urls=["stun:turn1.ap-in-1.anedya.io:3478"]),
        RTCIceServer(
            urls       = ["turn:turn1.ap-in-1.anedya.io:3478"],
            username   = username,
            credential = credential,
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
        record_path:  str  = "recordings",
    ):
        self.camera_index = camera_index
        self.enable_audio = enable_audio

        self._active_peers: dict[str, dict]              = {}
        self._event_loop:   asyncio.AbstractEventLoop | None = None
        self._mqtt_client:  mqtt_lib.Client | None       = None

        self.recorder = RecordingManager(record_path=record_path)
        self.source:   CameraSource | None = None
        self.audio_source: MicrophoneSource | None = None

        self._heartbeat_task: Future | None = None

    async def _heartbeat_loop(self) -> None:
        """Periodically publish device heartbeat to Anedya."""
        while True:
            if self._mqtt_client:
                try:
                    result = self._mqtt_client.publish(TOPIC_HEARTBEAT, json.dumps({}), qos=1)
                    if result.rc != mqtt_lib.MQTT_ERR_SUCCESS:
                        log.warning("Heartbeat publish failed rc=%s", result.rc)
                    else:
                        log.debug("Heartbeat published")
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
        client.username_pw_set(ANEDYA_DEVICE_ID, ANEDYA_CONNECTION_KEY)

        tls_context = ssl.create_default_context()
        tls_context.load_verify_locations(cadata=ANEDYA_CA_CERT)

        client.tls_set_context(tls_context)

        client.reconnect_delay_set(min_delay=1, max_delay=30)

        client.on_connect    = self._on_mqtt_connect
        client.on_message    = self._on_mqtt_message
        client.on_disconnect = self._on_mqtt_disconnect
        client.on_subscribe  = lambda _c, _u, mid, granted_qos: log.info(
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
            log.info("Connected to Anedya broker — subscribing to value-store updates")
            client.subscribe(TOPIC_VALUESTORE_UPDATES)
            client.subscribe(TOPIC_RESPONSES)
            client.subscribe(TOPIC_ERRORS)

            # Start heartbeat once connection is live
            if (
                self._heartbeat_task is None
                or self._heartbeat_task.done()
            ) and self._event_loop:
                self._heartbeat_task = asyncio.run_coroutine_threadsafe(
                    self._heartbeat_loop(),
                    self._event_loop
                )
        else:
            reason = self._MQTT_RETURN_CODES.get(rc, f"unknown (rc={rc})")
            log.error("MQTT connection refused: %s — check credentials", reason)

    def _on_mqtt_disconnect(self, _client, _userdata, rc) -> None:
        if rc != 0:
            log.warning("MQTT disconnected (rc=%d) — paho will reconnect with backoff", rc)

    def _on_mqtt_message(self, _client, _userdata, message) -> None:
        try:
            payload = json.loads(message.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.warning("Unparseable MQTT message on %s: %s", message.topic, exc)
            return

        if message.topic == TOPIC_VALUESTORE_UPDATES:
            self._handle_valuestore_update(payload)
        elif message.topic == TOPIC_RESPONSES:
            log.info("MQTT response: %s", payload)
        elif message.topic == TOPIC_ERRORS:
            log.error("MQTT error: %s", payload)

    def _handle_valuestore_update(self, payload: dict) -> None:
        """Dispatch an async offer-handling coroutine when an offer key arrives.

        MQTT callbacks run in paho's background thread, so the coroutine is
        scheduled onto the asyncio event loop via run_coroutine_threadsafe.
        Direct await here would crash because this is not an async function
        and we are not on the event loop thread.
        """
        log.debug("Value-store update: %s", payload)
        key = payload.get("key", "")
        if not key.startswith("offer_"):
            log.info("Value-store update ignored (key=%r)", key)
            return

        session_id = key[len("offer_"):]
        log.info("Incoming WebRTC offer (session=%s)", session_id)
        assert self._event_loop is not None

        future = asyncio.run_coroutine_threadsafe(
            self._handle_offer(session_id, payload.get("value", "")),
            self._event_loop,
        )
        future.add_done_callback(
            lambda f: log.error("_handle_offer raised: %s", f.exception())
            if f.exception() else None
        )

    def _write_to_valuestore(self, key: str, value: str) -> None:
        """Publish a string value to the Anedya value store over MQTT."""
        assert self._mqtt_client is not None
        message = json.dumps({
            "reqId": "", 
            "key":   key,
            "value": value,
            "type":  "string",
        })
        self._mqtt_client.publish(TOPIC_VALUESTORE_SET, message, qos=1)
        log.debug("Value-store write: namespace=node/%s key=%s", ANEDYA_NODE_ID, key)

    async def _handle_offer(self, session_id: str, raw_value: str) -> None:
        """Create a peer connection, attach tracks, and publish the WebRTC answer.

        Steps:
          1. Parse the offer SDP and TURN credentials from the value-store payload.
          2. Build the ICE server list from the provided TURN credentials.
          3. Create an RTCPeerConnection and attach video and audio tracks.
          4. Register a DataChannel handler for seek / live / timeline commands.
          5. Create and apply the local answer SDP.
          6. Wait for ICE gathering to complete (15 s timeout).
          7. Publish the final answer SDP to the value store.
        """
        try:
            data      = json.loads(raw_value)
            offer_sdp = data["offer"]
        except Exception as exc:
            log.error("Malformed offer payload (session=%s): %s", session_id, exc)
            return

        log.info("Processing offer (session=%s)", session_id)

        turn_data = data.get("turn")
        if not turn_data:
            log.error("No TURN credentials in offer (session=%s)", session_id)
            return

        try:
            ice_servers = build_turn_ice_servers(
                turn_data["endpoint"],
                turn_data["username"],
                turn_data["credential"],
            )
        except (KeyError, ValueError) as exc:
            log.error("Invalid TURN data in offer (session=%s): %s", session_id, exc)
            return

        if self.source is None:
            log.error("Camera source not ready — cannot handle offer (session=%s)", session_id)
            return

        if session_id in self._active_peers:
            log.warning("Session %s already active — closing stale connection", session_id)
            await self._close_peer_session(session_id)

        peer_connection = RTCPeerConnection(
            configuration=RTCConfiguration(iceServers=ice_servers)
        )
        video_track = WebcamTrack(self.source, self.recorder)
        audio_track = (
            MicrophoneAudioTrack(self.audio_source)
            if self.enable_audio and self.audio_source
            else None
        )

        self._active_peers[session_id] = {
            "pc":    peer_connection,
            "video": video_track,
            "audio": audio_track,
        }

        peer_connection.addTrack(video_track)
        if audio_track:
            peer_connection.addTrack(audio_track)

        @peer_connection.on("datachannel")
        def on_data_channel(channel):
            log.info("DataChannel opened (session=%s, label=%s)", session_id, channel.label)

            def push_timeline_to_peer() -> None:
                timeline        = self.recorder.get_timeline()
                playback_offset = video_track.current_playback_offset()
                # In live mode report the slider at the far-right (end of recording).
                # The peer UI uses this to position the scrubber at "now".
                if video_track.mode == "live":
                    playback_offset = timeline["duration"]
                channel.send(json.dumps({
                    "type":            "timeline",
                    "mode":            video_track.mode,
                    "playback_offset": playback_offset,
                    **timeline,
                }))

            @channel.on("message")
            def on_channel_message(raw_message):
                try:
                    command = json.loads(raw_message)
                except json.JSONDecodeError:
                    return

                action = command.get("cmd")
                if action in ("list", "timeline"):
                    push_timeline_to_peer()
                elif action == "seek":
                    offset = float(command.get("offset", 0))
                    if video_track.seek(offset):
                        push_timeline_to_peer()
                    else:
                        channel.send(json.dumps({
                            "type":    "error",
                            "message": "No finalized recording available yet",
                        }))
                elif action == "live":
                    video_track.go_live()
                    push_timeline_to_peer()

            # Send the current timeline immediately so the peer UI can render
            # the scrubber without waiting for the first user interaction.
            push_timeline_to_peer()

        @peer_connection.on("connectionstatechange")
        async def on_connection_state_change():
            log.info(
                "Peer connection state: %s (session=%s)",
                peer_connection.connectionState,
                session_id,
            )
            if peer_connection.connectionState in ("failed", "closed"):
                await self._close_peer_session(session_id)

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
                    "ICE gathering timed out (session=%s) — proceeding with available candidates",
                    session_id,
                )

        answer_payload = json.dumps({
            "sdp":  peer_connection.localDescription.sdp,
            "type": peer_connection.localDescription.type,
        })
        self._write_to_valuestore(f"answer_{session_id}", answer_payload)
        log.info("Answer published to value store (session=%s)", session_id)

    async def _close_peer_session(self, session_id: str) -> None:
        """Release all resources owned by one viewer session."""
        peer = self._active_peers.pop(session_id, None)
        if not peer:
            return

        await peer["video"].stop()
        if peer["audio"]:
            peer["audio"].release()
        await peer["pc"].close()
        log.info("Peer session closed (session=%s)", session_id)

    async def run(self) -> None:
        """Start all subsystems and block until a shutdown signal is received."""
        self._event_loop = asyncio.get_event_loop()
        self._connect_to_mqtt_broker()

        # Recorder must be running before the camera source starts so that
        # the very first frames are not dropped while the queue is being created.
        asyncio.create_task(self.recorder.run())
        await self.recorder.wait_until_ready()

        source = CameraSource(self.camera_index, self.recorder)
        await source.start()

        if self.enable_audio:
            try:
                self.audio_source = MicrophoneSource()
                self.audio_source.start()
            except Exception as exc:
                self.enable_audio = False
                self.audio_source = None
                log.warning("Audio disabled: %s", exc)

        self.source = source
        log.info("Streamer running — recording started, waiting for peers")
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    async def shutdown(self) -> None:
        """Gracefully close all peers, the camera, the recorder, and MQTT."""
        for session_id in list(self._active_peers):
            await self._close_peer_session(session_id)

        if self.source:
            await self.source.stop()
            self.source = None

        if self.audio_source:
            self.audio_source.release()
            self.audio_source = None

        self.recorder.stop()

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None

        if self._mqtt_client:
            self._mqtt_client.loop_stop()
            self._mqtt_client.disconnect()
            log.info("MQTT disconnected")
