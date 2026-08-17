# Windows Desktop Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows desktop installer that bundles the MRRC Modern server
(FT-710 and IC-7300/IC-7300MK2 backends), Python runtime, static UI, optional
FTDI DLL support, and a desktop launcher.

**Architecture:** Package the existing FastAPI server with PyInstaller
one-folder mode and wrap it with an Inno Setup installer. Add a small launcher
that reads a Windows config file, starts the bundled server process, opens the
browser, and stops the process on exit. Make FT4222 true spectrum work for the
FT-710 by bundling FTDI DLLs and making `backends.ft710.scope_pipe` runnable
from a frozen distribution. IC-7300/MK2 uses the same bundle but does not need
FTDI DLLs.

**Tech Stack:** Python 3.11+, PyInstaller, Inno Setup, FastAPI/Uvicorn, PyAudio,
pyserial, FTDI D2XX/LibFT4222 DLLs (FT-710 only).

---

## File Structure

- Create `windows/launcher.py`: Windows desktop launcher, config handling, server subprocess lifecycle, browser open.
- Create `windows/default.env`: editable default runtime config.
- Create `packaging/pyinstaller/ft710_server.spec`: PyInstaller one-folder server build.
- Create `packaging/pyinstaller/ft710_launcher.spec`: PyInstaller launcher build.
- Create `packaging/windows/MRRC-FT710.iss`: Inno Setup installer definition.
- Create `packaging/windows/build.ps1`: Windows build orchestration.
- Create `vendor/ftdi/windows/README.md`: FTDI DLL acquisition and license/source notes.
- Modify `scope_libraries.py`: PyInstaller-aware resource and Windows DLL discovery.
- Modify `server.py`: PyInstaller-aware scope pipe subprocess command.
- Add tests in `tests/test_windows_packaging_paths.py`.

> **Update (2026-08-17):** The packaging targets were renamed to
> `mrrc_modern_server.spec` / `mrrc_modern_launcher.spec` / `MRRC-Modern.iss`
> and now bundle both `backends.ft710.*` and `backends.ic7300.*`.  The installed
> app supports `MRRC_RADIO_MODEL=ft710|ic7300|ic7300mk2`; FTDI DLLs are only
> required for FT-710 true spectrum.

## Task 1: PyInstaller Runtime Path Helpers

**Files:**
- Modify: `scope_libraries.py`
- Test: `tests/test_windows_packaging_paths.py`

- [ ] **Step 1: Write failing tests**

Add tests that define the expected path behavior:

```python
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import scope_libraries


class WindowsPackagingPathTests(unittest.TestCase):
    def test_resource_roots_include_pyinstaller_meipass(self):
        fake_meipass = Path("/tmp/ft710_meipass")
        with patch.object(sys, "_MEIPASS", str(fake_meipass), create=True):
            roots = scope_libraries.get_resource_roots()
        self.assertIn(fake_meipass, roots)

    def test_resource_roots_include_frozen_executable_dir(self):
        fake_exe = "/tmp/ft710_app/ft710-server.exe"
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", fake_exe):
            roots = scope_libraries.get_resource_roots()
        self.assertIn(Path("/tmp/ft710_app"), roots)

    def test_windows_vendor_dir_is_searched(self):
        with patch.object(scope_libraries.sys, "platform", "win32"):
            dirs = scope_libraries.get_candidate_library_dirs()
        self.assertTrue(any(str(path).endswith("vendor/ftdi/windows/bin/x64") for path in dirs))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
python -m unittest tests.test_windows_packaging_paths -v
```

Expected: fails because `get_resource_roots()` and `get_candidate_library_dirs()` do not exist.

- [ ] **Step 3: Implement runtime path helpers**

In `scope_libraries.py`, add:

```python
def get_resource_roots() -> List[Path]:
    roots: List[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(SCRIPT_DIR)
    unique: List[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def get_candidate_library_dirs() -> List[Path]:
    candidate_dirs: List[Path] = []
    ftdi_dir = os.environ.get("FT710_FTDI_LIB_DIR")
    if ftdi_dir:
        candidate_dirs.append(Path(ftdi_dir))
    for root in get_resource_roots():
        candidate_dirs.extend([
            root / "lib",
            root / "vendor" / "ftdi",
            root / "vendor" / "ftdi" / "windows" / "bin" / "x64",
        ])
    candidate_dirs.extend(_linux_system_dirs())
    return candidate_dirs
```

Update `find_ftdi_libraries()` to use `get_candidate_library_dirs()`.

- [ ] **Step 4: Run tests and verify pass**

Run:

```powershell
python -m unittest tests.test_windows_packaging_paths -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add scope_libraries.py tests/test_windows_packaging_paths.py
git commit -m "feat: add frozen FTDI library discovery"
```

## Task 2: Windows DLL Loader Path

**Files:**
- Modify: `scope_pipe.py`
- Test: `tests/test_windows_packaging_paths.py`

- [ ] **Step 1: Write failing test**

Append:

```python
    def test_configure_windows_dll_search_path_calls_add_dll_directory(self):
        calls = []

        def fake_add_dll_directory(path):
            calls.append(Path(path))

        with patch.object(scope_libraries.sys, "platform", "win32"), \
             patch.object(scope_libraries.os, "add_dll_directory", fake_add_dll_directory, create=True), \
             patch.object(scope_libraries, "get_candidate_library_dirs", return_value=[Path("/tmp/missing"), Path("/tmp/exists")]), \
             patch.object(Path, "is_dir", lambda self: str(self) == "/tmp/exists"):
            scope_libraries.configure_windows_dll_search_path()

        self.assertEqual(calls, [Path("/tmp/exists")])
```

- [ ] **Step 2: Run test and verify failure**

Run:

```powershell
python -m unittest tests.test_windows_packaging_paths.WindowsPackagingPathTests.test_configure_windows_dll_search_path_calls_add_dll_directory -v
```

Expected: fails because `configure_windows_dll_search_path()` does not exist.

- [ ] **Step 3: Implement DLL search path setup**

In `scope_libraries.py`, add:

```python
_DLL_DIRECTORY_HANDLES: list[object] = []


def configure_windows_dll_search_path() -> None:
    if sys.platform != "win32":
        return
    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is None:
        return
    for directory in get_candidate_library_dirs():
        if directory.is_dir():
            handle = add_dll_directory(str(directory))
            _DLL_DIRECTORY_HANDLES.append(handle)
```

In `scope_pipe.py`, before `require_ftdi_libraries()`:

```python
from scope_libraries import configure_windows_dll_search_path
...
configure_windows_dll_search_path()
ft4222_path, ftd2xx_path = require_ftdi_libraries()
```

- [ ] **Step 4: Run tests**

Run:

```powershell
python -m unittest tests.test_windows_packaging_paths -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add scope_libraries.py scope_pipe.py tests/test_windows_packaging_paths.py
git commit -m "feat: configure windows ftdi dll search path"
```

## Task 3: Frozen Scope Pipe Startup

**Files:**
- Modify: `server.py`
- Test: `tests/test_windows_packaging_paths.py`

- [ ] **Step 1: Write failing tests**

Append:

```python
import server


class ScopePipeCommandTests(unittest.TestCase):
    def test_unfrozen_scope_pipe_command_uses_python_script(self):
        cmd = server._scope_pipe_command()
        self.assertEqual(Path(cmd[1]).name, "scope_pipe.py")

    def test_frozen_scope_pipe_command_uses_bundled_exe(self):
        with patch.object(server.sys, "frozen", True, create=True), \
             patch.object(server.sys, "executable", r"C:\MRRC-FT710\ft710-server.exe"), \
             patch.object(server.Path, "exists", lambda self: self.name == "scope_pipe.exe"):
            cmd = server._scope_pipe_command()
        self.assertEqual(Path(cmd[0]).name, "scope_pipe.exe")
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
python -m unittest tests.test_windows_packaging_paths.ScopePipeCommandTests -v
```

Expected: fails because `_scope_pipe_command()` does not exist.

- [ ] **Step 3: Implement command helper**

In `server.py`, near static path constants:

```python
def _runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return SCRIPT_DIR


def _scope_pipe_command() -> list[str] | None:
    if getattr(sys, "frozen", False):
        exe_name = "scope_pipe.exe" if sys.platform == "win32" else "scope_pipe"
        pipe_exe = _runtime_dir() / exe_name
        if pipe_exe.exists():
            return [str(pipe_exe)]
    scope_pipe_path = SCRIPT_DIR / "scope_pipe.py"
    if scope_pipe_path.exists():
        return [sys.executable, str(scope_pipe_path)]
    return None
```

Change `_ensure_scope_pipe_running()` to call `_scope_pipe_command()` and pass
`*_scope_pipe_command()` into `asyncio.create_subprocess_exec`.

- [ ] **Step 4: Run tests**

Run:

```powershell
python -m unittest tests.test_windows_packaging_paths -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_windows_packaging_paths.py
git commit -m "feat: support frozen scope pipe startup"
```

## Task 4: Windows Desktop Launcher

**Files:**
- Create: `windows/launcher.py`
- Create: `windows/default.env`

- [ ] **Step 1: Create launcher**

Create `windows/launcher.py`:

```python
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path


APP_NAME = "MRRC FT-710"
DEFAULT_PORT = "8888"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def config_path() -> Path:
    return app_dir() / "ft710.env"


def ensure_config() -> Path:
    path = config_path()
    if not path.exists():
        default = app_dir() / "windows" / "default.env"
        if default.exists():
            path.write_text(default.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            path.write_text("FT710_WEB_HOST=127.0.0.1\nFT710_WEB_PORT=8888\nFT710_SERIAL_PORT=COM3\n", encoding="utf-8")
    return path


def load_env(path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def server_executable() -> Path:
    exe = app_dir() / "ft710-server.exe"
    if exe.exists():
        return exe
    return app_dir() / "server.py"


def build_command() -> list[str]:
    server = server_executable()
    if server.suffix.lower() == ".exe":
        return [str(server), "--no-ssl"]
    return [sys.executable, str(server), "--no-ssl"]


def main() -> int:
    cfg = ensure_config()
    env = load_env(cfg)
    port = env.get("FT710_WEB_PORT", DEFAULT_PORT)
    host = env.get("FT710_WEB_HOST", "127.0.0.1")
    url_host = "127.0.0.1" if host in ("::", "0.0.0.0", "") else host
    url = f"http://{url_host}:{port}"

    print(f"{APP_NAME}")
    print(f"Config: {cfg}")
    print(f"URL:    {url}")
    print("Close this window to stop the server.")

    proc = subprocess.Popen(build_command(), cwd=str(app_dir()), env=env)
    time.sleep(2.0)
    webbrowser.open(url)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Create default config**

Create `windows/default.env`:

```ini
FT710_SERIAL_PORT=COM3
FT710_BAUD_RATE=38400
FT710_WEB_HOST=127.0.0.1
FT710_WEB_PORT=8888
FT710_WEB_PASSWORD=changeme_please_use_strong_password!
FT710_SCOPE_PORT=
FT710_SCOPE_BAUD=115200
FT710_AUDIO_RX_DEVICE=
FT710_AUDIO_TX_DEVICE=
FT710_FTDI_LIB_DIR=vendor\ftdi\windows\bin\x64
```

- [ ] **Step 3: Run syntax check**

Run:

```powershell
python -m py_compile windows\launcher.py
```

Expected: no output and exit code 0.

- [ ] **Step 4: Commit**

```bash
git add windows/launcher.py windows/default.env
git commit -m "feat: add windows desktop launcher"
```

## Task 5: FTDI Vendor Documentation

**Files:**
- Create: `vendor/ftdi/windows/README.md`

- [ ] **Step 1: Add FTDI acquisition notes**

Create:

```markdown
# FTDI Windows Runtime Files

This directory is for Windows FTDI runtime files used by the MRRC FT-710
desktop package.

Expected files:

- `bin/x64/FT4222.dll`
- `bin/x64/ftd2xx.dll`

Official sources:

- LibFT4222: https://ftdichip.com/software-examples/ft4222h-software-examples/
- Expected archive: `LibFT4222-v1.4.8.zip`
- D2XX: https://ftdichip.com/drivers/d2xx-drivers/
- Expected archive: `CDM-v2.12.36.20-WHQL-Certified.zip`

The FTDI site may return HTTP 403 to automated direct downloads. If that
happens, download the ZIP files in a browser and place them in `downloads/`,
or extract the DLLs manually into `bin/x64/`.

Before distributing an installer, update the project license/notice file with
the applicable FTDI redistribution terms.
```

- [ ] **Step 2: Commit**

```bash
git add vendor/ftdi/windows/README.md
git commit -m "docs: document windows ftdi dll sources"
```

## Task 6: PyInstaller Specs

**Files:**
- Create: `packaging/pyinstaller/ft710_server.spec`
- Create: `packaging/pyinstaller/scope_pipe.spec`
- Create: `packaging/pyinstaller/ft710_launcher.spec`

- [ ] **Step 1: Create server spec**

Create a one-folder spec that includes `static`, config seed data, and vendor DLLs.

- [ ] **Step 2: Create scope pipe spec**

Create a one-file `scope_pipe.exe` spec including `scope_frame.py` and
`scope_libraries.py`.

- [ ] **Step 3: Create launcher spec**

Create a console launcher spec so the user can close the window to stop the
server.

- [ ] **Step 4: Build on Windows**

Run:

```powershell
pyinstaller packaging\pyinstaller\scope_pipe.spec --noconfirm
pyinstaller packaging\pyinstaller\ft710_server.spec --noconfirm
pyinstaller packaging\pyinstaller\ft710_launcher.spec --noconfirm
```

Expected: `dist\scope_pipe\scope_pipe.exe`, `dist\ft710-server\ft710-server.exe`,
and `dist\MRRC-FT710\MRRC-FT710.exe` exist.

- [ ] **Step 5: Commit**

```bash
git add packaging/pyinstaller
git commit -m "build: add pyinstaller specs"
```

## Task 7: Inno Setup Installer

**Files:**
- Create: `packaging/windows/MRRC-FT710.iss`
- Create: `packaging/windows/build.ps1`

- [ ] **Step 1: Create Inno Setup script**

Define app metadata, install files from `dist\windows\MRRC-FT710`, desktop
shortcut, Start Menu shortcut, config edit shortcut, and uninstall behavior.

- [ ] **Step 2: Create build script**

The script should:

```powershell
python -m py_compile *.py
python -m unittest discover -s tests -v
pyinstaller packaging\pyinstaller\scope_pipe.spec --noconfirm
pyinstaller packaging\pyinstaller\ft710_server.spec --noconfirm
pyinstaller packaging\pyinstaller\ft710_launcher.spec --noconfirm
iscc packaging\windows\MRRC-FT710.iss
```

- [ ] **Step 3: Commit**

```bash
git add packaging/windows
git commit -m "build: add windows installer build scripts"
```

## Task 8: Verification

**Files:**
- Modify as needed from failures found above.

- [ ] **Step 1: Local Python verification**

Run:

```bash
python -m py_compile *.py
python -m unittest discover -s tests -v
```

Expected: no syntax errors and test suite passes.

- [ ] **Step 2: Windows build verification**

On Windows 11:

```powershell
packaging\windows\build.ps1
```

Expected: `dist\windows\MRRC-FT710-Setup.exe`.

- [ ] **Step 3: Windows hardware smoke test**

Install the setup EXE, set `FT710_SERIAL_PORT=COMx`, launch app, and verify:

- `/login` loads.
- CAT state updates.
- RX/TX audio devices are detected.
- Spectrum log says `scope_pipe: first frame received`.
- Removing FT4222 DLLs produces S-meter fallback rather than a crash.

- [ ] **Step 4: Final commit**

```bash
git add .
git commit -m "build: package windows desktop app"
```

## Self-Review

- Spec coverage: launcher, bundled runtime, FTDI DLLs, frozen scope pipe, fallback mode, and installer outputs are covered.
- Placeholder scan: no `TBD` or `TODO` markers are present.
- Type consistency: helper names are consistent across tasks.
