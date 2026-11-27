<p align="right">
  <a href="passport_flash.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Flashing a Passport from the box's web page

A freshly shipped MRRC-Passport knows nothing: the customer has to join the
hotspot it raises (iOS calls it "no internet"), then type the Wi-Fi name, the
Wi-Fi password, the box address and the box password on a phone keypad.

The box already knows all of that, and it is the only machine on site that has a
USB port and runs all day. So: plug the terminal into the box, open
`/passport`, click once — firmware and network configuration are written
together.

This page is the operator-facing guide. The design record is
`docs/superpowers/specs/2026-11-25-passport-flash-design.md`.

---

## 0. What it actually does

Five steps, in this order (`passport_flash.plan_flash()`):

| # | Step | What it means |
| --- | --- | --- |
| 1 | `chip id` | Identify the chip. A board that is not ESP32-C3 is refused **before anything is written** |
| 2 | `firmware <version>` | Write the merged image to `0x0` (bootloader + partition table + app), with `--verify` |
| 3 | `wifi config` | Write the NVS partition to `0x9000` (Wi-Fi SSID/password, server host, server password), with `--verify` |
| 4 | `read back` | Read `0x9000` back and compare it with the intended config |
| 5 | `reset` | `hard_reset run` — the board boots into its new configuration |

Two things this buys you:

- **No keypad entry.** The box copies its own Wi-Fi and server address into the
  terminal's NVS.
- **No silent success.** Step 4 is a real read-back; a mismatch fails the job
  loudly instead of leaving a board that boots with the wrong password.

---

## 1. Prerequisites

| Item | Requirement |
| --- | --- |
| Box image | **1.28.99 or newer.** The flasher travels **with the code**: `vendor/esptool/` is esptool plus its dependencies as a plain directory, and the server hands it to `python -m esptool` as `PYTHONPATH`. No pip, no network, no restart — a hot-fix patch that moves files is enough. (Verified 2026-11-27 on `192.168.1.217` with the pip package *uninstalled*: `esptool.py v4.14.0`, `esptool: true`.) On a tree without the bundle (a desktop build, or a box older than it) install once with `sudo /opt/mrrc_modern/venv/bin/pip install 'esptool>=4.7,<5'` |
| Terminal | A Passport / ESP32-C3 board |
| Cable | A **data** USB cable (charge-only cables show nothing) |
| Network | The box reaches `www.vlsc.net` (the firmware comes from the site; the box does not build or host images) |

**Unplug the radio first.** The W103D has two USB ports and the FT-710's CAT and
scope cables occupy both. The radio drops off when you unplug it; plug it back
afterwards and it recovers by itself.

---

## 2. Open the page

```
https://<box>:8888/passport
```

The header shows a state pill and a `← 返回电台` link. The first card reports the
environment:

- `本机 esptool` — **有** / 没有
- `固件目录` — the catalog URL
- `串口` — candidates, e.g. `/dev/ttyACM0 (原生 USB)`

Only ESP32-class ports are listed, and **the radio's own serial port is never a
candidate** — writing 1.7 MB into an FT-710's CAT port is not a failed flash, it
is an incident.

---

## 3. Step 1 — firmware (`1 · 固件`)

1. Click **读取目录**. The box fetches
   `https://www.vlsc.net/mrrc_modern/downloads/passport/firmware.json` and fills
   the dropdown. A failure here is a `502` and the reason is shown in the log.
2. Pick a version.
3. Click **下载并校验**. The image is downloaded and its SHA-256 checked
   **before** the flash starts, so the write phase contains no download.

A build that was already downloaded is reused (`downloaded` in the dropdown).

## 4. Step 2 — device (`2 · 设备`)

Plug the board in, then click **重新识别** if it does not appear. The dropdown
labels each port:

- `（原生 USB）` — USB-Serial-JTAG, the normal case
- `（USB 转串口）` — a bridge chip

**Board not appearing?** Hold the board's **BOOT** button while plugging the USB
cable in (download mode), then click **重新识别**.

## 5. Step 3 — the configuration to write (`3 · 要写进设备的配网`)

| Field | Prefill | Notes |
| --- | --- | --- |
| `Wi-Fi 名称（SSID）` | the network the box is currently on | editable |
| `Wi-Fi 口令` | **empty** | type it once per board. NetworkManager does not hand a passphrase to a non-root user, and the server deliberately does not ask for it |
| `设备要连的地址` | this box's LAN address | change it to the public entry (`w103d.vlsc.net`) if the terminal will be used away from home |
| `盒子网页口令` | empty | **empty = use this box's own web password.** The value never appears in the response or the log |

## 6. Step 4 — write (`4 · 写入`)

Click **开始刷写**. The log pane (`pre#log`) shows esptool's own output, and the
page polls `/api/passport/state` once a second while the job runs.

A run looks roughly like:

```
chip id
  ... Chip is ESP32-C3 ...
firmware 1.3.1
  ... Writing at 0x00010000... (100 %)
wifi config
  ... Writing at 0x00009000... (100 %)
read back
reset
done
```

The job is a **single-slot lock**: a second start while one is running gets
`409 已经有一次刷写在进行`. The state pill turns `done` (green) or `failed` (red)
with a reason.

### What is verified, and what is not

- **Verified**: every written byte (`--verify`), and the NVS partition is read
  back and compared field by field.
- **Not verified by the page**: that the board actually joins the Wi-Fi and
  reaches the box. That is the acceptance step below — it needs a mains cable
  and a minute.

---

## 7. Acceptance (do this every time)

1. Unplug the USB cable from the box, plug the board into a USB power supply.
2. Watch the board: it should boot to its main screen and connect.
3. On the box, open `/` (or `terminal` sessions). The Passport should appear as a
   terminal session with `role=operator`.
4. Tune with it: the frequency on the radio follows.

A board that flashes fine but never connects is almost always a wrong Wi-Fi
passphrase or a host value it cannot reach — re-run step 3 with the same
firmware (the NVS write is fast; the merged image is skipped if unchanged).

---

## 8. Troubleshooting

| Symptom | Cause / action |
| --- | --- |
| Start button greyed out, `本机 esptool 没有` | Neither the venv nor `vendor/esptool/` provides esptool. A tree that ships the bundle is healthy by construction, so this means the tree is old or incomplete — update it, or install once with `sudo /opt/mrrc_modern/venv/bin/pip install 'esptool>=4.7,<5'` |
| `那个串口不在可选列表里` (400) | The port disappeared between listing and starting, or it is the radio's port. Click `重新识别` |
| `固件版本不在目录里` (400) | The catalog changed after the page loaded. `读取目录` again |
| `已经有一次刷写在进行` (409) | One job at a time. Wait for it, or reload the page after it finishes |
| `catalog: ...` (502) | The box could not reach `www.vlsc.net`. Check its network / DNS (`/settings` → 网络) |
| `download: ...` (502) | Catalog fetched but the image failed or the SHA-256 did not match. Retry; if it persists the published artifact is stale |
| No port even after BOOT+plug | Another data cable; or the board is not an ESP32-C3 (the chip check would refuse it anyway) |
| `read back` mismatch | The job fails on purpose (a short read-back is not a trimmed success). Re-run; if it repeats, the board's flash is failing |
| Flash ok, board reboots into the setup AP | The NVS write did not take, or the SSID/passphrase is wrong. Re-run step 3 |

---

## 9. Safety boundaries (by construction)

- **The Wi-Fi passphrase never leaves the box's own process**: it goes into the
  NVS image in memory, and `passport_flash.redact()` scrubs it from every line
  that reaches the page or `server.log` (the log pane ends up in screenshots and
  support bundles).
- **The box's web password is never requested by the page** when the field is
  left empty; the server substitutes its own.
- **Every interface of the radio's USB device is excluded**, not just the path `cat.port` names: the FT-710's CP2105 exposes CAT (`if00`) and scope (`if01`) as two ttys, and until 2026-11-27 the page offered the scope port as a Passport candidate. `candidate_ports()` now groups ports by their by-id device key (`usb_device_key()`) and drops the whole chip.
- **Wrong chip aborts before the first write**, so a mis-plugged board is not
  half-flashed.
- **`listen` / `sdr` sessions get `403`** — flashing is an operator action.

---

## 10. Status

Code and tests are in place (`passport_flash.py`, `tests/test_passport_flash.py`,
21 tests passing, including the byte-identical NVS comparison against ESP-IDF's
own generator). Shipped in **1.28.99 · FP86**.

**Partially verified on hardware, 2026-11-27** (box `192.168.1.217`, 1.28.99):
`esptool` installed, `GET /api/passport/state` reports `esptool: true`, the port
list is empty with nothing plugged in (the radio's `ttyUSB1` used to appear),
the SSID prefill returns `CMCC-cnham`, and `POST /api/passport/firmware` fetched
the catalog (`1.3.1`).

**A real flash onto a Passport has not been performed yet** (as recorded in
CHANGELOG): one board plus one box, flash, unplug, power it, confirm it joins the
Wi-Fi and shows up as a terminal session. Section 7 is that checklist.
