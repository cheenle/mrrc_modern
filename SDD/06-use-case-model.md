# 6. Use Case Model (ART 0508)

## 6.1 Actors

| Actor | Description |
|-------|-------------|
| HAM Operator | Primary user controlling the selected radio from browser |
| System Maintainer | Starts service, manages serial ports/backend selection, observes logs |
| Browser Runtime | Supplies WebSocket, Web Audio, touch, microphone, Canvas |
| Yaesu FT-710 | Radio device controlled via serial CAT, scope via FT4222 SPI, audio via USB |
| Icom IC-7300 / IC-7300MK2 | Radio device controlled via USB CI-V serial, 0x27 spectrum on same port, 48kHz native USB audio |

## 6.2 Core Use Cases

### UC-001: Start Control Session

| Field | Description |
|-------|-------------|
| Goal | Open the web UI and establish control/audio/spectrum channels |
| Preconditions | Server running, supported radio connected via USB, browser can reach host |
| Basic Flow | User opens `http://host:8888`; login page; enter password; server sets `mrrc_auth` cookie; redirects to `/`; `index.html` loads; `bodyload()` connects `/WSradio`; on open receives `fullState` with `radioModel`, `radioDisplayName`, `capabilities`, bands/modes; connects `/WSspectrum`, `/WSaudioRX`, `/WSaudioTX` |
| Postconditions | UI fully rendered: frequency display, mode badge, band indicator, S-meter, waterfall, multi-meter, all controls active |
| Exceptions | Serial not connected → UI still usable; auth expired → redirect to `/login`; stale service worker cache |

### UC-002: Remote Receive Audio

| Field | Description |
|-------|-------------|
| Goal | Hear the selected radio's RX audio in the browser |
| Preconditions | Supported radio connected via USB; USB audio input available; `/WSaudioRX` connected |
| Basic Flow | Server captures Int16 PCM from the radio's USB audio via PyAudio (44.1kHz for FT-710, 48kHz for IC-7300); resamples to 48kHz if needed; Opus encodes; broadcasts tagged frames to audio_rx_clients; browser decodes (Opus WASM or Int16→Float32); AudioWorklet playback with jitter buffer |
| Postconditions | Operator hears radio audio through browser/phone speakers |
| Exceptions | PyAudio unavailable → audio disabled; Opus unavailable → PCM fallback (768kbps); AudioContext suspended (iOS) → resume on user interaction |

### UC-003: Tune Frequency and Mode

| Field | Description |
|-------|-------------|
| Goal | Change the radio frequency and operating mode |
| Preconditions | `/WSradio` connected; backend serial connected |
| Basic Flow | User taps band button, tuning arrows, or mode cycle; frontend sends `{"type":"set","field":"freq","value":14200000}` or `{"type":"set","field":"mode","value":"USB"}`; backend translates to radio-specific frames (e.g., FT-710 CAT `FA014200000;`/`MD02;`, IC-7300 CI-V frequency/mode commands); radio responds; state updates; broadcast to all clients |
| Postconditions | UI reflects new frequency/mode; radio follows |
| Notes | Band/mode mapping and command format are backend-specific; the frontend uses `capabilities` from `fullState` to render the correct bands and modes |

### UC-004: Monitor Spectrum and S-Meter

| Field | Description |
|-------|-------------|
| Goal | See signal context while listening |
| Preconditions | Server running; scope_pipe or S-meter polling active |
| Basic Flow (Real Scope) | FT-710: `scope_pipe` reads 4096-byte SPI frames → extracts 850-point wf1+wf2 → writes to stdout pipe → server parses → broadcasts via `/WSspectrum` binary → browser renders waterfall row. IC-7300: `civ_controller` demuxes 0x27 spectrum frames → `civ_scope` scales/upsamples 475 bins to 850 → `/WSspectrum` binary → browser renders waterfall row |
| Basic Flow (Fallback) | Backend S-meter poll → `RadioState.s_meter` → `ScopeHandler.update_from_radio_state()` → synthetic multi-peak Gaussian spectrum → `/WSspectrum` same binary format |
| Waterfall | Browser receives 850-byte wf1 array; appends to top of 120-row canvas; scrolls older rows down; colormap: black→blue→green→yellow→red |
| S-Meter | Canvas horizontal bar with S1–S9+60 gradient; peak-from-spectrum or CAT SM0; dBm digital readout |
| Frequency Scale | Auto-scaled labels below waterfall based on scope span + VFO center |

### UC-005: PTT and Tune Control

| Field | Description |
|-------|-------------|
| Goal | Key/de-key transmitter safely |
| Preconditions | `/WSradio` connected; CAT serial connected |
| Basic Flow | User touches-and-holds PTT button; `handlePTTStart()` → `sendCommand('ptt', true)`; backend sends radio-specific PTT key command (FT-710 CAT `TX1;`, IC-7300 CI-V 0x1C 0x00 key); radio keys TX; server starts TX audio stream; user releases; `handlePTTEnd()` → `sendCommand('ptt', false)`; backend sends radio-specific PTT unkey command (FT-710 `TX0;`, IC-7300 CI-V 0x1C 0x00 unkey) (fire-and-forget); stop TX audio stream; TX-status poll + PTT watchdog confirm RX |
| Tune Flow | User taps TUNE; backend starts radio-specific tune sequence (FT-710: `TX2` + `AC003`; IC-7300: backend-managed internal ATU tune); tap again or timeout ends tune and returns to RX |
| Safety | Touch cancel (touchcancel/mouseleave) → release; dead-man switch on WS disconnect; beforeunload beacon; PTT watchdog 500ms interval |
| Postconditions | Radio returns to RX |

### UC-006: Adjust Radio Settings

| Field | Description |
|-------|-------------|
| Goal | Change filter, preamp, attenuator, NR, NB, compressor, etc. |
| Preconditions | `/WSradio` connected |
| Basic Flow | User toggles NR switch → `{"type":"set","field":"nr","value":true}` → backend sends radio-specific command (e.g., FT-710 `NR01;`); state updates; broadcast. Similar for NB, AN, COMP, ATU, ATT, PRE. Sliders for NR level, NB level, RF power, AF gain, mic gain |
| Postconditions | Radio settings updated; UI reflects new state within one poll cycle |

### UC-007: Manage Memory Channels

| Field | Description |
|-------|-------------|
| Goal | Save and recall frequency/mode to memory slots |
| Preconditions | `/WSradio` connected |
| Basic Flow | User taps memory slot → `{"type":"memSave","channels":[...]}` → server writes `mem_channels.json` → broadcasts updated channels to all clients. Tap saved channel → sends freq/mode set commands |
| Postconditions | Memories persist across server restarts |

### UC-008: View Multi-Meter Telemetry

| Field | Description |
|-------|-------------|
| Goal | Monitor TX power, ALC, SWR, drain current/voltage |
| Preconditions | `/WSradio` connected; radio may be in TX for PWR/ALC/SWR |
| Basic Flow | Poll scheduler queries backend-specific meter commands (e.g., FT-710 CAT `RM4;`–`RM8;`) at 500ms (TX) / 5s (RX) intervals; updates `RadioState`; broadcasts dirty fields; UI renders horizontal bar meters |
| Postconditions | Real-time meter bars visible during TX |
