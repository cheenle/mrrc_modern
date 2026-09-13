---
name: windows-installer
description: Use when building, rebuilding, verifying, or deploying the MRRC Modern Windows installer (MRRC-Modern-Setup.exe / Inno Setup), or when the Win11 KVM build VM is unreachable mid-build ("No route to host", qemu OOM-killed), the build script prints BUILD_DONE although nothing was built, PyInstaller or iscc fails on the VM, tests fail only on the VM (WinError 32 temp cleanup, GBK locale), the built exe is missing FTDI DLLs / opus.dll / static assets / mem_channels.json / lameenc, the VM builds the wrong product (MRRC_FT8-Setup.exe), the venv is missing (Activate.ps1 not found), an installed app will not start at all, or TX audio crackles on the VM.
---

# Windows Installer Build (MRRC Modern)

## Overview

The Windows release is built **on the Win11 KVM VM, never on the Mac** (no Windows toolchain locally; PyInstaller 6.21.0 + Inno Setup 6.7.3 + Python 3.12.4 live in the VM). Topology: Mac → `ssh ham.vlsc.net` (KVM host, sudo passwordless) → `ssh cheenle@192.168.122.133` (win11 VM; default shell is **PowerShell 5.1**). Full operator manual: `win_pack.md`. The end-to-end release sequence (both platforms + website) lives in the `dual-platform-release` skill.

Build gate mirrors macOS: full test suite → 3 PyInstaller specs → `iscc`. A non-zero step aborts the build (`Invoke-Checked`), so a green run implies 693 tests passed.

## Build Flow (v1.14.0-proven)

0. **Pre-flight on Mac**: full suite green via `.venv/bin/python -m unittest discover -s tests` (NOT system `python3` — no deps); version bumped in `packaging/windows/MRRC-Modern.iss` (`#define MyAppVersion`) to match the top `CHANGELOG.md` entry.
1. **Source zip** from repo root (~27 MB) with the exact exclusion set from `win_pack.md` Step 1 — `./certs/*` and `./promo/*` MUST be excluded, `.agents/` MUST be INCLUDED (gotcha 2).
2. **Upload via jump host**: `scp dist/mrrc_modern_src.zip ham.vlsc.net:/tmp/` then ham relays it to the VM.
3. **Extract with venv preservation** (gotcha 7): write `extract_v<ver>.ps1` locally (stop `python*`/`MRRC-Modern*` processes → `Move-Item C:\mrrc_modern\venv C:\mrrc_venv_keep -Force` → `Remove-Item C:\mrrc_modern -Recurse -Force` → `Expand-Archive` → move venv back → print `EXTRACT_DONE venv=<bool>`), scp it to the VM, run with `powershell -NoProfile -ExecutionPolicy Bypass -File`. Template in `win_pack.md` Step 3.
4. **Build** with a per-version script `C:\Users\cheenle\build_mrrc_v<ver>.ps1` (Inno Setup dir onto PATH → activate `C:\mrrc_modern\venv` → `packaging\windows\build.ps1`), scp'd to the VM and run the same way. Output: `C:\mrrc_modern\dist\windows\MRRC-Modern-Setup.exe`.
5. **Verify on VM**: exe exists (45–46 MB range), `dist\windows\MRRC-Modern\_internal\vendor\ftdi\windows\bin\x64` has `FT4222.dll` + `ftd2xx.dll`, `_internal\static\index.html` and `_internal\mem_channels.json` exist, `(Get-FileHash ... -Algorithm SHA256).Hash`.
6. **Retrieve via jump**: ham pulls from the VM, Mac pulls from ham; `shasum -a 256` locally must equal the VM hash.

## CRITICAL Gotchas (each caused a real failure)

1. **Never use `C:\Users\cheenle\build_vm.ps1`.** It was repurposed to `Set-Location C:\mrrc_ft8` (a different project) and silently builds a broken `MRRC_FT8-Setup.exe` (hit 2026-08-15). Every release creates its own `build_mrrc_v<ver>.ps1` with `Set-Location C:\mrrc_modern`.
2. **Zip exclusion list is load-bearing.** Excluding `./.agents/*` breaks `tests/test_sdd_harness.py` → 24 test failures on the VM (the harness files live there); excluding `./website/*` breaks `tests/test_sdd_docs_consistency.py` + the `diagram-copy` rules in `tests/test_release_artifacts.py` → 14 failures, because those tests read the landing pages and the generated SDD pages (v1.16.0 gate hit exactly this). Keep `website/` in and exclude only `website/downloads/`, `website/videos/`, `website/__pycache__/`. Excluding `./certs/*` is mandatory (TLS private keys). Forgetting `./promo/*` makes a ~670 MB zip that uploads forever.
3. **PowerShell 5.1 on the VM**: `&&` is invalid (use `;` or a script); `2>` redirects as UTF-16LE (`iconv -f UTF-16LE -t UTF-8` before grep); multi-hop ssh quoting is a trap — always write a local `.ps1`, scp it, execute with `-File`.
4. **TX audio can never be verified on this VM.** KVM USB passthrough breaks isochronous OUT scheduling (playback timing chaos on MME and WASAPI alike; RX capture and FT4222 bulk are fine). Don't debug TX crackling here — physical Windows hardware only.
5. **Radio USB unplug kills COM/audio until passthrough is reattached.** If the VM suddenly has only COM1 and no `USB Audio`: the radio was unplugged/powered off; after re-plugging, re-run the three `sudo virsh -c qemu:///system attach-device win11` commands (VID:PID loop in `win_pack.md` §6) and restart the server.
6. **Processes launched over SSH die when the session ends** (Windows job object). The persistent 8443 service runs via scheduled task `MRRC-Modern-HTTPS` → `start_mrrc_modern_https.ps1` (hidden `Start-Process`), never a bare SSH-launched launcher console.
7. **venv preservation**: the Move-Item trick in Step 3 avoids the documented 4-command pip reinstall. If the venv was still deleted, re-run the FULL `win_pack.md` §2.2 sequence starting with `python -m venv venv` (otherwise `.\venv\Scripts\Activate.ps1` is missing and the build fails immediately).
8. **Dependency drift**: if `requirements*.txt` changed since the last build, re-run the two pip install commands after extraction (the preserved venv has the old deps).

9. **The KVM host can OOM-kill the whole VM, mid-build** (2026-09-12: the extraction died after ~5 min and the VM was simply gone). `ham.vlsc.net` also runs heavy neighbours (a ~9 GB java process was resident); win11 asks for 16 GB on a 28 GB host, so the kernel picked the biggest process — `qemu-system-x86`. Symptoms, in order: `ssh` suddenly says `No route to host`; `virsh -c qemu:///system domifaddr win11` says `domain is not running`; `/var/log/libvirt/qemu/win11.log` ends with `shutting down, reason=crashed`; `sudo dmesg -T | grep -i oom` shows `Killed process … (qemu-system-x86)`. Remedy: shrink the VM for the build and give the host swap, then start it again:

```bash
sudo virsh -c qemu:///system setmaxmem win11 10G --config && sudo virsh -c qemu:///system setmem win11 10G --config
sudo fallocate -l 16G /swap2.img && sudo chmod 600 /swap2.img && sudo mkswap /swap2.img && sudo swapon /swap2.img
sudo virsh -c qemu:///system start win11          # check `free -m` first: <1 GB available = it will die again
```

Restore 16 GB (`setmaxmem`/`setmem 16G --config`) once the neighbour shrinks, or leave 10 GB if it does not.

10. **`BUILD_DONE` is not a success signal.** The per-version wrapper prints it unconditionally; `build.ps1`'s `Invoke-Checked` aborts the *child* PowerShell (exit 1) and the wrapper happily continues to the next line. A v1.15.0 attempt printed `BUILD_DONE` with a failed test gate and no exe at all. Always prove the artifact: `Get-Item dist\windows\MRRC-Modern-Setup.exe` (fresh mtime + 45–46 MB) and `Get-FileHash`.

11. **Extract with `tar -xf`, not `Expand-Archive`.** Windows' bundled bsdtar unpacks the ~13 MB source zip in seconds and with far less memory/CPU than the PowerShell cmdlet (the OOM in gotcha 9 hit an `Expand-Archive` run). Have the script assert its own result: `EXTRACT_DONE venv=True server=True` (venv moved back, `server.py` present), with `Expand-Archive` only as a fallback.

12. **"I installed it and it will not run" is a launcher startup failure with no visible message.** Triage: start `C:\Program Files\MRRC Modern\MRRC-Modern-Launcher.exe` (note: *Launcher*, not the old `MRRC-Modern.exe` name) over SSH with `Start-Process … -RedirectStandardOutput o.txt -RedirectStandardError e.txt`, then read `e.txt` — the traceback is there even though the user's console window closed instantly. Since v1.15.0 the launcher also writes `%LOCALAPPDATA%\MRRC-Modern\launcher.log` and shows a message box. Known cause found exactly this way: `mrrc_modern.env` saved by an ANSI (GBK) editor was not valid UTF-8 (`e2 80 3f` where the shipped template has `e2 80 94`) → `load_env` raised `UnicodeDecodeError` before anything printed. Fixed in `macos/first_run.py: read_env_text` (BOM → UTF-8 → cp936 → latin-1) plus `guarded_main()/report_fatal()`. Both launchers share that reader — **a launcher fix means rebuilding BOTH installers.**

13. **Windows-only test failures are usually open-file handling.** A test that leaves a file open (an MP3 session, a log) passes on macOS/Linux, where unlinking an open file is legal, and fails the VM gate with `PermissionError: [WinError 32] … being used by another process` during `TemporaryDirectory.cleanup()`. Close the handle in `tearDown`/`asyncTearDown` (`session.close_without_finishing()`); treat "green locally, red on the VM" as a real portability bug, not a VM quirk.

## Verification

### Prove the code is inside the artifact (do not grep it)

`strings(1)`/`grep` cannot see into PyInstaller's compressed PYZ — a missing or stale symbol looks identical to a present one. Walk the archive instead (the VM's venv has PyInstaller 6.21.0):

```python
# bundle_check.py — run on the VM: .\venv\Scripts\python.exe bundle_check.py
import marshal, types
from PyInstaller.archive.readers import CArchiveReader
exe = r"C:\mrrc_modern\dist\windows\MRRC-Modern\MRRC-Modern-Server.exe"
r = CArchiveReader(exe)
# the entry *script* (`server`) is a CArchive TOC entry, not a PYZ module:
code = marshal.loads(r.extract("server"))
seen = set(); walk = lambda c: (seen.update(c.co_names), [walk(k) for k in c.co_consts if isinstance(k, types.CodeType)])
walk(code)
print("_ensure_rec_writer" in seen)          # the symbol you shipped
z = r.open_embedded_archive(next(n for n, e in r.toc.items() if e[4] == "z"))
print("recorder" in z.toc)                   # + imported modules in the PYZ
```

### Smoke-test the launcher, not only the server exe

The user runs the *launcher*; starting `MRRC-Modern-Server.exe` by hand proves less. Run the launcher once with the real (already existing) user config and watch its output — a non-UTF-8 `mrrc_modern.env` used to kill it before it printed anything (gotcha 12):

```powershell
$o="$env:TEMP\l_out.txt"; $e="$env:TEMP\l_err.txt"
Start-Process "C:\Program Files\MRRC Modern\MRRC-Modern-Launcher.exe" -PassThru -RedirectStandardOutput $o -RedirectStandardError $e
Start-Sleep 25; Get-Content $o; Get-Content $e       # expect the URL banner, no traceback
```

```bash
# on Mac, after retrieval — all three must agree:
ls -l dist/windows/MRRC-Modern-v<ver>-Windows-x64-Setup.exe        # size == VM size
shasum -a 256 dist/windows/MRRC-Modern-v<ver>-Windows-x64-Setup.exe # == VM Get-FileHash
# VM-side structural checks (win_pack.md Step 5): FTDI DLLs, static, mem_channels.json
```

Install smoke test on the VM: Start Menu shortcut opens the login page; launcher console shows `RX audio started ... (USB Audio ...) @ 44100 Hz`; COM4 (Enhanced port) answers `FA;`. TX audio checks are forbidden here (gotcha 4).

## Website Deploy

The exe is **server-managed on <www.vlsc.net>** — `website/deploy.sh` EXCLUDES `downloads/` from its tar. Upload both names (generic `MRRC-Modern-Setup.exe` + versioned) to `/tmp/*.new`, then `sudo -n mv` into `/var/www/vlsc.net/mrrc_modern/downloads/`, `chown www-data:www-data`, `chmod 644`. Old versioned installers stay (archive); only the generic name is replaced in place. Then `curl -sI` both URLs → 200 + `content-length` == byte size, and hash one downloaded copy. Full sequence: `dual-platform-release` skill / `win_pack.md` §4.

## Common Mistakes

| Symptom | Cause / Fix |
| --- | --- |
| Build produces `MRRC_FT8-Setup.exe`, `C:\mrrc_modern\dist` empty | Used the hijacked `build_vm.ps1` (gotcha 1); use per-version script |
| 24 harness test failures on the VM | Source zip excluded `./.agents/*` (gotcha 2) |
| `.\venv\Scripts\Activate.ps1` not found at build | venv deleted during extract (gotcha 7); re-run §2.2 full four commands |
| `&&` parse error / empty grep over redirected output | PowerShell 5.1 quirks (gotcha 3); use `;` and scripts |
| TX audio crackles on the VM | KVM isochronous OUT is broken — do not debug (gotcha 4); verify on physical hardware |
| COM ports + USB audio vanish on the VM | Radio USB unplugged; re-attach passthrough (gotcha 5) |
| Service disappears after SSH logout | SSH-launched process killed by job object (gotcha 6); use the scheduled-task path |
| `virsh list` shows no win11 VM | Missing `sudo` + `qemu:///system`: `sudo virsh -c qemu:///system list --all` |
| VM vanishes mid-build, `ssh` says `No route to host`, `domifaddr` says domain not running | Host OOM-killed qemu (16 GB VM on a 28 GB host with a 9 GB neighbour): shrink to 10 GB + add swap, see gotcha 9 |
| Script printed `BUILD_DONE` but no (or an old) exe exists | `BUILD_DONE` is unconditional (gotcha 10): check the exe mtime + hash, and read the test output above it |
| Installed app does nothing at all, console flashes | Launcher startup exception — reproduce with redirects / read `%LOCALAPPDATA%\MRRC-Modern\launcher.log` (gotcha 12); check the env file's bytes when it mentions `UnicodeDecodeError` |
| Tests green on the Mac, `WinError 32` on the VM | A test leaked an open file; close it in teardown (gotcha 13) |
