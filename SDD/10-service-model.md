# 10. Service Model (ART 0582)

## 10.1 Service Portfolio

| Service | Type | Status | Responsibility |
|---------|------|--------|----------------|
| StaticUIService | Core | Implemented | Serve mobile UI assets from `static/` with MIME types |
| ControlService | Core | Implemented | `/WSradio` JSON command dispatch, state broadcast, memory management |
| RXAudioService | Core | Implemented | Capture FT-710 USB audio → Opus encode → `/WSaudioRX` tagged frame broadcast |
| TXAudioService | Core | Implemented | Receive `/WSaudioTX` tagged frames → Opus decode → resample 48→44.1k → PyAudio → radio |
| SpectrumService | Core | Implemented | FT4222 scope data + S-meter fallback → `/WSspectrum` binary broadcast |
| CATSerialService | Core | Implemented | Serial CAT protocol over USB Enhanced COM Port (38400, 8N1) |
| PollingService | Core | Implemented | 7-task adaptive background polling with priority-command yield |
| ScopePipeService | Core | Implemented | Manage scope_pipe subprocess lifecycle; read stdout/stderr |
| MemoryChannelService | Core | Implemented | `/api/mem_channels` GET/POST with JSON persistence |
| ATR1000Service | Optional | Implemented | `/WSatr1000` Browser↔Server: optional external-tuner state + tune assist, token-gated, closed when disabled |
| AuthService | Support | Implemented | Password login, session tokens, cookie management, WS gating |
| StatusService | Support | Implemented | `/api/status` full radio state JSON |
| ProcessService | Support | Implemented | `start.sh` / `stop.sh` background service management, PID file |

## 10.2 Service Dependencies

```text
ControlService
  → CATSerialService
  → PollingService

RXAudioService
  → AudioHandler (PyAudio capture + Opus encode)

TXAudioService
  → AudioHandler (Opus decode + PyAudio playback)

SpectrumService
  → ScopePipeService (FT4222 path)
  → PollingService (S-meter fallback path)

PollingService
  → CATSerialService
  → RadioState

StaticUIService
  → browser runtime

AuthService
  → all WebSocket endpoints (token validation on connect)
  → all HTTP routes (cookie-based middleware)
```

## 10.3 Service Interfaces

| Service | Input | Output | Protocol |
|---------|-------|--------|----------|
| ControlService | JSON commands | JSON state updates | WS `/WSradio` |
| RXAudioService | PyAudio PCM chunks | Tagged binary frames (Opus/PCM) | WS `/WSaudioRX` |
| TXAudioService | Tagged binary frames + text control | PyAudio playback | WS `/WSaudioTX` |
| SpectrumService | ScopeFrame or RadioState | Binary spectrum frames (v1/v2) | WS `/WSspectrum` |
| CATSerialService | Command strings | Response strings | Serial (38400,8N1) |
| MemoryChannelService | JSON array | JSON array + broadcast | HTTP `/api/mem_channels` |
| AuthService | Password + request | Cookie + token + redirect | HTTP `/api/auth/*` |
| StatusService | GET request | Full radio state JSON (50+ fields) | HTTP `/api/status` |
| ATR1000Service | JSON `atrTune` / `ping` | JSON `atrState` / `atrTuneResult` / `pong` | WS `/WSatr1000` |

## 10.4 Control Service Command Contract

Key commands (see `_execute_set_command` in `server.py` for complete list):

| Command Field | Values | CAT Command | Notes |
|---------------|--------|-------------|-------|
| `freq` / `vfo_a_freq` | Hz int | `FA<9d>;` | Set VFO-A frequency |
| `vfo_b_freq` | Hz int | `FB<9d>;` | Set VFO-B frequency |
| `mode` | "USB","LSB",... | `MD0<X>;` | Set operating mode |
| `ptt` | true/false | `TX1;` / `TX0;` | Priority command path; preempts poll queries |
| `tune` | true/false | `TX2;` + `AC003;` / `AC000;` + `TX0;` | Tune carrier + tuner start/stop sequence |
| `filter` / `filter_width` | 00–23 | `SH00<NN>;` | Filter width index (P1=0, P2=0 fixed); post-set `SH0;` read-back (~150 ms) broadcasts the radio's actual index |
| `af_gain` | 0–255 | `AG0<NNN>;` | AF gain |
| `rf_gain` | 0–255 | `RG0<NNN>;` | RF gain |
| `meter_display` | 0–5 | `MS<P1>0;` | Radio front-panel meter selection |
| `amc_level` | 1–100 | `AO<NNN>;` | AMC output level |
| `rf_power` | 5–100 | `PC<NNN>;` | RF power |
| `preamp` | 0,1,2 | `PA0<N>;` | OFF/AMP1/AMP2 |
| `att` / `attenuator` | 0,1,2,3 | `RA0<N>;` | OFF/6dB/12dB/18dB |
| `nr` / `noise_reduction` | true/false | `NR0<0/1>;` | Noise reduction toggle |
| `nb` / `noise_blanker` | true/false | `NB0<0/1>;` | Noise blanker toggle |
| `an` / `auto_notch` | true/false | `BC0<0/1>;` | Auto notch toggle |
| `comp` / `compressor` | true/false | `PR0<0/1>;` | Compressor toggle |
| `tuner` | 0,1,2 | `AC000;` / `AC001;` / `AC003;` | ATU OFF/ON/TUNE start; mapping differs from Hamlib (`AC010`/`AC011`) — see gap-analysis §1.1 note |
| `vfo` | "A","B" | `VS0;` / `VS1;` | Active VFO switch |
| `split` | true/false | `ST<0/1>;` | Split operation |
| `band` | "20m","40m",... | `BS<NN>;` | Band stacking register |
| `scope_span` | 0–9 | `SS05<NN>;` | Scope span index |
| `scope_speed` | 0–5 | `SS00<NN>;` | Scope sweep speed |
| `scope_mode` | 0–9 | `SS06<NN>;` | Scope display mode |

## 10.5 Service Quality Targets

| Service | Quality Target |
|---------|----------------|
| ControlService | PTT/TUNE commands bypass queued polls and execute with low latency |
| RXAudioService | Continuous playback under LAN jitter; Opus 64kbps default |
| TXAudioService | Low-latency mic → radio path (< 500ms) |
| SpectrumService | ~30fps FT4222; ~10fps fallback; identical binary format |
| CATSerialService | Serial lock prevents interleaved commands; timeout 3s |
| AuthService | 30-day session; all WS endpoints gated; redirect on expiry |
