# 1. Executive Summary

## 1.1 Project Overview

MRRC FT-710 is a mobile-first browser remote-control system for the Yaesu FT-710 HF/50MHz transceiver. It provides a web UI, WebSocket control/audio/spectrum channels, serial CAT protocol communication, FT4222 SPI scope data capture, PyAudio sound card audio streaming, Opus codec compression, and real-time waterfall/S-meter/multi-meter visualization.

The codebase is a standalone Python FastAPI/Uvicorn service. It does not depend on wfview, Hamlib, or any external radio middleware — CAT commands are sent directly over a serial port (USB Enhanced COM Port), scope data is read from the FTDI FT4222 SPI chip via a subprocess, and RX/TX audio is captured/played via the FT-710's built-in USB audio interface through PyAudio.

## 1.2 Current Design Goals

| Goal | Target | Current Evidence |
| ------ | -------- | ------------------ |
| Mobile-first operation | iPhone/mobile browser as primary UI | `static/index.html`, `ft710.css`, `ft710_main.js`, `ft710_ui.js` |
| Full radio control | All essential CAT commands via WebSocket | `cat_controller.py` with 40+ command helpers |
| Real-time spectrum | Waterfall from FT4222 SPI or S-meter fallback | `scope_pipe.py`, `scope_handler.py` |
| Bidirectional audio | RX audio from radio to browser; TX audio from browser to radio | `audio_handler.py`, `/WSaudioRX`, `/WSaudioTX` |
| Safe PTT handling | Multiple layered safeguards against stuck TX | `ptt_manager.js` watchdog, dead-man switch, unload beacon |
| Minimal deployment | Single Python process serves UI, WS, audio, scope, radio bridge | `server.py` FastAPI lifespan |

## 1.3 Implemented Core Features

| Feature | Status | Description |
| --------- | -------- | ------------- |
| Mobile PWA-style UI | Implemented | Safe-area support, manifest, service worker, dark amber theme |
| Control WebSocket | Implemented | `/WSradio` JSON: fullState, stateUpdate, set/get commands, auth |
| RX audio WebSocket | Implemented | `/WSaudioRX` tagged dual-codec frames (0x00=PCM, 0x01=Opus 48kHz mono) |
| TX audio WebSocket | Implemented | `/WSaudioTX` tagged mic frames → Opus decode → PyAudio → radio |
| Spectrum WebSocket | Implemented | `/WSspectrum` binary: v1=851B wf1, v2=1701B wf1+wf2, ~30fps |
| Spectrum dual-mode | Implemented | FT4222 SPI (real FFT data) + S-meter fallback (synthetic Gaussian peaks) |
| Serial CAT protocol | Implemented | Full FT-710 CAT command set via pyserial, asyncio.to_thread() I/O |
| 7-task polling | Implemented | 100ms–5s adaptive polling (7 asyncio tasks) with skip-on-command |
| S-meter + Multi-meter | Implemented | Canvas S-meter bar + PWR/ALC/SWR/Id/Vd horizontal bar meters |
| Memory channels | Implemented | `/api/mem_channels` GET/POST with `mem_channels.json` persistence |
| Session auth | Implemented | Password login, `ft710_auth` cookie (30-day), `?token=` on WebSocket |
| PTT safety | Implemented | Touch-and-hold TX; PTT watchdog; dead-man switch; unload beacon |

## 1.4 Architecture Layers

![Layer Architecture](diagrams/layer-architecture.svg)

## 1.5 Current Project Status

As of 2026-07-06, the full control + spectrum + audio pipeline is implemented. RX audio is captured from the FT-710's USB audio interface via PyAudio, encoded with Opus (48kHz mono), and streamed to the browser via `/WSaudioRX`. TX audio is captured from the browser microphone, encoded with Opus (or PCM fallback), decoded server-side, and played to the FT-710 via PyAudio output. PTT triggers TX audio stream start/stop. Spectrum comes from the FT4222 SPI chip (when available) with automatic S-meter fallback. All radio controls (frequency, mode, filter, gains, PTT, NR/NB/AN, compressor, ATU, scope settings) are available through the WebSocket JSON API.
