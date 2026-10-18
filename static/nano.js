/* MRRC Modern — 天线诊断台（/Nano）。
 *
 * 设计目标只有一条：**野外架好天线，一眼看出能不能用、要不要剪**。
 * 所以主视图是「判定卡」而不是仪表盘：一个大 SWR、一个阻抗、一个修剪建议，
 * 下面一条曲线。仪器式的东西（点数、设备开关、固件）都折进「设备」抽屉里。
 *
 * 前缀安全：直接访问 /Nano（base ''），公网反代 /mrrc_modern/<呼号>/Nano。
 */
'use strict';

const URL_BASE = location.pathname.slice(0, location.pathname.lastIndexOf('/Nano'));

// 波段：扫描范围比波段窗口宽一点，谐振点才看得见；判定只用 band 窗口内的点。
const BANDS = [
    {id: '80m', label: '80m', band: [3.5, 4.0], scan: [3.3, 4.2]},
    {id: '60m', label: '60m', band: [5.33, 5.41], scan: [5.2, 5.6]},
    {id: '40m', label: '40m', band: [7.0, 7.3], scan: [6.6, 7.7]},
    {id: '30m', label: '30m', band: [10.1, 10.15], scan: [9.8, 10.5]},
    {id: '20m', label: '20m', band: [14.0, 14.35], scan: [13.4, 15.0]},
    {id: '17m', label: '17m', band: [18.068, 18.168], scan: [17.4, 18.9]},
    {id: '15m', label: '15m', band: [21.0, 21.45], scan: [20.2, 22.2]},
    {id: '12m', label: '12m', band: [24.89, 24.99], scan: [24.2, 25.7]},
    {id: '10m', label: '10m', band: [28.0, 29.7], scan: [27.0, 30.5]},
    {id: '6m', label: '6m', band: [50.0, 54.0], scan: [48.0, 56.0]},
    {id: 'hf', label: 'HF 全段', band: [3.5, 30.0], scan: [1.8, 30.0]},
];

const state = {
    sweep: null,
    overlay: null,          // 基准曲线
    band: BANDS[2],         // 默认 40m
    dev: null,
    lastSavedId: null,
    custom: null,           // [start_hz, stop_hz] 自定义范围优先
};

// ── 基础 ────────────────────────────────────────────────────────────

async function api(path, opts) {
    const init = Object.assign({credentials: 'same-origin', method: 'GET'}, opts || {});
    init.headers = Object.assign({}, init.headers || {});
    if (init.body && typeof init.body !== 'string') {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(init.body);
    }
    const res = await fetch(URL_BASE + '/api/nano/' + path, init);
    if (res.status === 401) location.href = URL_BASE + '/login';
    return res;
}

async function readJson(res) {
    // 500 回的是纯文本 "Internal Server Error"，直接 res.json() 只会给用户一句
    // "SyntaxError: Unexpected token 'I'" —— 那等于没说。
    const text = await res.text();
    try { return JSON.parse(text); } catch (e) {
        return {error: 'http_' + res.status,
                detail: '服务器返回 ' + res.status + '：' + text.slice(0, 200)};
    }
}

function banner(text, kind) {
    const el = document.getElementById('banner');
    el.className = text ? ('show ' + (kind || '')) : '';
    if (text) el.textContent = text;
}

function fmtHz(hz) {
    if (hz >= 1e6) return (hz / 1e6).toFixed(hz >= 1e7 ? 2 : 3) + ' MHz';
    return (hz / 1e3).toFixed(1) + ' kHz';
}

function fmtOhm(v) {
    if (v === null || v === undefined) return '—';
    return Math.abs(v) >= 1000 ? (v / 1000).toFixed(2) + 'k' : v.toFixed(1);
}

function nearest(freqs, f) {
    let lo = 0, hi = freqs.length - 1;
    while (hi - lo > 1) {
        const mid = (lo + hi) >> 1;
        if (freqs[mid] < f) lo = mid; else hi = mid;
    }
    return Math.abs(freqs[lo] - f) <= Math.abs(freqs[hi] - f) ? lo : hi;
}

// ── 判定（野外只要这个）─────────────────────────────────────────────

function inBand(band) {
    const s = state.sweep;
    if (!s) return [];
    const lo = band[0] * 1e6, hi = band[1] * 1e6;
    const idx = [];
    s.freqs.forEach((f, i) => { if (f >= lo && f <= hi) idx.push(i); });
    return idx;
}

function verdictColor(maxSwr) {
    if (maxSwr <= 1.5) return ['ok', '免调可用'];
    if (maxSwr <= 2.0) return ['warn', '天调可修'];
    if (maxSwr <= 3.0) return ['warn', '勉强能用'];
    return ['bad', '不建议用'];
}

function renderVerdict() {
    const s = state.sweep;
    const el = {
        band: document.getElementById('v-band'),
        chip: document.getElementById('v-chip'),
        swr: document.getElementById('v-swr'),
        z: document.getElementById('v-z'),
        f: document.getElementById('v-f'),
        trim: document.getElementById('v-trim'),
    };
    if (!s) return;
    const band = state.band.band;
    const idx = inBand(band);
    const center = (band[0] + band[1]) / 2 * 1e6;
    el.band.textContent = state.band.label;

    const ci = nearest(s.freqs, center);
    if (!idx.length) {
        el.chip.className = 'chip bad';
        el.chip.textContent = '不在本次扫描内';
        return;
    }
    const swrs = idx.map(i => s.swr[i]);
    const maxSwr = Math.max.apply(null, swrs);
    const [cls, label] = verdictColor(maxSwr);
    el.chip.className = 'chip ' + cls;
    el.chip.textContent = label + '（段内最高 ' + maxSwr.toFixed(2) + '）';
    el.swr.textContent = s.swr[ci].toFixed(2);

    const z = s.z[ci] || [0, 0];
    el.z.textContent = fmtOhm(z[0]) + (z[1] >= 0 ? ' +j' : ' −j') + fmtOhm(Math.abs(z[1])) + 'Ω';

    // 谐振点：段内最低 SWR 的那个点
    const bi = idx.reduce((a, b) => (s.swr[b] < s.swr[a] ? b : a), idx[0]);
    const fRes = s.freqs[bi];
    el.f.textContent = fmtHz(fRes) + ' (' + s.swr[bi].toFixed(2) + ')';

    // 修剪建议：谐振频率与波段中心的相对偏差 ≈ 振子长度的相对偏差
    const dev = (fRes - center) / center;          // >0 = 谐振偏高 = 偏短
    const pct = Math.abs(dev * 100);
    const t = el.trim;
    t.className = 'trim';
    if (pct < 0.5) {
        t.innerHTML = '<b>谐振正好在波段中心</b>，长度不用动。' +
            '段内 SWR 全在 ' + Math.min.apply(null, swrs).toFixed(2) + '–' +
            maxSwr.toFixed(2) + '。';
    } else if (dev > 0) {
        t.innerHTML = '谐振偏高 <b>' + pct.toFixed(1) + '%</b>（' + fmtHz(fRes) +
            ' vs 中心 ' + fmtHz(center) + '）→ 振子<b>偏短，需要加长</b>约 ' +
            pct.toFixed(1) + '%：每 10 m 加 ' + (pct * 10).toFixed(0) + ' cm。';
    } else {
        t.className = 'trim crit';
        t.innerHTML = '谐振偏低 <b>' + pct.toFixed(1) + '%</b>（' + fmtHz(fRes) +
            ' vs 中心 ' + fmtHz(center) + '）→ 振子<b>偏长，需要缩短</b>约 ' +
            pct.toFixed(1) + '%：每 10 m 剪 ' + (pct * 10).toFixed(0) + ' cm。';
    }
    if (state.overlay) {
        const oi = nearest(state.overlay.freqs, center);
        const d = s.swr[ci] - state.overlay.swr[oi];
        t.innerHTML += '<br>与基准相比：中心 SWR ' + (d >= 0 ? '+' : '') + d.toFixed(2) + '。';
    }
}

// ── 画图 ────────────────────────────────────────────────────────────

function fitCanvas(canvas) {
    // CSS 尺寸以布局为准（getBoundingClientRect），位图按 dpr 放大。canvas.height
    // 只能当位图高度用 —— 没有 CSS height 规则时它同时是布局高度，每次重绘都会
    // 把画布撑高一倍（Retina 屏第一次刷新就露馅）。
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const cssW = Math.max(1, Math.round(rect.width));
    const cssH = Math.max(1, Math.round(rect.height));
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    ctx.font = '11px -apple-system, system-ui, sans-serif';
    return {ctx, w: cssW, h: cssH};
}

function draw() {
    const canvas = document.getElementById('plot');
    const {ctx, w, h} = fitCanvas(canvas);
    const pad = {l: 34, r: 10, t: 12, b: 22};
    const s = state.sweep;
    ctx.strokeStyle = '#3a382f';
    ctx.lineWidth = 1;
    ctx.strokeRect(pad.l, pad.t, w - pad.l - pad.r, h - pad.t - pad.b);
    if (!s) {
        ctx.fillStyle = '#6f695c';
        ctx.textAlign = 'center';
        ctx.fillText('选波段 → 扫描', w / 2, h / 2);
        return;
    }
    const f0 = s.freqs[0], f1 = s.freqs[s.freqs.length - 1];
    const xf = f => pad.l + (Math.log10(f) - Math.log10(f0)) /
        (Math.log10(f1) - Math.log10(f0)) * (w - pad.l - pad.r);
    const yMax = 5;
    const ys = v => h - pad.b - Math.min(v, yMax) / yMax * (h - pad.t - pad.b);

    // 波段窗口
    const [blo, bhi] = state.band.band;
    ctx.fillStyle = 'rgba(245,166,35,.10)';
    ctx.fillRect(xf(blo * 1e6), pad.t, xf(bhi * 1e6) - xf(blo * 1e6), h - pad.t - pad.b);

    // 网格
    ctx.strokeStyle = '#2b2a24';
    ctx.fillStyle = '#6f695c';
    ctx.textAlign = 'right';
    [1, 1.5, 2, 3, 4, 5].forEach(v => {
        const y = ys(v);
        ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
        ctx.fillText(String(v), pad.l - 4, y + 3);
    });
    ctx.textAlign = 'center';
    const ticks = [1.8, 3.5, 5, 7, 10, 14, 18, 21, 24.9, 28, 50].map(v => v * 1e6)
        .filter(f => f >= f0 && f <= f1);
    ticks.forEach(f => ctx.fillText((f / 1e6) + 'M', xf(f), h - pad.b + 14));

    // 曲线
    const trace = (data, colour, width, dash) => {
        ctx.strokeStyle = colour;
        ctx.lineWidth = width;
        ctx.setLineDash(dash || []);
        ctx.beginPath();
        data.swr.forEach((v, i) => {
            const x = xf(data.freqs[i]), y = ys(v);
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.stroke();
        ctx.setLineDash([]);
    };
    if (state.overlay) trace(state.overlay, 'rgba(160,200,255,.55)', 1.4, [4, 3]);
    trace(s, '#f5a623', 2.2);

    // 判定线 1.5 / 2.0
    [1.5, 2.0].forEach(v => {
        ctx.strokeStyle = 'rgba(47,191,95,.35)';
        ctx.setLineDash([3, 4]);
        ctx.beginPath(); ctx.moveTo(pad.l, ys(v)); ctx.lineTo(w - pad.r, ys(v)); ctx.stroke();
        ctx.setLineDash([]);
    });

    // 谐振点
    const idx = inBand(state.band.band);
    if (idx.length) {
        const bi = idx.reduce((a, b) => (s.swr[b] < s.swr[a] ? b : a), idx[0]);
        const x = xf(s.freqs[bi]);
        ctx.strokeStyle = 'rgba(47,191,95,.9)';
        ctx.setLineDash([2, 3]);
        ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, h - pad.b); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = '#2fbf5f';
        ctx.textAlign = 'left';
        ctx.fillText(s.swr[bi].toFixed(2), x + 3, pad.t + 11);
    }
}

function showTip(ev, index) {
    const tip = document.getElementById('tip');
    const s = state.sweep;
    if (!s || index < 0) { tip.style.display = 'none'; return; }
    const z = s.z[index] || [0, 0];
    let text = fmtHz(s.freqs[index]) + '\nSWR ' + s.swr[index].toFixed(2)
        + '\nZ ' + fmtOhm(z[0]) + (z[1] >= 0 ? ' +j' : ' −j') + fmtOhm(Math.abs(z[1]));
    if (state.overlay) {
        const i = nearest(state.overlay.freqs, s.freqs[index]);
        text += '\nΔ基准 ' + (s.swr[index] - state.overlay.swr[i]).toFixed(2);
    }
    tip.textContent = text;
    tip.style.display = 'block';
    const x = ev.touches ? ev.touches[0].clientX : ev.clientX;
    const y = ev.touches ? ev.touches[0].clientY : ev.clientY;
    tip.style.left = Math.min(window.innerWidth - (tip.offsetWidth || 120) - 8, x + 14) + 'px';
    tip.style.top = Math.max(8, y - 70) + 'px';
}

function bindHover() {
    const canvas = document.getElementById('plot');
    const s0 = () => state.sweep;
    const move = ev => {
        const s = s0();
        if (!s) return;
        const rect = canvas.getBoundingClientRect();
        const x = (ev.touches ? ev.touches[0].clientX : ev.clientX) - rect.left;
        const l = 34, r = rect.width - 10;
        const f0 = s.freqs[0], f1 = s.freqs[s.freqs.length - 1];
        const frac = Math.min(1, Math.max(0, (x - l) / (r - l)));
        const f = Math.pow(10, Math.log10(f0) + frac * (Math.log10(f1) - Math.log10(f0)));
        const idx = nearest(s.freqs, f);
        showTip(ev, idx);
        document.getElementById('readout').textContent =
            fmtHz(s.freqs[idx]) + '   SWR ' + s.swr[idx].toFixed(2) + '   点击可设为标记';
    };
    canvas.addEventListener('mousemove', move);
    canvas.addEventListener('mouseleave', () => {
        document.getElementById('tip').style.display = 'none';
    });
    canvas.addEventListener('touchstart', move, {passive: true});
    canvas.addEventListener('touchmove', move, {passive: true});
    canvas.addEventListener('touchend', () => {
        setTimeout(() => { document.getElementById('tip').style.display = 'none'; }, 1500);
    });
    // 点一下图 = 在设备屏幕上把标记挪到那里（野外不用抠屏幕）
    canvas.addEventListener('click', async ev => {
        const s = s0();
        if (!s) return;
        const rect = canvas.getBoundingClientRect();
        const x = ev.clientX - rect.left;
        const l = 34, r = rect.width - 10;
        const f0 = s.freqs[0], f1 = s.freqs[s.freqs.length - 1];
        const frac = Math.min(1, Math.max(0, (x - l) / (r - l)));
        const f = Math.pow(10, Math.log10(f0) + frac * (Math.log10(f1) - Math.log10(f0)));
        await api('marker', {method: 'POST', body: {index: nearest(s.freqs, f)}});
    });
}

// ── 波段与扫描 ──────────────────────────────────────────────────────

function renderBands() {
    const box = document.getElementById('bands');
    box.innerHTML = '';
    BANDS.forEach(b => {
        const el = document.createElement('button');
        el.textContent = b.label;
        if (b.id === state.band.id) el.className = 'on';
        el.onclick = () => {
            state.band = b;
            state.custom = null;
            renderBands();
            syncCenterSpan();
            scanOnce();
        };
        box.appendChild(el);
    });
}

function syncCenterSpan() {
    if (state.custom) {
        document.getElementById('center').value = ((state.custom[0] + state.custom[1]) / 2e6).toFixed(3);
        document.getElementById('span').value = ((state.custom[1] - state.custom[0]) / 1e6).toFixed(3);
        return;
    }
    const [a, b] = state.band.scan;
    document.getElementById('center').value = ((a + b) / 2).toFixed(3);
    document.getElementById('span').value = (b - a).toFixed(3);
}

function currentRange() {
    if (state.custom) return state.custom;
    const [a, b] = state.band.scan;
    return [Math.round(a * 1e6), Math.round(b * 1e6)];
}

function applyCustomRange() {
    const c = parseFloat(document.getElementById('center').value);
    const s = parseFloat(document.getElementById('span').value);
    if (isNaN(c) || isNaN(s) || s <= 0) { banner('中心/带宽填一下', 'warn'); return; }
    let start = Math.round((c - s / 2) * 1e6);
    let stop = Math.round((c + s / 2) * 1e6);
    start = Math.max(50000, start);
    stop = Math.min(900000000, stop);
    if (stop <= start) { banner('带宽太小', 'warn'); return; }
    state.custom = [start, stop];
    scanOnce();
}

async function scanOnce() {
    const btn = document.getElementById('scan');
    const [start, stop] = currentRange();
    const points = parseInt(document.getElementById('points').value, 10);
    btn.disabled = true;
    const t0 = performance.now();
    document.getElementById('readout').textContent = '扫描中…';
    try {
        const res = await api('scan', {method: 'POST',
            body: {start: start, stop: stop, points: points}});
        const body = await readJson(res);
        if (!res.ok) {
            document.getElementById('readout').textContent = body.detail || body.error || '扫描失败';
            if (body.error === 'tx_active') banner('电台在发射，已锁定扫描', 'warn');
            return;
        }
        banner('');
        state.sweep = body;
        state.lastSavedId = null;
        draw();
        renderVerdict();
        document.getElementById('readout').textContent =
            fmtHz(body.freqs[0]) + ' – ' + fmtHz(body.freqs[body.freqs.length - 1])
            + ' · ' + body.freqs.length + ' 点 · ' + Math.round(performance.now() - t0) + ' ms';
    } catch (e) {
        document.getElementById('readout').textContent = '扫描失败：' + e;
    } finally {
        btn.disabled = false;
    }
}

// ── 状态 / 设备 ──────────────────────────────────────────────────────

async function refreshStatus(full) {
    try {
        const res = await api('status' + (full ? '?full=1' : ''));
        if (!res.ok) return;
        const st = await readJson(res);
        state.dev = st;
        document.getElementById('meta').textContent = st.connected
            ? ((st.board || 'NanoVNA') + ' · ' + (st.version || '?')
               + (st.vbat_mv ? ' · ' + (st.vbat_mv / 1000).toFixed(2) + 'V' : ''))
            : '设备未连接';
        renderDevice(st);
        if (st.device_sweep && st.device_sweep.start_hz) {
            const ds = document.getElementById('dev-start');
            const de = document.getElementById('dev-stop');
            if (ds && !ds.value) ds.value = (st.device_sweep.start_hz / 1e6).toFixed(2);
            if (de && !de.value) de.value = (st.device_sweep.stop_hz / 1e6).toFixed(2);
        }
        if (st.settings) renderSettings(st.settings);
        if (st.flags !== undefined) renderFlags();
        // 顺序是判据：忙 ≠ 掉线
        if (st.tx) banner('电台在发射，已锁定扫描', 'warn');
        else if (st.busy) banner('设备忙（另一次操作正在进行）', 'warn');
        else if (!st.connected) banner('设备掉线 — 检查 USB 连接', 'bad');
        else banner('');
    } catch (e) {
        banner('连不上服务器', 'bad');
    }
}

function renderDevice(st) {
    const dl = document.getElementById('devinfo');
    dl.innerHTML = [
        ['状态', st.connected ? '在线' : '掉线'],
        ['型号', st.board || '—'],
        ['固件', st.version || '—'],
        ['电池', st.vbat_mv ? st.vbat_mv + ' mV' : '—'],
        ['标定', st.cal || '（无）'],
        ['设备扫频', st.device_sweep && st.device_sweep.start_hz
            ? fmtHz(st.device_sweep.start_hz) + ' – ' + fmtHz(st.device_sweep.stop_hz) : '—'],
    ].map(r => '<dt>' + r[0] + '</dt><dd>' + r[1] + '</dd>').join('');
}

function renderSettings(s) {
    const box = document.getElementById('settings');
    if (!s) { box.textContent = '设备未连接'; return; }
    const smooth = s.smooth == null ? 0 : Math.round(s.smooth);
    box.innerHTML =
        '<div class="setting"><label>平滑</label>'
        + '<input type="range" id="set-smooth" min="0" max="8" value="' + smooth + '">'
        + '<span id="val-smooth" style="min-width:2em;text-align:right">' + smooth + '</span></div>'
        + '<div class="setting"><label>输出功率</label><select id="set-power">'
        + [0, 1, 2, 3, 255].map(v => '<option value="' + v + '"'
            + (s.power === v ? ' selected' : '') + '>' + (v === 255 ? '自动' : v) + '</option>').join('')
        + '</select></div>'
        + '<div class="setting"><label>电延迟 (ps)</label>'
        + '<input type="number" id="set-edelay" step="10" value="'
        + (s.edelay_s11_ps == null ? 0 : Math.round(s.edelay_s11_ps)) + '"></div>'
        + '<div class="setting"><label>测量带宽</label><span>'
        + (s.bandwidth_hz ? s.bandwidth_hz + ' Hz' : '—') + '</span></div>';
    const sm = document.getElementById('set-smooth');
    sm.oninput = () => { document.getElementById('val-smooth').textContent = sm.value; };
    sm.onchange = () => applySetting('smooth', parseInt(sm.value, 10));
    document.getElementById('set-power').onchange = e => applySetting('power', parseInt(e.target.value, 10));
    document.getElementById('set-edelay').onchange = e => applySetting('edelay_s11_ps', parseInt(e.target.value, 10));
}

const TRACE_LABELS = {
    logmag: '对数幅度 (dB)', swr: '驻波 SWR', phase: '相位', delay: '群延时',
    smith: 'Smith 圆图', polar: '极坐标', linear: '线性幅度',
    real: '实部', imag: '虚部', r: '电阻 R', x: '电抗 X', z: '阻抗 |Z|',
};

async function applyFieldPreset() {
    const btn = document.getElementById('field-preset');
    btn.disabled = true;
    try {
        const res = await api('field-preset', {method: 'POST', body: {}});
        const body = await readJson(res);
        if (!res.ok) { banner(body.detail || body.error || '配置失败', 'bad'); return; }
        banner('野外配置已就位（槽 ' + (body.slot == null ? 4 : body.slot)
               + '）：拔下来后开机 → RECALL → ' + (body.slot == null ? 4 : body.slot));
        setTimeout(() => banner(''), 3500);
        renderView();
        refreshStatus(true);
    } catch (e) {
        banner('配置失败：' + e, 'bad');
    } finally {
        btn.disabled = false;
    }
}

async function applyDeviceSweep() {
    const start = parseFloat(document.getElementById('dev-start').value);
    const stop = parseFloat(document.getElementById('dev-stop').value);
    const points = parseInt(document.getElementById('dev-points').value, 10);
    if (isNaN(start) || isNaN(stop) || stop <= start) { banner('起止频率不对', 'warn'); return; }
    const res = await api('device-sweep', {method: 'POST', body: {
        start: Math.round(start * 1e6), stop: Math.round(stop * 1e6), points: points}});
    const body = await readJson(res);
    if (!res.ok) { banner(body.detail || '设置失败', 'bad'); return; }
    banner('设备扫描范围已设为 ' + start + '–' + stop + ' MHz / ' + points + ' 点（已写 flash）');
    setTimeout(() => banner(''), 3000);
    refreshStatus(true);
}

async function applyTraceParams() {
    const scale = parseFloat(document.getElementById('trace-scale').value);
    const refpos = parseFloat(document.getElementById('trace-refpos').value);
    const body = {};
    if (!isNaN(scale)) body.scale = scale;
    if (!isNaN(refpos)) body.refpos = refpos;
    const res = await api('trace', {method: 'POST', body: body});
    const out = await readJson(res);
    banner(res.ok ? '设备迹线刻度已更新' : (out.detail || '设置失败'), res.ok ? '' : 'bad');
    if (res.ok) setTimeout(() => banner(''), 2000);
}

async function renderView() {
    const box = document.getElementById('view');
    let v;
    try {
        const res = await api('view');
        if (!res.ok) { box.innerHTML = '<p class="hint">读不到设备显示状态</p>'; return; }
        v = await readJson(res);
    } catch (e) { box.innerHTML = '<p class="hint">读不到设备显示状态</p>'; return; }
    const tr = (v.traces || [])[0];
    const opts = (v.types || []).map(t =>
        '<option value="' + t + '"' + (tr && tr.type === t ? ' selected' : '') + '>'
        + (TRACE_LABELS[t] || t) + '</option>').join('');
    box.innerHTML =
        '<div class="setting"><label>屏幕迹线</label><select id="view-trace">' + opts + '</select></div>'
        + '<div class="setting"><label>标记</label><span>'
        + ((v.markers || []).length ? fmtHz(v.markers[0].freq_hz) : '未开')
        + '（点曲线可移动）</span></div>';
    if (tr) {
        if (tr.scale != null) document.getElementById('trace-scale').value = tr.scale;
        if (tr.refpos != null) document.getElementById('trace-refpos').value = tr.refpos;
    }
    const sel = document.getElementById('view-trace');
    if (sel) {
        sel.onchange = async () => {
            await api('trace', {method: 'POST', body: {type: sel.value, src: 0, id: 0}});
            banner('设备屏幕已切到 ' + (TRACE_LABELS[sel.value] || sel.value));
            setTimeout(() => banner(''), 1800);
        };
    }
}

const FLAG_LABELS = {
    grid: '网格显示数值', dot: '网格用点线', flip: '翻转屏幕',
    bk: '记住关机前状态', auto: '文件自动命名', mode: '标记搜索取最小',
    avg: '平滑取算术平均', separator: '小数分隔符用逗号',
};

function renderFlags() {
    const box = document.getElementById('flags');
    const flags = (state.dev && state.dev.flags) || {};
    box.innerHTML = Object.keys(FLAG_LABELS).map(k =>
        '<div class="setting"><label>' + FLAG_LABELS[k] + '</label>'
        + '<input type="checkbox" data-flag="' + k + '"' + (flags[k] ? ' checked' : '') + '></div>').join('');
    box.querySelectorAll('input[data-flag]').forEach(el => {
        el.onchange = () => applySetting('cfg_' + el.getAttribute('data-flag'), el.checked ? 1 : 0);
    });
}

async function applySetting(name, value) {
    try {
        const res = await api('settings', {method: 'POST', body: {name: name, value: value}});
        const body = await readJson(res);
        if (!res.ok) { banner(body.detail || '设置失败', 'warn'); return; }
        if (body.settings) renderSettings(body.settings);
    } catch (e) { banner('设置失败：' + e, 'warn'); }
}

async function saveToDevice() {
    try {
        const res = await api('settings', {method: 'POST', body: {name: 'saveconfig', value: 1}});
        const body = await readJson(res);
        banner(res.ok ? '已写入设备 flash（含标定）' : (body.detail || '保存失败'), res.ok ? '' : 'bad');
        if (res.ok) setTimeout(() => banner(''), 2500);
    } catch (e) { banner('保存失败：' + e, 'bad'); }
}

function renderCal() {
    const box = document.getElementById('cal');
    box.innerHTML = '';
    for (let slot = 0; slot < 5; slot++) {
        const b = document.createElement('button');
        b.className = 'ghost';
        b.textContent = 'recall ' + slot;
        b.onclick = () => calAction('recall', slot);
        box.appendChild(b);
    }
    const save = document.createElement('button');
    save.className = 'ghost';
    save.textContent = 'save…';
    save.onclick = () => {
        const slot = parseInt(prompt('写入哪个标定槽？0–4', '0'), 10);
        if (isNaN(slot)) return;
        if (prompt('这会覆盖设备里那一组标定。输入 SAVE 确认：', '') !== 'SAVE') return;
        calAction('save', slot, 'SAVE');
    };
    box.appendChild(save);
}

async function calAction(action, slot, confirmText) {
    const res = await api('cal', {method: 'POST',
        body: {action: action, slot: slot, confirm: confirmText}});
    const body = await readJson(res);
    banner(res.ok ? '标定槽已' + (action === 'save' ? '写入' : '读取') : (body.detail || '操作失败'),
           res.ok ? '' : 'bad');
    if (res.ok) setTimeout(() => banner(''), 2000);
    refreshStatus(true);
}

// ── 存盘与对比 ──────────────────────────────────────────────────────

async function saveSweep() {
    if (!state.sweep) { banner('先扫一次', 'warn'); return; }
    const label = document.getElementById('label').value.trim();
    const res = await api('sweeps', {method: 'POST', body: {label: label}});
    const body = await readJson(res);
    if (!res.ok) { banner(body.detail || '保存失败', 'bad'); return; }
    state.lastSavedId = body.id;
    banner('已保存：' + body.id);
    setTimeout(() => banner(''), 1500);
    loadSweeps();
}

async function loadSweeps() {
    const res = await api('sweeps');
    if (!res.ok) return;
    const items = (await readJson(res)).sweeps || [];
    const ul = document.getElementById('sweeps');
    if (!items.length) { ul.innerHTML = '<li>还没有存过扫描</li>'; return; }
    ul.innerHTML = '';
    items.forEach(it => {
        const li = document.createElement('li');
        const lbl = document.createElement('span');
        lbl.className = 'lbl';
        lbl.textContent = (it.label || it.id) + ' · ' + (it.created || '').slice(5, 16).replace('T', ' ');
        const base = document.createElement('button');
        base.textContent = '设为基准';
        base.onclick = () => loadOverlay(it.id);
        const del = document.createElement('button');
        del.textContent = '删';
        del.onclick = async () => {
            if (!confirm('删除 ' + it.id + '？')) return;
            await api('sweeps/' + it.id, {method: 'DELETE'});
            loadSweeps();
        };
        li.appendChild(lbl); li.appendChild(base); li.appendChild(del);
        ul.appendChild(li);
    });
}

async function loadOverlay(id) {
    const res = await api('sweeps/' + id);
    if (!res.ok) return;
    state.overlay = await readJson(res);
    draw();
    renderVerdict();
    banner('基准：' + id + '（蓝色虚线）');
    setTimeout(() => banner(''), 2500);
}

function exportSweep(kind) {
    if (!state.lastSavedId) { banner('先保存这次扫描', 'warn'); return; }
    location.href = URL_BASE + '/api/nano/sweeps/' + state.lastSavedId + '?format=' + kind;
}

// ── 固件（折叠在设备里）─────────────────────────────────────────────

async function loadFirmware() {
    const box = document.getElementById('firmware');
    let fw;
    try {
        const res = await api('firmware');
        if (!res.ok) { box.innerHTML = '<p class="hint">读不到版本信息</p>'; return; }
        fw = await readJson(res);
    } catch (e) {
        box.innerHTML = '<p class="hint">读不到版本信息</p>';
        return;
    }
    if (!fw.enabled) {
        box.innerHTML = '<p class="hint">当前固件 ' + (fw.installed_version || '未知')
            + '。网页升级入口已上锁（设置 <code>MRRC_NANO_ALLOW_FLASH=1</code> 并重启服务后出现）。<br>'
            + '手动升级：<code>dfu-util -a 0 -s 0x08000000:leave -D 镜像.bin</code>，'
            + '设备 DFU 入口在 CONFIG → EXPERT SETTINGS → ▶DFU。</p>';
        return;
    }
    const variants = Object.entries(fw.variants || {}).map(([k, v]) =>
        '<option value="' + k + '"' + (fw.variant === k ? ' selected' : '') + '>' + v + '</option>').join('');
    const releases = (fw.releases || []).map(r =>
        '<option value="' + r.tag + '">' + r.tag + '</option>').join('');
    box.innerHTML =
        '<div class="setting"><label>当前</label><span>' + (fw.installed_version || '?') + '</span></div>'
        + '<div class="setting"><label>变体</label><select id="fw-variant">' + variants + '</select></div>'
        + '<div class="setting"><label>目标</label><select id="fw-tag">' + releases + '</select></div>'
        + '<div class="setting"><label>确认字</label>'
        + '<input type="text" id="fw-confirm" placeholder="FLASH" style="flex:1"></div>'
        + '<div class="row"><button class="ghost" id="fw-go">开始升级</button></div>'
        + '<p class="hint">升级会整片擦除 flash：标定与设置全清，事后要重做 OPEN/SHORT/LOAD。'
        + '变体看机器标签（MS/SI/ZK 结尾）。</p><p class="hint" id="fw-status"></p>';
    document.getElementById('fw-go').onclick = startFlash;
}

async function startFlash() {
    const status = document.getElementById('fw-status');
    const res = await api('flash', {method: 'POST', body: {
        tag: document.getElementById('fw-tag').value,
        variant: document.getElementById('fw-variant').value,
        confirm: document.getElementById('fw-confirm').value.trim(),
        ch0_open: true,
    }});
    const body = await readJson(res);
    if (!res.ok) { status.textContent = body.detail || body.error || '启动失败'; return; }
    status.textContent = '开始了…';
    const deadline = Date.now() + 6 * 60 * 1000;
    while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 1500));
        const s = await readJson(await api('flash/status'));
        status.textContent = s.state + (s.detail ? ' — ' + s.detail : '')
            + (s.after_version ? '（' + s.before_version + ' → ' + s.after_version + '）' : '');
        if (!s.running && ['done', 'failed', 'variant_mismatch'].includes(s.state)) {
            loadFirmware();
            refreshStatus(true);
            return;
        }
    }
    status.textContent = '等了 6 分钟还没结束 —— 看盒子日志';
}

// ── 启动 ────────────────────────────────────────────────────────────

function boot() {
    renderBands();
    syncCenterSpan();
    document.getElementById('scan').onclick = scanOnce;
    document.getElementById('apply-freq').onclick = applyCustomRange;
    document.getElementById('baseline').onclick = () => {
        if (!state.sweep) { banner('先扫一次', 'warn'); return; }
        state.overlay = state.sweep;
        draw(); renderVerdict();
        banner('当前曲线已设为基准');
        setTimeout(() => banner(''), 2000);
    };
    document.getElementById('save').onclick = saveSweep;
    document.getElementById('saveconfig').onclick = saveToDevice;
    document.getElementById('field-preset').onclick = applyFieldPreset;
    document.getElementById('dev-apply').onclick = applyDeviceSweep;
    document.getElementById('trace-apply').onclick = applyTraceParams;
    document.getElementById('exportCsv').onclick = () => exportSweep('csv');
    document.getElementById('exportS1p').onclick = () => exportSweep('s1p');
    renderCal();
    bindHover();
    window.addEventListener('resize', draw);
    refreshStatus(true);
    loadSweeps();
    loadFirmware();
    renderView();
    setInterval(() => refreshStatus(false), 5000);
}

boot();
