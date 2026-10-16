# MRRC Modern Rust Draft SDD

> Draft-only Software Design Description for a Rust reimplementation of MRRC Modern. This document set does not supersede the production `SDD/` directory until hardware acceptance, protocol compatibility, and formal SDD change control are complete.

## Purpose

This SDD describes the target Rust architecture being developed under `draft/mrrc-rust/`. The goal is a production-compatible replacement for the current Python FastAPI server while preserving browser wire protocols, radio command semantics, PTT safety, audio timing, spectrum formats, authentication rules, and backend capability discovery.

## Document Index

| # | Chapter | File |
|---|---------|------|
| 1 | Executive Summary | [01-executive-summary.md](01-executive-summary.md) |
| 2 | Business Direction | [02-business-direction.md](02-business-direction.md) |
| 3 | Project Definition | [03-project-definition.md](03-project-definition.md) |
| 4 | System Context | [04-system-context.md](04-system-context.md) |
| 5 | Non-Functional Requirements | [05-non-functional-requirements.md](05-non-functional-requirements.md) |
| 6 | Use Case Model | [06-use-case-model.md](06-use-case-model.md) |
| 7 | Subject Area Model | [07-subject-area-model.md](07-subject-area-model.md) |
| 8 | Architecture Decisions | [08-architecture-decisions.md](08-architecture-decisions.md) |
| 9 | Architecture Overview | [09-architecture-overview.md](09-architecture-overview.md) |
| 10 | Service Model | [10-service-model.md](10-service-model.md) |
| 11 | Component Model | [11-component-model.md](11-component-model.md) |
| 12 | Operational Model | [12-operational-model.md](12-operational-model.md) |
| 13 | Feasibility Assessment | [13-feasibility-assessment.md](13-feasibility-assessment.md) |
| 14 | Version History | [14-version-history.md](14-version-history.md) |
| 15 | PTT Safety Architecture | [15-ptt-safety-architecture.md](15-ptt-safety-architecture.md) |
| A | Rust Migration Delta | [RUST-MIGRATION-DELTA.md](RUST-MIGRATION-DELTA.md) |
| B | Acceptance Matrix | [ACCEPTANCE-MATRIX.md](ACCEPTANCE-MATRIX.md) |

## Quick Facts

| Attribute | Value |
|-----------|-------|
| Document ID | SDD-MRRC-RUST-DRAFT-2026-001 |
| Draft Version | R0.2 |
| Baseline Date | 2026-08-29 |
| Status | Draft architecture and tested core modules under `draft/mrrc-rust/`; not production runtime |
| Runtime Target | Rust 1.98+, Tokio/Axum planned, typed actors, explicit blocking-device boundaries |
| Current Implemented Draft Core | config, backend capabilities, FT-710 command guards, CI-V codec/scope parser, state dirty tracking, memory validation/persistence, session ownership, audio frame model, scope payload encoding, command actor seam |
| Production Compatibility Rule | Keep current browser WS/HTTP wire contracts stable until hardware acceptance proves parity |

## Rust System At A Glance

```text
Browser / Mobile App
  | Same HTTP + WebSocket protocol as production
  v
Rust MRRC Runtime
  | app::AppRuntime
  | web service actors: control / audio / spectrum / auth / memory
  | radio actor: exclusive serial boundary + priority command path
  | backend traits: ft710 / ic7300 / ic7300mk2
  | audio actor: 48 kHz codec domain + per-backend device bridge
  | scope actor: FT4222 subprocess path or CI-V 0x27 path
  v
Radio hardware and USB audio devices
```

## Compatibility Contract

The Rust draft is compatible only if these remain unchanged:

- `/WSradio`, `/WSaudioRX`, `/WSaudioTX`, `/WSspectrum`, optional `/WSatr1000` endpoint meanings.
- Audio binary frame tags `0x00` PCM and `0x01` Opus.
- Spectrum binary payload sizes: 851 bytes for v1 and 1701 bytes for v2.
- `fullState`, `stateUpdate`, `value`, `memChannels`, and `pong` JSON message shapes.
- PTT release remains fire-and-forget at the radio command layer, backed by watchdogs and disconnect safety paths.
- FT-710 command errata are encoded as guarded builders, not copied as free-form strings.
