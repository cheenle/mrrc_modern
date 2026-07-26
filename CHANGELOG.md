# Changelog

All notable changes to the FT-710 Web Control project.

## [v1.6.3] — 2026-07-25 — Windows Installer Robustness Fixes

Deep audit of the Windows packaging chain (`windows/`, `packaging/`); all
fixes verified hardware-free, suite now 271 tests.

### Fixes
- **Packaged web UI 404 (P0)**: PyInstaller 6 onedir puts datas under
  `_internal/`, but `STATIC_DIR` pointed next to the exe — the installed app
  served only the inline fallback login page and "Static files not found".
  `server.py` now resolves bundled resources via `_resource_dir()`
  (`sys._MEIPASS`-aware); the launcher's starter-channel seeding also checks
  `_internal/mem_channels.json`.
- **scope_pipe orphan holding FT4222 (P1)**: on Windows, terminating the
  onefile bootloader never reached the real child process, which kept the
  FT4222 device open forever. `scope_pipe.py` now emits the documented len=0
  stdout heartbeat every 1 s — a dead parent closes the pipe, the next write
  raises EPIPE, and the pipe exits cleanly instead of orphaning.
- **Silent build failures (P1)**: `build.ps1` relied on
  `$ErrorActionPreference`, which does not cover native commands — failed
  tests or PyInstaller runs no longer slip through into an installer
  (`Invoke-Checked` wrapper checks `$LASTEXITCODE`).
- **Launcher self-spawn chain (P2)**: with `ft710-server.exe` missing (e.g.
  antivirus quarantine), the frozen launcher used to "fall back" to
  `[sys.executable, server.py]` — i.e. spawn another launcher, recursively.
  It now reports the missing exe and exits.
- **Version drift (P2)**: installer `AppVersion` 1.6.0 → 1.6.3; new
  `packaging/windows/requirements-build.txt` pins `pyinstaller==6.21.0`
  (PyInstaller 6 already changed the onedir layout once).
- Installer no longer ships `windows/__pycache__`; stale cache-bust test
  assertions updated (v17 → v18).

## [v1.6.2] — 2026-07-21 — Spectrum Freeze After USB Reconnect

### Fixes
- **Spectrum survives serial hiccups**: the connection watchdog's reconnect
  path now re-runs the scope-init CAT sequence (`EX040101`/`EX040200`) via a
  new `PollScheduler(on_reconnected=...)` hook wired to `_init_scope_cat`.
  Previously a USB re-enumeration reset the radio's scope output, CAT
  reconnected fine, but no FFT frames ever resumed — the waterfall sat frozen
  on stale data (and, with `serial_connected=true`, without even the
  "radio disconnected" hint). Hook failures are logged and non-fatal.

## [v1.6.1] — 2026-07-21 — Web Frontend Safety & UX Overhaul

### Safety
- **PTT watchdog actually armed**: PTT/TUNE buttons and Space-bar PTT now
  route through `PTTManager` (previously bypassed — watchdog, pagehide
  force-RX and unload beacon were dead code); broken `sendBeacon` removed
- **TUNE is press-and-hold** (was latch-on-click — accidental TX path)
- **Keyboard guards**: ignore `e.repeat` and events from inputs

### Fixes
- Silent `renderFreqScale` crash (missing `range` arg) that skipped
  VFO/PTT renders every update cycle
- Server `error` messages now show a toast banner
- 🔊 Vol slider is browser-local volume (localStorage), no longer fought
  by the CAT `af_gain` poll
- S-meter label: relative `dB` (S9=0) instead of misleading `dBm`

### Features
- Waterfall/FFT click-to-QSY (8 px drag threshold)
- Desktop layout ≥768 px (720 px container, 120 px waterfall, larger controls)
- Filter tables server-authoritative (`fullState.filterTables`)
- `lame.js` lazy-loaded on first REC click; asset cache bust v17

## [v1.6.0] — 2026-07-21 — Windows Desktop Installer

### Windows Package
- **Desktop installer**: `MRRC-FT710-Setup.exe` (28.3 MB, x64) built with
  PyInstaller 6.21 + Inno Setup 6.7.3 — embedded Python 3.12 runtime, no
  manual Python install required
- **Launcher app** (`MRRC-FT710.exe`): seeds `%LOCALAPPDATA%\MRRC-FT710\ft710.env`,
  starts the server, waits for `/api/health`, opens the browser;
  CTRL_BREAK-based graceful stop
- **Frozen scope worker**: `scope_pipe.exe` bundled for FT4222 true
  spectrum (requires `vendor\ftdi\windows\bin\x64` FTDI DLLs, otherwise
  S-meter fallback)
- Download: <https://www.vlsc.net/mrrc_ft710/downloads/MRRC-FT710-Setup.exe>
  or GitHub Releases

### Build Fixes
- **PyInstaller specs**: `ROOT = Path(SPECPATH).parents[1]` — `SPECPATH`
  is the spec directory in PyInstaller 6, `parents[2]` escaped the repo
  root and broke the build entirely
- **Frozen server start**: `uvicorn.run(app)` with the app object instead
  of the `"server:app"` import string, which a frozen exe cannot import

### Verified
- End-to-end on a clean Windows 11 VM: silent install → launcher →
  server start → web login → `/api/health` + 4 WebSocket channels OK

## [v1.1.0] — 2026-07-14 — iOS App Enhancement

### iOS App Features
- **Complete Opus Codec Implementation**: Full libopus integration via C bridge
- **Unified Audio Session Management**: Consistent TX/RX audio handling
- **Error Handling UI**: User-friendly error alerts and recovery options
- **Performance Monitoring**: Real-time connection and audio quality metrics
- **Comprehensive Testing**: Unit tests for core components
- **Documentation**: Complete iOS development guides

### Technical Improvements
- Optimized spectrum rendering with performance monitoring
- Enhanced PTT button implementation (removed duplication)
- Added audio session route change handling
- Improved error propagation and user feedback
- Pre-allocated buffers for memory efficiency

### Security & Stability
- Robust error handling for audio operations
- Graceful degradation on connection failures
- Thread-safe audio processing
- Memory leak prevention through proper cleanup

## [v2.0.0] — 2026-07-14 — Stability & Security Hardening

### Security
- **Login rate limiting**: Max 5 attempts per 5 minutes per IP (`_check_login_rate_limit`)
- **Strong default password**: Changed from `ft710` to `changeme_please_use_strong_password!`
- **Password strength warnings**: Client-side feedback for weak passwords
- **Health check endpoint**: `/api/health` returns uptime, radio connection status, and degraded state
- **Startup time tracking**: Monitored via health endpoint

### Critical Fixes
- **Race condition fix**: `_cancel_polls` changed from `bool` to `asyncio.Event` in `cat_controller.py` and `poll_scheduler.py` — eliminates TOCTOU race between priority commands (PTT/Tune) and background pollers
- **Python 3.10+ compatibility**: Added `from __future__ import annotations` to `config.py` and `server.py`; removed `asyncio.Lock` from `radio_state.py` dataclass field (was causing RuntimeError on Python 3.9)
- **Removed duplicate `rf_gain` handler** in `server.py`

### Performance Optimizations
- **Initial sync speed**: `initial_state_sync` sleep reduced from 50ms to 20ms (60% faster connection)
- **Log noise reduction**: IF poll debug threshold raised from 50→1000 consecutive errors; TX meter logging throttled to first 5 seconds
- **Class-level state cleanup**: `_tx_meter_first_logged` moved from class-level to instance-level in `poll_scheduler.py`
- **Module-level `import time`**: Added missing import in `poll_scheduler.py`

### Code Quality
- **Debug cleanup**: Removed verbose `_dbg_*` flags and associated logging from `audio_handler.py`
- **Docstring fix**: Corrected misleading sample rate description in `opus_rx.py` (16kHz → 48kHz)
- **Test compatibility**: Updated `FakeCat` mock in `tests/test_poll_scheduler.py` to use `asyncio.Event()` for `_cancel_polls`

### Testing
- **206/206 tests passing** (was 35 failing before fixes)
- Full pytest and unittest coverage maintained

### Documentation
- Created `SECURITY_GUIDE.md` — complete security configuration guide
- Created `QUICKSTART.md` — step-by-step setup guide
- Created `FIXES_SUMMARY.md` — detailed fix documentation
- Created `FINAL_VERIFICATION.md` — verification report
- Created `EXECUTIVE_SUMMARY.md` — executive summary (Chinese)
- Created `COMPLETION_REPORT.md` — completion report (Chinese)
- Updated `DEPENDENCIES.md` — Python version requirement clarified
- Updated `README.md` — reflects current state
- Created `docs/TX_LINK_ANALYSIS.md` — TX audio chain deep analysis report

---

## [v2.1.0] — 2026-07-14 — TX Link Analysis Complete

### Analysis Completed
- **TX audio chain deep review**: Full stack analysis from browser → WebSocket → radio
- **Issues identified**: 2 high-risk, 3 medium-risk, 3 low-risk
- **Key findings**:
  - PTT control path inconsistency (high risk)
  - TX meter polling condition error (high risk)
  - AudioWorklet SAB path not implemented (medium risk)
  - TX Opus availability not checked (medium risk)
  - Unused TxJitterBuffer (medium risk)

### Recommendations
- Fix PTT button to use PTTManager immediately
- Clean up AudioWorklet SAB code within 1 week
- Add TX Opus availability check within 1 week
- Upgrade frontend Opus library within 1 month
- Develop TX end-to-end tests within 1 month

**Status**: Analysis complete, fixes pending implementation

---

## [v2.2.0] — 2026-07-14 — iOS App Analysis Complete

### Analysis Completed
- **FT710Mobile iOS app deep review**: Full stack analysis from SwiftUI → WebSocket → radio
- **Issues identified**: 2 high-risk, 3 medium-risk, 3 low-risk
- **Key findings**:
  - Opus encoder/decoder not implemented (high risk)
  - Dual PTT button implementation (high risk)
  - Audio session configuration incomplete (medium risk)
  - Error handling insufficient (medium risk)
  - Memory management risks (medium risk)

### Recommendations
- Implement Opus codec support immediately
- Unify PTT button implementation
- Add comprehensive error handling
- Implement unit tests
- Consider internationalization

**Status**: Analysis complete, fixes pending implementation

---

## [v1.2.0] — Previous Release

### Features
- Bidirectional Opus audio (RX/TX) with jitter buffers
- Real-time FFT spectrum + waterfall (FT4222 SPI + S-meter fallback)
- PTT safety: dead-man switch, triple verify, forced RX on disconnect
- Graceful TX audio drain before RF drop
- Mobile-first responsive UI (iPhone/iOS Safari optimized)
- Multi-meter telemetry (PWR/ALC/SWR/Id/Vd)
- Memory channels with persistent storage
- 5-tier adaptive background polling
- Dirty-state broadcasting (only changed fields sent to clients)
- PWA support (manifest + service worker)

---

## [v1.0.0] — Initial Release

- Basic FT-710 web control server
- Serial CAT communication via pyserial
- WebSocket-based real-time state updates
- S-meter display
- Frequency/mode/band control
