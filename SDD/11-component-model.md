# 11. Component Model (ART 0515)

## 11.1 Component Inventory

| Component | Type | File | Responsibility |
|-----------|------|------|----------------|
| FastAPIApp | Backend core | `server.py` | Route registration, lifespan, auth middleware, static serving, all WebSockets |
| RadioBackendFactory | Backend core | `backends/__init__.py` | `create_backend(model)` lazy factory; registered keys `ft710`, `ic7300`, `ic7300mk2`; selected by `MRRC_RADIO_MODEL` |
| RadioBackend | Backend core | `backends/base.py` | `RadioBackend` ABC + `RadioCapabilities` dataclass + `ScopeProducer` protocol; CAT surface, defaulted hooks for bands/modes/filter tables/poll lists/scope init |
| CatController | Backend core | `backends/ft710/cat_controller.py` (root shim: `cat_controller.py`) | FT-710 serial CAT protocol: connect, disconnect, send/query/set, priority set path for PTT/TUNE preemption; high-level FT-710 command helpers |
| CivCodec | Backend core | `backends/ic7300/civ_codec.py` | Pure CI-V framing/BCD encoding/scope-segment codec for IC-7300/MK2 |
| CivController | Backend core | `backends/ic7300/civ_controller.py` | Async CI-V demux: reader thread → frame parser → echo drop / 0x27 scope queue / transceive broadcast / pending-response matching; 3-tier priority; reconnect |
| CivScopeProducer | Backend core | `backends/ic7300/civ_scope.py` | `ScopeProducer` implementation: CI-V 0x27 475 bins → scale 160→255 → upsample 850 → `ScopeHandler` |
| RadioState | Backend core | `radio_state.py` | Dataclass with dirty-field change tracking; to_dict/to_dirty_dict serialization; from_sync_result deserialization; derived properties (mode_name, s_unit, band_name, filter_hz) |
| PollScheduler | Backend core | `poll_scheduler.py` | 7-task asyncio polling (IF/VFO/TX-status/TX-meters/settings/slow/watchdog), skip-on-command, cancel-aware preemption for priority radio writes; watchdog re-runs scope init (`on_reconnected` hook) after reconnect |
| AudioHandler | Backend core | `audio_handler.py` | PyAudio device enumeration, RX capture stream, TX playback stream, Opus encode (via RxOpusEncoder), multi-layer audio device auto-detection parameterized by backend (name hints + mono heuristic + full-duplex) |
| OpusCodec | Backend support | `opus_rx.py` | RxOpusEncoder (48kHz mono, 64kbps), TxOpusDecoder (48kHz mono); direct ctypes libopus bindings; bitrate via max_data_bytes cap |
| ScopeHandler | Backend core | `scope_handler.py` | Spectrum data container; update_from_scope_frame (real) and update_from_radio_state (synthetic); get_spectrum_binary for WS broadcast |
| ScopePipe | Backend core | `backends/ft710/scope_pipe.py` (root shim: `scope_pipe.py`) | Standalone subprocess: FT4222 SPI init + read loop; frame sync; stdout binary frames + stderr STATUS diagnostics (FT-710 only) |
| ScopePipeProducer | Backend core | `backends/ft710/scope_producer.py` | `ScopeProducer` implementation: owns `scope_pipe` subprocess spawn/read/auto-restart/TX-notify (FT-710 only) |
| ScopeFrame | Backend support | `backends/ft710/scope_frame.py` (root shim: `scope_frame.py`) | Shared frame parsing: parse_pipe_payload, WF_SIZE constant, quality metrics |
| ScopeLibraries | Backend support | `backends/ft710/scope_libraries.py` (root shim: `scope_libraries.py`) | FTDI library discovery and SPI clock configuration |
| Config | Backend support | `config.py` | Protocol-neutral constants + shared UI mode tables; per-backend tables live in `backends/ft710/config_ft710.py` and `backends/ic7300/config_ic7300.py` |
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
  1. create_backend(MRRC_RADIO_MODEL) → `RadioBackend` instance + `RadioCapabilities`
  2. backend.connect() → open serial port → send ID; → verify radio (FT-710 ID or IC-7300 CI-V ID)
  3. If connected: backend.initial_state_sync() → backend-specific queries → RadioState.from_sync_result()
  4. backend.init_scope() → FT-710: send scope-init extended CAT commands; IC-7300: enable CI-V 0x27 scope
  5. PollScheduler(backend, radio, on_state_changed=_broadcast_state).start()
  6. AudioHandler() → init PyAudio → scan devices (per-backend hints/rate) → start_rx() → open capture stream
  7. TxOpusDecoder() → init libopus decoder for TX path
  8. create_task(_audio_rx_loop()) → 20ms RX capture + encode + broadcast loop
  9. create_task(_audio_tx_drain_loop()) → 10ms TX queue drain loop
 10. ScopeHandler() → set up on_frame callback
 11. create_task(_broadcast_spectrum_loop()) → 30fps spectrum broadcast
 12. backend.create_scope_producer() → FT-710: launch scope_pipe subprocess + create_task(_read_scope_pipe()); IC-7300: wire CI-V 0x27 demux to ScopeHandler
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
