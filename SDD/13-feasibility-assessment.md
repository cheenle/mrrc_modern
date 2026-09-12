# 13. Feasibility Assessment (ART 0530)

## 13.1 Feasibility Summary

| Dimension | Assessment | Explanation |
|-----------|------------|-------------|
| CAT/CI-V control feasibility | High | FT-710 CAT is field-tested; IC-7300/MK2 CI-V frame construction/parsing is conformance-tested against Icom manuals, with physical-radio ACK/timing acceptance pending |
| Spectrum feasibility | High | FT-710 FT4222 is field-tested; IC-7300/MK2 CI-V 0x27 activation, parsing, and queue behavior are software-tested, with physical waveform cadence pending |
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
| R12 | 四台 Yaesu 机型（FTDX10/FTDX101D/MP/FTX-1F）的 CAT 表、S 表曲线与音频采样率来自离线证据（Hamlib 4.7.2 + 手册），**没有真机验证**；频谱通路在 Yaesu CAT 参考中不存在 | Medium | Medium | V2.46：profile 驱动 + 逐表 `provenance` + `unverified_meters` 标注；TX 走 `MRRC_ALLOW_UNVERIFIED_TX` 门禁（默认拒绝发射）；`ID;` 只读校验；`_diag_yaesu.py` 供现场一键回传（含音频枚举与表头对照），闭合前不声称已验证 |
| R11 | 电台 USB 串口桥在物理层反复重枚举（macOS `ENXIO`/`ENOENT`），导致 CAT 周期性中断（现场单日 171 次）；软件无法阻止，只能快速恢复 | Medium | Low | V2.43：重连不再跑 24 条全量同步（原先占串口锁约 62 秒），改由轮询分层在数秒内补齐；退避提示 + 日志静音 + ENXIO 可操作提示；硬件侧建议见运维文档（直连 USB、避免无源 HUB、检查线缆/供电） |
| R10 | Recording disk growth is unbounded **by operator choice** — recordings are never deleted automatically; a long-running unattended server can fill the disk (the Pi image boots from an SD card) | Medium | Low-Medium | Deliberate per the V2.42 design review: the panel shows total usage, the per-session cap `MRRC_RECORDINGS_MAX_SESSION_MIN` (default 240 min) stops a forgotten recording without deleting anything, and `MRRC_RECORDINGS_DIR` can point at a bigger volume |
| R9 | Unverified model profiles (IC-705/IC-7610/IC-7760) ship without hardware evidence — the 689-bin amplitude ceiling, the inherited+rescaled meter curves and the 48 kHz USB-audio properties are assumptions, and an unverified radio could be keyed with a frame the profile got wrong | Medium-High | Medium | `verified=False` + `unverified_meters` capability list; PTT/TUNE refused at the backend boundary until `MRRC_ALLOW_UNVERIFIED_TX=1` (release never gated); `_diag_civ.py` report round-trip turns each assumption into a measurement; SDD V2.41 |

## 13.3 Assumptions

| ID | Assumption | Confidence | Validation |
|----|------------|------------|------------|
| A1 | Selected radio connected via USB with correct serial parameters (FT-710 Enhanced COM Port at 38400 baud; IC-7300 CI-V at explicit 115200 8N1 with USB port unlinked from [REMOTE]) | High | Backend ID response; physical-radio acceptance checklist |
| A2 | Selected radio USB audio device recognized by OS | High | PyAudio device enumeration |
| A3 | libopus available on server (Homebrew `opus` package) | Medium-High | ctypes find_library("opus") |
| A4 | FTDI libraries in `lib/` match OS architecture (FT-710 backend only) | Medium | scope_pipe startup log |
| A5 | Browser supports WebSocket, Web Audio, Canvas | High | Modern browsers |
| A6 | libft4222.dylib from wfview app bundle for correct version (FT-710 backend only) | Medium | scope_pipe SPI read success |
| A8 | `lameenc` ships prebuilt wheels for every target platform (macOS arm64/x86_64, Windows x64, Linux aarch64), so the recorder needs no ffmpeg and no compiler on the build machines | High | Verified on macOS locally; must be re-verified inside the DMG and the Windows installer by recording a short sample (packaging step in the V2.42 plan) |
| A7 | The IC-705/IC-7610/IC-7760 share the IC-7300 CI-V command surface (frequency/mode/preamp/AGC/NB/NR/compressor/filter-width/squelch/RF-power/PTT/tune, meter sub-codes) — evidenced by identical wfview rig command tables, not by a radio | Medium | `_diag_civ.py` read-only probe step; `19 00` identity log |
| A9 | The four Yaesu models share the FT-710's ASCII-CAT command surface (frequency/mode/filter/PTT/gains/meters/memory), differing mainly in mode codes, filter slots, bands, power class and meter curves — evidenced by Hamlib 4.7.2's shared `newcat.c` core plus per-model table files (`ftdx10.c`, `ftdx101.c`, `ftdx101mp.c`, `ftx1/`) and the FTX-1 CAT reference (90/90 commands mapped) | Verified offline; no unit tested, so every derived table carries provenance and the transmit gate stays on (AD-019) |

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
| I8 | `serve_static` path traversal — `STATIC_DIR / path` (server.py) builds the response path from the request URL without `resolve()` + containment check, so an authenticated non-browser client can read arbitrary server-readable files (e.g. `GET /../server.py`, cert keys). Browsers normalize `..`, so exposure is raw-HTTP clients. | High | **Resolved 2026-08-26** (`_resolve_static_path` resolves the join and rejects unless contained inside `STATIC_DIR`; traversal and absolute request paths now 404 instead of falling through to the SPA fallback; regression tests in `tests/test_server_security.py`) | ~~Resolve the joined path and reject unless `is_relative_to(STATIC_DIR)` before `FileResponse`; add a regression test~~ Done |
| I9 | Login password compared with `!=` (non-constant-time, timing side channel) and a weak default password only logs a warning — a fresh install that skips the warning is effectively open. | Medium | **Partially resolved 2026-08-26** (`hmac.compare_digest` via `_password_matches`; `_warn_if_default_password()` fires a loud startup WARNING when the well-known default is active; login-time <12-char warning kept). Still open: first-login forced password change, shorter cookie TTL. | ~~Switch to `hmac.compare_digest`~~ Done; forced first-login change remains future work |
| I10 | Web client audio/spectrum subchannels (`/WSaudioRX`, `/WSaudioTX`, `/WSspectrum`) have no independent reconnect — only `/WSradio` auto-reconnects, so a transient drop yields "controls alive but audio dead" until the control channel also cycles. | Medium | **Resolved 2026-08-26** (`subchannelReconnect` gives each subchannel its own exponential backoff 1s→30s with on-open reset, CONNECTING-state guards against socket stacking, auth-expiry 4001 stays with the control flow, and `_webClientOff` suppresses self-heal while the power button has the client OFF; contract test in `tests/test_server_ws_protocol.py`) | ~~Give each subchannel its own exponential-backoff reconnect~~ Done |
| I11 | iOS app PTT release race (P0 in docs/IOS_APP_ANALYSIS.md): `DragGesture.onEnded` sends `ptt:false` only when the server echo already shows `tx_status > 0`, and there is no client watchdog/scenePhase guard — fast taps over WAN can leave the radio keyed up. Approved PTTManager fix design not yet implemented (0/N tasks). | High | **Partially resolved 2026-08-26** (release race fixed: PTT gesture now tracks local `pttHeld` like TUNE's `tuneHeld`, releases unconditionally on gesture end with optimistic local `txStatus` update in `RadioViewModel.setPTT`; requires device verification). Still open: 500 ms×3 watchdog, scenePhase force-release. Server side gained an opt-in stuck-keyup layer: `MRRC_PTT_MAX_TX_SECONDS` watchdog forces RX after continuous TX beyond the limit, covering zombie-but-connected sockets that neither client watchdogs nor the dead-man switch see. | ~~Unconditional release on gesture end~~ Done; watchdog + scenePhase remain |
| I12 | Stuck-keyup gap for connected-but-hung clients: every existing TX-release layer assumes either a working client or a disconnect. A zombie socket mid-TX keys the radio forever. | High | **Resolved 2026-08-26** (server-side `_max_tx_watchdog`, opt-in via `MRRC_PTT_MAX_TX_SECONDS`, default off so operator QSO patterns are never interrupted; fire-and-forget unkey + zeroed TX meters + error toast, no verify loop per ch15) | Opt-in max-continuous-TX force-RX implemented as a new outermost safety layer |

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

MRRC Modern is fully feasible for remote operation of supported radios. Core software paths — backend-specific control, bidirectional Opus audio, spectrum waterfall, meter telemetry, memories, authentication, and PTT safety — are implemented. FT-710 has field-test history; the IC-7300/MK2 CI-V byte formats and asynchronous behavior are conformance-tested without hardware, so physical USB enumeration, command ACK timing, scope cadence, RF/tuner/power behavior, and RX/TX audio quality remain acceptance items rather than verified claims. iOS still requires HTTPS for microphone access. FT-710 FT4222 scope requires its libraries and degrades to S-meter fallback; IC-7300/MK2 scope requires the CI-V USB port unlinked from [REMOTE] at an explicitly selected 115200 baud.
