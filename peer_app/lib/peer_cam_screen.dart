import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_svg/flutter_svg.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'sdp_compressor.dart';
// QR pairing is disabled — the Node ID is entered manually in Settings. Kept
// here (commented) in case scan-to-pair is wanted again; also uncomment the
// import below and the QR IconButton in _buildHeader, and re-add mobile_scanner
// to pubspec.yaml if restored.
// import 'qr_code_scanner.dart';

const String anedyaApiBase = 'https://api.ap-in-1.anedya.io/v1';

// Keys used to persist settings on-device via SharedPreferences.
const String prefKeyNodeId = 'peer_app.nodeId';
const String prefKeyApiKey = 'peer_app.apiKey';
const String prefKeyRelayOnly = 'peer_app.relayOnly';

/// Main viewer screen. Manages the full WebRTC connection lifecycle:
/// settings → TURN fetch → offer → poll answer → stream → playback.
class PeerCamScreen extends StatefulWidget {
  const PeerCamScreen({super.key});

  @override
  State<PeerCamScreen> createState() => _PeerCamScreenState();
}

class _PeerCamScreenState extends State<PeerCamScreen> {
  String _nodeId = ''; // Anedya Node ID of the Pi camera device
  String _apiKey = ''; // Anedya Platform API key
  bool _forceRelayOnly =
      false; // when true, forces WebRTC to use TURN relay only

  late TextEditingController _nodeIdController;
  late TextEditingController _apiKeyController;
  bool _isSettingsPanelVisible = false;

  // Stream / connection state
  bool _isStreamActive = false;
  bool _isInErrorState = false;
  String _statusText = 'Ready - press Start';
  String _networkModeText = 'Mode: --';
  bool _isTurnMode = false;
  String _logOutput = '';

  // Device online/offline status (polled from Anedya's health API).
  String _deviceStatus = 'unknown'; // 'online' | 'offline' | 'unknown'
  String _deviceStatusText = 'Device: —';
  Timer? _deviceStatusTimer;

  // WebRTC objects
  final RTCVideoRenderer _videoRenderer = RTCVideoRenderer();
  RTCPeerConnection? _peerConnection;
  RTCDataChannel? _dataChannel; // ordered channel named "control"
  MediaStreamTrack? _audioTrack;
  Timer? _answerPollTimer; // polls the command status for the Pi's SDP answer
  SdpCompressor? _sdpCompressor; // zstd+dict codec for signaling payloads
  Timer? _timelinePollTimer; // requests timeline state from Pi every 2 s
  Timer? _timelineRenderTimer; // locally smooths timeline between snapshots
  bool _isDisposed = false;
  bool _isMuted = false;
  DateTime? _lastSeekTime;

  // Timeline state
  double _totalRecordedSeconds = 0.0; // total duration available for scrubbing
  double _currentPositionSeconds = 0.0; // current playback position
  double _timelineSnapshotPositionSeconds = 0.0;
  DateTime? _timelineSnapshotAt;
  String _timelineMode = 'live';
  bool _isUserScrubbing = false; // true while user drags the slider
  bool _showTimelinePanel = false;
  bool _showGoLiveButton = false;
  String _timelineStatusText =
      'Recording starts immediately. Playback appears after first finalized segment.';
  String _currentTimeLabel = '00:00';
  String _totalDurationLabel = 'LIVE';

  @override
  void initState() {
    super.initState();
    _videoRenderer.initialize();
    _nodeIdController = TextEditingController();
    _apiKeyController = TextEditingController();
    _loadSavedSettings().then((_) => _startDeviceStatusPolling());
    // Warm up the zstd codec + dictionary so the first Start is not delayed.
    _initSdpCompressor();
  }

  /// Loads the zstd codec + shared dictionary. Never throws — a failure just
  /// leaves [_sdpCompressor] null so [_startStream] can report it clearly.
  Future<void> _initSdpCompressor() async {
    try {
      _sdpCompressor = await SdpCompressor.instance();
    } catch (error) {
      _appendLog('zstd init failed: $error');
    }
  }

  /// Reads node ID, API key, and relay-only flag from device storage.
  Future<void> _loadSavedSettings() async {
    final prefs = await SharedPreferences.getInstance();
    _safeSetState(() {
      _nodeId = prefs.getString(prefKeyNodeId) ?? '';
      _apiKey = prefs.getString(prefKeyApiKey) ?? '';
      _forceRelayOnly = prefs.getBool(prefKeyRelayOnly) ?? false;
      _nodeIdController.text = _nodeId;
      _apiKeyController.text = _apiKey;
    });
  }

  /// Persists current settings to device storage.
  Future<void> _saveSettings() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(prefKeyNodeId, _nodeId);
    await prefs.setString(prefKeyApiKey, _apiKey);
    await prefs.setBool(prefKeyRelayOnly, _forceRelayOnly);
    // Re-check device status immediately against the new node/key.
    _startDeviceStatusPolling();
  }

  @override
  void dispose() {
    _isDisposed = true;
    _deviceStatusTimer?.cancel();
    _answerPollTimer?.cancel();
    _timelinePollTimer?.cancel();
    _timelineRenderTimer?.cancel();
    _closePeerConnection();
    _videoRenderer.dispose();
    _nodeIdController.dispose();
    _apiKeyController.dispose();
    super.dispose();
  }

  /// Safe setState — no-ops after dispose or when widget is unmounted.
  void _safeSetState(VoidCallback fn) {
    if (!_isDisposed && mounted) setState(fn);
  }

  void _appendLog(String message) =>
      _safeSetState(() => _logOutput += '$message\n');

  // HTTP headers. The key may already carry a "Bearer " prefix (the Settings
  // hint says it is optional), so only add one when it is missing.
  Map<String, String> get _requestHeaders => {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'Authorization': _apiKey.toLowerCase().startsWith('bearer ')
        ? _apiKey
        : 'Bearer $_apiKey',
  };

  // --- WebRTC signaling over Anedya Commands ------------------------------
  // The offer is sent as the "webrtc_offer" command; the Pi replies by
  // attaching the compressed answer SDP as this command's ackdata while the
  // command is "processing", then concludes success/failure.

  /// Sends the compressed offer as a command. Returns the Anedya-assigned
  /// commandId, which correlates the answer back to this offer.
  Future<String> _sendOfferViaCommand(String dataB64) async {
    final response = await http.post(
      Uri.parse('$anedyaApiBase/commands/send'),
      headers: _requestHeaders,
      body: jsonEncode({
        'nodeId': _nodeId,
        'command': 'webrtc_offer',
        'data': dataB64,
        // "string": the compressed offer base64 is well under the 1023-char cap.
        // (Binary type mangles the inbound command payload on the device side,
        // so we keep the offer as string.)
        'type': 'string',
        'expiry': DateTime.now().millisecondsSinceEpoch + 300000,
      }),
    );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception(
        'Command send failed: ${response.statusCode} ${response.body}',
      );
    }
    final decoded = jsonDecode(response.body);
    if (decoded is! Map<String, dynamic>) {
      throw Exception('Command send returned malformed response');
    }
    // Field name has varied across API versions; accept whichever is present.
    final commandId =
        decoded['commandId'] ?? decoded['id'] ?? decoded['commandID'];
    if (commandId is! String || commandId.isEmpty) {
      throw Exception('Command send returned no id: ${response.body}');
    }
    return commandId;
  }

  /// Removes non-relay ICE candidate lines from an SDP, keeping `typ relay`
  /// candidates and all other lines. Shrinks the offer so it fits the command
  /// payload cap; connectivity is unaffected because both peers use the shared
  /// TURN relay bundled with the offer.
  String _stripNonRelayCandidates(String sdp) {
    final lines = sdp.split('\n');
    final kept = lines.where((line) {
      final trimmed = line.trim();
      if (!trimmed.startsWith('a=candidate:')) return true;
      return trimmed.contains('typ relay');
    });
    return kept.join('\n');
  }

  /// Polls a command's details: status + ackdata (base64(zstd(answer SDP))).
  /// Returns null on HTTP/parse error so the poller keeps trying.
  Future<Map<String, dynamic>?> _getCommandStatus(String commandId) async {
    final response = await http.post(
      Uri.parse('$anedyaApiBase/commands/getDetails'),
      headers: _requestHeaders,
      body: jsonEncode({'commandId': commandId}),
    );
    if (response.statusCode < 200 || response.statusCode >= 300) return null;
    final decoded = jsonDecode(response.body);
    return decoded is Map<String, dynamic> ? decoded : null;
  }

  /// Asks Anedya to provision a short-lived TURN relay for this session.
  /// Returns relay data including username, credential, and expiry timestamp.
  ///
  /// Both the app and the Pi use the same TURN allocation details so they
  /// can reach each other even through strict NAT.
  Future<Map<String, dynamic>> _fetchTurnCredentials() async {
    final response = await http.post(
      Uri.parse('$anedyaApiBase/relay/create'),
      headers: _requestHeaders,
      body: jsonEncode({'relayType': 'turn'}),
    );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception('TURN fetch failed: ${response.statusCode}');
    }
    final decoded = jsonDecode(response.body);
    if (decoded is! Map<String, dynamic> || decoded['relayData'] is! Map) {
      throw Exception(
        decoded['error']?.toString() ?? 'No relayData in response',
      );
    }
    final relayData = Map<String, dynamic>.from(decoded['relayData'] as Map);
    // flutter_webrtc expects the field named 'password'; Anedya returns it as 'credential'.
    relayData['password'] = relayData['credential'];
    relayData['relayExpiry'] = decoded['relayExpiry'];
    return relayData;
  }

  // --- Device online/offline status ---------------------------------------
  // The Pi heartbeats Anedya every ~30 s. We ask Anedya's health API whether
  // the node has been seen within [_deviceStatusThresholdSeconds], both as a
  // live badge (polled) and as a gate before starting a stream.
  static const int _deviceStatusThresholdSeconds = 90;
  static const Duration _deviceStatusPollInterval = Duration(seconds: 10);

  /// Returns the node's health info ({ online, lastHeartbeat }) or throws.
  Future<Map<String, dynamic>> _fetchNodeHealth() async {
    final response = await http.post(
      Uri.parse('$anedyaApiBase/health/status'),
      headers: _requestHeaders,
      body: jsonEncode({
        'nodes': [_nodeId],
        'lastContactThreshold': _deviceStatusThresholdSeconds,
      }),
    );
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw Exception('Health check failed: ${response.statusCode}');
    }
    final decoded = jsonDecode(response.body);
    if (decoded is! Map<String, dynamic>) {
      throw Exception('Malformed health response');
    }
    if (decoded['success'] == false) {
      throw Exception(decoded['error'] ?? decoded['reasonCode'] ?? 'health error');
    }
    final info = decoded['data']?[_nodeId];
    if (info is! Map<String, dynamic>) {
      throw Exception('health API returned no data for this node');
    }
    return info;
  }

  String _formatClock(DateTime dt) =>
      '${dt.hour.toString().padLeft(2, '0')}:${dt.minute.toString().padLeft(2, '0')}';

  void _setDeviceStatus(String state, String text) {
    _safeSetState(() {
      _deviceStatus = state;
      _deviceStatusText = text;
    });
  }

  /// Refreshes the badge once. Never throws — network/config problems just
  /// show as "unknown" so the poller keeps running.
  Future<void> _refreshDeviceStatus() async {
    if (_nodeId.isEmpty) {
      _setDeviceStatus('unknown', 'Device: set Node ID');
      return;
    }
    if (_apiKey.isEmpty) {
      _setDeviceStatus('unknown', 'Device: set API key');
      return;
    }
    try {
      final info = await _fetchNodeHealth();
      final lastHb = info['lastHeartbeat'];
      if (info['online'] == true) {
        final t = lastHb is num
            ? _formatClock(
                DateTime.fromMillisecondsSinceEpoch(
                  lastHb.toInt() * 1000,
                ).toLocal(),
              )
            : '—';
        _setDeviceStatus('online', 'Device: ONLINE (last $t)');
      } else {
        final t = lastHb is num
            ? DateTime.fromMillisecondsSinceEpoch(
                lastHb.toInt() * 1000,
              ).toLocal().toString()
            : 'never';
        _setDeviceStatus('offline', 'Device: OFFLINE (last $t)');
      }
    } catch (_) {
      _setDeviceStatus('unknown', 'Device: status unknown');
    }
  }

  /// Starts (or restarts) the background poll. Call after settings change.
  void _startDeviceStatusPolling() {
    _deviceStatusTimer?.cancel();
    _refreshDeviceStatus();
    _deviceStatusTimer = Timer.periodic(
      _deviceStatusPollInterval,
      (_) => _refreshDeviceStatus(),
    );
  }

  /// Sends a JSON command to the Pi over the WebRTC data channel.
  /// No-ops silently if the channel is not open yet.
  ///
  /// Supported commands:
  ///   { cmd: 'timeline' }           — request current timeline state
  ///   { cmd: 'seek', offset: <s> }  — jump to position in seconds
  ///   { cmd: 'live' }               — return to live (streaming) mode
  void _sendDataChannelCommand(Map<String, dynamic> command) {
    final channel = _dataChannel;
    if (channel != null &&
        channel.state == RTCDataChannelState.RTCDataChannelOpen) {
      channel.send(RTCDataChannelMessage(jsonEncode(command)));
    }
  }

  /// Applies a timeline message from the Pi and updates the slider UI.
  ///
  /// Message shape:
  ///   { type: 'timeline', duration: <s>, playback_offset: <s>, mode: 'live'|'playback' }
  ///
  /// [duration]        = total recorded seconds available for scrubbing.
  /// [playback_offset] = Pi's current read position (absent when in live mode).
  void _applyTimelineUpdate(Map<String, dynamic> timelineData) {
    final totalDuration = (timelineData['duration'] as num?)?.toDouble() ?? 0.0;
    final playbackPosition = timelineData['playback_offset'] != null
        ? (timelineData['playback_offset'] as num).toDouble()
        : totalDuration;
    _timelineMode = timelineData['mode']?.toString() ?? 'live';
    _timelineSnapshotPositionSeconds = playbackPosition;
    _timelineSnapshotAt = DateTime.now();

    _safeSetState(() {
      _showTimelinePanel = true;
      _totalRecordedSeconds = totalDuration;

      // Don't override slider position while the user is actively dragging it.
      if (!_isUserScrubbing) {
        _currentPositionSeconds = totalDuration > 0
            ? playbackPosition.clamp(0.0, totalDuration)
            : 0.0;
      }

      _currentTimeLabel = _formatDuration(_currentPositionSeconds);
      _totalDurationLabel = totalDuration > 0
          ? _formatDuration(totalDuration)
          : 'LIVE';

      if (totalDuration <= 0) {
        // No segments finalized yet — recording just started.
        _timelineStatusText =
            'Recording starts immediately. Playback appears after first finalized segment.';
        _showGoLiveButton = false;
        return;
      }

      if (timelineData['mode'] == 'live') {
        _timelineStatusText = 'Live mode';
        final recentSeek =
            _lastSeekTime != null &&
            DateTime.now().difference(_lastSeekTime!) <
                const Duration(seconds: 3);
        if (!recentSeek) _showGoLiveButton = false;
      } else {
        final secondsBehindLive = (totalDuration - playbackPosition).clamp(
          0.0,
          double.infinity,
        );
        _timelineStatusText =
            'Playback mode - ${_formatDuration(secondsBehindLive)} behind live';
        // Show Go Live button so the user can return to the live edge.
        _showGoLiveButton = true;
      }
    });
  }

  double _displayedTimelinePosition() {
    if ((_timelineMode == 'playback' || _timelineMode == 'gap') &&
        _timelineSnapshotAt != null) {
      final elapsed =
          DateTime.now().difference(_timelineSnapshotAt!).inMilliseconds /
          1000.0;
      return (_timelineSnapshotPositionSeconds + elapsed).clamp(
        0.0,
        _totalRecordedSeconds,
      );
    }
    return _totalRecordedSeconds;
  }

  void _updateTimelineDisplay() {
    if (_isUserScrubbing) return;

    final position = _totalRecordedSeconds > 0
        ? _displayedTimelinePosition()
        : 0.0;

    _safeSetState(() {
      _currentPositionSeconds = position;
      _currentTimeLabel = _formatDuration(position);
      _totalDurationLabel = _totalRecordedSeconds > 0
          ? _formatDuration(_totalRecordedSeconds)
          : 'LIVE';

      if (_totalRecordedSeconds <= 0) {
        _timelineStatusText =
            'Recording starts immediately. Playback appears after first finalized segment.';
        _showGoLiveButton = false;
      } else if (_timelineMode == 'live') {
        _timelineStatusText = 'Live mode';
        final recentSeek =
            _lastSeekTime != null &&
            DateTime.now().difference(_lastSeekTime!) <
                const Duration(seconds: 3);
        if (!recentSeek) _showGoLiveButton = false;
      } else if (_timelineMode == 'gap') {
        _timelineStatusText = 'No recording available';
        _showGoLiveButton = true;
      } else {
        final secondsBehindLive = (_totalRecordedSeconds - position).clamp(
          0.0,
          double.infinity,
        );
        _timelineStatusText =
            'Playback mode - ${_formatDuration(secondsBehindLive)} behind live';
        _showGoLiveButton = true;
      }
    });
  }

  /// Formats a duration in seconds to MM:SS or HH:MM:SS.
  String _formatDuration(double totalSeconds) {
    final seconds = totalSeconds.clamp(0, double.infinity).toInt();
    final hours = seconds ~/ 3600;
    final minutes = (seconds % 3600) ~/ 60;
    final secs = seconds % 60;
    if (hours > 0) {
      return '${hours.toString().padLeft(2, '0')}:'
          '${minutes.toString().padLeft(2, '0')}:'
          '${secs.toString().padLeft(2, '0')}';
    }
    return '${minutes.toString().padLeft(2, '0')}:${secs.toString().padLeft(2, '0')}';
  }

  void _resetTimelineState() {
    _safeSetState(() {
      _showTimelinePanel = false;
      _showGoLiveButton = false;
      _totalRecordedSeconds = 0.0;
      _currentPositionSeconds = 0.0;
      _timelineSnapshotPositionSeconds = 0.0;
      _timelineSnapshotAt = null;
      _timelineMode = 'live';
      _isUserScrubbing = false;
      _currentTimeLabel = '00:00';
      _totalDurationLabel = 'LIVE';
      _timelineStatusText =
          'Recording starts immediately. Playback appears after first finalized segment.';
    });
  }

  /// Runs the full WebRTC signaling flow:
  ///   Step 0 → gate on device online status (don't handshake an offline Pi)
  ///   Step 1 → fetch TURN credentials
  ///   Step 2 → create peer connection + data channel + transceivers
  ///   Step 3 → create SDP offer, gather ICE candidates
  ///   Step 4 → send offer + TURN data as the "webrtc_offer" command (zstd+dict)
  ///   Step 5 → poll the command status for the Pi's answer + terminal result
  Future<void> _startStream() async {
    _answerPollTimer?.cancel();
    _timelinePollTimer?.cancel();
    _timelineRenderTimer?.cancel();
    _timelineRenderTimer = null;
    await _closePeerConnection();

    _safeSetState(() {
      _statusText = 'Checking device status...';
      _networkModeText = 'Mode: --';
      _isTurnMode = false;
      _isStreamActive = true;
      _isInErrorState = false;
    });

    // Step 0: bail out early if the Pi is offline — no point handshaking a
    // device that has not heartbeated recently.
    try {
      final info = await _fetchNodeHealth();
      if (info['online'] != true) {
        final lastHb = info['lastHeartbeat'];
        final last = lastHb is num
            ? DateTime.fromMillisecondsSinceEpoch(
                lastHb.toInt() * 1000,
              ).toLocal().toString()
            : 'never';
        _setDeviceStatus('offline', 'Device: OFFLINE (last $last)');
        _handleError('Device is OFFLINE (last heartbeat: $last)');
        return;
      }
      _setDeviceStatus('online', 'Device: ONLINE');
    } catch (error) {
      _handleError('Device status check failed: $error');
      return;
    }

    _safeSetState(() => _statusText = 'Fetching TURN credentials...');

    // Step 1: fetch short-lived TURN credentials from Anedya.
    Map<String, dynamic> turnCredentials;
    try {
      turnCredentials = await _fetchTurnCredentials();
      final relayExpiry = turnCredentials['relayExpiry'];
      _appendLog(
        relayExpiry is num
            ? 'TURN ready (expires ${DateTime.fromMillisecondsSinceEpoch(relayExpiry.toInt() * 1000).toLocal().toIso8601String()})'
            : 'TURN ready',
      );
    } catch (error) {
      _handleError('TURN error: $error');
      return;
    }

    // Step 2: create the peer connection, data channel, and receive-only transceivers.
    try {
      _peerConnection = await createPeerConnection({
        // When relay-only is on, force all traffic through TURN — useful for
        // debugging NAT issues or confirming TURN is working.
        'iceTransportPolicy': _forceRelayOnly ? 'relay' : 'all',
        'iceServers': [
          {
            'urls': [
              'stun:turn1.ap-in-1.anedya.io:3478',
              'turn:turn1.ap-in-1.anedya.io:3478',
            ],
            'username': turnCredentials['username'],
            'credential': turnCredentials['password'],
          },
        ],
      });

      // Data channel must be created before the offer so its m-line is included
      // in the SDP. Ordered delivery ensures commands arrive in sequence.
      final dataChannelConfig = RTCDataChannelInit()..ordered = true;
      _dataChannel = await _peerConnection!.createDataChannel(
        'control',
        dataChannelConfig,
      );

      _dataChannel!.onDataChannelState = (state) {
        if (state == RTCDataChannelState.RTCDataChannelOpen) {
          _appendLog('DataChannel open - requesting timeline');
          _safeSetState(() => _showTimelinePanel = true);
          // Request an initial timeline snapshot immediately, then poll
          // every 2 seconds so the slider updates as recording grows.
          _sendDataChannelCommand({'cmd': 'timeline'});
          _timelinePollTimer = Timer.periodic(
            const Duration(seconds: 2),
            (_) => _sendDataChannelCommand({'cmd': 'timeline'}),
          );
          _timelineRenderTimer ??= Timer.periodic(
            const Duration(milliseconds: 250),
            (_) => _updateTimelineDisplay(),
          );
        }
      };

      _dataChannel!.onMessage = (message) {
        final data = jsonDecode(message.text) as Map<String, dynamic>;
        if (data['type'] == 'timeline') {
          _applyTimelineUpdate(data);
        } else if (data['type'] == 'error') {
          _appendLog('Error from Pi: ${data['message']}');
        }
      };

      // This app is viewer-only — it receives video and audio but never sends any.
      await _peerConnection!.addTransceiver(
        kind: RTCRtpMediaType.RTCRtpMediaTypeVideo,
        init: RTCRtpTransceiverInit(direction: TransceiverDirection.RecvOnly),
      );
      await _peerConnection!.addTransceiver(
        kind: RTCRtpMediaType.RTCRtpMediaTypeAudio,
        init: RTCRtpTransceiverInit(direction: TransceiverDirection.RecvOnly),
      );

      _peerConnection!.onTrack = (trackEvent) {
        _appendLog('Got remote track: ${trackEvent.track.kind}');
        if (trackEvent.track.kind == 'audio') {
          _audioTrack = trackEvent.track;
          _audioTrack!.enabled = !_isMuted;
        }
        if (trackEvent.streams.isNotEmpty) {
          _videoRenderer.srcObject = trackEvent.streams.first;
          _safeSetState(() {
            _statusText = 'Streaming';
            _isInErrorState = false;
          });
        }
      };

      _peerConnection!.onConnectionState = (connectionState) async {
        _appendLog('PC state: ${connectionState.name}');
        if (connectionState ==
            RTCPeerConnectionState.RTCPeerConnectionStateConnected) {
          await _logConnectionType();
        } else if (connectionState ==
            RTCPeerConnectionState.RTCPeerConnectionStateFailed) {
          _handleError('Connection failed');
          stopStream(logStop: false);
        }
      };
    } catch (error) {
      _handleError('Peer setup error: $error');
      stopStream(logStop: false);
      return;
    }

    // Step 3: create the SDP offer and wait for ICE candidate gathering.
    try {
      _safeSetState(() => _statusText = 'Gathering ICE...');

      // Register BEFORE setLocalDescription — gathering can complete near-instantly
      // on some platforms and the event fires before we could register after.
      final iceCompleter = Completer<void>();
      _peerConnection!.onIceGatheringState = (RTCIceGatheringState state) {
        if (state == RTCIceGatheringState.RTCIceGatheringStateComplete &&
            !iceCompleter.isCompleted) {
          iceCompleter.complete();
        }
      };

      final offer = await _peerConnection!.createOffer();
      await _peerConnection!.setLocalDescription(offer);

      try {
        await iceCompleter.future.timeout(const Duration(seconds: 8));
      } on TimeoutException {
        _appendLog(
          'ICE gathering timed out — proceeding with available candidates',
        );
      }

      final localDescription = await _peerConnection!.getLocalDescription();
      if (localDescription == null) {
        throw Exception('Local description is null');
      }

      final sdpLines = localDescription.sdp?.split('\n') ?? [];
      final hasRelayCandidates = sdpLines.any(
        (l) => l.trim().startsWith('a=candidate:') && l.contains('typ relay'),
      );
      if (!hasRelayCandidates) {
        _appendLog(
          'Warning: Failed to create relay candidate. Please check your quota limits.',
        );
      }

      // Step 4: send the offer + TURN credentials as the "webrtc_offer" command.
      // The Pi receives this over MQTT and begins its answer flow. TURN
      // credentials are bundled so both sides use the same relay allocation.
      final codec = _sdpCompressor;
      if (codec == null) {
        throw Exception('zstd codec not ready');
      }

      // The command payload has a hard ~1023-char cap. Two size reductions keep
      // a full media SDP under budget after zstd+dict:
      //   1. Drop non-relay ICE candidates. Both sides reach each other through
      //      the shared TURN relay bundled below, so host/srflx candidates in
      //      the offer add bytes (and entropy the dictionary can't model)
      //      without changing connectivity.
      //   2. Send only the three TURN fields the Pi actually reads
      //      (endpoint/username/credential) — the rest is duplicated or unused.
      final trimmedSdp = _stripNonRelayCandidates(localDescription.sdp ?? '');
      final trimmedTurn = {
        'endpoint': turnCredentials['endpoint'],
        'username': turnCredentials['username'],
        'credential': turnCredentials['credential'],
      };
      final offerJson = jsonEncode({
        'offer': {'sdp': trimmedSdp, 'type': localDescription.type},
        'turn': trimmedTurn,
      });
      // Compress (zstd + shared dict) + base64; the Pi decompresses with the
      // identical dictionary. This is what makes a full media SDP fit a command.
      final payload = codec.compressToBase64(offerJson);
      _appendLog(
        'Offer: raw=${offerJson.length}B -> command payload=${payload.length}B',
      );
      if (payload.length > 1023) {
        throw Exception(
          'Offer payload ${payload.length}B exceeds the 1023-char command cap',
        );
      }

      _safeSetState(() => _statusText = 'Sending offer...');
      final commandId = await _sendOfferViaCommand(payload);
      _appendLog('Offer sent (commandId=$commandId) - polling for answer...');
      _safeSetState(() => _statusText = 'Waiting for Pi...');

      // Step 5: poll the command status for the Pi's answer + terminal result.
      _startPollingForAnswer(commandId, codec);
    } catch (error) {
      _handleError('Offer flow error: $error');
      stopStream(logStop: false);
    }
  }

  /// Polls the command status every 2 seconds for up to 90 seconds (45
  /// attempts). While the command is "processing" the Pi attaches the answer
  /// SDP as ackdata; applying it completes the handshake. The Pi then concludes
  /// the command "success" or "failure" (terminal).
  void _startPollingForAnswer(String commandId, SdpCompressor codec) {
    int pollAttempts = 0;
    bool answerApplied = false;
    const maxAttempts = 45;
    _answerPollTimer = Timer.periodic(const Duration(seconds: 2), (
      timer,
    ) async {
      pollAttempts++;
      if (pollAttempts > maxAttempts) {
        timer.cancel();
        _answerPollTimer = null;
        if (!answerApplied) {
          _handleError('Timeout: Pi did not respond in time');
          stopStream(logStop: false);
        }
        return;
      }
      try {
        final cmd = await _getCommandStatus(commandId);
        if (cmd == null) {
          _appendLog('Status fetch failed ($pollAttempts)');
          return; // try again next tick
        }

        final status = cmd['status'];
        if (status == 'failure') {
          timer.cancel();
          _answerPollTimer = null;
          _appendLog('Command status=failure: ${cmd['ackdata'] ?? cmd['error'] ?? ''}');
          _handleError('Pi reported failure');
          stopStream(logStop: false);
          return;
        }
        if (status == 'success') {
          timer.cancel();
          _answerPollTimer = null;
          _appendLog('Command status=success (Pi confirmed connection)');
          return;
        }

        // The answer SDP rides in ackdata while status is "processing".
        final ackData = cmd['ackdata'];
        if (!answerApplied && ackData is String && ackData.isNotEmpty) {
          answerApplied = true;
          // Same compression as the offer, reversed.
          final answerSdp =
              jsonDecode(codec.decompressFromBase64(ackData)) as Map<String, dynamic>;
          await _peerConnection?.setRemoteDescription(
            RTCSessionDescription(
              answerSdp['sdp'] as String,
              answerSdp['type'] as String,
            ),
          );
          _appendLog('Answer applied - WebRTC connecting...');
        }
      } catch (error) {
        _appendLog('Poll/apply answer error: $error');
      }
    });
  }

  /// Tears down the active stream: cancels timers, closes the peer connection,
  /// clears the video renderer, and resets all UI state back to idle.
  Future<void> stopStream({bool logStop = true}) async {
    _answerPollTimer?.cancel();
    _answerPollTimer = null;
    _timelinePollTimer?.cancel();
    _timelinePollTimer = null;
    _timelineRenderTimer?.cancel();
    _timelineRenderTimer = null;

    await _closePeerConnection();
    _videoRenderer.srcObject = null;
    _audioTrack = null;
    _lastSeekTime = null;
    _resetTimelineState();

    if (logStop) _appendLog('Stream stopped');
    _safeSetState(() {
      _statusText = 'Ready - press Start';
      _networkModeText = 'Mode: --';
      _isTurnMode = false;
      _isStreamActive = false;
      _isInErrorState = false;
      _isMuted = false;
    });
  }

  Future<void> _closePeerConnection() async {
    final connection = _peerConnection;
    _peerConnection = null;
    _dataChannel = null;
    if (connection != null) {
      try {
        await connection.close();
      } catch (_) {}
    }
  }

  /// Reads WebRTC stats after connection to log whether traffic is flowing
  /// through the TURN relay or directly P2P — useful for diagnosing NAT issues.
  Future<void> _logConnectionType() async {
    final connection = _peerConnection;
    if (connection == null) return;
    try {
      final stats = await connection.getStats();
      final statsById = {for (final report in stats) report.id: report};
      for (final report in stats) {
        if (report.type != 'candidate-pair') continue;
        if (report.values['state']?.toString() != 'succeeded') continue;
        final localCandidateId = report.values['localCandidateId']?.toString();
        final remoteCandidateId = report.values['remoteCandidateId']
            ?.toString();
        final localCandidateType = localCandidateId == null
            ? null
            : statsById[localCandidateId]?.values['candidateType']?.toString();
        final remoteCandidateType = remoteCandidateId == null
            ? null
            : statsById[remoteCandidateId]?.values['candidateType']?.toString();
        _appendLog(
          localCandidateType == 'relay' || remoteCandidateType == 'relay'
              ? 'Candidate type: TURN (relayed)'
              : 'Candidate type: P2P (direct)',
        );
        final isTurn =
            localCandidateType == 'relay' || remoteCandidateType == 'relay';
        _safeSetState(() {
          _isTurnMode = isTurn;
          _networkModeText = isTurn ? 'Mode: TURN' : 'Mode: P2P';
        });
        return;
      }
    } catch (error) {
      _appendLog('Stats error: $error');
    }
  }

  void _handleError(String message) {
    _appendLog(message);
    _safeSetState(() {
      _statusText = message.startsWith('Connection failed')
          ? 'Connection failed'
          : 'Error';
      _isInErrorState = true;
      _isStreamActive = false;
    });
  }

  // Status badge colors
  Color get _statusBackgroundColor {
    if (_isInErrorState) return const Color(0xFF450A0A);
    if (_isStreamActive || _statusText.startsWith('Ready')) {
      return const Color(0xFF14532D);
    }
    return const Color(0xFF222222);
  }

  Color get _statusTextColor {
    if (_isInErrorState) return const Color(0xFFF87171);
    if (_isStreamActive || _statusText.startsWith('Ready')) {
      return const Color(0xFF4ADE80);
    }
    return const Color(0xFFEEEEEE);
  }

  Color get _networkModeBackgroundColor {
    if (_networkModeText == 'Mode: --') return const Color(0xFF222222);
    return _isTurnMode ? const Color(0xFF3B2F0A) : const Color(0xFF0F3A2E);
  }

  Color get _networkModeTextColor {
    if (_networkModeText == 'Mode: --') return const Color(0xFFAAAAAA);
    return _isTurnMode ? const Color(0xFFFACC15) : const Color(0xFF34D399);
  }

  @override
  Widget build(BuildContext context) {
    final contentWidth = MediaQuery.of(context).size.width.clamp(0.0, 640.0);
    return Scaffold(
      backgroundColor: const Color(0xFF0A0A0A),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(16),
          child: Center(
            child: SizedBox(
              width: contentWidth,
              child: Column(
                children: [
                  _buildHeader(),
                  const SizedBox(height: 8),
                  _buildDeviceStatusBadge(),
                  const SizedBox(height: 8),
                  _buildStatusBadge(),
                  const SizedBox(height: 8),
                  _buildNetworkModeBadge(),
                  const SizedBox(height: 16),
                  _buildVideoView(),
                  const SizedBox(height: 8),
                  _buildSettingsToolbar(),
                  const SizedBox(height: 8),
                  _buildControlButtons(),
                  const SizedBox(height: 8),
                  _buildRelayOnlyToggle(),
                  if (_isSettingsPanelVisible) ...[
                    const SizedBox(height: 8),
                    _buildSettingsPanel(),
                  ],
                  if (_showTimelinePanel) ...[
                    const SizedBox(height: 8),
                    _buildTimelinePanel(),
                  ],
                  const SizedBox(height: 8),
                  _buildLogOutput(),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildHeader() {
    return const Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        Text(
          'PI CAM',
          style: TextStyle(
            color: Color(0xFFEEEEEE),
            fontSize: 19,
            letterSpacing: 0.95,
          ),
        ),
        // QR pairing is disabled — the Node ID is entered manually in Settings.
        // To restore scan-to-pair: re-add mobile_scanner to pubspec.yaml,
        // uncomment the qr_code_scanner import at the top of this file, and
        // restore the IconButton below.
        //
        // IconButton(
        //   icon: const Icon(Icons.qr_code_scanner, color: Colors.white),
        //   onPressed: () async {
        //     await Navigator.push(
        //       context,
        //       MaterialPageRoute(
        //         builder: (_) => QRScannerScreen(
        //           onNodeIdScanned: (scannedNodeId) {
        //             _safeSetState(() {
        //               _nodeId = scannedNodeId;
        //               _nodeIdController.text = scannedNodeId;
        //               _statusText = 'Node set via QR';
        //             });
        //             _saveSettings();
        //           },
        //         ),
        //       ),
        //     );
        //   },
        // ),
      ],
    );
  }

  Widget _buildDeviceStatusBadge() {
    Color background;
    Color foreground;
    switch (_deviceStatus) {
      case 'online':
        background = const Color(0xFF14532D);
        foreground = const Color(0xFF4ADE80);
        break;
      case 'offline':
        background = const Color(0xFF450A0A);
        foreground = const Color(0xFFF87171);
        break;
      default:
        background = const Color(0xFF222222);
        foreground = const Color(0xFFAAAAAA);
    }
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: BoxDecoration(
        color: background,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(color: foreground, shape: BoxShape.circle),
          ),
          const SizedBox(width: 7),
          Text(
            _deviceStatusText,
            style: TextStyle(color: foreground, fontSize: 12.8),
          ),
        ],
      ),
    );
  }

  Widget _buildStatusBadge() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 13, vertical: 7),
      decoration: BoxDecoration(
        color: _statusBackgroundColor,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        _nodeId.isNotEmpty
            ? _statusText
            : 'No device configured — tap Settings',
        style: TextStyle(color: _statusTextColor, fontSize: 13.6),
      ),
    );
  }

  Widget _buildNetworkModeBadge() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      decoration: BoxDecoration(
        color: _networkModeBackgroundColor,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        _networkModeText,
        style: TextStyle(color: _networkModeTextColor, fontSize: 12.8),
      ),
    );
  }

  Widget _buildVideoView() {
    return AspectRatio(
      aspectRatio: 16 / 9,
      child: ClipRRect(
        borderRadius: BorderRadius.circular(12),
        child: Stack(
          children: [
            Positioned.fill(
              child: Container(
                color: const Color(0xFF111111),
                child: _videoRenderer.srcObject == null
                    ? const Center(
                        child: Text(
                          'Video Feed',
                          style: TextStyle(
                            color: Color(0xFFEEEEEE),
                            fontSize: 14,
                          ),
                        ),
                      )
                    : RTCVideoView(
                        _videoRenderer,
                        objectFit:
                            RTCVideoViewObjectFit.RTCVideoViewObjectFitContain,
                      ),
              ),
            ),
            // "Powered By Anedya" watermark, mirroring the browser viewer.
            // Shown only while media is flowing.
            if (_videoRenderer.srcObject != null)
              const Positioned(
                right: 10,
                bottom: 10,
                child: _AnedyaWatermark(),
              ),
          ],
        ),
      ),
    );
  }

  Widget _buildSettingsToolbar() {
    return Align(
      alignment: Alignment.centerRight,
      child: TextButton(
        onPressed: () => _safeSetState(
          () => _isSettingsPanelVisible = !_isSettingsPanelVisible,
        ),
        style: TextButton.styleFrom(
          backgroundColor: const Color(0xFF374151),
          foregroundColor: Colors.white,
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
        ),
        child: const Text('Settings'),
      ),
    );
  }

  Widget _buildControlButtons() {
    final hasDeviceConfigured = _nodeId.isNotEmpty;
    return Column(
      children: [
        Row(
          children: [
            Expanded(
              child: _buildButton(
                label: 'Start Stream',
                color: const Color(0xFF2563EB),
                onPressed: (!hasDeviceConfigured || _isStreamActive)
                    ? null
                    : _startStream,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: _buildButton(
                label: 'Stop Stream',
                color: const Color(0xFFDC2626),
                onPressed: (!hasDeviceConfigured || !_isStreamActive)
                    ? null
                    : () => stopStream(),
              ),
            ),
          ],
        ),
        // Go Live appears only when the Pi is in playback mode (scrubbing behind live).
        if (_showGoLiveButton) ...[
          const SizedBox(height: 8),
          SizedBox(
            width: double.infinity,
            child: _buildButton(
              label: 'Go Live',
              color: const Color(0xFF059669),
              onPressed: () {
                _sendDataChannelCommand({'cmd': 'live'});
                _timelineMode = 'live';
                _timelineSnapshotPositionSeconds = _totalRecordedSeconds;
                _timelineSnapshotAt = DateTime.now();
                _updateTimelineDisplay();
                _appendLog('Switched to live');
              },
            ),
          ),
        ],
      ],
    );
  }

  Widget _buildButton({
    required String label,
    required Color color,
    required VoidCallback? onPressed,
  }) {
    return ElevatedButton(
      onPressed: onPressed,
      style: ElevatedButton.styleFrom(
        backgroundColor: color,
        disabledBackgroundColor: const Color(0xFF333333),
        foregroundColor: Colors.white,
        disabledForegroundColor: const Color(0xFF666666),
        minimumSize: const Size(0, 48),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
        elevation: 0,
        textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
      ),
      child: Text(label),
    );
  }

  Widget _buildRelayOnlyToggle() {
    return Wrap(
      alignment: WrapAlignment.center,
      spacing: 16,
      children: [
        /*
        Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Checkbox(
              value: _forceRelayOnly,
              onChanged: (isChecked) {
                _safeSetState(() => _forceRelayOnly = isChecked ?? false);
                _saveSettings();
              },
              activeColor: const Color(0xFF2563EB),
            ),
            const Text(
              'Force relay/TURN only',
              style: TextStyle(color: Color(0xFFEEEEEE), fontSize: 14),
            ),
          ],
        ),
        */
        Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Checkbox(
              value: _isMuted,
              onChanged: (isChecked) {
                final muted = isChecked ?? false;
                _safeSetState(() => _isMuted = muted);
                _audioTrack?.enabled = !muted;
              },
              activeColor: const Color(0xFF2563EB),
            ),
            const Text(
              'Mute audio',
              style: TextStyle(color: Color(0xFFEEEEEE), fontSize: 14),
            ),
          ],
        ),
      ],
    );
  }

  Widget _buildSettingsPanel() {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: const Color(0xFF151515),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            'SETTINGS',
            style: TextStyle(
              color: Color(0x99EEEEEE),
              fontSize: 14,
              letterSpacing: 0.8,
            ),
          ),
          const SizedBox(height: 14),
          _buildInputField(
            label: 'Anedya Node ID',
            controller: _nodeIdController,
            hint: 'Node UUID',
          ),
          const SizedBox(height: 12),
          _buildInputField(
            label: 'Anedya API Key',
            controller: _apiKeyController,
            hint: 'Bearer ...',
            obscureText: true,
          ),
          const SizedBox(height: 14),
          SizedBox(
            width: double.infinity,
            child: ElevatedButton(
              onPressed: () {
                _safeSetState(() {
                  _nodeId = _nodeIdController.text.trim();
                  _apiKey = _apiKeyController.text.trim();
                  _isSettingsPanelVisible = false;
                });
                _saveSettings();
                _appendLog('Settings saved');
              },
              style: ElevatedButton.styleFrom(
                backgroundColor: const Color(0xFF374151),
                foregroundColor: Colors.white,
                minimumSize: const Size(0, 44),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(8),
                ),
                elevation: 0,
              ),
              child: const Text('Save Settings'),
            ),
          ),
          const SizedBox(height: 10),
          const Text(
            'Node ID and API key are stored on this device via SharedPreferences. '
            'Prefix the key with Bearer if your token needs it.',
            style: TextStyle(
              color: Color(0x80EEEEEE),
              fontSize: 12,
              height: 1.5,
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildInputField({
    required String label,
    required TextEditingController controller,
    required String hint,
    bool obscureText = false,
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label,
          style: const TextStyle(color: Color(0xBFEEEEEE), fontSize: 13),
        ),
        const SizedBox(height: 6),
        TextField(
          controller: controller,
          obscureText: obscureText,
          style: const TextStyle(color: Color(0xFFEEEEEE), fontSize: 15),
          decoration: InputDecoration(
            hintText: hint,
            hintStyle: const TextStyle(color: Color(0xFF666666)),
            filled: true,
            fillColor: const Color(0xFF111111),
            border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(8),
              borderSide: const BorderSide(color: Color(0xFF2D2D2D)),
            ),
            enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(8),
              borderSide: const BorderSide(color: Color(0xFF2D2D2D)),
            ),
            contentPadding: const EdgeInsets.symmetric(
              horizontal: 12,
              vertical: 12,
            ),
          ),
        ),
      ],
    );
  }

  /// Timeline panel — shows current position, total duration, and a scrub slider.
  /// Appears once the data channel opens; the slider becomes interactive only after
  /// the Pi finalizes its first recording segment (totalRecordedSeconds > 0).
  Widget _buildTimelinePanel() {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: const Color(0xFF151515),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        children: [
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              Text(
                _currentTimeLabel,
                style: const TextStyle(color: Color(0xBFEEEEEE), fontSize: 13),
              ),
              Text(
                _totalDurationLabel,
                style: const TextStyle(color: Color(0xBFEEEEEE), fontSize: 13),
              ),
            ],
          ),
          Slider(
            value: _totalRecordedSeconds > 0
                ? _currentPositionSeconds.clamp(0, _totalRecordedSeconds)
                : 0,
            min: 0,
            max: _totalRecordedSeconds > 0 ? _totalRecordedSeconds : 1,
            // Null callbacks render the slider as disabled (no recorded footage yet).
            onChangeStart: _totalRecordedSeconds > 0
                ? (_) => _safeSetState(() => _isUserScrubbing = true)
                : null,
            onChanged: _totalRecordedSeconds > 0
                ? (newPosition) => _safeSetState(() {
                    _currentPositionSeconds = newPosition;
                    _currentTimeLabel = _formatDuration(newPosition);
                  })
                : null,
            onChangeEnd: _totalRecordedSeconds > 0
                ? (selectedPosition) {
                    _lastSeekTime = DateTime.now();
                    _timelineMode = 'playback';
                    _timelineSnapshotPositionSeconds = selectedPosition;
                    _timelineSnapshotAt = DateTime.now();
                    _safeSetState(() {
                      _isUserScrubbing = false;
                      _showGoLiveButton = true;
                    });
                    _sendDataChannelCommand({
                      'cmd': 'seek',
                      'offset': selectedPosition,
                    });
                    _appendLog(
                      'Seeking to ${_formatDuration(selectedPosition)}',
                    );
                  }
                : null,
            activeColor: const Color(0xFF2563EB),
            inactiveColor: const Color(0xFF2D2D2D),
          ),
          Align(
            alignment: Alignment.centerLeft,
            child: Text(
              _timelineStatusText,
              style: const TextStyle(color: Color(0xBFEEEEEE), fontSize: 12),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildLogOutput() {
    return Align(
      alignment: Alignment.centerLeft,
      child: Text(
        _logOutput,
        style: const TextStyle(
          color: Color(0x80EEEEEE),
          fontSize: 12,
          height: 1.6,
        ),
      ),
    );
  }
}

/// "Powered By" + Anedya logo watermark overlaid on the video feed, mirroring
/// the browser viewer's corner badge (text + the same SVG mark at ~12px tall).
class _AnedyaWatermark extends StatelessWidget {
  const _AnedyaWatermark();

  @override
  Widget build(BuildContext context) {
    return Opacity(
      opacity: 0.8,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 4),
        decoration: BoxDecoration(
          color: const Color(0x66000000),
          borderRadius: BorderRadius.circular(6),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Text(
              'Powered By',
              style: TextStyle(
                color: Colors.white,
                fontSize: 11,
                letterSpacing: 0.2,
              ),
            ),
            const SizedBox(width: 6),
            SvgPicture.asset('assets/anedya_logo.svg', height: 12),
          ],
        ),
      ),
    );
  }
}
