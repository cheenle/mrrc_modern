# 10. Service Model (ART 0582)

## 10.1 Service Portfolio

| Service | Type | Status | Responsibility |
| --- | --- | --- | --- |
| StaticUIService | Core | Implemented | SPA and independent pages with MIME, ETag/conditional requests and deliberate cache policy |
| ControlService | Core | Implemented | `/WSradio` dispatch, dirty state, memories, recording/CQ state and role gates |
| TerminalService | Core | Implemented (1.28.86 · FP73) | `/WSterminal` single-socket control/media multiplex and `/terminal` reference client (AD-039) |
| BackendFactoryService | Core | Implemented | 15-key registry, lazy creation, serial/network setup metadata and model verification table |
| CATSerialService | Backend | Implemented | FT-710 ASCII CAT and shared Yaesu core; serial or SCU-LAN10 serial-lookalike transport |
| CIVService | Backend | Implemented | Shared Icom/Xiegu CI-V framing/demux, profiles, 0x27 scope and serial/Icom-LAN transports |
| SunSDRDeviceService | Backend | Implemented; whole model TX-gated | Exclusive UDP boot/control/IQ/telemetry, native DSP audio/scope and bounded PTT recovery |
| FlexDeviceService | Backend | RX field-exercised; TX-gated | SmartSDR TCP objects, VITA-49 UDP, own pan/slice/antenna, meters, audio and FFT |
| RXAudioService | Core | Implemented | USB/network source → 48 kHz bus → native taps and Opus48/PCM48 listener fan-out |
| TXAudioService | Core | Backend-dependent | Owner arbitration, mic gain/recording and USB/network modulation; SCU-LAN10 refuses TX |
| SpectrumService | Core | Implemented | Backend real sources + S-meter fallback → AD-025 full/short/divider variants |
| PollingService | Core | Implemented | Seven adaptive tiers, measured baud cost, priority preemption and reconnect scope init |
| ScopePipeService | Core | Implemented | FT-710 FT4222 subprocess lifecycle and TX pause/resume |
| MemoryChannelService | Core | Implemented | `/api/mem_channels` and WS memory messages with JSON persistence |
| RecordingService | Core | Implemented | Per-session writer, free-space/session guards, list/Range/delete APIs |
| CQService | Optional asset | Implemented | Finite one-touch call with shared TX queue and abort/release paths |
| FT8Service | Work mode | Implemented | `/api/ft8/*`, `/WSft8`, native worker, fast/deep cascade, persistence and SSB restore |
| SDRReceiverService | Independent | Implemented on supported boxes | `/sdr`, `/WSsdr`, exclusive device pipe, native channelizer, tuning/share/cap rules |
| NanoVNAService | Instrument | Implemented | Guarded `/api/nano/*`, exact binary scans, analysis/exports, baseline and optional DFU |
| BoxOperationsService | Platform | Implemented on NetworkManager/bluez boxes | `/setup`, `/settings`, Wi-Fi/Bluetooth/local control/speaker, fixed root helper and report |
| AuthService | Support | Implemented | Persistent operator/listen tokens, separate SDR auth, cookie/Bearer transport and throttling |
| StatusAndMetricsService | Support | Implemented | `/api/status`, `/api/health`, `/api/session_metrics`, `/api/perf` and `/Perf` |
| SupportService | Support | Implemented | Redacted bundle/upload/save/download plus external answer/board workflow |
| UpdateService | Support | Platform-dependent | Manifest check/apply/state and self-proof; box line has independent version/fixpack/update scripts |
| CloudAndHelpService | Support | Implemented | Tunnel truth, certificate/application actions and bounded loopback-only SSH help windows |

## 10.2 Service Dependencies

```text
ControlService / TerminalService
  → AuthService + role gates
  → BackendFactoryService → active backend/controller
  → PollingService → RadioState → dirty-field fan-out
  → shared TX owner / PTT keyer / safety services

RXAudioService
  → active backend audio factory (PyAudio by default, network adapter when supplied)
  → recorder / FT8 / local speaker taps
  → audio_levels listener copy → Opus/PCM fan-out

TXAudioService
  → TerminalService or /WSaudioTX through _feed_tx_uplink_frame
  → mic gain → recording tap → active backend TX adapter

SpectrumService
  → active ScopeProducer (SPI / CI-V / SCU / SunSDR / Flex)
  → ScopeHandler → spectrum_profile → legacy/terminal fan-out
  → PollingService S-meter fallback when no real producer exists

SDRReceiverService / NanoVNAService / BoxOperationsService
  → independent device/system owners; never borrow the radio CAT controller
```

## 10.3 Service Interfaces

| Surface | Input | Output | Protocol |
| --- | --- | --- | --- |
| Control | JSON set/get/memory/ping/txhb | fullState/stateUpdate/value/errors/safety pushes | WS `/WSradio` |
| Terminal | Control JSON, spectrumCaps, channel-tagged TX | Control JSON, channel-tagged RX/spectrum | WS `/WSterminal` |
| RX/TX audio | Codec-tagged binary and stream text | Backend PCM or tagged client audio | WS `/WSaudioRX`, `/WSaudioTX` |
| Spectrum | spectrumCaps | 1701/851 B binary variants | WS `/WSspectrum` |
| Authentication | Password/request | Cookie/session state | `/api/auth/*`, `/api/sdr/login` |
| Setup/status | GET/validated POST | Read-back configuration, devices, health/status | `/api/setup`, `/api/devices`, `/api/status`, `/api/health` |
| Memories/recordings/CQ | JSON/WAV/path operations | Persistent state, MP3 Range stream or deletion | `/api/mem_channels`, `/api/recordings*`, `/api/cq*` |
| Audio levels | dB values in documented ranges | One shared level shape | `/api/audio/levels` |
| FT8 | Mode/engine/settings/QSO requests | FT8 state/events/stats | `/api/ft8/*`, `/WSft8` |
| Box operations | Validated network/system values | Sanitized status or fixed action result | `/api/box/*` |
| Network backend setup | SCU-LAN/Flex/G7250/SunSDR settings | Saved/read-back state or explicit test failure | `/api/setup`, `/api/box/scu-lan*` |
| Instrument/SDR | Guarded scan/tune/subscription | Analysis or independent SDR media | `/api/nano/*`, `/WSsdr` |
| Support/update/cloud/help | Operator-initiated commands | Bundle/update/tunnel/help evidence | `/api/support/*`, `/api/update/*`, `/api/cloud/*`, `/api/help/*` |
| Metrics/performance | Authenticated GET | Counts/bytes/deltas/lag without identifiers | `/api/session_metrics`, `/api/perf` |

All HTTP/WS construction is path-prefix safe. Session tokens use Bearer/cookie (deprecated query only for compatibility); no new endpoint puts a token in a URL. Non-GET operator mutations remain forbidden to listen/SDR roles even when a route exists.

### 10.3.1 Cloud Hub observability contract

`GET /api/cloud/state` reports `tunnel_running` and `tunnel_error`, not merely a configured public entry. Missing `fleet/frpc` or instance configuration names the actual path/reason and deduplicates warnings. `GET /api/session_metrics` supplies listener/operator sessions and sockets, lifetime/window peaks and media uplink bytes; terminal media bytes remain in `audio_rx`/`spectrum`. These are the instance-side inputs to Hub decisions, not a replacement for the Hub registry/lease SDD.

## 10.4 Control Service Command Contract

The WebSocket field set is backend-neutral; the following CAT strings are **FT-710/shared-Yaesu examples**, not universal wire bytes. Icom/Xiegu, SCU-LAN10, SunSDR and Flex translate inside their backend. The frontend uses `fullState.capabilities`, bands, modes, filters and live antenna/scope data rather than assuming this table.

| Command Field | Values | Example mapping / authority | Notes |
| --- | --- | --- | --- |
| `freq`, `vfo_a_freq`, `vfo_b_freq` | Hz integer within backend bands | `FA/FB<9d>;`, CI-V BCD, network tune | Flex targets owned slice; Yaesu receiver selection follows active VFO |
| `mode` | Backend mode register/name | `MD0<X>;`, CI-V 0x06, `slice set … mode=` | Mode tables and UI labels come from backend |
| `ptt` | Boolean | `TX1/TX0;`, CI-V 1C 00, `xmit 1/0` | Key-up gated, release never gated; per-socket arbitration |
| `tune` | Boolean | TX2/ATU/backend bounded carrier | Unsupported paths refuse rather than emit a silent carrier |
| `filter`, `filter_width` | Backend index | Profile prefix + two digits | FTDX10/101/FTX-1 family `SH00`, FT-891 `SH01`, FT-991A `SH0`; SUB uses `SH10` where supported. Trailing digits are bandwidth indices resolved to Hz by mode, not generic slot IDs |
| `af_gain`, `rf_gain`, `rf_power` | Backend range | `AG/RG/PC` or live network ranges | Flex RF gain uses radio-reported SCU low/high/step; unsupported controls hide |
| `preamp`, `att` | Profile steps | `PA/RA` or CI-V BCD | Labels/steps are profile data |
| `nr`, `nb`, `an`, `comp` and levels | Boolean/range | `NR/NB/BC/PR`, `RL/NL` or backend commands | Command shape and read-back are backend-specific |
| `tuner` | 0/1/2 | FT-710 `AC000/AC001/AC003`; FT-991A tune `AC002` | Profile `tuner_tune_cmd`, never Hamlib `AC010/AC011` assumption |
| `vfo`, `split` | A/B, Boolean | `VS/ST` or backend equivalent | Dual-receive `both/main` returns to VFO-A on explicit selection |
| `power` | Boolean/restart/cancelRescue | `PS1/PS0` and guarded rescue | TX/FT8 guards, PS0 double-write, post-PS1 window and read-back |
| `antenna` / `antenna_name` | Integer legacy or live name | Serial `AN…`; Flex `slice set <own> rxant=<name>` | Flex candidates from `ant_list`; one RX antenna command affects the hosting pan/SCU |
| `agc_threshold`, AGC/NB/NR controls | Backend range | `slice set` or CAT/CI-V | Unknown live values remain unknown, not fabricated defaults |
| `scope_span`, `scope_speed`, `scope_mode` | Backend/UI range | `SS…`, CI-V 0x27, pan/network settings | No-scope serial backends may treat these as UI-axis state without sending fake CAT |
| `recording`, `cq` | Boolean/state | Server-side services | Share state/PTT/media infrastructure and broadcast progress |

## 10.5 Service Quality Targets

| Service | Quality Target |
| --- | --- |
| Control | Priority PTT/TUNE bypass queued polls; state read-back/status is authoritative |
| RX audio | Wall-clock 20 ms cadence, bounded queues, no ever-growing stale backlog |
| TX audio | <500 ms media target, 60 ms prebuffer/400 ms cap, visible drops and no cross-owner frames |
| Spectrum | New real frames only, identical fallback suppression, AD-025 per-socket bandwidth |
| Terminal | One session for all channels; media congestion never kills control/PTT; disconnect releases once |
| Backend/controller | Minimum common surface always present; degraded/missing attributes do not prevent safety restart |
| Auth/roles | Persistent 30-day operator/listen sessions, independent SDR domain, no URL credentials |
| Operations | Real endpoint/tunnel/audio/scope/update evidence and off-loop blocking work |
