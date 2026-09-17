# 12. Operational Model (ART 0522)

## 12.1 Runtime Topology

```text
Client Browser
  → http://host:8888 (or https:// with reverse proxy)
  → WS endpoints on same host/port

MRRC Host
  → python3 server.py
  → Uvicorn on 0.0.0.0:8888 (configurable via MRRC_WEB_HOST / MRRC_WEB_PORT)
  → Backend selected by MRRC_RADIO_MODEL (ft710 / ic7300 / ic7300mk2)
  → FT-710: Serial CAT via USB Enhanced COM Port (configurable via MRRC_SERIAL_PORT)
  → IC-7300/MK2: CI-V via USB serial port (configurable via MRRC_SERIAL_PORT, backend default 115200 8N1)
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
  → Radio menu: CI-V USB Port = Unlink from [REMOTE]
  → USB CI-V serial port (explicit 115200 baud, not Auto; IC-7300 default 0x94, MK2 default 0xB6)
  → 0x27 spectrum data on the same CI-V port (display + data-output switches ON)
  → 48kHz native USB Audio interface
```

```text
Yaesu FTDX10 / FTDX101D / FTDX101MP / FTX-1F (experimental)
  → USB connection to host
  → USB serial bridge (same ASCII-CAT protocol as the FT-710; 38400 8N1)
  → No scope interface: the Yaesu scope waveform is undocumented, so the
    server keeps broadcasting the S-meter synthesiser instead
  → USB Audio interface (sample rate per profile, family default 44.1kHz,
    TODO(hw-verify))
```

**First connection is receive-only for an unverified model** (IC-705/IC-7610/
IC-7760 and all four Yaesu models): the startup log reports
`Radio model <key> is NOT hardware-verified — transmit is DISABLED`, the
connection dialog labels the model 实验性/仅接收, and `set_ptt(True)`/
`set_tune(True)` are refused until the operator sets
`MRRC_ALLOW_UNVERIFIED_TX=1` and restarts. PTT **releases are never gated**,
and the model identity check (`ID;`, read-only) only logs.


## 12.2 Configuration

| Name | Default | Purpose |
|------|---------|---------|
| `MRRC_RADIO_MODEL` | `ft710` | Backend selection (registry-validated): `ft710`, `ic7300`, `ic7300mk2`, `ic705`, `ic7610`, `ic7760`, `ftdx10`, `ftdx101d`, `ftdx101mp`, `ftx1` |
| `IC7300_CIV_ADDR` | `0x94` | IC-7300 CI-V radio address (hex) |
| `IC7300MK2_CIV_ADDR` | `0xB6` | IC-7300MK2 CI-V radio address (hex) |
| `MRRC_SERIAL_PORT` | `/dev/cu.SLAB_USBtoUART` | Radio serial port (FT-710 Enhanced COM Port or IC-7300 CI-V port) |
| `MRRC_BAUD_RATE` | backend default | CAT/CI-V baud: FT-710 and the four Yaesu models `38400`; Icom models `115200`; an explicit value overrides the backend default |
| `MRRC_WEB_PORT` | `8888` | Uvicorn listen port |
| `MRRC_WEB_PASSWORD` | `changeme_please_use_strong_password!` | Web login password |
| `MRRC_WEB_HOST` | `::` | Bind address |
| `MRRC_FTDI_LIB_DIR` | *(auto)* | Directory containing FTDI libraries |
| `MRRC_FT4222_CLK_DIV` | `6` | SPI clock divider (1=fastest, 9=slowest; CLK_DIV_64 default) |
| `MRRC_SCOPE_PORT` | *(optional)* | Scope serial port (Standard COM Port, SCU-LAN10) |
| `MRRC_SCOPE_BAUD` | `115200` | Scope serial baud rate |
| `MRRC_MEM_FILE` | `mem_channels.json` | Memory channel store (Windows launcher uses `%LOCALAPPDATA%`) |
| `MRRC_AUDIO_RX_DEVICE` | *(auto)* | Audio input device — index or name substring; Windows package pre-locks `USB Audio` (FT-710 built-in sound card) |
| `MRRC_AUDIO_TX_DEVICE` | *(auto)* | Audio output device — index or name substring; Windows package pre-locks `USB Audio` |
| `.ft710-server.pid` | runtime | Process ID for start/stop scripts |

## 12.3 Startup Modes

| Mode | Command | Behavior |
|------|---------|----------|
| Foreground | `python server.py` | Direct console output; Ctrl-C to stop |
| Background | `./start.sh` | Starts in background, logs to `logs/`, writes PID file |
| Stop | `./stop.sh` | Reads PID file, sends SIGTERM, cleans up PID |
| FT-710 mode | `MRRC_RADIO_MODEL=ft710 python server.py` | Default Yaesu FT-710 backend |
| IC-7300 mode | `MRRC_RADIO_MODEL=ic7300 MRRC_SERIAL_PORT=/dev/cu.usbserial-... MRRC_BAUD_RATE=115200 python server.py` | Icom IC-7300 backend (CI-V 115200 8N1) |
| IC-7300MK2 mode | `MRRC_RADIO_MODEL=ic7300mk2 MRRC_SERIAL_PORT=/dev/cu.usbserial-... MRRC_BAUD_RATE=115200 python server.py` | Icom IC-7300MK2 backend (default CI-V address 0xB6) |
| Custom port | `MRRC_WEB_PORT=8889 python server.py` | Override listen port |
| Custom serial | `MRRC_SERIAL_PORT=/dev/ttyUSB0 python server.py` | Override serial port |
| Custom password | `MRRC_WEB_PASSWORD=mysecret python server.py` | Override login password |

## 12.4 Connection Matrix

| Source | Target | Protocol | Port/Path | Description |
|--------|--------|----------|-----------|-------------|
| Browser | Server | HTTP | `$MRRC_WEB_PORT` | Static UI |
| Browser | Server | WS | `/WSradio` | Control (JSON) |
| Browser | Server | WS | `/WSaudioRX` | RX audio (binary tagged) |
| Browser | Server | WS | `/WSaudioTX` | TX mic uplink (binary tagged + text) |
| Browser | Server | WS | `/WSspectrum` | Spectrum waterfall (binary) |
| Browser | Server | WS | `/WSradio` | `recording` set command + `recordingState` broadcasts (recording control rides the control channel) |
| Browser | Server | HTTP | `/api/recordings` | Recording list + total usage |
| Browser | Server | HTTP | `/api/recordings/{name}` | Recording stream (Range → seeking; inline player + download) |
| Browser | Server | HTTP | `/api/recordings/{name}` (DELETE) | Delete a recording (409 while it is being recorded) |
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
| Verify scope | FT-710: "scope_pipe: first frame received — spectrum active"; IC-7300: first confirm Unlink from [REMOTE] + explicit 115200, then check "CI-V scope: first complete waveform — spectrum active"; real frames broadcast at up to 30 Hz without duplicates |
| Verify RX audio | Open browser; listen for radio audio; check "RX ...K" and startup log fields `host`, `default`, `actual`, `channels` (IC-7300/MK2: `actual=48000Hz`) |
| Verify TX audio | Key PTT; speak; confirm on monitoring receiver and check TX open log (`actual=48000Hz` on IC-7300/MK2) |
| Verify PTT safety | Release PTT; confirm radio returns to RX; check log for backend-specific unkey command |
| Verify CQ key | Press CQ: the button shows STOP, `cqState` broadcasts `calling` with `frames_sent` rising at 50/s, and the radio keys for the length of the recording (~6 s) then drops back to RX. Log: `CQ: keyed the radio` … `CQ complete after 307/307 frames` … `CQ session: written=… write_err=0 queue_drops=0`. Pressing CQ again mid-call shows `aborted`/`aborted_by_user` and unkeys immediately. |
| Replace the CQ recording | Put your own 16-bit WAV anywhere readable and start with `MRRC_CQ_FILE=/path/to/cq.wav python server.py` (mono or stereo, any sample rate; normalised to 48 kHz mono once at startup). Check the startup line `CQ ready: <path> (…)`; if the file is missing, corrupt or longer than 30 s the log says `CQ key disabled: <reason>` and the button stays inert. |
| Change backend | `MRRC_RADIO_MODEL=ic7300 python server.py` |
| Change IC-7300 CI-V address | `IC7300_CIV_ADDR=0x94 python server.py` |
| Change password | `MRRC_WEB_PASSWORD=newpass python server.py` |
| Change serial port | `MRRC_SERIAL_PORT=/dev/ttyUSB0 ./start.sh` |
| View server status | `curl http://localhost:8888/api/status` (with auth cookie) |

## 12.5.1 支持链路（诊断包与接收端）

1. **部署接收端**（维护者，幂等）：`./deploy_support_receiver.sh`。它会创建 systemd unit
   `support-receiver-modern`（端口 8098）、存储目录 `/var/www/support-modern`（`www-data`，**不在 docroot**）、
   口令文件 `/etc/mrrc-modern-support.env`（0600 root）、nginx 路径 `/mrrc_modern/support/`，
   并打印两条自检：本地与公网的 `/api/list` 无口令时必须 **401**。
2. **读一个诊断包**：先看 `diagnostics/summary.txt`（启动次数 vs `Traceback`、数据新鲜度、
   音频/串口/频谱事实、TX 门禁、录音写盘告警），再按需看 `logs/`、`state/config-redacted.env`、
   `diagnostics/env.json`。`manifest.json` 记有每个文件的 sha256、脱敏计数与告警。
3. **收包**：`https://www.vlsc.net/mrrc_modern/support/api/list`（Basic 口令见维护者的口令文件），
   编号形如 `20260917-072530-ab12`。
4. **排查未复现的问题**：包内 `warnings` 里"日志可能过旧"意味着这不是现场；`summary.txt` 会显式写出。
5. **答复怎么产生**：`dev_tools/support_autopilot.py`（crontab 每 10 分钟，`--once --publish`）轮询接收端 → 解包 →
   `pi` 只读分析（提示词带探查预算：≤5 次检索 / ≤6 个文件）→ 按 JSON 契约渲染答复卡 →
   仅当 `status` 为 `answered`/`needs_fix` 时更新 `website/answers/index.html`、commit 并 rsync 单文件上线。
   人工预检用 `--inspect <编号>`（不调模型）；`--status` 看已处理清单；`--force <编号>` 重跑。
   草稿永远留在 `dist/support_answers/`，即使发布失败也不丢结论。

## 12.6 Logs and Artifacts

| Artifact | Purpose |
|----------|---------|
| `logs/` directory | Server stdout/stderr when background-started |
| `MRRC_LOG_DIR/server.log` | Rotating server log (2 MB × 2, UTF-8): the canonical log, written in every launch mode. Default `MRRC_LOG_DIR` = the install dir's `logs/`; the desktop launchers point it at the user data directory (Program Files and /Applications are not writable) |
| `MRRC_LOG_DIR/server-stdout.log` | Launcher tee of the child's stdout/stderr, **rebuilt at each launch and closed once the server answers HTTP**: the only witness of a server that dies before its own logging exists. It keeps draining afterwards so a chatty server can never block on a full pipe. On systemd (`install.sh`) and launchd the same name is used for the process redirect — never `server.log`, which the in-process handler owns |
| `support-out/` | Built diagnostics bundles, newest 5 (`MRRC_LOG_DIR`'s sibling); `support-export/` holds the copies made by 「只保存到本地」 |
| `version.txt` | App version shipped inside each artifact (macOS `Contents/MacOS/`, Windows install dir, rpi64 `/opt/mrrc_modern/`); the bundle manifest and the future one-click upgrade read it |
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
| CI-V 0x27 not arriving (IC-7300) | Verify `CI-V USB Port = Unlink from [REMOTE]`, explicit 115200 (not Auto), and the model-specific CI-V address; run `_diag_ic7300_scope.py`; initialization enables both `27 10` display and `27 11` data output; the bounded queue drops oldest data rather than replaying stale frames |
| Audio not working | Check PyAudio device list in logs; verify the selected radio's USB audio appears |
| Port already in use | `./stop.sh` first; check for stale processes |
| Stale JS cached | Service worker bypasses JS/HTML; version query strings |
| Stuck TX | Multiple safety layers (see Ch. 15); server forces RX on WS disconnect |
