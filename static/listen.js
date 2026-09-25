/* MRRC Modern — listen-only interface (/listen).
 *
 * Self-contained: does NOT load ft710_main.js / ft710_ui.js. The server
 * enforces the role (only freq/mode set commands, no TX uplink, read-only
 * REST); this page simply never offers the forbidden controls.
 */
'use strict';

// ── Auth helpers ────────────────────────────────────────────────────

// URL base for every server endpoint. Direct access serves this page at
// /listen (base ''); the public reverse proxy serves it at
// /mrrc_modern/listen (base '/mrrc_modern'). All URLs are built from the
// page's own location so both work without configuration.
const URL_BASE = location.pathname.slice(
    0, location.pathname.lastIndexOf('/listen'));

function getAuthToken() {
    const m = document.cookie.match(/(?:^|;\s*)mrrc_auth=([^;]*)/);
    return m ? m[1] : '';
}

function wsUrlWithAuth(path) {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return proto + '//' + window.location.host + URL_BASE + path +
        '?token=' + encodeURIComponent(getAuthToken());
}

function staticUrlWithAuth(path) {
    const sep = path.indexOf('?') >= 0 ? '&' : '?';
    return URL_BASE + path + sep + 'token=' + encodeURIComponent(getAuthToken());
}

function getCookie(name) {
    const m = document.cookie.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
    return m ? m[1] : null;
}

function setCookie(name, value, days) {
    const exp = new Date(Date.now() + days * 864e5).toUTCString();
    document.cookie = name + '=' + value + '; expires=' + exp + '; path=/; SameSite=Lax';
}

function handleAuthExpired() {
    window.location.replace(URL_BASE + '/login?next=' +
        encodeURIComponent(URL_BASE + '/listen'));
}

// ── State mirror ────────────────────────────────────────────────────

const radioState = {};
let bands = [];
let uiModes = [];
let memChannels = [];
let gainBoost = 10.0;

// ── DOM ─────────────────────────────────────────────────────────────

const el = {};
[
    'freq-display', 'mode-badge', 'band-name', 'waterfall', 'meter-fill',
    'meter-text', 'freq-input', 'freq-set-btn', 'band-btns', 'mode-btns',
    'mem-list', 'vol-slider', 'status-line', 'conn-dot', 'tx-badge',
    'start-overlay', 'start-btn', 'logout-btn', 'online-count', 'net-stats',
].forEach((id) => { el[id] = document.getElementById(id); });

let statusTimer = null;
function showStatus(text, isError) {
    el['status-line'].textContent = text || '';
    el['status-line'].style.color = isError === false ? '#9ca3af' : '#ef4444';
    if (statusTimer) clearTimeout(statusTimer);
    if (text) {
        statusTimer = setTimeout(() => { el['status-line'].textContent = ''; }, 6000);
    }
}

// ── Rendering ───────────────────────────────────────────────────────

function formatFreq(hz) {
    if (!hz || hz <= 0) return '--.---.---';
    const s = String(Math.round(hz)).padStart(9, '0');
    return s.slice(0, 3).replace(/^0+/, '') + '.' + s.slice(3, 6) + '.' + s.slice(6);
}

function renderState() {
    const freq = radioState.active_freq ||
        (radioState.active_vfo === 'B' ? radioState.vfo_b_freq : radioState.vfo_a_freq);
    el['freq-display'].textContent = formatFreq(freq);
    el['mode-badge'].textContent =
        radioState.mode_display || radioState.mode_name || '---';
    el['band-name'].textContent = radioState.band_name || '';

    const raw = typeof radioState.s_meter === 'number' ? radioState.s_meter : 0;
    el['meter-fill'].style.width = Math.round((raw / 255) * 100) + '%';
    let meterText = radioState.s_unit || '—';
    if (typeof radioState.s_meter_dbm === 'number') {
        meterText += '  ' + Math.round(radioState.s_meter_dbm) + ' dBm';
    }
    el['meter-text'].textContent = meterText;

    el['tx-badge'].classList.toggle('on', (radioState.tx_status || 0) > 0);

    if (radioState.serial_connected === false) {
        showStatus('电台未连接 (CAT)', true);
    }

    document.querySelectorAll('#mode-btns .qbtn').forEach((btn) => {
        const name = btn.dataset.mode;
        btn.classList.toggle('active',
            name === radioState.mode_name || name === radioState.mode_display);
    });
}

function renderBandButtons() {
    el['band-btns'].innerHTML = '';
    bands.forEach((b) => {
        const btn = document.createElement('button');
        btn.className = 'qbtn';
        btn.textContent = b.name;
        btn.addEventListener('click', () => {
            if (b.default_freq) sendSet('freq', b.default_freq);
        });
        el['band-btns'].appendChild(btn);
    });
}

function renderModeButtons() {
    el['mode-btns'].innerHTML = '';
    uiModes.forEach((name) => {
        const btn = document.createElement('button');
        btn.className = 'qbtn';
        btn.textContent = name;
        btn.dataset.mode = name;
        btn.addEventListener('click', () => sendSet('mode', name));
        el['mode-btns'].appendChild(btn);
    });
    renderState();
}

function renderMemChannels() {
    el['mem-list'].innerHTML = '';
    let any = false;
    memChannels.forEach((ch) => {
        if (!ch || !ch.freq) return;
        any = true;
        const btn = document.createElement('button');
        btn.className = 'qbtn mem-btn';
        const label = document.createElement('span');
        label.textContent = ch.label || ((ch.freq / 1000).toFixed(1) + ' kHz');
        const freq = document.createElement('span');
        freq.className = 'mem-freq';
        freq.textContent = (ch.freq / 1e6).toFixed(4) + ' ' + (ch.mode || '');
        btn.appendChild(label);
        btn.appendChild(freq);
        btn.addEventListener('click', () => sendMsg({
            type: 'memRecall', freq: ch.freq, mode: ch.mode,
        }));
        el['mem-list'].appendChild(btn);
    });
    document.getElementById('mem-panel').classList.toggle('hidden', !any);
}

// ── Control WebSocket (/WSradio) ────────────────────────────────────

let ctrlWs = null;
let ctrlRetry = 0;
let netBytes = 0;
let netBytesLast = 0;
let netRate = 0;
let rttMs = null;
let pingSentAt = null;
let pingTimer = null;

function updateNetStats() {
    netRate = netBytes - netBytesLast;
    netBytesLast = netBytes;
    const kb = netRate >= 1024 ? (netRate / 1024).toFixed(1) + ' KB/s'
        : netRate + ' B/s';
    el['net-stats'].textContent = ' · ↓ ' + kb +
        (rttMs !== null ? ' · ' + rttMs + ' ms' : '');
}

function sendMsg(obj) {
    if (ctrlWs && ctrlWs.readyState === WebSocket.OPEN) {
        ctrlWs.send(JSON.stringify(obj));
    }
}

function sendSet(field, value) {
    sendMsg({ type: 'set', field: field, value: value });
}

function connectControl() {
    const ws = new WebSocket(wsUrlWithAuth('/WSradio'));
    ctrlWs = ws;

    ws.onopen = () => {
        ctrlRetry = 0;
        el['conn-dot'].classList.add('on');
        showStatus('', false);
        if (pingTimer) clearInterval(pingTimer);
        pingTimer = setInterval(() => {
            if (ctrlWs && ctrlWs.readyState === WebSocket.OPEN) {
                pingSentAt = performance.now();
                sendMsg({ type: 'ping' });
            }
        }, 5000);
    };

    ws.onmessage = (ev) => {
        netBytes += ev.data.length;
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }

        if (msg.type === 'pong') {
            if (pingSentAt !== null) {
                rttMs = Math.round(performance.now() - pingSentAt);
                pingSentAt = null;
            }
        } else if (msg.type === 'fullState') {
            Object.assign(radioState, msg.data || {});
            bands = Array.isArray(msg.bands) ? msg.bands : [];
            uiModes = Array.isArray(msg.modes) ? msg.modes : [];
            memChannels = Array.isArray(msg.memChannels) ? msg.memChannels : [];
            const caps = msg.capabilities || {};
            if (typeof caps.audio_gain_boost === 'number') gainBoost = caps.audio_gain_boost;
            renderBandButtons();
            renderModeButtons();
            renderMemChannels();
            renderState();
        } else if (msg.type === 'stateUpdate') {
            Object.assign(radioState, msg.fields || {});
            renderState();
        } else if (msg.type === 'value') {
            radioState[msg.field] = msg.value;
            renderState();
        } else if (msg.type === 'memChannels') {
            memChannels = Array.isArray(msg.channels) ? msg.channels : [];
            renderMemChannels();
        } else if (msg.type === 'onlineUsers') {
            el['online-count'].textContent = ' · 在线 ' + msg.count + ' 人';
        } else if (msg.type === 'error') {
            showStatus(msg.message || '命令被拒绝', true);
        }
    };

    ws.onclose = (ev) => {
        el['conn-dot'].classList.remove('on');
        if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
        if (ev.code === 4001) { handleAuthExpired(); return; }
        ctrlRetry = Math.min(ctrlRetry + 1, 6);
        setTimeout(connectControl, 500 * Math.pow(2, ctrlRetry));
    };

    ws.onerror = () => { try { ws.close(); } catch (e) { /* noop */ } };
}

// ── Waterfall (/WSspectrum) ─────────────────────────────────────────

const WF_COLS = 850;
const colormap = (() => {
    const map = [];
    for (let i = 0; i < 256; i++) {
        // Black → deep red → amber heat map.
        const r = Math.min(255, i * 1.4);
        const g = Math.min(255, Math.max(0, (i - 60) * 1.1));
        const b = Math.min(120, Math.max(0, (i - 180) * 0.6));
        map.push([r | 0, g | 0, b | 0]);
    }
    return map;
})();

let wfCtx = null;
let spectrumRetry = 0;

function drawWaterfallRow(wf1) {
    if (!wfCtx) return;
    const canvas = el.waterfall;
    const h = canvas.height;
    wfCtx.drawImage(canvas, 0, 1);
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

function connectSpectrum() {
    const ws = new WebSocket(wsUrlWithAuth('/WSspectrum'));
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => { spectrumRetry = 0; };

    ws.onmessage = (ev) => {
        const data = new Uint8Array(ev.data);
        netBytes += data.byteLength;
        if (data.length < 851) return;
        const version = data[0];
        if (version < 1 || version > 2) return;
        drawWaterfallRow(data.subarray(1, 851));
    };

    ws.onclose = (ev) => {
        if (ev.code === 4001) return; // control channel handles the redirect
        spectrumRetry = Math.min(spectrumRetry + 1, 6);
        setTimeout(connectSpectrum, 500 * Math.pow(2, spectrumRetry));
    };

    ws.onerror = () => { try { ws.close(); } catch (e) { /* noop */ } };
}

// ── RX audio (/WSaudioRX) ───────────────────────────────────────────

const AUDIO_TAG_PCM = 0x00;
const AUDIO_TAG_OPUS = 0x01;

let audioCtx = null;
let rxGainNode = null;
let rxNode = null;
let rxWs = null;
let audioRetry = 0;
let rxOpusDecoder = null;
let rxFrames = 0;
let rxDecoded = 0;
let rxDropped = 0;
let rxPeak = 0;
let jitterMs = 0;
let micActivated = false;
let micPending = false;

const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

// ── Screen Wake Lock (pattern from legacy mrrc mobile_modern.js) ────
// Keeps the screen — and with it the audio — alive while listening.
// The OS releases the lock on screen-off/tab-hide, so it is re-requested
// on every visibilitychange back to visible.
let wakeLock = null;

async function requestWakeLock() {
    if (wakeLock || !('wakeLock' in navigator)) return;
    try {
        wakeLock = await navigator.wakeLock.request('screen');
        wakeLock.addEventListener('release', () => { wakeLock = null; });
    } catch (e) { /* NotAllowedError etc. — listening still works */ }
}

document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') requestWakeLock();
});

async function activateAudioSession() {
    // iOS Safari quirk (field-confirmed on the main UI: RX only sounds
    // after the first PTT): Web Audio playback stays silent until the
    // audio session is flipped to play-and-record. Requesting — and
    // immediately releasing — the microphone does exactly that.
    // Nothing is recorded; the tracks are stopped at once.
    // MUST NOT block: in some browsers (in-app webviews) the permission
    // prompt never settles, so this runs in parallel with a timeout.
    if (!isIOS || micActivated || micPending) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return;
    micPending = true;
    try {
        const stream = await Promise.race([
            navigator.mediaDevices.getUserMedia({ audio: true }),
            new Promise((_, reject) =>
                setTimeout(() => reject(new Error('mic prompt timeout')), 10000)),
        ]);
        stream.getTracks().forEach((t) => t.stop());
        micActivated = true;
    } catch (e) {
        showStatus('麦克风权限未授予/无响应 — 若无声可在系统设置里允许后重试 ' +
            '（不录音，仅借权限激活扬声器输出）', true);
    } finally {
        micPending = false;
        updateDiag();
    }
}

function updateDiag() {
    const line = document.getElementById('diag-line');
    if (!line) return;
    if (!audioCtx) { line.textContent = ''; return; }
    rxPeak *= 0.85; // slow decay so the level reads like a meter
    const kind = rxNode ? 'worklet' : (spPush ? 'scriptproc' : 'none');
    const mic = micActivated ? ' | mic✓' : (micPending ? ' | mic…(弹窗未答)' : '');
    let text = 'audio: ' + audioCtx.state + ' | ' + kind + mic +
        (wakeLock ? ' | 🔒' : '') +
        ' | 帧 ' + rxFrames + ' 解码 ' + rxDecoded + ' 丢 ' + rxDropped +
        ' | lvl ' + Math.round(rxPeak * 100) + '% | buf ' + jitterMs + 'ms';
    if (audioCtx.state === 'suspended') text += ' — 点一下页面激活';
    else if (rxFrames > 50 && rxDecoded === 0) text += ' — Opus 解码失败';
    line.textContent = text;
}

function getRxOpusDecoder() {
    if (rxOpusDecoder) return rxOpusDecoder;
    try {
        if (typeof OpusDecoder === 'undefined') return null;
        rxOpusDecoder = new OpusDecoder(48000, 1);
    } catch (e) {
        rxOpusDecoder = null;
    }
    return rxOpusDecoder;
}

function decodeRxAudioFrame(data) {
    if (!data || data.byteLength < 1) return null;
    const bytes = new Uint8Array(data);
    const tag = bytes[0];
    const payload = data.slice(1);

    if (tag === AUDIO_TAG_OPUS) {
        const dec = getRxOpusDecoder();
        if (!dec) return null;
        try {
            const f32 = dec.decode_float(payload);
            return new Float32Array(f32); // copy out of the WASM heap
        } catch (e) { return null; }
    }

    // PCM (0x00 or legacy untagged): Int16 → Float32
    try {
        if (payload.byteLength < 2) return null;
        const i16 = new Int16Array(payload);
        const f32 = new Float32Array(i16.length);
        const scale = 1.0 / 32767.0;
        for (let i = 0; i < i16.length; i++) f32[i] = i16[i] * scale;
        return f32;
    } catch (e) { return null; }
}

function applyVolume() {
    if (!rxGainNode || !audioCtx) return;
    const v = parseInt(el['vol-slider'].value, 10) || 0;
    const target = Math.min(10.0, (v / 255.0) * gainBoost);
    rxGainNode.gain.setTargetAtTime(target, audioCtx.currentTime, 0.015);
}

async function initAudio() {
    // Runs in parallel — the audio chain must never wait on a permission
    // prompt that may never settle (in-app webviews hang getUserMedia).
    const activation = activateAudioSession();
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({
        latencyHint: 'interactive',
        sampleRate: 48000,
    });
    if (audioCtx.state === 'suspended') {
        try { await audioCtx.resume(); } catch (e) { /* noop */ }
    }
    rxGainNode = audioCtx.createGain();
    audioCtx.onstatechange = updateDiag;

    try {
        if (!audioCtx.audioWorklet) throw new Error('AudioWorklet unavailable');
        await audioCtx.audioWorklet.addModule(
            staticUrlWithAuth('/rx_worklet_processor.js?v=2'));
        rxNode = new AudioWorkletNode(audioCtx, 'rx-player');
        rxNode.port.postMessage({
            type: 'config', prebufferMs: 120, recoveryMs: 60, maxMs: 300,
        });
        rxNode.port.onmessage = (ev) => {
            if (ev.data && ev.data.type === 'stats') {
                jitterMs = Math.round(ev.data.bufferMs || 0);
            }
        };
        rxNode.connect(rxGainNode);
    } catch (e) {
        // iOS Safari: same ScriptProcessor fallback as the main UI
        // (60 ms prebuffer, 300 ms cap).
        rxNode = null;
        setupScriptProcessorFallback();
    }
    rxGainNode.connect(audioCtx.destination);

    const saved = getCookie('mrrc_listen_vol');
    if (saved !== null && !isNaN(parseInt(saved, 10))) {
        el['vol-slider'].value = parseInt(saved, 10);
    }
    applyVolume();
    connectAudioRX();
}

let spPush = null;

function setupScriptProcessorFallback() {
    const node = audioCtx.createScriptProcessor(2048, 1, 1);
    let queue = [];
    let queued = 0;
    const prebuffer = Math.round(0.06 * audioCtx.sampleRate);
    const maxBuffer = Math.round(0.3 * audioCtx.sampleRate);
    let priming = true;

    node.onaudioprocess = (ev) => {
        const out = ev.outputBuffer.getChannelData(0);
        let written = 0;
        if (priming) {
            if (queued < prebuffer) { out.fill(0); return; }
            priming = false;
        }
        while (written < out.length && queue.length > 0) {
            const chunk = queue[0];
            const n = Math.min(chunk.length, out.length - written);
            out.set(chunk.subarray(0, n), written);
            written += n;
            queued -= n;
            if (n >= chunk.length) queue.shift();
            else queue[0] = chunk.subarray(n);
        }
        if (written < out.length) {
            out.fill(0, written);
            queued = 0;
            queue = [];
            priming = true;
        }
    };

    spPush = (f32) => {
        queue.push(f32);
        queued += f32.length;
        while (queued > maxBuffer && queue.length > 1) {
            queued -= queue.shift().length;
        }
    };
    node.connect(rxGainNode);
}

function connectAudioRX() {
    const ws = new WebSocket(wsUrlWithAuth('/WSaudioRX'));
    ws.binaryType = 'arraybuffer';
    rxWs = ws;

    ws.onopen = () => { audioRetry = 0; };

    ws.onmessage = (ev) => {
        netBytes += ev.data.byteLength;
        rxFrames += 1;
        const f32 = decodeRxAudioFrame(ev.data);
        if (!f32) { rxDropped += 1; return; }
        rxDecoded += 1;
        // Cheap peak sample for the diag level meter (every 32nd sample).
        for (let i = 0; i < f32.length; i += 32) {
            const a = f32[i] < 0 ? -f32[i] : f32[i];
            if (a > rxPeak) rxPeak = a;
        }
        if (rxNode) rxNode.port.postMessage({ type: 'push', payload: f32 });
        else if (spPush) spPush(f32);
    };

    ws.onclose = (ev) => {
        if (ev.code === 4001) return;
        audioRetry = Math.min(audioRetry + 1, 6);
        setTimeout(connectAudioRX, 500 * Math.pow(2, audioRetry));
    };

    ws.onerror = () => { try { ws.close(); } catch (e) { /* noop */ } };
}

// ── UI wiring ───────────────────────────────────────────────────────

function tuneFromInput() {
    const khz = parseFloat(el['freq-input'].value);
    if (isNaN(khz) || khz < 30 || khz > 75000) {
        showStatus('无效频率 — 输入 kHz 值，例如 7050', true);
        return;
    }
    sendSet('freq', Math.round(khz * 1000));
    el['freq-input'].value = '';
}

el['freq-set-btn'].addEventListener('click', tuneFromInput);
el['freq-input'].addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') tuneFromInput();
});

el['vol-slider'].addEventListener('input', () => {
    applyVolume();
    setCookie('mrrc_listen_vol', el['vol-slider'].value, 365);
});

document.getElementById('test-sound-btn').addEventListener('click', async () => {
    if (!audioCtx) { showStatus('请先点「开始收听」', true); return; }
    await activateAudioSession(); // iOS: mic permission unlocks playback
    if (audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
    // 1) iOS media-session activation: an <audio> element playing switches
    //    Safari's audio session out of the ambient (silent-switch-muted)
    //    category — a known cure for "Web Audio is silent on iPhone".
    try {
        const a = new Audio('data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAIlYAAESsAAACABAAZGF0YQAAAAA=');
        a.play().catch(() => {});
    } catch (e) { /* noop */ }
    // 2) 0.6 s 880 Hz tone through the SAME gain chain as the radio audio.
    const osc = audioCtx.createOscillator();
    const g = audioCtx.createGain();
    osc.frequency.value = 880;
    g.gain.value = 0.2;
    osc.connect(g);
    g.connect(rxGainNode);
    osc.start();
    osc.stop(audioCtx.currentTime + 0.6);
    showStatus('测试音已播放（880Hz, 0.6s）— 听到了吗？', false);
});

el['logout-btn'].addEventListener('click', async () => {
    try {
        await fetch(URL_BASE + '/api/auth/logout', { method: 'POST' });
    } catch (e) { /* noop */ }
    handleAuthExpired();
});

el['start-btn'].addEventListener('click', async () => {
    el['start-overlay'].classList.add('hidden');
    const canvas = el.waterfall;
    canvas.width = WF_COLS;
    canvas.height = 180;
    wfCtx = canvas.getContext('2d');
    wfCtx.fillStyle = '#000';
    wfCtx.fillRect(0, 0, canvas.width, canvas.height);
    connectControl();
    connectSpectrum();
    requestWakeLock(); // keep screen (and audio) alive while listening
    await initAudio();
    updateDiag();
    setInterval(updateDiag, 1000);
    setInterval(updateNetStats, 1000);
});

// iOS can leave the context suspended when the activating gesture was
// interrupted (page switch, control-center swipe): any later tap resumes it.
['touchend', 'click'].forEach((evt) => {
    document.addEventListener(evt, () => {
        if (audioCtx && audioCtx.state === 'suspended') {
            audioCtx.resume().then(updateDiag).catch(() => {});
        }
    }, { passive: true });
});

// Token must exist before anything runs — otherwise back to login.
if (!getAuthToken()) {
    handleAuthExpired();
}

if (isIOS) {
    const note = document.getElementById('ios-note');
    if (note) {
        note.style.display = 'block';
        note.textContent = 'iPhone 提示：系统会请求麦克风权限 — ' +
            '这只是激活扬声器输出的手段（iOS 限制），不会录音。';
    }
}
