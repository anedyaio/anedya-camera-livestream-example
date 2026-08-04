import 'dart:convert';
import 'dart:ffi' as ffi;
import 'dart:io' show Platform;
import 'dart:typed_data';

import 'package:ffi/ffi.dart';
import 'package:flutter/services.dart' show rootBundle;
import 'package:zstandard_native/zstandard_native.dart';

/// zstd + trained-dictionary compression for WebRTC signaling payloads.
///
/// The offer (and the Pi's answer) are large WebRTC SDPs whose bulk is constant
/// boilerplate. A shared trained dictionary lets each payload carry only its
/// deltas, shrinking the value dramatically so a full SDP fits inside an Anedya
/// command payload (~1023-char cap).
///
/// This is the Dart counterpart of the browser's `window.sdpCompress` /
/// `sdpDecompress` (see peer/public/index.html) and the streamer's
/// `_compress_signaling` / `_decompress_signaling` (see camera_streamer.py).
/// All three MUST use byte-identical dictionaries:
///   assets/signaling_dict.bin == streamer/signaling_dict.bin == peer zstd-dict.bin
class SdpCompressor {
  SdpCompressor._(this._bindings, this._dict);

  // Match the browser/streamer level so output is interoperable in size budget.
  static const int _zstdLevel = 19;
  static const String _dictAsset = 'assets/signaling_dict.bin';

  final ZstandardNativeBindings _bindings;
  final Uint8List _dict;

  static SdpCompressor? _instance;

  /// Loads libzstd + the shared dictionary once. Safe to call repeatedly.
  static Future<SdpCompressor> instance() async {
    final existing = _instance;
    if (existing != null) return existing;

    final lib = _openLibrary();
    final bindings = ZstandardNativeBindings(lib);
    final dictData = await rootBundle.load(_dictAsset);
    final dict = dictData.buffer.asUint8List(
      dictData.offsetInBytes,
      dictData.lengthInBytes,
    );
    return _instance = SdpCompressor._(bindings, dict);
  }

  /// Opens the native libzstd shipped by the `zstandard` platform plugins.
  /// The library name matches each plugin's `default_package` output.
  static ffi.DynamicLibrary _openLibrary() {
    if (Platform.isAndroid) {
      return ffi.DynamicLibrary.open('libzstandard_android.so');
    }
    if (Platform.isIOS) {
      return ffi.DynamicLibrary.open('zstandard_ios.framework/zstandard_ios');
    }
    if (Platform.isMacOS) {
      return ffi.DynamicLibrary.open('zstandard_macos.framework/zstandard_macos');
    }
    if (Platform.isWindows) {
      return ffi.DynamicLibrary.open('zstandard_windows.dll');
    }
    if (Platform.isLinux) {
      return ffi.DynamicLibrary.open('libzstandard_linux_plugin.so');
    }
    throw UnsupportedError('zstd signaling not supported on this platform');
  }

  /// JSON string -> base64(zstd-with-dict). Mirrors browser `sdpCompress`.
  String compressToBase64(String jsonStr) {
    final src = Uint8List.fromList(utf8.encode(jsonStr));
    final bound = _bindings.ZSTD_compressBound(src.length);

    final srcPtr = malloc<ffi.Uint8>(src.length);
    final dstPtr = malloc<ffi.Uint8>(bound);
    final dictPtr = malloc<ffi.Uint8>(_dict.length);
    final cctx = _bindings.ZSTD_createCCtx();
    try {
      srcPtr.asTypedList(src.length).setAll(0, src);
      dictPtr.asTypedList(_dict.length).setAll(0, _dict);

      final written = _bindings.ZSTD_compress_usingDict(
        cctx,
        dstPtr.cast(),
        bound,
        srcPtr.cast(),
        src.length,
        dictPtr.cast(),
        _dict.length,
        _zstdLevel,
      );
      _checkError(written, 'compress');

      final out = Uint8List.fromList(dstPtr.asTypedList(written));
      return base64.encode(out);
    } finally {
      _bindings.ZSTD_freeCCtx(cctx);
      malloc.free(srcPtr);
      malloc.free(dstPtr);
      malloc.free(dictPtr);
    }
  }

  /// base64(zstd-with-dict) -> JSON string. Mirrors browser `sdpDecompress`.
  String decompressFromBase64(String b64) {
    final src = base64.decode(b64);

    final srcPtr = malloc<ffi.Uint8>(src.length);
    final dictPtr = malloc<ffi.Uint8>(_dict.length);
    srcPtr.asTypedList(src.length).setAll(0, src);
    dictPtr.asTypedList(_dict.length).setAll(0, _dict);

    // The one-shot compressor writes the content size into the frame header,
    // so we can size the destination exactly.
    final contentSize =
        _bindings.ZSTD_getFrameContentSize(srcPtr.cast(), src.length);
    if (contentSize <= 0) {
      malloc.free(srcPtr);
      malloc.free(dictPtr);
      throw StateError('zstd: unknown frame content size');
    }

    final dstPtr = malloc<ffi.Uint8>(contentSize);
    final dctx = _bindings.ZSTD_createDCtx();
    try {
      final written = _bindings.ZSTD_decompress_usingDict(
        dctx,
        dstPtr.cast(),
        contentSize,
        srcPtr.cast(),
        src.length,
        dictPtr.cast(),
        _dict.length,
      );
      _checkError(written, 'decompress');
      return utf8.decode(dstPtr.asTypedList(written));
    } finally {
      _bindings.ZSTD_freeDCtx(dctx);
      malloc.free(srcPtr);
      malloc.free(dstPtr);
      malloc.free(dictPtr);
    }
  }

  void _checkError(int code, String op) {
    if (_bindings.ZSTD_isError(code) != 0) {
      final namePtr = _bindings.ZSTD_getErrorName(code);
      final name = namePtr.cast<Utf8>().toDartString();
      throw StateError('zstd $op failed: $name');
    }
  }
}
