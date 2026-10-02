# Windows Desktop Installer Guide

This guide covers the Windows desktop package for MRRC Web Control
(the Yaesu FT-710, the Icom IC-7300/IC-7300MK2 plus preview profiles for
IC-705/IC-7610/IC-7760, and the Yaesu SDR family FTDX10/FTDX101D/FTDX101MP/FTX-1F).
The package is designed for Windows 11 and
Windows 12-class x64 desktop systems. It installs a user-launched desktop app
with an embedded Python runtime; users do not need to install Python manually.

## Download (v1.24.6 Stable)

| File | Size | SHA-256 |
|------|------|---------|
| `MRRC-Modern-v1.24.6-Windows-x64-Setup.exe` | {{WIN_MB}} MB ({{WIN_BYTES}} bytes) | `{{WIN_SHA}}` |

- Fast mirror (recommended in CN): <https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-Setup.exe>
- Versioned mirror: <https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-v1.24.6-Windows-x64-Setup.exe>
- GitHub repository: <https://github.com/cheenle/mrrc_modern>

{{WIN_RELEASE_NARRATIVE}}

**Boundary**: this Windows VM has no physical sound card path (KVM breaks isochronous USB OUT), so
**TX audio still needs acceptance on real Windows hardware**; the VM also had no radio attached during
this run (COM3/COM4 absent), so CAT/audio device behaviour is unverified here.

The earlier v1.24.5 package (54,087,234 bytes, SHA-256 `28ac7743…`) and v1.24.4
(54,076,580 bytes, SHA-256 `12a828f0…`) remain downloadable as archives; v1.24.6 supersedes both.

> **Support note — why this release is 1.24.6 and not a rebuilt 1.24.5.** A machine that installed the
> *first* 1.24.5 build (the one published before the `ssl_bootstrap` import fix) will **never** be
> offered the rebuilt 1.24.5 package: the upgrade channel compares **version strings**, and they are
> equal. Such a machine keeps showing the black screen until it is given a *higher* version — which is
> exactly what v1.24.6 is. When a user reports "I already installed the fixed build and it is still
> black", check `version.txt` **and** the install timestamp, not just the version number.

**What's new in v1.24.6**: **the launcher asks the server which address actually answers, instead of
guessing.** A field machine (hostname `MRRC`) that had installed 1.24.5 still showed a black screen; the
forensics — its own `server.log` plus running the packaged `MRRC-Modern-Server.exe` over SSH — showed the
app was perfectly healthy on plain HTTP (`/login` → **200**, 1726 bytes) while the browser had been sent
to `https://`, i.e. a protocol error rendered as a blank page. Four independent causes were fixed:
① the launcher used to derive the scheme from *its own* ability to produce a certificate while the server
derived TLS from *its own* ability to load one — two decisions, one socket. Both launchers now probe
`/api/health` (new shared `launcher_net.py`; any HTTP answer counts, including `401`) and open whichever
scheme responds, **saying so out loud** when they switch. ② On Windows `SO_REUSEADDR` does not mean what
it means on POSIX: it lets a second process bind a port that is *already listening*, so two servers could
both print `Server ready!` and the browser might land on the one holding no radio (the field log showed
every line duplicated 1–10 ms apart — two stacks each enumerating audio devices and opening the CAT port).
Windows now binds with `SO_EXCLUSIVEADDRUSE`, retries `WSAEADDRINUSE` for ~3 s, then fails with a message
naming the port and "another MRRC Modern is already running"; the launcher probes *before* spawning and
reuses an instance that is already up. ③ Starting the Server directly (the Start Menu has a shortcut for
it) used to aim its writable defaults at the read-only install directory:
`Permission denied: 'C:\Program Files\MRRC Modern\mrrc_modern.env.tmp'` meant **a new password every
start that was never saved** — the real cause of "I installed the new package and still cannot log in" —
and `Recording disabled: …\recordings is not writable`. `_writable_runtime_dir()` now falls back to the
per-user data directory (`LOG_DIR` and `certs` already did). ④ A bare start now reads the user's
`mrrc_modern.env` (`config.load_user_config_into_environ()`, only when frozen or when `MRRC_CONFIG_FILE`
is explicit, `setdefault` only, so real environment variables still win) — previously the server bound `::`
while the user's file said `MRRC_WEB_HOST=127.0.0.1`. Also: the rotating file handler is attached
idempotently (the frozen package loads this module twice, as `__main__` and as `server`, which is the
other half of those duplicated log lines), and a new stdlib-AST gate
(`tests/test_undefined_app_module_names.py`) fails the build when an app module is used as `name.attr`
without ever being imported — the class of defect that shipped twice (`cloud_hub` in v1.24.1,
`ssl_bootstrap` in v1.24.5).

**What's new in v1.24.5**: **no certificate means the app signs one, instead of quietly serving
plain HTTP.** Two build-machine-only defaults combined into a dead UI on a real machine: the
certificate pointed into the (read-only) install directory under a developer-machine filename that
exists on nobody's computer, and a missing certificate degraded the server to plain HTTP while the
launcher opened an **https://** URL — the browser showed a protocol error and the window stayed
black. The certificate now lives in the user's own writable data directory
(`%LOCALAPPDATA%\MRRC-Modern\certs`), is generated on first run, and plain HTTP is left only to an
explicit `--no-ssl`. A certificate that appears *after* start-up (what re-enrolling with the Cloud
Hub produces) is detected by file identity — path + timestamp + size — and prompts the restart that
actually enables it. Also new: a cleanup script that removes every generation of leftovers, and the
server-side signing path itself now works when the Server is started without the launcher.

**What's new in v1.20.0**: the **listen-only interface `/listen`** — an operator can hand a visitor a
second, *separate* password (`MRRC_LISTEN_PASSWORD`, empty = disabled) that unlocks a page with a large
frequency readout, direct frequency/band entry, mode buttons, memory recall, S-meter, waterfall and RX
audio with browser-side volume. The restriction is enforced **server-side** (a role gate on `/WSradio`
that only lets `freq`/`mode` sets and `memRecall` through, `4003` close on `/WSaudioTX` and `/WSatr1000`,
and read-only REST), so it cannot be bypassed by the page. This release also fixes the iPhone
main-UI auto-lock (the Wake Lock request now fires from a real user gesture) and activates the iOS
audio session at power-on so RX is audible without pressing PTT first.

**What's new in v1.16.0**: the **Yaesu SDR family** — FTDX10 / FTDX101D /
FTDX101MP / FTX-1F — behind one profile-driven ASCII-CAT core ported from the
verified FT-710 path (AD-018). These four profiles are **experimental and
receive-only**: transmit is refused until you check the radio and set
`MRRC_ALLOW_UNVERIFIED_TX=1`, and they have no real spectrum source, so the UI
shows the S-meter synthesised spectrum. The same release makes the release
itself checkable (`release-artifacts.json` + `release_check.py`, enforced by
the test suite).

**v1.15.0 brought**: QSO recording moved to the server (device-domain RX
PCM + decoded mic PCM on one monotonic 16 kHz timeline, incremental `lameenc`
MP3 written while recording, crash-safe), a new **Recordings panel** (list,
seekable player — the server answers Range requests — download, delete), and a
CAT recovery fix (a reconnect no longer runs a 24-query state sweep that used
to hold the serial lock for ~60 s). The browser-side recorder and its 530 KB
`lame.js` encoder are gone. See `CHANGELOG.md` for the full list; CAT protocol
and PTT behaviour are unchanged for the FT-710/IC-7300 path since v1.14.2 (this release adds model profiles, it does not alter the existing ones).

Browser capture and Opus remain at 48 kHz. Every decoded 960-sample TX frame is
converted to 882 samples before the FT-710 playback device is opened/written at
16-bit/44.1 kHz, including after PortAudio reinitialization. A WASAPI endpoint
advertising a 48 kHz shared-mode mix rate does not change the radio device
boundary. TX-session logs expose `queue_drops`; a healthy physical acceptance
run requires `decode_fail=0`, `write_err=0`, `queue_drops=0`, and
`non_owner_drops=0`. The release VM did not provide an authoritative physical
RF speech/noise path, so over-the-air monitoring remains an operator check.

## User Installation

> **HTTPS by default (v1.7.6+)**: on first launch the app generates a
> throwaway self-signed certificate (`%LOCALAPPDATA%\MRRC-Modern\certs\`)
> and starts on HTTPS — the browser warns "untrusted" once; accept it
> (Chrome/Edge: Advanced → Proceed; Safari: Show Details → Visit). HTTPS
> is required for audio when you open the UI from another device
> (phone/tablet): plain HTTP on a LAN address disables AudioWorklet and
> the microphone in the browser. To use your own certificate set
> `MRRC_SSL_CERT`/`MRRC_SSL_KEY` in `mrrc_modern.env`; to go back to plain
> HTTP set `MRRC_SSL=off`.

### 1. Install required device drivers

Install these before launching the app:

- **FT-710**: Silicon Labs CP210x Universal Windows Driver for the Enhanced COM Port.
  FTDI D2XX driver only if you want FT4222 true spectrum.
- **IC-7300 / IC-7300MK2**: Standard Windows USB-serial driver for the CI-V port
  (usually installed automatically). No FTDI drivers are required.

After connecting the radio USB cable, open Device Manager and check:

- **FT-710**: Ports (COM & LPT) shows two Silicon Labs CP210x COM ports.
  The lower-numbered CP210x COM port is typically the Enhanced COM Port for CAT.
- **IC-7300**: Ports (COM & LPT) shows one COM port for USB CI-V.
- USB audio devices include the radio audio input and output.

### 2. Install MRRC Modern

Run:

```text
MRRC-Modern-Setup.exe
```

The installer creates:

- Start Menu shortcut: `MRRC Modern`
- Optional desktop shortcut
- Start Menu shortcut: `Edit Configuration`

### 3. Edit configuration

Use the Start Menu `Edit Configuration` shortcut, or open:

```text
%LOCALAPPDATA%\MRRC-Modern\mrrc_modern.env
```

The launcher copies `windows/default.env` to `%LOCALAPPDATA%\MRRC-Modern\mrrc_modern.env`
on first run. This is the file you edit. Variable names use the `MRRC_*`
prefix; the legacy `FT710_*` prefixes are still accepted for backward
compatibility (`MRRC_*` wins when both are set), so existing configs keep
working — but new configs should use `MRRC_*`.

Set `MRRC_RADIO_MODEL` to `ft710`, `ic7300`, or `ic7300mk2`. The two radio
families need different serial ports, baud rates, drivers, and (for FT-710
true spectrum) FTDI libraries. The complete per-radio configurations are:

**FT-710:**

```ini
MRRC_RADIO_MODEL=ft710
MRRC_SERIAL_PORT=COM3          # Enhanced COM Port (lower of the two CP210x ports)
MRRC_BAUD_RATE=38400
MRRC_WEB_HOST=127.0.0.1
MRRC_WEB_PORT=8888
MRRC_WEB_PASSWORD=change_this_password
MRRC_SCOPE_PORT=               # leave empty to auto-detect the FT4222 port
MRRC_SCOPE_BAUD=115200
MRRC_AUDIO_RX_DEVICE=USB Audio
MRRC_AUDIO_TX_DEVICE=USB Audio
MRRC_FTDI_LIB_DIR=vendor\ftdi\windows\bin\x64
#MRRC_ATR1000_HOST=
#MRRC_ATR1000_PORT=60001
```

**IC-7300 / IC-7300MK2:**

```ini
MRRC_RADIO_MODEL=ic7300        # or ic7300mk2
MRRC_SERIAL_PORT=COM5          # USB CI-V COM port from Device Manager
MRRC_BAUD_RATE=115200
MRRC_WEB_HOST=127.0.0.1
MRRC_WEB_PORT=8888
MRRC_WEB_PASSWORD=change_this_password
MRRC_AUDIO_RX_DEVICE=USB Audio
MRRC_AUDIO_TX_DEVICE=USB Audio
# No FTDI libraries required — comment out / omit MRRC_FTDI_LIB_DIR.
# If the radio's CI-V address is not the default 0x94:
#IC7300_CIV_ADDR=0x94
```

Key differences:

- **IC-7300**: standard Windows USB-serial driver (auto-installed); **no FTDI
  driver or DLLs**; CAT is CI-V over a single COM port at **115200** baud.
  Make sure the radio's `CI-V USB Baud Rate` menu matches. Spectrum comes from
  CI-V `0x27` frames over the CAT port.
- **FT-710**: Silicon Labs CP210x driver required; FTDI D2XX only for FT4222
  true spectrum. Two COM ports — use the **lower-numbered** one (Enhanced COM
  Port) for CAT. Set `MOD SOURCE = USB` per mode in the radio menu for TX audio.

Change `MRRC_WEB_PASSWORD` before exposing the app beyond localhost.

## Audio (RX/TX) Setup

The server opens the radio's **built-in USB sound card** for both receive
(RX) and transmit (TX) audio. On Windows this card enumerates under a
**generic name — `USB Audio CODEC` or `USB Audio Device`**, depending on the
driver/OS build — it does *not* always contain "FT-710" or "YAESU", which is
why auto-detection can pick the wrong device (laptop mic for RX, PC speakers
for TX). Lock the device explicitly as follows.

- **FT-710**: native 44.1 kHz. The server converts 960 samples at 48 kHz to
  882 samples at 44.1 kHz before queueing PyAudio.
- **IC-7300**: native 48 kHz. No resampling is required.

For FT-710, the selected Windows output entry is opened at 44.1 kHz even if
its displayed WASAPI default rate is 48 kHz.

### 1. Identify the device

- Windows Settings → System → Sound (or Device Manager → Sound, video and
  game controllers): the FT-710 appears as `USB Audio CODEC` or `USB Audio
  Device` (recording: `Microphone (...)`, playback: `Speakers (...)`).
  On localized Windows the name is wrapped, e.g. `麦克风 (USB Audio Device)`.
- Every startup, the launcher console prints the full PortAudio device list
  (`PyAudio initialized. Available devices:`) with indices, channel counts,
  and sample rates. The same physical device usually appears once per host
  API (MME / DirectSound / WASAPI); any entry opens the same hardware.

### 2. Lock the device in `mrrc_modern.env`

Edit `%LOCALAPPDATA%\MRRC-Modern\mrrc_modern.env` (Start Menu → `Edit
Configuration`):

```ini
MRRC_AUDIO_RX_DEVICE=USB Audio
MRRC_AUDIO_TX_DEVICE=USB Audio
```

- `USB Audio` is the common substring of both enumeration forms
  (`USB Audio CODEC` / `USB Audio Device`), so one value covers every
  driver variant.
- A **name substring** is stable across reboots; a numeric **index** is not
  (it can shift when devices are added/removed).
- New packages ship these two lines pre-filled in `windows/default.env`.
- If the PC has **more than one** matching USB audio device (e.g. an
  external digimode interface), set the **index** from the startup device
  list instead, e.g. `MRRC_AUDIO_RX_DEVICE=4`.

### 3. Windows sound settings

- Do **not** set the radio's USB audio playback device as the Windows
  **default playback device** — otherwise system and browser sounds would
  modulate the transmitter whenever PTT is keyed. Keep the PC speakers as
  default.
- Optional, avoids OS resampling: Sound control panel → Recording and
  Playback → the radio's USB audio device → Properties → Advanced → set
  both to **16 bit, 44100 Hz (CD Quality)**, and disable audio enhancements.
- RX loudness: Recording → the radio's USB audio device → Levels.

### 4. FT-710 menu settings

RX audio needs no menu change — the receiver AF is always present on the
USB audio device.

TX modulation source is configured **per mode** (FT-710 Operation Manual,
`FUNC` → `RADIO SETTING`):

| Menu | Setting | Value |
| ------ | --------- | ------- |
| `RADIO SETTING` → `MODE SSB` | `MOD SOURCE` | **`USB`** |
| `RADIO SETTING` → `MODE AM` | `MOD SOURCE` | `USB` (if AM is used) |
| `RADIO SETTING` → `MODE FM` | `MOD SOURCE` | `USB` (if FM is used) |
| `RADIO SETTING` → `MODE PSK/DATA` | `MOD SOURCE` | `USB` (for DATA-U / DATA-L / PSK) |

- `MOD SOURCE` choices: `MIC` (front-panel mic) / `USB` (rear USB jack) /
  `REAR` (RTTY/DATA jack) / `AUTO`. The factory default `AUTO` selects the
  modulation input "automatically according to the transmission method" —
  if PTT keys the radio but there is **no modulation**, set `USB`
  explicitly.
- Leave `RPTT SELECT` = `OFF` everywhere: PTT is keyed by CAT command on the
  Enhanced COM Port, not by RTS/DTR.
- TX audio level: use the 🎙 Vol slider in the web UI; keep ALC out of the
  red zone.

### 5. Verify

1. Launcher console shows
   `RX audio started: [n] ... (USB Audio ...) @ 44100 Hz`.
2. RX: the browser plays band noise that follows the radio's AF gain.
3. TX: the first PTT shows `TX audio started: [n] ... @ 44100 Hz`.
4. TX: hold PTT and speak — the PO/ALC meter on the radio (and in the web
   UI) moves; confirm on a monitoring receiver.

### 4. Launch

Start `MRRC Modern` from the Start Menu or desktop shortcut. The launcher:

1. Reads `%LOCALAPPDATA%\MRRC-Modern\mrrc_modern.env`.
2. Starts the bundled server and waits for it to answer HTTP before opening
   `http://localhost:8888` in the default browser (up to ~15 seconds on the
   first run).
3. Use Ctrl-C in the launcher window for a graceful stop (audio drains and
   PTT releases first).

**Warning:** closing the launcher window with × kills both processes
*abruptly* — there is no graceful cleanup. If the radio is transmitting, it
can stay keyed. Always release PTT before closing the window.

## FT4222 True Spectrum (FT-710 only)

The Windows package supports FT4222 true spectrum for the FT-710 when these
runtime files are present:

```text
vendor\ftdi\windows\bin\x64\FT4222.dll
vendor\ftdi\windows\bin\x64\ftd2xx.dll
```

If either DLL is missing, the app still runs and uses the S-meter fallback
spectrum. The fallback is useful for basic activity visualization but does not
provide true FFT waterfall data.

## Opus Runtime

TX/RX compressed audio needs a Windows `opus.dll`. The package searches:

```text
opus.dll
_internal\opus.dll
vendor\opus\windows\bin\x64\opus.dll
```

If `TX Opus decoder unavailable: libopus not found` appears, browser TX audio
may be silent or fall back depending on the client path. Put `opus.dll` in
`vendor\opus\windows\bin\x64` before building a release package.

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
- FTDI DLLs in `vendor\ftdi\windows\bin\x64` for FT-710 FT4222 support (not required for IC-7300)

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
dist\windows\MRRC-Modern\
dist\windows\MRRC-Modern-Setup.exe
```

The build script runs syntax checks and the test suite before packaging.

## Build Components

| File | Purpose |
| ------ | --------- |
| `windows\launcher.py` | Desktop launcher; starts/stops the server and opens the browser |
| `windows\default.env` | Initial user configuration template |
| `packaging\pyinstaller\mrrc_modern_server.spec` | Bundles the FastAPI server, both radio backends, and the static UI |
| `packaging\pyinstaller\scope_pipe.spec` | Bundles the FT-710 FT4222 scope worker |
| `packaging\pyinstaller\mrrc_modern_launcher.spec` | Bundles the desktop launcher |
| `packaging\windows\MRRC-Modern.iss` | Inno Setup installer definition |
| `packaging\windows\build.ps1` | End-to-end Windows build script |

## Verification Checklist

After installing on Windows:

1. Launch `MRRC Modern`.
2. Confirm the browser opens the login page.
3. Log in with `MRRC_WEB_PASSWORD`.
4. Confirm frequency, mode, and S-meter update from the radio.
5. Confirm RX audio works.
6. Confirm TX audio reaches the radio only when PTT is active.
7. Open a spectrum client and check logs for `scope_pipe: first frame received` (FT-710) or CI-V scope frame reception (IC-7300).
8. Temporarily remove one FTDI DLL (FT-710 only) and confirm the app falls back to S-meter spectrum instead of crashing.

## Troubleshooting

| Symptom | Likely Cause | Fix |
| --------- | -------------- | ----- |
| `Failed to connect to COM3: could not open port 'COM3': FileNotFoundError` | Default `COM3` does not exist on this Windows machine, or the CP210x driver is not installed | Install the Silicon Labs CP210x driver, reconnect the radio, then set `MRRC_SERIAL_PORT=COMx` to the Enhanced COM Port shown in Device Manager |
| Browser opens but radio state does not update | Wrong COM port | Set `MRRC_SERIAL_PORT` to the Enhanced COM Port |
| `Server did not answer within 15s` while Uvicorn says `http://[::]:8888` | Older launcher probed IPv4 loopback while the server was listening on IPv6 wildcard | Open `http://localhost:8888`, or update to a package with the launcher fix |
| `TX Opus decoder unavailable: libopus not found` | Missing Windows `opus.dll` | Add `vendor\opus\windows\bin\x64\opus.dll` before building, or install/copy `opus.dll` next to the app |
| App starts but FT4222 spectrum is unavailable | Missing `FT4222.dll` or `ftd2xx.dll` (or not using FT-710) | Place both DLLs in `vendor\ftdi\windows\bin\x64` before building; IC-7300 uses CI-V `0x27` and does not need FTDI |
| Login fails | Wrong password | Check `%LOCALAPPDATA%\MRRC-Modern\mrrc_modern.env` |
| Audio device not found | Windows selected another audio device | Set `MRRC_AUDIO_RX_DEVICE` / `MRRC_AUDIO_TX_DEVICE` by name or index (see *Audio (RX/TX) Setup*) |
| No RX audio, or RX sounds like room noise | Auto-detect picked the laptop mic instead of the FT-710's USB sound card | Lock `MRRC_AUDIO_RX_DEVICE=USB Audio` (or the index from the startup device list) |
| PTT keys but TX audio plays through the PC speakers | Auto-detect picked the wrong output device | Lock `MRRC_AUDIO_TX_DEVICE=USB Audio` (or the index) |
| PTT keys, correct device, but no RF modulation | Radio menu `MOD SOURCE` is `MIC` | Set `FUNC` → `RADIO SETTING` → `MODE SSB` → `MOD SOURCE` = `USB` (see *Audio (RX/TX) Setup*) |
| Windows/browser sounds are heard on the air during TX | The radio's USB audio device is the Windows default playback device | Set the PC speakers as the Windows default output |
| Port 8888 already in use | Another local service is listening | Change `MRRC_WEB_PORT` |
