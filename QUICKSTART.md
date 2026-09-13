# MRRC Web Control — Quick Start Guide

## 🚀 Immediate Setup (5 minutes)

### Prerequisites

- **Python 3.10+** installed (`python3 --version`)
- **A supported radio** over one USB cable. Ten `MRRC_RADIO_MODEL` keys today:
  - Yaesu FT-710 (`ft710`)
  - Icom `ic7300` / `ic7300mk2`, plus `ic705` / `ic7610` / `ic7760` (preview profiles)
  - Yaesu SDR family `ftdx10` / `ftdx101d` / `ftdx101mp` / `ftx1` (experimental)
  - The last six are **receive-only until you check the radio and set
    `MRRC_ALLOW_UNVERIFIED_TX=1`**; the Yaesu SDR family has no real spectrum
    source (the UI shows the S-meter synthesised spectrum).
- **Browser**: Safari 15+, Chrome, or Firefox

### Windows Installer Path

On Windows 11/12, prefer the desktop installer. It embeds Python and starts the
server from a launcher window.

```powershell
# Build on Windows:
packaging\windows\build.ps1

# Then install:
dist\windows\MRRC-FT710-Setup.exe
```

Configuration is stored at:

```text
%LOCALAPPDATA%\MRRC-FT710\ft710.env
```

Set `MRRC_RADIO_MODEL` in this file (see the Prerequisites list for the ten keys; default `ft710`).
See [docs/WINDOWS_INSTALLER_GUIDE.md](docs/WINDOWS_INSTALLER_GUIDE.md).

### 1. Install Dependencies

```bash
cd /Users/cheenle/HAM/mrrc_ft710
pip3 install -r requirements.txt
```

### 2. Configure Security (REQUIRED)

```bash
# Select radio backend (default is ft710); 10 keys today:
#   ft710
#   ic7300 / ic7300mk2 / ic705 / ic7610 / ic7760   (Icom CI-V)
#   ftdx10 / ftdx101d / ftdx101mp / ftx1          (Yaesu ASCII-CAT)
# The last six are receive-only until you check the radio and set
# MRRC_ALLOW_UNVERIFIED_TX=1.
export MRRC_RADIO_MODEL="ft710"

# Set a strong password (16+ characters recommended)
export MRRC_WEB_PASSWORD="YourStrongPassword123!"

# Optional: Change port (default: 8888)
export MRRC_WEB_PORT="8888"

# Optional: Bind to localhost only (more secure)
export MRRC_WEB_HOST="127.0.0.1"

# Optional: IC-7300 CI-V address if non-default (default 0x94)
export IC7300_CIV_ADDR="0x94"
```

### 3. Start the Server

#### FT-710

```bash
# macOS (Enhanced COM Port):
MRRC_RADIO_MODEL=ft710 MRRC_SERIAL_PORT=/dev/cu.usbserial-0121DB3A0 python3 server.py

# Linux:
MRRC_RADIO_MODEL=ft710 MRRC_SERIAL_PORT=/dev/ttyUSB0 python3 server.py
```

#### IC-7300 / IC-7300MK2

Use a single USB cable. The same cable carries CI-V control and 48kHz USB audio.
Set `MENU` → `SET` → `Connectors` → `USB(CIV) Function` to `CI-V` and
`USB(CIV) Baud Rate` to `115200`. Default CI-V address is `0x94`.

```bash
# macOS:
MRRC_RADIO_MODEL=ic7300 MRRC_SERIAL_PORT=/dev/cu.usbserial-A1234567 python3 server.py

# Linux:
MRRC_RADIO_MODEL=ic7300 MRRC_SERIAL_PORT=/dev/ttyUSB0 python3 server.py
```

For IC-7300MK2, use `MRRC_RADIO_MODEL=ic7300mk2`.

#### Convenience script

```bash
./start.sh
```

### 4. Open in Browser

Navigate to: **http://localhost:8888**

Enter your password when prompted.

---

## 🔧 Optional: FT4222 Spectrum (Real FFT Data)

For true 850-point FFT spectrum waterfall:

1. macOS: copy `libft4222.dylib` and `libftd2xx.dylib` to `mrrc_ft710/lib/`, then install `ftd2xx.cfg` to `/usr/local/lib/` with `DetachKernelDriver=1`.
2. Linux: copy compatible `libft4222.so` and `libftd2xx.so` into the repo's `vendor/ftdi/` or point `MRRC_FTDI_LIB_DIR` at them.
3. Windows installer: place `FT4222.dll` and `ftd2xx.dll` in `vendor\ftdi\windows\bin\x64` before running `packaging\windows\build.ps1`.

Without FT4222, the app falls back to S-meter-based synthetic spectrum.

---

## 🎙️ Server-Side Recording (v1.15.0+)

Press **REC** in the browser: the QSO is recorded **on the server** (RX + your
microphone on one 16 kHz timeline, incremental MP3), so a page reload, a closed
tab or a CAT dropout cannot stop it. The ☰ menu → **Recordings** panel lists the
files with a seekable player, download and delete (a recording in progress
cannot be deleted).

```bash
export MRRC_RECORDINGS_DIR=~/mrrc-recordings   # default: <runtime dir>/recordings
export MRRC_RECORDINGS_BITRATE=64              # kbps, 16 kHz mono
export MRRC_RECORDINGS_MAX_SESSION_MIN=240     # stop a forgotten REC (0 = off)
```

Files are never deleted automatically; `MRRC_RECORDINGS_MAX_SESSION_MIN` only
stops. Log line `Recording ready: … (16 kHz mono MP3)` on startup means the MP3
encoder (`lameenc`) is available.

---

## 🎤 Audio Setup

The server uses PyAudio (PortAudio) for USB audio capture/playback:

- **FT-710 RX/TX**: 44.1kHz mono USB audio; the server resamples to/from 48kHz for Opus.
- **IC-7300 RX/TX**: 48kHz mono USB audio; no resampling required.

If you have multiple audio devices, specify them:

```bash
# FT-710
export MRRC_AUDIO_RX_DEVICE="FT-710"   # Match by name
export MRRC_AUDIO_TX_DEVICE="3"        # Match by index

# IC-7300 (often enumerates as "USB Audio CODEC" or similar)
export MRRC_AUDIO_RX_DEVICE="USB Audio CODEC"
export MRRC_AUDIO_TX_DEVICE="USB Audio CODEC"
```

---

## 🏥 Health Check

```bash
curl http://localhost:8888/api/health
```

Expected response:
```json
{
  "status": "healthy",
  "radio_connected": true,
  "uptime_seconds": 120,
  "clients": 1
}
```

---

## 🛑 Stop the Server

```bash
./stop.sh
# Or press Ctrl+C in the terminal
```

---

## 📋 Troubleshooting

| Problem | Solution |
|---------|----------|
| "Serial port not found" | Check `MRRC_SERIAL_PORT` matches your device (legacy `FT710_*` names still work) |
| "Audio device not found" | Check `MRRC_AUDIO_RX_DEVICE` / `MRRC_AUDIO_TX_DEVICE` |
| "Permission denied" on serial | Add user to dialout group (Linux) or check USB permissions (macOS) |
| Login rate limited | Wait 5 minutes or check your IP |
| No audio | Ensure libopus is installed: `brew install opus` (macOS) |
| Spectrum shows flat line | FT-710: check FT4222 libraries/DLLs. IC-7300: spectrum is derived from CI-V `0x27` frames on the same serial port; confirm CI-V scope output is enabled in radio menus |
| `MRRC_RADIO_MODEL` ignored | Make sure it is exported in the same shell before `python server.py` |
| Pressed PTT but the radio stays in RX | The model is one of the six unverified profiles: check it with `_diag_civ.py` / `_diag_yaesu.py`, then set `MRRC_ALLOW_UNVERIFIED_TX=1` and restart |
| Recording button shows "⚠ 丢失 N 块" | The writer could not keep up (slow/network disk or a saturated CPU); the file has holes — point `MRRC_RECORDINGS_DIR` at a local disk |
| `Recording disabled: the MP3 encoder is missing` | `pip install -r requirements.txt` (provides `lameenc`) and restart |

---

## 📖 More Documentation

- [SECURITY_GUIDE.md](SECURITY_GUIDE.md) — Security configuration details
- [DEPENDENCIES.md](DEPENDENCIES.md) — Cross-platform dependency guide
- [docs/WINDOWS_INSTALLER_GUIDE.md](docs/WINDOWS_INSTALLER_GUIDE.md) — Windows installer and FT4222 packaging
- [README.md](README.md) — Full feature documentation
- [SDD/](SDD/) — Software Design Description
