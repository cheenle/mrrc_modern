# 9. Architecture Overview (ART 0512)

## 9.1 Logical Architecture

```text
Mobile/Desktop Browser
  index.html / ft710.css / ft710_main.js / ft710_ui.js
  Web Audio playback, mic capture, PTT UI, waterfall canvas
        |
        | HTTP/WS
        v
FastAPI MRRC FT-710 Server (`server.py`)
  static file serving
  /WSradio — control (JSON)
  /WSaudioRX — RX audio fan-out (binary tagged Opus/PCM)
  /WSaudioTX — TX mic uplink (binary tagged → decode → PyAudio)
  /WSspectrum — waterfall fan-out (binary)
  /api/mem_channels — memory channel CRUD
  /api/status — full radio state
  /api/auth/* — session management
        |
        | in-process imports
        v
Backend Modules
  cat_controller.py — serial CAT protocol (pyserial + asyncio threads)
  radio_state.py — shared state with dirty-field tracking
  poll_scheduler.py — 7-task adaptive polling with priority-command preemption
  audio_handler.py — PyAudio capture/playback + Opus codec
  opus_rx.py — libopus ctypes wrapper
  scope_handler.py — spectrum data + S-meter fallback
  scope_pipe.py — standalone FT4222 SPI subprocess
  config.py — constants, mode/band tables, S-meter calibration
        |
        | Serial / SPI / USB Audio
        v
Yaesu FT-710
```

## 9.2 WebSocket Endpoints

### 9.2.1 /WSradio (JSON text)

Control channel. Carries all radio commands, state updates, and memory management.

**Server → Client:**
- `{"type":"fullState","data":{...},"bands":[...],"modes":[...]}` — initial sync on connect
- `{"type":"stateUpdate","fields":{...},"dirty":[...]}` — partial changed-field update
- `{"type":"value","field":"...","value":...}` — single-value query response
- `{"type":"memChannels","channels":[...]}` — memory channel broadcast
- `{"type":"pong"}` — keepalive response

**Client → Server:**
- `{"type":"set","field":"...","value":...}` — command (40+ supported fields)
- `{"type":"get","field":"..."}` — query current value
- `{"type":"memSave","channels":[...]}` — save memory channels
- `{"type":"memDelete","index":N}` — delete memory slot
- `{"type":"ping"}` — keepalive

### 9.2.2 /WSaudioRX (binary)

**Format:** 1-byte codec tag (0x00=PCM, 0x01=Opus) + payload.

Server captures 48kHz Int16 mono from FT-710 USB audio → Opus encodes (64kbps default) → broadcasts to all `audio_rx_clients` at 20ms intervals. Browser decodes via WASM OpusDecoder (or Int16→Float32 for PCM) → AudioWorklet playback with jitter buffer.

### 9.2.3 /WSaudioTX (binary + text)

**Binary:** 1-byte codec tag + encoded mic audio. Server decodes (Opus→PCM or pass-through PCM) → queues to PyAudio output stream → played to FT-710 USB audio input.

**Text:** `"s:"` = stop TX; `"m:rate,encode,..."` = settings.

### 9.2.4 /WSspectrum (binary)

**v1 format:** 1-byte version (0x01) + 850 bytes wf1 = 851 bytes.
**v2 format:** 1-byte version (0x02) + 850 bytes wf1 + 850 bytes wf2 = 1701 bytes.

Broadcast at ~30 fps from FT4222 data or S-meter fallback.

## 9.3 RX Audio Signal Chain

```text
FT-710 USB Audio Output
  → PyAudio capture (44.1kHz, Int16, mono, 882-sample chunks = 20ms)
    → AudioHandler.read_rx_chunk() (non-blocking poll from asyncio loop)
      → resample_441_to_48 (882→960 samples, frame-aligned)
        → AudioHandler.encode_rx_audio()
        → Opus encode (RxOpusEncoder, 64kbps, 20ms frames)
          → 1-byte tag (0x01) + Opus packet
        → OR PCM fallback: 1-byte tag (0x00) + Int16 bytes
          → /WSaudioRX broadcast to all audio_rx_clients
            → Browser: decodeRxAudioFrame()
              → Opus: WASM OpusDecoder.decode_float() → Float32Array
              → PCM: Int16Array → Float32Array (÷32767)
                → AudioWorklet 'rx-player': port.postMessage({type:'push', payload})
                  → Jitter buffer (220ms prebuffer, 90ms recovery, 800ms max)
                    → AudioContext.destination → speakers
```

## 9.4 TX Audio Signal Chain

```text
Browser Microphone
  → getUserMedia({sampleRate:48000, channelCount:1})
    → AudioContext.createMediaStreamSource → GainNode (device-side mic gain, 🎙 Vol slider 0–200 → 0–2×, browser-local, cookie `ft710_micVol`) → AudioWorklet 'tx-capture' (ScriptProcessor fallback)
      → 48kHz float32 20ms frames → postMessage → main thread → Worker ('float_frame')
        → Opus Worker (tx_opus_worker.js, 48kHz):
          → OpusEncoder.encode_float(960-sample frames)
            → 1-byte tag (0x01) + Opus packet (64kbps CBR, complexity=5)
          → OR PCM fallback: 1-byte tag (0x00) + Int16 bytes
            → postMessage({type:'tx_audio', data:tagged.buffer}, [tagged.buffer])
              → main thread: wsAudioTX.send(tagged)
                → /WSaudioTX → server
                  → Opus: TxOpusDecoder.decode() → Int16 PCM (48kHz, 960 samples/frame)
                  → PCM: pass-through
                    → AudioHandler.feed_tx_audio() → resample_48_to_441 (960→882, frame-aligned) → _tx_queue
                      → AudioHandler.write_tx_chunk() (10ms drain loop)
                        → PyAudio output stream (44.1kHz, mono) → FT-710 USB Audio Input
```

**TX runs at 48 kHz throughout the codec domain** — browser capture and Opus encode/decode — and the server bridges to the FT-710's native 44.1 kHz USB audio via frame-aligned resampling (960↔882 = exactly 20 ms, ratio 160:147) on every platform, including Windows. This eliminates the v1.0 sample-rate mismatch (16 kHz mic → 48 kHz playback) and prevents a Windows shared-mode mix rate from bypassing the device-domain bridge.

## 9.5 Spectrum Signal Chain

### 9.5.1 FT4222 Path (Real FFT Data)

```text
FT-710 FT4222 SPI chip
  → scope_pipe.py subprocess (ctypes → libft4222)
    → 4096-byte SPI reads → frame sync → v2 payload
      → stdout: [4-byte BE length][version(1B) + wf1(850B) + wf2(850B) + metadata]
        → server.py _read_scope_pipe() (asyncio stdout reader)
          → parse_pipe_payload() → ScopeFrame
            → scope.spectrum_rx1 = wf1, scope.spectrum_rx2 = wf2
            → metadata: s_meter, vfoa_freq, mode, span, preamp, att
            → _on_scope_frame() → update RadioState, broadcast
    → stderr: "STATUS:..." diagnostic lines → server logging
    ← stdin: "TX:1"/"TX:0" control (server → pipe, V2.7) — SPI reads
      pause while TX (radio garbles the scope stream during transmit),
      one clean re-sync on TX→RX; stdin EOF also stops the pipe
```

### 9.5.2 S-Meter Fallback (Synthetic Spectrum)

```text
CAT SM0; poll (100ms)
  → radio_state.s_meter (0–255)
    → scope_handler.update_from_radio_state(radio)
      → generate Gaussian peaks at band edges + center
        → scale to 850-point wf1 array
          → scope.get_spectrum_binary() → v1 (851B) or v2 (1701B) frame
            → /WSspectrum broadcast (same format as FT4222 path)
```

## 9.6 CAT Polling Architecture

```text
PollScheduler (asyncio, 7 cooperative tasks)
  Tier 1  (100ms):      FA; MD0; SM0;                     → vfo_a_freq, mode, s_meter
  Tier 1b (500ms):      VS; FB;                           → active_vfo, vfo_b_freq
  Tier 2A (500ms, TX):  RM3; RM4; RM5; RM6;              → comp, alc, power, swr
  Tier 2B (500ms):      TX;                               → tx_status
  Tier 3  (2s):         SH0; AG0; RG0; PC; PA0; RA0; NB0; NR0; BC; AC; SS01; AN; GT; MS; PS;
                        → filter/gains/preamp/att/NR/NB/AN/tuner/scope/antenna/agc/meter_display
  Tier 4  (5s):         RM7; RM8; PR; CO; AO; RI0;       → id, vd, compressor, contour, amc, radio-info telemetry
  Tier 5  (1s):         connection watchdog               → reconnect + full-state re-sync + scope re-init

User commands skip next poll for affected fields (`skip_next_poll`).
Poll loops also re-check the skip AFTER each in-flight query returns and discard the response
when a user command landed mid-flight (stale-read guard in the IF/VFO/settings loops — without
it, a pre-command `SH0;` response overwrote a just-set filter width and snapped the UI back).
The `filter` set handler additionally reads `SH0;` back ~150 ms after `SH00<NN>;` and
broadcasts the radio's actual index (logged as `Filter read-back: index=N (requested M)`).
PTT/TUNE commands use priority writes (`send_priority_set_command`) and set `_cancel_polls`,
which poll loops and read threads observe to release the serial lock quickly.
CAT I/O remains serialized through `CatController._lock`; blocking serial work is offloaded via `asyncio.to_thread()`.
```

## 9.7 State Broadcasting

```text
Command or Poll → RadioState.update(field=value)
  → _dirty_fields.add(field)
    → _broadcast_state()
      → dirty = radio.get_and_clear_dirty()
        → update = {type:"stateUpdate", fields:radio.to_dirty_dict(dirty), dirty:list(dirty)}
          → json.dumps → send_text to all ctrl_clients
            → browser: handleMessage() → Object.assign(radioState, msg.fields) → renderUpdates(msg.dirty)
```

## 9.8 ATR1000 Tuner Linkage (optional)

```text
Browser (static/modules/atr1000.js — inert unless fullState.atr1000Enabled)
  | /WSatr1000?token=... (JSON: atrState / atrTuneResult / atrTune / ping)
  v
server.py
  | _broadcast_state() freq-dirty hook → notify_freq (auto-apply learned LC relays)
  | TX on/off hook → notify_tx (device push mode, learning window)
  | _atr_tune_assist() → TX2 carrier → SWR gate → full tune → keep+learn / rollback
  v
atr1000_client.py (asyncio WS client: binary frames [0xFF,CMD,LEN,DATA],
  5s reconnect, 55-min refresh, TX-no-SYNC watchdog, throttled relay writes,
  LearningBuffer stability-window learning)
  | ws://$FT710_ATR1000_HOST:$FT710_ATR1000_PORT (default port 60001)
  v
ATR1000 networked antenna tuner
```

Three linkage behaviors:

1. **Freq change → relay apply** — `_broadcast_state` on vfo_a_freq/vfo_b_freq/active_vfo dirty calls `notify_freq`; learned LC values from `atr1000_tuner.json` (TunerStorage: learn gate SWR 1.0–1.8, 1kHz keys ±5kHz nearest, atomic writes) are pushed to the tuner (5s write throttle).
2. **TX on/off → `notify_tx`** — switches the tuner to device push mode and opens the learning window (LearningBuffer, 4-sample stability).
3. **Tune assist** — client `{"type":"atrTune"}` runs `_atr_tune_assist()` server-side: TX2 carrier → skip if SWR≤1.6 → snapshot relays → full tune (mode=2) → keep+learn if SWR improved ≥0.02 else rollback → carrier always dropped in `finally` (ATR_TUNE_* constants: 0.3s settle, 5s min, 45s deadline, 0.8s compare settle, 2.5s meter wait).

**Default-disabled isolation**: `FT710_ATR1000_HOST` empty (default) means no client task, no network traffic, linkage hooks short-circuit, `/WSatr1000` closes with code 4000, and the frontend module is never initialized. ATR data is deliberately kept out of RadioState (separate channel, same precedent as spectrum); the audio path is untouched.
