(() => {
    function descriptor(dlg) {
        var model = dlg.querySelector('#cs-model').value;
        return (
            (dlg._connectionCatalog && dlg._connectionCatalog[model]) || {
                transport: 'serial',
                host_required: false,
            }
        );
    }

    function update(dlg) {
        var network = descriptor(dlg).transport === 'network';
        var flex = isFlex(dlg);
        resyncHost(dlg);
        dlg.querySelector('#cs-serial-fields').hidden = network;
        dlg.querySelector('#cs-audio-fields').hidden = network;
        dlg.querySelector('#cs-network-fields').hidden = !network;
        // A FlexRadio needs no address at all in the normal case; saying so
        // here is cheaper than a support call about a blank required field.
        dlg.querySelector('#cs-sunsdr-host').placeholder = flex
            ? '可留空：自动发现（UDP 4992）'
            : '例如 192.168.16.200';
        dlg.querySelector('#cs-sunsdr-local-host').parentElement.hidden = flex;
    }

    // The host field is one box shared by two models, and their addresses are
    // not interchangeable: a SunSDR2 DX at 192.168.16.200 handed to the flex
    // backend is a guaranteed TCP-4992 timeout, because no Flex lives at the
    // SunSDR's address. That is exactly what happened on 2026-11-16 (support
    // bundle 20261116-000702-36fa): the operator switched the dropdown from
    // SunSDR2 DX to FlexRadio, the form still held the SunSDR host, and the save
    // wrote it as MRRC_FLEX_HOST — the radio then never came up. So the field
    // is re-derived from the *selected* model's own saved value whenever that
    // selection changes; a host typed for the previous model is not carried
    // over.
    function resyncHost(dlg) {
        var saved = dlg._savedConnection;
        if (!saved) return;
        var flex = isFlex(dlg);
        dlg.querySelector('#cs-sunsdr-host').value =
            (flex ? saved.flex_host : saved.sunsdr_host) || '';
        dlg.querySelector('#cs-sunsdr-local-host').value = saved.sunsdr_local_host || '';
    }

    function isFlex(dlg) {
        return dlg.querySelector('#cs-model').value === 'flex';
    }

    function sync(dlg, current) {
        dlg._connectionCatalog = current.connections || {};
        dlg._savedConnection = current;
        // FlexRadio has its own host field because an empty value is valid
        // there (the box discovers the radio on UDP 4992), while a SunSDR
        // without an address cannot be reached at all.
        var flex = current.radio_model === 'flex';
        dlg.querySelector('#cs-sunsdr-host').value =
            (flex ? current.flex_host : current.sunsdr_host) || '';
        dlg.querySelector('#cs-sunsdr-local-host').value = current.sunsdr_local_host || '';
        update(dlg);
    }

    function fields(dlg) {
        var out = {};
        if (descriptor(dlg).transport !== 'network') return out;
        var host = dlg.querySelector('#cs-sunsdr-host').value.trim();
        if (isFlex(dlg)) {
            // Empty is a valid answer: auto-discovery.
            out.flex_host = host;
            return out;
        }
        out.sunsdr_host = host;
        out.sunsdr_local_host = dlg.querySelector('#cs-sunsdr-local-host').value.trim();
        return out;
    }

    function fillSelect(sel, items, key, current) {
        sel.replaceChildren();
        var auto = document.createElement('option');
        auto.value = '';
        auto.textContent = '自动（推荐）';
        sel.appendChild(auto);
        var seen = {};
        items.forEach((it) => {
            var v = it[key];
            if (!v || seen[v]) return;
            seen[v] = 1;
            var opt = document.createElement('option');
            opt.value = v;
            // A loopback/virtual endpoint (Windows' 主声音捕获驱动程序 / 立体声混音)
            // opens fine and carries no radio audio, so picking one makes RX
            // silent with nothing in the log. The server flags them; say so here.
            opt.textContent = it.loopback ? v + '（电脑自带，非电台）' : v;
            if (current && v.indexOf(current) !== -1) opt.selected = true;
            sel.appendChild(opt);
        });
    }

    function post(path, body, done) {
        fetch(window.apiUrl(path), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: body ? JSON.stringify(body) : '{}',
        })
            .then((r) => r.json().then((j) => ({ ok: r.ok, j: j })))
            .then(done)
            .catch(() => {
                done({ ok: false, j: { error: 'network' } });
            });
    }

    function waitAndReload(dlg) {
        var msg = dlg.querySelector('#cs-msg');
        msg.textContent = '已保存，正在重启…';
        var t = setInterval(() => {
            fetch(window.apiUrl('/api/health')).catch(() => {
                clearInterval(t);
                var t2 = setInterval(() => {
                    fetch(window.apiUrl('/api/health'))
                        .then(() => {
                            clearInterval(t2);
                            window.location.reload();
                        })
                        .catch(() => {});
                }, 1200);
            });
        }, 800);
    }

    function openDlg(dlg) {
        dlg.style.display = 'flex';
        dlg.querySelector('#cs-msg').textContent = '';
        Promise.all([
            fetch(window.apiUrl('/api/devices')).then((r) => (r.ok ? r.json() : null)),
            fetch(window.apiUrl('/api/setup')).then((r) => (r.ok ? r.json() : null)),
        ]).then((rs) => {
            var dev = rs[0];
            var cur = rs[1];
            if (!dev || !cur) return;
            dlg.querySelector('#cs-model').value = cur.radio_model || 'ft710';
            fillSelect(dlg.querySelector('#cs-port'), dev.serial_ports, 'device', cur.serial_port);
            fillSelect(
                dlg.querySelector('#cs-audio-rx'),
                dev.audio_rx,
                'name',
                cur.audio_rx_device
            );
            fillSelect(
                dlg.querySelector('#cs-audio-tx'),
                dev.audio_tx,
                'name',
                cur.audio_tx_device
            );
            sync(dlg, cur);
        });
    }

    function init() {
        var dlg = document.getElementById('conn-dialog');
        if (!dlg) return;
        dlg.querySelector('#cs-model').addEventListener('change', () => {
            update(dlg);
        });
        document.getElementById('cs-save').addEventListener('click', () => {
            var pwd = dlg.querySelector('#cs-password').value;
            if (pwd && pwd.length < 8) {
                dlg.querySelector('#cs-msg').textContent = '密码至少 8 位';
                return;
            }
            var body = {
                radio_model: dlg.querySelector('#cs-model').value,
                serial_port: dlg.querySelector('#cs-port').value,
                audio_rx_device: dlg.querySelector('#cs-audio-rx').value,
                audio_tx_device: dlg.querySelector('#cs-audio-tx').value,
            };
            Object.assign(body, fields(dlg));
            if (pwd) body.password = pwd;
            post('/api/setup', body, (res) => {
                if (res.ok) {
                    waitAndReload(dlg);
                } else {
                    dlg.querySelector('#cs-msg').textContent =
                        '保存失败：' + ((res.j && res.j.error) || '未知错误');
                }
            });
        });
        document.getElementById('cs-restart').addEventListener('click', () => {
            post('/api/restart', null, (res) => {
                if (res.ok) {
                    waitAndReload(dlg);
                } else {
                    dlg.querySelector('#cs-msg').textContent =
                        '重启失败：' + ((res.j && res.j.error) || '未知错误');
                }
            });
        });
        document.getElementById('cs-close').addEventListener('click', () => {
            dlg.style.display = 'none';
        });
        document.querySelectorAll("[data-action='conn-settings']").forEach((el) => {
            el.addEventListener('click', (e) => {
                e.preventDefault();
                openDlg(dlg);
            });
        });
    }

    window.MRRCConnectionSettings = { sync: sync, fields: fields, update: update };
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
