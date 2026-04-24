import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter_webrtc/flutter_webrtc.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

import 'qr_code_scanner.dart';

const String _kApiBase = 'https://api.ap-in-1.anedya.io/v1';
const String _kPrefNodeId = 'peer_app.nodeId';
const String _kPrefApiKey = 'peer_app.apiKey';
const String _kPrefRelayOnly = 'peer_app.relayOnly';

class PeerCamScreen extends StatefulWidget {
  const PeerCamScreen({super.key});

  @override
  State<PeerCamScreen> createState() => _PeerCamScreenState();
}

class _PeerCamScreenState extends State<PeerCamScreen> {
  String _nodeId = '';
  String _apiKey = '';
  bool _relayOnly = false;

  late TextEditingController _nodeIdCtrl;
  late TextEditingController _apiKeyCtrl;
  bool _showSettings = false;

  bool _isStreaming = false;
  bool _hasError = false;
  String _status = 'Ready - press Start';
  String _logs = '';

  final RTCVideoRenderer _renderer = RTCVideoRenderer();
  RTCPeerConnection? _pc;
  RTCDataChannel? _dc;
  Timer? _pollTimer;
  Timer? _timelineTimer;
  bool _disposed = false;

  double _sliderMax = 0.0;
  double _sliderValue = 0.0;
  bool _draggingSlider = false;
  bool _showTimeline = false;
  bool _showLiveBtn = false;
  String _timelineStatus =
      'Recording starts immediately. Playback appears after first finalized segment.';
  String _timelineCurrent = '00:00';
  String _timelineEnd = 'LIVE';

  @override
  void initState() {
    super.initState();
    _renderer.initialize();
    _nodeIdCtrl = TextEditingController();
    _apiKeyCtrl = TextEditingController();
    _loadPrefs();
  }

  Future<void> _loadPrefs() async {
    final prefs = await SharedPreferences.getInstance();
    _ss(() {
      _nodeId = prefs.getString(_kPrefNodeId) ?? '';
      _apiKey = prefs.getString(_kPrefApiKey) ?? '';
      _relayOnly = prefs.getBool(_kPrefRelayOnly) ?? false;
      _nodeIdCtrl.text = _nodeId;
      _apiKeyCtrl.text = _apiKey;
    });
  }

  Future<void> _savePrefs() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_kPrefNodeId, _nodeId);
    await prefs.setString(_kPrefApiKey, _apiKey);
    await prefs.setBool(_kPrefRelayOnly, _relayOnly);
  }

  @override
  void dispose() {
    _disposed = true;
    _pollTimer?.cancel();
    _timelineTimer?.cancel();
    _closePc();
    _renderer.dispose();
    _nodeIdCtrl.dispose();
    _apiKeyCtrl.dispose();
    super.dispose();
  }

  void _ss(VoidCallback fn) {
    if (!_disposed && mounted) setState(fn);
  }

  void _log(String msg) => _ss(() => _logs += '$msg\n');

  Map<String, String> get _headers => {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Authorization': 'Bearer $_apiKey',
      };

  Map<String, dynamic> get _vsNs => {'scope': 'node', 'id': _nodeId};

  Future<void> _vsSet(String key, String value) async {
    final r = await http.post(
      Uri.parse('$_kApiBase/valuestore/setValue'),
      headers: _headers,
      body: jsonEncode({'namespace': _vsNs, 'key': key, 'value': value, 'type': 'string'}),
    );
    if (r.statusCode < 200 || r.statusCode >= 300) {
      throw Exception('vsSet failed: ${r.statusCode} ${r.body}');
    }
  }

  Future<String?> _vsGet(String key) async {
    final r = await http.post(
      Uri.parse('$_kApiBase/valuestore/getValue'),
      headers: _headers,
      body: jsonEncode({'namespace': _vsNs, 'key': key}),
    );
    if (r.statusCode < 200 || r.statusCode >= 300) return null;
    final d = jsonDecode(r.body);
    if (d is! Map<String, dynamic>) return null;
    final v = d['value'];
    return v is String && v.isNotEmpty ? v : null;
  }

  Future<Map<String, dynamic>> _fetchTurn() async {
    final r = await http.post(
      Uri.parse('$_kApiBase/relay/create'),
      headers: _headers,
      body: jsonEncode({'relayType': 'turn'}),
    );
    if (r.statusCode < 200 || r.statusCode >= 300) {
      throw Exception('TURN fetch failed: ${r.statusCode}');
    }
    final d = jsonDecode(r.body);
    if (d is! Map<String, dynamic> || d['relayData'] is! Map) {
      throw Exception(d['error']?.toString() ?? 'No relayData');
    }
    final rd = Map<String, dynamic>.from(d['relayData'] as Map);
    rd['password'] = rd['credential'];
    rd['relayExpiry'] = d['relayExpiry'];
    return rd;
  }

  void _sendCmd(Map<String, dynamic> cmd) {
    final dc = _dc;
    if (dc != null && dc.state == RTCDataChannelState.RTCDataChannelOpen) {
      dc.send(RTCDataChannelMessage(jsonEncode(cmd)));
    }
  }

  void _renderTimeline(Map<String, dynamic> msg) {
    final duration = (msg['duration'] as num?)?.toDouble() ?? 0.0;
    final position = msg['playback_offset'] != null
        ? (msg['playback_offset'] as num).toDouble()
        : duration;

    _ss(() {
      _showTimeline = true;
      _sliderMax = duration;
      if (!_draggingSlider) {
        _sliderValue = duration > 0 ? position.clamp(0.0, duration) : 0.0;
      }
      _timelineCurrent = _fmtTime(_sliderValue);
      _timelineEnd = duration > 0 ? _fmtTime(duration) : 'LIVE';

      if (duration <= 0) {
        _timelineStatus =
            'Recording starts immediately. Playback appears after first finalized segment.';
        _showLiveBtn = false;
        return;
      }

      if (msg['mode'] == 'live') {
        _timelineStatus = 'Live mode';
        _showLiveBtn = false;
      } else {
        final behind = (duration - position).clamp(0.0, double.infinity);
        _timelineStatus = 'Playback mode - ${_fmtTime(behind)} behind live';
        _showLiveBtn = true;
      }
    });
  }

  String _fmtTime(double totalSeconds) {
    final s = totalSeconds.clamp(0, double.infinity).toInt();
    final h = s ~/ 3600;
    final m = (s % 3600) ~/ 60;
    final sec = s % 60;
    if (h > 0) {
      return '${h.toString().padLeft(2, '0')}:${m.toString().padLeft(2, '0')}:${sec.toString().padLeft(2, '0')}';
    }
    return '${m.toString().padLeft(2, '0')}:${sec.toString().padLeft(2, '0')}';
  }

  void _resetTimeline() {
    _ss(() {
      _showTimeline = false;
      _showLiveBtn = false;
      _sliderMax = 0.0;
      _sliderValue = 0.0;
      _draggingSlider = false;
      _timelineCurrent = '00:00';
      _timelineEnd = 'LIVE';
      _timelineStatus =
          'Recording starts immediately. Playback appears after first finalized segment.';
    });
  }

  Future<void> _startStream() async {
    _pollTimer?.cancel();
    _timelineTimer?.cancel();
    await _closePc();

    _ss(() {
      _status = 'Fetching TURN credentials...';
      _isStreaming = true;
      _hasError = false;
    });

    Map<String, dynamic> relay;
    try {
      relay = await _fetchTurn();
      final exp = relay['relayExpiry'];
      _log(exp is num
          ? 'TURN ready (expires ${DateTime.fromMillisecondsSinceEpoch(exp.toInt() * 1000).toLocal().toIso8601String()})'
          : 'TURN ready');
    } catch (e) {
      _failWithError('TURN error: $e');
      return;
    }

    try {
      _pc = await createPeerConnection({
        'iceTransportPolicy': _relayOnly ? 'relay' : 'all',
        'iceServers': [
          {
            'urls': [
              'stun:turn1.ap-in-1.anedya.io:3478',
              'turn:turn1.ap-in-1.anedya.io:3478',
            ],
            'username': relay['username'],
            'credential': relay['password'],
          }
        ],
      });

      final dcInit = RTCDataChannelInit()..ordered = true;
      _dc = await _pc!.createDataChannel('control', dcInit);

      _dc!.onDataChannelState = (state) {
        if (state == RTCDataChannelState.RTCDataChannelOpen) {
          _log('DataChannel open - requesting timeline');
          _ss(() => _showTimeline = true);
          _sendCmd({'cmd': 'timeline'});
          _timelineTimer = Timer.periodic(
            const Duration(seconds: 2),
            (_) => _sendCmd({'cmd': 'timeline'}),
          );
        }
      };

      _dc!.onMessage = (msg) {
        final data = jsonDecode(msg.text) as Map<String, dynamic>;
        if (data['type'] == 'timeline') {
          _renderTimeline(data);
        } else if (data['type'] == 'error') {
          _log('Error: ${data['message']}');
        }
      };

      await _pc!.addTransceiver(
        kind: RTCRtpMediaType.RTCRtpMediaTypeVideo,
        init: RTCRtpTransceiverInit(direction: TransceiverDirection.RecvOnly),
      );
      await _pc!.addTransceiver(
        kind: RTCRtpMediaType.RTCRtpMediaTypeAudio,
        init: RTCRtpTransceiverInit(direction: TransceiverDirection.RecvOnly),
      );

      _pc!.onTrack = (e) {
        _log('Got remote track: ${e.track.kind}');
        if (e.streams.isNotEmpty) {
          _renderer.srcObject = e.streams.first;
          _ss(() {
            _status = 'Streaming';
            _hasError = false;
          });
        }
      };

      _pc!.onConnectionState = (state) async {
        _log('PC state: ${state.name}');
        if (state == RTCPeerConnectionState.RTCPeerConnectionStateConnected) {
          await _logTransport();
        } else if (state == RTCPeerConnectionState.RTCPeerConnectionStateFailed) {
          _failWithError('Connection failed');
          stopStream(logStop: false);
        }
      };
    } catch (e) {
      _failWithError('Peer setup error: $e');
      stopStream(logStop: false);
      return;
    }

    try {
      _ss(() => _status = 'Gathering ICE...');
      final offer = await _pc!.createOffer();
      await _pc!.setLocalDescription(offer);

      await Future.delayed(const Duration(seconds: 2));

      final local = await _pc!.getLocalDescription();
      if (local == null) throw Exception('Local description null');

      final sessionId = _newSessionId();
      final offerKey = 'offer_$sessionId';
      final answerKey = 'answer_$sessionId';

      _ss(() => _status = 'Sending offer...');
      await _vsSet(
        offerKey,
        jsonEncode({
          'offer': {'sdp': local.sdp, 'type': local.type},
          'turn': relay,
        }),
      );
      _log('Offer written (key=$offerKey) - polling for answer...');
      _ss(() => _status = 'Waiting for Pi...');

      _startPollAnswer(answerKey);
    } catch (e) {
      _failWithError('Offer flow error: $e');
      stopStream(logStop: false);
    }
  }

  void _startPollAnswer(String answerKey) {
    int attempts = 0;
    _pollTimer = Timer.periodic(const Duration(seconds: 2), (timer) async {
      attempts++;
      if (attempts > 30) {
        timer.cancel();
        _pollTimer = null;
        _failWithError('Timeout: Pi did not respond in time');
        stopStream(logStop: false);
        return;
      }
      try {
        final value = await _vsGet(answerKey);
        if (value == null) return;
        timer.cancel();
        _pollTimer = null;
        final d = jsonDecode(value) as Map<String, dynamic>;
        await _pc?.setRemoteDescription(
          RTCSessionDescription(d['sdp'] as String, d['type'] as String),
        );
        _log('Answer applied - WebRTC connecting...');
      } catch (e) {
        _log('Poll error: $e');
      }
    });
  }

  Future<void> stopStream({bool logStop = true}) async {
    _pollTimer?.cancel();
    _pollTimer = null;
    _timelineTimer?.cancel();
    _timelineTimer = null;

    await _closePc();
    _renderer.srcObject = null;
    _resetTimeline();

    if (logStop) _log('Stream stopped');
    _ss(() {
      _status = 'Ready - press Start';
      _isStreaming = false;
      _hasError = false;
    });
  }

  Future<void> _closePc() async {
    final pc = _pc;
    _pc = null;
    _dc = null;
    if (pc != null) {
      try {
        await pc.close();
      } catch (_) {}
    }
  }

  Future<void> _logTransport() async {
    final pc = _pc;
    if (pc == null) return;
    try {
      final stats = await pc.getStats();
      final byId = {for (final r in stats) r.id: r};
      for (final r in stats) {
        if (r.type != 'candidate-pair') continue;
        if (r.values['state']?.toString() != 'succeeded') continue;
        final localId = r.values['localCandidateId']?.toString();
        final remoteId = r.values['remoteCandidateId']?.toString();
        final lt = localId == null ? null : byId[localId]?.values['candidateType']?.toString();
        final rt = remoteId == null ? null : byId[remoteId]?.values['candidateType']?.toString();
        _log(lt == 'relay' || rt == 'relay' ? 'Candidate type: TURN' : 'Candidate type: P2P');
        return;
      }
    } catch (e) {
      _log('Stats error: $e');
    }
  }

  void _failWithError(String msg) {
    _log(msg);
    _ss(() {
      _status = msg.startsWith('Connection failed') ? 'Connection failed' : 'Error';
      _hasError = true;
      _isStreaming = false;
    });
  }

  String _newSessionId() {
    const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
    final rand = Random.secure();
    return List.generate(8, (_) => chars[rand.nextInt(chars.length)]).join();
  }

  Color get _statusBg {
    if (_hasError) return const Color(0xFF450A0A);
    if (_isStreaming || _status.startsWith('Ready')) return const Color(0xFF14532D);
    return const Color(0xFF222222);
  }

  Color get _statusFg {
    if (_hasError) return const Color(0xFFF87171);
    if (_isStreaming || _status.startsWith('Ready')) return const Color(0xFF4ADE80);
    return const Color(0xFFEEEEEE);
  }

  @override
  Widget build(BuildContext context) {
    final w = MediaQuery.of(context).size.width.clamp(0.0, 640.0);
    return Scaffold(
      backgroundColor: const Color(0xFF0A0A0A),
      body: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(16),
          child: Center(
            child: SizedBox(
              width: w,
              child: Column(
                children: [
                  _buildHeader(),
                  const SizedBox(height: 8),
                  _buildStatus(),
                  const SizedBox(height: 16),
                  _buildVideo(),
                  const SizedBox(height: 8),
                  _buildToolbar(),
                  const SizedBox(height: 8),
                  _buildButtons(),
                  const SizedBox(height: 8),
                  _buildRelayToggle(),
                  if (_showSettings) ...[const SizedBox(height: 8), _buildSettingsPanel()],
                  if (_showTimeline) ...[const SizedBox(height: 8), _buildTimeline()],
                  const SizedBox(height: 8),
                  _buildLogs(),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildHeader() {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        const Text(
          'PI CAM',
          style: TextStyle(
            color: Color(0xFFEEEEEE),
            fontSize: 19,
            letterSpacing: 0.95,
          ),
        ),
        IconButton(
          icon: const Icon(Icons.qr_code_scanner, color: Colors.white),
          onPressed: () async {
            await Navigator.push(
              context,
              MaterialPageRoute(
                builder: (_) => QRScannerScreen(
                  onScan: (nodeId) {
                    _ss(() {
                      _nodeId = nodeId;
                      _nodeIdCtrl.text = nodeId;
                      _status = 'Node set via QR';
                    });
                    _savePrefs();
                  },
                ),
              ),
            );
          },
        ),
      ],
    );
  }

  Widget _buildStatus() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 13, vertical: 7),
      decoration: BoxDecoration(
        color: _statusBg,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        _nodeId.isNotEmpty ? _status : 'No device configured — tap Settings',
        style: TextStyle(color: _statusFg, fontSize: 13.6),
      ),
    );
  }

  Widget _buildVideo() {
    return AspectRatio(
      aspectRatio: 16 / 9,
      child: ClipRRect(
        borderRadius: BorderRadius.circular(12),
        child: Container(
          color: const Color(0xFF111111),
          child: _renderer.srcObject == null
              ? const Center(
                  child: Text(
                    'Video Feed',
                    style: TextStyle(color: Color(0xFFEEEEEE), fontSize: 14),
                  ),
                )
              : RTCVideoView(
                  _renderer,
                  objectFit: RTCVideoViewObjectFit.RTCVideoViewObjectFitContain,
                ),
        ),
      ),
    );
  }

  Widget _buildToolbar() {
    return Align(
      alignment: Alignment.centerRight,
      child: TextButton(
        onPressed: () => _ss(() => _showSettings = !_showSettings),
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

  Widget _buildButtons() {
    final hasDevice = _nodeId.isNotEmpty;
    return Column(
      children: [
        Row(
          children: [
            Expanded(
              child: _btn(
                'Start Stream',
                const Color(0xFF2563EB),
                (!hasDevice || _isStreaming) ? null : _startStream,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: _btn(
                'Stop Stream',
                const Color(0xFFDC2626),
                (!hasDevice || !_isStreaming) ? null : () => stopStream(),
              ),
            ),
          ],
        ),
        if (_showLiveBtn) ...[
          const SizedBox(height: 8),
          SizedBox(
            width: double.infinity,
            child: _btn('Go Live', const Color(0xFF059669), () {
              _sendCmd({'cmd': 'live'});
              _log('Switched to live');
            }),
          ),
        ],
      ],
    );
  }

  Widget _btn(String label, Color color, VoidCallback? onPressed) {
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

  Widget _buildRelayToggle() {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        Checkbox(
          value: _relayOnly,
          onChanged: (v) {
            _ss(() => _relayOnly = v ?? false);
            _savePrefs();
          },
          activeColor: const Color(0xFF2563EB),
        ),
        const Text(
          'Force relay/TURN only',
          style: TextStyle(color: Color(0xFFEEEEEE), fontSize: 14),
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
            style: TextStyle(color: Color(0x99EEEEEE), fontSize: 14, letterSpacing: 0.8),
          ),
          const SizedBox(height: 14),
          _field('Anedya Node ID', _nodeIdCtrl, 'Node UUID'),
          const SizedBox(height: 12),
          _field('Anedya API Key', _apiKeyCtrl, 'Raw API key (no Bearer prefix)', obscure: true),
          const SizedBox(height: 14),
          SizedBox(
            width: double.infinity,
            child: ElevatedButton(
              onPressed: () {
                _ss(() {
                  _nodeId = _nodeIdCtrl.text.trim();
                  _apiKey = _apiKeyCtrl.text.trim();
                  _showSettings = false;
                });
                _savePrefs();
                _log('Settings saved');
              },
              style: ElevatedButton.styleFrom(
                backgroundColor: const Color(0xFF374151),
                foregroundColor: Colors.white,
                minimumSize: const Size(0, 44),
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                elevation: 0,
              ),
              child: const Text('Save Settings'),
            ),
          ),
          const SizedBox(height: 10),
          const Text(
            'Node ID and API key are stored on this device via SharedPreferences. '
            'Enter the raw API key — Bearer is added automatically.',
            style: TextStyle(color: Color(0x80EEEEEE), fontSize: 12, height: 1.5),
          ),
        ],
      ),
    );
  }

  Widget _field(
    String label,
    TextEditingController ctrl,
    String hint, {
    bool obscure = false,
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label, style: const TextStyle(color: Color(0xBFEEEEEE), fontSize: 13)),
        const SizedBox(height: 6),
        TextField(
          controller: ctrl,
          obscureText: obscure,
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
            contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 12),
          ),
        ),
      ],
    );
  }

  Widget _buildTimeline() {
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
                _timelineCurrent,
                style: const TextStyle(color: Color(0xBFEEEEEE), fontSize: 13),
              ),
              Text(
                _timelineEnd,
                style: const TextStyle(color: Color(0xBFEEEEEE), fontSize: 13),
              ),
            ],
          ),
          Slider(
            value: _sliderMax > 0 ? _sliderValue.clamp(0, _sliderMax) : 0,
            min: 0,
            max: _sliderMax > 0 ? _sliderMax : 1,
            onChangeStart: _sliderMax > 0 ? (_) => _ss(() => _draggingSlider = true) : null,
            onChanged: _sliderMax > 0
                ? (v) => _ss(() {
                      _sliderValue = v;
                      _timelineCurrent = _fmtTime(v);
                    })
                : null,
            onChangeEnd: _sliderMax > 0
                ? (v) {
                    _ss(() => _draggingSlider = false);
                    _sendCmd({'cmd': 'seek', 'offset': v});
                    _log('Seeking to ${_fmtTime(v)}');
                  }
                : null,
            activeColor: const Color(0xFF2563EB),
            inactiveColor: const Color(0xFF2D2D2D),
          ),
          Align(
            alignment: Alignment.centerLeft,
            child: Text(
              _timelineStatus,
              style: const TextStyle(color: Color(0xBFEEEEEE), fontSize: 12),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildLogs() {
    return Align(
      alignment: Alignment.centerLeft,
      child: Text(
        _logs,
        style: const TextStyle(color: Color(0x80EEEEEE), fontSize: 12, height: 1.6),
      ),
    );
  }
}
