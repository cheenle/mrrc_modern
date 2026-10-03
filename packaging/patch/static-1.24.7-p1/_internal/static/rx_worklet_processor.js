// AudioWorklet RX player for raw Float32 frames pushed by controls.js.
//
// Jitter buffer is TIME-based (milliseconds), not frame-count based. PCM frames
// are variable-length and the stream runs at 48 kHz, so a frame-count threshold
// (the old min:2/max:30) meant wildly different buffer DURATIONS depending on
// codec and rate — at 48 kHz "min:2" was only ~26 ms, far too shallow for a
// remote link, so any network jitter > a couple frames dropped the queue below
// the floor and inserted silence = the audible stutter.
//
// Now we track total queued SAMPLES and convert ms watermarks via the global
// `sampleRate`. Two watermarks with HYSTERESIS:
//   prebufferMs — initial cold-start cushion: hold silent until first filled.
//   recoveryMs  — re-arm cushion after an underrun: a single starved render
//                 quantum only needs to refill to THIS (much smaller) level
//                 before resuming, NOT the full prebuffer.
// Why this matters for Opus: process() runs every 128 samples (~2.67 ms), but
// Opus frames arrive in 20 ms bursts (0,1,1,0,1… per source chunk because the
// 960-sample encoder frame never aligns to the ~629-sample resampled chunk).
// A burst gap easily starves one quantum; the OLD code then re-primed the FULL
// 220 ms every time = a long stall = the "Opus stutter". With a small recovery
// watermark the resume gap is ~recoveryMs, inaudible. WFM is one-way so the
// steady-state latency (~prebufferMs) is harmless.

class RxPlayerProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.queuedSamples = 0;
    this.underruns = 0;

    // Time-based watermarks (defaults tuned for a remote link). Overridable via
    // a 'config' message. sampleRate is the AudioWorklet global (48000 here).
    this.prebufferMs = 220;   // cold-start cushion
    this.recoveryMs = 90;     // re-arm cushion after an underrun (hysteresis)
    this.maxMs = 800;         // hard cap; drop oldest beyond this to bound latency
    this.priming = true;      // gate closed, accumulating
    this.gateMs = this.prebufferMs;  // active gate threshold (prebuffer vs recovery)

    this._statsCounter = 0;
    this._statsInterval = Math.round(sampleRate * 2 / 128); // report every ~2s

    this.port.onmessage = (event) => {
      const data = event.data;
      if (data && data.type === 'push' && data.payload instanceof Float32Array) {
        this.queue.push(data.payload);
        this.queuedSamples += data.payload.length;
        const maxSamples = (this.maxMs / 1000) * sampleRate;
        while (this.queuedSamples > maxSamples && this.queue.length > 1) {
          this.queuedSamples -= this.queue.shift().length;
        }
      } else if (data && (data.type === 'flush' || data.type === 'reset')) {
        this.queue.length = 0;
        this.queuedSamples = 0;
        this.underruns = 0;
        this.priming = true;
        this.gateMs = this.prebufferMs;  // cold restart uses full cushion
      } else if (data && data.type === 'config') {
        if (typeof data.prebufferMs === 'number') {
          this.prebufferMs = Math.max(20, data.prebufferMs);
        }
        if (typeof data.recoveryMs === 'number') {
          this.recoveryMs = Math.max(10, data.recoveryMs);
        }
        if (typeof data.maxMs === 'number') {
          this.maxMs = Math.max(this.prebufferMs + 40, data.maxMs);
        }
        // Back-compat: accept legacy frame-count {min,max} by treating each
        // frame as ~13 ms (512-sample chunk @ 39 kHz → ~629 @ 48 kHz).
        if (typeof data.min === 'number') {
          this.prebufferMs = Math.max(20, data.min * 13);
        }
        if (typeof data.max === 'number') {
          this.maxMs = Math.max(this.prebufferMs + 40, data.max * 13);
        }
        // Keep recovery below prebuffer, and the active gate consistent if we
        // haven't started yet.
        this.recoveryMs = Math.min(this.recoveryMs, this.prebufferMs);
        if (this.priming) this.gateMs = this.prebufferMs;
      }
    };
  }

  process(inputs, outputs) {
    const output = outputs[0];
    const out = output[0];
    if (!out) return true;

    // Gate: stay silent until the queue fills to the active watermark (full
    // prebuffer on cold start, smaller recovery cushion after an underrun).
    if (this.priming) {
      const gateSamples = (this.gateMs / 1000) * sampleRate;
      if (this.queuedSamples < gateSamples) {
        out.fill(0);
        return true;
      }
      this.priming = false;  // enough buffered → resume draining
    }

    let written = 0;
    while (written < out.length && this.queue.length > 0) {
      const cur = this.queue[0];
      const n = Math.min(cur.length, out.length - written);
      out.set(cur.subarray(0, n), written);
      written += n;
      this.queuedSamples -= n;
      if (n >= cur.length) {
        this.queue.shift();
      } else {
        this.queue[0] = cur.subarray(n);
      }
    }

    if (written < out.length) {
      // Underran mid-block: fill the rest with silence and re-arm the gate at
      // the SMALL recovery watermark (not the full prebuffer) so resume is fast
      // and a transient burst gap doesn't cause a long stall.
      out.fill(0, written);
      this.underruns++;
      this.priming = true;
      this.gateMs = this.recoveryMs;
    }

    this._statsCounter++;
    if (this._statsCounter >= this._statsInterval) {
      this._statsCounter = 0;
      var bufferMs = (this.queuedSamples / sampleRate) * 1000;
      this.port.postMessage({type: 'stats', bufferMs: Math.round(bufferMs)});
    }

    return true;
  }
}

registerProcessor('rx-player', RxPlayerProcessor);
