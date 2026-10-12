// Waterfall canvas renderer for compact WF01 binary frames (§10.2).

import { subscribe } from "./state.js";

const HEADER_BYTES = 22; // "<4sIQfH": magic, seq, epoch_ms, bin_hz, count

export function parseFrame(buffer) {
  const view = new DataView(buffer);
  if (buffer.byteLength < HEADER_BYTES) return null;
  const magic = String.fromCharCode(
    view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3),
  );
  if (magic !== "WF01") return null;
  const count = view.getUint16(20, true);
  if (buffer.byteLength !== HEADER_BYTES + count) return null;
  return {
    seq: view.getUint32(4, true),
    epochMs: Number(view.getBigUint64(8, true)),
    binHz: view.getFloat32(16, true),
    bins: new Uint8Array(buffer, HEADER_BYTES, count),
  };
}

function colorFor(value) {
  // 0..255 → dark blue → cyan → yellow → white
  const t = value / 255;
  const r = Math.min(255, Math.round(t * t * 2 * 255));
  const g = Math.min(255, Math.round(t * 1.5 * 255));
  const b = Math.min(255, Math.round(40 + t * 215));
  return [r, g, b];
}

export function createWaterfall(canvas) {
  const ctx = canvas.getContext("2d");
  let lastSeq = null;

  // A dead waterfall stream leaves the canvas frozen on the last frames,
  // which looks exactly like a live band (2026-08-02 field confusion).
  // Dim it so stale pixels are obviously not live.
  subscribe(({ connected }) => {
    canvas.classList.toggle("offline", !(connected && connected.waterfall));
  });

  function resize() {
    const rect = canvas.getBoundingClientRect();
    // Compare against floored sizes: rect dimensions are fractional CSS
    // pixels, so comparing raw would mismatch every frame, and assigning
    // canvas.width/height clears the bitmap — erasing all waterfall history.
    const w = Math.max(1, Math.floor(rect.width));
    const h = Math.max(1, Math.floor(rect.height));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
      lastSeq = null;
    }
  }

  function push(frame) {
    resize();
    const { width, height } = canvas;
    // scroll existing content down one line, draw the newest line at the top
    ctx.drawImage(canvas, 0, 0, width, height - 1, 0, 1, width, height - 1);
    const image = ctx.createImageData(width, 1);
    const count = frame.bins.length;
    for (let x = 0; x < width; x += 1) {
      const bin = frame.bins[Math.floor((x / width) * count)];
      const [r, g, b] = colorFor(bin);
      image.data[x * 4] = r;
      image.data[x * 4 + 1] = g;
      image.data[x * 4 + 2] = b;
      image.data[x * 4 + 3] = 255;
    }
    ctx.putImageData(image, 0, 0);
    lastSeq = frame.seq;
  }

  window.addEventListener("resize", resize);
  return { push, lastSeq: () => lastSeq };
}
