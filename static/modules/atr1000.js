/**
 * FT-710 ATR1000 External Tuner Module (optional feature)
 * ========================================================
 * Connects to /WSatr1000 and renders the compact ATR meter row
 * (power / SWR / relay L-C / tuning state) plus the ATR TUNE button
 * (server-side tune assist: TX2 carrier → full tune → SWR compare →
 * rollback on no improvement).
 *
 * Fully inert unless the server reports atr1000Enabled in fullState:
 * init(false) never opens a socket and never unhides the row.
 */

(function() {
    'use strict';

    let ws = null;
    let enabled = false;
    let reconnectDelay = 1000;
    let reconnectTimer = null;
    let tuneInProgress = false;

    function $(id) { return document.getElementById(id); }

    // ── Rendering ─────────────────────────────────────────────────
    function showRow(show) {
        const row = $('atr-row');
        if (row) row.hidden = !show;
    }

    function setBar(id, pct) {
        const el = $(id);
        if (el) el.style.width = Math.max(0, Math.min(100, pct)) + '%';
    }

    function render(state) {
        if (!state.connected) {
            showRow(true);
            const lc = $('atr-lc-val');
            if (lc) lc.textContent = '离线';
            const btn = $('btn-atr-tune');
            if (btn) btn.disabled = true;
            return;
        }
        showRow(true);

        const pwr = state.power || 0;
        const swr = state.swr || 0;
        const pwrVal = $('atr-pwr-val');
        if (pwrVal) pwrVal.textContent = pwr.toFixed(0);
        setBar('atr-pwr-bar', pwr / 120 * 100);  // 120W full scale, like CAT meter

        const swrVal = $('atr-swr-val');
        if (swrVal) swrVal.textContent = swr > 0 ? swr.toFixed(1) : '-';
        setBar('atr-swr-bar', Math.min(swr, 5) / 5 * 100);  // SWR 5 = full scale

        const lc = $('atr-lc-val');
        if (lc) {
            const mode = state.sw === 1 ? 'CL' : 'LC';
            lc.textContent = mode + ' L=' + (state.ind || 0) + ' C=' + (state.cap || 0) +
                (state.tuning ? ' ⋯' : '');
        }

        const btn = $('btn-atr-tune');
        if (btn) {
            btn.disabled = false;
            btn.classList.toggle('tuning', !!(state.tuning || tuneInProgress));
            btn.textContent = (state.tuning || tuneInProgress) ? '···' : 'TUNE';
        }
    }

    // ── WebSocket ─────────────────────────────────────────────────
    function connect() {
        if (!enabled) return;
        const token = FT710Settings.getAuthToken();
        if (!token) return;
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        try {
            ws = new WebSocket(proto + '//' + location.host + '/WSatr1000?token=' + token);
        } catch(e) {
            scheduleReconnect();
            return;
        }

        ws.onopen = function() {
            reconnectDelay = 1000;
        };
        ws.onmessage = function(ev) {
            let msg;
            try { msg = JSON.parse(ev.data); } catch(e) { return; }
            if (msg.type === 'atrState') {
                render(msg);
            } else if (msg.type === 'atrTuneResult') {
                onTuneResult(msg);
            } else if (msg.type === 'error' && typeof showToast === 'function') {
                showToast(msg.message || 'ATR1000 error');
            }
        };
        ws.onclose = function() {
            ws = null;
            showRow(enabled);  // keep row visible but mark offline-ish state
            const btn = $('btn-atr-tune');
            if (btn) { btn.disabled = true; btn.classList.remove('tuning'); btn.textContent = 'TUNE'; }
            tuneInProgress = false;
            scheduleReconnect();
        };
        ws.onerror = function() {
            if (ws) ws.close();
        };
    }

    function scheduleReconnect() {
        if (!enabled || reconnectTimer) return;
        reconnectTimer = setTimeout(function() {
            reconnectTimer = null;
            connect();
        }, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 30000);
    }

    function send(obj) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(obj));
            return true;
        }
        return false;
    }

    // ── Tune assist ───────────────────────────────────────────────
    function onTuneResult(msg) {
        if (msg.phase === 'start') {
            tuneInProgress = true;
        } else {
            tuneInProgress = false;
            if (typeof showToast === 'function') {
                let text;
                if (msg.phase === 'skipped') {
                    text = 'ATR: SWR ' + (msg.swr_before || '?') + ' 已达标,无需调谐';
                } else if (msg.phase === 'success') {
                    text = 'ATR 调谐完成: SWR ' + msg.swr_before + ' → ' + msg.swr_after;
                } else if (msg.phase === 'rollback') {
                    text = 'ATR 调谐无改善,已回滚 (SWR ' + msg.swr_before + ')';
                } else {
                    text = 'ATR 调谐失败: ' + (msg.message || msg.phase);
                }
                showToast(text);
            }
        }
        const btn = $('btn-atr-tune');
        if (btn) {
            btn.classList.toggle('tuning', tuneInProgress);
            btn.textContent = tuneInProgress ? '···' : 'TUNE';
        }
    }

    function wireButton() {
        const btn = $('btn-atr-tune');
        if (!btn) return;
        btn.addEventListener('click', function() {
            if (tuneInProgress) return;  // server-side sequence; no cancel for now
            if (send({type: 'atrTune'})) {
                tuneInProgress = true;
                btn.classList.add('tuning');
                btn.textContent = '···';
            } else if (typeof showToast === 'function') {
                showToast('ATR1000 未连接');
            }
        });
    }

    // ── Init (called from ft710_main.js on every fullState) ───────
    let wired = false;
    function init(atrEnabled) {
        enabled = atrEnabled === true;
        if (!wired) {
            wired = true;
            wireButton();
        }
        if (!enabled) {
            showRow(false);
            return;
        }
        if (!ws) connect();  // idempotent across repeated fullState
    }

    window.ATR1000 = { init: init };
})();
