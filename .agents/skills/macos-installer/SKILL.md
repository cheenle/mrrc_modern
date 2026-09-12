---
name: macos-installer
description: Use when building, rebuilding, verifying, or deploying the MRRC Modern macOS installer (.dmg), or troubleshooting a macOS .app that fails to boot / the bundled server that dies at startup ("Failed to load Python shared library .../Contents/Frameworks/Python"), IPv4 refused when bound to "::", HTTPS or self-signed cert issues on the launcher, or the connection-settings restart chain.
---

# macOS Installer Build (MRRC Modern)

## Overview

The macOS release is a locally-built DMG: `packaging/macos/build.sh` runs tests → 3 PyInstaller specs → hand-assembles `Contents/MacOS/` → ad-hoc codesign → `hdiutil` DMG. Version is read from the top `## [vX.Y.Z]` heading in `CHANGELOG.md` — rename/keep that heading first. Since v1.13.0 the launcher serves **HTTPS by default**: a user-supplied `MRRC_SSL_CERT`/`MRRC_SSL_KEY` wins, otherwise a self-signed cert is auto-generated (wiring: gotcha 8); `MRRC_SSL=off` reverts to plain HTTP. The cert SANs cover localhost/hostname/127.0.0.1/::1/LAN IPs; browsers warn once on first visit (click Advanced → Continue) — call this out for novice users.

## Prerequisites

- Python 3.13 venv (`.venv`) with the runtime deps AND `pyinstaller==6.21.0` + `rumps` (`packaging/macos/requirements-build.txt`). From scratch:

  ```bash
  python3.13 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt        # fastapi, uvicorn, pyserial, pyaudio...
  .venv/bin/python -m pip install -r packaging/macos/requirements-build.txt
  ```

- Xcode Command Line Tools (`codesign`, `hdiutil`), `brew install portaudio` (pyaudio).
- FTDI dylibs at `vendor/ftdi/macos/libft4222.dylib` + `libftd2xx.dylib` (universal arm64). Missing → S-meter fallback (warn only).
- Apple Silicon only (arm64). No Developer ID → ad-hoc signed → first launch is right-click → Open once.

## Build

```bash
.venv/bin/python -m pip install -r packaging/macos/requirements-build.txt   # once
PYTHON=.venv/bin/python packaging/macos/build.sh                            # tests + PyInstaller + .app + dmg
```

Output: `dist/macos/MRRC-Modern-v<ver>-arm64.dmg` (checksums printed at the end). `build.sh` gates on the test suite — do not bypass.

## CRITICAL Gotchas (each caused a real broken build)

1. **`Contents/Frameworks` symlink (MUST stay).** PyInstaller's onedir exe inside a `.app` switches to "bundle mode": the Python framework AND the whole data tree (`sys._MEIPASS`: base_library.zip, numpy, static/) resolve from `Contents/Frameworks`, not the `_internal` sibling. build.sh creates `Contents/Frameworks -> MacOS/_internal`. If removed, the server dies instantly with `[PYI-XXXX:ERROR] Failed to load Python shared library '.../Contents/Frameworks/Python'` — this was latent in every macOS package before v1.13.0.

2. **Dual-stack `::` needs a pre-bound socket.** `asyncio.loop.create_server(host="::")` sets `IPV6_V6ONLY=1` (IPv6-only), so uvicorn's plain `host="::"` refuses IPv4 — and the launcher opens `http://127.0.0.1:8888` (IPv4), locking out the local user. server.py must pre-bind a `V6ONLY=0` socket and hand it to `uvicorn.Server(uvicorn.Config(app,...)).run(sockets=[sock])`. Do NOT pass `sockets=` to `uvicorn.run()` or `uvicorn.Config` — uvicorn 0.52 has no such parameter (TypeError crash). A manually created `socket.socket(AF_INET6)` + `setsockopt(IPV6_V6ONLY,0)` is dual-stack on macOS.

3. **Password banner is loopback-only.** `server.py::_first_run_password_banner(request)` renders the auto-generated login password only when `MRRC_AUTO_PASSWORD=1` AND the client is 127.0.0.1/::1 — binding `::` would otherwise leak it to the LAN. Keep that guard.

4. **Launcher monitor** (`macos/launcher.py::_monitor_loop`): restarts the server when it exits with code 42 (web "保存并重启"), and must `self.proc = None` on ANY non-42 exit — otherwise it busy-spins at 100% CPU on the reaped process's `wait()`. `_quitting` is set before stopping in `on_quit`/`_on_sigterm`.

5. **rumps/NSApplication swallows SIGTERM.** The menu-bar launcher's Python `signal.SIGTERM` handler never runs (run loop blocks the main thread). Graceful quit = the menu-bar **Quit MRRC Modern** item. For programmatic cleanup: SIGTERM the server process first, then SIGKILL the launcher.

6. **`--password` CLI arg is inert.** server.py sets `os.environ["MRRC_WEB_PASSWORD"]` AFTER config import, so login compares against the imported `config.WEB_PASSWORD`. For headless verification pass the password via env (`MRRC_WEB_PASSWORD=...`), not `--password`.

7. **First-run zero-config.** `macos/first_run.py` (imports only stdlib+pyserial, no rumps): generates a random web password, scans serial ports (`/dev/cu.*`, bogus ports excluded), probes FT-710 (ASCII `ID;` @38400) then IC-7300 (CI-V 0x19 @115200). `macos/default.env` leaves `MRRC_WEB_PASSWORD`/`MRRC_SERIAL_PORT`/`MRRC_RADIO_MODEL` empty to trigger it. `MRRC_PORT_CONFIRMED=1` marks a probed port as settled. The launcher passes `MRRC_CONFIG_FILE` so the web connection-settings dialog can persist changes.

8. **macOS launcher HTTPS wiring** (mirrors `windows/launcher.py::ssl_material`). `macos/launcher.py` must keep `ssl_material(env)`, `local_url(env, secure=...)`, and `build_command(ssl_pair)` (passes `--ssl-cert`/`--ssl-key`; `--no-ssl` only when the pair is None). `start_server()` re-resolves `ssl_material` + `self.url` on every spawn so TextEdit config edits (e.g. adding `MRRC_SSL_CERT`) apply on Restart. `wait_for_server(..., secure=True)` probes with TLS verification skipped (the self-signed cert isn't in any trust store). The launcher onefile spec MUST list `ssl_bootstrap` + `cryptography` in hiddenimports (cert generation needs `cryptography`, which is also pulled into the server onedir by requirements.txt). Cert lives at `~/Library/Application Support/MRRC-Modern/certs/{server.crt,server.key}` via `ssl_bootstrap.ensure_self_signed(user_data_dir()/certs)`.

9. **DMG must be the classic installer layout.** `hdiutil create -srcfolder "$APP_BUNDLE"` produces a DMG holding ONLY the bare .app — no "Applications" shortcut, which breaks the "drag into Applications" step the website guide promises and confuses novices. build.sh must stage a folder with `ln -sf /Applications <stage>/Applications` + a copy of the .app, then `-srcfolder` that staging dir (Step 6). After a DMG-layout change the SHA-256 on the website download card MUST be updated (the checksums change).

10. **Interpreter selection is explicit-path only.** `build.sh` is bash — run it with `bash`, never `python` (SyntaxError at `set -euo pipefail`; plain `python` may not even exist). Do NOT `source .venv/bin/activate`: this repo's `.venv` was copied from the `mrrc_ft710` project, whose activate hardcodes the wrong `VIRTUAL_ENV`, so activation silently selects an interpreter without PyInstaller. Test runs outside build.sh also need the explicit path (`.venv/bin/python -m unittest discover -s tests`) — Homebrew `python3` has no project deps. Canary: if a PyInstaller error message mentions `mrrc_ft710`, you activated the wrong venv.

## Verification

Post-build structural checks (confirm the critical bits landed):

```bash
ls -la dist/macos/MRRC-Modern.app/Contents/ | grep Frameworks        # expect Frameworks -> MacOS/_internal
defaults read dist/macos/MRRC-Modern.app/Contents/Info.plist CFBundleShortVersionString   # = CHANGELOG version
test -f dist/macos/MRRC-Modern.app/Contents/MacOS/vendor/ftdi/macos/libft4222.dylib && echo "FTDI bundled"
# DMG must be the classic installer layout (app + Applications symlink), not a bare .app:
hdiutil attach -readonly -nobrowse dist/macos/MRRC-Modern-*.dmg && ls -la "/Volumes/MRRC Modern" && hdiutil detach "/Volumes/MRRC Modern"
#   expect: MRRC-Modern.app  AND  Applications -> /Applications
```

Headless (dual-stack + HTTPS + banner + endpoints) — run from the repo root (the heredoc imports `ssl_bootstrap` from the source tree):

```bash
TMP=$(mktemp -d)
# generate a self-signed cert with the bundled bootstrap, then serve HTTPS on ::
.venv/bin/python - "$TMP" <<'PY'
import os, ssl_bootstrap, sys
from pathlib import Path
ssl_bootstrap.ensure_self_signed(Path(sys.argv[1]))
PY
MRRC_WEB_PASSWORD=testpass MRRC_AUTO_PASSWORD=1 \
  dist/macos/MRRC-Modern.app/Contents/MacOS/MRRC-Modern-Server \
  --ssl-cert "$TMP/server.crt" --ssl-key "$TMP/server.key" --host :: --port 8899 --serial-port "" &
# expect 401 from BOTH (IPv4 + IPv6) over HTTPS; plain HTTP must fail:
curl -sk -o /dev/null -w '%{http_code}\n' https://127.0.0.1:8899/api/health
curl -sk -o /dev/null -w '%{http_code}\n' 'https://[::1]:8899/api/health'
curl -s  -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8899/api/health   # 000
# expect the auto-password banner on the login page (loopback):
curl -sk https://127.0.0.1:8899/login | grep -q '首次运行已自动生成密码' && echo "banner OK"
rm -rf "$TMP"
```

GUI end-to-end (the "安装即可用" chain):

1. Mount the DMG → copy `MRRC-Modern.app` to `/Applications`
2. `xattr -dr com.apple.quarantine /Applications/MRRC-Modern.app` (or right-click → Open once)
3. `open /Applications/MRRC-Modern.app`
4. Verify menu-bar icon appears and the browser opens the login page (HTTPS — first visit shows the self-signed warning; Advanced → Continue) with the auto-generated password banner
5. Verify real hardware auto-detected: `MRRC_SERIAL_PORT` + `MRRC_PORT_CONFIRMED=1` in `~/Library/Application Support/MRRC-Modern/mrrc_modern.env`
6. Verify the restart chain: `POST /api/setup` triggers a launcher auto-restart (server PID changes)
7. Verify `GET /api/devices` lists the real serial/audio devices

Cleanup: `kill -TERM <server_pid>` then `pkill -KILL -f MRRC-Modern-Launcher`.

## Website Deploy

- DMG goes to **<www.vlsc.net>** `/var/www/vlsc.net/mrrc_modern/downloads/` (sudo mv + chown www-data + chmod 644). `website/deploy.sh` EXCLUDES `downloads/` — the DMG is server-managed, never in the deploy tar; `website/downloads/*.dmg` is gitignored (untracked staging only).
- `website/index.html` + `website/zh/index.html`: BEFORE deploying, update the version badge, the macOS download card (new size + SHA-256 printed by build.sh), and the macOS install guide track. Then `echo y | ./deploy.sh` from `website/` (uploads HTML, backs up, nginx -t). Deploy target is <www.vlsc.net> (user must confirm production deploys).
- Keep the hero dual-platform (macOS primary + Windows) — the landing page should not favor one platform.

## Common Mistakes

| Symptom | Cause / Fix |
| --- | --- |
| `[PYI-XXXX] Failed to load Python shared library .../Contents/Frameworks/Python` | Missing `Contents/Frameworks -> MacOS/_internal` symlink (see gotcha 1) |
| Browser can't reach 127.0.0.1:8888 but ::1 works | `::` bound IPv6-only (see gotcha 2); pre-bind V6ONLY=0 + `Server.run(sockets=[sock])` |
| Password banner visible on LAN | Loopback guard removed (see gotcha 3) |
| Launcher pegs CPU after server stops | Monitor didn't park `self.proc=None` on non-42 exit (see gotcha 4) |
| Menu-bar Quit leaves server running | SIGTERM under rumps doesn't fire; use the menu item (see gotcha 5) |
| Login rejects the `--password` you passed | Pass via env, not CLI (see gotcha 6) |
| DMG contains only the bare .app, no Applications shortcut | Staging dir with `ln -sf /Applications` skipped (see gotcha 9); after re-fixing the layout, the website card SHA-256 MUST be updated |
| TextEdit config edits (e.g. adding `MRRC_SSL_CERT`) ignored after Restart | `start_server()` must re-resolve `ssl_material` + `self.url` on every spawn (see gotcha 8) |
| Double-clicking the .app does nothing, no window, no error | Launcher raised before rumps started: it now logs to `~/Library/Application Support/MRRC-Modern/launcher.log` and shows an alert; reproduce from a terminal to see the traceback. A non-UTF-8 `mrrc_modern.env` (ANSI/GBK editor) used to raise `UnicodeDecodeError` in `load_env` — read it via `macos.first_run.read_env_text` (BOM → UTF-8 → cp936 → latin-1), never `read_text(encoding="utf-8")` |
| Fixing the launcher/env reader but only rebuilding the DMG | The Windows and macOS launchers share `macos/first_run.py`: a launcher fix invalidates **both** installers (see dual-platform-release gotcha 1) |
| Rebuild shows old version | `CHANGELOG.md` top heading not bumped to `## [vX.Y.Z]` |
| `SyntaxError: invalid syntax` at `set -euo pipefail` / `No module named PyInstaller` from `mrrc_ft710/.venv` | build.sh is a BASH script run as Python, or `source .venv/bin/activate` was used: this repo's `.venv` was copied from the `mrrc_ft710` project and its activate script hardcodes `VIRTUAL_ENV=/Users/cheenle/HAM/mrrc_ft710/.venv` — activation puts the WRONG project's venv (no PyInstaller) on PATH (v1.14.0 build, 2026-09-09). System `python3` (Homebrew) also lacks the project deps (50 `No module named 'serial'` test errors). Always invoke explicitly: `PYTHON=$(pwd)/.venv/bin/python bash packaging/macos/build.sh` — never `source .venv/bin/activate`, never `python build.sh` (see gotcha 10) |
