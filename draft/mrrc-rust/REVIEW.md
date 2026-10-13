# MRRC Modern Deep Review And Rust Rewrite Draft

This review is based on the current Python/FastAPI backend, static browser UI, backend plugins, tests, and the SDD guardrails. The files in this directory are a draft Rust migration target, not a replacement for the production runtime.

The Rust-specific design record is now maintained in `draft/SDD-Rust/` and should be read with this implementation draft.

## Current Architecture

The project is organized around a FastAPI server with five authenticated WebSocket channels, a dirty-field `RadioState`, a seven-task poll scheduler, backend-specific CAT/CI-V controllers, USB-audio capture/playback, Opus transport, and spectrum producers. The most valuable design choices are already explicit: backend capability discovery, command serialization at the serial boundary, tagged audio frames, and separated scope producers.

## Findings

1. `server.py` is the highest-risk module because it combines auth, static serving, WebSocket lifecycle, PTT ownership, memory-channel persistence, spectrum fanout, audio fanout, and ATR1000 integration in one global-state process. Rust should split this into `web`, `session`, `radio_service`, `audio_service`, and `scope_service` actors.
2. `PollScheduler` has the right reliability model, but the stale-read and priority-preemption invariants are convention-based. Rust should encode them as a `StaleReadGuard` and cancellation token passed through every poll query path.
3. The backend abstraction is now strong enough to port, but Python still exposes legacy FT-710 names in neutral layers (`CatController` type hints, FT-710 default tables). Rust should define protocol-neutral traits first and keep Yaesu/Icom in separate modules.
4. Audio correctness depends on subtle rate-domain facts. Rust should make `CodecRate=48000`, device-rate capability, frame size, and resample boundary typed constants, with tests for 960 to 882 and 882 to 960 frames.
5. The current security posture is materially improved for static traversal and password compare, but default-password policy remains intentionally incomplete. Rust should not copy the weak-default behavior without a migration decision.
6. `mem_channels.json` persistence is still a reliability and validation edge. Rust should parse a typed schema, validate slot count and fields, and write atomically with backup.
7. CI-V parsing is a good candidate for early Rust migration because it is pure, byte-oriented, and already heavily tested. The draft includes a no-dependency parser and BCD helpers.
8. FT-710 command construction should be centralized so known dangerous errata cannot reappear. The draft exposes only guarded command builders for `SH00NN`, `PR00/PR01`, and `AC000/AC001/AC003`.

## Dialectical Analysis 1: Rewrite Scope

Thesis: A full Rust rewrite can reduce runtime races and encode hardware protocol invariants in types.

Antithesis: The current system has years of field fixes across audio, PTT, serial timing, Windows USB quirks, FT4222 scope behavior, and browser worklets. A direct rewrite risks losing undocumented timing behavior even if the code compiles.

Synthesis: Migrate by seams. Start with pure protocol/state/audio-frame modules, then introduce a Rust sidecar for CI-V or CAT, then replace service actors behind the unchanged WebSocket protocol. Keep the browser protocol stable until hardware acceptance tests pass.

## Dialectical Analysis 2: Async Model

Thesis: Rust async tasks and channels are a better fit than Python globals for client/session ownership, pollers, and fanout.

Antithesis: Async Rust plus serial/audio FFI can become harder to reason about than the current Python if blocking device APIs are mixed into async executors incorrectly.

Synthesis: Use actors with explicit message boundaries. Blocking serial/audio devices must run in dedicated threads or `spawn_blocking`, while async tasks exchange typed messages. The serial actor owns the port exclusively, preserving the current `CatController`/`CivController` invariant.

## Dialectical Analysis 3: Safety Versus Feature Parity

Thesis: Rust should aggressively fix open issues such as memory validation, auth defaults, and multi-client arbitration during the port.

Antithesis: Fixing semantics while changing language makes regressions harder to attribute, especially around PTT and reconnect behavior.

Synthesis: Classify changes into compatibility-preserving port work and intentional behavior changes. Preserve WS message formats, PTT fire-and-forget release, poll cadences, and audio framing. Track security improvements as explicit Rust-only design deltas with tests and SDD updates before production replacement.

## Rust Module Map

| Python Area | Rust Draft Module | Migration Rule |
|-------------|-------------------|----------------|
| `config.py` | `src/config.rs` | Preserve `MRRC_*` first, `FT710_*` fallback |
| `radio_state.py` | `src/state.rs` | Dirty-field tracking is mandatory |
| `backends/base.py` | `src/backends/mod.rs` | Trait-first backend boundary |
| `backends/ft710/cat_controller.py` | `src/backends/ft710.rs` | Guard dangerous CAT command formats |
| `backends/ic7300/civ_codec.py` | `src/protocol/civ.rs` | Pure parser/codec with unit tests |
| `audio_handler.py` / `audio_resample.py` | `src/audio.rs` | 48kHz codec domain, per-backend device rate |
| `poll_scheduler.py` | `src/scheduler.rs` | Seven-tier cadence plus stale-read guard |
| `server.py` | `src/web.rs` and future actors | Keep WS routes and token auth contract |
| TX owner globals | `src/service/session.rs` | PTT client's token claims the audio uplink |
| memory helpers | `src/memory.rs` | Draft tightens schema validation before persistence |
| scope fanout | `src/scope.rs` | Preserve v1/v2 binary payload sizes and no duplicate real frames |

## Acceptance Gates For A Production Rust Port

1. All current Python unit tests must have Rust equivalents or protocol-level compatibility tests.
2. `/WSradio`, `/WSspectrum`, `/WSaudioRX`, `/WSaudioTX`, and optional `/WSatr1000` must remain wire-compatible.
3. Hardware validation must cover FT-710 CAT, FT4222 scope, IC-7300/MK2 CI-V scope, RX/TX audio, PTT release, reconnect, and Windows USB audio recovery.
4. SDD chapter 8 needs a new architecture decision before Rust becomes production, because AD-001 currently commits the server to FastAPI/Uvicorn.

## Draft Implementation Status

The Rust draft now includes typed protocol models for `/WSradio`, audio TX text controls, memory-channel validation, static path containment, TX ownership/session tracking, command planning, CI-V parsing, FT-710 guarded command builders, scope payload encoding, and dirty-state tracking. It is still intentionally not wired to hardware or an HTTP runtime; the next step is adding Tokio/Axum actors behind these tested boundaries.

The next increment added transport and actor seams: `transport` defines the exclusive serial boundary, `service::actor` applies planned set commands through that boundary while marking poll results stale, `audio::TxJitterBuffer` models prebuffer and oldest-frame drops, and `memory` now has an atomic JSON persistence draft. These are still draft-local and do not affect the Python production runtime.

The third increment added backend selection, FT-710 response parsers, CI-V scope segment parsing, and an explicit seven-task poll plan. This keeps the migration anchored to the production SDD invariants before any runtime framework is introduced.
