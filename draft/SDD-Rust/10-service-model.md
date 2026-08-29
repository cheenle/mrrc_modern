# 10. Service Model

## 10.1 Service Portfolio

| Service | Rust Module | Responsibility |
|---------|-------------|----------------|
| AppRuntimeService | `app` | Assemble config, backend, state, sessions, memory |
| StaticUIService | `web` future runtime | Serve frontend with path containment |
| AuthService | `service::session`, future web runtime | Password/token validation, cookie and WS gating |
| ControlService | `protocol::ws`, `service::radio`, `service::actor` | WS command dispatch and state updates |
| BackendFactoryService | `backends` | Select FT-710, IC-7300, or IC-7300MK2 capabilities |
| SerialTransportService | `transport` | Exclusive serial read/write boundary |
| PollingService | `scheduler` | Seven-task poll model and stale-read guard |
| RXAudioService | `audio` future actor | Capture, resample, encode, broadcast |
| TXAudioService | `audio`, `service::session` | Decode, ownership gate, jitter buffer, playback |
| SpectrumService | `scope`, `protocol::civ` | Encode real/fallback spectrum frames |
| MemoryChannelService | `memory` | Validate and atomically persist memory channels |
| ATR1000Service | future optional module | Tuner linkage and tune assist |

## 10.2 Interfaces

| Interface | Input | Output |
|-----------|-------|--------|
| Radio actor set | field/value JSON | backend command write result |
| Serial transport | command bytes, priority flag | response bytes or error |
| State broadcast | dirty state object | `stateUpdate` JSON |
| Audio RX | device PCM | tagged WS frame |
| Audio TX | tagged WS frame | device PCM frame |
| Scope | backend frame/segment | v1/v2 WS payload |
| Memory | channel vector | validated JSON file and broadcast |

## 10.3 Command Contract

Rust command planning preserves current public fields. Backend-specific builders produce the radio bytes. FT-710 examples:

| Field | Rust Planner | Notes |
|-------|--------------|-------|
| `freq` | active VFO frequency command | Uses active VFO from state |
| `vfo_a_freq` | VFO-A frequency command | 9-digit frequency encoding |
| `vfo_b_freq` | VFO-B frequency command | FT-710 direct VFO-B command |
| `mode` | mode command | Hex mode nibble |
| `ptt` | priority TX/RX command | Sets skip key `tx_status` |
| `filter_width` | guarded FT-710 filter-width builder | Uses fixed safe command shape |
| `compressor` | guarded compressor builder | Only valid OFF/ON values |
| `tuner` | guarded tuner builder | OFF/ON/TUNE mapping |

## 10.4 Quality Targets

- PTT commands carry `priority=true`.
- User commands set skip-poll windows before serial write.
- Memory updates are rejected if they fail schema validation.
- Static serving rejects traversal before filesystem response.
