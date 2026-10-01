// Cloud Hub onboarding from the settings dialog.
//
// The tenant-side story used to be: open a terminal, paste a token and a one-time secret, run a
// script, then hand the operator a root command. This is the same thing with three buttons - fill
// in a callsign, apply, and wait; once the operator approves, the app signs its certificate,
// enrolls, writes its own configuration and starts the tunnel.
(() => {
    var dialog = null;
    var poll = null;

    function el(id) {
        return document.getElementById(id);
    }

    function api(path, body) {
        // Same prefix mechanism the rest of the page uses (settings_manager.js basePath).
        var url = window.FT710Settings && FT710Settings.url ? FT710Settings.url(path) : path;
        var opts = {
            method: body ? 'POST' : 'GET',
            headers: { 'Content-Type': 'application/json' },
        };
        if (body) opts.body = JSON.stringify(body);
        return fetch(url, opts).then((r) => r.json().then((j) => ({ ok: r.ok, j: j })));
    }

    function show(msg, kind) {
        var box = el('cloud-msg');
        if (!box) return;
        box.textContent = msg || '';
        box.style.color = kind === 'error' ? '#ff6b6b' : kind === 'ok' ? '#4ade80' : '#9ca3af';
    }

    function render(state) {
        var applied = !!(state.callsign && state.has_token);
        el('cloud-form').style.display = applied ? 'none' : 'block';
        el('cloud-pending').style.display = applied && !state.connected ? 'block' : 'none';
        el('cloud-done').style.display = state.connected ? 'block' : 'none';
        if (applied) {
            el('cloud-callsign-shown').textContent = state.callsign;
        }
        if (state.connected) {
            var link = el('cloud-entry');
            link.href = state.entry;
            link.textContent = state.entry;
            el('cloud-cert').textContent = state.cert || '—';
            el('cloud-tunnel').textContent = state.tunnel_running
                ? '已连接'
                : state.tunnel_error || '未运行（稍候会自动重试）';
            el('cloud-tunnel').style.color = state.tunnel_running ? '#4ade80' : '#fbbf24';
        }
        el('cloud-portal').textContent = state.portal || '';
    }

    function refresh(quiet) {
        return api('/api/cloud/state').then((r) => {
            if (!r.ok) {
                show('读取状态失败', 'error');
                return;
            }
            render(r.j);
            if (r.j.callsign && r.j.has_token && !r.j.connected) {
                api('/api/cloud/refresh').then((rr) => {
                    if (rr.j && rr.j.connected) {
                        show('已接入 ✓ 正在重启以启用证书…', 'ok');
                        setTimeout(() => {
                            location.reload();
                        }, 2500);
                    } else if (!quiet) {
                        show(
                            rr.j && rr.j.error
                                ? rr.j.error
                                : '等待运维批准（状态：' +
                                      ((rr.j && rr.j.status) || 'applied') +
                                      '）'
                        );
                    }
                });
            }
        });
    }

    function open() {
        if (!dialog) return;
        dialog.style.display = 'flex';
        show('');
        refresh();
        if (poll) clearInterval(poll);
        poll = setInterval(() => {
            refresh(true);
        }, 20000);
    }

    function close() {
        if (dialog) dialog.style.display = 'none';
        if (poll) {
            clearInterval(poll);
            poll = null;
        }
    }

    function submitApply() {
        var callsign = (el('cloud-callsign').value || '').trim();
        var contact = (el('cloud-contact').value || '').trim();
        var secretField = el('cloud-secret');
        var secret = secretField ? (secretField.value || '').trim() : '';
        if (!callsign) {
            show('请填呼号', 'error');
            return;
        }
        show(secret ? '接入中…' : '提交中…');
        api('/api/cloud/apply', { callsign: callsign, contact: contact, secret: secret }).then((r) => {
            if (!r.ok) {
                show((r.j && r.j.error) || '提交失败', 'error');
                return;
            }
            if (r.j && r.j.connected) {
                show('已接入 ✓ 正在重启以启用证书…', 'ok');
                setTimeout(() => {
                    location.reload();
                }, 2500);
                return;
            }
            show('已提交 ✓ 等运维批准后这里会自动继续', 'ok');
            refresh();
        });
    }

    function init() {
        dialog = el('cloud-dialog');
        if (!dialog) return;
        dialog.querySelector('#cloud-close').addEventListener('click', close);
        dialog.querySelector('#cloud-apply').addEventListener('click', submitApply);
        dialog.querySelector('#cloud-refresh').addEventListener('click', () => {
            refresh();
        });
        document.querySelectorAll("[data-action='cloud-hub']").forEach((elm) => {
            elm.addEventListener('click', (e) => {
                e.preventDefault();
                open();
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
