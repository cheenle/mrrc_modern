/* 性能负载页 (AD-031).
 *
 * 只做三件事：拉 /api/perf、把数字画成人能读的东西、画一条 60 点的火花线。
 * 判定文字由服务端算（host_perf.verdict）——前端不该有两个「什么是瓶颈」的定义。
 */
'use strict';

// 前缀安全：入口可能是 <呼号>.mrrc.vlsc.net（根）或 www.vlsc.net/mrrc_modern/<呼号>/
const URL_BASE = location.pathname.replace(/\/Perf$/, '');

const POLL_MS = 2000;
const HISTORY = 60;
const history = { cpu: [], load: [], mem: [], disk: [], lag: [] };

let paused = false;
let timer = null;
let failures = 0;

const $ = (id) => document.getElementById(id);

function fmtPct(value) {
    return value === null || value === undefined ? '—' : value.toFixed(1) + '%';
}

function fmtBytes(value) {
    if (value === null || value === undefined) return '—';
    const units = ['B', 'kB', 'MB', 'GB'];
    let size = value;
    let unit = 0;
    while (Math.abs(size) >= 1000 && unit < units.length - 1) {
        size /= 1000;
        unit += 1;
    }
    return (unit === 0 ? Math.round(size) : size.toFixed(1)) + ' ' + units[unit];
}

function setText(id, text) {
    const node = $(id);
    if (node) node.textContent = text;
}

function spark(canvasId, values, color) {
    const canvas = $(canvasId);
    if (!canvas) return;
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth || 200;
    const height = canvas.clientHeight || 34;
    // 按设备像素比设置后备存储，否则 Retina 上会糊成一片（而且每次刷新翻一倍）
    if (canvas.width !== Math.round(width * ratio) ||
        canvas.height !== Math.round(height * ratio)) {
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(height * ratio);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (values.length < 2) return;
    const max = Math.max(1, ...values);
    ctx.beginPath();
    values.forEach((value, index) => {
        const x = (index / (HISTORY - 1)) * width;
        const y = height - (Math.min(value, max) / max) * (height - 2) - 1;
        if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.lineTo((values.length - 1) / (HISTORY - 1) * width, height);
    ctx.lineTo(0, height);
    ctx.closePath();
    ctx.fillStyle = color.replace('rgb', 'rgba').replace(')', ',0.12)');
    ctx.fill();
}

function push(series, value) {
    if (value === null || value === undefined || Number.isNaN(value)) return series;
    series.push(value);
    while (series.length > HISTORY) series.shift();
    return series;
}

function renderVerdict(verdict) {
    const box = $('verdict');
    if (!box) return;
    box.className = 'verdict ' + (verdict.level || 'unknown');
    const tag = box.querySelector('.tag');
    const label = { ok: '正常', warn: '注意', bad: '瓶颈', unknown: '采样中' };
    if (tag) tag.textContent = label[verdict.level] || '采样中';
    setText('verdict-text', verdict.text || '');
}

function renderHost(host) {
    const cpu = host.cpu || {};
    const mem = host.mem || {};
    const disk = host.disk || {};
    const thermal = host.thermal || {};

    setText('cpu-big', fmtPct(cpu.busy_pct));
    const cores = cpu.cores || [];
    setText('cpu-sub', (host.cpus ? host.cpus + ' 核 · ' : '') +
        (cores.length ? cores.map((v) => Math.round(v) + '%').join(' / ') : '—'));

    setText('load-big', host.load_per_core === null || host.load_per_core === undefined
        ? '—' : host.load_per_core.toFixed(2));
    const load = host.load || {};
    setText('load-sub', '1/5/15 分钟 ' + [load.load1, load.load5, load.load15]
        .map((v) => (v === undefined ? '—' : v.toFixed(2))).join(' / '));

    setText('mem-big', fmtPct(mem.used_pct));
    setText('mem-sub', [mem.available_mb === undefined || mem.available_mb === null
        ? '—' : mem.available_mb + ' MB 可用',
        mem.total_mb ? mem.total_mb + ' MB 总量' : '',
        mem.swap_used_mb ? '交换 ' + mem.swap_used_mb + ' MB' : '']
        .filter(Boolean).join(' · '));

    setText('disk-big', fmtBytes(disk.write_bps) + '/s');
    setText('disk-sub', '读 ' + fmtBytes(disk.read_bps) + '/s · 忙 ' + fmtPct(disk.busy_pct));

    setText('temp-big', thermal.max_c === undefined ? '—' : thermal.max_c.toFixed(1) + '°C');
    setText('temp-sub', thermal.freq_mhz === undefined ? '—'
        : Math.round(thermal.freq_mhz) + ' MHz' +
          (thermal.cores_online ? ' × ' + thermal.cores_online : ''));

    push(history.cpu, cpu.busy_pct);
    push(history.load, host.load_per_core);
    push(history.mem, mem.used_pct);
    push(history.disk, disk.busy_pct);
    spark('spark-cpu', history.cpu, 'rgb(245,166,35)');
    spark('spark-load', history.load, 'rgb(140,190,255)');
    spark('spark-mem', history.mem, 'rgb(47,191,95)');
    spark('spark-disk', history.disk, 'rgb(232,181,41)');

    const tbody = $('net-table').querySelector('tbody');
    const rows = host.net || [];
    tbody.innerHTML = rows.length
        ? rows.map((flow) => '<tr><td>' + flow.name + '</td><td class="num">' +
            fmtBytes(flow.rx_bps) + '/s</td><td class="num">' + fmtBytes(flow.tx_bps) +
            '/s</td></tr>').join('')
        : '<tr><td colspan="3" style="color:var(--muted)">没有可显示的业务接口</td></tr>';
}

function renderProcs(processes) {
    const tbody = $('proc-table').querySelector('tbody');
    const rows = processes || [];
    if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="4" style="color:var(--muted)">这段时间没有进程在吃 CPU</td></tr>';
        return;
    }
    const top = Math.max(1, ...rows.map((row) => row.cpu_pct));
    tbody.innerHTML = rows.map((row) =>
        '<tr><td>' + row.name + '</td><td class="num">' + row.pid +
        '</td><td class="num">' + row.cpu_pct.toFixed(1) + '%' +
        '</td><td><div class="bar"><i style="width:' +
        Math.min(100, (row.cpu_pct / top) * 100).toFixed(0) + '%"></i></div></td></tr>').join('');
}

function renderService(service) {
    const audio = service.audio || {};
    const clients = service.clients || {};
    const sessions = service.sessions || {};
    const listeners = sessions.listeners || {};
    const operators = sessions.operators || {};
    const pairs = [
        ['版本', service.version || '—'],
        ['型号', service.model || '—'],
        ['电台', service.radio_connected ? '已连接' : '未连接'],
        ['运行', service.uptime_s === null || service.uptime_s === undefined
            ? '—' : Math.round(service.uptime_s / 60) + ' 分钟'],
        ['页面连接', ['radio', clients.radio || 0, 'spectrum', clients.spectrum || 0,
            'audio_rx', clients.audio_rx || 0, 'audio_tx', clients.audio_tx || 0].join(' ')],
        ['会话', '操作 ' + (operators.sockets || 0) + ' · 收听 ' + (listeners.sockets || 0)],
        ['TX 队列', (audio.queued === undefined ? '—' : audio.queued + ' 帧') +
            (audio.queue_drops ? ' · 丢弃 ' + audio.queue_drops : '')],
        ['RX 静音', audio.rx_silent_s === undefined ? '—' : audio.rx_silent_s + ' s'],
        ['事件循环', (service.event_loop_lag_ms || 0) + ' ms（峰值 ' +
            (service.event_loop_peak_ms || 0) + ' ms）'],
    ];
    $('service-kv').innerHTML = pairs
        .map((pair) => '<dt>' + pair[0] + '</dt><dd>' + pair[1] + '</dd>').join('');
    setText('lag-big', (service.event_loop_lag_ms || 0) + ' ms');
    push(history.lag, service.event_loop_lag_ms);
    spark('spark-lag', history.lag, 'rgb(232,86,63)');
}

function stamp() {
    const now = new Date();
    return now.toLocaleTimeString('zh-CN', { hour12: false });
}

async function tick() {
    try {
        const response = await fetch(URL_BASE + '/api/perf', { credentials: 'same-origin' });
        if (response.status === 401) {
            location.href = URL_BASE + '/login';
            return;
        }
        if (response.status === 403) {
            setText('verdict-text', '这个会话没有权限看性能数据（只听会话看不到）');
            paused = true;
            if (timer) clearInterval(timer);
            return;
        }
        if (!response.ok) throw new Error('HTTP ' + response.status);
        const data = await response.json();
        failures = 0;
        renderVerdict(data.verdict || {});
        renderHost(data.host || {});
        renderProcs((data.host || {}).processes);
        renderService(data.service || {});
        setText('head-meta', stamp());
    } catch (error) {
        failures += 1;
        setText('head-meta', '读取失败 ' + failures + ' 次（' + error.message + '）');
        if (failures >= 3) {
            renderVerdict({ level: 'unknown', text: '取不到性能数据：服务可能正在重启' });
        }
    }
}

function setPaused(next) {
    paused = next;
    const button = $('btn-pause');
    if (button) {
        button.textContent = paused ? '继续' : '暂停';
        button.classList.toggle('on', paused);
    }
    if (timer) {
        clearInterval(timer);
        timer = null;
    }
    if (!paused) {
        tick();
        timer = setInterval(tick, POLL_MS);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    $('btn-pause').addEventListener('click', () => setPaused(!paused));
    tick();
    timer = setInterval(tick, POLL_MS);
});
