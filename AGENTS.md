# Repository Guidelines

## Project Structure & Module Organization

This repository contains a Python FastAPI server for Yaesu FT-710 web control plus a static browser UI. Core backend modules live at the repository root:

| Module | Responsibility |
|--------|----------------|
| `server.py` | FastAPI app, auth, 4 WebSocket endpoints, REST APIs, lifespan management |
| `cat_controller.py` | Serial CAT protocol (pyserial + asyncio.to_thread), 40+ command helpers |
| `radio_state.py` | `RadioState` dataclass with dirty-field change tracking and derived properties |
| `poll_scheduler.py` | 7-task adaptive background polling (100ms→5s) with skip-on-command and post-query stale-read discard; watchdog re-runs scope init (`on_reconnected`) after serial reconnect |
| `audio_handler.py` | PyAudio sound card capture/playback (44.1kHz native device rate), Opus encode, FT-710 device auto-detection (name + mono-channel heuristic) |
| `audio_resample.py` | 44.1kHz ↔ 48kHz frame-aligned SRC (numpy linear interp; 882↔960 = 20ms) |
| `opus_rx.py` | libopus ctypes wrapper: `RxOpusEncoder` (48kHz), `TxOpusDecoder` (48kHz) |
| `scope_handler.py` | Spectrum data container: FT4222 real FFT + S-meter Gaussian fallback |
| `scope_pipe.py` | Standalone subprocess for FT4222 SPI I/O (avoids asyncio/ctypes conflicts); 1s len=0 stdout heartbeat for dead-parent EPIPE detection |
| `scope_frame.py` | Shared frame parsing, pipe payload encode/decode, quality metrics |
| `scope_libraries.py` | FTDI library discovery and SPI clock configuration |
| `config.py` | Mode tables, bands, filter widths, S-meter calibration, all constants |
| `atr1000_client.py` | Optional asyncio WS client for networked ATR1000 tuner: binary frame protocol, 5s reconnect, 55-min refresh, TX-no-SYNC watchdog, learning, throttled relay writes, `notify_freq`/`notify_tx` sync hooks |
| `atr1000_tuner.py` | `TunerStorage` LC-learning JSON store (learn gate SWR 1.0–1.8, 1kHz keys ±5kHz nearest, atomic writes) |

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
FT710_SERIAL_PORT=/dev/cu.usbserial-0121DB3A0 python server.py
```

Run tests:
```bash
python -m unittest discover -s tests -v
```

Environment variables: `FT710_SERIAL_PORT`, `FT710_BAUD_RATE`, `FT710_WEB_PORT`, `FT710_WEB_PASSWORD`, `FT710_WEB_HOST`, `FT710_FTDI_LIB_DIR`, `FT710_FT4222_CLK_DIV`, `FT710_SCOPE_PORT`, `FT710_SCOPE_BAUD`, `FT710_ATR1000_HOST`, `FT710_ATR1000_PORT`.

## Coding Style & Naming Conventions

Python: 4-space indentation, type hints for shared state, `UPPER_CASE` for module constants, `PascalCase` for classes, `snake_case` for functions/variables. JavaScript: `camelCase` names; UI rendering in `ft710_ui.js` or `static/modules/`; avoid mixing logic into `index.html`.

## Testing Guidelines

No configured pytest suite yet. At minimum: `python -m py_compile *.py`. Hardware-dependent changes should document: connected FT-710, serial port, FT4222 availability, audio device. Name tests `test_*.py`. Keep hardware-independent logic testable without a radio.

## Commit & Pull Request Guidelines

Short imperative summaries. Pull requests should describe user-visible behavior, list verification steps, call out hardware requirements, and include screenshots/recordings for UI changes.

## Security & Configuration Tips

Never commit passwords, serial-device paths, or local driver assumptions. Use environment variables for deployment-specific values. All WebSocket endpoints require auth token (`?token=` query param). Auth tokens cleared on server restart.
