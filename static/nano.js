/* MRRC Modern — NanoVNA 天线诊断台（/Nano，spec 2026-10-18）。
 *
 * 自包含：不加载 ft710_main.js / ft710_ui.js，不碰电台。所有物理量（SWR、R+jX）
 * 都由服务端算好送过来 —— 前端只画，不重算，避免两处公式各自漂移。
 *
 * 前缀安全：直接访问是 /Nano（base ''），公网反代是
 * /mrrc_modern/<呼号>/Nano（base '/mrrc_modern/<呼号>'）。所有请求都从页面自己的
 * location 推出来，不写死任何以 / 开头的接口路径。
 */
'use strict';

const URL_BASE = location.pathname.slice(0, location.pathname.lastIndexOf('/Nano'));

const state = {
    sweep: null,        // 最近一次扫描（含 swr / z / resonances）
    overlay: null,      // 叠加对比的那一条
    tab: 'swr',
    lastSavedId: null,
    device: null,
    settings: null,
};

// ── 基础设施 ────────────────────────────────────────────────────────

async function api(path, opts) {
    const init = Object.assign({ credentials: 'same-origin', method: 'GET' }, opts || {});
    init.headers = Object.assign({}, init.headers || {});
    if (init.body && typeof init.body !== 'string') {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(init.body);
    }
    const res = await fetch(URL_BASE + '/api/nano/' + path, init);
    if (res.status === 401) {
        location.href = URL_BASE + '/login';
    }
    return res;
}

async function readJson(res) {
    // 500 会返回纯文本 "Internal Server Error"，直接 res.json() 只会给用户一句
    // "SyntaxError: Unexpected token 'I'" —— 那等于没说。这里退化成可读的详情。
    const text = await res.text();
    try {
        return JSON.parse(text);
    } catch (e) {
        return {error: 'http_' + res.status,
                detail: '服务器返回 ' + res.status + '：' + text.slice(0, 200)};
    }
}

function banner(text, kind) {
    const el = document.getElementById('banner');
    if (!text) {
        el.className = '';
        el.textContent = '';
        return;
    }
    el.textContent = text;
    el.className = 'show' + (kind === 'warn' ? ' warn' : '');
}

function fmtHz(hz) {
    if (hz >= 1e6) return (hz / 1e6).toFixed(hz >= 1e7 ? 2 : 3) + ' MHz';
    if (hz >= 1e3) return (hz / 1e3).toFixed(1) + ' kHz';
    return hz + ' Hz';
}

function fmtOhm(v) {
    if (v === null || v === undefined) return '—';
    return Math.abs(v) >= 1000 ? (v / 1000).toFixed(2) + 'k' : v.toFixed(1);
}

// ── 画布 ────────────────────────────────────────────────────────────

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
    return { ctx, w: cssW, h: cssH };
}

function frame(ctx, w, h, pad) {
    ctx.strokeStyle = '#3a3a3a';
    ctx.lineWidth = 1;
    ctx.strokeRect(pad.l, pad.t, w - pad.l - pad.r, h - pad.t - pad.b);
}

function swrColour(swr) {
    if (swr >= 3) return '#ef4444';
    if (swr >= 2) return '#eab308';
    return '#22c55e';
}

// X 轴：对数频率。返回 freq -> px
function logX(f0, f1, l, r) {
    const lo = Math.log10(f0), hi = Math.log10(f1);
    return f => l + (Math.log10(f) - lo) / (hi - lo) * (r - l);
}

function yScale(v0, v1, t, b) {
    return v => b - (Math.min(Math.max(v, v0), v1) - v0) / (v1 - v0) * (b - t);
}

function drawGridFreq(ctx, xf, f0, f1, t, b) {
    ctx.fillStyle = '#666';
    ctx.strokeStyle = '#2f2f2f';
    ctx.textAlign = 'center';
    const marks = [1, 2, 3.5, 5, 7, 10, 14, 18, 21, 24.9, 28, 50, 54, 100, 144, 430]
        .map(v => v * 1e6).filter(f => f >= f0 && f <= f1);
    marks.forEach(f => {
        const x = xf(f);
        ctx.beginPath(); ctx.moveTo(x, t); ctx.lineTo(x, b); ctx.stroke();
        ctx.fillText(f >= 1e9 ? (f / 1e9) + 'G' : (f / 1e6) + 'M', x, b + 14);
    });
}

function drawSwr(canvas) {
    const { ctx, w, h } = fitCanvas(canvas);
    const pad = { l: 34, r: 10, t: 12, b: 22 };
    const s = state.sweep;
    if (!s) return emptyPlot(ctx, w, h, pad);
    const f0 = s.freqs[0], f1 = s.freqs[s.freqs.length - 1];
    const xf = logX(f0, f1, pad.l, w - pad.r);
    const ys = yScale(0, 10, pad.t, h - pad.b);

    frame(ctx, w, h, pad);
    drawGridFreq(ctx, xf, f0, f1, pad.t, h - pad.b);

    ctx.fillStyle = '#666'; ctx.textAlign = 'right';
    [1, 2, 3, 5, 7, 10].forEach(v => {
        const y = ys(v);
        ctx.strokeStyle = v === 1 ? '#3b3b3b' : '#2a2a2a';
        ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
        ctx.fillText(String(v), pad.l - 4, y + 3);
    });

    const trace = (data, dashed, colour) => {
        ctx.strokeStyle = colour;
        ctx.lineWidth = dashed ? 1.2 : 1.8;
        ctx.setLineDash(dashed ? [4, 3] : []);
        ctx.beginPath();
        data.swr.forEach((v, i) => {
            const x = xf(data.freqs[i]), y = ys(v);
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.stroke();
        ctx.setLineDash([]);
    };
    if (state.overlay) trace(state.overlay, true, 'rgba(245,158,11,.55)');
    trace(s, false, '#f59e0b');

    // 谐振点：竖线 + 频率
    (s.resonances || []).forEach(r => {
        const x = xf(r.f_min_hz);
        ctx.strokeStyle = 'rgba(34,197,94,.8)';
        ctx.setLineDash([2, 3]);
        ctx.beginPath(); ctx.moveTo(x, pad.t); ctx.lineTo(x, h - pad.b); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = '#22c55e'; ctx.textAlign = 'left';
        ctx.fillText(r.swr_min.toFixed(2) + ' @ ' + fmtHz(r.f_min_hz), x + 3, pad.t + 10);
    });
    // 可疑点（孤立 1.00 是掉读，不是谐振）
    (s.suspect || []).forEach(i => {
        ctx.fillStyle = '#ef4444';
        ctx.beginPath(); ctx.arc(xf(s.freqs[i]), ys(s.swr[i]), 3, 0, 7); ctx.fill();
    });
}

function drawSmith(canvas) {
    const { ctx, w, h } = fitCanvas(canvas);
    const s = state.sweep;
    if (!s) return emptyPlot(ctx, w, h, { l: 10, r: 10, t: 10, b: 10 });
    const cx = w / 2, cy = h / 2, R = Math.min(w, h) / 2 - 16;
    ctx.strokeStyle = '#3a3a3a'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();
    ctx.strokeStyle = '#2a2a2a';
    [0.25, 0.5, 0.75].forEach(k => {
        ctx.beginPath(); ctx.arc(cx, cy, R * k, 0, Math.PI * 2); ctx.stroke();
    });
    ctx.beginPath(); ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R); ctx.stroke();

    const trace = (data, dashed, colour) => {
        ctx.strokeStyle = colour; ctx.lineWidth = dashed ? 1.2 : 1.8;
        ctx.setLineDash(dashed ? [4, 3] : []);
        ctx.beginPath();
        data.s11.forEach((p, i) => {
            const x = cx + p[0] * R, y = cy - p[1] * R;
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.stroke(); ctx.setLineDash([]);
    };
    if (state.overlay) trace(state.overlay, true, 'rgba(245,158,11,.55)');
    trace(s, false, '#f59e0b');
    ctx.fillStyle = '#666'; ctx.textAlign = 'left';
    ctx.fillText('Smith · |Γ| 圆', 10, 14);
}

function drawZ(canvas) {
    const { ctx, w, h } = fitCanvas(canvas);
    const pad = { l: 40, r: 10, t: 12, b: 22 };
    const s = state.sweep;
    if (!s) return emptyPlot(ctx, w, h, pad);
    const mags = s.z.map(p => Math.hypot(p[0], p[1]));
    const top = Math.max(100, Math.min(2000, Math.ceil(Math.max.apply(null, mags) / 100) * 100));
    const f0 = s.freqs[0], f1 = s.freqs[s.freqs.length - 1];
    const xf = logX(f0, f1, pad.l, w - pad.r);
    const ys = yScale(0, top, pad.t, h - pad.b);
    frame(ctx, w, h, pad);
    drawGridFreq(ctx, xf, f0, f1, pad.t, h - pad.b);
    [0.25, 0.5, 0.75, 1].forEach(k => {
        const v = top * k, y = ys(v);
        ctx.strokeStyle = '#2a2a2a';
        ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
        ctx.fillStyle = '#666'; ctx.textAlign = 'right';
        ctx.fillText(v.toFixed(0) + 'Ω', pad.l - 4, y + 3);
    });
    ctx.strokeStyle = '#f59e0b'; ctx.lineWidth = 1.8;
    ctx.beginPath();
    s.z.forEach((p, i) => {
        const x = xf(s.freqs[i]), y = ys(Math.hypot(p[0], p[1]));
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
}

function drawRX(canvas) {
    const { ctx, w, h } = fitCanvas(canvas);
    const pad = { l: 40, r: 10, t: 12, b: 22 };
    const s = state.sweep;
    if (!s) return emptyPlot(ctx, w, h, pad);
    let peak = 100;
    s.z.forEach(p => { peak = Math.max(peak, Math.abs(p[0]), Math.abs(p[1])); });
    peak = Math.min(2000, Math.ceil(peak / 100) * 100);
    const f0 = s.freqs[0], f1 = s.freqs[s.freqs.length - 1];
    const xf = logX(f0, f1, pad.l, w - pad.r);
    const ys = yScale(-peak, peak, pad.t, h - pad.b);
    frame(ctx, w, h, pad);
    drawGridFreq(ctx, xf, f0, f1, pad.t, h - pad.b);
    const zero = ys(0);
    ctx.strokeStyle = '#3b3b3b';
    ctx.beginPath(); ctx.moveTo(pad.l, zero); ctx.lineTo(w - pad.r, zero); ctx.stroke();
    const trace = (idx, colour, dashed) => {
        ctx.strokeStyle = colour; ctx.lineWidth = 1.6;
        ctx.setLineDash(dashed ? [4, 3] : []);
        ctx.beginPath();
        s.z.forEach((p, i) => {
            const x = xf(s.freqs[i]), y = ys(p[idx]);
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.stroke(); ctx.setLineDash([]);
    };
    trace(0, '#f59e0b', false);
    trace(1, '#60a5fa', true);
    ctx.fillStyle = '#f59e0b'; ctx.textAlign = 'left';
    ctx.fillText('R', pad.l + 4, pad.t + 12);
    ctx.fillStyle = '#60a5fa';
    ctx.fillText('X', pad.l + 18, pad.t + 12);
}

function emptyPlot(ctx, w, h, pad) {
    frame(ctx, w, h, pad);
    ctx.fillStyle = '#666'; ctx.textAlign = 'center';
    ctx.fillText('还没有扫描', w / 2, h / 2);
}

function draw() {
    const canvas = document.getElementById('plot');
    if (state.tab === 'smith') drawSmith(canvas);
    else if (state.tab === 'z') drawZ(canvas);
    else if (state.tab === 'rx') drawRX(canvas);
    else drawSwr(canvas);
}

// ── 读数 ────────────────────────────────────────────────────────────

function readoutAt(index) {
    const s = state.sweep;
    if (!s || index < 0 || index >= s.freqs.length) return;
    const z = s.z[index] || [0, 0];
    const lines = [
        fmtHz(s.freqs[index]) + '   SWR ' + s.swr[index].toFixed(2),
        'Z = ' + fmtOhm(z[0]) + (z[1] >= 0 ? ' + j' : ' − j') + fmtOhm(Math.abs(z[1])) + ' Ω',
    ];
    if (state.overlay) {
        const i = nearestIndex(state.overlay.freqs, s.freqs[index]);
        lines.push('对比 ' + fmtHz(state.overlay.freqs[i]) + ' SWR '
            + state.overlay.swr[i].toFixed(2) + '（Δ '
            + (s.swr[index] - state.overlay.swr[i]).toFixed(2) + '）');
    }
    document.getElementById('readout').textContent = lines.join('\n');
}

function nearestIndex(freqs, f) {
    let lo = 0, hi = freqs.length - 1;
    while (hi - lo > 1) {
        const mid = (lo + hi) >> 1;
        if (freqs[mid] < f) lo = mid; else hi = mid;
    }
    return Math.abs(freqs[lo] - f) <= Math.abs(freqs[hi] - f) ? lo : hi;
}

function bindHover() {
    const canvas = document.getElementById('plot');
    const handler = ev => {
        const s = state.sweep;
        if (!s) return;
        const rect = canvas.getBoundingClientRect();
        const x = ((ev.touches ? ev.touches[0].clientX : ev.clientX) - rect.left);
        const pad = { l: 34, r: 10 };
        const f0 = s.freqs[0], f1 = s.freqs[s.freqs.length - 1];
        const l = pad.l, r = rect.width - pad.r;
        const frac = Math.min(1, Math.max(0, (x - l) / (r - l)));
        const f = Math.pow(10, Math.log10(f0) + frac * (Math.log10(f1) - Math.log10(f0)));
        readoutAt(nearestIndex(s.freqs, f));
    };
    canvas.addEventListener('mousemove', handler);
    canvas.addEventListener('touchstart', handler, { passive: true });
    canvas.addEventListener('touchmove', handler, { passive: true });
}

// ── 状态与设置 ──────────────────────────────────────────────────────

async function refreshStatus(full) {
    try {
        const res = await api('status' + (full ? '?full=1' : ''));
        if (!res.ok) return;
        const st = await readJson(res);
        state.device = st;
        renderDevice(st);
        if (st.settings) renderSettings(st.settings);
        if (st.tx) banner('电台在发射，已锁定扫描', 'warn');
        else if (!st.connected) banner('设备掉线 — 检查 USB 连接', 'warn');
        else if (st.busy) banner('设备忙（另一次操作正在进行）', 'warn');
        else banner('');
    } catch (e) {
        banner('连不上服务器', 'warn');
    }
}

function renderDevice(st) {
    const meta = document.getElementById('meta');
    if (!st.connected) {
        meta.textContent = '未连接';
    } else {
        meta.textContent = (st.board || 'NanoVNA') + ' · ' + (st.version || '?')
            + (st.vbat_mv ? ' · ' + (st.vbat_mv / 1000).toFixed(2) + ' V' : '');
    }
    const dl = document.getElementById('devinfo');
    const rows = [
        ['状态', st.connected ? '在线' : '掉线'],
        ['型号', st.board || '—'],
        ['固件', st.version || '—'],
        ['编译', st.build || '—'],
        ['平台', st.platform || '—'],
        ['端口', st.port || '—'],
        ['电池', st.vbat_mv ? (st.vbat_mv + ' mV') : '—'],
        ['标定', st.cal || '—'],
        ['设备扫频', st.device_sweep && st.device_sweep.start_hz
            ? fmtHz(st.device_sweep.start_hz) + ' – ' + fmtHz(st.device_sweep.stop_hz)
              + ' / ' + st.device_sweep.points + ' 点' : '—'],
    ];
    dl.innerHTML = rows.map(r => '<dt>' + r[0] + '</dt><dd>' + r[1] + '</dd>').join('');
}

function renderSettings(s) {
    state.settings = s;
    const box = document.getElementById('settings');
    if (!s) { box.textContent = '设备未连接'; return; }
    const smooth = s.smooth === null || s.smooth === undefined ? 0 : Math.round(s.smooth);
    box.innerHTML =
        '<div class="setting"><label>平滑</label>'
        + '<input type="range" id="set-smooth" min="0" max="8" value="' + smooth + '">'
        + '<span class="val" id="val-smooth">' + smooth + '</span></div>'
        + '<div class="setting"><label>输出功率</label>'
        + '<select id="set-power">'
        + [0, 1, 2, 3, 255].map(v => '<option value="' + v + '"'
            + ((s.power === v) ? ' selected' : '') + '>'
            + (v === 255 ? '自动' : v) + '</option>').join('')
        + '</select></div>'
        + '<div class="setting"><label>电延迟 S11 (ps)</label>'
        + '<input type="number" id="set-edelay" step="10" value="'
        + (s.edelay_s11_ps === null || s.edelay_s11_ps === undefined ? 0 : Math.round(s.edelay_s11_ps))
        + '"></div>'
        + '<div class="setting"><label>带宽</label><span class="val">'
        + (s.bandwidth_hz ? (s.bandwidth_hz + ' Hz') : '—') + '</span></div>'
        + '<p class="hint">tcxo 是整台仪器的频率基准，故意不在页面上 —— 用命令行改。</p>';

    const smoothEl = document.getElementById('set-smooth');
    smoothEl.oninput = () => { document.getElementById('val-smooth').textContent = smoothEl.value; };
    smoothEl.onchange = () => applySetting('smooth', parseInt(smoothEl.value, 10));
    document.getElementById('set-power').onchange = e => applySetting('power', parseInt(e.target.value, 10));
    document.getElementById('set-edelay').onchange = e => applySetting('edelay_s11_ps', parseInt(e.target.value, 10));
}

async function applySetting(name, value) {
    try {
        const res = await api('settings', { method: 'POST', body: { name: name, value: value } });
        const body = await readJson(res);
        if (!res.ok) { banner(body.detail || '设置失败', 'warn'); return; }
        renderSettings(body.settings);
    } catch (e) {
        banner('设置失败：' + e, 'warn');
    }
}

function renderCal() {
    const box = document.getElementById('cal');
    box.innerHTML = '';
    for (let slot = 0; slot < 5; slot++) {
        const b = document.createElement('button');
        b.textContent = 'recall ' + slot;
        b.onclick = () => calAction('recall', slot);
        box.appendChild(b);
    }
    const save = document.createElement('button');
    save.textContent = 'save…';
    save.onclick = () => {
        const slot = parseInt(prompt('写入哪个标定槽？0–4', '0'), 10);
        if (isNaN(slot)) return;
        const confirmText = prompt('这会覆盖设备里那一组标定。输入 SAVE 确认：', '');
        if (confirmText !== 'SAVE') return;
        calAction('save', slot, 'SAVE');
    };
    box.appendChild(save);
}

async function calAction(action, slot, confirm) {
    try {
        const res = await api('cal', { method: 'POST', body: { action: action, slot: slot, confirm: confirm } });
        const body = await readJson(res);
        if (!res.ok) { banner(body.detail || '操作失败', 'warn'); return; }
        banner('');
        renderDevice(state.device || {});
        refreshStatus(true);
        if (body.cal) {
            const dl = document.getElementById('devinfo');
            dl.querySelectorAll('dd').forEach(dd => {
                if (dd.previousElementSibling && dd.previousElementSibling.textContent === '标定') {
                    dd.textContent = body.cal;
                }
            });
        }
    } catch (e) {
        banner('操作失败：' + e, 'warn');
    }
}

// ── 扫描与存盘 ──────────────────────────────────────────────────────

async function scanOnce() {
    const btn = document.getElementById('scan');
    const [start, stop] = document.getElementById('preset').value.split('-').map(Number);
    const points = parseInt(document.getElementById('points').value, 10);
    const out = document.getElementById('readout');
    btn.disabled = true;
    const t0 = performance.now();
    out.textContent = '扫描中…';
    try {
        const res = await api('scan', { method: 'POST', body: { start: start, stop: stop, points: points } });
        const body = await readJson(res);
        if (!res.ok) {
            out.textContent = body.detail || body.error || '扫描失败';
            if (body.error === 'tx_active') banner('电台在发射，已锁定扫描', 'warn');
            return;
        }
        state.sweep = body;
        state.lastSavedId = null;
        draw();
        summarise(body);
    } catch (e) {
        out.textContent = '扫描失败：' + e;
    } finally {
        btn.disabled = false;
        const dt = Math.round(performance.now() - t0);
        if (state.sweep) {
            out.textContent += '\n' + dt + ' ms（设备侧 ' + state.sweep.elapsed_ms + ' ms）';
        }
    }
}

function summarise(body) {
    const lines = [];
    lines.push(fmtHz(body.freqs[0]) + ' – ' + fmtHz(body.freqs[body.freqs.length - 1])
        + ' · ' + body.freqs.length + ' 点');
    const best = body.swr.indexOf(Math.min.apply(null, body.swr));
    lines.push('最低 SWR ' + body.swr[best].toFixed(2) + ' @ ' + fmtHz(body.freqs[best]));
    (body.resonances || []).forEach(r => {
        lines.push('谐振 ' + fmtHz(r.f_min_hz) + ' SWR ' + r.swr_min.toFixed(2));
    });
    if ((body.suspect || []).length) {
        lines.push('可疑掉读点 ' + body.suspect.length + ' 个（红点）');
    }
    if (body.restored === false) {
        lines.push('注意：设备屏幕范围未能还原');
    }
    document.getElementById('readout').textContent = lines.join('\n');
}

async function saveSweep() {
    if (!state.sweep) { banner('先扫一次再存', 'warn'); return; }
    const label = document.getElementById('label').value.trim();
    try {
        const res = await api('sweeps', { method: 'POST', body: { label: label } });
        const body = await readJson(res);
        if (!res.ok) { banner(body.detail || '保存失败', 'warn'); return; }
        state.lastSavedId = body.id;
        banner('');
        loadSweeps();
    } catch (e) {
        banner('保存失败：' + e, 'warn');
    }
}

async function loadSweeps() {
    try {
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
            lbl.textContent = (it.label || it.id) + '  ·  ' + (it.created || '').slice(0, 16).replace('T', ' ');
            const over = document.createElement('button');
            over.textContent = '叠加';
            over.onclick = () => loadOverlay(it.id);
            const del = document.createElement('button');
            del.textContent = '删';
            del.onclick = async () => {
                if (!confirm('删除 ' + it.id + '？')) return;
                await api('sweeps/' + it.id, { method: 'DELETE' });
                loadSweeps();
            };
            li.appendChild(lbl); li.appendChild(over); li.appendChild(del);
            ul.appendChild(li);
        });
    } catch (e) { /* 列表失败不影响测量 */ }
}

async function loadOverlay(id) {
    try {
        const res = await api('sweeps/' + id);
        if (!res.ok) return;
        state.overlay = await readJson(res);
        state.overlay.id = id;
        draw();
        document.getElementById('readout').textContent = '叠加：' + id + '（橙色虚线）';
    } catch (e) { /* ignore */ }
}

function exportSweep(kind) {
    if (!state.lastSavedId) { banner('先保存这次扫描，再导出', 'warn'); return; }
    location.href = URL_BASE + '/api/nano/sweeps/' + state.lastSavedId + '?format=' + kind;
}

// ── 固件升级 ────────────────────────────────────────────────────────

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
    state.firmware = fw;
    if (!fw.enabled) {
        box.innerHTML = '<p class="hint">升级入口已上锁：在盒子上设置 '
            + '<code>MRRC_NANO_ALLOW_FLASH=1</code> 并重启服务后出现。'
            + '当前固件：' + (fw.installed_version || '未知') + '</p>';
        return;
    }
    const variants = Object.entries(fw.variants || {})
        .map(([key, label]) => '<option value="' + key + '"'
            + (fw.variant === key ? ' selected' : '') + '>' + label + '</option>').join('');
    const releases = (fw.releases || []).map(r =>
        '<option value="' + r.tag + '">' + r.tag + ' · ' + (r.published || '').slice(0, 10)
        + '</option>').join('');
    box.innerHTML =
        '<dl class="kv"><dt>当前固件</dt><dd>' + (fw.installed_version || '未知') + '</dd>'
        + '<dt>已记住变体</dt><dd>' + (fw.variant || '未选') + '</dd></dl>'
        + '<div class="setting"><label>变体</label><select id="fw-variant">' + variants + '</select></div>'
        + '<div class="setting"><label>目标版本</label><select id="fw-tag">' + releases + '</select></div>'
        + '<div class="setting"><label>确认字</label>'
        + '<input type="text" id="fw-confirm" placeholder="输入 FLASH" style="flex:1"></div>'
        + '<div class="setting"><label>CH0 现在开路</label>'
        + '<input type="checkbox" id="fw-open" checked></div>'
        + '<div class="row"><button class="primary" id="fw-go">开始升级</button></div>'
        + '<p class="hint" id="fw-status"></p>';
    document.getElementById('fw-go').onclick = startFlash;
}

async function startFlash() {
    const variant = document.getElementById('fw-variant').value;
    const tag = document.getElementById('fw-tag').value;
    const confirm = document.getElementById('fw-confirm').value.trim();
    const ch0_open = document.getElementById('fw-open').checked;
    const status = document.getElementById('fw-status');
    if (confirm !== 'FLASH') { status.textContent = '确认字不对：请输入 FLASH'; return; }
    try {
        const res = await api('flash', {method: 'POST',
            body: {tag: tag, variant: variant, confirm: confirm, ch0_open: ch0_open}});
        const body = await readJson(res);
        if (!res.ok) { status.textContent = body.detail || body.error || '启动失败'; return; }
        status.textContent = '开始了：' + tag + ' (' + variant + ')';
        waitFlash();
    } catch (e) {
        status.textContent = '启动失败：' + e;
    }
}

async function waitFlash() {
    const status = document.getElementById('fw-status');
    const deadline = Date.now() + 6 * 60 * 1000;
    while (Date.now() < deadline) {
        await new Promise(r => setTimeout(r, 1500));
        let st;
        try {
            const res = await api('flash/status');
            if (!res.ok) continue;
            st = await readJson(res);
        } catch (e) { continue; }
        status.textContent = st.state + (st.detail ? ' — ' + st.detail : '')
            + (st.after_version ? '（' + st.before_version + ' → ' + st.after_version + '）' : '');
        if (!st.running && ['done', 'failed', 'variant_mismatch'].includes(st.state)) {
            if (st.state === 'done') status.textContent += ' ✅';
            loadFirmware();
            refreshStatus(true);
            return;
        }
    }
    status.textContent = '等了 6 分钟还没结束 —— 看盒子日志';
}

// ── 启动 ────────────────────────────────────────────────────────────

function boot() {
    document.querySelectorAll('#tabs button').forEach(b => {
        b.onclick = () => {
            document.querySelectorAll('#tabs button').forEach(x => x.classList.remove('on'));
            b.classList.add('on');
            state.tab = b.getAttribute('data-tab');
            draw();
        };
    });
    document.getElementById('scan').onclick = scanOnce;
    document.getElementById('save').onclick = saveSweep;
    document.getElementById('exportCsv').onclick = () => exportSweep('csv');
    document.getElementById('exportS1p').onclick = () => exportSweep('s1p');
    document.getElementById('refresh').onclick = () => refreshStatus(true);
    renderCal();
    bindHover();
    window.addEventListener('resize', draw);
    refreshStatus(true);
    loadSweeps();
    loadFirmware();
    setInterval(() => refreshStatus(false), 5000);
}

boot();
