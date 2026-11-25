/* 刷 MRRC-Passport（ESP32-C3）—— 盒子当烧录站。
 *
 * 页面只做三件事：把盒子知道的东西预填进表单、在刷写期间轮询进度、把 esptool 的
 * 输出原样给人看。它**不**决定能不能刷：端口白名单、固件 SHA、NVS 合法性全在
 * 服务端（`passport_flash.py`），这样离线也能测，页面改坏了不会变成刷坏一台板子。
 *
 * 前缀安全：页面可能挂在 /passport 或 www.vlsc.net/mrrc_modern/<呼号>/passport，
 * 所有请求都从 URL_BASE 拼出来（tests/test_path_prefix.py 会扫这个文件）。
 */
const URL_BASE = location.pathname.replace(/\/passport$/, '');

const $ = (id) => document.getElementById(id);
let polling = null;

function setState(text, kind) {
    const el = $('state');
    el.textContent = text;
    el.className = 'state' + (kind ? ' ' + kind : '');
}

async function api(path, options) {
    const response = await fetch(
        URL_BASE + path,
        Object.assign(
            {
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
            },
            options || {}
        )
    );
    if (response.status === 401 || response.status === 302) {
        // 只取同源 pathname：URL_BASE 来自 location.pathname，不是外部输入。
        location.href = new URL(URL_BASE + '/login', location.origin).pathname;
        throw new Error('需要登录');
    }
    let body = {};
    try {
        body = await response.json();
    } catch (e) {
        /* 非 JSON：交给调用方看状态码 */
    }
    return { ok: response.ok, status: response.status, body: body };
}

function renderEnv(state) {
    const rows = [];
    rows.push(['本机 esptool', state.esptool ? '有' : '没有 —— 需要 1.28.99 及以后的镜像']);
    rows.push(['固件目录', state.catalogUrl]);
    rows.push([
        '可选串口',
        (state.ports || []).map((p) => p.path + ' (' + p.kind + ')').join('、') || '没有',
    ]);
    $('env').innerHTML = rows
        .map(
            ([k, v]) =>
                '<span>' +
                k +
                '</span><b>' +
                String(v).replace(
                    /[<>&]/g,
                    (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' })[c]
                ) +
                '</b>'
        )
        .join('');
    $('env-note').textContent = state.esptool
        ? '刷写期间请勿拔线；整个过程约 30–60 秒。'
        : '本机没有 esptool，这台盒子暂时刷不了 —— 先升级镜像（pip 包随镜像发布）。';
    $('go').disabled = !state.esptool;
}

function renderPorts(ports) {
    const sel = $('port');
    const keep = sel.value;
    sel.replaceChildren();
    if (!ports.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = '没有发现设备（插上板子后点「重新识别」）';
        sel.appendChild(option);
        return;
    }
    for (const p of ports) {
        const option = document.createElement('option');
        option.value = p.path;
        option.textContent = p.path + (p.kind === 'acm' ? '（原生 USB）' : '（USB 转串口）');
        sel.appendChild(option);
    }
    if (keep && ports.some((p) => p.path === keep)) sel.value = keep;
}

function renderFirmware(list) {
    const sel = $('version');
    const keep = sel.value;
    sel.replaceChildren();
    if (!list || !list.length) {
        const option = document.createElement('option');
        option.value = '';
        option.textContent = '先点「读取目录」';
        sel.appendChild(option);
        return;
    }
    for (const f of list) {
        const option = document.createElement('option');
        option.value = f.version;
        option.textContent =
            f.version +
            ' · ' +
            Math.round(f.size / 1024) +
            ' KB' +
            (f.downloaded ? ' · 已校验' : '') +
            (f.notes ? ' · ' + f.notes : '');
        sel.appendChild(option);
    }
    if (keep && list.some((f) => f.version === keep)) sel.value = keep;
}

function renderJob(job) {
    if (!job) return;
    if (job.lines && job.lines.length) {
        $('log').textContent = job.lines.join('\n');
        $('log').scrollTop = $('log').scrollHeight;
    }
    if (job.state === 'running') {
        setState('刷写中…', 'run');
        $('go').disabled = true;
        return;
    }
    $('go').disabled = false;
    if (job.state === 'done') {
        setState('完成，配网已回读校验', 'ok');
    } else if (job.state === 'failed') {
        setState('失败：' + (job.reason || '未知原因'), 'bad');
    } else {
        setState('就绪');
    }
}

async function refreshState() {
    const { ok, body } = await api('/api/passport/state');
    if (!ok) {
        setState('读取失败', 'bad');
        return;
    }
    renderEnv(body);
    renderPorts(body.ports || []);
    renderFirmware(body.firmware || []);
    if (body.suggest) {
        if (!$('ssid').value) $('ssid').value = body.suggest.ssid || '';
        if (!$('host').value) $('host').value = body.suggest.host || '';
    }
    renderJob(body.job);
    if (body.job && body.job.state === 'running' && !polling) startPolling();
    if (body.job && body.job.state !== 'running') stopPolling();
}

function startPolling() {
    if (polling) return;
    polling = setInterval(refreshState, 1000);
}

function stopPolling() {
    if (!polling) return;
    clearInterval(polling);
    polling = null;
}

async function fetchCatalog() {
    setState('读取固件目录…');
    const { ok, body } = await api('/api/passport/firmware', { method: 'POST', body: '{}' });
    if (!ok) {
        setState('目录读取失败：' + ((body && body.reason) || '未知'), 'bad');
        return;
    }
    renderFirmware(body.builds || []);
    setState('目录已更新', 'ok');
    // 目录落在服务端，重新取一次 state 才能把「已下载」标记对齐。
    refreshState();
}

async function downloadFirmware() {
    const version = $('version').value;
    if (!version) {
        setState('先选一个固件版本', 'bad');
        return;
    }
    setState('下载并校验 ' + version + '…', 'run');
    const { ok, body } = await api('/api/passport/flash', {
        method: 'POST',
        body: JSON.stringify({
            port: '',
            version: version,
            ssid: '',
            pass: '',
            host: '',
            srv_pass: '',
        }),
    });
    // 空端口会被服务端拒绝；下载是在刷写里做的，所以这里只提示先选设备。
    if (!ok) setState('固件会在刷写时自动下载并校验（' + ((body && body.reason) || '') + '）');
}

async function go() {
    const payload = {
        port: $('port').value,
        version: $('version').value,
        ssid: $('ssid').value.trim(),
        pass: $('pass').value,
        host: $('host').value.trim(),
        srv_pass: $('srvpass').value,
    };
    if (!payload.port) {
        setState('没有选中设备', 'bad');
        return;
    }
    if (!payload.version) {
        setState('没有选中固件', 'bad');
        return;
    }
    if (!payload.ssid) {
        setState('Wi-Fi 名称不能为空', 'bad');
        return;
    }
    if (!payload.host) {
        setState('地址不能为空', 'bad');
        return;
    }
    setState('开始刷写…', 'run');
    $('log').textContent = '正在准备…';
    const { ok, body } = await api('/api/passport/flash', {
        method: 'POST',
        body: JSON.stringify(payload),
    });
    if (!ok) {
        setState('没有开始：' + ((body && body.reason) || '未知'), 'bad');
        return;
    }
    startPolling();
}

$('refresh').addEventListener('click', fetchCatalog);
$('download').addEventListener('click', downloadFirmware);
$('reprobe').addEventListener('click', refreshState);
$('go').addEventListener('click', go);

refreshState().catch((e) => setState('读取失败：' + e.message, 'bad'));
