---
name: macos-installer
description: Use when building, rebuilding, verifying, or deploying the MRRC Modern macOS installer (.dmg), or troubleshooting a macOS .app that fails to boot / the bundled server that dies at startup ("Failed to load Python shared library .../Contents/Frameworks/Python"), IPv4 refused when bound to "::", HTTPS or self-signed cert issues on the launcher, or the connection-settings restart chain.
---

# macOS Installer Build (MRRC Modern)

## 现场缺陷：用户看到「已损坏，无法打开」（2026-09-17 上报）

**不是签名"不够好"，而是根签名根本没成功** —— `codesign --verify` 报
`code has no resources but signature indicates they must be present`，`Contents/_CodeSignature`
不存在。`build.sh` 把签名失败写成了警告（`|| echo WARNING: ... non-fatal`），于是**每个版本都带着
坏签名发布**（v1.17.0 的 DMG 实测同样如此）。

**根因**：codesign 会遍历 `Contents/MacOS` 与 `Contents/Frameworks` 寻找嵌套代码，遇到**数据文件/数据目录**
就判为「未签名的代码对象」而拒绝签整个 bundle，报错逐次指向：

| 布局 | codesign 报的 subcomponent |
| --- | --- |
| 数据树在 `MacOS/_internal` | `.../MacOS/_internal/macos/default.env` — `code object is not signed at all` |
| 数据树移到真 `Frameworks/` | `.../Frameworks/click-8.4.2.dist-info` — `bundle format unrecognized` |
| 数据树在 `Resources/` + `Frameworks` 与 `MacOS/_internal` 两个符号链接 | **签名成功**（`valid on disk`） |
| 再加 `MacOS/{macos,version.txt,mem_channels.json,vendor}` 符号链接 | 又失败（codesign 顺着这些链接把目录当嵌套代码） |

**已验证可用的布局**：数据树放 `Contents/Resources/`，只保留
`Contents/Frameworks -> Resources` 与 `Contents/MacOS/_internal -> ../Resources` 两个符号链接，
`Contents/MacOS` 里除可执行文件外**什么都不能有**。此布局下 `codesign --verify` 通过、
`spctl` 只报「无 Developer ID」（用户右键打开即可，不再是"已损坏"），且**冻结服务真跑成功**
（`/api/health` 401、`server.log` 落盘、无 "Failed to load Python"）。

**启动器已改**：`runtime_path()`（`macos/launcher.py` 与 `windows/launcher.py`）先看 `app_dir()`，再回退 `app_dir()/_internal/`，因此 `macos/`、`version.txt`、`mem_channels.json`、`vendor/` 可以住在数据树里。另有两条实测教训：`mv ... || true` 会**吞掉失败**并留下真目录（`ln -sfn` 随后把符号链接嵌进去，签名继续失败）——现在用 `cp -Rf` + `rm -rf` 并断言 `MacOS/_internal` 必须是符号链接；`spctl` 对 ad-hoc 包报 `rejected (no usable signature)`/`no Developer ID` 是**正常**结果（用户右键打开即可），而 `damaged`/`invalid signature` 才是必须阻断发布的状态。

## 装机后「RX 无声」= 音频输入权限（2026-09-18 第二次上报）

应用装好了、电台正常、日志里 RX 流**打开成功** —— 就是没声音。日志只有
`RX audio is near-silent (peak=0.0%)`，提示"检查电台 AF 增益/USB 连接"，把人引向电台。

**真相在系统日志里**（`log show --predicate 'subsystem == "com.apple.TCC"'`）：

```
tccd: Refusing authorization request for service kTCCServiceMicrophone ...
      without NSMicrophoneUsageDescription key
```

打包的 `Info.plist` **没有任何权限说明键**。macOS 把**音频输入**（电台 USB CODEC）归入麦克风权限类；
缺键时 **CoreAudio 依然允许 `open stream` 成功，但把所有输入缓冲填零** —— 不抛错、不返回失败，
只是纯零采样。这就是它最难查的地方：**"流健康"与"有数据"在 macOS 上不是一回事。**

修法：`packaging/macos/Info.plist` 必须有 `NSMicrophoneUsageDescription`
（`build.sh` 已加断言，缺键 `exit 1`）；`server.py` 的静默警告在 frozen macOS 上追加指向
`系统设置 → 隐私与安全性 → 麦克风`。**用户必须点一次"允许"** —— 旧包连询问都不会弹。

排查套路（下次遇到"有流无声"直接照做）：

```bash
log show --last 30m --predicate 'subsystem == "com.apple.TCC"' | grep -i mrrc   # 权限拒绝？
/usr/libexec/PlistBuddy -c "Print :NSMicrophoneUsageDescription" "/Applications/MRRC-Modern.app/Contents/Info.plist"
```

**注意**：ad-hoc 签名下 TCC 授权绑定的是当次构建的 cdhash，**每次升级都会重新询问**；
要免除这一点需要 Developer ID 签名。

**用户侧立刻解封**（对已下载的 v1.17.0/v1.18.0 同样有效）：

```bash
sudo xattr -dr com.apple.quarantine "/Applications/MRRC Modern.app"
```

**build.sh 已加硬门禁**：签名或校验失败即 `exit 1`，`spctl` 报 damaged/invalid 也 `exit 1`
（此前正是这条静默警告让坏包一路发出）。

## 「只有构建机看不见」的两个打包缺陷（2026-10-04 第三次上报，同一台机器）

同一台 Mac 报了两件看似无关的事，根因同类：**打包机上存在、用户机上不存在的东西被当成了常驻假设**。

### 1. 所有外发 HTTPS 都在失败（接入云端 / 诊断包上传 / 软件更新）

报错 `certificate verify failed: unable to get local issuer certificate (_ssl.c:1032)` —— 但 portal 完全正常
（`curl` 200、链完整）。原因是包内 `libcrypto.3.dylib` 是**构建机的** OpenSSL，其编译期默认 CA 路径是
构建机的 MacPorts 目录 `/opt/local/...`；用户机没有 `/opt/local` ⇒ 信任库为空。

```bash
# 包里那份 OpenSSL 到底把默认 CA 指向哪：
strings -a /Applications/MRRC-Modern.app/Contents/Resources/libcrypto.3.dylib \
  | grep -aE "^/(opt|usr|etc).*(openssl|cert)"
# 本机有没有那个目录（构建机上有，用户机上没有 —— 这就是为什么它逃过了所有人）：
ls -d /opt/local 2>&1
```

**排查/解封**：`SSL_CERT_FILE=/etc/ssl/cert.pem`（macOS 系统 bundle）—— 加进
`~/Library/Application Support/MRRC-Modern/mrrc_modern.env` 后重启应用即可。修复见 `net_tls.py`

+ 随包 `vendor/ca/cacert.pem` + 构建闸门 `dev_tools/tls_trust_gate.py`（**故意打空 CA 环境**，
未修法必须复现为空）。顺带记住：**诊断包上传失败与 portal 失败是同一个根因** —— 报告包不是
从应用上传的，而是运维手工拿到的，这本身就说明 HTTPS 出站坏了。

### 2. 真 FT4222 频谱从未启动（界面一直画 S 表合成曲线）

日志 42 次 `scope_pipe exited (frames=0, connected=False)`、`Spectrum broadcast active: S-meter fallback`，
而 `scope_pipe: first frame received — spectrum active` **一次都没有**。硬件和库都在，唯一的问题是
`MRRC_FTDI_LIB_DIR`：随包 `default.env` 给的是相对值 `vendor/ftdi/macos`，启动器把它锚到了
`Contents/MacOS/`，而数据树（签名后）在 `Contents/Resources/` —— 只有 `Contents/MacOS/_internal`
这条链接通到它。

```bash
# 运行中的 server 被告知的目录，必须真实存在：
ps eww -p $(pgrep -f MRRC-Modern-Server) | tr ' ' '\n' | grep MRRC_FTDI_LIB_DIR
ls /Applications/MRRC-Modern.app/Contents/Resources/vendor/ftdi/macos/
```

**解封**：把绝对路径写进 `~/Library/Application Support/MRRC-Modern/mrrc_modern.env` 并重启。
修复：`macos/launcher.py:load_env` 的相对路径改走 `runtime_path()`（回退 `_internal`）。
**教训**：任何「随包模板里的相对路径」都必须经 `runtime_path()`/`_internal` 解析 ——
`app_dir()` 在签名后的 .app 里不是数据树。

## Overview

The macOS release is a locally-built DMG: `packaging/macos/build.sh` runs tests → 3 PyInstaller specs → hand-assembles `Contents/MacOS/` → ad-hoc codesign → `hdiutil` DMG. Version is read from the top `## [vX.Y.Z]` heading in `CHANGELOG.md` — rename/keep that heading first. Since v1.13.0 the launcher serves **HTTPS by default**: a user-supplied `MRRC_SSL_CERT`/`MRRC_SSL_KEY` wins, otherwise a self-signed cert is auto-generated (wiring: gotcha 8); `MRRC_SSL=off` reverts to plain HTTP. The cert SANs cover localhost/hostname/127.0.0.1/::1/LAN IPs; browsers warn once on first visit (click Advanced → Continue) — call this out for novice users.

## Prerequisites

+ Python 3.13 venv (`.venv`) with the runtime deps AND `pyinstaller==6.21.0` + `rumps` (`packaging/macos/requirements-build.txt`). From scratch:

  ```bash
  python3.13 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt        # fastapi, uvicorn, pyserial, pyaudio...
  .venv/bin/python -m pip install -r packaging/macos/requirements-build.txt
  ```

+ Xcode Command Line Tools (`codesign`, `hdiutil`), `brew install portaudio` (pyaudio).
+ FTDI dylibs at `vendor/ftdi/macos/libft4222.dylib` + `libftd2xx.dylib` (universal arm64). Missing → S-meter fallback (warn only).
+ Apple Silicon only (arm64). No Developer ID → ad-hoc signed → first launch is right-click → Open once.

## Build

```bash
.venv/bin/python -m pip install -r packaging/macos/requirements-build.txt   # once
PYTHON=.venv/bin/python packaging/macos/build.sh                            # tests + PyInstaller + .app + dmg
```

Output: `dist/macos/MRRC-Modern-v<ver>-arm64.dmg` (checksums printed at the end). `build.sh` gates on the test suite — do not bypass.

## CRITICAL Gotchas (each caused a real broken build)

1. **`Contents/Frameworks` symlink (MUST stay).** PyInstaller's onedir exe inside a `.app` switches to "bundle mode": the Python framework AND the whole data tree (`sys._MEIPASS`: base_library.zip, numpy, static/) resolve from `Contents/Frameworks`, not the `_internal` sibling. build.sh creates `Contents/Frameworks -> MacOS/_internal`. If removed, the server dies instantly with `[PYI-XXXX:ERROR] Failed to load Python shared library '.../Contents/Frameworks/Python'` — this was latent in every macOS package before v1.13.0.

2. **Dual-stack `::` needs a pre-bound socket.** `asyncio.loop.create_server(host="::")` sets `IPV6_V6ONLY=1` (IPv6-only), so uvicorn's plain `host="::"` refuses IPv4 — and the launcher opens `http://127.0.0.1:8888` (IPv4), locking out the local user. server.py must pre-bind a `V6ONLY=0` socket and hand it to `uvicorn.Server(uvicorn.Config(app,...)).run(sockets=[sock])`. Do NOT pass `sockets=` to `uvicorn.run()` or `uvicorn.Config` — uvicorn 0.52 has no such parameter (TypeError crash). A manually created `socket.socket(AF_INET6)` + `setsockopt(IPV6_V6ONLY,0)` is dual-stack on macOS.

   **Since v1.24.6 EVERY host is pre-bound, not just `::`.** `main()` used to branch: `host in ("::","")` got the guarded pre-bind, everything else went to `uvicorn.run(host=...)` — so an explicit `MRRC_WEB_HOST=127.0.0.1` skipped the platform bind semantics, the restart retry, and the actionable error entirely. All hosts now go through `_bind_listener_socket()` (wildcard → `_bind_dual_stack_socket()`, a specific address → its own family, a specific IPv6 address staying `V6ONLY=1` so it does not also claim the IPv4 wildcard), sharing one `_bind_with_retry()`. Binding therefore happens **before** uvicorn starts, so a server that binds nothing can no longer log `Server ready!` first. An AST guard in `tests/test_server_startup_guards.py` fails if `uvicorn.run(` reappears in `server.py`. Measured on the frozen binary: a second instance exits **1** with `RuntimeError: cannot listen on port 18893: it is already taken ([Errno 48] …). Another MRRC Modern is most likely still running — …`, and exactly one listener remains.

3. **Password banner is loopback-only.** `server.py::_first_run_password_banner(request)` renders the auto-generated login password only when `MRRC_AUTO_PASSWORD=1` AND the client is 127.0.0.1/::1 — binding `::` would otherwise leak it to the LAN. Keep that guard.

4. **Launcher monitor** (`macos/launcher.py::_monitor_loop`): restarts the server when it exits with code 42 (web "保存并重启"), and must `self.proc = None` on ANY non-42 exit — otherwise it busy-spins at 100% CPU on the reaped process's `wait()`. `_quitting` is set before stopping in `on_quit`/`_on_sigterm`.

5. **rumps/NSApplication swallows SIGTERM.** The menu-bar launcher's Python `signal.SIGTERM` handler never runs (run loop blocks the main thread). Graceful quit = the menu-bar **Quit MRRC Modern** item. For programmatic cleanup: SIGTERM the server process first, then SIGKILL the launcher.

6. **`--password` CLI arg is inert.** server.py sets `os.environ["MRRC_WEB_PASSWORD"]` AFTER config import, so login compares against the imported `config.WEB_PASSWORD`. For headless verification pass the password via env (`MRRC_WEB_PASSWORD=...`), not `--password`.

7. **First-run zero-config.** `macos/first_run.py` (imports only stdlib+pyserial, no rumps): generates a random web password, scans serial ports (`/dev/cu.*`, bogus ports excluded), probes FT-710 (ASCII `ID;` @38400) then IC-7300 (CI-V 0x19 @115200). `macos/default.env` leaves `MRRC_WEB_PASSWORD`/`MRRC_SERIAL_PORT`/`MRRC_RADIO_MODEL` empty to trigger it. `MRRC_PORT_CONFIRMED=1` marks a probed port as settled. The launcher passes `MRRC_CONFIG_FILE` so the web connection-settings dialog can persist changes.

8. **macOS launcher HTTPS wiring** (mirrors `windows/launcher.py::ssl_material`). `macos/launcher.py` must keep `ssl_material(env)`, `local_url(env, secure=...)`, and `build_command(ssl_pair)` (passes `--ssl-cert`/`--ssl-key`; `--no-ssl` only when the pair is None). `start_server()` re-resolves `ssl_material` + `self.url` on every spawn so TextEdit config edits (e.g. adding `MRRC_SSL_CERT`) apply on Restart. The launcher onefile spec MUST list `ssl_bootstrap` + `cryptography` in hiddenimports (cert generation needs `cryptography`, which is also pulled into the server onedir by requirements.txt). Cert lives at `~/Library/Application Support/MRRC-Modern/certs/{server.crt,server.key}` via `ssl_bootstrap.ensure_self_signed(user_data_dir()/certs)`.

   **Since v1.24.6 the launcher no longer trusts its own scheme decision.** It used to compute `secure = ssl_pair is not None` and then `webbrowser.open(url)` unconditionally — so when the server degraded to plain HTTP for any reason, the browser was sent to an `https://` port nobody was listening on and the user saw a **black window over a perfectly healthy app** (that is the reported "installed, then black screen"). Both launchers now share `launcher_net.py` (`answers()` probes `/api/health`, where *any* HTTP status counts as "listening", including 401; `served_url()` tries the preferred scheme then the other; TLS is implied by the URL scheme): `url_to_open()` opens whichever scheme actually answers and **says so** instead of degrading silently, and `running_instance_url()` probes *before* spawning so an already-running server is reused rather than doubled. `launcher_net.py` lives at the repo root and must be importable from `macos/launcher.py` — PyInstaller picks it up by import analysis, but confirm it in the launcher's PYZ (`windows-installer` skill, Verification layer 3).

9. **DMG must be the classic installer layout.** `hdiutil create -srcfolder "$APP_BUNDLE"` produces a DMG holding ONLY the bare .app — no "Applications" shortcut, which breaks the "drag into Applications" step the website guide promises and confuses novices. build.sh must stage a folder with `ln -sf /Applications <stage>/Applications` + a copy of the .app, then `-srcfolder` that staging dir (Step 6). After a DMG-layout change the SHA-256 on the website download card MUST be updated (the checksums change).

10. **Interpreter selection is explicit-path only.** `build.sh` is bash — run it with `bash`, never `python` (SyntaxError at `set -euo pipefail`; plain `python` may not even exist). Do NOT `source .venv/bin/activate`: this repo's `.venv` was copied from the `mrrc_ft710` project, whose activate hardcodes the wrong `VIRTUAL_ENV`, so activation silently selects an interpreter without PyInstaller. Test runs outside build.sh also need the explicit path (`.venv/bin/python -m unittest discover -s tests`) — Homebrew `python3` has no project deps. Canary: if a PyInstaller error message mentions `mrrc_ft710`, you activated the wrong venv.

11. **A packaged app must never keep writable state inside its own bundle.** `_writable_runtime_dir()` used to prefer `_runtime_dir()` whenever `os.access(..., W_OK)` — and inside a `.app` that test passes, because the owner can write `Contents/MacOS`. A bare server start therefore created `Contents/MacOS/recordings/`, and **one file in it breaks the code signature**:

    ```
    dist/macos/MRRC-Modern.app: a sealed resource is missing or invalid
    file added: .../MRRC-Modern.app/Contents/MacOS/recordings/probe.mp3
    ```

    (deleting the file restored `valid on disk` + `satisfies its Designated Requirement`). So recording a single QSO would leave the app failing signature checks on its next launch — the same class of accident as v1.18.1, where data under `Contents/MacOS` made codesign refuse the whole bundle. **"Writable" is not the same as "correct"**: since v1.24.6, when `sys.frozen` the function returns `default_user_dir()` without consulting `os.access` at all. Source checkouts and the Linux/Pi install are unaffected and still keep their files next to the code. Both launchers already `setdefault` `MRRC_MEM_FILE` / `MRRC_RECORDINGS_DIR` to the per-user directory, so this also removes the last way for a bare start and a launcher start to disagree about where the same files live.

    Verify it in the clean-room run, not by reading the code: `Recording ready:` must name a path under `~/Library/Application Support/MRRC-Modern/`, the bundle must have **0 files touched** by the run, and `codesign --verify` must still pass **afterwards**.

12. **No macOS metadata may reach the bundle.** `build.sh` assembles from the working tree, so a stray `static/.DS_Store` (or an AppleDouble `._*` left by a tar made without `COPYFILE_DISABLE=1`) is copied into `Contents/Resources/static/` and shipped to every user — where FastAPI's static handler will serve it on request. Check the built bundle: `find dist/macos/MRRC-Modern.app \( -name .DS_Store -o -name '._*' \) | wc -l` must be **0**, and so must the count of `*.pem` / `*.key` / `.env` / `mrrc_modern.env` inside it.

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

### Clean-room run of the FROZEN binary (the layer that actually catches broken releases)

Everything above can pass while the package is still wrong: v1.24.1 shipped `cloud_hub` unused, v1.24.5 shipped `ssl_bootstrap` unimported, and v1.24.6's discarded builds were killed by *this* step alone. Run the packaged server with an **empty `HOME`** and **no launcher environment** — equivalent to a user starting the binary themselves:

```bash
TH=$(mktemp -d); mkdir -p "$TH/Library/Application Support/MRRC-Modern"
printf 'MRRC_WEB_HOST=127.0.0.1\nMRRC_WEB_PORT=18893\nMRRC_WEB_PASSWORD=\n' \
  > "$TH/Library/Application Support/MRRC-Modern/mrrc_modern.env"
# Use the FIELD configuration (an explicit host), not the default: two v1.24.6 defects
# existed only on the explicit-host path, while the `::` default measured green.
A=dist/macos/MRRC-Modern.app/Contents/MacOS/MRRC-Modern-Server
find dist/macos/MRRC-Modern.app -newermt '-1 minute' -type f | wc -l   # bundle baseline
HOME="$TH" "$A" > "$TH/a.out" 2> "$TH/a.err" &
sleep 12
curl -k -s -o /dev/null -w 'https /login -> %{http_code}\n' https://127.0.0.1:18893/login       # 200
curl    -s -o /dev/null -w 'http  /login -> %{http_code}\n' http://127.0.0.1:18893/login        # 000
curl -k -s -o /dev/null -w '/api/health -> %{http_code}\n' https://127.0.0.1:18893/api/health   # 401
grep -E 'signed a self-signed|SSL enabled|Recording ready|starting on port' \
  "$TH/Library/Application Support/MRRC-Modern/logs/server.log"
# duplicate-handler check: one instance must write each line once
L="$TH/Library/Application Support/MRRC-Modern/logs/server.log"
echo "lines=$(grep -c . "$L") distinct=$(sed -E 's/^[0-9-]+ [0-9:,]+ //' "$L" | sort -u | grep -c .)"
# a second instance must fail loudly, exit non-zero, and never claim readiness
HOME="$TH" "$A" > "$TH/b.out" 2> "$TH/b.err"; echo "B exit=$?"
grep -c 'Server ready' "$TH/b.err"; tail -6 "$TH/b.err"     # RuntimeError naming the port
lsof -nP -iTCP:18893 -sTCP:LISTEN | grep -c LISTEN         # exactly 1
kill %1 2>/dev/null
```

Then assert the bundle survived the run — **this is gotcha 11's only real test**:

```bash
find dist/macos/MRRC-Modern.app -newermt '-3 minutes' -type f | wc -l         # 0
ls dist/macos/MRRC-Modern.app/Contents/MacOS/                                 # no recordings/
codesign --verify --verbose=2 dist/macos/MRRC-Modern.app                      # still valid on disk
find dist/macos/MRRC-Modern.app \( -name .DS_Store -o -name '._*' \) | wc -l   # 0 (gotcha 12)
find dist/macos/MRRC-Modern.app \( -name '*.pem' -o -name '*.key' -o -name '.env' \) | wc -l   # 0
rm -rf "$TH"
```

Expected evidence, measured on v1.24.6: `signed a self-signed certificate for 127.0.0.1` → `SSL enabled with a self-signed certificate just generated: …/MRRC-Modern/certs/fullchain.pem` → `Recording ready: …/Library/Application Support/MRRC-Modern/recordings` (a **user** path, never a bundle path) → `https /login 200`, `http /login 000`, `/api/health 401`; log `lines=26 distinct=25`, where the single repeat is `Opening serial port` (logged twice by design: initial connect + scope-init), **not** a duplicated handler; instance B exits `1` with `RuntimeError: cannot listen on port 18893 … Another MRRC Modern is most likely still running`, logs `Server ready` **0** times, and exactly one listener remains.

Also walk the frozen bytecode — the `CArchiveReader` recipe in the `windows-installer` skill (Verification layer 3) works unchanged on `Contents/MacOS/MRRC-Modern-Server` with entry `server` and on `MRRC-Modern-Launcher` with entry `launcher`. Two traps: a symbol read via `getattr(mod, "NAME", …)` lives in **`co_consts`**, not `co_names`; and a function called from another module's body (`config.load_user_config_into_environ`) must be sought in **that PYZ module**, not in the entry script.

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

+ DMG goes to **<www.vlsc.net>** `/var/www/vlsc.net/mrrc_modern/downloads/` (sudo mv + chown www-data + chmod 644). `website/deploy.sh` EXCLUDES `downloads/` — the DMG is server-managed, never in the deploy tar; `website/downloads/*.dmg` is gitignored (untracked staging only).
+ `website/index.html` + `website/zh/index.html`: BEFORE deploying, update the version badge, the macOS download card (new size + SHA-256 printed by build.sh), and the macOS install guide track. Then `echo y | ./deploy.sh` from `website/` (uploads HTML, backs up, nginx -t). Deploy target is <www.vlsc.net> (user must confirm production deploys).
+ Keep the hero dual-platform (macOS primary + Windows) — the landing page should not favor one platform.

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
| Fixing the launcher/env reader but only rebuilding the DMG | The Windows and macOS launchers share `macos/first_run.py`: a launcher fix invalidates **both** installers (see dual-platform-release gotcha 3) |
| Rebuild shows old version | `CHANGELOG.md` top heading not bumped to `## [vX.Y.Z]` |
| App fails signature checks / reports "is damaged" **after it has been used** | Writable state was created inside the bundle (`Contents/MacOS/recordings/…`): one added file breaks the seal (gotcha 11). `_writable_runtime_dir()` must return the per-user directory whenever `sys.frozen` |
| Bundle contains `.DS_Store` or `._*` files | Stray macOS metadata in the working tree, copied in by `build.sh` (gotcha 12); clean the tree and rebuild — FastAPI's static handler serves them to users on request |
| A second server logs `Server ready!` and then dies on a bare `[Errno 48]` | The explicit-host path handed the bind to `uvicorn.run()` (gotcha 2): every host must go through `_bind_listener_socket()`, which binds before uvicorn starts |
| Browser shows a **black/blank window** while the server is running fine | The launcher opened the scheme it *computed* while the server served the other one (gotcha 8): probe `/api/health` through `launcher_net.served_url()` and open whichever answers, announcing any switch |
| Two instances fight over the port and the radio's serial port | The launcher spawned a second server instead of reusing the running one: probe with `running_instance_url()` **before** spawning |
| Suite is green locally but the VM gate dies with `WinError 32` | A test closed its log handler **outside** the `with tempfile.TemporaryDirectory()` block (`windows-installer` gotcha 13) |
| `SyntaxError: invalid syntax` at `set -euo pipefail` / `No module named PyInstaller` from `mrrc_ft710/.venv` | build.sh is a BASH script run as Python, or `source .venv/bin/activate` was used: this repo's `.venv` was copied from the `mrrc_ft710` project and its activate script hardcodes `VIRTUAL_ENV=/Users/cheenle/HAM/mrrc_ft710/.venv` — activation puts the WRONG project's venv (no PyInstaller) on PATH (v1.14.0 build, 2026-09-09). System `python3` (Homebrew) also lacks the project deps (50 `No module named 'serial'` test errors). Always invoke explicitly: `PYTHON=$(pwd)/.venv/bin/python bash packaging/macos/build.sh` — never `source .venv/bin/activate`, never `python build.sh` (see gotcha 10) |
