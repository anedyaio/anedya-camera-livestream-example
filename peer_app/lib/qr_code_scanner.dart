import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

class QRScannerScreen extends StatefulWidget {
  final void Function(String nodeId) onScan;

  const QRScannerScreen({super.key, required this.onScan});

  @override
  State<QRScannerScreen> createState() => _QRScannerScreenState();
}

class _QRScannerScreenState extends State<QRScannerScreen> {
  final TextEditingController _ctrl = TextEditingController();
  final MobileScannerController _scanner = MobileScannerController();

  @override
  void dispose() {
    _scanner.dispose();
    _ctrl.dispose();
    super.dispose();
  }

  void _submit(String value) {
    if (value.trim().isEmpty) return;
    widget.onScan(value.trim());
    Navigator.pop(context);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF0A0A0A),
      appBar: AppBar(
        title: const Text('Scan QR'),
        backgroundColor: const Color(0xFF0A0A0A),
        foregroundColor: Colors.white,
      ),
      body: SafeArea(
        child: Column(
          children: [
            Expanded(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(12),
                  child: MobileScanner(
                    controller: _scanner,
                    onDetect: (barcode) async {
                      final raw = barcode.barcodes.first.rawValue;
                      if (raw == null) return;
                      try {
                        final data = jsonDecode(raw);
                        final nodeId = data['node_id']?.toString();
                        if (nodeId != null && nodeId.isNotEmpty) {
                          await _scanner.stop();
                          widget.onScan(nodeId);
                          if (mounted) Navigator.pop(context);
                        }
                      } catch (_) {}
                    },
                  ),
                ),
              ),
            ),
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 8),
              child: Text(
                'OR ENTER MANUALLY',
                style: TextStyle(color: Color(0x80EEEEEE), fontSize: 12, letterSpacing: 1.2),
              ),
            ),
            Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                children: [
                  TextField(
                    controller: _ctrl,
                    style: const TextStyle(color: Colors.white),
                    onSubmitted: _submit,
                    decoration: InputDecoration(
                      hintText: 'Enter Node ID',
                      hintStyle: const TextStyle(color: Color(0xFF666666)),
                      filled: true,
                      fillColor: const Color(0xFF111111),
                      border: OutlineInputBorder(
                        borderRadius: BorderRadius.circular(8),
                        borderSide: BorderSide.none,
                      ),
                    ),
                  ),
                  const SizedBox(height: 12),
                  SizedBox(
                    width: double.infinity,
                    child: ElevatedButton(
                      onPressed: () => _submit(_ctrl.text),
                      style: ElevatedButton.styleFrom(
                        backgroundColor: const Color(0xFF2563EB),
                        foregroundColor: Colors.white,
                        minimumSize: const Size(0, 48),
                        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
                        elevation: 0,
                      ),
                      child: const Text(
                        'Connect',
                        style: TextStyle(fontWeight: FontWeight.w600),
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
