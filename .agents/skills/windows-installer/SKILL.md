---
name: windows-installer
description: Use when building, rebuilding, verifying, or deploying the MRRC Modern Windows installer (MRRC-Modern-Setup.exe / Inno Setup), or when the Win11 KVM build VM is unreachable, PyInstaller or iscc fails on the VM, tests fail only on the VM, the built exe is missing FTDI DLLs / opus.dll / static assets / mem_channels.json, the VM builds the wrong product (MRRC_FT8-Setup.exe), the venv is missing on the VM (Activate.ps1 not found), or TX audio crackles on the VM and cannot be fixed.
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
2. **Zip exclusion list is load-bearing.** Excluding `./.agents/*` breaks `tests/test_sdd_harness.py` → 24 test failures on the VM (the harness files live there). Excluding `./certs/*` is mandatory (TLS private keys). Forgetting `./promo/*` makes a ~670 MB zip that uploads forever.
3. **PowerShell 5.1 on the VM**: `&&` is invalid (use `;` or a script); `2>` redirects as UTF-16LE (`iconv -f UTF-16LE -t UTF-8` before grep); multi-hop ssh quoting is a trap — always write a local `.ps1`, scp it, execute with `-File`.
4. **TX audio can never be verified on this VM.** KVM USB passthrough breaks isochronous OUT scheduling (playback timing chaos on MME and WASAPI alike; RX capture and FT4222 bulk are fine). Don't debug TX crackling here — physical Windows hardware only.
5. **Radio USB unplug kills COM/audio until passthrough is reattached.** If the VM suddenly has only COM1 and no `USB Audio`: the radio was unplugged/powered off; after re-plugging, re-run the three `sudo virsh -c qemu:///system attach-device win11` commands (VID:PID loop in `win_pack.md` §6) and restart the server.
6. **Processes launched over SSH die when the session ends** (Windows job object). The persistent 8443 service runs via scheduled task `MRRC-Modern-HTTPS` → `start_mrrc_modern_https.ps1` (hidden `Start-Process`), never a bare SSH-launched launcher console.
7. **venv preservation**: the Move-Item trick in Step 3 avoids the documented 4-command pip reinstall. If the venv was still deleted, re-run the FULL `win_pack.md` §2.2 sequence starting with `python -m venv venv` (otherwise `.\venv\Scripts\Activate.ps1` is missing and the build fails immediately).
8. **Dependency drift**: if `requirements*.txt` changed since the last build, re-run the two pip install commands after extraction (the preserved venv has the old deps).

## Verification

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
