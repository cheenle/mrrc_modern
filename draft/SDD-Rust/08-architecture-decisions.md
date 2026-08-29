# 8. Architecture Decisions

## RAD-001: Replace FastAPI Runtime With Rust Actor Runtime

| Attribute | Value |
|-----------|-------|
| Status | Draft |
| Decision | Use Rust with an actor-oriented server runtime, planned around Tokio/Axum |

The current production system uses Python FastAPI. The Rust draft changes the implementation runtime but not the external protocol. Actors isolate control, serial, audio, scope, auth, and memory responsibilities.

## RAD-002: Preserve Browser Wire Protocol

| Attribute | Value |
|-----------|-------|
| Status | Draft |
| Decision | Keep current REST and WebSocket routes and message shapes |

The existing browser and mobile clients must work unchanged. Protocol models are captured in `protocol::ws` and `web` before any HTTP runtime is introduced.

## RAD-003: Exclusive Blocking Device Actors

| Attribute | Value |
|-----------|-------|
| Status | Draft |
| Decision | Serial, audio, and FT4222 access are owned by dedicated actors or blocking workers |

Blocking device APIs must not run on async reactor threads. `transport::SerialTransport` is the current seam for radio command execution.

## RAD-004: Typed Backend Command Builders

| Attribute | Value |
|-----------|-------|
| Status | Draft |
| Decision | Backend modules expose command builders rather than free-form command strings |

FT-710 errata and CI-V frame rules are encoded in functions with unit tests. This prevents reintroducing known dangerous or invalid command forms.

## RAD-005: Dirty-Field Broadcast Preserved

| Attribute | Value |
|-----------|-------|
| Status | Draft |
| Decision | Rust `RadioState` preserves changed-field tracking and JSON `stateUpdate` generation |

Full-state spam would regress bandwidth and UI behavior. The Rust state model keeps compact broadcasts.

## RAD-006: Tagged Audio Transport Preserved

| Attribute | Value |
|-----------|-------|
| Status | Draft |
| Decision | Keep 1-byte audio codec tag with PCM and Opus variants |

The audio wire format remains compatible with browser and mobile clients.

## RAD-007: 48 kHz Codec Domain Preserved

| Attribute | Value |
|-----------|-------|
| Status | Draft |
| Decision | Keep 48 kHz codec domain and per-backend device-rate bridge |

FT-710 keeps 44.1 kHz device frames with 960 to 882 sample conversion. IC-7300/MK2 remain 48 kHz native.

## RAD-008: PTT Priority And Release Safety Preserved

| Attribute | Value |
|-----------|-------|
| Status | Draft |
| Decision | PTT/TUNE commands carry priority metadata and release remains non-blocking |

The Rust actor sets stale poll guards and priority flags before writing PTT commands. Safety is layered rather than dependent on post-release blocking verification.

## RAD-009: Memory Validation Tightened

| Attribute | Value |
|-----------|-------|
| Status | Draft Delta |
| Decision | Rust memory service validates slot count and fields before persistence |

This is an intentional hardening delta. It must be documented before production replacement because invalid persisted data may be rejected rather than silently accepted.

## RAD-010: Scope Payload Compatibility Preserved

| Attribute | Value |
|-----------|-------|
| Status | Draft |
| Decision | Keep spectrum v1/v2 binary payload formats and no duplicate real-frame broadcast |

The Rust `scope` module encodes payload sizes and frame-count suppression directly.

## RAD-011: Backend Factory Is Explicit

| Attribute | Value |
|-----------|-------|
| Status | Draft |
| Decision | `BackendKey` parses configured model and exposes immutable capabilities |

Runtime construction is deterministic and testable through `AppRuntime::from_config`.

## RAD-012: CI-V Parser Is Pure Core Logic

| Attribute | Value |
|-----------|-------|
| Status | Draft |
| Decision | CI-V framing, BCD helpers, and scope segment parsing live in pure Rust protocol modules |

This enables exhaustive tests before connecting serial hardware.
