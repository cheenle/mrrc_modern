/* CQ recording in the browser (spec 2026-10-26-cq-recording-design.md).
 *
 * Why the browser records instead of uploading a file: the server ships an
 * encoder (lameenc) and no decoder, so anything the operator hands us must
 * already be 16-bit PCM WAV. `MediaRecorder` produces webm/opus, which would be
 * a dead end — so this module captures raw PCM through an AudioWorklet and
 * writes the RIFF header itself.
 *
 * A plain script with a global, exactly like box_api.js and listen.js: the page
 * is served straight off disk by the same FileResponse route, with no build step.
 */
(function (global) {
  'use strict';

  var TARGET_RATE = 48000;
  var WORKLET_URL = null;

  /* Accumulate inside the worklet: one message per 128-sample render quantum
   * would be 375 messages/second on the main thread for no benefit. */
  var WORKLET_SRC = [
    'class CqCapture extends AudioWorkletProcessor {',
    '  constructor() {',
    '    super();',
    '    this._buf = new Float32Array(4096);',
    '    this._n = 0;',
    '    this.port.onmessage = () => this.flush();',
    '  }',
    '  process(inputs) {',
    '    var input = inputs[0] && inputs[0][0];',
    '    if (!input) return true;',
    '    for (var i = 0; i < input.length; i++) {',
    '      this._buf[this._n++] = input[i];',
    '      if (this._n === this._buf.length) {',
    '        this.port.postMessage(this._buf.slice(0));',
    '        this._n = 0;',
    '      }',
    '    }',
    '    return true;',
    '  }',
    '  flush() {',
    '    if (this._n) { this.port.postMessage(this._buf.slice(0, this._n)); this._n = 0; }',
    '  }',
    '}',
    'registerProcessor("cq-capture", CqCapture);'
  ].join('\n');

  var session = null;

  function writeAscii(view, offset, text) {
    for (var i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  }

  /* 16-bit PCM mono RIFF/WAVE. `peak` is the pre-encoding peak so the page can
   * warn about a take that will only be audible after normalisation. */
  function encodeWav(chunks, sampleRate) {
    var total = 0;
    for (var c = 0; c < chunks.length; c++) total += chunks[c].length;
    var buffer = new ArrayBuffer(44 + total * 2);
    var view = new DataView(buffer);
    writeAscii(view, 0, 'RIFF');
    view.setUint32(4, 36 + total * 2, true);
    writeAscii(view, 8, 'WAVE');
    writeAscii(view, 12, 'fmt ');
    view.setUint32(16, 16, true);          // PCM chunk size
    view.setUint16(20, 1, true);           // format: PCM
    view.setUint16(22, 1, true);           // mono
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);   // byte rate
    view.setUint16(32, 2, true);           // block align
    view.setUint16(34, 16, true);          // bits per sample
    writeAscii(view, 36, 'data');
    view.setUint32(40, total * 2, true);
    var offset = 44;
    var peak = 0;
    for (var k = 0; k < chunks.length; k++) {
      var chunk = chunks[k];
      for (var i = 0; i < chunk.length; i++) {
        var s = Math.max(-1, Math.min(1, chunk[i]));
        var abs = Math.abs(s);
        if (abs > peak) peak = abs;
        view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
        offset += 2;
      }
    }
    return {
      blob: new Blob([view], { type: 'audio/wav' }),
      peak: peak,
      seconds: sampleRate ? total / sampleRate : 0
    };
  }

  async function start() {
    if (session) throw new Error('已经在录了');
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error('这个浏览器不支持录音（需要 HTTPS 与麦克风权限）');
    }
    var stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        // Echo cancellation is on purpose: the radio's own audio is often
        // playing in the same room, and a CQ that repeats the other station is
        // worse than a quiet one. AGC stays off — the server normalises peaks,
        // so automatic gain here would only fight it.
        echoCancellation: true,
        autoGainControl: false,
        noiseSuppression: false
      }
    });
    var ctx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: TARGET_RATE
    });
    var chunks = [];
    try {
      if (!WORKLET_URL) {
        WORKLET_URL = URL.createObjectURL(
          new Blob([WORKLET_SRC], { type: 'application/javascript' }));
      }
      await ctx.audioWorklet.addModule(WORKLET_URL);
      var source = ctx.createMediaStreamSource(stream);
      var node = new AudioWorkletNode(ctx, 'cq-capture');
      node.port.onmessage = function (event) { chunks.push(event.data); };
      // A silent sink: the worklet must be pulled by the graph, and nobody
      // should hear their own microphone while recording.
      var mute = ctx.createGain();
      mute.gain.value = 0;
      source.connect(node);
      node.connect(mute);
      mute.connect(ctx.destination);
      session = {
        ctx: ctx,
        stream: stream,
        source: source,
        node: node,
        mute: mute,
        chunks: chunks,
        started: performance.now()
      };
    } catch (error) {
      stream.getTracks().forEach(function (t) { t.stop(); });
      await ctx.close().catch(function () {});
      throw error;
    }
    return { sampleRate: ctx.sampleRate };
  }

  function elapsedSeconds() {
    if (!session) return 0;
    return (performance.now() - session.started) / 1000;
  }

  function isRecording() {
    return !!session;
  }

  async function stop() {
    if (!session) throw new Error('没有在录');
    var current = session;
    session = null;
    try {
      current.node.port.postMessage({ flush: true });
    } catch (ignored) { /* worklet already gone */ }
    // Let the last frames arrive before tearing the graph down.
    await new Promise(function (resolve) { setTimeout(resolve, 80); });
    try {
      current.source.disconnect();
      current.node.disconnect();
      current.mute.disconnect();
    } catch (ignored) { /* already disconnected */ }
    current.stream.getTracks().forEach(function (t) { t.stop(); });
    var rate = current.ctx.sampleRate;
    await current.ctx.close().catch(function () {});
    return encodeWav(current.chunks, rate);
  }

  global.CQRecorder = {
    start: start,
    stop: stop,
    isRecording: isRecording,
    elapsedSeconds: elapsedSeconds,
    encodeWav: encodeWav,
    TARGET_RATE: TARGET_RATE
  };
})(window);
