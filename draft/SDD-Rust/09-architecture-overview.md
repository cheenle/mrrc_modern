# 9. Architecture Overview

## 9.1 Logical Architecture

```text
AppRuntime
  |-- WebRuntime (future Axum)
  |     |-- Control WS
  |     |-- Audio RX/TX WS
  |     |-- Spectrum WS
  |     |-- REST auth/status/memory
  |-- SessionRegistry / AuthTokenStore
  |-- RadioActor
  |     |-- RadioBackend trait
  |     |-- SerialTransport boundary
  |-- PollScheduler tasks
  |-- AudioActor
  |     |-- Opus/PCM codec domain
  |     |-- device-rate bridge
  |-- ScopeActor
  |     |-- FT4222 path or CI-V 0x27 path
  |-- MemoryStore
```

## 9.2 WebSocket Endpoints

The Rust server keeps the same endpoints:

| Endpoint | Data | Rust Model |
|----------|------|------------|
| `/WSradio` | JSON text | `protocol::ws::RadioClientMessage`, `RadioServerMessage` |
| `/WSaudioRX` | binary tagged audio | `audio`, `protocol::ws::AudioCodecTag` |
| `/WSaudioTX` | binary tagged audio plus text controls | `parse_audio_tx_text` |
| `/WSspectrum` | v1/v2 binary scope frames | `scope::ScopeFrame` |
| `/WSatr1000` | optional JSON | future optional tuner service |

## 9.3 Control Path

`ControlService` receives a JSON `set`, validates it, calls `service::radio` to plan a backend command, and sends it to `RadioActor`. The actor marks user-command pause and skip-poll windows before writing through `SerialTransport`.

## 9.4 Audio Path

Audio remains 20 ms frame based. Codec-domain samples are 48 kHz. FT-710 device frames use 44.1 kHz and exact 20 ms conversion. TX uses `TxJitterBuffer` to prebuffer and cap latency.

## 9.5 Spectrum Path

FT-710 scope frames and IC-7300 CI-V scope segments converge into `ScopeFrame`. Real frames are sent only when the frame counter advances. Fallback spectrum generation will attach behind the same encoder.

## 9.6 Polling Architecture

`PollIntervals::task_plan` defines seven poll tasks: fast IF, VFO, TX status, TX meters, settings, slow telemetry, and connection watchdog. Every poll query uses bounded timeout and checks `StaleReadGuard` after awaited responses.

## 9.7 State Broadcasting

State changes flow through `RadioState::update`. Dirty fields are converted to JSON and wrapped by `web::state_update_message`. Clients continue to merge partial updates.
