# MRRC Rust Rewrite Draft

This directory contains the Rust rewrite workstream only. It does not modify or supersede the production Python/FastAPI runtime.

## Contents

| Path | Purpose |
|------|---------|
| `mrrc-rust/` | Compilable Rust draft crate with tested core protocol/service modules |
| `SDD-Rust/` | Rust-based Software Design Description, rewritten from the production SDD intent |

## Current Scale

- Rust core tests: growing compatibility suite for protocol constants, PTT safety, audio timing, scope payloads, memory validation, session ownership, and backend command guards.
- Rust SDD: 15 chapters plus migration delta and acceptance matrix.

## Current Validation

Run from `draft/mrrc-rust/`:

```bash
cargo fmt
cargo test
cargo clippy --all-targets -- -D warnings
cargo run
```

Run from repository root:

```bash
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check draft
```

## Boundary

All Rust rewrite work stays in `draft/` until the production SDD is formally amended, hardware acceptance is complete, and a migration plan is approved.
