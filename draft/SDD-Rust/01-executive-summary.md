# 1. Executive Summary

The Rust draft redesigns MRRC Modern as a typed, actor-oriented runtime while preserving the existing browser and hardware behavior. The current production server is Python/FastAPI; this draft targets Rust for stricter protocol modeling, safer shared-state boundaries, explicit blocking I/O isolation, and compile-time enforcement of radio-specific command rules.

The project is not a greenfield protocol redesign. It is a compatibility-preserving port whose first success criterion is that the existing browser UI and mobile clients connect without modification. The Rust runtime must reproduce current control, audio, spectrum, memory, authentication, backend-selection, and PTT safety semantics before it can replace production.

## 1.1 Strategic Outcome

The desired end state is a Rust server that:

- Exposes the same HTTP and WebSocket API as the current runtime.
- Encodes radio command rules in typed backends instead of ad hoc strings.
- Keeps serial, audio, and scope hardware access behind exclusive actor boundaries.
- Preserves field-proven timing constraints such as poll timeouts, audio frame sizes, and PTT priority handling.
- Provides better validation for memory channels, auth tokens, static paths, and future multi-client arbitration.

## 1.2 Current Draft Status

The draft implementation under `draft/mrrc-rust/` already includes tested modules for configuration, backend selection, FT-710 command builders, CI-V framing, scope parsing, audio frame/rate logic, TX jitter buffering, state dirty tracking, memory validation/persistence, session ownership, command planning, and runtime assembly. Hardware I/O and HTTP server wiring are intentionally not yet active.

## 1.3 Replacement Policy

The Rust implementation may not replace production until:

- It passes protocol compatibility tests against the existing frontend.
- It passes hardware acceptance on FT-710 and IC-7300/MK2.
- It demonstrates PTT release safety under disconnect, crash, page-close, and watchdog scenarios.
- It preserves RX/TX audio timing and sample-rate boundaries.
- The production SDD is formally amended to supersede the Python FastAPI architecture decision.
