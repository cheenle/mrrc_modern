# Repository Guidelines

## Project Structure & Module Organization

This repository contains a Python FastAPI server for remote radio control (Yaesu FT-710 and Icom IC-7300/IC-7300MK2 via pluggable backends) plus a static browser UI. Core backend modules live at the repository root:

| Module | Responsibility |
|--------|----------------|
| `server.py` | FastAPI app, auth, 4 WebSocket endpoints, REST APIs, lifespan management; TX uplink ownership follows the PTT client and same-session replacement connections |
| `cat_controller.py` | Compatibility shim — real module moved to `backends/ft710/cat_controller.py`: Serial CAT protocol (pyserial + asyncio.to_thread), 40+ command helpers |
| `radio_state.py` | `RadioState` dataclass with dirty-field change tracking and derived properties |
| `poll_scheduler.py` | 7-task adaptive background polling (100ms→5s) with skip-on-command and post-query stale-read discard; watchdog re-runs scope init (`on_reconnected`) after serial reconnect |
| `audio_handler.py` | PyAudio sound card capture/playback with per-backend device rate and name hints (FT-710: fixed 44.1kHz with 960→882 resample before TX; IC-7300: 48kHz native, no resample), Opus encode, radio USB-audio auto-detection (FT-710/YAESU name, "USB Audio CODEC"/"USB Audio Device" Windows names, mono/full-duplex heuristics); TX session stats include oldest-frame `queue_drops`; `restart_rx()` reopens RX capture on every TX→RX transition (Windows-only full-duplex wedge workaround); on TX/RX stream-open failure re-initializes PortAudio once and retries with a name-resolved index (USB re-enumeration on radio power cycles invalidates cached device IDs — macOS -9999) |
| `audio_resample.py` | 44.1kHz ↔ 48kHz frame-aligned SRC (numpy linear interp; 882↔960 = 20ms) |
| `opus_rx.py` | libopus ctypes wrapper: `RxOpusEncoder` (48kHz), `TxOpusDecoder` (48kHz) |
| `scope_handler.py` | Spectrum data container: FT4222 real FFT + S-meter Gaussian fallback |
| `scope_pipe.py` | Compatibility shim (still the PyInstaller entry) — real module moved to `backends/ft710/scope_pipe.py`: standalone subprocess for FT4222 SPI I/O (avoids asyncio/ctypes conflicts); 1s len=0 stdout heartbeat + stdin-EOF for dead-parent detection; stdin `TX:1`/`TX:0` control — SPI reads pause while TX (radio garbles scope stream); resync = device close/settle/reopen (byte-by-byte resync impossible: per-byte SingleRead = separate SPI transaction); server kills it via process tree (`taskkill /T`) on Windows; launched unfrozen as `python -m backends.ft710.scope_pipe` with cwd=repo root |
| `ssl_bootstrap.py` | First-run self-signed TLS cert generation (ECDSA P-256, 10y, SANs localhost/hostname/LAN IPs) so the desktop launcher starts HTTPS by default; `FT710_SSL_CERT/KEY` override, `FT710_SSL=off` escape |
| `scope_frame.py` | Compatibility shim — real module moved to `backends/ft710/scope_frame.py`: shared frame parsing, pipe payload encode/decode, quality metrics |
| `scope_libraries.py` | Compatibility shim — real module moved to `backends/ft710/scope_libraries.py`: FTDI library discovery and SPI clock configuration |
| `config.py` | Protocol-neutral constants (serial/web/SSL/auth/poll/reconnect/PTT) + shared UI mode tables and the `_interp` calibration helper; FT-710-specific tables (modes, bands, filter widths, meter calibrations, scope spans/port) moved to `backends/ft710/config_ft710.py` |
| `atr1000_client.py` | Optional asyncio WS client for networked ATR1000 tuner: binary frame protocol, 5s reconnect, 55-min refresh, TX-no-SYNC watchdog, learning, throttled relay writes, `notify_freq`/`notify_tx` sync hooks |
| `atr1000_tuner.py` | `TunerStorage` LC-learning JSON store (learn gate SWR 1.0–1.8, 1kHz keys ±5kHz nearest, atomic writes) |

Pluggable radio backends live in `backends/` (selected via `MRRC_RADIO_MODEL`, default `ft710`):

| Module | Responsibility |
|--------|----------------|
| `backends/__init__.py` | `create_backend(model, ...)` lazy factory — registered keys `"ft710"`, `"ic7300"`, `"ic7300mk2"` |
| `backends/base.py` | `RadioBackend` ABC (CAT surface mirroring `CatController`), `RadioCapabilities` dataclass (`to_dict()` for JSON), `ScopeProducer` protocol, defaulted hooks: `bands`/`ui_modes`/`mode_name_to_num`/`filter_tables()`/`state_tables()`/poll-item lists/`init_scope()`/`create_scope_producer()` |
| `backends/ft710/` | FT-710 backend: `backend.py` (`FT710Backend` thin delegate + `init_scope()` EX040101/EX040200 + UI tables), `cat_controller.py`, `scope_pipe.py`, `scope_producer.py` (ScopeProducer: owns the scope_pipe subprocess — spawn/read/auto-restart/TX-notify, moved from `server.py` in Phase 1), `scope_frame.py`, `scope_libraries.py`, `config_ft710.py` (FT-710-only tables) |
| `backends/ic7300/` | IC-7300/MK2 backend: `backend.py` (`IC7300Backend`/`IC7300MK2Backend`), `civ_codec.py` (pure CI-V framing/BCD/scope-segment codec), `civ_controller.py` (async CI-V demux: reader thread → frame parser → echo drop / 0x27 scope queue / transceive broadcast / pending-response matching; 3-tier priority; reconnect), `civ_scope.py` (`CivScopeProducer`: CI-V 0x27 475 bins → scale 160→255 → upsample 850 → `ScopeHandler`), `config_ic7300.py` (Icom-only tables; USB CI-V 115200 8N1, addr 0x94 via `IC7300_CIV_ADDR`) |

Frontend assets in `static/`:
- `index.html` — SPA shell (mobile-first responsive layout)
- `ft710.css` — Dark amber theme, iPhone safe-area support
- `ft710_main.js` — WebSocket client (4 channels), state management, audio RX/TX, spectrum
- `ft710_ui.js` — All UI rendering: waterfall, S-meter, meters, controls, PTT
- `rx_worklet_processor.js` — AudioWorklet: time-based jitter buffer RX playback
- `tx_capture_worklet.js` — AudioWorklet: mic capture (48kHz)
- `tx_opus_worker.js` — Web Worker: Opus encode from mic samples (48kHz, 64kbps CBR)
- `modules/opus_codec.js` + `opus_wasm.js` — Browser-side WASM Opus codec
- `modules/ptt_manager.js` — PTT state machine + safety watchdog
- `modules/settings_manager.js` — Cookie persistence for all preferences (settings, AF volume, scope options, memory channels)
- `modules/atr1000.js` — ATR1000 tuner WS client + meter row + ATR TUNE button (inert unless `atr1000Enabled`)

iOS app in `FT710Mobile/` (SwiftUI, iOS 17, real device only — bundled `libopus.a` is arm64-device-only so simulator builds fail to link). See `FT710Mobile/CLAUDE.md` for build/test commands and protocol facts, `FT710Mobile/docs/ARCHITECTURE.md` for layer design, and `docs/IOS_APP_ANALYSIS.md` for the 2026-07-20 audit with the P0–P2 known-issue list.

Android app in `FT710Android/` (Kotlin + Jetpack Compose, minSdk 26, NDK/libopus). Pure logic is JVM-testable; CI gate `./gradlew test assembleDebug lintDebug`. Protocol facts and PTT safety rules in `FT710Android/CLAUDE.md`, toolchain steps in `FT710Android/BUILD_GUIDE.md`, design in `docs/superpowers/specs/2026-08-16-ft710-android-app-design.md`.

SDD (Software Design Description) in `SDD/` — 15-chapter IBM TeamSD documentation.

## SDD-Guardian Skill & Context Harness

`.agents/skills/sdd-guardian/` turns the SDD into enforceable engineering guardrails for any agent working in this repo (auto-discovered as a project-level skill):

- `SKILL.md` — 6-phase lifecycle (brief → design → implement → test → verify → doc-sync/commit) plus the golden-rule constraint table.
- `harness/constraints.json` — machine-readable constraint registry distilled from SDD AD-001…AD-015, incident history (DN freq-drift, PR errata, SH format, 16kHz crackling, V1.7 stale-read race), and open issues I6/I7.
- `harness/index.json` — knowledge routing index: maps files/topics to SDD refs across all 15 chapters (ADs, NFR-001…065, UC-001…008, risks R1–R8, assumptions A1–A6, issues I1–I7, success criteria SC1–SC9). Holds no content — refs are sliced live from `SDD/*.md`, so it never goes stale.
- `harness/sdd_context.py` — stdlib-only CLI: `prime` (session digest), `brief <paths>|--task` (full engineering brief: constraints + live-extracted SDD sections), `sdd <id|keyword>` (one item: AD-011, NFR-060, UC-005, R4, I6, 9.6…), `context` (fast constraints view), `check <paths>|--staged` (exit 2 on block violations), `hook` (PreToolUse mode).
- `references/` — full constraint catalog with rationale + phase checklists.

Before editing, run `python3 .agents/skills/sdd-guardian/harness/sdd_context.py brief <files>`; before committing, `... check --staged` must be clean. To make enforcement automatic (session-start context injection + pre-edit blocking), install the hooks once: `python3 .agents/skills/sdd-guardian/harness/install_hooks.py` (appends `[[hooks]]` to `~/.kimi-code/config.toml`, idempotent, backs up first). Behavior changes still owe the doc-sync described in SKILL.md Phase 5 (SDD chapters + version history + this file + README + tests/README).

## Build, Test, and Development Commands

Install dependencies:
```bash
pip install -r requirements.txt
```

Run the server:
```bash
# FT-710
MRRC_RADIO_MODEL=ft710 FT710_SERIAL_PORT=/dev/cu.usbserial-0121DB3A0 python server.py

# IC-7300
MRRC_RADIO_MODEL=ic7300 FT710_SERIAL_PORT=/dev/cu.usbserial-A1234567 python server.py
```

Run tests:
```bash
python -m unittest discover -s tests -v
```

Environment variables: `MRRC_RADIO_MODEL` (backend key, default `ft710`), `IC7300_CIV_ADDR` (IC-7300 CI-V address, default `0x94`), `FT710_SERIAL_PORT`, `FT710_BAUD_RATE`, `FT710_WEB_PORT`, `FT710_WEB_PASSWORD`, `FT710_WEB_HOST`, `FT710_FTDI_LIB_DIR`, `FT710_FT4222_CLK_DIV`, `FT710_SCOPE_PORT`, `FT710_SCOPE_BAUD`, `FT710_ATR1000_HOST`, `FT710_ATR1000_PORT`.

## Coding Style & Naming Conventions

Python: 4-space indentation, type hints for shared state, `UPPER_CASE` for module constants, `PascalCase` for classes, `snake_case` for functions/variables. JavaScript: `camelCase` names; UI rendering in `ft710_ui.js` or `static/modules/`; avoid mixing logic into `index.html`.

## Testing Guidelines

Run the full suite with `python -m unittest discover -s tests -v` (currently 593 tests). At minimum: `python -m py_compile *.py`. Hardware-dependent changes should document: connected radio model, serial port, FT4222 availability (FT-710), audio device. Name tests `test_*.py`. Keep hardware-independent logic testable without a radio.

## Commit & Pull Request Guidelines

Short imperative summaries. Pull requests should describe user-visible behavior, list verification steps, call out hardware requirements, and include screenshots/recordings for UI changes.

## Security & Configuration Tips

Never commit passwords, serial-device paths, or local driver assumptions. Use environment variables for deployment-specific values. All WebSocket endpoints require auth token (`?token=` query param). Auth tokens cleared on server restart.
