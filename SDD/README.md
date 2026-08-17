# MRRC Modern / `mrrc_modern` SDD — Software Design Description

> Web Remote Control for supported radios (Yaesu FT-710, Icom IC-7300 / IC-7300MK2)  
> IBM Team Solution Design (TeamSD) v2.3.2 aligned documentation set

## Purpose

This SDD is the canonical design record for the `mrrc_modern` codebase — a Python FastAPI server that bridges a browser to one or more supported HF/50MHz transceivers via pluggable radio backends. It captures requirements, architecture, design decisions, component boundaries, operational model, capability inventory, known gaps, and evolution history.

Runtime facts are derived from `server.py`, `backends/*`, `audio_handler.py`, `radio_state.py`, `poll_scheduler.py`, `scope_handler.py`, `config.py`, `opus_rx.py`, `static/index.html`, `static/ft710_main.js`, `static/ft710_ui.js`, and `static/modules/*`.

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
| Document ID | SDD-MRRC-MODERN-2026-001 |
| SDD Version | V2.20 |
| Baseline Date | 2026-08-17 |
| Status | v1.8.0 Windows Stable; FT710Android native client implemented (JVM tests green, device acceptance pending); IC-7300/MK2 backend implemented |
| Project | MRRC Modern / `mrrc_modern` |
| Primary Radios | Yaesu FT-710; Icom IC-7300 / IC-7300MK2 (selectable via backend) |
| Backend Selection | `MRRC_RADIO_MODEL=ft710\|ic7300\|ic7300mk2` (default `ft710`) |
| Runtime | Python 3.12+, FastAPI, Uvicorn, NumPy, PyAudio |
| Frontend | HTML5, CSS3, vanilla JavaScript, Web Audio API |
| Transport | HTTP/WS for browser; Serial CAT (Yaesu) or USB CI-V (Icom) for radio; FT4222 SPI for FT-710 scope; CI-V 0x27 spectrum for IC-7300 |
| Default Entry | `http://localhost:8888` |

## System at a Glance

```text
Browser (iPhone / Desktop / Tablet)
  | HTTP + WebSocket: /WSradio /WSaudioRX /WSaudioTX /WSspectrum
  v
FastAPI/Uvicorn MRRC Server (server.py)
  | Pluggable backend selected by MRRC_RADIO_MODEL
  |   ft710: Serial CAT (USB Enhanced COM Port, 38400 baud) → Yaesu FT-710
  |          FT4222 SPI (scope_pipe subprocess) → real spectrum data
  |   ic7300/ic7300mk2: USB CI-V serial (115200 8N1, addr 0x94) → Icom IC-7300/MK2
  |                     CI-V 0x27 spectrum on the same port
  | PyAudio (USB Audio device) → RX/TX audio capture/playback
  | Opus codec (libopus) → compressed audio transport
  v
Yaesu FT-710 or Icom IC-7300 / IC-7300MK2 Radio
```

## Capability Summary

| Area | Status | Notes |
|------|--------|-------|
| Mobile UI | Implemented | `static/index.html`, `ft710.css`, `ft710_main.js`, `ft710_ui.js` |
| Radio control | Implemented | Backend-specific command set: frequency, mode, filter, PTT, gains, etc. |
| Spectrum waterfall | Implemented | FT-710: FT4222 SPI real + S-meter fallback; IC-7300: CI-V 0x27 real + S-meter fallback |
| RX audio | Implemented | PyAudio capture → Opus/PCM → /WSaudioRX → browser playback (per-backend sample rate) |
| TX audio | Implemented | Browser mic → /WSaudioTX → Opus decode → PyAudio → radio (per-backend sample rate) |
| S-meter + Multi-meter | Implemented | Backend-specific meter polling (e.g., FT-710 RM3-RM8) |
| Radio telemetry (RI) | Implemented | Hi-SWR, recorder, RX/TX, tuner, scan, squelch-open status from `RI0;` |
| Meter display + AMC | Implemented | `MS`/`AO` commands exposed in state and control path |
| Memory channels | Implemented | `/api/mem_channels` GET/POST with JSON persistence |
| Session authentication | Implemented | Shared-password login; `_auth_tokens` + `mrrc_auth` cookie; all WS gated |
| PTT safety | Implemented | Touch-and-hold, PTT watchdog, dead-man switch, unload beacon |
| Scope visualization | Implemented | 850-point FFT waterfall, frequency scale, S-meter bar |
