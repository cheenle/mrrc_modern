# 2. Business Direction

## 2.1 Business Objective

MRRC Modern enables safe remote operation of supported amateur-radio transceivers from browsers and mobile clients. The Rust draft supports the same business objective with a lower-risk long-term runtime for concurrency, protocol safety, deployment packaging, and maintainability.

## 2.2 Drivers For Rust

| Driver | Rust Design Response |
|--------|----------------------|
| Safety-critical PTT behavior | Explicit actor ownership, priority command metadata, tested session ownership |
| Protocol correctness | Typed CAT/CI-V command builders and parsers |
| Concurrency complexity | Message-passing actors instead of process-wide mutable globals |
| Long-running reliability | Isolated blocking device actors and explicit reconnect state machines |
| Packaging | Single native server binary target after hardware-dependent libraries are integrated |
| Future radio support | Backend traits and capability model are first-class Rust types |

## 2.3 Non-Goals

- Do not change the frontend protocol during the migration.
- Do not redesign the UI as part of the Rust port.
- Do not replace hardware-specific behavior with generic Hamlib abstractions.
- Do not remove existing safety layers for PTT release.
- Do not claim production readiness before physical hardware acceptance.
