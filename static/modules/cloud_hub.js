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
        return fetch(url, opts).then((r) =>
            r.json()
                .catch(() => ({
                    // 非 JSON 的响应几乎只有一种来源：这个地址被 SPA 回退用 index.html 应答了
                    // （基址算错、或未登录被 302 到登录页）。直接抛出去只会得到一句
                    // "Uncaught (in promise) SyntaxError"，既看不出是哪个地址、也看不出状态码
                    // —— 2026-10-03 就卡在这里。把地址和状态码带回界面。
                    error:
                        '接口 ' + url + ' 返回了非 JSON（HTTP ' + r.status + '）' +
                        (r.redirected ? '，并被重定向到 ' + r.url : ''),
                }))
                .then((j) => ({ ok: r.ok, j: j }))
        );
    }

    function show(msg, kind) {
        var box = el('cloud-msg');
        if (!box) return;
        box.textContent = msg || '';
        box.style.color = kind === 'error' ? '#ff6b6b' : kind === 'ok' ? '#4ade80' : '#9ca3af';
    }

    var lastState = null;

    var AUTO_LABELS = {
        idle: '还没申请',
        applied: '已提交，等待运维批准',
        verified: '已核验，等待分配入口',
        granted: '已批准，正在接入…',
        busy: '正在接入…',
        connected: '已接入',
        restarting: '已接入，正在重启以启用新证书',
        unreachable: '联系不上 hub（会一直重试）',
        'connect-failed': '接入失败（会一直重试）',
    };

    // 服务端自己在轮询（server.py 的 cloud-autoconnect 线程），所以这行显示的是**本机**
    // 最后一次询问的结果，而不是这个窗口的。它存在的理由：以前"从来没去问"和"问了还没批"
    // 在界面上完全一样，2026-10-03 实测 BG6LH 因此卡了几个小时没人能看出是哪一边在等。
    function renderAuto(auto) {
        var box = el('cloud-auto');
        if (!box) return;
        auto = auto || {};
        var status = auto.status || 'idle';
        var line = '自动接入：本机自己定时问 hub，不必守着这个窗口';
        if (auto.at) {
            var secs = Math.max(0, Math.round(Date.now() / 1000 - auto.at));
            var when = secs < 90 ? secs + ' 秒前' : Math.round(secs / 60) + ' 分钟前';
            line += '（上次询问 ' + when + '：' + (AUTO_LABELS[status] || status) + '）';
        }
        if (auto.error) line += ' — ' + auto.error;
        box.textContent = line;
    }

    function render(state) {
        lastState = state;
        var applied = !!(state.callsign && state.has_token);
        el('cloud-form').style.display = applied ? 'none' : 'block';
        el('cloud-pending').style.display = applied && !state.connected ? 'block' : 'none';
        el('cloud-done').style.display = state.connected ? 'block' : 'none';
        if (applied) {
            el('cloud-callsign-shown').textContent = state.callsign;
        }
        renderAuto(state.autoconnect);
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
        if (state.connected && state.cert_reload_required) {
            el('cloud-restart').style.display = 'inline-block';
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
                        // The certificate is signed during connect, and the running process still
                        // holds the one it started with, so the entry answers 502 until a restart.
                        show(
                            rr.j.cert_reload_required
                                ? '已接入 ✓ 还需重启应用以启用新证书（否则入口会 502）'
                                : '已接入 ✓',
                            rr.j.cert_reload_required ? 'ok' : 'ok'
                        );
                        if (rr.j.cert_reload_required) {
                            el('cloud-restart').style.display = 'inline-block';
                        }
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
        api('/api/cloud/apply', { callsign: callsign, contact: contact, secret: secret }).then(
            (r) => {
                if (!r.ok) {
                    show((r.j && r.j.error) || '提交失败', 'error');
                    return;
                }
                if (r.j && r.j.connected) {
                    show(
                        r.j.cert_reload_required
                            ? '已接入 ✓ 还需重启应用以启用新证书（否则入口会 502）'
                            : '已接入 ✓',
                        'ok'
                    );
                    if (r.j.cert_reload_required) {
                        el('cloud-restart').style.display = 'inline-block';
                    }
                    setTimeout(() => {
                        location.reload();
                    }, 2500);
                    return;
                }
                show('已提交 ✓ 等运维批准后这里会自动继续', 'ok');
                refresh();
            }
        );
    }

    function init() {
        dialog = el('cloud-dialog');
        if (!dialog) return;
        dialog.querySelector('#cloud-close').addEventListener('click', close);
        dialog.querySelector('#cloud-apply').addEventListener('click', submitApply);
        dialog.querySelector('#cloud-refresh').addEventListener('click', () => {
            refresh();
        });
        dialog.querySelector('#cloud-claim').addEventListener('click', () => {
            // The same claim the operator's one-time secret performs, reachable from the panel an
            // already-applied user actually sees. Without this they had a Refresh button and nowhere
            // to paste the secret - reported by a user on 1.24.7.
            var secret = (el('cloud-secret-pending').value || '').trim();
            if (!secret) {
                show('请填入运维给的登记口令', 'error');
                return;
            }
            if (!lastState || !lastState.callsign) {
                show('还没提交过申请', 'error');
                return;
            }
            show('接入中…');
            api('/api/cloud/apply', {
                callsign: lastState.callsign,
                contact: '',
                secret: secret,
            }).then((r) => {
                if (!r.ok) {
                    show((r.j && r.j.error) || '接入失败', 'error');
                    return;
                }
                if (r.j && r.j.connected) {
                    show(
                        r.j.cert_reload_required
                            ? '已接入 ✓ 还需重启应用以启用新证书（否则入口会 502）'
                            : '已接入 ✓',
                        'ok'
                    );
                    if (r.j.cert_reload_required) {
                        el('cloud-restart').style.display = 'inline-block';
                    }
                    setTimeout(() => {
                        location.reload();
                    }, 2500);
                    return;
                }
                show('口令已受理，状态：' + ((r.j && r.j.status) || '处理中'), 'ok');
                refresh();
            });
        });
        dialog.querySelector('#cloud-restart').addEventListener('click', () => {
            show('正在重启…页面会在几秒后自动回来');
            api('/api/cloud/restart', {}).then(() => {
                setTimeout(() => {
                    location.reload();
                }, 6000);
            });
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
