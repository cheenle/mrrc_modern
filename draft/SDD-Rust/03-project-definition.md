# 3. Project Definition

## 3.1 Scope

The Rust project covers the server-side runtime currently implemented by the Python modules at repository root and under `backends/`. The browser UI, iOS app, and Android app remain clients of the same protocol.

## 3.2 In Scope

- Web server and WebSocket endpoints.
- Auth token and cookie handling.
- Static UI serving with path containment.
- Radio backend factory for FT-710, IC-7300, and IC-7300MK2.
- Yaesu CAT command construction/parsing.
- Icom CI-V framing, demux, response matching, and scope segment parsing.
- Poll scheduler with stale-read protection and priority preemption.
- Dirty-field state model and state broadcast generation.
- RX/TX audio services and Opus/PCM tagged frame transport.
- Scope fanout and fallback generation interface.
- Memory channel validation and atomic persistence.
- Optional ATR1000 service compatibility.

## 3.3 Out Of Scope For R0.1

- Production hardware driver integration.
- Replacing the current Python service scripts.
- Changing mobile app protocols.
- Changing regulatory/safety user behavior.

## 3.4 Deliverables

- Rust library crate with tested core protocol and service logic.
- Rust SDD draft document set in `draft/SDD-Rust/`.
- Migration delta from Python architecture to Rust architecture.
- Future Axum/Tokio runtime plan.
