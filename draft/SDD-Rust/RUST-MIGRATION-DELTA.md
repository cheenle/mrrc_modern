# Rust Migration Delta

## Architectural Deltas

| Area | Production Python | Rust Draft Target |
|------|-------------------|-------------------|
| Runtime | FastAPI/Uvicorn globals plus async tasks | Actor-oriented runtime with typed service boundaries |
| Serial | pyserial plus asyncio lock/to_thread | exclusive `SerialTransport` actor / blocking worker |
| Web protocol | dynamic dict JSON | serde-modeled JSON messages |
| State | dataclass and dirty set | typed map and dirty JSON projection |
| Backend factory | Python lazy factory | `BackendKey` plus trait/capability model |
| Audio | PyAudio and ctypes Opus | future audio actor; frame/rate model already encoded |
| Memory | JSON read/write | validation plus atomic JSON persistence |
| Scope | Python handler and subprocess/CI-V path | typed payload encoder and CI-V segment parser |
| Auth | token set in process | token TTL store plus restart-clear semantics |

## Compatibility-Preserving Items

- Endpoint names.
- WS message shapes.
- Audio tags.
- Spectrum payload sizes.
- PTT priority and release semantics.
- Backend selection environment variable.

## Intentional Hardening Items

- Memory POST validation.
- Static path containment as a reusable function.
- Typed command planning for known radio errata.
- Token expiration model as explicit runtime state.

## Required Before Production Replacement

1. Implement concrete Axum/Tokio web runtime.
2. Implement concrete serial/audio/scope runtime layers.
3. Build compatibility test suite against current frontend.
4. Run hardware acceptance on supported radios.
5. Amend the production SDD, README, packaging docs, and test inventory.
