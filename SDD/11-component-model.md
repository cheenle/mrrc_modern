# 11. Component Model (ART 0515)

## 11.1 Component Inventory

| Component | Type | File | Responsibility |
|-----------|------|------|----------------|
| FastAPIApp | Backend core | `server.py` | Route registration, lifespan, auth middleware, static serving, all WebSockets |
| CatController | Backend core | `cat_controller.py` | Serial CAT protocol: connect, disconnect, send/query/set, priority set path for PTT/TUNE preemption; high-level FT-710 command helpers |
| RadioState | Backend core | `radio_state.py` | Dataclass with dirty-field change tracking; to_dict/to_dirty_dict serialization; from_sync_result deserialization; derived properties (mode_name, s_unit, band_name, filter_hz) |
| PollScheduler | Backend core | `poll_scheduler.py` | 7-task asyncio polling (IF/VFO/TX-status/TX-meters/settings/slow/watchdog), skip-on-command, cancel-aware preemption for priority CAT writes; watchdog re-runs scope init (`on_reconnected` hook) after reconnect |
| AudioHandler | Backend core | `audio_handler.py` | PyAudio device enumeration, RX capture stream (48kHz), TX playback stream (48kHz), Opus encode (via RxOpusEncoder), multi-layer audio device auto-detection (name + mono heuristic + full-duplex) |
| OpusCodec | Backend support | `opus_rx.py` | RxOpusEncoder (48kHz mono, 64kbps), TxOpusDecoder (48kHz mono); direct ctypes libopus bindings; bitrate via max_data_bytes cap |
| ScopeHandler | Backend core | `scope_handler.py` | Spectrum data container; update_from_scope_frame (real) and update_from_radio_state (synthetic); get_spectrum_binary for WS broadcast |
| ScopePipe | Backend core | `scope_pipe.py` | Standalone subprocess: FT4222 SPI init + read loop; frame sync; stdout binary frames + stderr STATUS diagnostics |
| ScopeFrame | Backend support | `scope_frame.py` | Shared frame parsing: parse_pipe_payload, WF_SIZE constant, quality metrics |
| ScopeLibraries | Backend support | `scope_libraries.py` | FTDI library discovery and SPI clock configuration |
| Config | Backend support | `config.py` | Mode tables (MODE_NUM_TO_NAME, MODE_NAME_TO_NUM), band definitions (BANDS), filter widths, S-meter calibration, constants |
| ATR1000Client | Backend support | `atr1000_client.py` | Optional asyncio client for networked ATR1000 tuner (frame protocol, reconnect/refresh, TX-no-SYNC, learning, throttled relay writes) |
| TunerStorage | Backend support | `atr1000_tuner.py` | LC-learning persistence (SWR-gated, atomic JSON) |
| COOPCOEPMiddleware | Backend support | `server.py` | Sets COOP:same-origin / COEP:credentialless for SharedArrayBuffer support |
| AuthMiddleware | Backend support | `server.py` | Cookie + query-param token validation; public path whitelist; redirect to /login |
| ControlWS | Backend core | `server.py` | `/WSradio` JSON message dispatch, state broadcast |
| RXAudioWS | Backend core | `server.py` | `/WSaudioRX` binary fan-out of tagged audio frames |
| TXAudioWS | Backend core | `server.py` | `/WSaudioTX` tagged mic frame ingress → decode → queue |
| SpectrumWS | Backend core | `server.py` | `/WSspectrum` binary fan-out of scope data |
| AudioRXLoop | Backend core | `server.py` | `_audio_rx_loop()` asyncio task: read PyAudio → encode → broadcast, 20ms cadence |
| AudioTXDrainLoop | Backend core | `server.py` | `_audio_tx_drain_loop()` asyncio task: drain PCM queue → PyAudio write, 10ms cadence |
| ScopeReadTask | Backend core | `server.py` | `_read_scope_pipe()` asyncio task: read stdout frames, parse, update scope |
| SpectrumBroadcastLoop | Backend core | `server.py` | `_broadcast_spectrum_loop()` asyncio task: 30fps broadcast |
| StateBroadcastTask | Backend core | `server.py` | `_broadcast_state()` called after commands and poll updates |
| MobileHTML | Frontend core | `static/index.html` | UI structure: header, waterfall canvas, S-meter, meters, controls, PTT footer, menu |
| MobileStyles | Frontend core | `static/ft710.css` | Dark amber theme, safe-area support, responsive layout |
| MainJS | Frontend core | `static/ft710_main.js` | WebSocket connect/reconnect, state management, message dispatch, audio RX/TX setup, spectrum receiver |
| UIJS | Frontend core | `static/ft710_ui.js` | All rendering: waterfall, S-meter, meters, button labels, PTT state, menu modals, event wiring |
| PTTManager | Frontend safety | `static/modules/ptt_manager.js` | PTT state machine, safety watchdog (500ms verify), beforeunload/pagehide beacons |
| SettingsManager | Frontend support | `static/modules/settings_manager.js` | Cookie persistence for auth token reading and all preferences (legacy web-storage values migrated to cookies on first load) |
| ATR1000Module | Frontend support | `static/modules/atr1000.js` | Tuner WS client + meter row rendering + ATR TUNE button (inert unless atr1000Enabled) |
| OpusWASM | Frontend audio | `static/modules/opus_wasm.js` | Emscripten-compiled libopus WASM binary |
| OpusCodecJS | Frontend audio | `static/modules/opus_codec.js` | JavaScript OpusEncoder/OpusDecoder classes wrapping WASM |
| RxWorklet | Frontend audio | `static/rx_worklet_processor.js` | AudioWorklet: queue-based playback with time-based jitter buffer (prebuffer 220ms, recovery 90ms, max 800ms) |
| TxCaptureWorklet | Frontend audio | `static/tx_capture_worklet.js` | AudioWorklet: mic capture → 48kHz float32 resample → 20ms frames via postMessage (SAB ring code present but currently unwired) |
| TxOpusWorker | Frontend audio | `static/tx_opus_worker.js` | Web Worker: Opus encoder (48kHz, 960-sample frames, 64kbps CBR, complexity=5); frames arrive via postMessage (SAB path dormant); posts to main thread for WS send (transferable buffers) |
| ServiceWorker | Frontend support | `static/sw.js` | Cache static assets; bypass JS/HTML to prevent stale cache |

## 11.2 Backend Component Collaboration (Startup)

```text
FastAPIApp lifespan startup:
  1. CatController.connect() → open serial port → send ID; → verify FT-710
  2. If connected: initial_state_sync() → 24+ CAT queries (incl. VS/FB/RG0/MS/AO/RI0) → RadioState.from_sync_result()
  3. _init_scope_cat() → send scope-init extended CAT commands
  4. PollScheduler(cat, radio, on_state_changed=_broadcast_state).start()
  5. AudioHandler() → init PyAudio → scan devices → start_rx() → open capture stream
  6. TxOpusDecoder() → init libopus decoder for TX path
  7. create_task(_audio_rx_loop()) → 20ms RX capture + encode + broadcast loop
  8. create_task(_audio_tx_drain_loop()) → 10ms TX queue drain loop
  9. ScopeHandler() → set up on_frame callback
 10. create_task(_broadcast_spectrum_loop()) → 30fps spectrum broadcast
 11. Launch scope_pipe subprocess → create_task(_read_scope_pipe())
```

## 11.3 Frontend Component Collaboration (Page Load)

```text
bodyload():
  1. connectWebSocket() → /WSradio?token=...
    → onopen: updateConnectionStatus(true), startPing()
    → connectSpectrumSocket() → /WSspectrum?token=...
    → connectAudioRX() → /WSaudioRX?token=... → startAudioRXPlayback()
      → new AudioContext(48000)
      → audioWorklet.addModule('rx_worklet_processor.js')
      → new AudioWorkletNode('rx-player')
    → connectAudioTX() → /WSaudioTX?token=...
    → onmessage: handleMessage(msg) → renderUpdates()
  2. requestWakeLock() → navigator.wakeLock.request('screen')
```

## 11.4 Frontend Component Collaboration (PTT)

```text
PTT button touchstart/mousedown:
  → PTTManager.pttStart()
    → handlePTTStart()
      → sendCommand('ptt', true)
      → startTXAudio()
        → new Worker('tx_opus_worker.js') (if not cached)
        → navigator.mediaDevices.getUserMedia({audio:{sampleRate:48000}})  // 48kHz
        → AudioContext({sampleRate:48000}) + AudioWorklet 'tx-capture' (ScriptProcessor fallback)
        → float32 20ms frames → Worker → Opus encode (48kHz, 960-sample frames, 64kbps CBR) → wsAudioTX.send()

PTT button touchend/mouseup:
  → PTTManager.pttEnd()
    → handlePTTEnd()
      → sendCommand('ptt', false)
      → stopTXAudio()
        → worker.postMessage({type:'stop'})
        → Keep mic stream cached (avoid re-prompt on next PTT)
        → wsAudioTX.send('s:')
      → PTTManager starts watchdog (500ms verify)
```
