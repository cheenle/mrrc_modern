# 6. Use Case Model (ART 0508)

## 6.1 Actors

| Actor | Description |
|-------|-------------|
| HAM Operator | Primary user controlling the selected radio from browser |
| System Maintainer | Starts service, manages serial ports/backend selection, observes logs |
| Browser Runtime | Supplies WebSocket, Web Audio, touch, microphone, Canvas |
| Yaesu FT-710 | Radio device controlled via serial CAT, scope via FT4222 SPI, audio via USB |
| Icom CI-V family | Radio devices controlled via USB CI-V serial, 0x27 spectrum on the same port, 48kHz native USB audio (IC-7300/MK2 verified; IC-705/7610/7760 experimental) |
| Yaesu ASCII-CAT family | Radio devices (FTDX10/FTDX101D/FTDX101MP/FTX-1F/FT-891) controlled over the same ASCII-CAT serial protocol as the FT-710 at 38400 8N1; experimental (no unit tested) → receive-only until `MRRC_ALLOW_UNVERIFIED_TX=1`, no scope frames |
| SunSDR2 DX | Network radio controlled over native UDP; RX/TX audio and spectrum share the same IQ session; no serial port or USB sound card; experimental and TX-gated until hardware acceptance (AD-033) |

## 6.2 Core Use Cases

### UC-001: Start Control Session

| Field | Description |
|-------|-------------|
| Goal | Open the web UI and establish control/audio/spectrum channels |
| Preconditions | Server running, supported radio reachable over its backend transport (USB serial/audio or LAN), browser can reach host |
| Basic Flow | User opens `http://host:8888`; login page; enter password; server sets `mrrc_auth` cookie; redirects to `/`; `index.html` loads; `bodyload()` connects `/WSradio`; on open receives `fullState` with `radioModel`, `radioDisplayName`, `capabilities`, bands/modes; connects `/WSspectrum`, `/WSaudioRX`, `/WSaudioTX` |
| Postconditions | UI fully rendered: frequency display, mode badge, band indicator, S-meter, waterfall, multi-meter, all controls active |
| Exceptions | Radio link not connected → UI still usable; auth expired → redirect to `/login`; stale service worker cache |

### UC-002: Remote Receive Audio

| Field | Description |
|-------|-------------|
| Goal | Hear the selected radio's RX audio in the browser |
| Preconditions | Supported radio reachable; USB audio input for USB models or network IQ for SunSDR2 DX; `/WSaudioRX` connected |
| Basic Flow | Backend captures USB PCM or demodulates network IQ into 48 kHz Int16 PCM; Opus encodes; broadcasts tagged frames to audio_rx_clients; browser decodes (Opus WASM or Int16→Float32); AudioWorklet playback with jitter buffer |
| Postconditions | Operator hears radio audio through browser/phone speakers |
| Exceptions | Audio source/link unavailable → clear disconnected/silent state; Opus unavailable → PCM fallback (768kbps); AudioContext suspended (iOS) → resume on user interaction |

### UC-003: Tune Frequency and Mode

| Field | Description |
|-------|-------------|
| Goal | Change the radio frequency and operating mode |
| Preconditions | `/WSradio` connected; backend serial or network link connected |
| Basic Flow | User taps band button, tuning arrows, or mode cycle; frontend sends `{"type":"set","field":"freq","value":14200000}` or `{"type":"set","field":"mode","value":"USB"}`; backend translates to radio-specific frames (e.g., FT-710 CAT `FA014200000;`/`MD02;`, IC-7300 CI-V frequency/mode commands); radio responds; state updates; broadcast to all clients |
| Postconditions | UI reflects new frequency/mode; radio follows |
| Notes | Band/mode mapping and command format are backend-specific; the frontend uses `capabilities` from `fullState` to render the correct bands and modes |

### UC-004: Monitor Spectrum and S-Meter

| Field | Description |
|-------|-------------|
| Goal | See signal context while listening |
| Preconditions | Server running; backend real-scope stream or S-meter polling active |
| Basic Flow (Real Scope) | FT-710 SPI and Icom CI-V frames fill `ScopeHandler`; LAN-IQ models FFT network samples into the same 850-bin shape (SunSDR2 DX uses its shared UDP engine); `/WSspectrum` broadcasts binary frames and the browser renders the waterfall |
| Basic Flow (Fallback) | Backend S-meter poll → `RadioState.s_meter` → `ScopeHandler.update_from_radio_state()` → synthetic multi-peak Gaussian spectrum → `/WSspectrum` same binary format |
| Waterfall | Browser receives 850-byte wf1 array; appends to top of 120-row canvas; scrolls older rows down; colormap: black→blue→green→yellow→red |
| S-Meter | Canvas horizontal bar with S1–S9+60 gradient; peak-from-spectrum or CAT SM0; dBm digital readout |
| Frequency Scale | Auto-scaled labels below waterfall based on scope span + VFO center |

### UC-005: PTT and Tune Control

| Field | Description |
|-------|-------------|
| Goal | Key/de-key transmitter safely |
| Preconditions | `/WSradio` connected; backend link connected; hardware-unverified models additionally require the explicit TX gate |
| Basic Flow | User touches-and-holds PTT → backend-specific serial/UDP key command → radio TX → server starts backend TX audio; release immediately sends the backend unkey command, stops TX audio and uses the existing watchdog/telemetry paths to confirm RX |
| Tune Flow | User taps TUNE; backend starts its supported carrier/ATU sequence (SunSDR2 DX generates a bounded 700 Hz network-IQ tone, not an ATU command); tap again or timeout returns to RX |
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
