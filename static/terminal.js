/* MRRC Modern — single-socket reference client (/terminal).
 *
 * One WebSocket carries control (text, the /WSradio protocol), RX audio,
 * TX audio and the spectrum.  Every binary frame starts with a channel byte:
 *
 *   0x00  RX audio   (same payload /WSaudioRX sends: codec tag + body)
 *   0x01  TX audio   (same payload /WSaudioTX accepts)
 *   0x02  spectrum   (same payload /WSspectrum sends: 0x01 + wf1 [+ wf2])
 *
 * Self-contained on purpose: it never loads the guarded SPA scripts (the two
 * tab-indented, lint-exempt bundles) and it is not part of the SPA.  Design:
 * docs/superpowers/specs/2026-11-17-ws-terminal-single-connection-design.md
 */

// ── Base path (served at /terminal or /<prefix>/terminal) ───────────

const URL_BASE = location.pathname.slice(0, location.pathname.lastIndexOf('/terminal'));

const CHAN_RX_AUDIO = 0x00;
const CHAN_TX_AUDIO = 0x01;
const CHAN_SPECTRUM = 0x02;

const AUDIO_TAG_PCM = 0x00;
const AUDIO_TAG_OPUS = 0x01;

const WF_COLS = 850;
//: 瀑布画布的内部行数。这决定「滚多快」——**不是帧率决定的**。
//:
//: 电台扫描帧约 5.3 帧/秒，一帧前进一行。屏幕上每秒移动的像素 =
//:   帧率 × (CSS 高度 / 内部行数)
//: 所以内部行数做得越大，同样的帧率看起来越慢：
//:   80 行（主界面 desktop 默认，本页也用 80）→ 一屏 15 s，看着在流
//:   320 行（本页上一版）                    → 一屏 60 s，看着像卡住
//: 主界面手机档是 45 行，一屏 8.5 s。想再快就减这个数，不要复制行 ——
//: 复制行会让每一帧占好几行像素，画面变粗、时间轴失真。
const WF_ROWS = 80;
const SPECTRUM_PROFILES = ['high', 'mid', 'low'];

function wsUrl(path) {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + window.location.host + URL_BASE + path;
}

function assetUrl(path) {
    return URL_BASE + path;
}

// ── Elements ───────────────────────────────────────────────────────

const el = {};
[
    'link-dot',
    'role-badge',
    'profile-badge',
    'online-badge',
    'freq',
    'mode',
    'mode-select',
    'step-minus',
    'step-plus',
    'meter-fill',
    'meter-label',
    'fft',
    'waterfall',
    'ptt',
    'vol',
    'status',
    'stats',
].forEach((id) => {
    el[id] = document.getElementById(id);
});

// ── State ──────────────────────────────────────────────────────────

// PTTManager reads this global for its release watchdog.
const radioState = {
    active_freq: 0,
    mode: '',
    tx_status: 0,
    s_meter: 0,
    s_unit: '',
};

let ws = null;
let wsRetry = 0;
let pingTimer = null;
let pingSentAt = null;
let rttMs = null;
// `high` is the legacy, always-delivered tier: on a scope-less model the
// S-meter fallback suppresses identical frames, and a lower tier can then
// wait for a change that never comes (reported as a follow-up defect).
let profile = 'high';

const counts = {
    rxFrames: 0,
    txFrames: 0,
    specFrames: 0,
    dropAudio: 0,
    dropTx: 0,
    dropUnknown: 0,
    bytes: 0,
};

let audioCtx = null;
let rxGain = null;
let rxNode = null;
let rxDecoder = null;
//: 播放侧抖动缓冲的水位（worklet 每 ~2 s 报一次）。它持续下降 = 进来的音频
//: 比播出去的少 —— 那才是「正常声音变卡顿」的直接证据，服务端看不到这一段。
let rxBufferMs = null;

let txStream = null;
let txSource = null;
let txNode = null;
let txEncoder = null;
let txActive = false;

let wfCtx = null;
let fftCtx = null;
let fftSmooth = null;

// ── Status line ────────────────────────────────────────────────────

let statusTimer = null;

function showStatus(text, isError) {
    el.status.textContent = text || '';
    el.status.style.color = isError ? 'var(--danger)' : 'var(--amber)';
    if (statusTimer) clearTimeout(statusTimer);
    if (!isError)
        statusTimer = setTimeout(() => {
            el.status.textContent = '';
        }, 6000);
}

function renderStats() {
    el.stats.textContent =
        'rtt ' +
        (rttMs === null ? '—' : rttMs + 'ms') +
        '  rx ' +
        counts.rxFrames +
        '  tx ' +
        counts.txFrames +
        '  spec ' +
        counts.specFrames +
        '  drop a/t/u ' +
        counts.dropAudio +
        '/' +
        counts.dropTx +
        '/' +
        counts.dropUnknown +
        '  buf ' +
        (rxBufferMs === null ? '—' : rxBufferMs + 'ms') +
        '  ' +
        (counts.bytes / 1024).toFixed(1) +
        ' KB';
}

// ── Outbound commands (globals PTTManager expects) ──────────────────

function sendMsg(obj) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(obj));
    return true;
}

function sendCommand(field, value) {
    return sendMsg({ type: 'set', field: field, value: value });
}

function handlePTTStart() {
    el.ptt.classList.add('live');
}

function handlePTTEnd() {
    el.ptt.classList.remove('live');
}

// ── Rendering ──────────────────────────────────────────────────────

function formatFreq(hz) {
    if (!hz) return '—';
    const n = Number(hz);
    return (n / 1e6).toFixed(5);
}

function renderState() {
    el.freq.textContent = formatFreq(radioState.active_freq);
    el.mode.textContent = radioState.mode || '—';
    if (el.mode_select_value !== radioState.mode) {
        el['mode-select'].value = radioState.mode || '';
        el.mode_select_value = radioState.mode;
    }
    const s = Number(radioState.s_meter) || 0;
    el['meter-fill'].style.width = Math.max(0, Math.min(100, (s / 255) * 100)) + '%';
    el['meter-label'].textContent = 'S ' + (radioState.s_unit || Math.round((s / 255) * 9));
    el['link-dot'].classList.toggle('on', ws && ws.readyState === WebSocket.OPEN);
    el['profile-badge'].textContent = 'spec: ' + profile;
    renderStats();
}

function renderModes(modes) {
    if (!Array.isArray(modes) || !modes.length) return;
    const current = el['mode-select'].value;
    el['mode-select'].replaceChildren();
    modes.forEach((name) => {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        el['mode-select'].appendChild(opt);
    });
    if (modes.includes(current)) el['mode-select'].value = current;
}

// ── Spectrum drawing ───────────────────────────────────────────────

//: 主界面的配色与窗口（WF_PALETTES.jet + scopeFloor/scopeCeil，默认 5..220）。
//: 这两样决定「看得见多少」：把 [5,220] 线性映射到 jet，噪底是亮蓝/青，
//: 载波走到黄/红；不套窗口、直接用 0..255 去索引一条暗琥珀色表，噪底落在
//: RGB(30,0,0) —— 屏幕上几乎全黑，同样的帧率也看不出在动（2026-11-17 现场：
//: 「差距不小」，两侧渲染同一份数据、同一帧率，差别只在配色与窗口）。
const SCOPE_FLOOR = 5;
const SCOPE_CEIL = 220;

function jet(v) {
    let r, g, b;
    if (v < 0.125) { const u = v / 0.125; r = 0; g = 0; b = Math.floor(128 + u * 127); }
    else if (v < 0.375) { const u = (v - 0.125) / 0.25; r = 0; g = Math.floor(u * 255); b = 255; }
    else if (v < 0.625) { const u = (v - 0.375) / 0.25; r = Math.floor(u * 255); g = 255; b = Math.floor(255 * (1 - u)); }
    else if (v < 0.875) { const u = (v - 0.625) / 0.25; r = 255; g = Math.floor(255 * (1 - u)); b = 0; }
    else { const u = (v - 0.875) / 0.125; r = Math.floor(255 * (1 - u * 0.5)); g = 0; b = 0; }
    return [r, g, b];
}

//: 取一个频点在 [floor, ceil] 窗口里的 0..1 位置，并线性插值到画布宽度。
function scopeValue(wf1, x, w) {
    const srcPos = (x / w) * wf1.length;
    const idx = Math.floor(srcPos);
    const frac = srcPos - idx;
    const v1 = wf1[idx] || 0;
    const v2 = (idx + 1 < wf1.length) ? wf1[idx + 1] : v1;
    const raw = v1 + (v2 - v1) * frac;
    const mapped = (raw - SCOPE_FLOOR) / Math.max(1, SCOPE_CEIL - SCOPE_FLOOR);
    return Math.max(0, Math.min(1, mapped));
}

function initCanvas() {
    if (el.waterfall.height !== WF_ROWS) {
        // 自愈：HTML 属性与常量必须一致，否则滚动速度会悄悄差几倍。
        el.waterfall.height = WF_ROWS;
    }
    wfCtx = el.waterfall.getContext('2d');
    fftCtx = el.fft.getContext('2d');
    wfCtx.fillStyle = '#000';
    wfCtx.fillRect(0, 0, WF_COLS, el.waterfall.height);
}

function drawSpectrum(wf1) {
    if (!wfCtx || !wf1 || wf1.length === 0) return;

    const w = WF_COLS;
    const dynRange = Math.max(1, SCOPE_CEIL - SCOPE_FLOOR);

    // ── FFT 曲线：与主界面同一套窗口 + 2× 幅度 + 青色描边 ──────────
    if (!fftSmooth || fftSmooth.length !== wf1.length) {
        fftSmooth = new Float32Array(wf1);
    } else {
        for (let i = 0; i < wf1.length; i++) {
            fftSmooth[i] = fftSmooth[i] * 0.7 + wf1[i] * 0.3;
        }
    }
    const h = el.fft.height;
    fftCtx.clearRect(0, 0, w, h);
    fftCtx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
    fftCtx.lineWidth = 0.5;
    for (let pct = 0.25; pct <= 0.75; pct += 0.25) {
        const gy = Math.round(h - pct * h) + 0.5;
        fftCtx.beginPath();
        fftCtx.moveTo(0, gy);
        fftCtx.lineTo(w, gy);
        fftCtx.stroke();
    }
    fftCtx.beginPath();
    for (let x = 0; x < w; x++) {
        const srcPos = (x / w) * fftSmooth.length;
        const idx = Math.floor(srcPos);
        const frac = srcPos - idx;
        const v1 = fftSmooth[idx] || 0;
        const v2 = (idx + 1 < fftSmooth.length) ? fftSmooth[idx + 1] : v1;
        const mapped = ((v1 + (v2 - v1) * frac) - SCOPE_FLOOR) / dynRange;
        const y = h - Math.max(0, Math.min(1, mapped * 2.0)) * h;
        if (x === 0) fftCtx.moveTo(x + 0.5, y);
        else fftCtx.lineTo(x + 0.5, y);
    }
    fftCtx.strokeStyle = '#06b6d4';
    fftCtx.lineWidth = 1.0;
    fftCtx.stroke();

    // ── 瀑布：整屏上移一行，新行画在**底部**（与主界面同方向）──────
    const wfH = el.waterfall.height;
    wfCtx.drawImage(el.waterfall, 0, 1, w, wfH - 1, 0, 0, w, wfH - 1);
    const row = wfCtx.createImageData(w, 1);
    for (let x = 0; x < w; x++) {
        const [r, g, b] = jet(scopeValue(wf1, x, w));
        row.data[x * 4] = r;
        row.data[x * 4 + 1] = g;
        row.data[x * 4 + 2] = b;
        row.data[x * 4 + 3] = 255;
    }
    wfCtx.putImageData(row, 0, wfH - 1);
}

// ── RX audio ───────────────────────────────────────────────────────

function decodeRx(body) {
    const bytes = new Uint8Array(body);
    if (bytes.length < 2) return null;
    const tag = bytes[0];
    const payload = body.slice(1);

    if (tag === AUDIO_TAG_OPUS) {
        if (!rxDecoder) {
            if (typeof OpusDecoder === 'undefined') return null;
            rxDecoder = new OpusDecoder(48000, 1);
        }
        try {
            return new Float32Array(rxDecoder.decode_float(payload));
        } catch {
            return null;
        }
    }

    // Anything that is neither PCM nor Opus is a protocol mismatch — say so
    // instead of handing the bytes to the PCM path.
    if (tag !== AUDIO_TAG_PCM) return null;
    if (payload.byteLength < 2) return null;
    const i16 = new Int16Array(payload);
    const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32767;
    return f32;
}

async function ensureAudio() {
    if (audioCtx) {
        if (audioCtx.state === 'suspended') {
            try {
                await audioCtx.resume();
            } catch {
                /* noop */
            }
        }
        return;
    }
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({
        latencyHint: 'interactive',
        sampleRate: 48000,
    });
    rxGain = audioCtx.createGain();
    rxGain.gain.value = 0.7;
    rxGain.connect(audioCtx.destination);

    if (!audioCtx.audioWorklet) {
        showStatus('浏览器不支持 AudioWorklet，音频不可用', true);
        return;
    }
    await audioCtx.audioWorklet.addModule(assetUrl('/rx_worklet_processor.js?v=2'));
    rxNode = new AudioWorkletNode(audioCtx, 'rx-player');
    rxNode.port.postMessage({
        type: 'config',
        prebufferMs: 400,
        recoveryMs: 250,
        maxMs: 1500,
    });
    rxNode.port.onmessage = (ev) => {
        if (ev.data && ev.data.type === 'stats') {
            rxBufferMs = Math.round(ev.data.bufferMs || 0);
            renderStats();
        }
    };
    rxNode.connect(rxGain);
    showStatus('音频已启动');
}

function playRx(body) {
    const f32 = decodeRx(body);
    if (!f32) {
        counts.dropAudio += 1;
        return;
    }
    counts.rxFrames += 1;
    if (rxNode) rxNode.port.postMessage({ type: 'push', payload: f32 });
}

// ── TX audio ───────────────────────────────────────────────────────

async function beginTx() {
    if (txActive) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        showStatus('未连接，无法发射', true);
        return;
    }
    await ensureAudio();
    try {
        txStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                channelCount: 1,
                echoCancellation: false,
                noiseSuppression: false,
                autoGainControl: false,
            },
        });
    } catch (e) {
        showStatus('麦克风不可用：' + e.message, true);
        return;
    }

    if (typeof OpusEncoder === 'undefined') {
        showStatus('Opus 编码器未加载', true);
        return;
    }
    if (!txEncoder) txEncoder = new OpusEncoder(48000, 1, 2048, 20);

    await audioCtx.audioWorklet.addModule(assetUrl('/tx_capture_worklet.js?v=1'));
    txSource = audioCtx.createMediaStreamSource(txStream);
    txNode = new AudioWorkletNode(audioCtx, 'tx-capture');
    txNode.port.onmessage = (ev) => {
        if (!txActive || !ev.data || ev.data.type !== 'frame') return;
        try {
            const float = new Float32Array(ev.data.frame);
            const packets = txEncoder.encode_float(float);
            for (let i = 0; i < packets.length; i++) sendTxPacket(packets[i]);
        } catch (e) {
            counts.dropTx += 1;
            // 一次都不该静默：上一版把每帧的 RangeError 吞成计数，现场只看到
            // 「按键生效、没有音频」，没有任何线索。
            if (counts.dropTx === 1) {
                showStatus('麦克风编码/发送失败：' + (e && e.message ? e.message : e), true);
            }
        }
    };
    txSource.connect(txNode);

    txActive = true;
    // Key the radio first: the uplink owner is this socket already, and a
    // keyed radio with a live mic stream is the normal order for a device.
    sendCommand('ptt', true);
    if (window.PTTManager) window.PTTManager.pttStart();
    handlePTTStart();
}

function sendTxPacket(packet) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    // `OpusEncoder.encode_float()` 返回的是 **ArrayBuffer**（没有 .length）——
    // 当成 typed array 用会得到 0 长度的目标缓冲，set() 抛 RangeError，而
    // catch 里只加一个丢弃计数：症状就是「PTT 键上了，一个字节都没发出去」。
    const opus = packet instanceof ArrayBuffer ? new Uint8Array(packet)
        : new Uint8Array(packet.buffer || packet);
    if (opus.length === 0) return;
    const body = new Uint8Array(1 + 1 + opus.length);
    body[0] = CHAN_TX_AUDIO;
    body[1] = AUDIO_TAG_OPUS;
    body.set(opus, 2);
    ws.send(body);
    counts.txFrames += 1;
}

function endTx() {
    if (!txActive) return;
    txActive = false;
    try {
        if (txNode) txNode.disconnect();
    } catch {
        /* noop */
    }
    try {
        if (txSource) txSource.disconnect();
    } catch {
        /* noop */
    }
    if (txStream) {
        txStream.getTracks().forEach((t) => {
            t.stop();
        });
        txStream = null;
    }
    txNode = null;
    txSource = null;
    sendCommand('ptt', false);
    if (window.PTTManager) window.PTTManager.pttEnd();
    handlePTTEnd();
    renderStats();
}

// ── Text frames ────────────────────────────────────────────────────

function onText(raw) {
    let msg;
    try {
        msg = JSON.parse(raw);
    } catch {
        return;
    }

    if (msg.type === 'pong') {
        if (pingSentAt !== null) {
            rttMs = Math.round(performance.now() - pingSentAt);
            pingSentAt = null;
        }
    } else if (msg.type === 'fullState') {
        Object.assign(radioState, msg.data || {});
        renderModes(msg.modes);
        renderState();
        // One declaration subscribes this socket to 0x02 frames.
        sendMsg({ type: 'spectrumCaps', profile: profile });
    } else if (msg.type === 'stateUpdate') {
        Object.assign(radioState, msg.fields || {});
        renderState();
    } else if (msg.type === 'value') {
        radioState[msg.field] = msg.value;
        renderState();
    } else if (msg.type === 'onlineUsers') {
        el['online-badge'].textContent = '在线 ' + msg.count;
    } else if (msg.type === 'error') {
        showStatus(msg.message || '命令被拒绝', true);
    } else if (msg.type === 'ptt_keyed_by_other') {
        showStatus('另一个客户端正在发射，PTT 已让出', true);
        endTx();
        if (window.PTTManager) window.PTTManager.cancelWatchdog();
    }
}

// ── The single socket ──────────────────────────────────────────────

function connect() {
    ws = new WebSocket(wsUrl('/WSterminal'));
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
        wsRetry = 0;
        el['link-dot'].classList.add('on');
        showStatus('已连接 /WSterminal');
        if (pingTimer) clearInterval(pingTimer);
        pingTimer = setInterval(() => {
            pingSentAt = performance.now();
            sendMsg({ type: 'ping' });
        }, 15000);
        renderState();
    };

    ws.onmessage = (ev) => {
        if (typeof ev.data === 'string') {
            onText(ev.data);
            return;
        }
        const buf = new Uint8Array(ev.data);
        if (buf.length < 2) return;
        counts.bytes += buf.length;
        const chan = buf[0];
        const body = ev.data.slice(1);

        if (chan === CHAN_RX_AUDIO) playRx(body);
        else if (chan === CHAN_SPECTRUM) {
            counts.specFrames += 1;
            drawSpectrum(new Uint8Array(body).subarray(1));
        } else counts.dropUnknown += 1;
    };

    ws.onclose = (ev) => {
        el['link-dot'].classList.remove('on');
        if (pingTimer) {
            clearInterval(pingTimer);
            pingTimer = null;
        }
        if (txActive) endTx();
        if (ev.code === 4001) {
            // Same-origin by construction: URL_BASE is a slice of this page's
            // own pathname, and the guard below keeps it that way.
            const loginUrl = URL_BASE + '/login?next=' + encodeURIComponent(location.pathname);
            if (loginUrl.startsWith('/')) window.location.replace(loginUrl);
            return;
        }
        wsRetry = Math.min(wsRetry + 1, 6);
        showStatus('连接断开，' + Math.round((500 * 2 ** wsRetry) / 1000) + ' s 后重连', true);
        setTimeout(connect, 500 * 2 ** wsRetry);
    };

    ws.onerror = () => {
        try {
            ws.close();
        } catch {
            /* noop */
        }
    };
}

// ── Controls ───────────────────────────────────────────────────────

function tuneBy(hz) {
    if (!radioState.active_freq) return;
    sendCommand('freq', Number(radioState.active_freq) + hz);
}

el['step-plus'].addEventListener('click', () => tuneBy(1000));
el['step-minus'].addEventListener('click', () => tuneBy(-1000));
el['mode-select'].addEventListener('change', (ev) => {
    sendCommand('mode', ev.target.value);
});

el['ptt'].addEventListener('pointerdown', async (ev) => {
    ev.preventDefault();
    el.ptt.setPointerCapture?.(ev.pointerId);
    await beginTx();
});
el['ptt'].addEventListener('pointerup', (ev) => {
    ev.preventDefault();
    endTx();
});
el['ptt'].addEventListener('pointercancel', () => endTx());
el['ptt'].addEventListener('contextmenu', (ev) => ev.preventDefault());

el['profile-badge'].addEventListener('click', () => {
    const next =
        SPECTRUM_PROFILES[(SPECTRUM_PROFILES.indexOf(profile) + 1) % SPECTRUM_PROFILES.length];
    profile = next;
    sendMsg({ type: 'spectrumCaps', profile: profile });
    renderStats();
    renderState();
});

el.vol.addEventListener('input', () => {
    if (rxGain) rxGain.gain.value = (Number(el.vol.value) / 255) * 1.2;
});

window.addEventListener('pagehide', () => {
    if (txActive) endTx();
});

// ── Boot ───────────────────────────────────────────────────────────

initCanvas();
renderState();
connect();
showStatus('点击页面启用音频，按住 PTT 发射');
document.addEventListener(
    'pointerdown',
    function once() {
        document.removeEventListener('pointerdown', once);
        ensureAudio().catch(() => {
            /* noop */
        });
    },
    { once: true }
);
