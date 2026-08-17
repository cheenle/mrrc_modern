# 13. Feasibility Assessment (ART 0530)

## 13.1 Feasibility Summary

| Dimension | Assessment | Explanation |
|-----------|------------|-------------|
| CAT/CI-V control feasibility | High | FT-710 CAT and IC-7300 CI-V command sets implemented and tested via pyserial |
| Spectrum feasibility | High | Dual-mode real scope + S-meter fallback working for both FT-710 (FT4222) and IC-7300/MK2 (CI-V 0x27) |
| RX audio feasibility | High | PyAudio capture → Opus encode → WS broadcast → browser playback (per-backend sample rate) |
| TX audio feasibility | High | Browser mic → Opus encode → WS → decode → PyAudio → radio (per-backend sample rate) |
| Mobile feasibility | Medium-High | Responsive UI adapts to backend `capabilities`; iOS requires HTTPS for mic (reverse proxy) |
| Operational feasibility | High | Single-process server, backend selection via env var, start/stop scripts, PID file management |
| Product completeness | High | All core features (control, audio, spectrum, meters, memories) implemented for both supported backends |

## 13.2 Risks

| ID | Risk | Probability | Impact | Mitigation |
|----|------|-------------|--------|------------|
| R1 | Serial port not found or wrong port | Medium | High | Log available ports; env var configuration; clear error messages |
| R2 | Real scope not available | Medium | Medium | Automatic S-meter fallback; scope_pipe/CI-V 0x27 exits gracefully |
| R3 | PyAudio device not matching selected radio | Low-Medium | Medium | Per-backend name-based auto-detection; device list logging; fallback to system default |
| R4 | TX release command lost | Low | Critical | TX-status poll (500ms) + browser watchdog, dead-man switch, unload beacon |
| R5 | Opus library not available | Low | Medium | Graceful PCM fallback on server and browser |
| R6 | Audio device contention | Low | Medium | PyAudio opens/closes streams on demand; only one TX stream at a time |
| R7 | scope_pipe subprocess crash | Low-Medium | Low | Server continues; falls back to S-meter spectrum; pipe exit handled in finally block |
| R8 | Stale frontend assets | Low | Medium | Service worker bypasses JS/HTML; version query strings |

## 13.3 Assumptions

| ID | Assumption | Confidence | Validation |
|----|------------|------------|------------|
| A1 | Selected radio connected via USB with correct serial parameters (FT-710 Enhanced COM Port at 38400 baud; IC-7300 CI-V at 115200 8N1) | High | Backend ID response |
| A2 | Selected radio USB audio device recognized by OS | High | PyAudio device enumeration |
| A3 | libopus available on server (Homebrew `opus` package) | Medium-High | ctypes find_library("opus") |
| A4 | FTDI libraries in `lib/` match OS architecture (FT-710 backend only) | Medium | scope_pipe startup log |
| A5 | Browser supports WebSocket, Web Audio, Canvas | High | Modern browsers |
| A6 | libft4222.dylib from wfview app bundle for correct version (FT-710 backend only) | Medium | scope_pipe SPI read success |

## 13.4 Current Issues

| ID | Issue | Priority | Status | Resolution Path |
|----|-------|----------|--------|-----------------|
| I1 | iOS Safari requires HTTPS for getUserMedia (mic access) | Medium | Open | Use TLS reverse proxy (nginx) or connect via HTTPS |
| I2 | PyAudio device index not configurable via env var | Low | Resolved (V2.6) | Implemented as `MRRC_AUDIO_RX_DEVICE` / `MRRC_AUDIO_TX_DEVICE` (index or name substring); Windows package pre-locks `USB Audio` |
| I3 | No per-band TX power control (FT-710 uses hardware power setting) | Low | N/A | FT-710 has hardware RF POWER knob; CAT `PC;` command sets power globally |
| I4 | No ATR-1000 / external tuner support | Low | Future | Could add via second serial port |
| I5 | No digital mode support (CW decoder, FT8, RTTY) | Low | Future | Specialized DSP/packet decode needed |
| I6 | No multi-client control arbitration — concurrent browsers can issue conflicting PTT/frequency commands (last-writer-wins) | Medium | Open | Define arbitration rules (e.g., single-controller lock or role-based gating) |
| I7 | `mem_channels.json` POST has no schema validation or backup | Low | Open | Server-side payload validation; keep `.bak` copy before overwrite |

## 13.5 Dependencies

| ID | Dependency | Type | Status |
|----|------------|------|--------|
| D1 | Python 3.12+ | Runtime | Required |
| D2 | FastAPI + Uvicorn | Runtime | Required (pip) |
| D3 | pyserial | Runtime | Required (pip) |
| D4 | PyAudio | Runtime audio | Required for audio (pip + portaudio) |
| D5 | NumPy | Runtime/DSP | Required (pip) |
| D6 | libopus | Optional codec | Optional (brew install opus / apt install libopus0) |
| D7 | libft4222 + libftd2xx | Optional scope | Required for real FT4222 spectrum |
| D8 | Supported radio + USB cable | Hardware | Required |
| D9 | Browser WebSocket/Web Audio/Canvas | Client | Required |

## 13.6 Feasibility Conclusion

MRRC Modern is fully feasible and production-ready for remote operation of supported radios. All core capabilities — backend-specific control (Yaesu CAT / Icom CI-V), bidirectional audio with Opus compression, real-time spectrum waterfall (dual-mode real scope + S-meter fallback), multi-meter telemetry, memory channels, session authentication, and comprehensive PTT safety — are implemented and verified for both the FT-710 and IC-7300/MK2 backends. The primary operational constraint is iOS requiring HTTPS for microphone access, solvable with a TLS reverse proxy. FT-710 FT4222 scope requires specific library setup but degrades gracefully to S-meter fallback; IC-7300/MK2 0x27 spectrum requires only the CI-V USB serial port.
