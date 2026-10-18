/* SDR 接收机页面逻辑。
 *
 * 与 /WSsdr 的契约（见 sdr_frames.py / server.ws_sdr）：
 *   文本帧：hello / state / channel_ack / error
 *   二进制帧：0x01 [seq u16][n u16][u8 bins]
 *             0x02 [chid u8][seq u16][level u8][len u16][opus]
 *
 * 页面自身只做三件事：画瀑布、把点选翻成一条 channel 命令、把 Opus 解出来推给 worklet。
 */
"use strict";

const MODES = [
  { id: 0, name: "AM", bw: 6000, step: 1000 },
  { id: 1, name: "USB", bw: 3000, step: 100 },
  { id: 2, name: "LSB", bw: 3000, step: 100 },
  { id: 3, name: "CW", bw: 500, step: 50 },
  { id: 4, name: "NFM", bw: 12500, step: 5000 },
  { id: 5, name: "WFM", bw: 200000, step: 50000 },
];

const BW_CHOICES = {
  0: [3000, 6000, 9000, 12000],
  1: [1800, 2400, 3000, 3600],
  2: [1800, 2400, 3000, 3600],
  3: [200, 500, 1000],
  4: [8000, 12500, 15000],
  5: [150000, 200000],
};

const PRESETS = [
  { label: "中波", hz: 900000, mode: 0, bw: 9000 },
  { label: "80m", hz: 3800000, mode: 2, bw: 3000 },
  { label: "40m", hz: 7100000, mode: 2, bw: 3000 },
  { label: "20m", hz: 14180000, mode: 1, bw: 3000 },
  { label: "15m", hz: 21200000, mode: 1, bw: 3000 },
  { label: "10m", hz: 28400000, mode: 1, bw: 3000 },
  { label: "6m", hz: 50300000, mode: 1, bw: 2400 },
  { label: "FM 广播", hz: 100000000, mode: 5, bw: 200000 },
  { label: "航空", hz: 121500000, mode: 0, bw: 12000 },
  { label: "2m", hz: 145500000, mode: 4, bw: 12500 },
];

const el = {
  ruler: document.getElementById("ruler"),
  wf: document.getElementById("wf"),
  center: document.getElementById("center"),
  cursor: document.getElementById("cursor"),
  offset: document.getElementById("offset"),
  edit: document.getElementById("edit"),
  presets: document.getElementById("presets"),
  modes: document.getElementById("modes"),
  bw: document.getElementById("bw"),
  squelch: document.getElementById("squelch"),
  volume: document.getElementById("volume"),
  cListeners: document.getElementById("cListeners"),
  cLock: document.getElementById("cLock"),
  cState: document.getElementById("cState"),
  levelBar: document.getElementById("levelBar"),
  banner: document.getElementById("banner"),
  toast: document.getElementById("toast"),
  cursorLabel: document.getElementById("cursorLabel"),
};

const S = {
  ws: null,
  connected: false,
  retry: 0,
  center: 7074000,
  span: 2400000,
  absFreq: 7074000,
  mode: 1,
  bw: 3000,
  listeners: 0,
  bins: 1024,
  hello: null,
  audio: null,        // {ctx, node, gain, decoder}
  audioBlocked: false,
};

// ── 工具 ────────────────────────────────────────────────────────────
function fmtHz(hz) {
  hz = Math.round(hz);
  if (hz >= 1e6) return (hz / 1e6).toFixed(4) + " MHz";
  return (hz / 1e3).toFixed(1) + " kHz";
}

function toast(text, ms = 1800) {
  el.toast.textContent = text;
  el.toast.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.toast.classList.remove("show"), ms);
}

function setBanner(text) {
  if (!text) { el.banner.classList.remove("show"); el.banner.textContent = ""; return; }
  el.banner.textContent = text;
  el.banner.classList.add("show");
}

function hzPerPx() {
  return S.span / Math.max(1, el.wf.clientWidth);
}

function offsetOf(px) {
  const w = el.wf.clientWidth || 1;
  return Math.round((px - w / 2) * hzPerPx());
}

function pxOfOffset(off) {
  const w = el.wf.clientWidth || 1;
  return w / 2 + off / hzPerPx();
}

// ── WebSocket ──────────────────────────────────────────────────────
function send(obj) {
  if (S.ws && S.ws.readyState === WebSocket.OPEN) {
    S.ws.send(JSON.stringify(obj));
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  // 令牌走 cookie（同源握手自动带），URL 里永远不放凭据（AD-024）
  const ws = new WebSocket(proto + "//" + location.host + "/WSsdr");
  ws.binaryType = "arraybuffer";
  S.ws = ws;

  ws.onopen = () => {
    S.connected = true;
    S.retry = 0;
    el.cState.textContent = "已连接";
    el.cState.className = "chip on";
    setBanner("");
    requestChannel();
  };
  ws.onclose = (ev) => {
    S.connected = false;
    el.cState.textContent = "重连中";
    el.cState.className = "chip warn";
    if (ev.code === 4001) { setBanner("会话已失效，请重新登录 /sdr"); return; }
    if (ev.code === 4003) { setBanner(ev.reason || "接收机不可用"); return; }
    S.retry = Math.min(S.retry + 1, 5);
    setTimeout(connect, Math.min(1000 * Math.pow(1.6, S.retry), 8000));
  };
  ws.onerror = () => {};
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      onText(msg);
    } else {
      onBinary(new DataView(ev.data));
    }
  };
}

function onText(msg) {
  const type = msg.event_type;
  if (type === "hello") {
    S.hello = msg.value || {};
    if (S.hello.sample_rate) S.span = S.hello.sample_rate;
    renderModes();
    renderPresets();
    return;
  }
  if (type === "state") {
    const v = msg.value || {};
    S.center = v.center_hz || S.center;
    if (v.span_hz) S.span = v.span_hz;
    if (typeof v.abs_freq === "number") S.absFreq = v.abs_freq;
    S.listeners = v.listeners || 0;
    el.cListeners.textContent = S.listeners + " 人";
    const hold = v.hold_remaining_s || 0;
    el.cLock.textContent = hold > 0 ? "锁定 " + Math.ceil(hold) + "s" : "可调谐";
    el.cLock.className = "chip" + (hold > 0 ? " warn" : "");
    if (v.available === false) setBanner("接收机不可用：" + (v.reason || "未知原因"));
    else setBanner("");
    renderReadout();
    drawRuler();
    drawOverlay();
    return;
  }
  if (type === "channel_ack") return;
  if (type === "error") {
    const v = msg.value || {};
    if (msg.code === "locked") {
      toast("有人在调谐，" + Math.ceil(v.retry_after || 0) + " 秒后可再试");
      S.absFreq = S.center + (S.absFreq - S.center); // 保持本地状态
      drawRuler(); drawOverlay();
    } else if (msg.code === "channel_full") {
      toast("解调通道已满，换一个有人听的频点");
    } else {
      toast(v.reason || "出错了");
    }
  }
}

function onBinary(dv) {
  if (dv.byteLength < 5) return;
  const tag = dv.getUint8(0);
  if (tag === 0x01) {
    const n = dv.getUint16(3, true);
    if (dv.byteLength < 5 + n) return;
    const bins = new Uint8Array(dv.buffer, dv.byteOffset + 5, n);
    S.bins = n;
    drawRow(bins);
    return;
  }
  if (tag === 0x02) {
    // 布局：0x02 | chid u8 | seq u16 | level u8 | len u16 | opus
    const chid = dv.getUint8(1);
    const level = dv.getUint8(4);
    const len = dv.getUint16(5, true);
    if (dv.byteLength < 7 + len) return;
    el.levelBar.style.width = Math.min(100, Math.round((level / 255) * 100 * 1.6)) + "%";
    const payload = new Uint8Array(dv.buffer, dv.byteOffset + 7, len);
    playOpus(payload);
  }
}

// ── 瀑布渲染 ────────────────────────────────────────────────────────
const LUT = (() => {
  const stops = [
    [0.0, 4, 6, 12], [0.22, 12, 26, 70], [0.45, 20, 96, 120],
    [0.68, 120, 150, 70], [0.84, 245, 166, 35], [1.0, 255, 246, 216],
  ];
  const lut = new Uint8Array(256 * 3);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let a = stops[0], b = stops[stops.length - 1];
    for (let k = 0; k < stops.length - 1; k++) {
      if (t >= stops[k][0] && t <= stops[k + 1][0]) { a = stops[k]; b = stops[k + 1]; break; }
    }
    const f = b[0] === a[0] ? 0 : (t - a[0]) / (b[0] - a[0]);
    lut[i * 3] = a[1] + (b[1] - a[1]) * f;
    lut[i * 3 + 1] = a[2] + (b[2] - a[2]) * f;
    lut[i * 3 + 2] = a[3] + (b[3] - a[3]) * f;
  }
  return lut;
})();

const rowCanvas = document.createElement("canvas");
const rowCtx = rowCanvas.getContext("2d");
const rowImage = rowCtx.createImageData(1024, 1);

function drawRow(bins) {
  const ctx = el.wf.getContext("2d");
  const w = el.wf.width, h = el.wf.height;
  if (!w || !h) return;
  // 自身下滚一行，再在上画新行（10 Hz，自 blit 很便宜）
  ctx.drawImage(el.wf, 0, 1);
  const n = bins.length;
  if (rowCanvas.width !== n) { rowCanvas.width = n; }
  const data = rowImage.data;
  for (let i = 0; i < n; i++) {
    const v = bins[i];
    data[i * 4] = LUT[v * 3];
    data[i * 4 + 1] = LUT[v * 3 + 1];
    data[i * 4 + 2] = LUT[v * 3 + 2];
    data[i * 4 + 3] = 255;
  }
  rowCtx.putImageData(rowImage, 0, 0, 0, 0, n, 1);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(rowCanvas, 0, 0, n, 1, 0, 0, w, 1);
  drawOverlay();
}

function drawRuler() {
  const c = el.ruler;
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (c.width !== w * dpr) { c.width = w * dpr; c.height = h * dpr; }
  const ctx = c.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#0b0c09";
  ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = "#8f8879";
  ctx.font = "10px ui-monospace, monospace";
  ctx.textBaseline = "top";
  const span = S.span;
  const left = S.center - span / 2;
  // 选一个"好看"的刻度间隔
  const target = 8;
  const raw = span / target;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const nice = [1, 2, 5, 10].map((m) => m * pow).find((v) => v >= raw) || pow * 10;
  const first = Math.ceil(left / nice) * nice;
  for (let f = first; f <= left + span; f += nice) {
    const x = ((f - left) / span) * w;
    ctx.fillStyle = "#2f2c24";
    ctx.fillRect(x, h - 6, 1, 6);
    ctx.fillStyle = "#8f8879";
    const label = f >= 1e6 ? (f / 1e6).toFixed(3) : (f / 1e3).toFixed(0);
    ctx.fillText(label + (f >= 1e6 ? "M" : "k"), Math.min(w - 34, Math.max(2, x + 3)), 4);
  }
}

function drawOverlay() {
  const c = el.wf;
  const ctx = c.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
    c.width = Math.round(w * dpr);
    c.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  // 中心线（虚线）与解调光标
  const cx = w / 2;
  const px = pxOfOffset(S.absFreq - S.center);
  ctx.save();
  ctx.setLineDash([3, 4]);
  ctx.strokeStyle = "rgba(245,166,35,.45)";
  ctx.beginPath(); ctx.moveTo(cx, 0); ctx.lineTo(cx, h); ctx.stroke();
  ctx.restore();
  ctx.strokeStyle = "#f5a623";
  ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
  const label = fmtHz(S.absFreq);
  el.cursorLabel.textContent = label;
  const lw = el.cursorLabel.offsetWidth || 90;
  el.cursorLabel.style.left = Math.max(lw / 2, Math.min(w - lw / 2, px)) + "px";
}

function renderReadout() {
  el.center.textContent = fmtHz(S.center);
  el.cursor.textContent = fmtHz(S.absFreq);
  const off = S.absFreq - S.center;
  el.offset.textContent = (off >= 0 ? "+" : "−") + fmtHz(Math.abs(off)).replace(" ", "");
  drawOverlay();
}

// ── 控件 ────────────────────────────────────────────────────────────
function renderModes() {
  el.modes.innerHTML = "";
  MODES.forEach((m) => {
    const b = document.createElement("button");
    b.textContent = m.name;
    b.className = m.id === S.mode ? "on" : "";
    b.onclick = () => {
      S.mode = m.id;
      S.bw = m.bw;
      renderModes();
      renderBw();
      requestChannel();
    };
    el.modes.appendChild(b);
  });
  renderBw();
}

function renderBw() {
  const choices = BW_CHOICES[S.mode] || [3000];
  el.bw.innerHTML = "";
  choices.forEach((v) => {
    const o = document.createElement("option");
    o.value = String(v);
    o.textContent = v >= 1000 ? (v / 1000) + " kHz" : v + " Hz";
    if (v === S.bw) o.selected = true;
    el.bw.appendChild(o);
  });
}

function renderPresets() {
  el.presets.innerHTML = "";
  PRESETS.forEach((p) => {
    const b = document.createElement("button");
    b.textContent = p.label;
    b.onclick = () => {
      S.mode = p.mode;
      S.bw = p.bw;
      S.absFreq = p.hz;
      renderModes();
      send({ event_type: "tune", value: p.hz });
      setTimeout(() => { S.absFreq = p.hz; requestChannel(); }, 120);
    };
    el.presets.appendChild(b);
  });
}

function requestChannel() {
  if (!S.connected) return;
  send({ event_type: "cursor", value: S.absFreq - S.center });
  send({ event_type: "channel", value: { mode: S.mode, bw_hz: S.bw } });
}

// 点选与拖动
let drag = null;
function onPointerDown(ev) {
  el.wf.setPointerCapture(ev.pointerId);
  drag = { x0: ev.clientX, x: ev.clientX, moved: false, center0: S.center };
}
function onPointerMove(ev) {
  if (!drag) return;
  const dx = ev.clientX - drag.x0;
  if (Math.abs(dx) > 6) {
    drag.moved = true;
    S.center = drag.center0 - dx * hzPerPx();     // 向右拖 = 频谱向右 = 中心降低
    renderReadout();
    drawRuler();
  }
}
function onPointerUp(ev) {
  if (!drag) return;
  const dx = ev.clientX - drag.x0;
  if (!drag.moved) {
    const rect = el.wf.getBoundingClientRect();
    S.absFreq = S.center + offsetOf(ev.clientX - rect.left);
    renderReadout();
    requestChannel();
  } else {
    S.center = Math.round(drag.center0 - dx * hzPerPx());
    send({ event_type: "tune", value: Math.round(S.center) });
  }
  drag = null;
}

function step(delta) {
  S.absFreq = Math.max(10000, S.absFreq + delta);
  renderReadout();
  requestChannel();
}

// ── 音频 ────────────────────────────────────────────────────────────
function playOpus(u8) {
  if (!S.audio) { queueAudio(u8); return; }
  try {
    const f32 = S.audio.decoder.decode_float(u8);
    S.audio.node.port.postMessage({ type: "push", payload: new Float32Array(f32) });
  } catch (e) { /* 丢一帧 */ }
}

let pendingAudio = [];
function queueAudio(u8) {
  if (pendingAudio.length < 40) pendingAudio.push(new Uint8Array(u8));
}

async function initAudio() {
  if (S.audio) {
    if (S.audio.ctx.state === "suspended") { try { await S.audio.ctx.resume(); } catch (e) {} }
    return;
  }
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)({
      latencyHint: "interactive", sampleRate: 48000,
    });
    await ctx.audioWorklet.addModule("rx_worklet_processor.js?v=2");
    const node = new AudioWorkletNode(ctx, "rx-player");
    node.port.postMessage({ type: "config", prebufferMs: 220, recoveryMs: 120, maxMs: 600 });
    const gain = ctx.createGain();
    gain.gain.value = parseInt(el.volume.value, 10) / 100;
    node.connect(gain);
    gain.connect(ctx.destination);
    const decoder = new OpusDecoder(48000, 1);
    S.audio = { ctx, node, gain, decoder };
    pendingAudio.forEach((p) => playOpus(p));
    pendingAudio = [];
  } catch (e) {
    S.audioBlocked = true;
    toast("浏览器不支持 AudioWorklet，音频不可用");
  }
}

// ── 事件绑定 ────────────────────────────────────────────────────────
el.wf.addEventListener("pointerdown", onPointerDown);
el.wf.addEventListener("pointermove", onPointerMove);
el.wf.addEventListener("pointerup", onPointerUp);
el.wf.addEventListener("pointercancel", () => { drag = null; });
el.wf.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const m = MODES.find((x) => x.id === S.mode) || MODES[0];
  step((ev.deltaY > 0 ? 1 : -1) * m.step);
}, { passive: false });

el.bw.addEventListener("change", () => {
  S.bw = parseInt(el.bw.value, 10);
  requestChannel();
});
el.squelch.addEventListener("input", () => {
  send({ event_type: "squelch", value: parseInt(el.squelch.value, 10) / 100 });
});
el.volume.addEventListener("input", () => {
  const v = parseInt(el.volume.value, 10) / 100;
  if (S.audio) S.audio.gain.gain.value = v;
  send({ event_type: "volume", value: v });
});

// 双击中心频率直接键入
el.center.addEventListener("dblclick", () => {
  el.edit.value = (S.center / 1e6).toFixed(4);
  el.edit.style.display = "inline-block";
  el.center.style.display = "none";
  el.edit.focus();
  el.edit.select();
});
function commitEdit() {
  const mhz = parseFloat(el.edit.value.replace(/[^0-9.]/g, ""));
  el.edit.style.display = "none";
  el.center.style.display = "";
  if (isFinite(mhz) && mhz > 0) {
    S.center = Math.round(mhz * 1e6);
    send({ event_type: "tune", value: S.center });
  }
}
el.edit.addEventListener("blur", commitEdit);
el.edit.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") commitEdit();
  if (ev.key === "Escape") { el.edit.style.display = "none"; el.center.style.display = ""; }
});

document.addEventListener("keydown", (ev) => {
  const m = MODES.find((x) => x.id === S.mode) || MODES[0];
  if (ev.key === "ArrowRight") { ev.preventDefault(); step(m.step); }
  if (ev.key === "ArrowLeft") { ev.preventDefault(); step(-m.step); }
});

window.addEventListener("resize", () => { drawRuler(); drawOverlay(); });
["pointerdown", "keydown", "touchstart"].forEach((e) =>
  document.addEventListener(e, () => { initAudio(); }, { once: true }));

renderModes();
renderPresets();
renderReadout();
drawRuler();
connect();
