# 2. Business Direction (BUS 411)

## 2.1 Vision

Make supported radios (Yaesu FT-710 and Icom IC-7300 / IC-7300MK2) usable from any phone browser with zero app installation: open a URL, see the spectrum, hear the audio, control the radio, and safely transmit — all through a single Python process.

## 2.2 Mission

Deliver a pragmatic, browser-native remote control surface for supported radios that favors direct serial protocol integration (Yaesu ASCII CAT or Icom CI-V), mobile ergonomics, low latency audio, and field maintainability over heavyweight framework or native-app complexity.

## 2.3 Business Goals

| ID | Goal | Description |
|----|------|-------------|
| G1 | Mobile RX confidence | Operator can reliably listen to the selected radio and see spectrum from phone browser |
| G2 | Safe remote control | Frequency, mode, PTT, tune, gain, and DSP controls behave predictably on supported radios |
| G3 | Bidirectional audio | RX audio streams to browser; TX audio from browser reaches radio |
| G4 | Minimal deployment | Single Python process serves UI, WebSockets, audio, scope, and radio bridge |
| G5 | Design continuity | SDD records implementation facts, decisions, risks, and future work |
| G6 | Visual signal awareness | Real-time waterfall + S-meter + multi-meter provide full operating context |

## 2.4 Objectives

| ID | Objective | Target | Current Status |
|----|-----------|--------|----------------|
| O1 | Full CAT control | All essential radio commands via WebSocket | Implemented |
| O2 | RX audio streaming | Browser receives continuous tagged dual-codec audio (Opus default, PCM fallback) | Implemented |
| O3 | TX audio uplink | Browser microphone → radio transmitter | Implemented |
| O4 | Real-time spectrum | Waterfall from FT4222 SPI or S-meter fallback | Implemented |
| O5 | PTT release safety | Multiple release safeguards | Implemented |
| O6 | Memory channels | Save/recall via Web UI + server persistence | Implemented |
| O7 | Multi-meter | Real-time PWR/ALC/SWR/Id/Vd displays | Implemented |
| O8 | Mobile-first UX | Optimized for touch, safe areas, one-screen operation | Implemented |

## 2.5 Strategy

| ID | Strategy | Description |
|----|----------|-------------|
| S1 | Mobile-first UI | Touch-optimized controls, safe-area support, PWA manifest |
| S2 | Direct radio protocol | Use serial CAT or CI-V directly; no Hamlib/rigctld dependency |
| S3 | Browser-native audio | RX playback via Web Audio/AudioWorklet; TX via getUserMedia |
| S4 | Small service surface | FastAPI owns static files, WebSockets, backend, audio in one process |
| S5 | Dual-mode spectrum | Real scope data when available (FT4222 SPI for FT-710, CI-V 0x27 for IC-7300), S-meter fallback always works |
| S6 | Document actual state | SDD distinguishes implemented from planned features |

## 2.6 Tactics

| ID | Tactic | Implementation |
|----|--------|----------------|
| T1 | Touch-and-hold PTT | `mousedown`/`touchstart` → TX; `mouseup`/`touchend` → RX |
| T2 | Fire-and-forget TX0 + poll verify | `TX0;` on release; 500ms TX-status poll + browser watchdog catch stuck keyup |
| T3 | AudioWorklet RX | Low-latency playback with jitter buffer (prebuffer 220ms, recovery 90ms) |
| T4 | Opus audio compression | ~64kbps Opus vs ~768kbps PCM — 12× bandwidth reduction |
| T5 | PyAudio radio detection | Auto-detect supported radio USB audio device by name (per-backend device hints) |
| T6 | scope_pipe subprocess | Isolate FT-710 FT4222 SPI I/O from asyncio event loop (FT-710 backend) |
