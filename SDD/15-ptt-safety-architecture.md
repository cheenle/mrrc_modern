# 15. PTT Safety Architecture (ART 0535)

> **Principle**: Release is more safety-critical than keying. A lost PTT release command can leave a radio transmitting indefinitely — causing harmful interference, overheating the radio, and violating spectrum regulations. Every layer must have an independent path to force RX.

## 15.1 Safety Model — Defense in Depth

The MRRC FT-710 PTT safety architecture provides **7 independent layers of defense** against stuck-TX scenarios.

| Layer | Location | Mechanism | Failure Mode Caught |
|-------|----------|-----------|---------------------|
| 1 | Browser UX | Touch-and-hold: release on `mouseup`/`touchend`/`mouseleave`/`touchcancel` | User intentionally releasing PTT |
| 2 | Browser → Server | `sendCommand('ptt', false)` over `/WSradio` | Normal network path |
| 3 | Browser | PTT Watchdog: 500ms interval checks `radioState.tx_status`; up to 3 retries | TX0 command or state broadcast lost |
| 4 | Server | Dead-man switch: force `TX0;` when the last control client disconnects during TX, or when the TX-audio owner disconnects during TX. Uplink ownership follows the PTT-ing client (token-matched on key-up) and is promoted to a remaining client on owner disconnect — an idle/zombie owner can no longer silently mute the uplink | Browser crash, tab close, network loss, audio socket drop, multi-client ownership |
| 5 | Browser | `beforeunload` → `navigator.sendBeacon()` with TX0 | Tab/browser close during TX |
| 6 | Browser | `pagehide` → `sendCommand('ptt', false)` | Mobile app switch / backgrounding |
| 7 | Server + Browser | Stop TX audio stream; clear audio queue; `wsAudioTX.send('s:')` | Audio continuing to feed radio after release |

**Removed layer — Triple TX0 Verify (removed in V1.2, 2026-07-08):** earlier releases queried `TX;` three times at 200ms intervals after every release and re-sent `TX0;` on non-zero. This added ~600ms to every release, and field observation showed the radio obeys `TX0;` on the first write (fire-and-forget). Stuck-keyup detection is now covered by the server-side TX-status poll (500ms) feeding the browser PTT watchdog (Layer 3).

## 15.2 Layer Details

### Layer 1: Touch-and-Hold UX

PTT only transmits while the user is actively touching the PTT button:

```javascript
pttBtn.addEventListener('mousedown', handlePTTStart);
pttBtn.addEventListener('touchstart', function(e) { e.preventDefault(); handlePTTStart(); });
pttBtn.addEventListener('mouseup', handlePTTEnd);
pttBtn.addEventListener('touchend', function(e) { e.preventDefault(); handlePTTEnd(); });
pttBtn.addEventListener('mouseleave', handlePTTEnd);
pttBtn.addEventListener('touchcancel', handlePTTEnd);
```

### Layer 2: Normal WebSocket Command

```javascript
function handlePTTEnd() {
    sendCommand('ptt', false);
    // ...
}
```

Server receives `{"type":"set","field":"ptt","value":false}` → sends CAT `TX0;` (fire-and-forget; see removal note in §15.1).

### Layer 3: PTT Watchdog (Browser)

```javascript
// ptt_manager.js
pttVerifyTimer = setInterval(function() {
    if (!pttActive && !tuneActive) {
        if (radioState.tx_status !== 0) {
            retries++;
            sendCommand('ptt', false);
            if (retries >= 3) {
                // Force locally
                radioState.tx_status = 0;
                radioState.is_transmitting = false;
                renderPTTState();
                renderStatusBar();
                stopPTTWatchdog();
            }
        } else {
            stopPTTWatchdog();
        }
    }
}, 500);
```

The watchdog's input is the server-side TX-status poll (500ms), which keeps `radioState.tx_status` fresh even if the release command's state update was lost.

### Layer 4: Dead-Man Switch (Server)

```python
# In /WSradio disconnect handler:
if not ctrl_clients and radio.is_transmitting and cat and cat.connected:
    logger.warning("Last client disconnected during TX! Forcing RX.")
    await cat.set_ptt(False)
    radio.update(tx_status=0)
    if audio:
        audio.stop_tx()
```

**ATR1000 tune assist (server-side TX2 keying):** the optional ATR tune assist (`_atr_tune_assist()`, §9.8) keys a TX2 carrier server-side for up to 45 s (ATR_TUNE deadline). Safety: the carrier drop is guaranteed by a `finally` block on every exit path (skip/success/rollback/error); the Layer 4 last-client-disconnect dead-man switch still applies while the carrier is up; an SWR≤1.6 gate skips tuning entirely; relays roll back when SWR does not improve. All ATR I/O runs in its own asyncio task — never on the audio path.

### Layer 5: Beforeunload Beacon

```javascript
window.addEventListener('beforeunload', function() {
    if (pttActive || tuneActive) {
        if (navigator.sendBeacon) {
            const blob = new Blob([
                JSON.stringify({type:'set', field:'ptt', value:false})
            ], {type: 'application/json'});
            navigator.sendBeacon('/WSradio', blob);
        }
    }
});
```

### Layer 6: Pagehide Handler

```javascript
window.addEventListener('pagehide', function() {
    if (pttActive || tuneActive) {
        sendCommand('ptt', false);
    }
});
```

### Layer 7: TX Audio Stream Stop

Server-side on PTT release:
```python
if audio:
    audio.stop_tx()
```

Browser-side on PTT release:
```javascript
function stopTXAudio() {
    txAudioRunning = false;
    // ...
    if (wsAudioTX && wsAudioTX.readyState === WebSocket.OPEN) {
        wsAudioTX.send('s:');  // Flush server TX queue
    }
}
```

## 15.3 Safety Flow Diagram

```text
Normal Release Path:
  PTT Button Release (any trigger)
    → handlePTTEnd()
      → sendCommand('ptt', false)          [Layers 1+2]
      → stopTXAudio() + wsAudioTX.send('s:') [Layer 7]
      → Server: CAT TX0; (fire-and-forget)
      → Server: TX-status poll (500ms) tracks RX
      → PTT Watchdog starts                 [Layer 3]

Emergency Paths:
  Browser crash / tab close:
    → beforeunload → sendBeacon TX0;        [Layer 5]
    → pagehide → sendCommand TX0;           [Layer 6]
    → WS disconnect → server dead-man switch [Layer 4]
    → Server stops TX audio                 [Layer 7]

  Network loss during TX:
    → WS disconnect → server dead-man switch [Layer 4]
    → Server stops TX audio                 [Layer 7]
    → Radio returns to RX (CAT timeout)
```

## 15.4 Testing the Safety Layers

| Test | Expected Behavior | Layers Tested |
|------|-------------------|---------------|
| Normal PTT press/release | TX → RX within 200ms | 1, 2 |
| Force-close browser during TX | Radio returns to RX | 4, 5, 6 |
| Network packet loss during release | Browser watchdog re-sends `ptt=false` (state via 500ms TX-status poll) | 3 |
| Tab background on iOS during TX | pagehide sends TX0 | 6 |
| Multiple rapid PTT toggles | Each release properly processed | All |
| Pull USB cable during TX | No software path can reach the radio; server marks CAT disconnected and forces RX state on reconnect | — (hardware) |
| Server kill -9 during TX | Radio returns to RX (CAT timeout, no polling) | Hardware |

## 15.5 Safety Design Principles

1. **Release is more important than keying.** Every release path must work even if the keying path is broken.
2. **No single point of failure.** 7 independent layers catch different failure modes.
3. **Fail safe, not fail dangerous.** If in doubt, force RX.
4. **Server has final authority.** Even if the browser is completely gone, the server forces RX (Layer 4).
5. **Verify, don't assume.** TX state is read back continuously via the 500ms TX-status poll, and the browser watchdog acts on any stuck-TX indication (Layer 3). The per-release triple readback was removed in V1.2 — see §15.1.
6. **Stop audio before RF.** Audio stream stops before the RF carrier drops, preventing hot-switching noise.
