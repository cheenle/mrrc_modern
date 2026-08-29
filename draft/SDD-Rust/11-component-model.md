# 11. Component Model

## 11.1 Rust Crate Components

| Component | Current Draft Files | Status |
|-----------|---------------------|--------|
| App assembly | `src/app.rs` | Implemented draft |
| Config | `src/config.rs` | Implemented draft |
| Backend traits | `src/backends/mod.rs` | Implemented draft |
| FT-710 backend core | `src/backends/ft710.rs` | Implemented draft |
| IC-7300 backend core | `src/backends/ic7300.rs` | Implemented draft |
| CI-V protocol | `src/protocol/civ.rs` | Implemented draft |
| WS protocol | `src/protocol/ws.rs` | Implemented draft |
| State | `src/state.rs` | Implemented draft |
| Scheduler model | `src/scheduler.rs` | Implemented draft |
| Audio model | `src/audio.rs` | Implemented draft |
| Scope model | `src/scope.rs` | Implemented draft |
| Memory | `src/memory.rs` | Implemented draft |
| Session/auth | `src/service/session.rs` | Implemented draft |
| Radio command service | `src/service/radio.rs` | Implemented draft |
| Radio actor seam | `src/service/actor.rs` | Implemented draft |
| Transport seam | `src/transport.rs` | Implemented draft |
| Web helpers | `src/web.rs` | Implemented draft |

## 11.2 Future Components

- `web_runtime`: Axum router, middleware, WebSocket upgrade handlers.
- `serial_runtime`: concrete serialport-backed `SerialTransport`.
- `audio_runtime`: CPAL or PortAudio binding implementation.
- `scope_runtime`: FT4222 process manager and CI-V scope queue consumer.
- `atr1000`: optional tuner actor and storage.
- `packaging`: launch scripts and service binaries.

## 11.3 Dependency Rule

Pure protocol modules must not depend on runtime modules. Hardware runtime modules may depend on pure protocol modules. Web handlers may depend on service traits, not concrete device drivers.
