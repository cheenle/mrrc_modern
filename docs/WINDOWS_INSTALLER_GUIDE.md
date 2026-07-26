# Windows Desktop Installer Guide

This guide covers the Windows desktop package for MRRC FT-710 Web Control.
The package is designed for Windows 11 and Windows 12-class x64 desktop
systems. It installs a user-launched desktop app with an embedded Python
runtime; users do not need to install Python manually.

## Download (v1.7.0)

| File | Size | MD5 |
|------|------|-----|
| `MRRC-FT710-Setup.exe` | _(fill after build)_ | _(fill after build)_ |

- Fast mirror (recommended in CN): <https://www.vlsc.net/mrrc_ft710/downloads/MRRC-FT710-Setup.exe>
- GitHub Releases: <https://github.com/cheenle/mrrc_ft710/releases>

The v1.7.0 package ships the optional ATR1000 tuner linkage, cookie-based
frontend settings, and the device-side mic gain (see CHANGELOG), on top of
the v1.6.3 packaging fixes: frozen `_internal` resource resolution,
scope_pipe stdout heartbeat, hardened `build.ps1`, and the launcher
self-spawn guard. Build it on Windows 11 (Python 3.12, PyInstaller 6.21.0,
Inno Setup 6.x) with `packaging/windows/build.ps1`; afterwards fill in the
size/MD5 table above (`md5sum dist/windows/MRRC-FT710-Setup.exe`).

## User Installation

### 1. Install required device drivers

Install these before launching the app:

- Silicon Labs CP210x Universal Windows Driver for the FT-710 Enhanced COM Port.
- FTDI D2XX driver if you want FT4222 true spectrum.

After connecting the FT-710 USB cable, open Device Manager and check:

- Ports (COM & LPT) shows two Silicon Labs CP210x COM ports.
- The lower-numbered CP210x COM port is typically the Enhanced COM Port for CAT.
- USB audio devices include the FT-710 audio input and output.

### 2. Install MRRC FT-710

Run:

```text
MRRC-FT710-Setup.exe
```

The installer creates:

- Start Menu shortcut: `MRRC FT-710`
- Optional desktop shortcut
- Start Menu shortcut: `Edit Configuration`

### 3. Edit configuration

Use the Start Menu `Edit Configuration` shortcut, or open:

```text
%LOCALAPPDATA%\MRRC-FT710\ft710.env
```

Typical configuration:

```ini
FT710_SERIAL_PORT=COM3
FT710_BAUD_RATE=38400
FT710_WEB_HOST=127.0.0.1
FT710_WEB_PORT=8888
FT710_WEB_PASSWORD=change_this_password
FT710_SCOPE_PORT=
FT710_SCOPE_BAUD=115200
FT710_AUDIO_RX_DEVICE=
FT710_AUDIO_TX_DEVICE=
FT710_FTDI_LIB_DIR=vendor\ftdi\windows\bin\x64
#FT710_ATR1000_HOST=
#FT710_ATR1000_PORT=60001
```

Set `FT710_SERIAL_PORT` to the FT-710 Enhanced COM Port from Device Manager.
Change `FT710_WEB_PASSWORD` before exposing the app beyond localhost.

### 4. Launch

Start `MRRC FT-710` from the Start Menu or desktop shortcut. The launcher:

1. Reads `%LOCALAPPDATA%\MRRC-FT710\ft710.env`.
2. Starts the bundled server and waits for it to answer HTTP before opening
   `http://127.0.0.1:8888` in the default browser (up to ~15 seconds on the
   first run).
3. Use Ctrl-C in the launcher window for a graceful stop (audio drains and
   PTT releases first).

**Warning:** closing the launcher window with × kills both processes
*abruptly* — there is no graceful cleanup. If the radio is transmitting, it
can stay keyed. Always release PTT before closing the window.

## FT4222 True Spectrum

The Windows package supports FT4222 true spectrum when these runtime files are
present:

```text
vendor\ftdi\windows\bin\x64\FT4222.dll
vendor\ftdi\windows\bin\x64\ftd2xx.dll
```

If either DLL is missing, the app still runs and uses the S-meter fallback
spectrum. The fallback is useful for basic activity visualization but does not
provide true FFT waterfall data.

Expected FTDI sources are documented in:

```text
vendor\ftdi\windows\README.md
```

The helper script can try to download the archives:

```powershell
vendor\ftdi\windows\fetch-ftdi.ps1
```

The FTDI site may block automated downloads. If that happens, use the printed
URLs in a browser, extract the DLLs, and place them in `bin\x64`.

## Building the Installer

Build on a Windows x64 machine.

### Prerequisites

- Python 3.11 or 3.12
- Project dependencies from `requirements.txt`
- PyInstaller
- Inno Setup with `iscc` available in `PATH`
- FTDI DLLs in `vendor\ftdi\windows\bin\x64` for FT4222 support

Install build tools:

```powershell
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r packaging\windows\requirements-build.txt
```

Build:

```powershell
packaging\windows\build.ps1
```

Expected outputs:

```text
dist\windows\MRRC-FT710\
dist\windows\MRRC-FT710-Setup.exe
```

The build script runs syntax checks and the test suite before packaging.

## Build Components

| File | Purpose |
|------|---------|
| `windows\launcher.py` | Desktop launcher; starts/stops the server and opens the browser |
| `windows\default.env` | Initial user configuration template |
| `packaging\pyinstaller\ft710_server.spec` | Bundles the FastAPI server and static UI |
| `packaging\pyinstaller\scope_pipe.spec` | Bundles the FT4222 scope worker |
| `packaging\pyinstaller\ft710_launcher.spec` | Bundles the desktop launcher |
| `packaging\windows\MRRC-FT710.iss` | Inno Setup installer definition |
| `packaging\windows\build.ps1` | End-to-end Windows build script |

## Verification Checklist

After installing on Windows:

1. Launch `MRRC FT-710`.
2. Confirm the browser opens the login page.
3. Log in with `FT710_WEB_PASSWORD`.
4. Confirm frequency, mode, and S-meter update from the radio.
5. Confirm RX audio works.
6. Confirm TX audio reaches the radio only when PTT is active.
7. Open a spectrum client and check logs for `scope_pipe: first frame received`.
8. Temporarily remove one FTDI DLL and confirm the app falls back to S-meter
   spectrum instead of crashing.

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Browser opens but radio state does not update | Wrong COM port | Set `FT710_SERIAL_PORT` to the Enhanced COM Port |
| App starts but FT4222 spectrum is unavailable | Missing `FT4222.dll` or `ftd2xx.dll` | Place both DLLs in `vendor\ftdi\windows\bin\x64` before building |
| Login fails | Wrong password | Check `%LOCALAPPDATA%\MRRC-FT710\ft710.env` |
| Audio device not found | Windows selected another audio device | Set `FT710_AUDIO_RX_DEVICE` / `FT710_AUDIO_TX_DEVICE` by name or index |
| Port 8888 already in use | Another local service is listening | Change `FT710_WEB_PORT` |

