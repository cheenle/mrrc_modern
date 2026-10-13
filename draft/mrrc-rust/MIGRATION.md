# Rust Migration Plan

See also `draft/SDD-Rust/` for the Rust-based Software Design Description.

## Phase 0: Compatibility Freeze

Keep the production Python server unchanged. Treat this draft as the target contract. Record all wire formats, command mappings, poll intervals, and audio frame sizes as tests.

## Phase 1: Pure Core Port

Port and test pure logic first: CI-V codec, FT-710 command builders, state dirty tracking, configuration parsing, audio frame sizing, memory-channel schema validation, and static-path containment.

## Phase 2: Device Actors

Introduce isolated actors for serial CAT/CI-V, scope input, and audio I/O. Each actor owns its blocking resource. Async code communicates through typed channels and never touches raw serial/audio handles directly.

Current draft modules for this phase:

- `service::radio` plans user-facing set commands into backend byte commands and priority metadata.
- `service::session` owns auth-token mapping and TX-audio ownership semantics.
- `scope` owns v1/v2 WebSocket payload encoding and duplicate-frame suppression.
- `memory` owns validation before any future atomic persistence layer.
- `app` wires configuration, backend selection, session registry, memory store, and radio state into one runtime object without touching hardware.

## Phase 3: Web Compatibility Server

Implement an Axum/Tokio-compatible server surface with the same REST and WebSocket paths. The frontend must connect without modification. Authentication remains cookie plus `?token=` for WebSockets.

## Phase 4: Hardware Shadow Runs

Run the Rust server in a lab environment against FT-710 and IC-7300/MK2. Compare command traces, state broadcasts, audio frame cadence, scope frame cadence, PTT latency, and reconnect behavior against the Python baseline.

## Phase 5: SDD Change Control

Before replacing production, update SDD architecture decisions, service model, implementation model, version history, README, AGENTS, packaging guides, and test inventory. AD-001 must be superseded or amended.
