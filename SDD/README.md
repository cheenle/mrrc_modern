# MRRC FT-710 SDD — Software Design Description

> Yaesu FT-710 Web Remote Control  
> IBM Team Solution Design (TeamSD) v2.3.2 aligned documentation set

## Purpose

This SDD is the canonical design record for the `mrrc_ft710` codebase — a Python FastAPI server that bridges a browser to a Yaesu FT-710 HF/50MHz transceiver via serial CAT protocol. It captures requirements, architecture, design decisions, component boundaries, operational model, capability inventory, known gaps, and evolution history.

Runtime facts are derived from `server.py`, `cat_controller.py`, `audio_handler.py`, `radio_state.py`, `poll_scheduler.py`, `scope_handler.py`, `scope_pipe.py`, `config.py`, `opus_rx.py`, `static/index.html`, `static/ft710_main.js`, `static/ft710_ui.js`, and `static/modules/*`.

## Document Index

| # | Chapter | ART Code | File |
|---|---------|----------|------|
| 1 | Executive Summary | - | [01-executive-summary.md](01-executive-summary.md) |
| 2 | Business Direction | BUS 411 | [02-business-direction.md](02-business-direction.md) |
| 3 | Project Definition | ENG 343 | [03-project-definition.md](03-project-definition.md) |
| 4 | System Context | APP 011 | [04-system-context.md](04-system-context.md) |
| 5 | Non-Functional Requirements | ART 0507 | [05-non-functional-requirements.md](05-non-functional-requirements.md) |
| 6 | Use Case Model | ART 0508 | [06-use-case-model.md](06-use-case-model.md) |
| 7 | Subject Area Model | APP 408 | [07-subject-area-model.md](07-subject-area-model.md) |
| 8 | Architecture Decisions | ART 0513 | [08-architecture-decisions.md](08-architecture-decisions.md) |
| 9 | Architecture Overview | ART 0512 | [09-architecture-overview.md](09-architecture-overview.md) |
| 10 | Service Model | ART 0582 | [10-service-model.md](10-service-model.md) |
| 11 | Component Model | ART 0515 | [11-component-model.md](11-component-model.md) |
| 12 | Operational Model | ART 0522 | [12-operational-model.md](12-operational-model.md) |
| 13 | Feasibility Assessment | ART 0530 | [13-feasibility-assessment.md](13-feasibility-assessment.md) |
| 14 | Version History | - | [14-version-history.md](14-version-history.md) |
| 15 | PTT Safety Architecture | ART 0535 | [15-ptt-safety-architecture.md](15-ptt-safety-architecture.md) |

## Quick Facts

| Attribute | Value |
|-----------|-------|
| Document ID | SDD-MRRC-FT710-2026-001 |
| SDD Version | V2.13 |
| Baseline Date | 2026-07-26 |
| Status | Production release (documentation synchronized with runtime) |
| Project | MRRC FT-710 |
| Primary Radio | Yaesu FT-710 (HF/50MHz Transceiver) |
| Runtime | Python 3.12+, FastAPI, Uvicorn, NumPy, PyAudio |
| Frontend | HTML5, CSS3, vanilla JavaScript, Web Audio API |
| Transport | HTTP/WS for browser, Serial CAT for radio, FT4222 SPI for scope |
| Default Entry | `http://localhost:8888` |

## System at a Glance

```text
Browser (iPhone / Desktop / Tablet)
  | HTTP + WebSocket: /WSradio /WSaudioRX /WSaudioTX /WSspectrum
  v
FastAPI/Uvicorn MRRC FT-710 Server (server.py)
  | Serial CAT (USB Enhanced COM Port, 38400 baud) → Yaesu FT-710
  | FT4222 SPI (scope_pipe subprocess) → real spectrum data
  | PyAudio (USB Audio device) → RX/TX audio capture/playback
  | Opus codec (libopus) → compressed audio transport
  v
Yaesu FT-710 Radio
```

## Capability Summary

| Area | Status | Notes |
|------|--------|-------|
| Mobile UI | Implemented | `static/index.html`, `ft710.css`, `ft710_main.js`, `ft710_ui.js` |
| Radio control | Implemented | Full CAT command set: frequency, mode, filter, PTT, gains, etc. |
| Spectrum waterfall | Implemented | FT4222 SPI real + S-meter fallback dual-mode |
| RX audio | Implemented | PyAudio capture → Opus/PCM → /WSaudioRX → browser playback |
| TX audio | Implemented | Browser mic → /WSaudioTX → Opus decode → PyAudio → radio |
| S-meter + Multi-meter | Implemented | PWR, ALC, SWR, Id, Vd from CAT RM3-RM8 polling |
| Radio telemetry (RI) | Implemented | Hi-SWR, recorder, RX/TX, tuner, scan, squelch-open status from `RI0;` |
| Meter display + AMC | Implemented | `MS`/`AO` commands exposed in state and control path |
| Memory channels | Implemented | `/api/mem_channels` GET/POST with JSON persistence |
| Session authentication | Implemented | Shared-password login; `_auth_tokens` + `ft710_auth` cookie; all WS gated |
| PTT safety | Implemented | Touch-and-hold, PTT watchdog, dead-man switch, unload beacon |
| Scope visualization | Implemented | 850-point FFT waterfall, frequency scale, S-meter bar |
