# 4. System Context

## 4.1 Actors

| Actor | Role |
|-------|------|
| Remote operator | Uses browser/mobile UI to control radio, listen, transmit, and view spectrum |
| Browser UI | Existing static frontend; consumes unchanged WebSocket and REST protocols |
| Radio hardware | FT-710 or IC-7300/MK2 connected via USB serial and USB audio |
| FT4222 device | FT-710-only real spectrum source through isolated process/driver boundary |
| ATR1000 tuner | Optional network tuner integrated over a separate WebSocket-like link |
| Operating system | Provides serial ports, audio devices, TLS files, and process lifecycle |

## 4.2 External Interfaces

| Interface | Rust Boundary |
|-----------|---------------|
| Browser HTTP/WS | Future Axum web service; current draft `web` and `protocol::ws` modules |
| Serial CAT/CI-V | `transport::SerialTransport` and backend-specific protocol actors |
| USB Audio | Future audio actor; current draft `audio` frame and jitter-buffer model |
| FT4222 Scope | Future scope actor / subprocess manager; current draft `scope` payload model |
| Memory JSON | `memory::MemoryStore` validation and atomic persistence |
| Environment config | `config::RuntimeConfig` with `MRRC_*` first and legacy fallback |

## 4.3 Context Diagram

```text
Operator
  v
Browser / Mobile Client
  v same HTTP + WS protocol
Rust MRRC Server
  | serial actor       -> radio CAT/CI-V
  | audio actor        -> radio USB audio device
  | scope actor        -> FT4222 or CI-V scope stream
  | memory service     -> JSON persistence
  | optional tuner     -> ATR1000 network service
```
