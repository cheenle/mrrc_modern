# 13. Feasibility Assessment

## 13.1 Feasibility Summary

The Rust port is technically feasible because the highest-risk protocol logic can be isolated and tested before hardware integration. The main risks are preserving field-discovered timing behavior and safely binding to audio/FT4222 libraries.

## 13.2 Risks

| ID | Risk | Impact | Mitigation |
|----|------|--------|------------|
| RR1 | Async runtime blocks on device I/O | PTT/audio stalls | Dedicated blocking actors and transport traits |
| RR2 | Audio device behavior differs from Python/PyAudio | TX/RX regressions | Hardware acceptance with actual device rates and queue stats |
| RR3 | FT4222 Rust integration unstable | Spectrum failure | Keep subprocess boundary or wrap existing pipe initially |
| RR4 | Protocol compatibility drift | Existing clients break | JSON and binary protocol tests before HTTP wiring |
| RR5 | PTT release regression | Safety/regulatory hazard | Chapter 15 safety acceptance gates |
| RR6 | Memory validation rejects existing malformed data | Upgrade surprise | Migration sanitizer and backup path |
| RR7 | Multi-client arbitration remains underspecified | Conflicting operators | Preserve current TX-owner rules first; design role lock separately |

## 13.3 Open Issues For Rust

- Concrete HTTP runtime not yet implemented.
- Concrete serialport implementation not yet implemented.
- Concrete audio backend not yet chosen.
- ATR1000 actor not yet ported.
- Hardware acceptance not yet run.
- Production SDD AD-001 still describes FastAPI and must be amended before replacement.

## 13.4 Go/No-Go Criteria

No-go if any of these fail:

- PTT release takes longer than production baseline.
- Audio uses 16 kHz capture/playback anywhere in the codec path.
- FT-710 command guards are bypassed by free-form strings.
- Static path containment is missing.
- WebSocket auth token is optional on any protected endpoint.
