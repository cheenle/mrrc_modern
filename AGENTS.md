# Repository Guidelines

## Project Structure & Module Organization

This repository contains a Python FastAPI server for remote radio control (Yaesu FT-710 and Icom IC-7300/IC-7300MK2 via pluggable backends) plus a static browser UI. Core backend modules live at the repository root:

| Module | Responsibility |
| -------- | ---------------- |
| `server.py` | FastAPI app, auth, 5 WebSocket endpoints (`/WSradio`, `/WSspectrum`, `/WSaudioRX`, `/WSaudioTX`, optional `/WSatr1000`), REST APIs, lifespan management; TX uplink ownership follows the PTT client and same-session replacement connections; spectrum loop schedules at 30 Hz and sends real scope data only when the frame counter advances |
| `cat_controller.py` | Compatibility shim — real module moved to `backends/ft710/cat_controller.py`: Serial CAT protocol (pyserial + asyncio.to_thread), 40+ command helpers |
| `radio_state.py` | `RadioState` dataclass with dirty-field change tracking and derived properties |
| `poll_scheduler.py` | 7-task adaptive background polling (100ms→5s) with skip-on-command and post-query stale-read discard; watchdog re-runs scope init (`on_reconnected`) after serial reconnect |
| `audio_handler.py` | PyAudio sound card capture/playback with per-backend device rate and name hints (FT-710: fixed 44.1kHz with 960→882 resample before TX; IC-7300: 48kHz native, no resample), Opus encode, radio USB-audio auto-detection (FT-710/YAESU name, "USB Audio CODEC"/"USB Audio Device" Windows names, mono/full-duplex heuristics); startup/open logs include host API plus default/actual rates and channels; TX session stats include oldest-frame `queue_drops`; `restart_rx()` reopens RX capture on every TX→RX transition (full-duplex codec recovery); on TX/RX stream-open failure re-initializes PortAudio once and retries with a name-resolved index (USB re-enumeration on radio power cycles invalidates cached device IDs — macOS -9999) |
| `audio_resample.py` | 44.1kHz ↔ 48kHz frame-aligned SRC (numpy linear interp; 882↔960 = 20ms) |
| `opus_rx.py` | libopus ctypes wrapper: `RxOpusEncoder` (48kHz), `TxOpusDecoder` (48kHz) |
| `recorder.py` | Server-side QSO recording (AD-017): `RecordingSession` = 16 kHz mono timeline (monotonic timestamps, 50 ms jitter tolerance, silence gap fill, incremental lameenc MP3 with per-block `flush()`), `_StreamingDecimator` (96-tap FIR 48k→16k), mrrc-compatible naming `<freq>kHz_<date>_<time>.mp3` and `recordings.json` index helpers. **16 kHz is a storage domain, not the codec/device domain** — 44.1k→48k goes through `audio_resample`, nothing here re-enters the audio path |
| `_diag_civ.py` | Generic multi-model CI-V self-check (spec 2026-09-12 §7): model ID → read-only probes → scope measurement (bin count / amplitude ceiling / segment count — the three facts that cannot be settled offline) → static meters incl. raw Vd/Id → optional 200 ms TX check with PTT read-back (`--tx-check --allow-tx`) → `finally` restores scope data output and releases PTT → paste-ready Markdown report naming `civ_profiles.py` as the correction target |
| `scope_handler.py` | Spectrum data container: FT4222 real FFT + S-meter Gaussian fallback. Note: the in-process `connect`/`read_loop`/`_resync` SPI code is legacy — production reads go through the `scope_pipe` subprocess (byte-by-byte resync was proven impossible on FT4222; see SDD V2.8), kept only as historical reference |
| `scope_pipe.py` | Compatibility shim (still the PyInstaller entry) — real module moved to `backends/ft710/scope_pipe.py`: standalone subprocess for FT4222 SPI I/O (avoids asyncio/ctypes conflicts); 1s len=0 stdout heartbeat + stdin-EOF for dead-parent detection; stdin `TX:1`/`TX:0` control — SPI reads pause while TX (radio garbles scope stream); resync = device close/settle/reopen (byte-by-byte resync impossible: per-byte SingleRead = separate SPI transaction); server kills it via process tree (`taskkill /T`) on Windows; launched unfrozen as `python -m backends.ft710.scope_pipe` with cwd=repo root |
| `ssl_bootstrap.py` | First-run self-signed TLS cert generation (ECDSA P-256, 10y, SANs localhost/hostname/LAN IPs) so the desktop launcher starts HTTPS by default; `MRRC_SSL_CERT/KEY` override (legacy `FT710_SSL_CERT/KEY` honored), `MRRC_SSL=off` escape |
| `scope_frame.py` | Compatibility shim — real module moved to `backends/ft710/scope_frame.py`: shared frame parsing, pipe payload encode/decode, quality metrics |
| `scope_libraries.py` | Compatibility shim — real module moved to `backends/ft710/scope_libraries.py`: FTDI library discovery and SPI clock configuration |
| `config.py` | Protocol-neutral constants (serial/web/SSL/auth/poll/reconnect/PTT) + backend-aware serial defaults (FT-710 38400; IC-7300/MK2 115200) + shared UI mode tables and the `_interp` calibration helper; FT-710-specific tables moved to `backends/ft710/config_ft710.py` |
| `_diag_ic7300_scope.py` | One-shot IC-7300/MK2 CI-V field diagnostic using the production checksum-free codec/parser; probes frequency/PTT, enables both scope display (`27 10`) and data output (`27 11`), and disables data output on exit |
| `atr1000_client.py` | Optional asyncio WS client for networked ATR1000 tuner: binary frame protocol, 5s reconnect, 55-min refresh, TX-no-SYNC watchdog, learning, throttled relay writes, `notify_freq`/`notify_tx` sync hooks |
| `atr1000_tuner.py` | `TunerStorage` LC-learning JSON store (learn gate SWR 1.0–1.8, 1kHz keys ±5kHz nearest, atomic writes) |

Pluggable radio backends live in `backends/` (selected via `MRRC_RADIO_MODEL`, default `ft710`):

| Module | Responsibility |
| -------- | ---------------- |
| `backends/__init__.py` | `create_backend(model, ...)` lazy factory + `known_models()` — registered keys `"ft710"`, `"ic7300"`, `"ic7300mk2"`, `"ic705"`, `"ic7610"`, `"ic7760"`; the server's model whitelist and `config._DEFAULT_BAUD_BY_MODEL` are registry-derived |
| `backends/base.py` | `RadioBackend` ABC (CAT surface mirroring `CatController`), `RadioCapabilities` dataclass (`to_dict()` for JSON), `ScopeProducer` protocol, defaulted hooks: `bands`/`ui_modes`/`mode_name_to_num`/`filter_tables()`/`state_tables()`/poll-item lists/`init_scope()`/`create_scope_producer()` |
| `backends/ft710/` | FT-710 backend: `backend.py` (`FT710Backend` thin delegate + `init_scope()` EX040101/EX040200 + UI tables), `cat_controller.py`, `scope_pipe.py`, `scope_producer.py` (ScopeProducer: owns the scope_pipe subprocess — spawn/read/auto-restart/TX-notify, moved from `server.py` in Phase 1), `scope_frame.py`, `scope_libraries.py`, `config_ft710.py` (FT-710-only tables) |
| `backends/ic7300/` | **Shared Icom CI-V core (directory name is historical)** + profile-driven backends: `civ_profiles.py` (`CivModelProfile`/`MeterCal` per model — address, Transceive set-mode item, scope geometry 475/11 or 689/15, bands, modes, attenuator steps, meter curves, `verified`/`unverified_meters`, provenance; `get_profile()`/`known_models()`), `backend.py` (`IC7300Backend` derives every table/capability from `_profile`; `IC7300MK2Backend`/`IC705Backend`/`IC7610Backend`/`IC7760Backend` override it only; unverified-model **TX gate** refuses keying unless `MRRC_ALLOW_UNVERIFIED_TX=1`, never a release; read-only `19 00` identity check logs observed bytes and only warns when a profile records an expectation), `civ_codec.py` (pure checksum-free CI-V framing/BCD/scope-segment codec incl. `encode/decode_bcd_byte` for the attenuator; `ScopeAssembler` warns once and adopts the radio's actual geometry when it disagrees with the profile), `civ_controller.py` (async CI-V demux: reader thread → frame parser → echo drop / profile-sized newest-data scope queue / transceive broadcast / pending-response matching; 3-tier priority; reconnect; documented power-on preamble; `get_model_id()`; attenuator written as packed BCD from the profile steps), `civ_scope.py` (`CivScopeProducer`: CI-V 0x27 waveform → profile amp ceiling → 255 → upsample 850 → `ScopeHandler`), `config_ic7300.py` (Icom-only tables; USB CI-V 115200 8N1, IC-7300 addr 0x94 via `IC7300_CIV_ADDR`, MK2 addr 0xB6 via `IC7300MK2_CIV_ADDR`, ALC raw 120 full scale) |

Frontend assets in `static/`:

- `index.html` — SPA shell (mobile-first responsive layout)
- `ft710.css` — Dark amber theme, iPhone safe-area support
- `ft710_main.js` — WebSocket client (4+1 channels: control/audio RX/TX/spectrum + optional ATR1000), state management, audio RX/TX, spectrum
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
- `harness/constraints.json` — machine-readable constraint registry distilled from SDD AD-001…AD-016, incident history (DN freq-drift, PR errata, SH format, 16kHz crackling, V1.7 stale-read race), and open issues I6–I11 (multi-client arbitration, mem POST validation, static path traversal, password compare, audio-subchannel reconnect, iOS PTT race).
- `harness/index.json` — knowledge routing index: maps files/topics to SDD refs across all 15 chapters (ADs, NFR-001…065, UC-001…008, risks R1–R8, assumptions A1–A6, issues I1–I11, success criteria SC1–SC9). Holds no content — refs are sliced live from `SDD/*.md`, so it never goes stale.
- `harness/sdd_context.py` — stdlib-only CLI: `prime` (session digest), `brief <paths>|--task` (full engineering brief: constraints + live-extracted SDD sections), `sdd <id|keyword>` (one item: AD-011, NFR-060, UC-005, R4, I6, 9.6…), `context` (fast constraints view), `check <paths>|--staged` (exit 2 on block violations), `hook` (PreToolUse mode).
- `references/` — full constraint catalog with rationale + phase checklists.

Before editing, run `python3 .agents/skills/sdd-guardian/harness/sdd_context.py brief <files>`; before committing, `... check --staged` must be clean. To make enforcement automatic (session-start context injection + pre-edit blocking), install the hooks once: `python3 .agents/skills/sdd-guardian/harness/install_hooks.py` (appends `[[hooks]]` to `~/.kimi-code/config.toml`, idempotent, backs up first). Behavior changes still owe the doc-sync described in SKILL.md Phase 5 (SDD chapters + version history + this file + README + tests/README).

## Release & Packaging Skills

Installer/release process is captured as project skills in `.agents/skills/` (each gotcha traces to a real broken build):

- `dual-platform-release/` — release-day orchestrator: version bump (CHANGELOG top entry is the single source of truth + `.iss`), build order, docs/SDD/website sync checklist, <www.vlsc.net> deploy, URL/SHA verification, commit/tag/**explicit tag push** (`--follow-tags` skips lightweight tags). Start here for 发布.
- `macos-installer/` — local DMG build (`packaging/macos/build.sh`): interpreter selection (`PYTHON=$(pwd)/.venv/bin/python`; never `source .venv/bin/activate` — copied venv points at mrrc_ft710), `Contents/Frameworks` symlink, dual-stack `::`, HTTPS-default wiring, classic DMG layout, headless + GUI verification.
- `windows-installer/` — Win11 KVM VM build via `ham.vlsc.net` jump: source-zip exclusion rules (`.agents/` in, `certs/`/`promo/` out), venv-preserving extract, per-version build script (never the hijacked `build_vm.ps1`), PowerShell 5.1 quirks, KVM USB limits (TX audio unverifiable on VM).

Operator manuals with full troubleshooting: `mac_pack.md` / `win_pack.md`.

## Tooling Guards (do not remove)

Two legacy frontend files — `static/ft710_main.js` and `static/ft710_ui.js` — must not be
reformatted or "autofixed" by tooling:

- They are tab-indented with double quotes, while `biome.json` configures 4-space/single-quote
  for the rest of the project. Letting biome format them rewrites ~3200 lines for no behavior
  change (and breaks the whitespace-sensitive frontend contract tests in
  `tests/test_ws_protocol.py`).
- Their top-level names (`radioCapabilities`, `AUDIO_TAG_PCM`, `RX_RECORDER_MIME`, …) are
  **cross-file globals** (classic `<script>` files, read from the other file). A single-file
  "unused variable" analysis calls them unused and renames them with a leading underscore —
  which silently breaks the capability-driven UI in the browser, invisible to the Python suite.

Guards in place (both verified against biome 2.5.6, the binary pi-lens runs):

| File | Guard |
| --- | --- |
| `biome.json` | `overrides` for both files: `formatter.enabled=false` plus the six rules whose autofixes mutate them (`noUnusedVariables`, `noUnusedFunctionParameters`, `noInnerDeclarations`, `useParseIntRadix`, `useTemplate`, `useOptionalChain`, `noGlobalIsNan`) turned off — verified: even a direct `biome check --write --unsafe` leaves both files byte-identical |
| `.pi-lens.json` | `ignore` for both files, so no diagnostic/autofix dispatch touches them |

Important: `biome.json` must stay **strict JSON** — a comment makes this biome fall back to its
built-in defaults (2-space/double-quote) and reformat every file it touches. Keep the rationale
here instead. If those two files ever come back dirty with tab→space or `catch (e)` → `catch (_e)`
churn, `git checkout -- static/ft710_main.js static/ft710_ui.js`.

## Build, Test, and Development Commands

Install dependencies:

```bash
pip install -r requirements.txt
```

Run the server:

```bash
# FT-710
MRRC_RADIO_MODEL=ft710 MRRC_SERIAL_PORT=/dev/cu.usbserial-0121DB3A0 python server.py

# IC-7300
MRRC_RADIO_MODEL=ic7300 MRRC_SERIAL_PORT=/dev/cu.usbserial-A1234567 MRRC_BAUD_RATE=115200 python server.py
```

Run tests:

```bash
python -m unittest discover -s tests -v
```

Environment variables: `MRRC_RADIO_MODEL` (backend key: `ft710`/`ic7300`/`ic7300mk2`/`ic705`/`ic7610`/`ic7760`, default `ft710`), `MRRC_RECORDINGS_DIR` (default `<runtime dir>/recordings`; launchers point it at the user data dir), `MRRC_RECORDINGS_BITRATE` (64), `MRRC_RECORDINGS_MAX_SESSION_MIN` (240, 0 = unlimited — stops a forgotten recording, never deletes), `MRRC_ALLOW_UNVERIFIED_TX` (opt-in transmit enable for the three hardware-unverified Icom models; default off), `IC7300_CIV_ADDR` (IC-7300 address, default `0x94`), `IC7300MK2_CIV_ADDR` (MK2 address, default `0xB6`), `MRRC_SERIAL_PORT`, `MRRC_BAUD_RATE` (backend default: 38400/115200), `MRRC_WEB_PORT`, `MRRC_WEB_PASSWORD`, `MRRC_WEB_HOST`, `MRRC_AUDIO_RX_DEVICE`, `MRRC_AUDIO_TX_DEVICE`, `MRRC_FTDI_LIB_DIR`, `MRRC_FT4222_CLK_DIV`, `MRRC_SCOPE_PORT`, `MRRC_SCOPE_BAUD`, `MRRC_ATR1000_HOST`, `MRRC_ATR1000_PORT`. All applicable variables also honor legacy `FT710_*` aliases through the config fallback (`MRRC_*` wins).

## Coding Style & Naming Conventions

Python: 4-space indentation, type hints for shared state, `UPPER_CASE` for module constants, `PascalCase` for classes, `snake_case` for functions/variables. JavaScript: `camelCase` names; UI rendering in `ft710_ui.js` or `static/modules/`; avoid mixing logic into `index.html`.

## Testing Guidelines

Run the full suite with `python -m unittest discover -s tests -v` (currently 891 tests across 45 modules). At minimum: `python -m py_compile *.py`. Hardware-dependent changes should document: connecte

## Commit & Pull Request Guidelines

Short imperative summaries. Pull requests should describe user-visible behavior, list verification steps, call out hardware requirements, and include screenshots/recordings for UI changes.

## Security & Configuration Tips

Never commit passwords, serial-device paths, or local driver assumptions. Use environment variables for deployment-specific values. All WebSocket endpoints require auth token (`?token=` query param). Auth tokens cleared on server restart.
