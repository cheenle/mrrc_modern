# 3. Project Definition (ENG 343)

## 3.1 Project Attributes

| Attribute | Value |
|-----------|-------|
| Project Name | MRRC FT-710 |
| Project Type | Web remote control for Yaesu FT-710 transceiver |
| Primary Users | HAM operators using phone or desktop browsers |
| Primary Radio | Yaesu FT-710 (HF/50MHz superheterodyne transceiver) |
| Server Platform | macOS/Linux with Python 3.12+ |
| Client Platform | Modern browser (Safari 15+, Chrome, Firefox) |
| Runtime Framework | FastAPI + Uvicorn |
| Frontend Stack | HTML/CSS/vanilla JavaScript/Web Audio API/Canvas |
| Radio Interface | Serial CAT (USB Enhanced COM Port, 38400 baud) |
| Scope Interface | FTDI FT4222 SPI via standalone Python subprocess |
| Audio Interface | PyAudio sound card capture/playback + libopus codec |

## 3.2 In Scope

- Serve a mobile-first web UI from `static/`.
- Maintain WebSocket control channel `/WSradio` with JSON protocol.
- Maintain RX audio channel `/WSaudioRX` using tagged dual-codec frames (Opus 48kHz default, Int16 PCM fallback).
- Maintain TX audio channel `/WSaudioTX` for browser microphone uplink.
- Maintain spectrum channel `/WSspectrum` with binary 850/1701-byte frames at ~30 fps.
- Implement dual-mode spectrum: FT4222 SPI (real FFT) + S-meter Gaussian fallback.
- Implement full FT-710 CAT command set via serial port with threaded I/O.
- 7-task background polling with adaptive skip-on-command.
- Multi-meter: PWR, ALC, SWR, Id, Vd from CAT RM3-RM8.
- S-meter from both CAT SM0 and scope frame metadata.
- Persist and serve memory channels via `/api/mem_channels`.
- Session authentication: shared-password login, cookie + token, all WS gated.
- PTT safety: touch-and-hold, browser watchdog, dead-man switch, unload beacon.
- Scope pipe protocol for FT4222 subprocess communication.

## 3.3 Out of Scope

- Native iOS/Android application **as a deliverable of this SDD**. Separate native clients — iOS (`FT710Mobile/`) and Android (`FT710Android/`, Kotlin + Jetpack Compose, implemented 2026-08-16) — and a marketing/documentation site (`website/`) live in this repository but are documented independently (`docs/IOS_*.md`, `FT710Mobile/CLAUDE.md`, `FT710Android/CLAUDE.md`, `docs/superpowers/specs/2026-08-16-ft710-android-app-design.md`) — they are outside this SDD's scope.
- Cloud-hosted multi-tenant service.
- Multi-user / per-user authentication (current auth is single shared password).
- Digital modes (CW decoder, FT8, RTTY decode).
- Logbook / QSO logging.
- Antenna tuner control (ATR-1000 or similar).
- Hamlib/rigctld integration; this codebase is FT-710 direct CAT.
- WDSP / advanced DSP processing (NR2, ANF, SNB — the FT-710 has its own hardware DSP).
- SDR IQ streaming (FT-710 is a superheterodyne, not an SDR).

## 3.4 Success Criteria

| ID | Criterion | Verification |
|----|-----------|--------------|
| SC1 | Serial CAT connection establishes | Server log shows "Connected to FT-710 (ID=...)" |
| SC2 | All radio controls work via Web UI | Frequency, mode, filter, PTT, gains, etc. respond correctly |
| SC3 | RX audio arrives at browser | `/WSaudioRX` receives tagged frames; audio plays through speakers |
| SC4 | TX audio reaches radio | PTT + mic → audible RF output on monitoring receiver |
| SC5 | Spectrum waterfall renders | Canvas shows 120-row history with frequency scale |
| SC6 | FT4222 scope works when available | Real 850-point FFT data at ~21fps |
| SC7 | S-meter fallback works without FT4222 | Synthetic Gaussian spectrum from CAT SM0 readings |
| SC8 | PTT cannot stick | Touch release always returns radio to RX |
| SC9 | Memory channels persist across restarts | Save channel, restart server, channel still present |

## 3.5 Major Milestones

| Milestone | Date | Deliverable |
|-----------|------|-------------|
| M1 | 2026-06 | CAT serial protocol + basic WebSocket control |
| M2 | 2026-06 | Spectrum scope (FT4222 + S-meter fallback) |
| M3 | 2026-06 | Multi-meter + S-meter visualization |
| M4 | 2026-06 | PTT safety architecture (triple verify + dead-man) |
| M5 | 2026-06 | Memory channel API |
| M6 | 2026-07 | Audio pipeline: PyAudio capture/playback + Opus codec + /WSaudioRX + /WSaudioTX |
| M7 | 2026-07 | AudioWorklet RX playback + TX mic capture in browser |
| M8 | 2026-07 | SDD documentation baseline |
