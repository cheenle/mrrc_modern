# macOS Desktop Installer Guide

This guide covers the macOS desktop package for MRRC Web Control
(FT-710 and IC-7300/IC-7300MK2). The package targets **Apple Silicon (arm64)**
Macs on macOS 11 (Big Sur) or later. It installs a user-launched menu-bar app
with an embedded Python runtime; users do not need to install Python manually.

## Download (v1.7.0)

| File | Size | MD5 | SHA-256 |
|------|------|-----|---------|
| `MRRC-FT710-v1.7.0-arm64.dmg` | TBD | TBD | TBD |

- Fast mirror (recommended in CN): <https://www.vlsc.net/mrrc_ft710/downloads/MRRC-FT710-v1.7.0-arm64.dmg>
- GitHub Releases: <https://github.com/cheenle/mrrc_ft710/releases>

> Intel (x86_64) Macs are not supported by this build. The v1.7.0 package was
> built on Apple Silicon (Python 3.12, PyInstaller 6.21.0, rumps 0.4) with the
> full test suite green on the build machine. It ships the optional ATR1000
> tuner linkage, cookie-based frontend settings, and the device-side mic gain
> (see CHANGELOG). It is **ad-hoc signed** (no Apple Developer ID), so
> Gatekeeper will warn on first launch — see "Install" below.

## User Installation

### 1. Connect the radio

Plug the radio USB cable into the Mac.

- **FT-710**: macOS loads the Silicon Labs CP210x driver automatically
  (AppleUSBCDC) — no separate driver install is needed. You should see a
  `SLAB_USBtoUART` device, e.g. `/dev/cu.SLAB_USBtoUART`. The lower-numbered
  of the two CP210x ports is the Enhanced COM Port for CAT.
- **IC-7300 / IC-7300MK2**: macOS loads the standard USB-serial driver for the
  CI-V port. You should see a device such as `/dev/cu.usbserial-*`. No FTDI
  drivers are required.

USB audio devices also appear automatically for both radios.

Confirm the serial port:

```bash
ls /dev/cu.*
```

### 2. Install MRRC FT-710

Mount the disk image and drag the app to Applications:

```text
MRRC-FT710-v1.7.0-arm64.dmg
```

1. Double-click the `.dmg` to mount it.
2. Drag **MRRC FT-710** into the **Applications** folder.
3. Eject the disk image.

### 3. Bypass Gatekeeper on first launch (ad-hoc signed)

Because the app is ad-hoc signed (no Apple Developer ID), macOS blocks the
first launch. Do **one** of the following:

- In Finder, **right-click** `MRRC FT-710` → **Open** → confirm **Open** in
  the dialog. This one-time bypass sticks for subsequent launches.
- Or remove the quarantine attribute from the Terminal:

  ```bash
  xattr -dr com.apple.quarantine /Applications/MRRC-FT710.app
  ```

### 4. Edit configuration

The config file is created on first launch:

```text
~/Library/Application Support/MRRC-FT710/ft710.env
```

Edit it from the menu-bar item **Edit Configuration…** (opens TextEdit), or
open it directly. Typical configuration:

```ini
MRRC_RADIO_MODEL=ft710
FT710_SERIAL_PORT=/dev/cu.SLAB_USBtoUART
FT710_BAUD_RATE=38400
FT710_WEB_HOST=127.0.0.1
FT710_WEB_PORT=8888
FT710_WEB_PASSWORD=change_this_password
FT710_SCOPE_PORT=
FT710_SCOPE_BAUD=115200
FT710_AUDIO_RX_DEVICE=
FT710_AUDIO_TX_DEVICE=
FT710_FTDI_LIB_DIR=vendor/ftdi/macos
#IC7300_CIV_ADDR=0x94
#FT710_ATR1000_HOST=
#FT710_ATR1000_PORT=60001
```

Set `MRRC_RADIO_MODEL` to `ft710`, `ic7300`, or `ic7300mk2`. For IC-7300,
set `FT710_SERIAL_PORT` to the CI-V port from `ls /dev/cu.*` and leave
`FT710_FTDI_LIB_DIR` empty (FTDI libraries are not required). Change
`FT710_WEB_PASSWORD` before exposing the app beyond localhost.

### 5. Launch

Launch **MRRC FT-710** from Applications (or Spotlight). The menu-bar app:

1. Reads `~/Library/Application Support/MRRC-FT710/ft710.env`.
2. Starts the bundled server and waits for it to answer HTTP before opening
   `http://127.0.0.1:8888` in the default browser (up to ~15 seconds on the
   first run).
3. Shows a **menu-bar item** labeled `MRRC FT-710 :8888` with:
   - **Open Web UI** — reopen the browser at the control page.
   - **Edit Configuration…** — open `ft710.env` in TextEdit.
   - **Restart Server** — stop and re-spawn the server.
   - **Quit MRRC FT-710** — stop the server gracefully and exit.

The app is a background (menu-bar) app — it has no Dock icon and no main menu
bar. Use **Quit MRRC FT-710** from its menu-bar item for a clean stop (audio
drains and PTT releases first).

> **Warning:** Force-quitting the app (Activity Monitor → Force Quit, or
> `kill -9`) can leave the `ft710-server` child process running and may leave
> the radio keyed if it was transmitting. Always release PTT and use the
> menu-bar **Quit** item.

## FT4222 True Spectrum (FT-710 only)

The macOS package supports FT4222 true spectrum for the FT-710 when these
runtime libraries are present in the app bundle:

```text
MRRC-FT710.app/Contents/MacOS/vendor/ftdi/macos/libft4222.dylib
MRRC-FT710.app/Contents/MacOS/vendor/ftdi/macos/libftd2xx.dylib
```

If either is missing, the app still runs and uses the S-meter fallback
spectrum. The fallback is useful for basic activity visualization but does not
provide true FFT waterfall data. The v1.7.0 package ships **without** the
dylibs (S-meter fallback); drop them into the bundle path above (or into the
`FT710_FTDI_LIB_DIR` you configure) to enable true spectrum.

The macOS dylibs are part of FTDI's LibFT4222 macOS build and the D2XX
driver package — see the FTDI website for the current archives.

## Building the Package

Build on an Apple Silicon Mac.

### Prerequisites

- macOS 11+ on Apple Silicon
- Xcode Command Line Tools (`codesign`, `hdiutil`)
- Homebrew: `brew install portaudio`
- Python 3.12
- Project dependencies from `requirements.txt`
- PyInstaller 6.21.0 and `rumps`

Install build tools:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r packaging/macos/requirements-build.txt
```

Build:

```bash
packaging/macos/build.sh
```

Expected outputs:

```text
dist/macos/MRRC-FT710.app
dist/macos/MRRC-FT710-v1.7.0-arm64.dmg
```

The build script runs syntax checks and the test suite before packaging, and
prints MD5/SHA-256 of the `.dmg` for the website download table. See
[mac_pack.md](mac_pack.md) for the full build-side walkthrough.

## Build Components

| File | Purpose |
|------|---------|
| `macos/launcher.py` | Menu-bar launcher; starts/stops the server and opens the browser |
| `macos/default.env` | Initial user configuration template |
| `packaging/pyinstaller/ft710_server.spec` | Bundles the FastAPI server and static UI (platform-aware) |
| `packaging/pyinstaller/scope_pipe.spec` | Bundles the FT4222 scope worker (platform-aware) |
| `packaging/macos/ft710_launcher.spec` | Bundles the menu-bar launcher into a `.app` |
| `packaging/macos/build.sh` | End-to-end macOS build script |
| `packaging/macos/requirements-build.txt` | PyInstaller + rumps (build-only) |

## Verification Checklist

After installing on macOS:

1. Launch `MRRC FT-710` (right-click → Open the first time).
2. Confirm the browser opens the login page.
3. Log in with `FT710_WEB_PASSWORD`.
4. Confirm frequency, mode, and S-meter update from the radio.
5. Confirm RX audio works.
6. Confirm TX audio reaches the radio only when PTT is active.
7. Open a spectrum client and check logs for `scope_pipe: first frame received`.
8. Temporarily remove one FTDI dylib and confirm the app falls back to S-meter
   spectrum instead of crashing.
9. Quit from the menu-bar item and confirm `pgrep ft710-server` is empty.

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| "MRRC FT-710 is damaged / can't be opened" | Gatekeeper blocking ad-hoc signature | Right-click → Open, or `xattr -dr com.apple.quarantine /Applications/MRRC-FT710.app` |
| Browser opens but radio state does not update | Wrong serial port | Set `FT710_SERIAL_PORT` to the correct device from `ls /dev/cu.*` |
| App starts but FT4222 spectrum is unavailable | Missing `libft4222.dylib` / `libftd2xx.dylib` (or not using FT-710) | Place both in the bundle's `Contents/MacOS/vendor/ftdi/macos/`; IC-7300 uses CI-V `0x27` and does not need FTDI |
| Login fails | Wrong password | Check `~/Library/Application Support/MRRC-FT710/ft710.env` |
| Audio device not found | macOS selected another device | Set `FT710_AUDIO_RX_DEVICE` / `FT710_AUDIO_TX_DEVICE` by name or index |
| Port 8888 already in use | Another local service is listening | Change `FT710_WEB_PORT` |
| Server keeps running after quit | App was Force Quit, not menu-bar Quit | `pkill -f ft710-server`; use the menu-bar Quit item next time |
