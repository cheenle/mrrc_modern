# MRRC Web Control — Cross-Platform Dependencies & Drivers

## Platform Support Overview

| Platform       | Architecture    | CAT/CI-V Serial | Audio I/O | Scope/Spectrum | Auto-Install |
|----------------|----------------|:---------------:|:---------:|:--------------:|:------------:|
| macOS 13+      | arm64 / x86_64 | ✅              | ✅        | ✅ (FT4222 / CI-V) | `./install.sh` |
| Debian/Ubuntu  | amd64 / arm64  | ✅              | ✅        | ⚠️ FT-710 needs libs / ✅ IC-7300 CI-V | `./install.sh` |
| Fedora/RHEL    | amd64 / arm64  | ✅              | ✅        | ⚠️ FT-710 needs libs / ✅ IC-7300 CI-V | `./install.sh` |
| Arch Linux     | amd64 / arm64  | ✅              | ✅        | ⚠️ FT-710 needs libs / ✅ IC-7300 CI-V | `./install.sh` |
| Raspberry Pi OS| armhf / arm64  | ✅              | ✅        | ⚠️ FT-710 needs libs / ✅ IC-7300 CI-V | `./install.sh` |
| Windows 11/12  | amd64          | ✅              | ✅        | ✅ with FTDI DLLs (FT-710) / ✅ CI-V (IC-7300) | desktop installer |

**Legend**: ✅ = fully supported, ⚠️ = requires manual library setup, ❌ = not available

FT-710 true spectrum requires FTDI FT4222 libraries. IC-7300/IC-7300MK2 use CI-V `0x27` scope frames on the same USB serial port and do **not** require FTDI libraries.


## Python Version

**Python 3.10+ required.**

The server uses `asyncio.TaskGroup`-compatible patterns, `str.removeprefix`/`removesuffix`,
and the `X | None` union syntax (`PEP 604`), all of which require Python 3.10+.
Python 3.11+ is recommended for `asyncio` performance improvements.

| OS | Install Python |
|----|---------------|
| macOS | `brew install python@3.12` |
| Debian/Ubuntu | `sudo apt install python3.12 python3.12-venv` |
| Fedora | `sudo dnf install python3.12` |
| Arch | `sudo pacman -S python` (typically 3.12+) |
| Raspberry Pi | `sudo apt install python3.11 python3.11-venv` (Bookworm) |
| Windows | Download from [python.org](https://www.python.org/downloads/) |

**Note**: Python 3.9 is NOT supported due to type hint syntax requirements.


## Core Python Packages

These are installed from `requirements.txt`:

| Package | Min Version | Purpose | Notes |
|---------|------------|---------|-------|
| `fastapi` | ≥0.100.0 | Web framework (HTTP + WebSocket routing) | |
| `uvicorn[standard]` | ≥0.23.0 | ASGI server (uvloop + httptools) | `[standard]` pulls in `websockets` (wsproto) and `watchfiles` |
| `pyserial` | ≥3.5 | Serial CAT/CI-V protocol (CP210x USB-UART) | Sync API; all blocking I/O offloaded to `asyncio.to_thread()` |
| `websockets` | ≥12.0 | WebSocket protocol (4 channels) | Pulled in by `uvicorn[standard]` but listed explicitly |
| `pyaudio` | ≥0.2.11 | Sound card I/O (PortAudio wrapper) | RX capture + TX playback at per-backend rate (FT-710: 44.1 kHz; IC-7300: 48 kHz) |
| `numpy` | ≥1.24.0 | Audio resampling (44.1k↔48k) + S-meter spectrum fallback | Stateless linear interpolation |

### Optional Python Packages

| Package | Purpose | When Needed |
|---------|---------|-------------|
| `cryptography` | TLS/SSL support for HTTPS | When using `--ssl-cert`/`--ssl-key` |
| `pyopenssl` | Alternative SSL backend | If `cryptography` is unavailable |
| `httptools` | HTTP parser acceleration | Auto-installed by `uvicorn[standard]` |
| `uvloop` | Faster event loop (Linux/macOS) | Auto-installed by `uvicorn[standard]` |

### Dev/Test Dependencies (`requirements-dev.txt`)

| Package | Purpose |
|---------|---------|
| `pytest` | Test runner |
| `pytest-asyncio` | Async test support |
| `pytest-cov` | Coverage reports |
| `mypy` | Static type checking |


## System / SDK Dependencies

### 1. PortAudio (Required — Audio I/O)

PortAudio is the native C library that PyAudio wraps. **Without PortAudio, the
entire audio subsystem (RX/TX) is unavailable**, though basic CAT control and
spectrum display will still work.

| OS | Install Command | Package(s) |
|----|----------------|------------|
| macOS | `brew install portaudio` | `portaudio` |
| Debian/Ubuntu | `sudo apt install portaudio19-dev libportaudio2` | `portaudio19-dev`, `libportaudio2` |
| Fedora/RHEL | `sudo dnf install portaudio-devel` | `portaudio-devel` |
| Arch | `sudo pacman -S portaudio` | `portaudio` |
| Raspberry Pi | `sudo apt install portaudio19-dev libportaudio2` | same as Debian |
| Windows | *Bundled with PyAudio wheel* | Precompiled in PyAudio wheel (no separate install) |

**macOS note:** Homebrew's portaudio is compiled with CoreAudio backend
— no extra drivers needed. `pyaudio` finds the Homebrew prefix automatically
when both are installed via brew.

**Linux note:** On ARM (Raspberry Pi), `portaudio19-dev` pulls in `libasound2-dev`
(ALSA dev headers). The FT-710 USB audio appears as an ALSA card (`hw:CARD=CODEC`).

**Windows note:** PyAudio wheels on PyPI include a bundled PortAudio DLL.
If installing from source (rare), install PortAudio via `vcpkg install portaudio`.

### 2. libopus (Recommended — Audio Compression)

libopus provides Opus audio codec support for ~10-12× bandwidth reduction
(768 kbps raw PCM → ~64 kbps Opus). **Without it, audio falls back to
uncompressed Int16 PCM (768 kbps mono)**, which may stutter on slow/mobile
connections.

| OS | Install Command | Package(s) |
|----|----------------|------------|
| macOS | `brew install opus` | `opus` |
| Debian/Ubuntu | `sudo apt install libopus0 libopus-dev` | `libopus0`, `libopus-dev` |
| Fedora/RHEL | `sudo dnf install opus opus-devel` | `opus`, `opus-devel` |
| Arch | `sudo pacman -S opus` | `opus` |
| Raspberry Pi | `sudo apt install libopus0 libopus-dev` | same as Debian |
| Windows | Place `opus.dll` in project root, or in `PATH` | No standard package |

**How the server finds libopus:**

The `opus_rx.py` module uses `ctypes.util.find_library("opus")` and falls back to:

```
macOS:  /opt/homebrew/lib/libopus.dylib  →  /usr/local/lib/libopus.dylib
Linux:  libopus.so.0  →  libopus.so
```

If libopus is installed but not found, set the standard library path:

```bash
# macOS
export DYLD_LIBRARY_PATH="/opt/homebrew/lib:$DYLD_LIBRARY_PATH"

# Linux
export LD_LIBRARY_PATH="/usr/lib/arm-linux-gnueabihf:$LD_LIBRARY_PATH"
```

**Verification:**
```bash
python -c "from opus_rx import RxOpusEncoder; e = RxOpusEncoder(); print('OK')"
```

### 3. OpenSSL / LibreSSL (Optional — HTTPS/TLS)

For SSL/TLS connections, the `uvicorn[standard]` package pulls in
`httptools`, which links to the system OpenSSL. For certificate-based
HTTPS, you also need `cryptography` (`pip install cryptography`).

| OS | Package | Notes |
|----|---------|-------|
| macOS | Built-in (LibreSSL) | Comes with macOS |
| Linux | `libssl-dev` / `openssl-devel` | Usually pre-installed |
| Windows | Built into Python | Schannel or bundled OpenSSL |


## USB / Serial Device Drivers

### FT-710

The FT-710 connects to the computer via a single USB cable but exposes
**four distinct USB interfaces**:

```
FT-710 USB (single cable)
├── Silicon Labs CP210x #1  →  Enhanced COM Port (CAT control, 38400 baud)
├── Silicon Labs CP210x #2  →  Standard COM Port (sub-display / SCU-LAN10, 115200 baud)
├── FTDI FT4222             →  SPI bridge to internal scope/spectrum processor
└── USB Audio Class 1.0     →  Mono audio input (RX) + Mono audio output (TX), 44.1 kHz
```

### IC-7300 / IC-7300MK2

The IC-7300 uses a **single USB cable** for both control and audio:

```
IC-7300 USB (single cable)
├── USB Serial (CI-V)   →  CAT control (115200 8N1, default address 0x94)
└── USB Audio Class     →  Stereo audio input (RX) + Mono audio output (TX), 48 kHz
```

**No FTDI libraries are required.** The only drivers needed are the OS built-in
USB-serial and USB-audio drivers (or the standard Silicon Labs/Icom USB driver
supplied by the OS). Spectrum data comes from CI-V `0x27` frames on the same
serial port.

### 3.1 Silicon Labs CP210x (CAT Serial — REQUIRED)

**This is the most critical driver.** Without it, the server cannot control
the radio at all (no frequency changes, no mode changes, no PTT).

#### macOS

| macOS Version | Driver Status |
|--------------|---------------|
| macOS 13 (Ventura)+ | **Built-in.** AppleUSBCDC driver handles CP210x natively. Plug and play. |
| macOS 10.15–12 | Built-in, but the official SiLabs driver may work better |
| macOS 10.14– | Install [SiLabs CP210x VCP Driver](https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers) |

**Device naming:** `/dev/cu.SLAB_USBtoUART` (Enhanced) and `/dev/cu.usbserial-*` (Standard).

**Verify:**
```bash
ls /dev/cu.SLAB_USBtoUART* /dev/cu.usbserial-* 2>/dev/null
# Should see two devices:
#   /dev/cu.SLAB_USBtoUART    (Enhanced COM, CAT port)
#   /dev/cu.SLAB_USBtoUART1   or /dev/cu.usbserial-XXXXXXXXX  (Standard COM)
```

#### Linux

**Built-in since kernel 2.6.** The `cp210x` kernel module is loaded automatically.

| Distro | Driver |
|--------|--------|
| All | Built-in `cp210x` kernel module |
| Raspberry Pi OS | Built-in (same kernel module) |

**Device naming:** `/dev/ttyUSB0` (Enhanced) and `/dev/ttyUSB1` (Standard).

**Permissions:** The user must be in the `dialout` group (Debian/Ubuntu/Fedora)
or `uucp` group (Arch):

```bash
# Check current permissions:
ls -l /dev/ttyUSB0
# If permissions are "crw-rw---- 1 root dialout ..." → you need dialout group
# If permissions are "crw-r--r-- 1 root root ..."   → no group access at all

# Quick check: can you write to the port?
[ -w /dev/ttyUSB0 ] && echo "Writable" || echo "NOT writable"

# Debian/Ubuntu/Fedora:
sudo usermod -a -G dialout $USER
# Log out and back in (or: newgrp dialout)

# Arch Linux:
sudo usermod -a -G uucp $USER
newgrp uucp

# Verify group membership after re-login:
groups | grep -E "dialout|uucp"
```

**Common serial permission scenarios:**

| Permission | Owner:Group | Meaning | Fix |
|-----------|-------------|---------|-----|
| `crw-rw----` | `root:dialout` | Members of `dialout` can read/write | `sudo usermod -a -G dialout $USER` |
| `crw-r--r--` | `root:root` | No group write — udev rules missing | Install udev rules (below) |
| `crw-rw-rw-` | `root:dialout` | World-writable (udev MODE=0666) | Already accessible ✅ |
| `crw-------` | `root:root` | Only root can access | `sudo chmod 666 /dev/ttyUSB0` (temporary) |

**udev rules (recommended):**
```
# /etc/udev/rules.d/99-ft710.rules
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", MODE="0666", SYMLINK+="ft710-cat"
```
Then: `sudo udevadm control --reload-rules && sudo udevadm trigger`

**Verify:**
```bash
ls /dev/ttyUSB* /dev/ft710-cat 2>/dev/null
# Should show ttyUSB0 (CAT) and ttyUSB1 (scope serial)
```

#### Windows

Download and install the **CP210x Universal Windows Driver** from
[Silicon Labs](https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers).

After installation, the FT-710 will appear as two COM ports:
- **COM port with lower number** → Enhanced COM Port (CAT) — use this as `FT710_SERIAL_PORT`
- **COM port with higher number** → Standard COM Port

**Device Manager:** Look under "Ports (COM & LPT)" → "Silicon Labs CP210x USB to UART Bridge".

**Verify:** Open Device Manager → Ports (COM & LPT). You should see two CP210x entries.

### 3.2 FTDI FT4222 (Scope/Spectrum — OPTIONAL)

The FT4222 is an internal SPI bridge chip that streams raw scope FFT data
from the FT-710's DSP at ~21-30 fps (850-point spectrum × 2 receivers).
**Without it, the spectrum waterfall falls back to a synthetic S-meter based
display** — still shows band activity but without true FFT resolution.

#### macOS

**Required components:**

1. **D2XX driver** (libftd2xx) — Claims the FT4222 from macOS's built-in
   AppleUSBFTDI/VCP driver so the D2XX API can access it directly.

2. **ftd2xx.cfg** — Configuration file at `/usr/local/lib/ftd2xx.cfg`
   with `DetachKernelDriver=1`. Without this, the FT4222 stays bound
   to the Apple VCP driver and D2XX calls fail.

3. **libft4222.dylib + libftd2xx.dylib** — Placed in `lib/` directory
   (bundled with project, sourced from the wfview app bundle).

**Setup:**
```bash
# Install D2XX config (one-time, requires sudo)
sudo mkdir -p /usr/local/lib
sudo cp lib/ftd2xx.cfg /usr/local/lib/

# Verify libraries are in place:
ls -l lib/libft4222.dylib lib/libftd2xx.dylib
```

**Important:** After installing `ftd2xx.cfg`, physically unplug and replug
the FT-710 USB cable for the D2XX driver to claim the device.

**Verify FT4222 connectivity:**
```bash
# Check IORegistry for FT4222
ioreg -p IOUSB -w0 -l | grep -A5 "FT4222"
# Should show FT4222 A device if properly connected
```

**Known issue — libft4222 version sensitivity:**
The FTDI SPI init (`FT4222_SPIMaster_Init`) can fail on certain versions
of `libft4222.dylib`. The project bundles a known-good version from wfview.
If scope readings are all zeros or SPI init fails, try:

```bash
cp lib/libft4222_wfview.dylib lib/libft4222.dylib
```

#### Linux

**Linux uses libftd2xx for direct USB access (no kernel driver detachment needed).**

On x86_64, pre-built FTDI libraries are available. On ARM (Raspberry Pi),
the libraries must be cross-compiled from source (FTDI does not provide
ARM Linux binaries for D2XX).

**x86_64 Linux — obtaining libraries:**
```bash
# Option A: Copy from wfview build directory (if you built wfview from source)
cp /path/to/wfview/build/lib/libft4222.so lib/
cp /path/to/wfview/build/lib/libftd2xx.so lib/

# Option B: Extract from wfview AppImage
./wfview-*.AppImage --appimage-extract
cp squashfs-root/usr/lib/libft4222.so lib/
cp squashfs-root/usr/lib/libftd2xx.so lib/

# Verify dependencies are satisfied:
ldd lib/libft4222.so | grep "not found"
# Should show no missing dependencies. If libftd2xx.so shows as "not found":
#   - Ensure libftd2xx.so is in the same directory, OR
#   - Set LD_LIBRARY_PATH: export LD_LIBRARY_PATH=$(pwd)/lib:$LD_LIBRARY_PATH
#   - Or add to ldconfig: echo "$(pwd)/lib" | sudo tee /etc/ld.so.conf.d/ft710.conf && sudo ldconfig
```

**Key diagnostic:**
```bash
# Check if libft4222.so can find its dependency (libftd2xx.so)
ldd lib/libft4222.so
# Look for: libftd2xx.so => /path/to/lib/libftd2xx.so
# If it says "not found", the linker can't find libftd2xx.so — fix with LD_LIBRARY_PATH

# Check library file types:
file lib/libft4222.so        # Should say "ELF 64-bit" or "ELF 32-bit"
file lib/libftd2xx.so        # Must match architecture of libft4222.so

# Verify symbols are exported:
nm -D lib/libft4222.so | grep FT4222_SPIMaster_Init
nm -D lib/libftd2xx.so | grep FT_OpenEx
```

**Environment overrides for Linux:**
```bash
# Per-library override (preferred)
export FT710_FT4222_LIB=/path/to/libft4222.so
export FT710_FTD2XX_LIB=/path/to/libftd2xx.so

# Or: shared directory
export FT710_FTDI_LIB_DIR=/path/to/ftdi_libs
```

**ARM Linux (Raspberry Pi):**
The FTDI D2XX libraries must be compiled from source. FTDI does not provide
ARM Linux binaries.

**Building libftd2xx for ARM from wfview source:**
```bash
# 1. Clone wfview
git clone https://gitlab.com/eliggett/wfview.git
cd wfview

# 2. Install build dependencies
sudo apt install cmake build-essential libusb-1.0-0-dev qtbase5-dev

# 3. Build (the FTDI libs are compiled as part of wfview's CMake)
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j4

# 4. Copy the compiled libraries to the FT710 project
cp lib/libft4222.so /path/to/FT710/lib/
cp lib/libftd2xx.so /path/to/FT710/lib/
```

**Without FTDI libraries:** The scope falls back to S-meter based spectrum
display — still functional for band activity monitoring. See
`FT710_FT4222_CLK_DIV` env var if you need to adjust SPI timing.

**udev rules for FT4222:**
```
# /etc/udev/rules.d/99-ft710.rules (add this line)
SUBSYSTEM=="usb", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="601c", MODE="0666"
```

**Verify FT4222 on Linux:**
```bash
lsusb | grep "0403:601c"
# Expected: Bus XXX Device XXX: ID 0403:601c Future Technology Devices International, Ltd FT4222
# If nothing shows: the FT-710 is not connected via USB, or the FT4222 chip is not exposed
# (this is normal for some FT-710 units — the scope pipeline will use S-meter fallback)
```

**Common Linux issues:**

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| `libftd2xx.so: cannot open shared object file` | Missing file or wrong path | Copy from wfview build, or set `LD_LIBRARY_PATH` |
| `libft4222.so` found but `libftd2xx.so` not found | Only one of two required libs present | Both libraries are needed — copy the missing one |
| `FT_OpenEx` fails (error 1, 2, or 3) | No FT4222 device connected | Check USB cable, verify `lsusb \| grep 0403:601c` |
| `FT4222_SPIMaster_Init` fails (error 1000) | SPI init failure — device state issue | Try replugging USB, or reduce `FT710_FT4222_CLK_DIV` |
| `libft4222.so: wrong ELF class` | Architecture mismatch (x86_64 .so on ARM) | Rebuild from source on target architecture |
| `FT710_FTDI_LIB_DIR` not found | scope_libraries.py search path issue | Set `FT710_FT4222_LIB` and `FT710_FTD2XX_LIB` explicitly |

#### Windows

FT4222 scope capture is supported by the Windows desktop package when the FTDI
runtime DLLs are bundled:

```text
vendor\ftdi\windows\bin\x64\FT4222.dll
vendor\ftdi\windows\bin\x64\ftd2xx.dll
```

The PyInstaller build produces a frozen `scope_pipe.exe` worker. At runtime the
server starts that worker for spectrum clients, and the worker adds the bundled
FTDI directory to the Windows DLL loader path before loading `FT4222.dll` and
`ftd2xx.dll`.

If either DLL is missing or the device cannot be initialized, the server stays
up and the spectrum waterfall uses the S-meter fallback.

### 3.3 USB Audio Class (Audio I/O — REQUIRED for Audio)

**No driver installation needed on any platform.** Both radios present as
standard USB Audio Class devices — plug and play on macOS, Linux, and Windows.

```
FT-710 USB Audio Device:
  - Input  (capture): 1 channel, 44,100 Hz, 16-bit  (RX audio from radio to server)
  - Output (playback): 1 channel, 44,100 Hz, 16-bit  (TX audio from server to radio)

IC-7300 USB Audio Device:
  - Input  (capture): 2 channels, 48,000 Hz, 16-bit  (RX audio from radio to server)
  - Output (playback): 1 channel, 48,000 Hz, 16-bit  (TX audio from server to radio)
```

**Auto-detection:** The `AudioHandler` automatically finds the radio audio
device by scanning PyAudio devices for names containing "FT-710", "FT710",
"YAESU", "USB Audio CODEC", or "USB Audio Device". It uses heuristics
(mono input, full-duplex capability) as fallbacks.

**Manual override:**
```bash
# By device index (from PyAudio enumeration at startup):
FT710_AUDIO_RX_DEVICE=3 FT710_AUDIO_TX_DEVICE=3 python server.py

# By name substring:
FT710_AUDIO_RX_DEVICE="FT-710" FT710_AUDIO_TX_DEVICE="FT-710" python server.py

# IC-7300 commonly enumerates as:
FT710_AUDIO_RX_DEVICE="USB Audio CODEC" FT710_AUDIO_TX_DEVICE="USB Audio CODEC" python server.py
```

**Headless / no-audio server:**
If the server has no audio hardware (e.g., cloud VM, headless Raspberry Pi
with no USB audio, Docker container without sound device passthrough), the
server starts normally but audio features are unavailable. The startup log
will show:

```
PyAudio not available — audio disabled. Install: pip install pyaudio
```

or

```
No audio input device found
```

In this mode:
- ✅ CAT control (frequency, mode, PTT, filters) — **fully functional**
- ✅ Spectrum waterfall (S-meter fallback if no FT4222)
- ✅ Web UI with all controls
- ❌ RX audio (no audio device to capture from)
- ❌ TX audio (no audio device to play to)

To suppress audio warnings on a headless server:
```bash
# Install pyaudio but expect no device — control-only mode
pip install pyaudio  # will import but find no devices
```


## Platform-Specific Installation Guides

### macOS (arm64 Apple Silicon / x86_64 Intel)

```bash
# 1. Install Homebrew (if not already)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. Install system dependencies
brew install python@3.12 portaudio opus

# 3. Clone and set up project
git clone <repo-url> FT710 && cd FT710

# 4. Create virtual environment
python3.12 -m venv venv
source venv/bin/activate

# 5. Install Python packages
pip install -r requirements.txt

# 6. Set up D2XX config for scope (one-time)
sudo mkdir -p /usr/local/lib
sudo cp lib/ftd2xx.cfg /usr/local/lib/
# Unplug and replug FT-710 USB cable

# 7. Run
FT710_SERIAL_PORT=/dev/cu.SLAB_USBtoUART python server.py
```

**Apple Silicon (arm64) notes:**
- All dependencies (Python, portaudio, opus) are natively compiled for arm64 via Homebrew
- The FTDI `.dylib` files in `lib/` are universal binaries (arm64 + x86_64)
- The Opus ctypes wrapper explicitly avoids `opus_encoder_ctl` (variadic C function)
  because the arm64 variadic ABI is incompatible with ctypes — instead uses
  `max_data_bytes` cap on `opus_encode()` to control bitrate

**Intel (x86_64) notes:**
- Homebrew installs to `/usr/local` instead of `/opt/homebrew`
- FTDI libraries in `lib/` work on both architectures

### Debian / Ubuntu (amd64 / arm64)

```bash
# 1. Install system dependencies
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev
sudo apt install -y portaudio19-dev libportaudio2
sudo apt install -y libopus0 libopus-dev

# 2. Add user to dialout group
sudo usermod -a -G dialout $USER
newgrp dialout

# 3. Set up project
cd FT710
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. (Optional) Install udev rules
sudo tee /etc/udev/rules.d/99-ft710.rules <<'EOF'
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", MODE="0666", SYMLINK+="ft710-cat"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="601c", MODE="0666"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger

# 5. Run
FT710_SERIAL_PORT=/dev/ttyUSB0 python server.py
```

### Fedora / RHEL

```bash
# 1. Install system dependencies
sudo dnf install -y python3.12 python3.12-devel
sudo dnf install -y portaudio-devel
sudo dnf install -y opus opus-devel

# 2. Add user to dialout group
sudo usermod -a -G dialout $USER

# 3. Set up project
cd FT710
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. Run
FT710_SERIAL_PORT=/dev/ttyUSB0 python server.py
```

### Arch Linux

```bash
# 1. Install system dependencies
sudo pacman -S python python-pip
sudo pacman -S portaudio
sudo pacman -S opus

# 2. Add user to uucp (or dialout) group
sudo usermod -a -G uucp $USER

# 3. Set up project
cd FT710
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. Run
FT710_SERIAL_PORT=/dev/ttyUSB0 python server.py
```

### Raspberry Pi (Raspberry Pi OS Bookworm)

```bash
# 1. Install system dependencies
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev
sudo apt install -y portaudio19-dev libportaudio2
sudo apt install -y libopus0 libopus-dev

# 2. Add user to dialout group
sudo usermod -a -G dialout $USER

# 3. Set up project
cd FT710
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. (Optional) Disable onboard audio if it conflicts
# Edit /boot/config.txt: comment out dtparam=audio=on

# 5. Run
FT710_SERIAL_PORT=/dev/ttyUSB0 python server.py
```

**Raspberry Pi notes:**
- FT4222 scope: Not available without cross-compiled FTDI libraries (ARM D2XX
  binaries are not provided by FTDI). Use S-meter fallback for spectrum display.
- PyAudio may need ALSA configuration if the FT-710 is not the default device.
  Check with `arecord -l` and `aplay -l` to confirm the FT-710 is listed.
- CPU usage: On Pi 4, the server (including Opus encoding + S-meter fallback
  spectrum generation) uses ~15-25% CPU. Pi Zero/1 may struggle with audio resampling.

### Windows 11/12 (desktop installer)

The recommended Windows path is the desktop installer. It embeds Python and
does not install a Windows service.

Build on Windows:

```powershell
# 1. Install Python 3.11/3.12 and Inno Setup
# 2. Install project and build dependencies
python -m venv venv
venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller

# 3. Optional but required for true FT4222 spectrum:
#    place FT4222.dll and ftd2xx.dll in vendor\ftdi\windows\bin\x64

# 4. Build app and installer
packaging\windows\build.ps1
```

Install:

```powershell
dist\windows\MRRC-FT710-Setup.exe
```

Runtime configuration:

```text
%LOCALAPPDATA%\MRRC-FT710\ft710.env
```

See `docs/WINDOWS_INSTALLER_GUIDE.md` for the full Windows procedure.

### Windows 10/11 (manual development setup)

```powershell
# 1. Install Python 3.12 from python.org
#    Check "Add Python to PATH" during installation

# 2. Install CP210x driver from Silicon Labs website
#    https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers

# 3. Set up project
cd FT710
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

# 4. Find COM port in Device Manager → Ports (COM & LPT)
#    Look for "Silicon Labs CP210x USB to UART Bridge"

# 5. Run
set FT710_SERIAL_PORT=COM3
python server.py
```

**Windows manual-mode notes:**
- FT4222 true spectrum requires `FT4222.dll` and `ftd2xx.dll` in `vendor\ftdi\windows\bin\x64`, `lib\`, or a directory referenced by `FT710_FTDI_LIB_DIR`.
- The `start.sh`/`stop.sh` scripts don't work on Windows.
- Performance may be slightly lower due to the `asyncio` event loop using
  `ProactorEventLoop` instead of `uvloop` (Linux/macOS only).
- Audio should work with standard PyAudio (bundled PortAudio DLL).


## Audio Device Configuration

### Understanding Radio USB Audio

Both radios present as a **single USB Audio device** with one input and one output:
- **Input (RX):** Audio FROM the radio (what you hear on the radio's speaker)
- **Output (TX):** Audio TO the radio (microphone/modulation input)

The native sample rate depends on the backend:
- **FT-710:** 44,100 Hz, mono, 16-bit. The server resamples to 48,000 Hz for Opus
  encoding and back to 44.1k for TX playback.
- **IC-7300:** 48,000 Hz (stereo input/mono output), 16-bit. No resampling is
  required because Opus already operates at 48 kHz.

### Troubleshooting Audio

**No audio input (RX silent):**
```bash
# Check if the FT-710 is listed as an audio device:
python -c "
import pyaudio
p = pyaudio.PyAudio()
for i in range(p.get_device_count()):
    info = p.get_device_info_by_index(i)
    print(f'[{i}] {info[\"name\"]} in={info[\"maxInputChannels\"]} out={info[\"maxOutputChannels\"]}')
p.terminate()
"
```

**Common issues:**
1. **macOS Privacy:** System Preferences → Privacy → Microphone → allow Terminal/Python
2. **Linux:** The FT-710 may not be the default ALSA device. Use `FT710_AUDIO_RX_DEVICE`
   to specify the correct device.
3. **Raspberry Pi:** Onboard audio (bcm2835) may conflict. Disable in `/boot/config.txt`
   by commenting `dtparam=audio=on`.
4. **Volume/gain check:** The server logs the audio peak level on startup. If below 0.5%,
   check the radio's AF gain setting (it affects the USB audio output level).

**TX audio not modulating:**
1. Check that `start_tx` is completing (server log shows "TX audio started")
2. Verify the FT-710's modulation source is set to USB (radio menu)
3. Check the TX audio queue log messages: if `_tx_stream is None` appears,
   the audio output stream wasn't opened successfully


## FTDI / Scope Troubleshooting

### macOS: D2XX Device Claiming

The most common issue on macOS is that the AppleUSBFTDI kernel driver
claims the FT4222 before the D2XX library can. This produces error 1000
(`FT_DEVICE_NOT_SUPPORTED`) during `FT4222_SPIMaster_Init`.

**Fix:**
1. Verify `ftd2xx.cfg` is at `/usr/local/lib/ftd2xx.cfg` with `DetachKernelDriver=1`
2. Unplug and replug the FT-710 USB cable
3. Confirm the FT4222 is no longer claimed by Apple:
   ```bash
   ioreg -p IOUSB -w0 -l | grep -B2 -A5 "FT4222"
   # Should NOT show "AppleUSBFTDI" as the driver
   ```

### macOS: PortAudio / FT4222 Conflict

In rare cases, opening PyAudio before FT4222 can prevent D2XX from
claiming the device (because CoreAudio probes all USB devices).
The server opens audio and scope in parallel during startup — if scope
SPI init fails on the first attempt, check the startup log for
"reinitializing_device" messages (automatic recovery).

### Linux: Missing ARM FTDI Libraries

FTDI does not provide pre-built ARM Linux `.so` files. Options:
1. **Use S-meter fallback** (set `--no-scope` to skip scope altogether)
2. **Cross-compile from wfview source** (complex; see wfview's CMake toolchain)
3. **Use a x86_64 Linux machine** for full scope support

### All Platforms: S-Meter Fallback

When scope hardware is unavailable, the server auto-generates a synthetic
spectrum from the CAT S-meter reading. This shows a Gaussian-shaped peak
whose height and width scale with signal strength — useful for band activity
monitoring even without FT4222 hardware. Look for:

```
Spectrum broadcast active: S-meter fallback, 1701 bytes/frame, 1 clients
```

vs.

```
Spectrum broadcast active: FT4222, 1701 bytes/frame, 1 clients
```


## Quick Verification Checklist

After installation, run this verification:

```bash
# 1. Python version
python --version  # Should be 3.11+

# 2. Core imports
python -c "import fastapi, uvicorn, serial, websockets, numpy; print('Core OK')"

# 3. Audio
python -c "import pyaudio; p = pyaudio.PyAudio(); print(f'Devices: {p.get_device_count()}'); p.terminate()"

# 4. Opus codec
python -c "from opus_rx import RxOpusEncoder; e = RxOpusEncoder(); print('Opus OK')"

# 5. Serial port
ls -l ${FT710_SERIAL_PORT:-/dev/cu.SLAB_USBtoUART}

# 6. FTDI libraries (macOS only)
ls -l lib/libft4222.dylib lib/libftd2xx.dylib

# 7. D2XX config (macOS only)
grep "DetachKernelDriver" /usr/local/lib/ftd2xx.cfg 2>/dev/null && echo "D2XX OK"

# 8. Server smoke test (starts and exits cleanly)
timeout 5 python server.py 2>&1 | head -20 || true
```


## Dependency Graph

```
FT-710 Server
├── Python 3.11+ (language runtime)
│   ├── fastapi >= 0.100.0 (web framework)
│   │   └── uvicorn[standard] >= 0.23.0 (ASGI server)
│   │       ├── httptools (C HTTP parser)
│   │       ├── uvloop (fast event loop, Linux/macOS only)
│   │       └── websockets >= 12.0 (WS protocol)
│   ├── pyserial >= 3.5 (serial CAT)
│   │   └── [OS] CP210x kernel driver or SiLabs VCP driver
│   ├── pyaudio >= 0.2.11 (audio I/O)
│   │   └── [OS] PortAudio C library
│   │       ├── macOS: CoreAudio (built-in)
│   │       ├── Linux: ALSA (built-in)
│   │       └── Windows: WASAPI / DirectSound (built-in)
│   ├── numpy >= 1.24.0 (resampling + S-meter fallback)
│   └── libopus (optional, via ctypes)
│       ├── macOS: /opt/homebrew/lib/libopus.dylib
│       ├── Linux: libopus.so.0
│       └── Windows: opus.dll
├── FTDI FT4222 Scope (optional, FT-710 only)
│   ├── libftd2xx.dylib/.so/.dll (D2XX driver API)
│   │   ├── macOS: ftd2xx.cfg at /usr/local/lib/ (DetachKernelDriver=1)
│   │   ├── Linux: udev rules for 0403:601c
│   │   └── Windows: FTDI D2XX driver + bundled ftd2xx.dll
│   └── libft4222.dylib/.so/.dll (FT4222 SPI API)
├── IC-7300 CI-V Scope (optional, IC-7300 only)
│   └── Same USB CI-V serial port — no extra drivers
└── [OS] USB Audio Class Driver (built-in all platforms)
    ├── FT-710 USB Audio (44.1 kHz, 1ch in, 1ch out)
    └── IC-7300 USB Audio (48 kHz, 2ch in, 1ch out)
```
