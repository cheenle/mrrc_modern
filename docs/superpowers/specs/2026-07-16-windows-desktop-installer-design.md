# Windows Desktop Installer Design

## Goal

Build a Windows 11/12 desktop installer for the MRRC Modern server
(FT-710 and IC-7300/IC-7300MK2). The installed application runs as a
user-launched desktop app, not a Windows service. It bundles the Python runtime
and dependencies so users do not need to install Python manually.

> **Update (2026-08-17):** The installer now bundles both radio backends.  The
> active backend is selected with `MRRC_RADIO_MODEL=ft710|ic7300|ic7300mk2`
> (default `ft710`).  FTDI DLLs are still bundled for FT-710 FT4222 true
> spectrum, but they are not required for IC-7300/MK2, which uses a single USB
> CI-V port.

## Supported Behavior

- Install a desktop/start-menu application named `MRRC Modern`.
- Launch a desktop window/console helper that starts the local FastAPI server.
- Read configuration from an editable Windows-side file.
- Open the browser to the local web UI after startup.
- Stop the server when the desktop launcher exits.
- Support CAT control through the FT-710 Enhanced COM Port or the IC-7300/MK2
  USB CI-V port.
- Support RX/TX USB audio through PyAudio.
- Support FT4222 true spectrum capture on Windows when FTDI DLLs and drivers
  are available (FT-710 only).
- Fall back to S-meter spectrum when FT4222 is unavailable (or when using
  IC-7300/MK2, which uses CI-V 0x27 spectrum instead).

## Packaging Approach

Use PyInstaller one-folder output plus Inno Setup.

PyInstaller packages:

- `server.py` and backend modules.
- `static/` browser UI assets.
- `mem_channels.json` seed data.
- FTDI Windows DLLs in `vendor/ftdi/windows/bin/x64/`.
- Optional `opus.dll` when provided.
- A Windows launcher executable that manages config, server process startup,
  browser opening, and shutdown.

Inno Setup packages:

- The PyInstaller output directory.
- Default configuration template.
- Start-menu and desktop shortcuts.
- License and third-party notices, including FTDI license language.

## FTDI Windows DLL Strategy

The installer will bundle FTDI DLLs when they are present locally:

- `FT4222.dll` from LibFT4222 Windows middleware.
- `ftd2xx.dll` from the D2XX Windows package.

Source records:

- LibFT4222 official page: `https://ftdichip.com/software-examples/ft4222h-software-examples/`
- Expected LibFT4222 archive: `https://ftdichip.com/wp-content/uploads/2025/06/LibFT4222-v1.4.8.zip`
- D2XX official page: `https://ftdichip.com/drivers/d2xx-drivers/`
- Expected D2XX archive: `https://ftdichip.com/wp-content/uploads/2025/03/CDM-v2.12.36.20-WHQL-Certified.zip`

The current environment cannot download these ZIPs automatically because the
FTDI site returns HTTP 403 for non-browser direct `curl` requests. The build
tooling will therefore support both:

- Already-downloaded ZIPs in `vendor/ftdi/windows/downloads/`.
- Already-extracted DLLs in `vendor/ftdi/windows/bin/x64/`.

The package build fails with a clear message if FT4222 packaging is requested
but the required DLLs are missing.

## Windows Scope Runtime Design

The existing `scope_libraries.py` already searches Windows DLL names. The
Windows package must make the PyInstaller runtime directory and bundled FTDI
directory visible to the DLL loader.

Implementation requirements:

- Resolve resource paths correctly when running under PyInstaller.
- Add the bundled FTDI DLL directory with `os.add_dll_directory()` on Windows.
- Ensure `scope_pipe.py` can be launched from a frozen application.
- Prefer bundled DLLs before system directories.
- Keep S-meter fallback active if FT4222 initialization fails.

## Configuration

The desktop launcher will use a config file, for example:

```ini
FT710_SERIAL_PORT=COM3
FT710_BAUD_RATE=38400
FT710_WEB_HOST=127.0.0.1
FT710_WEB_PORT=8888
FT710_WEB_PASSWORD=changeme_please_use_strong_password!
FT710_SCOPE_PORT=
FT710_SCOPE_BAUD=115200
FT710_AUDIO_RX_DEVICE=
FT710_AUDIO_TX_DEVICE=
```

Users can edit the file from a Start Menu shortcut. First-run behavior should
use safe local defaults and show a clear warning that the default password must
be changed before LAN exposure.

## Build Outputs

Expected outputs:

- `dist/windows/MRRC-Modern/` from PyInstaller.
- `dist/windows/MRRC-Modern-Setup.exe` from Inno Setup.

## Verification

Hardware-independent checks:

- `python -m py_compile *.py`
- `python -m unittest discover -s tests -v`
- PyInstaller build completes on Windows.
- Frozen launcher starts the server and serves `/login`.
- Static assets load from the frozen runtime.

Hardware checks on Windows 11:

- FT-710 Enhanced COM Port can be selected as `COMx`.
- CAT polling updates frequency/mode/S-meter.
- USB audio input/output appears in PyAudio device list.
- RX audio streams to browser.
- TX audio reaches the radio.
- FT4222 mode emits real 1701-byte spectrum frames.
- Disconnecting FT4222 falls back to S-meter spectrum without crashing.

## Out of Scope

- Windows service installation.
- Code signing certificate automation.
- FTDI driver installer redistribution beyond bundled DLLs and license notices.
- ARM64 Windows support.
