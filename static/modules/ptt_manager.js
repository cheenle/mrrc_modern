/**
 * FT-710 PTT Manager
 * ==================
 * PTT state machine with safety watchdog.
 * Serial CAT is reliable, but we still verify TX state and handle
 * edge cases (touch events, browser tab close, etc.).
 */

(function() {
    'use strict';

    let pttActive = false;
    let tuneActive = false;
    let pttVerifyTimer = null;

    // ── PTT Safety Watchdog ──────────────────────────────────────
    // After releasing PTT, verify the radio actually returned to RX.
    // If not, force-resend TX0; up to 3 times.

    function startPTTWatchdog() {
        stopPTTWatchdog();
        let retries = 0;
        const maxRetries = 3;

        pttVerifyTimer = setInterval(function() {
            if (!pttActive && !tuneActive) {
                // We expect RX state
                if (typeof radioState !== 'undefined' && radioState.tx_status !== 0) {
                    retries++;
                    console.warn('PTT watchdog: TX still active after release! Retry', retries);
                    if (typeof sendCommand === 'function') {
                        sendCommand('ptt', false);
                    }
                    if (retries >= maxRetries) {
                        console.error('PTT watchdog: max retries reached. Forcing RX locally.');
                        if (typeof radioState !== 'undefined') {
                            radioState.tx_status = 0;
                            radioState.is_transmitting = false;
                        }
                        if (typeof renderPTTState === 'function') renderPTTState();
                        if (typeof renderStatusBar === 'function') renderStatusBar();
                        stopPTTWatchdog();
                    }
                } else {
                    // All good
                    stopPTTWatchdog();
                }
            }
        }, 500);
    }

    function stopPTTWatchdog() {
        if (pttVerifyTimer) {
            clearInterval(pttVerifyTimer);
            pttVerifyTimer = null;
        }
    }

    // ── Public API ────────────────────────────────────────────────
    window.PTTManager = {
        get isActive() { return pttActive || tuneActive; },

        pttStart: function() {
            pttActive = true;
            if (typeof handlePTTStart === 'function') {
                handlePTTStart();
            }
            stopPTTWatchdog(); // Stop any existing watchdog
        },

        pttEnd: function() {
            pttActive = false;
            if (typeof handlePTTEnd === 'function') {
                handlePTTEnd();
            }
            startPTTWatchdog();
        },

        tuneStart: function() {
            tuneActive = true;
            if (typeof handleTuneStart === 'function') {
                handleTuneStart();
            }
        },

        tuneEnd: function() {
            tuneActive = false;
            if (typeof handleTuneEnd === 'function') {
                handleTuneEnd();
            }
            startPTTWatchdog();
        },

        forceRX: function() {
            pttActive = false;
            tuneActive = false;
            if (typeof sendCommand === 'function') {
                sendCommand('ptt', false);
            }
            stopPTTWatchdog();
        },

        // Server told us another client holds the key (our ptt:false was
        // ignored by the control-plane arbitration). Stand down: retrying
        // would keep fighting the client that is actually transmitting, and
        // the forced-local-RX below would desync our UI from the radio.
        cancelWatchdog: function() {
            stopPTTWatchdog();
        },
    };

    // ── Safety: force RX on page hide (mobile app switch) ─────────
    window.addEventListener('pagehide', function() {
        if (pttActive || tuneActive) {
            if (typeof sendCommand === 'function') {
                sendCommand('ptt', false);
            }
        }
    });

    // NOTE: tab close / reload is covered server-side — when the last
    // control WebSocket disconnects during TX, the server forces RX
    // (server.py ws_radio finally block). The previous sendBeacon here
    // POSTed to /WSradio, a WebSocket-only endpoint, so it never worked.

    console.log('PTT Manager initialized');
})();
