# Changelog

All notable changes to the FT-710 Web Control project.

## [v1.7.8] — 2026-07-28 — Windows TX Restored to 44.1 kHz Device Audio

### Fixed
- **Windows TX now keeps the FT-710 USB device domain at 44.1 kHz**
  (SDD V2.14). Browser capture and Opus remain correctly fixed at 48 kHz
  with 960 samples per 20 ms; every decoded frame is now unconditionally
  resampled to 882 samples before PyAudio opens/writes the radio at
  44.1 kHz. The same rule applies after PortAudio reinitialization.
- Removed the Windows policy that promoted a same-name WASAPI endpoint's
  advertised 48 kHz `defaultSampleRate` into the FT-710 device rate and
  bypassed SRC. That value is the Windows shared-mode mix rate, not proof
  of the radio's USB hardware clock. The earlier KVM pacing result remains
  useful incident evidence but is not a valid hardware-rate decision.
- Preserved the Windows TX→RX capture reopen workaround and the v1.7.7
  PortAudio terminate/reinitialize/device-name re-resolution recovery.

### Tests
- Replaced the obsolete WASAPI-selection contract with regressions for
  fixed 44.1 kHz stream opening, exact 960→882 conversion, 44.1 kHz byte
  budgets, and rate preservation after device re-enumeration. Suite stays
  at 435 tests. RF speech/noise acceptance remains pending the operator's
  evening FT-710 test; this build is not yet a public download.

### Follow-up — 2026-07-31 TX Audit
- Fixed the frontend intentional-disconnect flag (`const` → `let`), which
  could throw before closing the audio sockets and leave a stale TX owner.
- A replacement `/WSaudioTX` connection from the same authenticated session
  now takes ownership; a different session still cannot steal on connect.
- TX jitter-buffer oldest-frame drops are now counted and logged as
  `queue_drops` on PTT release, exposing Windows output pacing regressions.
- Added four regressions and made source-contract tests formatting-agnostic;
  suite is 439 tests. Physical Win11 + FT-710 RF acceptance remains pending.

## [v1.7.7] — 2026-07-28 — Audio Survives Radio Power Cycles (Power Switch Withdrawn)

### Fixed
- **TX/RX audio survives radio power cycles** (SDD V2.12): every
  power-off/on re-enumerates the FT-710's USB sound card, invalidating
  the CoreAudio device IDs cached inside PortAudio at `Pa_Initialize`
  time — every subsequent stream open failed with `-9999` until the
  server was restarted (field report: "TX audio device unavailable" after
  radio restarts). `audio_handler` now re-initializes PortAudio once and
  retries with a freshly resolved device index when TX/RX stream opens
  fail. Note: index-locked `FT710_AUDIO_RX/TX_DEVICE=<n>` configs are
  inherently fragile across re-enumeration — lock by **name**
  (e.g. `USB Audio Device`) instead.

### Added
- `PS;` is polled in the Tier-3 settings loop, so power changes made at
  the radio's front panel are reflected in `power_on` state.

### Withdrawn (after field reliability testing, SDD V2.13)
- **The header power switch (CAT `PS0;`/`PS1;`) was removed before
  release.** Two days of live testing proved the FT-710's CAT power
  control too fragile for a remote UI button: (1) a `PS0;` landing
  seconds after `PS1;` (mid-boot) wedged the radio's CAT MCU — serial/
  audio/scope USB all enumerated but CAT permanently deaf until a
  physical power cycle; (2) `PS1;` wake-up proved unreliable even with
  retry+verify (3 attempts over 36 s failed to wake a healthy radio).
  The `power` WS command remains for the maintenance scripts
  (`_power_cycle*.py`), hardened with the lessons learned: 15 s boot
  window rejecting `PS0` after `PS1`, `PS1` retry ≤3× with `FA;`
  read-back verification, `PS0` double-send, power-off refused while TX.

### Tests
- New `tests/test_power_switch.py` (9 tests: boot-window rejection,
  TX-while-off rejection, PS0 double-send, PS1 retry/verify/give-up,
  error reporting) and `PortAudioReinitTests` in `tests/test_audio.py`
  (3 tests: reinit-then-succeed for RX and TX, give-up path). Cache-bust
  css v20 / main v23 / ui v22 / sw `ft710-v23`. Suite 435 tests.

## [v1.7.6] — 2026-07-26 — HTTPS by Default on Windows (Self-Signed Bootstrap)

### Changed
- **The Windows app now starts on HTTPS by default** (SDD V2.10): the
  launcher no longer hardcodes `--no-ssl`. On first run it generates a
  throwaway self-signed certificate (ECDSA P-256, 10-year, SANs for
  localhost / hostname / LAN IPs) into `%LOCALAPPDATA%\MRRC-FT710\certs\`
  via the new `ssl_bootstrap.py`, and starts `ft710-server` with
  `--ssl-cert/--ssl-key`. HTTPS matters off-localhost: plain HTTP on a
  LAN address is not a browser secure context, which disables
  AudioWorklet and `getUserMedia` (RX/TX audio). The browser shows an
  "untrusted" warning once — accept it, or point
  `FT710_SSL_CERT`/`FT710_SSL_KEY` at a real certificate. Escape hatch:
  `FT710_SSL=off` restores the old HTTP behaviour. Launcher URL probe
  skips TLS verification for the self-signed bootstrap cert.

### Tests
- New `tests/test_ssl_bootstrap.py` (6 tests) and launcher SSL tests
  (6 tests); suite 421 tests. `cryptography>=41` is now a hard
  dependency (was commented out).

## [v1.7.5] — 2026-07-26 — Hotfix: NameError in start_tx (v1.7.4 Regression)

### Fixed
- **`name 'sys' is not defined` on PTT** (v1.7.4 regression): the
  WASAPI selection branch in `start_tx()` referenced `sys.platform`
  without importing `sys` at module level, so every PTT on the v1.7.4
  build failed with NameError. Added the import plus two end-to-end
  `start_tx` regression tests (`StartTxWindowsTests`: win32 opens the
  WASAPI 48 kHz entry, darwin stays at 44.1 kHz) — the previous tests
  only exercised `_wasapi_tx_variant` in isolation. Suite 409 tests.

## [v1.7.4] — 2026-07-26 — Windows TX Crackle Fix (WASAPI 48 kHz Output)

### Fixed
- **TX audio crackles into noise on Windows** (SDD V2.9): the C-Media
  codec's MME 44.1 kHz playback path paces ~1.4× slow (measured on the
  Win11 KVM rig: 50×20 ms frames block 1.36–1.42 s instead of 1.00 s),
  so the TX drain loop falls behind, the 400 ms queue cap drops 24–34 %
  of voice frames, and the transmitted audio is chopped into crackle.
  The codec's WASAPI entry at its native 48 kHz mix rate paces
  correctly (ratio 0.96). `start_tx()` now prefers the same-name WASAPI
  entry on Windows and opens the stream at that entry's native rate;
  `feed_tx_audio()` passes 48 kHz PCM through unchanged at 48 kHz
  (no 48→44.1 resample) and uses per-rate byte budgets for the
  pre-buffer/cap/graceful-drain. macOS behavior is unchanged (CoreAudio
  device domain stays at 44.1 kHz). AD-011 amended: the device-domain
  rate is host-API-dependent, not universally 44.1 kHz.

### Tests
- New `WindowsWasapiTxTests` (5 tests: WASAPI variant selection,
  other-device/WASAPI-absent guards, 48 k feed passthrough, 44.1 k feed
  resample); suite 407 tests.

## [v1.7.3] — 2026-07-26 — Windows RX Audio Dies After First PTT (Full-Duplex Wedge)

### Fixed
- **RX audio silent after the first PTT on Windows** (SDD V2.8): opening
  the TX playback stream on the FT-710's C-Media USB codec silently
  wedges the RX capture stream (MME/DirectSound full-duplex driver
  quirk — the stream stays open and error-free but delivers silence;
  macOS CoreAudio is unaffected). `AudioHandler.restart_rx()` now
  reopens the capture stream on every TX→RX transition (hooked in
  `_broadcast_state` on `tx_status`, covers PTT/TUNE/physical PTT),
  Windows-only, no-op elsewhere. Field symptom: audio perfect after
  server restart, gone after one PTT; hardware capture verified healthy
  (max 43% FS) with the server stopped.
- **Scope resync actually works now** (SDD V2.8): the byte-by-byte
  `sync_stream` resync could never succeed on the FT4222 — every 1-byte
  `SingleRead` is its own SPI transaction (CS toggles per call), so a
  contiguous multi-byte sync pattern is unobservable. It only consumed
  the stream and churned recovery into `fatal:too_many_reinits` after
  every PTT (even with the V2.7 TX pause, whose resume used it).
  Replaced with `resync_device()`: close → 1 s idle-bus settle → reopen
  — the pattern that reliably realigns (same as a pipe restart).

### Tests
- New `RestartRxTests` (4 tests: Windows stop→start order, non-Windows
  no-op, RX-not-running guard, failed-reopen path); suite 402 tests.

## [v1.7.2] — 2026-07-26 — TX-Safe Spectrum (Scope Pipe TX Pause)

### Fixed
- **Spectrum wrecked for 30–45 s after every PTT** (SDD V2.7): the
  FT-710 garbles its scope stream during TX, but `scope_pipe` kept
  reading it — sync_lost → stall reinit → more sync failures →
  `fatal:too_many_reinits`, then a full pipe restart before real FFT
  data returned. The pipe is now TX-aware: the server pushes `TX:1` /
  `TX:0` over the pipe's stdin on every `tx_status` transition
  (PTT/TUNE, any source); while TX is active the pipe pauses SPI reads
  and freezes all sync/stall recovery counters, and runs one clean
  re-sync when RX resumes. Post-PTT recovery: ~40 s → ~1 frame.
- **Zombie scope_pipe held the FT4222 on Windows**: killing the
  PyInstaller onefile bootloader (`proc.terminate()`) never reached the
  real worker, so the next pipe failed `FT_OpenEx` with
  FT_DEVICE_NOT_FOUND for ~10–15 s. The server now kills the pipe via
  `taskkill /PID <pid> /T /F` on Windows; the pipe additionally treats
  stdin EOF (parent died) as a shutdown signal.
- **Waterfall during TX**: now shows "TX 发射中 — 频谱暂停" instead of
  stale/garbled fallback rows (`ft710_ui.js`).
- **Windows package now ships libopus**: `vendor/opus/windows/bin/x64/opus.dll`
  (x64, from the PyOgg wheel) added to the repo and to `ft710_server.spec`
  datas. Previously the installer carried no libopus, killing server-side
  Opus (RX fell back to raw PCM, TX audio dead) and requiring a manual
  DLL drop into the install dir after every reinstall.

### Tests
- New `tests/test_scope_pipe_tx.py` (13 tests: control-line parsing,
  server TX-notify transitions/force/dead-pipe guard, Windows taskkill
  vs POSIX SIGTERM); suite 398 tests.

## [v1.7.1] — 2026-07-26 — Windows Audio Device Lock & Installer Diagnostics

### Fixed
- **Windows RX/TX audio broken after install** (SDD V2.6): the FT-710's
  built-in USB sound card enumerates on Windows under generic names —
  `USB Audio CODEC` or `USB Audio Device` depending on driver/OS build —
  with no "FT-710"/"YAESU" substring, so auto-detection fell through to
  the channel heuristics and grabbed the laptop microphone (RX) or PC
  speakers (TX). `audio_handler.py` adds a generic USB-audio name tier
  (`USB_AUDIO_NAME_HINTS`) to both RX and TX device selection, ranked
  below the FT-710-specific names and above the mono/full-duplex
  heuristics; multi-match (per-host-API duplicates) warns and points at
  the env-var lock.
- **CAT connect failure now logs the serial ports actually visible**
  (SDD V2.5), so a wrong default `COM3` is diagnosable from the console.
- **Launcher health probe**: `FT710_WEB_HOST=::` now maps to
  `http://localhost:<port>` instead of a false 15 s startup warning.
- **Frozen-app Opus**: `opus_rx.py` searches packaged `opus.dll`
  locations (`opus.dll`, `_internal\opus.dll`,
  `vendor\opus\windows\bin\x64\opus.dll`); `build.ps1` warns when
  `vendor\opus\windows` is absent; PyInstaller specs ship the
  platform-matched FTDI tree.

### Changed
- **`windows/default.env` pre-locks the audio devices**:
  `FT710_AUDIO_RX_DEVICE` / `FT710_AUDIO_TX_DEVICE` = `USB Audio` (the
  common substring of both Windows enumeration forms; name locking is
  reboot-stable, indices are not).
- **New "Audio (RX/TX) Setup" section** in the Windows installer guide:
  device identification from the startup PortAudio list, env locking,
  sound-panel guidance (never make the radio's USB audio the default
  playback device), and the radio-side modulation routing
  (`RADIO SETTING` → per-mode `MOD SOURCE` = `USB`) verified against the
  FT-710 Operation Manual; four new troubleshooting rows.

## [v1.7.0] — 2026-07-26 — ATR1000 Tuner Linkage & Frontend Settings in Cookies

### Added
- **Optional ATR1000 tuner linkage** (SDD V2.4; default disabled, enable
  with `FT710_ATR1000_HOST`/`FT710_ATR1000_PORT`): asyncio-native client
  for the networked ATR1000 (`atr1000_client.py`) with the LC-learning
  store ported from the mrrc project (`atr1000_tuner.py`). Three linkage
  behaviors: frequency change auto-applies learned relay LC; TX state sync
  (device push mode + stability-window learning); server-side tune assist
  (TX2 carrier → skip when SWR ≤ 1.6 → full tune → rollback when no
  improvement, carrier always dropped). New token-gated `/WSatr1000`
  channel; compact ATR meter row (power/SWR/relay/tuning) with an ATR TUNE
  button appears in the web UI only when the feature is enabled and
  connected. Radio-internal TUNE is unchanged. When disabled there is no
  client, no task, no network — zero impact on installs without the tuner.
- **Device-side mic gain (🎙 Vol)**: software GainNode on the browser mic
  capture graph (0–200 slider → 0–2×, 100 = unity), persisted in a cookie;
  independent of the radio's CAT mic gain.

### Changed
- **All frontend settings now persist in cookies** (SDD V2.3): unified the
  previous localStorage/sessionStorage mix behind `settings_manager.js`
  cookie helpers with a one-time legacy migration. Memory channels now
  survive browser restarts; the radio-side mic gain slider persists and is
  re-applied to the radio on every connect.
- Frozen Windows app: the ATR1000 learned-data store honors
  `FT710_ATR1000_STORE` (the launcher points it at the user data dir so
  learned data stays writable and survives reinstalls); the PyInstaller
  spec lists the new modules explicitly.

### Fixes (previously uncommitted, SDD V2.1/V2.2)
- TX uplink ownership promotion/claim + per-session TX observability.
- Windows packaging chain: frozen `_internal` resource resolution,
  scope_pipe stdout heartbeat, hardened `build.ps1`, launcher
  self-spawn guard.

Suite: 373 tests.

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
