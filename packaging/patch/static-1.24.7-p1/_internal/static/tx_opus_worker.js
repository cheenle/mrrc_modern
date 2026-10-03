// TX Opus Worker — Opus-encodes mic frames and posts packets to the main thread.
//
// Architecture (as currently wired):
//   AudioWorklet (audio thread) → postMessage('frame') → main thread
//        → postMessage('float_frame') → this Worker (encode)
//        → postMessage({type:'tx_audio', data:ArrayBuffer}) → main thread
//        → wsAudioTX.send(tagged) → server
//
// (A SharedArrayBuffer ring path also exists in this file — sabInit/pollSAB —
// but the main thread never allocates the SAB, so it is currently dormant.)
//
// Previously the Worker had its own WebSocket.  On mobile Safari, ws.send()
// blocks the Worker's event loop on TCP backpressure (WiFi power-save stalls),
// causing the "burst-then-gap" pattern the server saw as 50-172 ms gaps in
// /tmp/tx_rx_probe.csv.  By posting encoded frames to the main thread via
// zero-copy Transferable, the Worker never blocks — the main thread's higher-
// priority event loop handles WebSocket backpressure without starving the
// SAB poll timer.  The main thread was already sending 'm:' settings and 's:'
// stop, so no control-plane changes are needed.
var AUDIO_TAG_OPUS = 0x01;
var AUDIO_TAG_PCM  = 0x00;
var FRAME_SIZE = 960;   // 20 ms @ 48 kHz

// Opus WASM runtime + codec (public assets, no auth needed).
// importScripts is synchronous — the OpusEncoder global is ready
// before the first onmessage handler runs.
importScripts('/modules/opus_wasm.js?v=tx-audio-4', '/modules/opus_codec.js?v=tx-audio-4');

// ── SAB ring buffer (consumer side) ────────────────────
// Layout: word[0]=write_pos, word[1]=read_pos, word[2+]=float32 data

var _sab = null;
var _ringMask = 0;
var _writePtr = null;
var _readPtr = null;
var _data = null;

function sabInit(sab, ringSize) {
  _sab = sab;
  _ringMask = ringSize - 1;
  _writePtr = new Uint32Array(sab, 0, 1);
  _readPtr  = new Uint32Array(sab, 4, 1);
  _data     = new Float32Array(sab, 8, ringSize);
}

function sabAvailable() {
  if (!_sab) return 0;
  return Atomics.load(_writePtr) - Atomics.load(_readPtr);
}

function sabRead(n) {
  if (!_sab) return null;
  var wp = Atomics.load(_writePtr);
  var rp = Atomics.load(_readPtr);
  var avail = wp - rp;
  n = Math.min(n, avail);
  if (n <= 0) return new Float32Array(0);
  var out = new Float32Array(n);
  var idx = rp & _ringMask;
  var size = _ringMask + 1;
  var first = Math.min(n, size - idx);
  for (var i = 0; i < first; i++) out[i] = _data[idx + i];
  if (n > first) {
    for (var i = 0; i < n - first; i++) out[first + i] = _data[i];
  }
  Atomics.store(_readPtr, 0, rp + n);
  return out;
}

// ── Opus encoder ───────────────────────────────────────

var encoder = null;
var running = false;
var _pollTimer = null;
var _useOpus = true;      // true = Opus encode, false = raw Int16 PCM
var _toneTimer = null;
var _tonePhase = 0;
var _toneFreq = 1000;
var _toneLevel = 0.2;

function ensureEncoder() {
  if (!encoder) {
    encoder = new OpusEncoder(48000, 1, 2048, 20);
  }
}

// Post a codec-tagged frame to the main thread for WebSocket delivery.
// Transfers the buffer (zero-copy) so the ~160-byte Opus packet (64kbps CBR)
// never crosses threads — only the ArrayBuffer handle moves.
function postEncodedFrame(tag, payload) {
  var src = new Uint8Array(payload);
  var tagged = new Uint8Array(1 + src.length);
  tagged[0] = tag;
  tagged.set(src, 1);
  self.postMessage({ type: 'tx_audio', data: tagged.buffer }, [tagged.buffer]);
}

function encodeAndPost(floatSamples) {
  if (_useOpus) {
    ensureEncoder();
    var packets = encoder.encode_float(floatSamples);
    for (var p = 0; p < packets.length; p++) {
      postEncodedFrame(AUDIO_TAG_OPUS, packets[p]);
    }
  } else {
    // PCM fallback: convert float32 → Int16
    var i16 = new Int16Array(floatSamples.length);
    for (var i = 0; i < floatSamples.length; i++) {
      var v = floatSamples[i] * 32767;
      i16[i] = v > 32767 ? 32767 : (v < -32768 ? -32768 : (v | 0));
    }
    postEncodedFrame(AUDIO_TAG_PCM, new Uint8Array(i16.buffer, i16.byteOffset, i16.byteLength));
  }
}

// ── Poll loop: one frame per poll, self-scheduling ─────
// AudioWorklet writes ~480 float32 samples to SAB every ~10 ms.
// A complete 960-sample Opus frame is ready every ~20 ms.  Polling
// at 5 ms catches each frame within 5 ms of readiness without ever
// draining more than one at a time.  The self-scheduling setTimeout
// (not setInterval) avoids callback stacking when the main thread
// is briefly busy processing a previous tx_audio post.

function pollSAB() {
  if (!running) return;
  var avail = sabAvailable();
  if (avail >= FRAME_SIZE) {
    var frame = sabRead(FRAME_SIZE);
    if (frame && frame.length >= FRAME_SIZE) {
      encodeAndPost(frame);
    }
  }
}

var _pollTimer = null;

function _scheduleNextPoll() {
  if (!running) return;
  _pollTimer = setTimeout(function() {
    pollSAB();
    _scheduleNextPoll();
  }, 5);
}

function startPolling() {
  if (_pollTimer) return;
  _scheduleNextPoll();
}

function stopPolling() {
  if (_pollTimer) {
    clearTimeout(_pollTimer);
    _pollTimer = null;
  }
}

function startTone(freq, level) {
  stopTone();
  ensureEncoder();
  _toneFreq = Math.max(20, Math.min(3000, Number(freq) || 1000));
  _toneLevel = Math.max(0.01, Math.min(0.8, Number(level) || 0.2));
  _tonePhase = 0;
  _toneTimer = setInterval(function() {
    var frame = new Float32Array(FRAME_SIZE);
    var step = 2 * Math.PI * _toneFreq / 48000;
    for (var i = 0; i < FRAME_SIZE; i++) {
      frame[i] = Math.sin(_tonePhase) * _toneLevel;
      _tonePhase += step;
      if (_tonePhase > 2 * Math.PI) _tonePhase -= 2 * Math.PI;
    }
    encodeAndPost(frame);
  }, 20);
}

function stopTone() {
  if (_toneTimer) {
    clearInterval(_toneTimer);
    _toneTimer = null;
  }
}

// ── Main-thread commands ───────────────────────────────

self.onmessage = function(ev) {
  var d = ev.data || {};
  if (d.type === 'sab') {
    // Receive SharedArrayBuffer ring from main thread
    sabInit(d.sab, d.ringSize || 16384);
    self.postMessage({ type: 'sab_ready' });
    // If we're already running (start arrived first), begin polling now
    if (running && _sab) startPolling();
  } else if (d.type === 'config') {
    if (typeof d.useOpus === 'boolean') _useOpus = d.useOpus;
  } else if (d.type === 'frame') {
    // Legacy postMessage path (SAB unavailable)
    if (!running || !d.frame) return;
    var frame = new Int16Array(d.frame);
    var f32 = new Float32Array(frame.length);
    for (var i = 0; i < frame.length; i++) f32[i] = frame[i] / 32768.0;
    encodeAndPost(f32);
  } else if (d.type === 'float_frame') {
    if (!running || !d.frame) return;
    encodeAndPost(new Float32Array(d.frame));
  } else if (d.type === 'start') {
    running = true;
    ensureEncoder();
    // Only start SAB polling if SAB is available; legacy path relies on 'frame' messages
    if (_sab) startPolling();
  } else if (d.type === 'stop') {
    running = false;
    stopPolling();
  } else if (d.type === 'tone_start') {
    startTone(d.freq, d.level);
  } else if (d.type === 'tone_stop') {
    stopTone();
  } else if (d.type === 'close') {
    running = false;
    stopPolling();
    stopTone();
  }
};
