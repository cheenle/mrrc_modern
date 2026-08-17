# 12. Operational Model (ART 0522)

## 12.1 Runtime Topology

```text
Client Browser
  → http://host:8888 (or https:// with reverse proxy)
  → WS endpoints on same host/port

MRRC Host
  → python3 server.py
  → Uvicorn on 0.0.0.0:8888 (configurable via FT710_WEB_HOST / FT710_WEB_PORT)
  → Backend selected by MRRC_RADIO_MODEL (ft710 / ic7300 / ic7300mk2)
  → FT-710: Serial CAT via USB Enhanced COM Port (configurable via FT710_SERIAL_PORT)
  → IC-7300/MK2: CI-V via USB serial port (configurable via FT710_SERIAL_PORT, 115200 8N1)
  → FT-710 FT4222 SPI: internal FTDI chip (via scope_pipe subprocess)
  → IC-7300/MK2 CI-V 0x27 spectrum: on the same CI-V serial port
  → USB Audio: supported radio USB audio device (auto-detected by PyAudio)
  → PID file: .ft710-server.pid
  → Logs: stdout/stderr (redirected to logs/ by start.sh)

Yaesu FT-710
  → USB connection to host
  → Enhanced COM Port (38400 baud, 8N1)
  → Standard COM Port (115200 baud, for SCU-LAN10 scope models — optional)
  → FT4222 SPI chip (internal)
  → USB Audio interface

Icom IC-7300 / IC-7300MK2
  → USB connection to host
  → USB CI-V serial port (115200 baud, 8N1, default address 0x94)
  → 0x27 spectrum data on the same CI-V port
  → 48kHz native USB Audio interface
```

## 12.2 Configuration

| Name | Default | Purpose |
|------|---------|---------|
| `MRRC_RADIO_MODEL` | `ft710` | Backend selection: `ft710`, `ic7300`, or `ic7300mk2` |
| `IC7300_CIV_ADDR` | `0x94` | IC-7300/MK2 CI-V controller address (hex) |
| `FT710_SERIAL_PORT` | `/dev/cu.SLAB_USBtoUART` | Radio serial port (FT-710 Enhanced COM Port or IC-7300 CI-V port) |
| `FT710_BAUD_RATE` | `38400` | CAT serial baud rate (FT-710 default; IC-7300 uses 115200 regardless) |
| `FT710_WEB_PORT` | `8888` | Uvicorn listen port |
| `FT710_WEB_PASSWORD` | `changeme_please_use_strong_password!` | Web login password |
| `FT710_WEB_HOST` | `::` | Bind address |
| `FT710_FTDI_LIB_DIR` | *(auto)* | Directory containing FTDI libraries |
| `FT710_FT4222_CLK_DIV` | `6` | SPI clock divider (1=fastest, 9=slowest; CLK_DIV_64 default) |
| `FT710_SCOPE_PORT` | *(optional)* | Scope serial port (Standard COM Port, SCU-LAN10) |
| `FT710_SCOPE_BAUD` | `115200` | Scope serial baud rate |
| `FT710_MEM_FILE` | `mem_channels.json` | Memory channel store (Windows launcher uses `%LOCALAPPDATA%`) |
| `FT710_AUDIO_RX_DEVICE` | *(auto)* | Audio input device — index or name substring; Windows package pre-locks `USB Audio` (FT-710 built-in sound card) |
| `FT710_AUDIO_TX_DEVICE` | *(auto)* | Audio output device — index or name substring; Windows package pre-locks `USB Audio` |
| `.ft710-server.pid` | runtime | Process ID for start/stop scripts |

## 12.3 Startup Modes

| Mode | Command | Behavior |
|------|---------|----------|
| Foreground | `python server.py` | Direct console output; Ctrl-C to stop |
| Background | `./start.sh` | Starts in background, logs to `logs/`, writes PID file |
| Stop | `./stop.sh` | Reads PID file, sends SIGTERM, cleans up PID |
| FT-710 mode | `MRRC_RADIO_MODEL=ft710 python server.py` | Default Yaesu FT-710 backend |
| IC-7300 mode | `MRRC_RADIO_MODEL=ic7300 FT710_SERIAL_PORT=/dev/cu.SLAB_USBtoUART python server.py` | Icom IC-7300 backend (CI-V 115200 8N1) |
| IC-7300MK2 mode | `MRRC_RADIO_MODEL=ic7300mk2 FT710_SERIAL_PORT=/dev/cu.SLAB_USBtoUART python server.py` | Icom IC-7300MK2 backend |
| Custom port | `FT710_WEB_PORT=8889 python server.py` | Override listen port |
| Custom serial | `FT710_SERIAL_PORT=/dev/ttyUSB0 python server.py` | Override serial port |
| Custom password | `FT710_WEB_PASSWORD=mysecret python server.py` | Override login password |

## 12.4 Connection Matrix

| Source | Target | Protocol | Port/Path | Description |
|--------|--------|----------|-----------|-------------|
| Browser | Server | HTTP | `$FT710_WEB_PORT` | Static UI |
| Browser | Server | WS | `/WSradio` | Control (JSON) |
| Browser | Server | WS | `/WSaudioRX` | RX audio (binary tagged) |
| Browser | Server | WS | `/WSaudioTX` | TX mic uplink (binary tagged + text) |
| Browser | Server | WS | `/WSspectrum` | Spectrum waterfall (binary) |
| Browser | Server | HTTP | `/api/status` | Full radio state |
| Browser | Server | HTTP | `/api/mem_channels` | Memory channels |
| Browser | Server | HTTP | `/api/auth/login` | Login |
| Browser | Server | HTTP | `/api/auth/logout` | Logout |
| Server | FT-710 | Serial | USB Enhanced COM | CAT commands |
| Server | FT-710 | SPI | Internal FT4222 | Scope data |
| Server | IC-7300/MK2 | Serial | USB CI-V port | CI-V commands (115200 8N1) |
| Server | IC-7300/MK2 | Serial | Same CI-V port | 0x27 spectrum data |
| FT-710 | Server | USB Audio | USB Audio IN | RX audio |
| Server | FT-710 | USB Audio | USB Audio OUT | TX audio |
| IC-7300/MK2 | Server | USB Audio | USB Audio IN | 48kHz RX audio |
| Server | IC-7300/MK2 | USB Audio | USB Audio OUT | 48kHz TX audio |

## 12.5 Operational Procedures

| Procedure | Steps |
|-----------|-------|
| Start service | `./start.sh`; check `logs/` for startup messages |
| Stop service | `./stop.sh` |
| Verify radio connection | Server log shows backend-specific connect message (FT-710 ID or IC-7300 CI-V ID) |
| Verify scope | FT-710: "scope_pipe: first frame received — spectrum active"; IC-7300: CI-V 0x27 frames demuxed |
| Verify RX audio | Open browser; listen for radio audio; check "RX ...K" bitrate indicator |
| Verify TX audio | Key PTT; speak; confirm on monitoring receiver |
| Verify PTT safety | Release PTT; confirm radio returns to RX; check log for backend-specific unkey command |
| Change backend | `MRRC_RADIO_MODEL=ic7300 python server.py` |
| Change IC-7300 CI-V address | `IC7300_CIV_ADDR=0x94 python server.py` |
| Change password | `FT710_WEB_PASSWORD=newpass python server.py` |
| Change serial port | `FT710_SERIAL_PORT=/dev/ttyUSB0 ./start.sh` |
| View server status | `curl http://localhost:8888/api/status` (with auth cookie) |

## 12.6 Logs and Artifacts

| Artifact | Purpose |
|----------|---------|
| `logs/` directory | Server stdout/stderr when background-started |
| `.ft710-server.pid` | Running process PID |
| `mem_channels.json` | Persisted memory channels |
| `config.py` | Protocol-neutral constants + shared UI mode tables |
| `backends/ft710/config_ft710.py` | FT-710-specific mode/band/filter/S-meter calibration tables |
| `backends/ic7300/config_ic7300.py` | IC-7300/MK2-specific mode/band/filter/tables |
| `lib/` | FTDI libraries (libft4222.dylib, libftd2xx.dylib, ftd2xx.cfg) — used only by FT-710 backend |

## 12.7 Operational Risks

| Risk | Mitigation |
|------|------------|
| Wrong serial port | Server logs warning; check `ls /dev/cu.*` or `ls /dev/ttyUSB*` |
| FT4222 not working (FT-710) | Falls back to S-meter synthetic spectrum; check D2XX config |
| CI-V 0x27 not arriving (IC-7300) | Verify `IC7300_CIV_ADDR` matches radio; check CI-V port baud/8N1; falls back to S-meter |
| Audio not working | Check PyAudio device list in logs; verify the selected radio's USB audio appears |
| Port already in use | `./stop.sh` first; check for stale processes |
| Stale JS cached | Service worker bypasses JS/HTML; version query strings |
| Stuck TX | Multiple safety layers (see Ch. 15); server forces RX on WS disconnect |
