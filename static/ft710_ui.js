/**
 * FT-710 Web Control — UI Rendering & Event Handlers
 * ===================================================
 * All DOM manipulation, canvas drawing, button logic, and event wiring.
 * Depends on: ft710_main.js (radioState, bands, uiModes, sendCommand, etc.)
 */

// ── Tuning step ─────────────────────────────────────────────────────
let currentTuneStep = 1000;  // Hz
const tuneSteps = [10, 100, 1000, 5000, 10000, 25000];
const DEFAULT_BAND_CYCLE = [
    {name: '160m', start: 1_800_000, end: 2_000_000, default_freq: 1_845_500},
    {name: '80m', start: 3_500_000, end: 4_000_000, default_freq: 3_850_000},
    {name: '60m', start: 5_250_000, end: 5_450_000, default_freq: 5_350_000},
    {name: '40m', start: 7_000_000, end: 7_300_000, default_freq: 7_050_000},
    {name: '30m', start: 10_100_000, end: 10_150_000, default_freq: 10_140_000},
    {name: '20m', start: 14_000_000, end: 14_350_000, default_freq: 14_270_000},
    {name: '17m', start: 18_068_000, end: 18_168_000, default_freq: 18_132_500},
    {name: '15m', start: 21_000_000, end: 21_450_000, default_freq: 21_400_000},
    {name: '12m', start: 24_890_000, end: 24_990_000, default_freq: 24_952_500},
    {name: '10m', start: 28_000_000, end: 29_700_000, default_freq: 28_450_000},
    {name: '6m', start: 50_000_000, end: 54_000_000, default_freq: 50_150_000},
    {name: '4m', start: 70_000_000, end: 70_500_000, default_freq: 70_250_000},
];

function tuneBy(delta) {
    const freq = radioState.active_vfo === 'A' ? radioState.vfo_a_freq : radioState.vfo_b_freq;
    const newFreq = Math.max(30000, Math.min(75000000, freq + delta));
    const field = radioState.active_vfo === 'A' ? 'freq' : 'vfo_b_freq';
    sendCommand(field, newFreq);
    // Optimistic update
    if (radioState.active_vfo === 'A') {
        radioState.vfo_a_freq = newFreq;
    } else {
        radioState.vfo_b_freq = newFreq;
    }
    renderFrequency();
}

// ── Frequency Display ───────────────────────────────────────────────
function formatFrequency(hz) {
    // Split 7.050.000 Hz into display digits:
    //   f10m f1m . f100k f10k f1k . f100h f10h
    //   0    7   .  0     5    0   .  0     0   = 07.050.00 = 7.050 MHz
    const m10 = Math.floor(hz / 10_000_000) % 10;   // tens of MHz
    const m1  = Math.floor(hz / 1_000_000) % 10;    // ones of MHz
    const k100 = Math.floor(hz / 100_000) % 10;     // 100s of kHz
    const k10  = Math.floor(hz / 10_000) % 10;      // 10s of kHz
    const k1   = Math.floor(hz / 1_000) % 10;       // 1s of kHz
    const h100 = Math.floor(hz / 100) % 10;         // 100s of Hz
    const h10  = Math.floor(hz / 10) % 10;          // 10s of Hz
    return { m10: String(m10), m1: String(m1), k100: String(k100), k10: String(k10), k1: String(k1), h100: String(h100), h10: String(h10) };
}

function renderFrequency() {
    const freq = radioState.active_vfo === 'A' ? radioState.vfo_a_freq : radioState.vfo_b_freq;
    const f = formatFrequency(freq);
    setText('f10m', f.m10);
    setText('f1m', f.m1);
    setText('f100k', f.k100);
    setText('f10k', f.k10);
    setText('f1k', f.k1);
    setText('f100h', f.h100);
    // Use f10h if available, otherwise add a trailing 0 span id
    const f10h = document.getElementById('f10h');
    if (f10h) f10h.textContent = f.h10;
}

function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

// ── S-Meter Rendering ───────────────────────────────────────────────
const S_METER_SEGMENTS = [
    { raw: 0, s: 'S0', dbm: -54 },
    { raw: 12, s: 'S1', dbm: -48 },
    { raw: 27, s: 'S2', dbm: -42 },
    { raw: 40, s: 'S3', dbm: -36 },
    { raw: 55, s: 'S4', dbm: -30 },
    { raw: 65, s: 'S5', dbm: -24 },
    { raw: 80, s: 'S6', dbm: -18 },
    { raw: 95, s: 'S7', dbm: -12 },
    { raw: 112, s: 'S8', dbm: -6 },
    { raw: 130, s: 'S9', dbm: 0 },
    { raw: 150, s: '+10', dbm: 10 },
    { raw: 172, s: '+20', dbm: 20 },
    { raw: 190, s: '+30', dbm: 30 },
    { raw: 220, s: '+40', dbm: 40 },
    { raw: 240, s: '+50', dbm: 50 },
    { raw: 255, s: '+60', dbm: 60 },
];

function renderSMeter() {
    const canvas = document.getElementById('smeter-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const w = canvas.width;
    const h = canvas.height;

    ctx.clearRect(0, 0, w, h);

    const raw = radioState.s_meter;
    // Map raw value to position
    const pos = (raw / 255) * w;

    // Background gradient
    const bgGrad = ctx.createLinearGradient(0, 0, w, 0);
    bgGrad.addColorStop(0, '#22c55e');
    bgGrad.addColorStop(0.3, '#22c55e');
    bgGrad.addColorStop(0.5, '#eab308');
    bgGrad.addColorStop(0.7, '#f59e0b');
    bgGrad.addColorStop(1, '#ef4444');
    ctx.fillStyle = bgGrad;
    ctx.fillRect(0, 2, pos, h - 4);

    // Unfilled portion
    ctx.fillStyle = 'rgba(255,255,255,0.05)';
    ctx.fillRect(pos, 2, w - pos, h - 4);

    // S-unit markers
    ctx.fillStyle = 'rgba(255,255,255,0.3)';
    const marks = [0, 12, 27, 40, 55, 65, 80, 95, 112, 130, 150, 172, 190, 220, 240, 255];
    for (const m of marks) {
        const x = (m / 255) * w;
        ctx.fillRect(x, 0, 1, h);
    }

    // Border
    ctx.strokeStyle = 'rgba(255,255,255,0.15)';
    ctx.lineWidth = 1;
    ctx.strokeRect(0.5, 0.5, w - 1, h - 1);

    // Update text values (dB relative to S9=0, not absolute dBm)
    setText('smeter-value', radioState.s_unit);
    setText('smeter-dbm', radioState.s_meter_dbm.toFixed(0) + ' dB');
}

// ── Meter Rendering ─────────────────────────────────────────────────
function renderMeters() {
    // Power meter — watts (calibrated in backend from RM5 raw 0-255).
    // FT-710 is a 100W radio (110W max on the scale).
    const pwrW = radioState.power_watts || 0;
    const pwrPct = Math.min(100, pwrW / 110 * 100);
    setMeterBar('meter-pwr-bar', pwrPct);
    setText('meter-pwr-val', pwrW.toFixed(1));

    // ALC meter — 0-100% deflection (RM4 raw 0-255).
    const alcPct = radioState.alc_pct || 0;
    setMeterBar('meter-alc-bar', alcPct);
    setText('meter-alc-val', alcPct.toFixed(0));

    // SWR meter — ratio 1.0..9.9 (calibrated in backend from RM6 raw).
    const swrVal = radioState.swr_ratio || 1.0;
    const swrPct = Math.min(100, (swrVal - 1) / 4 * 100);  // 1.0->0%, 5.0->100%
    setMeterBar('meter-swr-bar', swrPct);
    setText('meter-swr-val', swrVal.toFixed(1));

    // Id (drain current) — amps (calibrated from RM7 raw).
    // FT-710 的 RM7 只在发射时响应; 接收时无电流遥测, 显示占位符 "—"
    // （避免 0.0A 误导为故障）。
    const idA = radioState.id_amps || 0;
    const hasId = radioState.id_amps > 0;
    const idPct = Math.min(100, idA / 26 * 100);
    setMeterBar('meter-id-bar', hasId ? idPct : 0);
    setText('meter-id-val', hasId ? idA.toFixed(1) : '—');

    // Vd (drain/supply voltage) — volts (calibrated from RM8 raw).
    const vdV = radioState.vd_volts || 0;
    const vdPct = Math.min(100, vdV / 16 * 100);
    setMeterBar('meter-vd-bar', vdPct);
    setText('meter-vd-val', vdV.toFixed(1));
}

function setMeter(barId, valId, pct, raw) {
    setMeterBar(barId, pct);
    setText(valId, raw);
}

function setMeterBar(barId, pct) {
    const bar = document.getElementById(barId);
    if (bar) {
        bar.style.width = Math.min(100, Math.max(0, pct)) + '%';
    }
}

// ── Status Bar ──────────────────────────────────────────────────────
function renderStatusBar() {
    setText('status-band', radioState.band_name);
    setText('status-mode', radioState.mode_display);

    const txEl = document.getElementById('status-tx');
    if (txEl) {
        if (radioState.tx_status === 1) {
            txEl.textContent = 'TX';
            txEl.classList.add('tx');
        } else if (radioState.tx_status === 2) {
            txEl.textContent = 'TUNE';
            txEl.classList.add('tx');
        } else {
            txEl.textContent = 'RX';
            txEl.classList.remove('tx');
        }
    }

    const dot = document.querySelector('#status-serial .status-dot');
    if (dot) {
        if (radioState.serial_connected) {
            dot.classList.add('connected');
        } else {
            dot.classList.remove('connected');
        }
    }

    const audioWarn = document.getElementById('status-audio-warn');
    if (audioWarn) {
        audioWarn.style.display = radioState.rx_audio_silent ? '' : 'none';
    }
}

// ── Button Labels ───────────────────────────────────────────────────
const modeCycle = ['LSB', 'USB', 'CW-U', 'AM', 'FM', 'RTTY-L', 'DATA-L'];

function getNextMode(currentMode) {
    const idx = modeCycle.indexOf(currentMode);
    return modeCycle[(idx + 1) % modeCycle.length];
}

function getBandCycle() {
    const serverBandsByName = new Map((bands || []).map(b => [b.name, b]));
    return DEFAULT_BAND_CYCLE.map(function(defaultBand) {
        return Object.assign({}, defaultBand, serverBandsByName.get(defaultBand.name) || {});
    });
}

// Dead simple: find current band in the cycle, return the next one.
// Falls off the end → wraps to 160m.  Unknown band → starts at 160m.
function getNextBand(currentBand) {
    const bandList = getBandCycle();
    let idx = bandList.findIndex(b => b.name === currentBand);
    if (idx < 0) idx = 0;   // unknown → 160m is as good a guess as any
    const nextIdx = (idx + 1) % bandList.length;
    return bandList[nextIdx];
}

function _filterTablesFor(modeName) {
    // Server-authoritative tables (fullState.filterTables, from config.py)
    // with the legacy hardcoded copies as fallback.
    const t = window.filterTables;
    let isNarrow;
    if (t && Array.isArray(t.narrowModes)) {
        isNarrow = t.narrowModes.includes(modeName);
    } else {
        isNarrow = ['CW-U','CW-L','RTTY-L','RTTY-U','DATA-L','DATA-U','PSK'].includes(modeName);
    }
    const widths = {};
    const list = t ? (isNarrow ? t.narrow : t.voice) : null;
    if (list) {
        for (const pair of list) widths[pair[0]] = pair[1];
    } else {
        const fullVoice = {1:300,2:400,3:600,4:850,5:1100,6:1200,7:1500,8:1650,9:1800,10:1950,11:2100,12:2250,13:2400,14:2450,15:2500,16:2600,17:2700,18:2800,19:2900,20:3000,21:3200,22:3500,23:4000};
        const fullNarrow = {1:50,2:100,3:150,4:200,5:250,6:300,7:350,8:400,9:450,10:500,11:600,12:800,13:1200,14:1400,15:1700,16:2000,17:2400,18:3000,19:3200,20:3500,21:4000};
        Object.assign(widths, isNarrow ? fullNarrow : fullVoice);
    }
    return { widths: widths, isNarrow: isNarrow };
}

function getNextFilter(currentIdx, modeName) {
    // Curated filter rotation for each mode group
    const isNarrow = _filterTablesFor(modeName).isNarrow;
    // Voice: 1.8k / 2.4k / 2.7k / 3k / 4k (WIDE/"无")
    const voiceList = [9, 13, 17, 20, 23];   // → 1800, 2400, 2700, 3000, 4000 Hz
    // Narrow: 150 / 300 / 500 / 1200 / 2400 / 4000
    const narrowList = [3, 6, 10, 13, 17, 21]; // → 150, 300, 500, 1200, 2400, 4000 Hz
    const list = isNarrow ? narrowList : voiceList;
    var pos = list.indexOf(currentIdx);
    if (pos < 0 || pos >= list.length - 1) return list[0];
    return list[pos + 1];
}

function getFilterLabel(idx, modeName) {
    const hz = _filterTablesFor(modeName).widths[idx];
    if (!hz) return '--';
    if (hz === 4000) return '无';
    if (hz >= 1000) return (hz/1000).toFixed(1) + 'k';
    return hz + 'Hz';
}

function renderButtonLabels() {
    const modeName = radioState.mode_name;
    const nextMode = getNextMode(modeName);
    setText('btn-mode', nextMode);
    document.getElementById('btn-mode').dataset.current = modeName;

    const bandName = radioState.band_name;
    const nextBand = getNextBand(bandName);
    if (nextBand) {
        setText('btn-band', nextBand.name);
        document.getElementById('btn-band').dataset.current = bandName;
    }

    const filterIdx = radioState.filter_width;
    const nextIdx = getNextFilter(filterIdx, modeName);
    setText('btn-filter', getFilterLabel(nextIdx, modeName));
    document.getElementById('btn-filter').dataset.current = filterIdx;

    // ATT cycle: OFF -> 6dB -> 12dB -> 18dB -> OFF (short labels ≤3 chars)
    const attLabels = {0:'OF', 1:'6d', 2:'12', 3:'18'};
    const nextAtt = (radioState.attenuator + 1) % 4;
    setText('btn-att', attLabels[nextAtt]);

    // PRE cycle: OFF -> AMP1 -> AMP2 -> OFF (short labels ≤3 chars)
    const preLabels = {0:'OF', 1:'A1', 2:'A2'};
    const nextPre = (radioState.preamp + 1) % 3;
    setText('btn-pre', preLabels[nextPre]);
}

// ── Toggle States ───────────────────────────────────────────────────
function renderToggles() {
    setDspBtn('dsp-nr', radioState.noise_reduction);
    setDspBtn('dsp-nb', radioState.noise_blanker);
    setDspBtn('dsp-an', radioState.auto_notch);
    setDspBtn('dsp-comp', radioState.compressor);
    setDspBtn('dsp-atu', radioState.tuner_status > 0);
}

function setDspBtn(id, active) {
    const el = document.getElementById(id);
    if (el) el.classList.toggle('on', active);
}

// ── Sliders ─────────────────────────────────────────────────────────
function renderSliders() {
    // NOTE: the AF slider is browser-side playback volume (cookie),
    // NOT the radio's AF gain — it is intentionally not rendered from
    // radio state, or the CAT poll would fight the user's setting.
    setSlider('slider-rfpower', 'val-rfpower', radioState.rf_power);
    // RF Gain (RG 0-255) shown as 0-100% on the slider.
    setSlider('slider-rfgain', 'val-rfgain',
        Math.round((radioState.rf_gain ?? 255) / 255 * 100));
}

function setSlider(sliderId, valId, value) {
    const slider = document.getElementById(sliderId);
    const val = document.getElementById(valId);
    if (slider) slider.value = value;
    if (val) val.textContent = value;
}

// ── VFO Buttons ─────────────────────────────────────────────────────
function renderVFOButtons() {
    const vfoa = document.getElementById('btn-vfoa');
    const vfob = document.getElementById('btn-vfob');
    if (vfoa) vfoa.classList.toggle('active', radioState.active_vfo === 'A');
    if (vfob) vfob.classList.toggle('active', radioState.active_vfo === 'B');

    const splitBtn = document.getElementById('btn-split');
    if (splitBtn) splitBtn.classList.toggle('split-on', radioState.split);

    const vfoInd = document.getElementById('vfo-indicator');
    if (vfoInd) vfoInd.textContent = 'VFO-' + radioState.active_vfo;
}

// ── Memory Channels ─────────────────────────────────────────────────
function renderMemoryChannels() {
    document.querySelectorAll('.mem-btn').forEach(btn => {
        const idx = parseInt(btn.dataset.mem);
        const ch = memChannels[idx];
        const freqEl = btn.querySelector('.mem-freq');
        if (freqEl) {
            if (ch && ch.freq) {
                freqEl.textContent = (ch.freq / 1e6).toFixed(3);
                btn.title = ch.label || '';
            } else {
                freqEl.textContent = '---';
                btn.title = '';
            }
        }
    });
}

// ── PTT Visual ──────────────────────────────────────────────────────
function renderPTTState() {
    const btn = document.getElementById('btn-ptt');
    if (btn) {
        btn.classList.toggle('tx-active', radioState.is_transmitting);
    }
    const tuneBtn = document.getElementById('btn-tune');
    if (tuneBtn) {
        tuneBtn.classList.toggle('tune-active', radioState.tx_status === 2);
    }
    // Re-apply RX gain: dim during TX/TUNE, restore on RX. This is the
    // single chokepoint reached by both the optimistic PTT handlers and
    // server-driven stateUpdate / the PTT watchdog's forced-RX path.
    if (typeof _applyAfGainToAudioNode === 'function') _applyAfGainToAudioNode();
    renderRecordingState();
}

function renderRecordingState() {
    const recordBtn = document.getElementById('btn-record');
    if (!recordBtn) return;
    const recorder = window.RXRecorder;
    const active = !!(recorder && recorder.isActive);
    recordBtn.disabled = false;
    recordBtn.classList.toggle('record-active', active);
    recordBtn.textContent = active ? 'STOP' : 'REC';
    recordBtn.title = '录制接收音频为 MP3 (128kbps，首次点击时加载编码器)';
}

function renderFFTPlot(wf1) {
    const canvas = document.getElementById('fft-canvas');
    if (!canvas) return;
    if (canvas.width < 100 || canvas.height < 5) return;

    const ctx = canvas.getContext('2d');
    const w = canvas.width;
    const h = canvas.height;

    // Clear
    ctx.clearRect(0, 0, w, h);

    // ── Grid lines ────────────────────────────────────────────────
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
    ctx.lineWidth = 0.5;
    ctx.shadowBlur = 0;
    // Horizontal reference lines at 25%, 50%, 75% amplitude
    for (let pct = 0.25; pct <= 0.75; pct += 0.25) {
        const gy = Math.round(h - pct * h) + 0.5;
        ctx.beginPath();
        ctx.moveTo(0, gy);
        ctx.lineTo(w, gy);
        ctx.stroke();
    }
    // Vertical lines at frequency tick positions
    const spanHz = SCOPE_SPAN_HZ[radioState.scope_span] || 100000;
    const vfoFreq = radioState.active_freq ||
        (radioState.active_vfo === 'B' ? radioState.vfo_b_freq : radioState.vfo_a_freq) ||
        14200000;
    const range = _computeFreqRange(vfoFreq, spanHz);
    const step = _freqStep(spanHz);
    const firstMark = Math.floor(range.leftEdge / step) * step;
    for (let f = firstMark; f <= range.rightEdge; f += step) {
        const vx = Math.round(((f - range.leftEdge) / spanHz) * w) + 0.5;
        if (vx < 0 || vx > w) continue;
        ctx.beginPath();
        ctx.moveTo(vx, 0);
        ctx.lineTo(vx, h);
        ctx.stroke();
    }

    // Shared floor/ceiling from scope display settings
    const floor = scopeFloor;
    const ceil = scopeCeil;
    const dynRange = Math.max(1, ceil - floor);

    // ── EMA smoothing: slow decay for stable FFT display ──────────
    const alpha = 0.30;  // blend 30% new data per frame
    const srcLen = wf1.length;
    if (!fftSmooth || fftSmooth.length !== srcLen) {
        // Initialize on first frame or size change
        fftSmooth = new Float32Array(wf1);
    } else {
        for (let i = 0; i < srcLen; i++) {
            fftSmooth[i] = fftSmooth[i] * (1 - alpha) + wf1[i] * alpha;
        }
    }

    // Build polyline path from smoothed data
    ctx.beginPath();
    let firstPoint = true;
    for (let x = 0; x < w; x++) {
        const srcPos = (x / w) * srcLen;
        const srcIdx = Math.floor(srcPos);
        const frac = srcPos - srcIdx;
        const v1 = fftSmooth[srcIdx] || 0;
        const v2 = (srcIdx + 1 < srcLen) ? fftSmooth[srcIdx + 1] : v1;
        const raw = v1 + (v2 - v1) * frac;

        const mapped = (raw - floor) / dynRange;
        const boost = 2.0;  // 2× amplitude boost for FFT plot
        const clamped = Math.max(0, Math.min(1, mapped * boost));
        // Invert Y: top = ceil (low y), bottom = floor (high y)
        const y = h - clamped * h;

        if (firstPoint) {
            ctx.moveTo(x + 0.5, y);
            firstPoint = false;
        } else {
            ctx.lineTo(x + 0.5, y);
        }
    }

    // Stroke with subtle glow
    ctx.strokeStyle = '#06b6d4';
    ctx.lineWidth = 1.0;
    ctx.shadowColor = 'rgba(6, 182, 212, 0.6)';
    ctx.shadowBlur = 2;
    ctx.stroke();

    // Reset shadow to avoid affecting other canvas draws
    ctx.shadowBlur = 0;
}

// ── Waterfall Rendering ─────────────────────────────────────────────
const WF_HISTORY = 120; // rows of waterfall history
let waterfallHistory = [];
let waterfallInitialized = false;

// ── Scope display settings (persisted in cookies) ──────────
let scopeFloor = parseInt(getStored('scopeFloor', '5'));
let scopeCeil = parseInt(getStored('scopeCeil', '220'));
let scopeTheme = getStored('scopeTheme', 'jet');
let fftSmooth = null;  // EMA-smoothed FFT buffer for slow decay

function getStored(key, fallback) {
    const v = FT710Settings.getCookie('ft710_' + key);
    return v !== null ? v : fallback;
}
function setStored(key, val) {
    FT710Settings.setCookie('ft710_' + key, String(val));
}

// ── Color palettes (matching wfview QCustomPlot themes) ───────────
const WF_PALETTES = {
    // Jet: black → dark blue → blue → cyan → green → yellow → red (classic)
    jet: function(v) {
        let r, g, b;
        if (v < 0.125) { let u = v / 0.125; r = 0; g = 0; b = Math.floor(128 + u * 127); }
        else if (v < 0.375) { let u = (v - 0.125) / 0.25; r = 0; g = Math.floor(u * 255); b = 255; }
        else if (v < 0.625) { let u = (v - 0.375) / 0.25; r = Math.floor(u * 255); g = 255; b = Math.floor(255 * (1 - u)); }
        else if (v < 0.875) { let u = (v - 0.625) / 0.25; r = 255; g = Math.floor(255 * (1 - u)); b = 0; }
        else { let u = (v - 0.875) / 0.125; r = Math.floor(255 * (1 - u * 0.5)); g = 0; b = 0; }
        return [r, g, b];
    },
    // Hot: black → red → orange → yellow → white
    hot: function(v) {
        let r, g, b;
        if (v < 0.33) { let u = v / 0.33; r = Math.floor(u * 255); g = 0; b = 0; }
        else if (v < 0.66) { let u = (v - 0.33) / 0.33; r = 255; g = Math.floor(u * 255); b = 0; }
        else { let u = (v - 0.66) / 0.34; r = 255; g = 255; b = Math.floor(u * 255); }
        return [r, g, b];
    },
    // Cold: black → dark blue → cyan → white
    cold: function(v) {
        let r, g, b;
        if (v < 0.5) { let u = v / 0.5; r = 0; g = Math.floor(u * 200); b = Math.floor(40 + u * 215); }
        else { let u = (v - 0.5) / 0.5; r = Math.floor(u * 255); g = Math.floor(200 + u * 55); b = 255; }
        return [r, g, b];
    },
    // Thermal: black → dark red → orange → yellow → white
    thermal: function(v) {
        let r, g, b;
        if (v < 0.25) { let u = v / 0.25; r = Math.floor(60 + u * 140); g = 0; b = 0; }
        else if (v < 0.5) { let u = (v - 0.25) / 0.25; r = Math.floor(200 + u * 55); g = Math.floor(u * 180); b = 0; }
        else if (v < 0.75) { let u = (v - 0.5) / 0.25; r = 255; g = Math.floor(180 + u * 75); b = Math.floor(u * 200); }
        else { let u = (v - 0.75) / 0.25; r = 255; g = 255; b = Math.floor(200 + u * 55); }
        return [r, g, b];
    },
    // Night: black → blue → purple → white (low-light friendly)
    night: function(v) {
        let r, g, b;
        if (v < 0.33) { let u = v / 0.33; r = 0; g = 0; b = Math.floor(u * 128); }
        else if (v < 0.66) { let u = (v - 0.33) / 0.33; r = Math.floor(u * 180); g = 0; b = Math.floor(128 + u * 127); }
        else { let u = (v - 0.66) / 0.34; r = Math.floor(180 + u * 75); g = Math.floor(u * 200); b = 255; }
        return [r, g, b];
    },
    // Gray: black → gray → white (monochrome)
    gray: function(v) {
        var val = Math.floor(v * 255);
        return [val, val, val];
    },
};

// Legacy palette (kept for reference — matches original hardcoded colors)
const WF_PALETTE_LEGACY = function(v) {
    return [
        Math.floor(v * v * 180),
        Math.floor(v * v * v * 255),
        Math.floor(5 + v * 250)
    ];
};

const SCOPE_SPAN_HZ = {
    0: 1000,
    1: 2000,
    2: 5000,
    3: 10000,
    4: 20000,
    5: 50000,
    6: 100000,
    7: 200000,
    8: 500000,
    9: 1000000,
};

function _isDesktopLayout() {
    return !!(window.matchMedia && window.matchMedia('(min-width: 768px)').matches);
}

function initWaterfall() {
    const canvas = document.getElementById('waterfall-canvas');
    if (!canvas) return;
    const rect = canvas.parentElement.getBoundingClientRect();
    const w = Math.max(100, rect.width - 8); // Minimum 100px wide
    const desktop = _isDesktopLayout();
    canvas.width = w;
    canvas.height = desktop ? 120 : 67;

    // Size FFT canvas to match width
    const fftCanvas = document.getElementById('fft-canvas');
    if (fftCanvas) {
        fftCanvas.width = w;
        fftCanvas.height = desktop ? 60 : 33;
    }

    waterfallHistory = [];
    waterfallInitialized = true;
}

function ensureWaterfallInitialized() {
    if (!waterfallInitialized) {
        initWaterfall();
    }
    // Re-check canvas size — layout may have changed
    const canvas = document.getElementById('waterfall-canvas');
    if (canvas && canvas.width < 100) {
        const rect = canvas.parentElement.getBoundingClientRect();
        if (rect.width > 100) {
            initWaterfall();
        }
    }
}

// Re-init on viewport resize / rotation (debounced)
let _wfResizeTimer = null;
window.addEventListener('resize', function() {
    clearTimeout(_wfResizeTimer);
    _wfResizeTimer = setTimeout(function() {
        const canvas = document.getElementById('waterfall-canvas');
        if (!canvas || !waterfallInitialized) return;
        const rect = canvas.parentElement.getBoundingClientRect();
        const targetW = Math.max(100, rect.width - 8);
        const targetH = _isDesktopLayout() ? 120 : 67;
        if (Math.abs(canvas.width - targetW) > 4 || canvas.height !== targetH) {
            initWaterfall();
        }
    }, 250);
});

function renderWaterfallRow(wf1) {
    ensureWaterfallInitialized();

    // Draw FFT spectrum line plot above waterfall
    renderFFTPlot(wf1);

    const canvas = document.getElementById('waterfall-canvas');
    if (!canvas) return;
    if (canvas.width < 100 || canvas.height < 10) return;

    const ctx = canvas.getContext('2d');
    const w = canvas.width;
    const h = canvas.height;

    // Radio disconnected: the fallback spectrum is a flat ~zero floor which
    // renders as a black void that looks like a UI failure. Say so instead.
    // Only show the red banner once the radio WS is up and confirms the
    // radio is gone — during page load / WS reconnect the radio WS has not
    // delivered fullState yet and serial_connected is still its initial
    // false, which would otherwise flash a false "电台未连接" (2026-08-15
    // field report: banner flashed on channel switch / tune while healthy).
    if (radioState.ws_connected && !radioState.serial_connected) {
        console.warn('[spectrum] radio WS up but serial_connected=false',
            JSON.stringify({ ws: radioState.ws_connected, serial: radioState.serial_connected }));
        ctx.clearRect(0, 0, w, h);
        ctx.fillStyle = 'rgba(239, 68, 68, 0.85)';
        ctx.font = 'bold 13px monospace';
        ctx.textAlign = 'center';
        ctx.fillText('电台未连接 — 检查 USB / 电源', w / 2, h / 2 + 4);
        const fftCanvas = document.getElementById('fft-canvas');
        if (fftCanvas) {
            const fctx = fftCanvas.getContext('2d');
            fctx.clearRect(0, 0, fftCanvas.width, fftCanvas.height);
        }
        return;
    }
    // wsRadio not up yet (page load / reconnect): neutral placeholder, not
    // an error banner — the spectrum WS often connects before fullState.
    if (!radioState.ws_connected) {
        ctx.clearRect(0, 0, w, h);
        ctx.fillStyle = 'rgba(148, 163, 184, 0.7)';
        ctx.font = 'bold 13px monospace';
        ctx.textAlign = 'center';
        ctx.fillText('频谱连接中…', w / 2, h / 2 + 4);
        return;
    }

    // Transmitting: the FT-710 garbles its scope stream during TX (the
    // server pauses SPI reads). Show a notice instead of stale data.
    if (radioState.tx_status !== 0) {
        ctx.clearRect(0, 0, w, h);
        ctx.fillStyle = 'rgba(239, 68, 68, 0.85)';
        ctx.font = 'bold 13px monospace';
        ctx.textAlign = 'center';
        ctx.fillText('TX 发射中 — 频谱暂停', w / 2, h / 2 + 4);
        const fftCanvas = document.getElementById('fft-canvas');
        if (fftCanvas) {
            const fctx = fftCanvas.getContext('2d');
            fctx.clearRect(0, 0, fftCanvas.width, fftCanvas.height);
        }
        return;
    }

    // Scroll canvas content up by 1px
    ctx.drawImage(canvas, 0, 1, w, h - 1, 0, 0, w, h - 1);

    // ── Color palette & floor/ceiling ────────────────────────────
    const palette = WF_PALETTES[scopeTheme] || WF_PALETTES.jet;
    const floor = scopeFloor;
    const ceil = scopeCeil;
    const dynRange = Math.max(1, ceil - floor);

    // Draw new row at the bottom (1px high)
    const srcLen = wf1.length;
    for (let x = 0; x < w; x++) {
        // Linear interpolation from source data to canvas width
        const srcPos = (x / w) * srcLen;
        const srcIdx = Math.floor(srcPos);
        const frac = srcPos - srcIdx;
        const v1 = wf1[srcIdx] || 0;
        const v2 = (srcIdx + 1 < srcLen) ? wf1[srcIdx + 1] : v1;
        const raw = v1 + (v2 - v1) * frac;

        // Apply floor/ceiling mapping: remap [floor, ceil] → [0, 1]
        const mapped = (raw - floor) / dynRange;
        const v = Math.max(0, Math.min(1, mapped));

        // Apply color palette
        const [r, g, b] = palette(v);
        ctx.fillStyle = 'rgb(' + r + ',' + g + ',' + b + ')';
        ctx.fillRect(x, h - 1, 1, 1);
    }

    // VFO red line — the server always runs CENTER mode (EX040200),
    // so the VFO frequency is the scope centre.  Compute everything
    // from the VFO and span alone — no dependency on the scope frame's
    // scope_start_freq (which lags behind CAT after tuning).
    const spanHz = SCOPE_SPAN_HZ[radioState.scope_span] || 100000;
    const vfoFreq = radioState.active_freq ||
        (radioState.active_vfo === 'B' ? radioState.vfo_b_freq : radioState.vfo_a_freq) ||
        14200000;
    const range = _computeFreqRange(vfoFreq, spanHz);

    // VFO is always at centre in CENTER mode → half the canvas width.
    const vfoX = w / 2;
    if (vfoX >= 0 && vfoX <= w) {
        const vx = Math.round(vfoX) + 0.5;
        ctx.strokeStyle = 'rgba(239, 68, 68, 0.45)';
        ctx.lineWidth = 0.2;
        ctx.beginPath();
        ctx.moveTo(vx, 0);
        ctx.lineTo(vx, h);
        ctx.stroke();
    }

    // Update frequency scale
    renderFreqScale(w, range);
}

function renderFreqScale(canvasWidth, range) {
    const scaleCanvas = document.getElementById('freq-scale-canvas');
    if (!scaleCanvas) return;
    scaleCanvas.width = canvasWidth;
    const ctx = scaleCanvas.getContext('2d');
    const w = scaleCanvas.width;
    ctx.fillStyle = '#1a1a1a';
    ctx.fillRect(0, 0, w, 20);

    const spanHz = range.rightEdge - range.leftEdge;
    const vfoFreq = range.centerFreq;
    const startFreq = range.leftEdge;

    // Adaptive step size — roughly 8-12 marks across the canvas width.
    const step = _freqStep(spanHz);
    const firstMark = Math.floor(startFreq / step) * step;

    // Tick marks and labels
    ctx.font = '9px monospace';
    for (let f = firstMark; f <= startFreq + spanHz; f += step) {
        const x = ((f - startFreq) / spanHz) * w;
        if (x < 0 || x > w) continue;

        // Tick line
        ctx.strokeStyle = '#444';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(Math.round(x) + 0.5, 0);
        ctx.lineTo(Math.round(x) + 0.5, 4);
        ctx.stroke();

        // Label
        ctx.fillStyle = '#999';
        ctx.textAlign = 'center';
        ctx.fillText(_formatFreqLabel(f, step), Math.round(x), 15);
    }

    // VFO marker on the scale (red triangle + label)
    const vfoX = ((vfoFreq - startFreq) / spanHz) * w;
    if (vfoX >= 0 && vfoX <= w) {
        const vx = Math.round(vfoX);
        // Red marker triangle
        ctx.fillStyle = '#ef4444';
        ctx.beginPath();
        ctx.moveTo(vx, 0);
        ctx.lineTo(vx - 4, 6);
        ctx.lineTo(vx + 4, 6);
        ctx.closePath();
        ctx.fill();
        // Label above scale
        ctx.fillStyle = '#ef4444';
        ctx.font = 'bold 9px monospace';
        ctx.textAlign = 'center';
        ctx.fillText((vfoFreq / 1e6).toFixed(3), vx, 20);
    }
}

// ── Frequency scale helpers ──────────────────────────────────────────

function _freqStep(spanHz) {
    if (spanHz <= 2000)       return 200;
    else if (spanHz <= 5000)  return 500;
    else if (spanHz <= 10000)  return 1000;
    else if (spanHz <= 25000)  return 2500;
    else if (spanHz <= 50000)  return 5000;
    else if (spanHz <= 100000) return 10000;
    else if (spanHz <= 200000) return 25000;
    else if (spanHz <= 500000) return 50000;
    else                       return 100000;
}

function _formatFreqLabel(freqHz, step) {
    if (step < 1000) {
        // 200/500 Hz steps — kHz with one decimal
        return (freqHz / 1000).toFixed(1) + 'k';
    } else if (step <= 5000) {
        // 1–5 kHz steps — integer kHz
        return (freqHz / 1000).toFixed(0) + 'k';
    } else if (step <= 25000) {
        // 10–25 kHz steps — MHz with two decimals
        return (freqHz / 1e6).toFixed(2);
    } else {
        // 50–100 kHz steps — MHz with three decimals
        return (freqHz / 1e6).toFixed(3);
    }
}

// ── Frequency range helper (VFO‑centred) ─────────────────────────
//
// The server always initialises the scope to CENTER mode (EX040200).
// In CENTER mode the scope display is centred on VFO‑A, so the VFO
// frequency *is* the scope centre.  Computing everything from the VFO
// and span alone is simpler and more accurate than relying on the
// scope frame's scope_start_freq, which arrives on a different async
// path and lags behind the CAT‑polled VFO after tuning.
function _computeFreqRange(vfoFreq, spanHz) {
    const halfSpan = spanHz / 2;
    return {
        leftEdge:  vfoFreq - halfSpan,
        centerFreq: vfoFreq,
        rightEdge: vfoFreq + halfSpan,
    };
}

function renderScopeSettings() {
    const spanSelect = document.getElementById('scope-span-select');
    if (spanSelect) spanSelect.value = String(radioState.scope_span);
    const speedSelect = document.getElementById('scope-speed-select');
    if (speedSelect) speedSelect.value = String(radioState.scope_speed);
    const themeSelect = document.getElementById('scope-theme-select');
    if (themeSelect) themeSelect.value = scopeTheme;
    const floorSlider = document.getElementById('slider-floor');
    const floorVal = document.getElementById('val-floor');
    if (floorSlider) floorSlider.value = scopeFloor;
    if (floorVal) floorVal.textContent = scopeFloor;
    const ceilSlider = document.getElementById('slider-ceil');
    const ceilVal = document.getElementById('val-ceil');
    if (ceilSlider) ceilSlider.value = scopeCeil;
    if (ceilVal) ceilVal.textContent = scopeCeil;
    const canvas = document.getElementById('waterfall-canvas');
    if (canvas && canvas.width > 0) {
        // Rebuild the VFO-centred range — renderFreqScale requires it
        // (previously called without it, throwing on every update cycle).
        const spanHz = SCOPE_SPAN_HZ[radioState.scope_span] || 100000;
        const vfoFreq = radioState.active_freq ||
            (radioState.active_vfo === 'B' ? radioState.vfo_b_freq : radioState.vfo_a_freq) ||
            14200000;
        renderFreqScale(canvas.width, _computeFreqRange(vfoFreq, spanHz));
    }
}

// ── Render All ──────────────────────────────────────────────────────
function renderAll() {
    initWaterfall();
    renderFrequency();
    renderSMeter();
    renderMeters();
    renderStatusBar();
    renderButtonLabels();
    renderToggles();
    renderSliders();
    renderScopeSettings();
    renderVFOButtons();
    renderMemoryChannels();
    renderPTTState();
}

// ── Render Updates (partial, from dirty field list) ─────────────────
function renderUpdates(dirtyFields) {
    const freqFields = ['vfo_a_freq', 'vfo_b_freq', 'active_vfo'];
    const meterFields = ['s_meter', 's_meter_dbm', 's_unit'];
    const txMeters = ['power_meter', 'alc_meter', 'swr_meter', 'id_meter', 'vd_meter'];
    const settingsFields = [
        'mode', 'mode_name', 'mode_display', 'band_name', 'tx_status',
        'is_transmitting', 'filter_width', 'filter_hz', 'preamp', 'preamp_label',
        'attenuator', 'attenuator_label', 'noise_blanker', 'noise_reduction',
        'auto_notch', 'compressor', 'tuner_status', 'rf_power',
        'split', 'serial_connected', 'rx_audio_silent', 'scope_span', 'scope_speed', 'scope_mode',
        'scope_start_freq',
    ];

    let needFreq = false, needSMeter = false, needMeters = false;
    let needStatus = false, needButtons = false, needToggles = false;
    let needSliders = false, needVFO = false, needPTT = false, needScope = false;

    for (const f of dirtyFields) {
        if (freqFields.includes(f)) needFreq = true;
        if (meterFields.includes(f)) needSMeter = true;
        if (txMeters.includes(f)) needMeters = true;
        if (settingsFields.includes(f)) {
            if (['mode','mode_name','mode_display'].includes(f)) needButtons = true;
            if (['band_name'].includes(f)) needButtons = needStatus = true;
            if (['tx_status','is_transmitting'].includes(f)) needStatus = needPTT = true;
            if (['filter_width','filter_hz','preamp','preamp_label','attenuator','attenuator_label'].includes(f)) needButtons = true;
            if (['noise_blanker','noise_reduction','auto_notch','compressor','tuner_status'].includes(f)) needToggles = true;
            if (['rf_power', 'rf_gain'].includes(f)) needSliders = true;
            if (['scope_span','scope_speed','scope_mode','scope_start_freq'].includes(f)) needScope = true;
            if (['split'].includes(f)) needVFO = true;
            if (['serial_connected'].includes(f)) needStatus = true;
            needStatus = true; // Most settings affect status bar
            needButtons = true; // Most settings affect button labels
        }
    }

    if (needFreq) renderFrequency();
    if (needSMeter) renderSMeter();
    if (needMeters) renderMeters();
    if (needStatus) renderStatusBar();
    if (needButtons) renderButtonLabels();
    if (needToggles) renderToggles();
    if (needSliders) renderSliders();
    if (needScope || needFreq) renderScopeSettings();
    if (needVFO || needFreq) renderVFOButtons();
    if (needPTT) renderPTTState();
}

function renderField(field) {
    renderUpdates([field]);
}

// ── PTT Handlers (called from ptt_manager.js) ───────────────────────
function handlePTTStart() {
    sendCommand('ptt', true);
    radioState.tx_status = 1;
    radioState.is_transmitting = true;
    renderPTTState();
    renderStatusBar();
    // Start TX audio capture
    if (typeof startTXAudio === 'function') startTXAudio();
}

function handlePTTEnd() {
    sendCommand('ptt', false);
    radioState.tx_status = 0;
    radioState.is_transmitting = false;
    renderPTTState();
    renderStatusBar();
    // Stop TX audio capture
    if (typeof stopTXAudio === 'function') stopTXAudio();
}

function handleTuneStart() {
    sendCommand('tune', true);
    radioState.tx_status = 2;
    renderPTTState();
    renderStatusBar();
}

function handleTuneEnd() {
    sendCommand('tune', false);
    radioState.tx_status = 0;
    renderPTTState();
    renderStatusBar();
}

// ── Event Wiring ────────────────────────────────────────────────────
function initUI() {
    // Mode button: cycles to next mode
    document.getElementById('btn-mode').addEventListener('click', function() {
        const nextMode = getNextMode(radioState.mode_name);
        sendCommand('mode', nextMode);
        radioState.mode_name = nextMode;
        renderButtonLabels();
        renderStatusBar();
    });

    // Band button: cycles to next band
    document.getElementById('btn-band').addEventListener('click', function() {
        const nextBand = getNextBand(radioState.band_name);
        if (nextBand) {
            console.log('[Band] click', {
                current: radioState.band_name,
                next: nextBand.name,
                bsr: nextBand.bsr,
                default_freq: nextBand.default_freq,
                bands: bands.map(b => b.name),
            });
            sendCommand('band', nextBand.name);
            radioState.band_name = nextBand.name;
            radioState.active_vfo = 'A';
            radioState.vfo_a_freq = nextBand.default_freq;
            renderFrequency();
            renderButtonLabels();
            renderStatusBar();
        }
    });

    // Filter button: cycles filter width
    document.getElementById('btn-filter').addEventListener('click', function() {
        const nextIdx = getNextFilter(radioState.filter_width, radioState.mode_name);
        sendCommand('filter', nextIdx);
        radioState.filter_width = nextIdx;
        renderButtonLabels();
    });

    // ATT button: cycles attenuator
    document.getElementById('btn-att').addEventListener('click', function() {
        const nextAtt = (radioState.attenuator + 1) % 4;
        sendCommand('att', nextAtt);
        radioState.attenuator = nextAtt;
        renderButtonLabels();
    });

    // PRE button: cycles preamp
    document.getElementById('btn-pre').addEventListener('click', function() {
        const nextPre = (radioState.preamp + 1) % 3;
        sendCommand('preamp', nextPre);
        radioState.preamp = nextPre;
        renderButtonLabels();
    });

    // Tuning buttons — resolve step dynamically from currentTuneStep
    document.querySelectorAll('.tune-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const action = this.dataset.action;
            let delta = 0;
            if (action === 'slow-left')  delta = -currentTuneStep;
            if (action === 'slow-right') delta = currentTuneStep;
            if (action === 'fast-left')  delta = -(currentTuneStep * 5);
            if (action === 'fast-right') delta = currentTuneStep * 5;
            if (delta !== 0) tuneBy(delta);
        });
    });

    // Step button — cycle through preset step sizes
    document.getElementById('btn-step').addEventListener('click', function() {
        const idx = tuneSteps.indexOf(currentTuneStep);
        const nextIdx = (idx + 1) % tuneSteps.length;
        currentTuneStep = tuneSteps[nextIdx];
        const labels = {10:'10Hz', 100:'100Hz', 1000:'1kHz', 5000:'5kHz', 10000:'10kHz', 25000:'25kHz'};
        const stepLabel = labels[currentTuneStep] || currentTuneStep + 'Hz';
        this.textContent = stepLabel;
        // Update tune button tooltips so the user knows what each does
        document.querySelectorAll('.tune-btn').forEach(btn => {
            const action = btn.dataset.action;
            if (action === 'slow-left' || action === 'slow-right') {
                btn.title = 'Step ' + stepLabel;
            } else if (action === 'fast-left' || action === 'fast-right') {
                btn.title = 'Step ' + (currentTuneStep * 5 / 1000) + 'kHz';
            }
        });
    });

    // DSP toggle buttons — click to toggle on/off
    function wireDspBtn(id, field, cmd, onVal, offVal) {
        const btn = document.getElementById(id);
        if (!btn) return;
        btn.addEventListener('click', function() {
            const cur = radioState[field];
            const next = !cur;
            const val = next ? (onVal !== undefined ? onVal : true) : (offVal !== undefined ? offVal : false);
            sendCommand(cmd, val);
            radioState[field] = next ? (onVal !== undefined ? onVal : true) : (offVal !== undefined ? offVal : false);
            btn.classList.toggle('on', next);
        });
    }
    wireDspBtn('dsp-nr',  'noise_reduction', 'nr',    true, false);
    wireDspBtn('dsp-nb',  'noise_blanker',   'nb',    true, false);
    wireDspBtn('dsp-an',  'auto_notch',      'an',    true, false);
    wireDspBtn('dsp-comp','compressor',       'comp',  true, false);
    wireDspBtn('dsp-atu', 'tuner_status',     'tuner', 1,    0);

    // Scope span selector
    document.getElementById('scope-span-select').addEventListener('change', function() {
        const v = parseInt(this.value);
        sendCommand('scope_span', v);
        radioState.scope_span = v;
        renderScopeSettings();
    });
    // Scope speed selector
    document.getElementById('scope-speed-select').addEventListener('change', function() {
        const v = parseInt(this.value);
        sendCommand('scope_speed', v);
        radioState.scope_speed = v;
        renderScopeSettings();
    });

    // Scope color theme selector
    const themeSelect = document.getElementById('scope-theme-select');
    themeSelect.value = scopeTheme;  // restore stored preference
    themeSelect.addEventListener('change', function() {
        scopeTheme = this.value;
        setStored('scopeTheme', scopeTheme);
    });

    // Floor slider: adjusts the noise-floor cutoff for the waterfall
    const floorSlider = document.getElementById('slider-floor');
    const floorVal = document.getElementById('val-floor');
    floorSlider.value = scopeFloor;
    floorVal.textContent = scopeFloor;
    floorSlider.addEventListener('input', function() {
        floorVal.textContent = this.value;
    });
    floorSlider.addEventListener('change', function() {
        scopeFloor = parseInt(this.value);
        setStored('scopeFloor', scopeFloor);
    });

    // Ceil slider: adjusts the signal ceiling for the waterfall
    const ceilSlider = document.getElementById('slider-ceil');
    const ceilVal = document.getElementById('val-ceil');
    ceilSlider.value = scopeCeil;
    ceilVal.textContent = scopeCeil;
    ceilSlider.addEventListener('input', function() {
        ceilVal.textContent = this.value;
    });
    ceilSlider.addEventListener('change', function() {
        scopeCeil = parseInt(this.value);
        setStored('scopeCeil', scopeCeil);
    });

    // NR/NB level sliders
    const nrSlider = document.getElementById('slider-nrlevel');
    if (nrSlider) {
        nrSlider.addEventListener('input', function() { setText('val-nrlevel', this.value); });
        nrSlider.addEventListener('change', function() { sendCommand('nr_level', parseInt(this.value)); });
    }
    const nbSlider = document.getElementById('slider-nblevel');
    if (nbSlider) {
        nbSlider.addEventListener('input', function() { setText('val-nblevel', this.value); });
        nbSlider.addEventListener('change', function() { sendCommand('nb_level', parseInt(this.value)); });
    }

    // Sliders
    document.getElementById('slider-rfpower').addEventListener('input', function() {
        setText('val-rfpower', this.value);
    });
    document.getElementById('slider-rfpower').addEventListener('change', function() {
        const v = parseInt(this.value);
        sendCommand('rf_power', v);
        radioState.rf_power = v;
    });

    // RF Gain slider (0-100% <-> RG 0-255)
    const rgSlider = document.getElementById('slider-rfgain');
    if (rgSlider) {
        rgSlider.addEventListener('input', function() { setText('val-rfgain', this.value); });
        rgSlider.addEventListener('change', function() {
            const pct = parseInt(this.value);
            const raw = Math.round(pct * 255 / 100);
            sendCommand('rf_gain', raw);
            radioState.rf_gain = raw;
        });
    }
    // Restore persisted browser volume before wiring
    (function() {
        var v = 128;
        try { var s = FT710Settings.getCookie('ft710_afVol'); if (s !== null) v = parseInt(s); } catch(e) {}
        if (isNaN(v)) v = 128;
        var sl = document.getElementById('slider-afgain');
        if (sl) sl.value = v;
        setText('val-afgain', v);
    })();
    document.getElementById('slider-afgain').addEventListener('input', function() {
        setText('val-afgain', this.value);
        FT710Settings.setCookie('ft710_afVol', this.value);
        // Route through the shared helper so the TX dim factor is respected
        // even if the operator nudges volume while keyed.
        if (typeof _applyAfGainToAudioNode === 'function') _applyAfGainToAudioNode();
    });
    const micSlider = document.getElementById('slider-micgain');
    if (micSlider) {
        micSlider.addEventListener('input', function() { setText('val-micgain', this.value); });
        micSlider.addEventListener('change', function() {
            const v = parseInt(this.value);
            sendCommand('mic_gain', v);
            radioState.mic_gain = v;
            // Persist locally; re-applied to the radio on every fullState
            FT710Settings.setCookie('ft710_micGain', String(v));
        });
    }
    // 🎙 Vol: device-side software mic gain (browser-local, like 🔊 Vol).
    // 0–200 → linear 0–2×, 100 = unity. Persisted in a cookie.
    (function() {
        var v = 100;
        try { var s = FT710Settings.getCookie('ft710_micVol'); if (s !== null) v = parseInt(s); } catch(e) {}
        if (isNaN(v)) v = 100;
        v = Math.max(0, Math.min(200, v));
        var sl = document.getElementById('slider-micvol');
        if (sl) { sl.value = v; setText('val-micvol', v); }
    })();
    const micVolSlider = document.getElementById('slider-micvol');
    if (micVolSlider) {
        micVolSlider.addEventListener('input', function() {
            setText('val-micvol', this.value);
            FT710Settings.setCookie('ft710_micVol', this.value);
            if (typeof _setDeviceMicGain === 'function') _setDeviceMicGain(parseInt(this.value) / 100);
        });
    }

    // VFO buttons
    document.getElementById('btn-vfoa').addEventListener('click', function() {
        if (radioState.active_vfo !== 'A') {
            sendCommand('vfo', 'A');
            radioState.active_vfo = 'A';
            renderVFOButtons();
            renderFrequency();
        }
    });
    document.getElementById('btn-vfob').addEventListener('click', function() {
        if (radioState.active_vfo !== 'B') {
            sendCommand('vfo', 'B');
            radioState.active_vfo = 'B';
            renderVFOButtons();
            renderFrequency();
        }
    });
    document.getElementById('btn-ab').addEventListener('click', function() {
        sendCommand('vfo_equal', true);
        radioState.vfo_a_freq = radioState.vfo_b_freq;
        renderFrequency();
    });
    document.getElementById('btn-split').addEventListener('click', function() {
        const newSplit = !radioState.split;
        sendCommand('split', newSplit);
        radioState.split = newSplit;
        renderVFOButtons();
    });

    // PTT button — routed through PTTManager so the safety watchdog and
    // pagehide force-RX stay armed (previously bypassed = dead watchdog).
    const pttBtn = document.getElementById('btn-ptt');
    const pttStart = function() { if (window.PTTManager) PTTManager.pttStart(); else handlePTTStart(); };
    const pttEnd = function() { if (window.PTTManager) PTTManager.pttEnd(); else handlePTTEnd(); };
    pttBtn.addEventListener('mousedown', pttStart);
    pttBtn.addEventListener('touchstart', function(e) { e.preventDefault(); pttStart(); });
    pttBtn.addEventListener('mouseup', pttEnd);
    pttBtn.addEventListener('touchend', function(e) { e.preventDefault(); pttEnd(); });
    pttBtn.addEventListener('mouseleave', pttEnd);
    pttBtn.addEventListener('touchcancel', pttEnd);

    // TUNE button — press-and-HOLD (carrier only while held). The previous
    // latch-on-click design could key the radio from a single accidental tap.
    const tuneBtn = document.getElementById('btn-tune');
    const tuneStart = function() { if (window.PTTManager) PTTManager.tuneStart(); else handleTuneStart(); };
    const tuneEnd = function() { if (window.PTTManager) PTTManager.tuneEnd(); else handleTuneEnd(); };
    tuneBtn.title = '按住发射调谐载波，松开即停';
    tuneBtn.addEventListener('mousedown', tuneStart);
    tuneBtn.addEventListener('touchstart', function(e) { e.preventDefault(); tuneStart(); });
    tuneBtn.addEventListener('mouseup', tuneEnd);
    tuneBtn.addEventListener('touchend', function(e) { e.preventDefault(); tuneEnd(); });
    tuneBtn.addEventListener('mouseleave', tuneEnd);
    tuneBtn.addEventListener('touchcancel', tuneEnd);

    // RX recording button
    const recordBtn = document.getElementById('btn-record');
    if (recordBtn) {
        recordBtn.addEventListener('click', async function() {
            if (window.RXRecorder) {
                const ok = await window.RXRecorder.toggle();
                if (ok === false && !window.RXRecorder.isActive && typeof showToast === 'function') {
                    showToast('MP3 编码器加载失败，无法录音');
                }
            }
        });
        renderRecordingState();
    }

    // Memory buttons
    document.querySelectorAll('.mem-btn').forEach(btn => {
        let pressTimer;
        let longPressHandled = false;
        const idx = parseInt(btn.dataset.mem);

        btn.addEventListener('click', function() {
            if (longPressHandled) {
                longPressHandled = false;
                return;
            }
            // Tap: recall
            const ch = memChannels[idx];
            if (ch && ch.freq) {
                sendMsg({
                    type: 'memRecall',
                    freq: ch.freq,
                    mode: ch.mode || undefined,
                });
                if (radioState.active_vfo === 'B') {
                    radioState.vfo_b_freq = ch.freq;
                } else {
                    radioState.vfo_a_freq = ch.freq;
                }
                if (ch.mode) {
                    radioState.mode_name = ch.mode;
                }
                renderFrequency();
                renderVFOButtons();
            }
        });

        function saveMemoryChannel() {
            longPressHandled = true;
            var saveFreq = radioState.active_vfo === 'A' ? radioState.vfo_a_freq : radioState.vfo_b_freq;
            const ch = {
                freq: saveFreq,
                mode: radioState.mode_name,
                label: radioState.band_name + ' ' + (saveFreq / 1e6).toFixed(3),
            };
            memChannels[idx] = ch;
            // Persist locally so channels survive page refresh
            FT710Settings.setCookie('ft710_memChannels', JSON.stringify(memChannels));
            sendMsg({
                type: 'memSave',
                channels: memChannels,
            });
            renderMemoryChannels();
        }

        // Long press: save (uses active VFO)
        btn.addEventListener('touchstart', function(e) {
            pressTimer = setTimeout(function() {
                saveMemoryChannel();
                hapticFeedback('medium');
            }, 800);
        });
        btn.addEventListener('touchend', function() { clearTimeout(pressTimer); });
        btn.addEventListener('touchcancel', function() { clearTimeout(pressTimer); });
        // Desktop long press (uses active VFO)
        btn.addEventListener('mousedown', function(e) {
            pressTimer = setTimeout(function() {
                saveMemoryChannel();
            }, 800);
        });
        btn.addEventListener('mouseup', function() { clearTimeout(pressTimer); });
        btn.addEventListener('mouseleave', function() { clearTimeout(pressTimer); });
    });

    // Waterfall / FFT click-to-tune (QSY). Click maps the x position to a
    // frequency inside the current span; sub-8px movement counts as a click,
    // larger drags are treated as scroll gestures and ignored.
    function wireScopeQSY(canvasId) {
        const cv = document.getElementById(canvasId);
        if (!cv) return;
        let downX = null;
        function qsy(clientX) {
            const rect = cv.getBoundingClientRect();
            const x = clientX - rect.left;
            const spanHz = SCOPE_SPAN_HZ[radioState.scope_span] || 100000;
            const vfoFreq = radioState.active_freq ||
                (radioState.active_vfo === 'B' ? radioState.vfo_b_freq : radioState.vfo_a_freq) ||
                14200000;
            const f = Math.max(30000, Math.min(75000000,
                Math.round(vfoFreq - spanHz / 2 + (x / rect.width) * spanHz)));
            const field = radioState.active_vfo === 'A' ? 'freq' : 'vfo_b_freq';
            sendCommand(field, f);
            if (radioState.active_vfo === 'A') radioState.vfo_a_freq = f;
            else radioState.vfo_b_freq = f;
            renderFrequency();
        }
        cv.addEventListener('mousedown', function(e) { downX = e.clientX; });
        cv.addEventListener('mouseup', function(e) {
            if (downX === null) return;
            const dx = Math.abs(e.clientX - downX);
            downX = null;
            if (dx <= 8) qsy(e.clientX);
        });
        cv.addEventListener('touchstart', function(e) { downX = e.touches[0].clientX; }, {passive: true});
        cv.addEventListener('touchend', function(e) {
            if (downX === null) return;
            const x = e.changedTouches[0].clientX;
            const dx = Math.abs(x - downX);
            downX = null;
            if (dx <= 8) qsy(x);
        });
        cv.title = '点击频谱直接 QSY 到对应频率';
        cv.style.cursor = 'crosshair';
    }
    wireScopeQSY('waterfall-canvas');
    wireScopeQSY('fft-canvas');

    // Menu
    document.getElementById('menu-toggle').addEventListener('click', function() {
        document.getElementById('main-menu').classList.add('open');
        document.getElementById('menu-overlay').classList.add('open');
    });
    document.getElementById('menu-close').addEventListener('click', closeMenu);
    document.getElementById('menu-overlay').addEventListener('click', closeMenu);
    document.querySelectorAll('.menu-item[data-action]').forEach(item => {
        item.addEventListener('click', function(e) {
            e.preventDefault();
            const action = this.dataset.action;
            handleMenuAction(action);
            closeMenu();
        });
    });
}

function closeMenu() {
    document.getElementById('main-menu').classList.remove('open');
    document.getElementById('menu-overlay').classList.remove('open');
}

function handleMenuAction(action) {
    switch (action) {
        case 'band-select':
            showBandSelector();
            break;
        case 'mode-select':
            showModeSelector();
            break;
        case 'memory-manage':
            showMemoryManager();
            break;
        case 'settings':
            // Scroll to DSP panel
            const dspPanel = document.querySelector('.dsp-panel');
            if (dspPanel) dspPanel.scrollIntoView({behavior:'smooth'});
            break;
        case 'logout':
            fetch('/api/auth/logout', {method:'POST'}).then(function() {
                window.location.replace('/login');
            });
            break;
    }
}

// ── Modal Dialogs ───────────────────────────────────────────────────
function showModal(title, items, onSelect, currentValue) {
    // Remove any existing modal
    const existing = document.querySelector('.modal-overlay');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';

    const content = document.createElement('div');
    content.className = 'modal-content';

    const titleEl = document.createElement('div');
    titleEl.className = 'modal-title';
    titleEl.textContent = title;

    const grid = document.createElement('div');
    grid.className = 'modal-grid';

    items.forEach(function(item) {
        const btn = document.createElement('button');
        btn.className = 'modal-btn';
        btn.textContent = item.label || item.name || item;
        if (item === currentValue || item.name === currentValue) {
            btn.classList.add('selected');
        }
        btn.addEventListener('click', function() {
            onSelect(item);
            overlay.remove();
        });
        grid.appendChild(btn);
    });

    const closeBtn = document.createElement('button');
    closeBtn.className = 'modal-close';
    closeBtn.textContent = 'Cancel';
    closeBtn.addEventListener('click', function() { overlay.remove(); });

    content.appendChild(titleEl);
    content.appendChild(grid);
    content.appendChild(closeBtn);
    overlay.appendChild(content);
    document.body.appendChild(overlay);

    overlay.addEventListener('click', function(e) {
        if (e.target === overlay) overlay.remove();
    });
}

function showBandSelector() {
    const items = bands.map(function(b) { return {name: b.name, label: b.name + ' (' + (b.start/1e6).toFixed(1) + '-' + (b.end/1e6).toFixed(1) + ' MHz)'}; });
    showModal('Select Band', items, function(band) {
        sendCommand('band', band.name);
        radioState.band_name = band.name;
        if (band.default_freq) {
            radioState.vfo_a_freq = band.default_freq;
            renderFrequency();
        }
        renderButtonLabels();
        renderStatusBar();
    }, radioState.band_name);
}

function showModeSelector() {
    const items = uiModes.map(function(m) { return {name: m, label: m}; });
    showModal('Select Mode', items, function(mode) {
        sendCommand('mode', mode.name);
        radioState.mode_name = mode.name;
        radioState.mode = (mode.name === 'LSB' ? 1 : mode.name === 'USB' ? 2 : mode.name === 'CW-U' ? 3 : mode.name === 'AM' ? 5 : mode.name === 'FM' ? 4 : mode.name === 'RTTY-L' ? 6 : mode.name === 'DATA-L' ? 8 : 1);
        renderButtonLabels();
        renderStatusBar();
    }, radioState.mode_name);
}

function showMemoryManager() {
    let html = '<div class="modal-title">Memory Manager</div>';
    for (let i = 0; i < 6; i++) {
        const ch = memChannels[i];
        const freqStr = ch ? (ch.freq / 1e6).toFixed(3) + ' MHz' : 'Empty';
        const label = ch ? (ch.label || '') : '';
        html += '<div style="display:flex;justify-content:space-between;align-items:center;padding:8px;border-bottom:1px solid #444;">';
        html += '<span style="font-weight:700;color:#f59e0b;">M' + (i+1) + '</span>';
        html += '<span>' + freqStr + '</span>';
        html += '<span style="font-size:11px;color:#999;">' + label + '</span>';
        html += '<button data-clear="' + i + '" style="background:#ef4444;color:#fff;border:none;border-radius:4px;padding:4px 8px;font-size:11px;">Clear</button>';
        html += '</div>';
    }
    html += '<button class="modal-close" id="mem-close">Close</button>';

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    const content = document.createElement('div');
    content.className = 'modal-content';
    content.innerHTML = html;
    overlay.appendChild(content);
    document.body.appendChild(overlay);

    content.querySelectorAll('[data-clear]').forEach(btn => {
        btn.addEventListener('click', function() {
            const idx = parseInt(this.dataset.clear);
            memChannels[idx] = null;
            // Persist locally so channels survive page refresh
            FT710Settings.setCookie('ft710_memChannels', JSON.stringify(memChannels));
            sendMsg({type: 'memSave', channels: memChannels});
            renderMemoryChannels();
            overlay.remove();
        });
    });
    document.getElementById('mem-close').addEventListener('click', function() { overlay.remove(); });
    overlay.addEventListener('click', function(e) { if (e.target === overlay) overlay.remove(); });
}

// ── Haptic Feedback ─────────────────────────────────────────────────
function hapticFeedback(pattern) {
    if ('vibrate' in navigator) {
        if (pattern === 'medium') navigator.vibrate(15);
        else navigator.vibrate(8);
    }
}

// ── Error toast ─────────────────────────────────────────────────────
// Server 'error' messages (e.g. "Radio not connected", "Band change
// failed") used to go to the console only — surface them to the operator.
function showToast(message, durationMs) {
    let toast = document.getElementById('ft710-toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'ft710-toast';
        document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(toast._hideTimer);
    toast._hideTimer = setTimeout(function() { toast.classList.remove('show'); }, durationMs || 3500);
}

// ── Frequency input (click-to-edit) ─────────────────────────────────
function initFreqInput() {
    const display = document.getElementById('freq-display');
    const input = document.getElementById('freq-input');
    if (!display || !input) return;

    display.addEventListener('click', function() {
        const freq = radioState.active_vfo === 'A' ? radioState.vfo_a_freq : radioState.vfo_b_freq;
        // Show MHz with kHz precision
        input.value = (freq / 1e6).toFixed(3);
        display.querySelectorAll('span').forEach(s => s.style.display = 'none');
        input.style.display = '';
        input.focus();
        input.select();
    });

    function hideInput() {
        input.style.display = 'none';
        display.querySelectorAll('span').forEach(s => s.style.display = '');
    }

    input.addEventListener('blur', function() { commitFreq(); });
    input.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { e.preventDefault(); commitFreq(); input.blur(); }
        if (e.key === 'Escape') { hideInput(); }
    });

    function commitFreq() {
        const raw = input.value.trim();
        if (!raw) { hideInput(); return; }
        let hz = parseFloat(raw);
        if (isNaN(hz)) { hideInput(); return; }
        // If entered as kHz (< 100,000), convert to Hz
        if (hz < 100000 && !raw.includes('.')) hz *= 1000;
        // If entered as MHz (has decimal or < 1000), convert to Hz
        if (hz < 1000) hz *= 1e6;
        hz = Math.round(hz);
        hz = Math.max(30000, Math.min(75000000, hz));
        const field = radioState.active_vfo === 'A' ? 'freq' : 'vfo_b_freq';
        sendCommand(field, hz);
        if (radioState.active_vfo === 'A') radioState.vfo_a_freq = hz;
        else radioState.vfo_b_freq = hz;
        renderFrequency();
        hideInput();
    }
}

// ── Initialize ──────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', function() {
    initUI();
    initFreqInput();
});
