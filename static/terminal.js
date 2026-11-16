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

const colormap = (() => {
    const map = [];
    for (let i = 0; i < 256; i++) {
        map.push([
            Math.min(255, i * 1.4) | 0,
            Math.min(255, Math.max(0, (i - 60) * 1.1)) | 0,
            Math.min(120, Math.max(0, (i - 180) * 0.6)) | 0,
        ]);
    }
    return map;
})();

function initCanvas() {
    wfCtx = el.waterfall.getContext('2d');
    fftCtx = el.fft.getContext('2d');
    wfCtx.fillStyle = '#000';
    wfCtx.fillRect(0, 0, WF_COLS, el.waterfall.height);
}

function drawSpectrum(wf1) {
    if (!wfCtx || !wf1 || wf1.length === 0) return;

    // FFT trace with a light EMA so a 11 fps scope stream does not flicker.
    if (!fftSmooth || fftSmooth.length !== wf1.length) {
        fftSmooth = new Float32Array(wf1.length);
    }
    for (let i = 0; i < wf1.length; i++) {
        fftSmooth[i] += 0.3 * (wf1[i] - fftSmooth[i]);
    }
    const h = el.fft.height;
    fftCtx.fillStyle = '#000';
    fftCtx.fillRect(0, 0, WF_COLS, h);
    fftCtx.beginPath();
    fftCtx.moveTo(0, h);
    for (let i = 0; i < fftSmooth.length; i++) {
        fftCtx.lineTo(i, h - (fftSmooth[i] / 255) * h);
    }
    fftCtx.lineTo(WF_COLS, h);
    fftCtx.closePath();
    fftCtx.fillStyle = 'rgba(245, 158, 11, 0.18)';
    fftCtx.fill();
    fftCtx.beginPath();
    for (let i = 0; i < fftSmooth.length; i++) {
        const y = h - (fftSmooth[i] / 255) * h;
        if (i === 0) fftCtx.moveTo(i, y);
        else fftCtx.lineTo(i, y);
    }
    fftCtx.strokeStyle = '#f59e0b';
    fftCtx.lineWidth = 1.5;
    fftCtx.stroke();

    // Waterfall: scroll one row down, paint the new one on top.
    wfCtx.drawImage(el.waterfall, 0, 1);
    const row = wfCtx.createImageData(WF_COLS, 1);
    for (let i = 0; i < WF_COLS; i++) {
        const c = colormap[wf1[i] & 0xff];
        row.data[i * 4] = c[0];
        row.data[i * 4 + 1] = c[1];
        row.data[i * 4 + 2] = c[2];
        row.data[i * 4 + 3] = 255;
    }
    wfCtx.putImageData(row, 0, 0);
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
        } catch {
            counts.dropTx += 1;
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
    const body = new Uint8Array(1 + 1 + packet.length);
    body[0] = CHAN_TX_AUDIO;
    body[1] = AUDIO_TAG_OPUS;
    body.set(packet, 2);
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
            const loginUrl = URL_BASE + '/login?next=' +
                encodeURIComponent(location.pathname);
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
