// AudioWorklet TX capture → 48 kHz float32 frames for the Opus worker.
// Active path: port.postMessage('frame') to the main thread, which forwards
// them to the Opus worker as 'float_frame'. The SharedArrayBuffer ring below
// (_writeRing) is dormant — the main thread never allocates the SAB.
//// Resamples the browser's actual capture rate to 48 kHz float32 and posts
// exact 20 ms frames to the main thread for Opus worker encoding.
//
// Ring buffer layout (same as modules/tx_sab_ring.js):
//   word[0] = write_pos (Uint32, producer advances atomically)
//   word[1] = read_pos  (Uint32, consumer advances atomically)
//   word[2+] = float32 sample data (power-of-2 size)

class TxCaptureSABProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super(options);
    this._inRate = sampleRate;                    // actual AudioContext rate
    this._outRate = 48000;
    this._resampleStep = this._inRate / this._outRate;
    this._blockSize = Math.round(this._outRate * 0.010);  // 480 samples @ 48k
    this._inBuf = new Float32Array(0);
    this._outBuf = new Float32Array(0);
    this._srcPos = 0;

    // SAB state (set via port message)
    this._sab = null;
    this._ringMask = 0;
    this._writePtr = null;
    this._readPtr = null;
    this._data = null;
    this._enabled = false;

    // Frame accumulator for legacy postMessage path (used when SAB unavailable)
    this._frameAcc = new Float32Array(0);
    this._frameSize = Math.round(this._outRate * 0.020);  // 960 samples @ 48kHz
    this._scale = 0x7FFF;

    this.port.onmessage = (ev) => {
      var d = ev.data || {};
      if (d.type === 'sab') {
        this._sab = d.sab;
        this._ringMask = (d.ringSize || 16384) - 1;
        this._writePtr = new Uint32Array(this._sab, 0, 1);
        this._readPtr  = new Uint32Array(this._sab, 4, 1);
        this._data     = new Float32Array(this._sab, 8, this._ringMask + 1);
        this._enabled  = true;
      } else if (d.type === 'flush') {
        this._inBuf = new Float32Array(0);
        this._outBuf = new Float32Array(0);
        this._frameAcc = new Float32Array(0);
        this._srcPos = 0;
        if (this._writePtr) Atomics.store(this._writePtr, 0, 0);
        if (this._readPtr)  Atomics.store(this._readPtr, 0, 0);
      }
    };
  }

  // ── SAB ring write (lock-free SPSC) ──────────────────

  _writeRing(samples, n) {
    if (!this._enabled || n === 0) return;
    var wp = Atomics.load(this._writePtr);
    var rp = Atomics.load(this._readPtr);
    var mask = this._ringMask;
    var size = mask + 1;
    var used = wp - rp;
    var free = size - used;
    if (n > free) {
      // Drop oldest samples (buffer is ~1s deep — this should be rare)
      var drop = n - free;
      Atomics.store(this._readPtr, 0, rp + drop);
    }
    var idx = wp & mask;
    var first = Math.min(n, size - idx);
    var data = this._data;
    for (var i = 0; i < first; i++) data[idx + i] = samples[i];
    if (n > first) {
      for (var i = 0; i < n - first; i++) data[i] = samples[first + i];
    }
    Atomics.store(this._writePtr, 0, wp + n);
  }

  _resampleTo48k(input) {
    if (Math.abs(this._resampleStep - 1) < 0.000001) {
      return input;
    }

    var merged = new Float32Array(this._inBuf.length + input.length);
    merged.set(this._inBuf);
    merged.set(input, this._inBuf.length);
    this._inBuf = merged;

    var output = [];
    while (Math.floor(this._srcPos) + 1 < this._inBuf.length) {
      var i = Math.floor(this._srcPos);
      var frac = this._srcPos - i;
      output.push(this._inBuf[i] + (this._inBuf[i + 1] - this._inBuf[i]) * frac);
      this._srcPos += this._resampleStep;
    }

    var consumed = Math.floor(this._srcPos);
    if (consumed > 0) {
      this._inBuf = this._inBuf.subarray(consumed);
      this._srcPos -= consumed;
    }

    return new Float32Array(output);
  }

  _appendOutput(samples) {
    if (!samples || samples.length === 0) return;
    var merged = new Float32Array(this._outBuf.length + samples.length);
    merged.set(this._outBuf);
    merged.set(samples, this._outBuf.length);
    this._outBuf = merged;
  }

  // ── Audio processing ─────────────────────────────────

  process(inputs, outputs) {
    var input = inputs[0];
    if (!input || !input[0] || input[0].length === 0) {
      return true;
    }
    var ch = input[0];

    this._appendOutput(this._resampleTo48k(ch));

    // Feed fullband 48 kHz microphone audio in fixed 10 ms chunks.
    while (this._outBuf.length >= this._blockSize) {
      var block = this._outBuf.subarray(0, this._blockSize);
      this._outBuf = this._outBuf.subarray(this._blockSize);

      var accMerged = new Float32Array(this._frameAcc.length + block.length);
      accMerged.set(this._frameAcc);
      accMerged.set(block, this._frameAcc.length);
      this._frameAcc = accMerged;
      while (this._frameAcc.length >= this._frameSize) {
        var frame = new Float32Array(this._frameSize);
        frame.set(this._frameAcc.subarray(0, this._frameSize));
        this._frameAcc = this._frameAcc.subarray(this._frameSize);
        this.port.postMessage({type: 'frame', frame: frame.buffer}, [frame.buffer]);
      }
    }

    return true;
  }
}

registerProcessor('tx-capture', TxCaptureSABProcessor);
