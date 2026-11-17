# 12. Operational Model (ART 0522)

## 12.1 Runtime Topology

```text
Operator/listener/browser/native/terminal
  → HTTPS/HTTP and same-origin WSS/WS (normally port 8888)
  → FastAPI/Uvicorn application and persistent session roles
  → active backend + optional independent SDR/Nano/ATR services

MRRC host
  → python3 server.py, desktop launcher, or systemd image service
  → MRRC_WEB_HOST/MRRC_WEB_PORT (real installations bind all interfaces;
    isolated clean-room tests deliberately use 127.0.0.1)
  → serial/USB, SCU-LAN10, Icom LAN, SunSDR or Flex transport
  → audio device or backend-owned network audio
  → logs, runtime settings/recordings and support artifacts in writable data directories

Exclusive workers (only when their feature is active)
  → FT-710 scope_pipe subprocess
  → spawned FT8 native DSP worker
  → SDRplay sdr_pipe subprocess + native shim/channelizer
```

The application process orchestrates these workers but does not share their device/file descriptors. Radio power, PTT release and configuration-save restart are handled before lifecycle transitions that could skip normal FastAPI shutdown.

## 12.2 Model and Transport Matrix

| Registry entry / family | Control and scope | Audio | Verification/operations |
| --- | --- | --- | --- |
| `ft710` | Serial ASCII CAT 38400; FT4222 SPI subprocess; optional SCU-LAN10 CAT/scope | USB 44.1 kHz or SCU network RX; SCU TX refused | Whole model verified; each media transport still needs its own checks |
| `ic7300`, `ic7300mk2` | USB CI-V 115200, address 0x94/0xB6, 0x27 scope | USB 48 kHz | Verified |
| `ic705`, `ic7610`, `ic7760` | Shared CI-V/profile and scope geometry | USB 48 kHz assumed by profile | Unverified, TX-gated |
| `ftdx10`, `ftdx101d`, `ftdx101mp`, `ftx1`, `ft891` | Shared ASCII CAT; serial models fallback except supported SCU-LAN10 entries | USB/external per profile or SCU network RX | Unverified, TX-gated; FT-891/991A factory CAT 4800, other defaults per profile |
| `ft991a` | Shared ASCII CAT at 4800 default, S-meter fallback | USB 44.1 kHz | Verified 2026-11-01; Vd/Id and some modes/ATU remain named gaps |
| `g7250` | USB CI-V with address discovery; `MRRC_G7250_NET=1` selects RS-BA1 control/CI-V/scope | USB or 16 kHz network RX→48 kHz; network CAT still delegates TX to sound card | Unverified, TX-gated; old raw-IQ path removed |
| `sunsdr2dx` | Native UDP 50001/50002, IQ FFT and telemetry | Native DSP 48 kHz RX/TX | Basic field evidence but `VERIFIED=False`, TX-gated |
| `flex` | SmartSDR TCP 4992 + VITA UDP, discovery or `MRRC_FLEX_HOST`, owned pan/slice FFT | 24 kHz float stereo network RX→48 kHz; 24 kHz Opus TX adapter | FLEX-6600 RX field evidence; `VERIFIED=False`, TX-gated |

`backends.known_models()` is the validation source for server, launcher and box profiles. `model_connection()` chooses serial versus host fields and supplies the correct host attribute for setup/startup logs. Saving a different model writes the selected model's own host value, clears inappropriate serial state and performs read-back before restart.

## 12.3 Configuration

| Name | Default | Purpose |
| --- | --- | --- |
| `MRRC_RADIO_MODEL` | `ft710` | One of the 15 registry keys |
| `MRRC_SERIAL_PORT` | Platform/profile dependent | Serial CAT/CI-V device; empty means unconfigured, not an override with an empty string |
| `MRRC_BAUD_RATE` | Backend default | Explicit baud override; same-model setup saves preserve the stored value |
| `MRRC_WEB_HOST` / `_PORT` | `::` / `8888` | HTTP/WS bind address and port |
| `MRRC_WEB_PASSWORD` | Known development default with loud warning | Operator password; installed systems must replace it |
| `MRRC_LISTEN_PASSWORD` / `MRRC_SDR_PASSWORD` | Empty = role disabled | Independent listen/SDR credentials |
| `MRRC_LOGIN_MAX_ATTEMPTS` / `_WINDOW_S` | `5` / `300` | Per-client-IP login throttle; attempts=0 explicitly disables limiting |
| `MRRC_SSL_CERT` / `_KEY` | First-run generated | TLS certificate/key; `MRRC_SSL=off` is an explicit escape |
| `MRRC_AUDIO_RX_DEVICE` / `_TX_DEVICE` | Auto | PortAudio index/name; explicit loopback choices warn |
| `MRRC_AUDIO_LEVELS_FILE` | Beside memory file | Atomic box RX/TX gain store |
| `MRRC_RECORDINGS_DIR` | Runtime data `recordings/` | MP3 storage |
| `MRRC_RECORDINGS_BITRATE` | `64` | MP3 bitrate ceiling |
| `MRRC_RECORDINGS_MAX_SESSION_MIN` | `240` | Stop a forgotten session; 0 unlimited, never deletes files |
| `MRRC_RECORDINGS_MIN_FREE_MB` | `200` | Refuse new recording below free-space threshold; 0 disables |
| `MRRC_CQ_FILE` | Packaged `static/audio/cq.wav` | CQ asset override; UI upload/reset APIs manage the custom asset |
| `MRRC_G7250_NET` | Off | Move G7250 CAT+scope+audio to Icom LAN together |
| `MRRC_G7250_NET_HOST` / `_USER` / `_PASS` | Empty | RS-BA1 endpoint/credentials; missing key is named on fallback |
| `MRRC_G7250_CIV_ADDR` | Auto | Pin CI-V address instead of 0xA4→0x88→0x70→0xA0 discovery |
| `MRRC_G7250_LAN_IP` | Unused legacy | Read only to warn that the removed raw-IQ route no longer uses it |
| `MRRC_SCU_LAN` | Off | Enable SCU-LAN10 transport for supported Yaesu models |
| `MRRC_SCU_LAN_HOST` / `_USER` / `_PASS` / `_PORT` | Empty / empty / empty / `50000` | LAN unit endpoint/credentials/base port |
| `MRRC_RX_SOURCE` | `both` | SCU dual receive `both`, `main`, `sub`; setting/API share the same value |
| `MRRC_SUNSDR_HOST` / `_LOCAL_HOST` | Required / auto | SunSDR radio and optional local bind address |
| `MRRC_SUNSDR_CONTROL_PORT` / `_IQ_PORT` | `50001` / `50002` | Native UDP ports |
| `MRRC_SUNSDR_SAMPLE_RATE` | `78125` | Device IQ rate selection |
| `MRRC_FLEX_HOST` | Empty = UDP discovery | SmartSDR radio address |
| `MRRC_FLEX_RX_ANT` | Empty | Reapply operator RX antenna after radio reconnect/session reset |
| `MRRC_FLEX_SPECTRUM_FPS` | `15` | Allowed 5/10/15/20/30 emit rate |
| `MRRC_ALLOW_UNVERIFIED_TX` | Off | Global opt-in for eligible unverified backends; backend-specific prohibitions still win |
| `MRRC_REMOTE_SESSION_TX_HEARTBEAT_S` | `0` = off | TX-phase liveness gate for heartbeat-capable sessions |
| `MRRC_PTT_MAX_TX_SECONDS` | `0` = off | Independent maximum continuous TX watchdog |
| `MRRC_ATR1000_HOST` / `_PORT` | Empty / configured default | Optional tuner isolation/endpoint |
| `MRRC_SDR`, `_MAX_CLIENTS`, `_MAX_CHANNELS`, `_SAMPLE_RATE` | On but password-off, 50, 8, 2400000 | Independent SDR enable/capacity/device rate |
| `MRRC_LOG_DIR`, `MRRC_MEM_FILE`, `MRRC_CONFIG_FILE` | Runtime/install dependent | Writable logs, memory store and the env file config writers/systemd must share |

Applicable legacy `FT710_*` aliases remain supported with `MRRC_*` taking precedence. An env file may contain a key only once: parsers and systemd use last-value-wins while writers collapse duplicate hits and verify read-back.

## 12.4 Startup and Common Operations

| Operation | Command / action |
| --- | --- |
| Foreground source run | `MRRC_RADIO_MODEL=ft710 MRRC_SERIAL_PORT=/dev/... python server.py` |
| Icom serial | `MRRC_RADIO_MODEL=ic7300 MRRC_SERIAL_PORT=/dev/... MRRC_BAUD_RATE=115200 python server.py` |
| SunSDR | `MRRC_RADIO_MODEL=sunsdr2dx MRRC_SUNSDR_HOST=radio.local python server.py` |
| Flex | `MRRC_RADIO_MODEL=flex MRRC_FLEX_HOST=radio.local python server.py` (host may be omitted for discovery) |
| Script service | `./start.sh`, `./stop.sh` where the source deployment uses PID-based management |
| Installed service | Platform launcher or systemd unit; do not start a second copy against exclusive devices |
| Change backend | Use connection settings or `mrrc-radio use <model>` on the box; verify saved endpoint and restart |
| Verify HTTP | Authenticated `curl -k https://host:8888/api/health` and `/api/status` |
| Verify scope | Identify real producer vs fallback, then check new-frame cadence/geometry and correct axis |
| Verify RX | Check actual rate/channels, PCM levels, queue/backlog and browser frame arrival |
| Verify TX | Only after TX gate/path acceptance; confirm modulation on a monitor and zero decode/write/queue/non-owner errors |
| Verify release | Normal, disconnect and terminal disconnect return to RX without repeated blocking read-back |
| Verify CQ | Finite call starts/stops, abort unkeys immediately and logs written/drop counters |

## 12.5 Support Chain

1. Deploy the receiver with `./deploy_support_receiver.sh` (dedicated service/storage/nginx path); unauthenticated list/download must fail.
2. Read `diagnostics/summary.txt` first, then `manifest.json`, redacted config/environment and relevant logs. On a box, both the app data log directory and install-dir `logs/server-stdout.log` matter: the latter carries off-loop/thread tracebacks the rotating app log may miss.
3. Bundles include `server.log` and `ft8-worker.log` rotations, source/version and real network/audio/radio facts. Passwords, tokens, private keys, recordings, memories and ATR learning data are excluded by allow-list plus secret-value passes.
4. Upload is operator initiated. Save-local leaves a copy for manual transfer. Newest-five retention applies to generated bundles, not QSO recordings.
5. The external autopilot/receiver/board workflow classifies and publishes answers without becoming a runtime dependency. Board web decisions use Basic Auth plus `X-MRRC-Decision: 1`; the Mac remains board-state authority and acknowledges queue items only after publishing.

## 12.6 Update Channels

**Manifest authority:** `website/downloads/latest.json` is generated from artifact size/SHA and validated by `upgrade_core.py`. Desktop `latest` and `boxLine.version` are independent three-part versions. `boxLine.fixpack` is display-only and never participates in comparison.

**Desktop line:** `GET /api/update/check` fetches/validates/compares and returns installer URL/SHA/size or a nonfatal unavailable reason. `upgrade_core.state.json` defines the proof schema: `installing` is not success; only the new version's boot may write `ok`. The generic desktop download/install slice is not implied merely because box apply exists.

**Box line:** `POST /api/update/apply` selects a full source package or an eligible patch, verifies it and runs `box_update.apply`; `GET /api/update/state` exposes progress/failure. Start is refused during PTT/TUNE/CQ/FT8 TX, insufficient disk or another update. The apply task has a done callback so a crashed background apply becomes visible. Restart releases PTT first. Patches require a compatible `requires` base; otherwise the full package is selected. `mrrc-update` remains the image/service-layer path, including updater self-update, ownership and native FT8 rebuild behavior.

## 12.7 Logs, Metrics and Artifacts

| Artifact | Purpose |
| --- | --- |
| `MRRC_LOG_DIR/server.log` | Canonical rotating application log (2 MB × 2) |
| `MRRC_LOG_DIR/ft8-worker.log[.1]` | Native worker fd 1/2, including Fortran/native death evidence |
| Install-dir `logs/server-stdout.log` | systemd/launcher stdout/stderr; startup and off-loop tracebacks |
| `support-out/`, `support-export/` | Built bundles and operator-saved copies |
| `version.txt` | Version running inside an artifact |
| `mem_channels.json`, audio-level/env/settings stores | Persistent operator/configuration data |
| `GET /api/session_metrics` | Sessions/sockets/peaks and media bytes, no token identifiers |
| `GET /api/perf`, `/Perf` | Delta rates, per-process attribution, thermal/frequency facts and event-loop lag |
| `GET /api/cloud/state` | Actual frpc process/error and configured entry/certificate state |

`MRRC_SESSION_METRICS_INTERVAL_S` controls periodic logs (0 disables only the periodic line), and `_WINDOW_S` controls the sliding peak window. Terminal is one socket kind; its audio/spectrum bytes stay in the corresponding media categories.

## 12.8 Cloud Hub and Remote Help

- Public entry may be a callsign subdomain or a path prefix. Every resource and WS URL remains prefix safe.
- Tokens use cookie/Bearer, not new query parameters.
- Hub deployments enable the TX heartbeat gate and should retain an independent maximum-TX bound. A plaintext relay can make every client appear as 127.0.0.1; login-limit disable is an explicit operational trade-off, not a hidden default.
- Missing tunnel binaries/config never produce a silent green state. `tunnel_error` carries the actionable path/reason.
- Remote help binds SSH to loopback with fresh password-only credentials and a three-hour ceiling. The Hub forwards plain TCP; SSH remains end-to-end. Windows uses PowerShell pipes rather than unavailable POSIX pty modules.
- `_schedule_restart()` releases PTT before `os._exit(42)` and must tolerate missing/degraded controller attributes.

## 12.9 W103D and Raspberry Pi Images

Both image lines install `/opt/mrrc_modern`, a service user with serial/audio permissions, a venv, native dependencies, FTDI/SDR/FT8 components as applicable, log rotation and systemd service layers. `packaging/common/` is the shared authority; only board-specific shell/network/boot details differ.

| Item | W103D | Raspberry Pi |
| --- | --- | --- |
| Base | Pinned ophub Armbian + board firmware/kernel handling | pi-gen stage |
| Boot config | `/boot/mrrc.env` | `/boot/firmware/mrrc.env` |
| Bind default | `0.0.0.0` | `::` |
| Copy exclusions | Shared `stage-tree.sh` authority | Same authority; parity tested |
| Version | Shared box/image line, display FP separate | Same box/image line |

Onboarding is LAN `/setup` + `/settings`; the failed W103D setup hotspot was retired. Wi-Fi profiles explicitly set autoconnect, support hidden/out-of-range preconfiguration and retain fallback DNS. Passwords stay in nmcli argv/local handling, never logs/helper protocol. Bluetooth operations are serialized and report `paired` separately from `keys_work`.

The two service environment paths (`EnvironmentFile` and `MRRC_CONFIG_FILE`) must identify the same file. `mrrc-radio` derives models from the registry, clears stale serial endpoints on model switch and reasserts TX gates. Unverified-TX uplift through settings requires exact confirmation and is applied at the next key-up without restart; backend-specific SCU prohibition remains absolute.

The W103D image has a published independent line; source/fixpack deliveries may be newer than the last flashed image. Use the update card/manifest to distinguish the running source version from the image artifact rather than treating them as one number.
