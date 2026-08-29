# macOS 小白零配置安装包 v1.13.0 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a novice-friendly macOS installer (`MRRC-Modern-v1.13.0-arm64.dmg`) that auto-configures web password, serial port, and radio model on first launch, bundles FT4222 true-spectrum dylibs, and requires zero terminal/config editing.

**Architecture:** The macOS menu-bar launcher (`macos/launcher.py`) runs a first-run auto-config step (`macos/first_run.py`) *before* spawning the server: it generates a random web password, scans `/dev/cu.*` for a serial port, and probes each candidate port (FT-710 ASCII `ID;` at 38400 / IC-7300 CI-V 0x19 at 115200) to pick the radio model. Results are written back to the user env file; the server then sees final config and shows the generated password in a login-page banner. Packaging specs are modernized and FTDI dylibs are bundled.

**Tech Stack:** Python 3.13 (local `.venv`), PyInstaller 6.21.0, pyserial, rumps/PyObjC (menu-bar launcher), FastAPI/uvicorn server, unittest (existing suite, currently 651 tests), `hdiutil`/`codesign` for DMG.

## Global Constraints

- Version must be **v1.13.0**; `CHANGELOG.md` top heading renamed from `## [Unreleased]` accordingly.
- Radio-model probe is read-only (one ID query per port) and must **never** crash first-run if a port can't open.
- `macos/first_run.py` imports only stdlib + `serial` (pyserial is a runtime dep) — **no rumps**, so tests stay green on the Windows VM.
- Tests are `unittest` style, discovered by `python -m unittest discover -s tests` (must stay green — currently 651).
- Build script `packaging/macos/build.sh` reads the version from the top `## [vX.Y.Z]` CHANGELOG heading — do not change that mechanism.
- FT4222 dylibs go in `vendor/ftdi/macos/` (copied from `lib/`); build.sh already bundles that dir into the app and launcher sets `MRRC_FTDI_LIB_DIR`.
- No Developer ID/notarization: ad-hoc signed, first launch is right-click → Open once.
- Do not change Windows packaging.

---

### Task 1: `macos/first_run.py` auto-config module (TDD)

**Files:**
- Create: `macos/first_run.py`
- Test: `tests/test_first_run.py`

**Interfaces:**
- Produces (used by Task 3 launcher and Task 2 tests):
  - `DEFAULT_WEB_PASSWORD: str = "changeme_please_use_strong_password!"`
  - `DEFAULT_SERIAL_PORTS: tuple[str, ...] = ("/dev/cu.SLAB_USBtoUART",)`
  - `RADIO_MODEL_FALLBACK: str = "ft710"`
  - `VALID_MODELS: frozenset[str] = frozenset({"ft710", "ic7300", "ic7300mk2"})`
  - `needs_first_run(env: dict[str, str]) -> bool`
  - `generate_password() -> str`
  - `detect_serial_ports(comports: list | None = None) -> list[str]`
  - `probe_ft710(ser) -> bool`
  - `probe_ic7300(ser) -> bool`
  - `probe_radio_model(port: str, open_func=serial.Serial, timeout: float = 1.0) -> str | None`
  - `update_env_file(path: Path, updates: dict[str, str]) -> None`
  - `apply_first_run(env: dict[str, str], config_path: Path, *, open_func=serial.Serial) -> dict[str, str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_first_run.py`:

```python
"""Tests for macos.first_run first-launch auto-config."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from macos import first_run as fr


class _FakeComPort:
    def __init__(self, device, description="", hwid=""):
        self.device = device
        self.description = description
        self.hwid = hwid


class _FakeSerialFT:
    """Fake serial that answers the FT-710 ASCII ID; query."""
    def __init__(self, *a, **k):
        pass
    def reset_input_buffer(self):
        pass
    def write(self, b):
        return len(b)
    def read(self, n):
        return b"ID017;"


class _FakeSerialIC:
    """Fake serial that answers the IC-7300 CI-V 0x19 model query."""
    def __init__(self, *a, **k):
        pass
    def reset_input_buffer(self):
        pass
    def write(self, b):
        return len(b)
    def read(self, n):
        # FE FE <from=0x94> <to=0xE0> 19 <model=0x94> <cks> FD
        return bytes([0xFE, 0xFE, 0x94, 0xE0, 0x19, 0x94, 0xFD, 0xFD])


class _FakeSerialNull:
    """Fake serial that never answers."""
    def __init__(self, *a, **k):
        pass
    def reset_input_buffer(self):
        pass
    def write(self, b):
        return len(b)
    def read(self, n):
        return b""


class NeedsFirstRunTests(unittest.TestCase):
    def test_true_when_no_done_flag(self):
        self.assertTrue(fr.needs_first_run({"MRRC_WEB_PASSWORD": "x"}))

    def test_true_when_default_password(self):
        env = {"MRRC_FIRST_RUN_DONE": "1", "MRRC_WEB_PASSWORD": fr.DEFAULT_WEB_PASSWORD}
        self.assertTrue(fr.needs_first_run(env))

    def test_true_when_serial_is_default(self):
        env = {"MRRC_FIRST_RUN_DONE": "1", "MRRC_SERIAL_PORT": "/dev/cu.SLAB_USBtoUART"}
        self.assertTrue(fr.needs_first_run(env))

    def test_false_when_done_and_configured(self):
        env = {
            "MRRC_FIRST_RUN_DONE": "1",
            "MRRC_WEB_PASSWORD": "S3cret!long",
            "MRRC_SERIAL_PORT": "/dev/cu.usbserial-A1",
            "MRRC_RADIO_MODEL": "ic7300",
        }
        self.assertFalse(fr.needs_first_run(env))


class GeneratePasswordTests(unittest.TestCase):
    def test_length_and_randomness(self):
        a, b = fr.generate_password(), fr.generate_password()
        self.assertGreaterEqual(len(a), 16)
        self.assertNotEqual(a, b)


class DetectSerialPortsTests(unittest.TestCase):
    def test_cp210x_ports_sort_first(self):
        ports = [
            _FakeComPort("/dev/cu.usbserial-OTHER", "USB Serial", "USB"),
            _FakeComPort("/dev/cu.SLAB_USBtoUART", "CP210x USB to UART Bridge", "USB"),
            _FakeComPort("/dev/cu.Bluetooth-Incoming-Port", "", ""),
        ]
        result = fr.detect_serial_ports(ports)
        self.assertEqual(result[0], "/dev/cu.SLAB_USBtoUART")
        self.assertEqual(len(result), 2)  # Bluetooth port excluded (not cu-radio)

    def test_empty_when_no_ports(self):
        self.assertEqual(fr.detect_serial_ports([]), [])


class ProbeRadioModelTests(unittest.TestCase):
    def test_ft710_detected(self):
        def open_func(**kw):
            return _FakeSerialFT()
        self.assertEqual(fr.probe_radio_model("/dev/cu.SLAB_USBtoUART", open_func=open_func), "ft710")

    def test_ic7300_detected(self):
        def open_func(**kw):
            return _FakeSerialIC()
        self.assertEqual(fr.probe_radio_model("/dev/cu.usbserial-A1", open_func=open_func), "ic7300")

    def test_none_when_no_radio(self):
        def open_func(**kw):
            return _FakeSerialNull()
        self.assertIsNone(fr.probe_radio_model("/dev/cu.usbserial-A1", open_func=open_func))


class UpdateEnvFileTests(unittest.TestCase):
    def test_updates_in_place_and_appends_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mrrc_modern.env"
            path.write_text("# comment\nMRRC_WEB_PASSWORD=old\nMRRC_RADIO_MODEL=ft710\n", encoding="utf-8")
            fr.update_env_file(path, {"MRRC_WEB_PASSWORD": "new", "MRRC_FIRST_RUN_DONE": "1"})
            text = path.read_text(encoding="utf-8")
        self.assertIn("# comment", text)
        self.assertIn("MRRC_WEB_PASSWORD=new", text)
        self.assertNotIn("=old", text)
        self.assertIn("MRRC_FIRST_RUN_DONE=1", text)


class ApplyFirstRunTests(unittest.TestCase):
    def _tmp_config(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "mrrc_modern.env"
        path.write_text(body, encoding="utf-8")
        return path

    def test_generates_password_and_marks_done(self):
        path = self._tmp_config("MRRC_WEB_PASSWORD=\nMRRC_SERIAL_PORT=\nMRRC_RADIO_MODEL=\n")
        with mock.patch.object(fr, "detect_serial_ports", return_value=[]):
            env = fr.apply_first_run({}, path, open_func=lambda **kw: _FakeSerialNull())
        self.assertEqual(env["MRRC_AUTO_PASSWORD"], "1")
        self.assertEqual(env["MRRC_FIRST_RUN_DONE"], "1")
        self.assertEqual(env["MRRC_RADIO_MODEL"], "ft710")
        self.assertNotEqual(env["MRRC_WEB_PASSWORD"], "")
        text = path.read_text(encoding="utf-8")
        self.assertIn(f"MRRC_WEB_PASSWORD={env['MRRC_WEB_PASSWORD']}", text)
        self.assertIn("MRRC_FIRST_RUN_DONE=1", text)

    def test_detects_port_and_model_from_probe(self):
        path = self._tmp_config("MRRC_WEB_PASSWORD=already-set\nMRRC_SERIAL_PORT=\nMRRC_RADIO_MODEL=\n")
        with mock.patch.object(
            fr, "detect_serial_ports", return_value=["/dev/cu.SLAB_USBtoUART"]
        ):
            env = fr.apply_first_run({}, path, open_func=lambda **kw: _FakeSerialFT())
        self.assertEqual(env["MRRC_SERIAL_PORT"], "/dev/cu.SLAB_USBtoUART")
        self.assertEqual(env["MRRC_RADIO_MODEL"], "ft710")

    def test_keeps_existing_config(self):
        path = self._tmp_config(
            "MRRC_WEB_PASSWORD=S3cret!long\nMRRC_SERIAL_PORT=/dev/cu.usbserial-A1\n"
            "MRRC_RADIO_MODEL=ic7300\n"
        )
        with mock.patch.object(fr, "detect_serial_ports") as detect:
            env = fr.apply_first_run({}, path, open_func=lambda **kw: _FakeSerialNull())
        detect.assert_not_called()
        self.assertEqual(env["MRRC_RADIO_MODEL"], "ic7300")
        self.assertEqual(env["MRRC_SERIAL_PORT"], "/dev/cu.usbserial-A1")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_first_run -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'macos.first_run'`.

- [ ] **Step 3: Implement the module**

Create `macos/first_run.py`:

```python
"""macOS first-launch auto-configuration for MRRC Modern.

Runs in the menu-bar launcher BEFORE the server starts. Generates a random
web password, scans /dev/cu.* for a serial port, and probes each candidate
port to identify the radio (FT-710 ASCII CAT vs Icom CI-V). Results are
written back to the user env file so the server always sees final config.

Imports only stdlib + pyserial on purpose: tests (and the Windows VM) do
not have rumps/PyObjC.
"""
from __future__ import annotations

import secrets
import serial
import serial.tools.list_ports
from pathlib import Path

DEFAULT_WEB_PASSWORD = "changeme_please_use_strong_password!"
DEFAULT_SERIAL_PORTS = ("/dev/cu.SLAB_USBtoUART",)
RADIO_MODEL_FALLBACK = "ft710"
VALID_MODELS = frozenset({"ft710", "ic7300", "ic7300mk2"})


def needs_first_run(env: dict[str, str]) -> bool:
    """True when first-launch auto-config should run.

    Once ``MRRC_FIRST_RUN_DONE=1`` is present we never re-trigger, even if a
    later value happens to match a default.
    """
    if env.get("MRRC_FIRST_RUN_DONE") == "1":
        return False
    pwd = env.get("MRRC_WEB_PASSWORD", "")
    port = env.get("MRRC_SERIAL_PORT", "").strip()
    model = env.get("MRRC_RADIO_MODEL", "").strip().lower()
    return (
        not pwd or pwd == DEFAULT_WEB_PASSWORD
        or not port or port in DEFAULT_SERIAL_PORTS
        or model not in VALID_MODELS
    )


def generate_password() -> str:
    return secrets.token_urlsafe(16)


def detect_serial_ports(comports: list | None = None) -> list[str]:
    """Return /dev/cu.* devices, CP210x/SLAB/usbserial radios first.

    Non-serial bogus ports (Bluetooth, IRComm, debug-console) are excluded so
    they can never be picked as the radio port.
    """
    ports = list(serial.tools.list_ports.comports()) if comports is None else list(comports)
    _bogus = ("/dev/cu.Bluetooth", "/dev/cu.IRComm", "/dev/cu.debug-console")
    candidates = [
        p for p in ports
        if p.device.startswith("/dev/cu.") and not p.device.startswith(_bogus)
    ]

    def _key(p) -> tuple[int, str]:
        text = (p.description + " " + p.hwid).lower()
        if any(s in text for s in ("cp210x", "slab", "usbserial", "usb serial", "usb-serial")):
            return (0, p.device)
        return (1, p.device)

    candidates.sort(key=_key)
    return [p.device for p in candidates]


def probe_ft710(ser) -> bool:
    """True if the device answers the Yaesu ASCII ``ID;`` query."""
    ser.reset_input_buffer()
    ser.write(b"AI0;")          # stop any Auto-Information streaming
    ser.timeout = 0.3
    ser.read(256)               # drain stale stream
    ser.reset_input_buffer()
    ser.write(b"ID;")
    ser.timeout = 1.0
    return ser.read(64).startswith(b"ID")


def probe_ic7300(ser) -> bool:
    """True if the device answers the Icom CI-V 0x19 model query."""
    ser.reset_input_buffer()
    # FE FE <to=0x00 broadcast> <from=0xE0 PC> 19 <cksum=0x00^0xE0^0x19=0xF9> FD
    frame = bytes([0xFE, 0xFE, 0x00, 0xE0, 0x19, 0xF9, 0xFD])
    ser.write(frame)
    ser.timeout = 1.0
    resp = ser.read(64)
    return resp.startswith(b"\xfe\xfe") and len(resp) >= 6 and resp[4] == 0x19


def probe_radio_model(port: str, open_func=serial.Serial, timeout: float = 1.0) -> str | None:
    """Try FT-710 then IC-7300 on ``port``; return model or None."""
    for baud, probe in ((38400, probe_ft710), (115200, probe_ic7300)):
        try:
            ser = open_func(port=port, baudrate=baud, timeout=timeout)
        except Exception:
            continue
        try:
            if probe(ser):
                return "ft710" if probe is probe_ft710 else "ic7300"
        except Exception:
            continue
        finally:
            try:
                ser.close()
            except Exception:
                pass
    return None


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Rewrite KEY=VALUE lines in place, preserving comments and appending new keys."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
    pending = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in pending:
                out.append(f"{key}={pending.pop(key)}")
                continue
        out.append(line)
    for key, value in pending.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def apply_first_run(
    env: dict[str, str],
    config_path: Path,
    *,
    open_func=serial.Serial,
) -> dict[str, str]:
    """Fill password/serial/model, persist, mark done. Returns updated env."""
    updates: dict[str, str] = {}

    pwd = env.get("MRRC_WEB_PASSWORD", "")
    if not pwd or pwd == DEFAULT_WEB_PASSWORD:
        pwd = generate_password()
        env["MRRC_WEB_PASSWORD"] = pwd
        updates["MRRC_WEB_PASSWORD"] = pwd
        env["MRRC_AUTO_PASSWORD"] = "1"
        updates["MRRC_AUTO_PASSWORD"] = "1"

    port = env.get("MRRC_SERIAL_PORT", "").strip()
    model = env.get("MRRC_RADIO_MODEL", "").strip().lower()
    port_unset = not port or port in DEFAULT_SERIAL_PORTS
    model_unset = model not in VALID_MODELS

    if port_unset or model_unset:
        found_port, found_model = None, None
        for candidate in detect_serial_ports():
            found_model = probe_radio_model(candidate, open_func=open_func)
            if found_model:
                found_port = candidate
                break
        if found_model:
            port, model = found_port, found_model
        else:
            if port_unset:
                ports = detect_serial_ports()
                port = ports[0] if ports else port
            if model_unset:
                model = RADIO_MODEL_FALLBACK

    env["MRRC_SERIAL_PORT"] = port
    env["MRRC_RADIO_MODEL"] = model
    updates["MRRC_SERIAL_PORT"] = port
    updates["MRRC_RADIO_MODEL"] = model
    env["MRRC_FIRST_RUN_DONE"] = "1"
    updates["MRRC_FIRST_RUN_DONE"] = "1"

    update_env_file(config_path, updates)
    return env
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_first_run -v`
Expected: PASS (all tests green).

- [ ] **Step 5: Commit**

```bash
git add macos/first_run.py tests/test_first_run.py
git commit -m "feat: first-run auto-config (password/serial/radio) for macOS launcher"
```

---

### Task 2: `config.py` — empty radio model must not crash the server

**Files:**
- Modify: `config.py:47`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: none (independent of Task 1; protects the server from an empty `MRRC_RADIO_MODEL` env value).
- Produces: `RADIO_MODEL` falls back to `"ft710"` when the env var is unset *or* empty.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py` (add `import importlib` and `from unittest.mock import patch` at the top if not present). `config.RADIO_MODEL` is computed at module import, so reload `config` inside a patched env, then reload it again to restore module state:

```python
class RadioModelEmptyFallbackTests(unittest.TestCase):
    def test_empty_model_env_falls_back_to_ft710(self):
        import config as cfg
        with patch("os.environ", {"MRRC_RADIO_MODEL": "", "MRRC_WEB_PASSWORD": "x"}, clear=True):
            importlib.reload(cfg)
            try:
                self.assertEqual(cfg.RADIO_MODEL, "ft710")
            finally:
                importlib.reload(cfg)  # restore module globals for the rest of the suite
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_config -v`
Expected: FAIL — `RADIO_MODEL == ""`, not `"ft710"`.

- [ ] **Step 3: Implement**

In `config.py` change line 47 from:

```python
RADIO_MODEL = os.environ.get("MRRC_RADIO_MODEL", "ft710").strip().lower()
```

to:

```python
RADIO_MODEL = (os.environ.get("MRRC_RADIO_MODEL") or "ft710").strip().lower()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest tests.test_config -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "fix: empty MRRC_RADIO_MODEL env falls back to ft710 (server crash guard)"
```

---

### Task 3: `macos/launcher.py` — run first-run auto-config

**Files:**
- Modify: `macos/launcher.py` (imports, `main()`, menu)

**Interfaces:**
- Consumes: Task 1's `first_run.needs_first_run`, `first_run.apply_first_run`.
- Produces: `ensure_first_run() -> dict[str, str]` (returns env after possibly applying first-run); menu item **Show Password…**.

- [ ] **Step 1: Add imports and a testable `ensure_first_run()`**

Add near the top of `macos/launcher.py`:

```python
from macos import first_run
```

Add after `load_env()` (keep the file's existing style):

```python
def ensure_first_run() -> dict[str, str]:
    """Apply first-launch auto-config if needed and surface the password.

    Returns the (possibly updated) env dict ready to pass to the server.
    """
    cfg = config_path()
    env = load_env(cfg)
    if first_run.needs_first_run(env):
        env = first_run.apply_first_run(env, cfg)
        if env.get("MRRC_AUTO_PASSWORD") == "1":
            rumps.notification(
                APP_NAME,
                "首次运行",
                f"登录密码已自动生成：{env.get('MRRC_WEB_PASSWORD', '')}\n"
                "已存入配置，菜单栏「Show Password…」可随时查看。",
            )
    return env


def show_password() -> None:
    env = load_env(config_path())
    pwd = _env(env, "MRRC_WEB_PASSWORD", "")
    rumps.alert(
        title=APP_NAME,
        message=f"Web 登录密码：{pwd}\n（菜单栏「Edit Configuration…」可修改）",
    )
```

- [ ] **Step 2: Wire into `main()` and add the menu item**

In `main()` replace:

```python
    env = load_env(config_path())
```

with:

```python
    env = ensure_first_run()
```

(It appears once — the first `load_env` call in `main()`.)

In `MRRCModernApp.__init__` menu list, add **Show Password…** after "Edit Configuration…":

```python
        self.menu = [
            "Open Web UI",
            "Edit Configuration…",
            "Show Password…",
            "Restart Server",
            None,  # separator
            "Quit MRRC Modern",
        ]
```

Add the handler next to `on_edit`:

```python
    @rumps.clicked("Show Password…")
    def on_show_password(self, _):
        show_password()
```

- [ ] **Step 3: Syntax check**

Run: `.venv/bin/python -m py_compile macos/launcher.py macos/first_run.py`
Expected: exit 0, no output.

- [ ] **Step 4: Functional smoke of `ensure_first_run()` with rumps stubbed**

`macos/launcher.py` imports `rumps` at module level, so stub it before import:

```bash
.venv/bin/python - <<'PY'
import sys, types
rumps = types.ModuleType("rumps")
rumps.App = type("App", (), {})
rumps.clicked = lambda *a, **k: (lambda f: f)
rumps.notification = lambda *a, **k: None
rumps.alert = lambda *a, **k: None
rumps.quit_application = lambda: None
sys.modules["rumps"] = rumps

import tempfile
from pathlib import Path
import macos.launcher as L

d = Path(tempfile.mkdtemp())
p = d / "mrrc_modern.env"
p.write_text("MRRC_WEB_PASSWORD=\nMRRC_SERIAL_PORT=\nMRRC_RADIO_MODEL=\n", encoding="utf-8")
L.config_path = lambda: p
env = L.ensure_first_run()
assert env["MRRC_FIRST_RUN_DONE"] == "1"
assert env["MRRC_RADIO_MODEL"] == "ft710"
assert env["MRRC_AUTO_PASSWORD"] == "1"
assert f"MRRC_WEB_PASSWORD={env['MRRC_WEB_PASSWORD']}" in p.read_text(encoding="utf-8")
print("ensure_first_run OK")
PY
```
Expected: prints `ensure_first_run OK`. (The real GUI `rumps.notification`/alert path is verified on the Mac at Task 10.)

- [ ] **Step 5: Commit**

```bash
git add macos/launcher.py
git commit -m "feat: launcher runs first-run auto-config and exposes Show Password"
```

---

### Task 4: `server.py` — login-page password banner + `/api/setup` (TDD)

**Files:**
- Modify: `server.py` (imports, `login_page`, new `/api/setup` route + helpers)
- Test: `tests/test_server_first_run.py`

**Interfaces:**
- Consumes: Task 2's `RADIO_MODEL` (already imported); existing `_verify_auth`, `WEB_PASSWORD`.
- Produces:
  - `_first_run_password_banner() -> str` — banner HTML or `""`.
  - `_setup_status() -> dict` — status payload for `/api/setup`.
  - `GET /api/setup` (auth-gated).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_server_first_run.py`:

```python
"""Tests for first-run banner + /api/setup status in server.py."""
import os
import unittest
from unittest.mock import patch

import server
from config import SERIAL_PORT, RADIO_MODEL


class LoginBannerTests(unittest.TestCase):
    def test_banner_empty_when_not_auto_password(self):
        with patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": ""}, clear=False):
            self.assertEqual(server._first_run_password_banner(), "")

    def test_banner_contains_password_when_auto(self):
        with patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": "1"}, clear=False), \
             patch.object(server, "WEB_PASSWORD", "abc123"):
            html = server._first_run_password_banner()
        self.assertIn("abc123", html)
        self.assertIn("首次运行", html)


class SetupStatusTests(unittest.TestCase):
    def test_status_reports_current_config(self):
        with patch.dict(
            os.environ,
            {"MRRC_AUTO_PASSWORD": "1", "MRRC_FIRST_RUN_DONE": "1"},
            clear=False,
        ):
            status = server._setup_status()
        self.assertTrue(status["first_run_done"])
        self.assertTrue(status["auto_password"])
        self.assertEqual(status["radio_model"], RADIO_MODEL)
        self.assertEqual(status["serial_port"], SERIAL_PORT)
        self.assertIn("audio_rx_device", status)
        self.assertIn("audio_tx_device", status)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_server_first_run -v`
Expected: FAIL — `AttributeError: module 'server' has no attribute '_first_run_password_banner'`.

- [ ] **Step 3: Implement**

In `server.py`:

1. Extend the `from config import (` block (currently around line 28–30) to also import the audio device names:

```python
    AUDIO_RX_DEVICE, AUDIO_TX_DEVICE,
```

2. Add `import html` if not already imported (check the top of the file; `grep -n "^import html" server.py`).

3. Add these helpers near `login_page` (before `@app.get("/login", ...)`):

```python
def _first_run_password_banner() -> str:
    """One-time banner showing the auto-generated login password.

    Only rendered while MRRC_AUTO_PASSWORD=1 (set by the launcher on first
    run). Once the user edits the password and removes that marker, the
    banner disappears.
    """
    if os.environ.get("MRRC_AUTO_PASSWORD") != "1":
        return ""
    safe = html.escape(str(WEB_PASSWORD))
    return (
        '<div style="background:#2d2410;color:#fbbf24;border:1px solid #b45309;'
        'border-radius:8px;padding:12px;margin:0 0 12px;font-size:14px;'
        'text-align:center;">'
        f'🔑 首次运行已自动生成密码：<b>{safe}</b><br>'
        '<span style="color:#9ca3af;font-size:12px;">'
        '如需修改：菜单栏 MRRC Modern → Edit Configuration…</span></div>'
    )


def _setup_status() -> dict:
    """Status payload shown on the first-run welcome banner after login."""
    return {
        "first_run_done": os.environ.get("MRRC_FIRST_RUN_DONE", "") == "1",
        "auto_password": os.environ.get("MRRC_AUTO_PASSWORD", "") == "1",
        "radio_model": RADIO_MODEL,
        "serial_port": SERIAL_PORT,
        "audio_rx_device": AUDIO_RX_DEVICE,
        "audio_tx_device": AUDIO_TX_DEVICE,
    }


@app.get("/api/setup", include_in_schema=False)
async def api_setup(request: Request):
    """First-run status for the welcome banner (requires login)."""
    if not _verify_auth(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return JSONResponse(_setup_status())
```

4. In `login_page` (line ~1791), splice the banner after the `<h1>`. The page HTML is built in two places (file read at line 1795, fallback string at 1797). Unify: after computing `html_body` (either the file text or the fallback string), do:

```python
    banner = _first_run_password_banner()
    if banner and html_body:
        html_body = html_body.replace(
            "<h1>MRRC Modern</h1>", "<h1>MRRC Modern</h1>" + banner, 1
        )
    return HTMLResponse(html_body, status_code=200)
```

Restructure the existing return at lines 1795–1820 so the fallback string is assigned to `html_body` instead of returned inline, then the common splice + return runs for both branches.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_server_first_run -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_server_first_run.py
git commit -m "feat: login-page password banner + /api/setup first-run status"
```

---

### Task 5: `static/index.html` — dismissible welcome banner

**Files:**
- Modify: `static/index.html`

**Interfaces:**
- Consumes: Task 4's `GET /api/setup` (auth-gated; requires cookie set after login).
- Produces: a one-time dismissible banner in the SPA showing detected radio/serial/audio when `auto_password` is true.

- [ ] **Step 1: Add the banner markup + logic**

Insert right after `<div class="safe-area-top"></div>` (line 22):

```html
    <div id="first-run-banner" style="display:none;background:#2d2410;color:#fbbf24;
      border-bottom:1px solid #b45309;padding:10px 16px;font-size:14px;line-height:1.5;">
      <strong>已自动配置</strong>：电台 <span id="fr-model"></span> · 串口
      <span id="fr-port"></span><br>
      <span style="color:#9ca3af;font-size:12px;">登录密码见登录页或菜单栏「Show Password…」。
      本提示可关闭。</span>
      <button id="fr-close" style="margin-left:8px;background:#b45309;color:#fff;border:none;
        border-radius:6px;padding:2px 10px;cursor:pointer;">知道了</button>
    </div>
```

Just before `</body>` (line 512), add:

```html
    <script>
      (function () {
        var banner = document.getElementById("first-run-banner");
        if (!banner || localStorage.getItem("mrrc_welcome_dismissed")) return;
        fetch("/api/setup")
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (s) {
            if (!s || !s.auto_password) return;
            document.getElementById("fr-model").textContent = s.radio_model || "—";
            document.getElementById("fr-port").textContent = s.serial_port || "自动";
            banner.style.display = "block";
          })
          .catch(function () {});
        document.getElementById("fr-close").addEventListener("click", function () {
          banner.style.display = "none";
          localStorage.setItem("mrrc_welcome_dismissed", "1");
        });
      })();
    </script>
```

- [ ] **Step 2: Verify the file parses**

Run: `.venv/bin/python -c "from pathlib import Path; t=Path('static/index.html').read_text(); assert t.count('<div id=\"first-run-banner\"')==1; assert t.count('mrrc_welcome_dismissed')==2; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit**

```bash
git add static/index.html
git commit -m "feat: first-run welcome banner in web UI"
```

---

### Task 6: PyInstaller specs — platform-aware server datas + launcher serial imports

**Files:**
- Modify: `packaging/pyinstaller/mrrc_modern_server.spec`
- Modify: `packaging/macos/mrrc_modern_launcher.spec`

**Interfaces:**
- Consumes: nothing new; enables bundling of `macos/first_run.py`, `macos/default.env`, and `serial.tools.list_ports`.
- Produces: a macOS `.app` whose launcher can import `first_run` + `serial`, and whose server carries the correct `default.env`.

- [ ] **Step 1: Server spec — platform-aware `default.env` + list_ports hiddenimports**

In `packaging/pyinstaller/mrrc_modern_server.spec`:

Replace the datas line (line 42):

```python
        (str(ROOT / "windows" / "default.env"), "windows"),
```

with a platform conditional before the `Analysis(...)` call (next to the existing `_vendor_data` block):

```python
# default.env: the launcher seeds the user env from the platform-appropriate
# template (windows/ for Win, macos/ for macOS). Do not ship the other
# platform's template.
if sys.platform == "darwin":
    _config_env_data = [(str(ROOT / "macos" / "default.env"), "macos")]
else:
    _config_env_data = [(str(ROOT / "windows" / "default.env"), "windows")]
```

and in `datas=[...]` use `*_config_env_data,` in place of the removed line.

Add to the `hiddenimports=[...]` list (after `"serial",`):

```python
        "serial.tools.list_ports",
        "serial.tools.list_ports_osx",
```

- [ ] **Step 2: Launcher spec — add serial + first_run + pathex**

In `packaging/macos/mrrc_modern_launcher.spec`:

- Change `pathex=[str(ROOT)]` to `pathex=[str(ROOT), str(ROOT / "macos")]`.
- In `hiddenimports`, add:

```python
        "serial",
        "serial.tools.list_ports",
        "serial.tools.list_ports_osx",
        "first_run",
```

- [ ] **Step 3: Validate both specs parse**

Run: `.venv/bin/python -m PyInstaller.utils.cliutils.spec` (or `python -m PyInstaller packaging/...` with a no-op) — simplest:

```bash
.venv/bin/python - <<'PY'
import ast
for f in ["packaging/pyinstaller/mrrc_modern_server.spec",
          "packaging/macos/mrrc_modern_launcher.spec"]:
    ast.parse(open(f).read())
    print(f, "OK")
PY
```
Expected: both print `OK`.

- [ ] **Step 4: Commit**

```bash
git add packaging/pyinstaller/mrrc_modern_server.spec packaging/macos/mrrc_modern_launcher.spec
git commit -m "build: platform-aware server datas; launcher bundles serial + first_run"
```

---

### Task 7: `macos/default.env` + FT4222 dylibs into `vendor/ftdi/macos/`

**Files:**
- Modify: `macos/default.env`
- Create: `vendor/ftdi/macos/libft4222.dylib`, `vendor/ftdi/macos/libftd2xx.dylib`

**Interfaces:**
- Consumes: Task 1 (empty values trigger auto-config).
- Produces: a first-run env template that intentionally leaves password/serial/model blank so `apply_first_run` fills them.

- [ ] **Step 1: Edit `macos/default.env`**

Set the three auto-config lines to empty and note the behavior:

```ini
# ── Radio Model ─────────────────────────────────────────────────────
# 留空 = 首次启动自动探测（FT-710 ASCII CAT / Icom CI-V）。
# 也可手动指定: ft710 / ic7300 / ic7300mk2
MRRC_RADIO_MODEL=

# ── Serial ──────────────────────────────────────────────────────────
# 留空 = 首次启动自动扫描 /dev/cu.*。
MRRC_SERIAL_PORT=
```

and:

```ini
MRRC_WEB_PASSWORD=
```

Keep every other key unchanged (`MRRC_BAUD_RATE=38400`, `MRRC_WEB_HOST=127.0.0.1`, `MRRC_WEB_PORT=8888`, `MRRC_SCOPE_PORT=`, `MRRC_SCOPE_BAUD=115200`, audio device lines, `MRRC_FTDI_LIB_DIR=vendor/ftdi/macos`). Add a comment above the password:

```ini
# 留空 = 首次启动自动生成随机密码（登录页横幅 + 菜单栏 Show Password… 可见）。
```

- [ ] **Step 2: Copy the FT4222 dylibs**

```bash
mkdir -p vendor/ftdi/macos
cp lib/libft4222.dylib vendor/ftdi/macos/
cp lib/libftd2xx.dylib vendor/ftdi/macos/
file vendor/ftdi/macos/*.dylib   # confirm: Mach-O universal (x86_64 arm64)
```

Expected: both files list `arm64` (universal).

- [ ] **Step 3: Commit**

```bash
git add macos/default.env vendor/ftdi/macos/libft4222.dylib vendor/ftdi/macos/libftd2xx.dylib
git commit -m "feat: first-run default.env auto-config + bundle FT4222 true-spectrum dylibs"
```

---

### Task 8: CHANGELOG → v1.13.0 + docs

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/MACOS_INSTALLER_GUIDE.md`
- Modify: `mac_pack.md`

**Interfaces:**
- Consumes: everything above.
- Produces: version heading `## [v1.13.0]` (build.sh reads it) and novice-facing docs.

- [ ] **Step 1: Rename the CHANGELOG heading + add macOS section**

Change line 5 from:

```markdown
## [Unreleased] — 2026-08-26 — Security & resilience hardening (SDD V2.28)
```

to:

```markdown
## [v1.13.0] — 2026-08-29 — macOS zero-config installer + security hardening (SDD V2.28)
```

Inside that section, add a `### macOS Installer` block (place it before `### Tests`):

```markdown
### macOS Installer
- **First-run zero-config**: the menu-bar launcher auto-generates a random web
  password, scans `/dev/cu.*` for the radio's serial port, and probes FT-710
  (ASCII `ID;`) vs Icom CI-V (0x19) to pick the radio model — no terminal, no
  config editing. The generated password is shown in a login-page banner and a
  menu-bar **Show Password…** item.
- **FT4222 true spectrum bundled**: `libft4222.dylib` / `libftd2xx.dylib` now
  ship inside the installer, so FT-710 gets a real FFT waterfall out of the box.
- Ad-hoc signed (no Developer ID) — first launch is right-click → Open once.
- Package renamed to `MRRC-Modern-v1.13.0-arm64.dmg`.
```

- [ ] **Step 2: Rewrite `docs/MACOS_INSTALLER_GUIDE.md` for novices**

Replace the whole file with a novice-oriented guide. Required structure and content:

```markdown
# macOS 安装使用说明（MRRC Modern v1.13.0）

本页面面向第一次使用 MRRC Modern 的用户 —— 不需要会命令行，不需要改文件。

## 一、安装（约 1 分钟）
1. 打开下载的 `MRRC-Modern-v1.13.0-arm64.dmg`。
2. 把 **MRRC Modern** 图标拖进 **应用程序** 文件夹。
3. 弹出磁盘后，到「应用程序」里找到 **MRRC Modern**。

## 二、首次打开
1. 首次打开：在「应用程序」里 **右键点击** MRRC Modern → **打开** → 再点 **打开**。
   （只有第一次需要这样，之后双击即可。这是 macOS 对未认证 App 的正常保护。）
2. 菜单栏（屏幕右上角）出现 **MRRC Modern :8888** 图标，浏览器自动打开登录页。

## 三、登录（零配置）
1. 登录页会显示一行橙色提示：**「首次运行已自动生成密码：XXXX」**。
2. 输入这个密码，点 **登录**，就能用了。
3. 忘了密码？点菜单栏 **MRRC Modern** 图标 → **Show Password…** 即可查看。

## 四、连电台（即插即用）
- **FT-710**：USB 线插上 Mac 即自动识别，真 FFT 频谱开箱即用。
- **IC-7300 / IC-7300MK2**：USB 线插上自动识别；频谱走 CI-V。
- 若同时插了多个串口设备，程序会优先选 CP210x/USB 串口；可到菜单栏
  **Edit Configuration…** 里确认 `MRRC_SERIAL_PORT`。

## 五、常用操作
- 浏览器再次打开控制页：菜单栏图标 → **Open Web UI**。
- 退出：菜单栏图标 → **Quit MRRC Modern**（先松开 PTT）。
- 改配置/密码：菜单栏图标 → **Edit Configuration…**（改后点 **Restart Server**）。

## 常见问题
| 问题 | 解决 |
|------|------|
| 提示"无法打开，因为无法验证开发者" | 右键 → 打开 → 再点打开（一次性） |
| 登录页没显示密码 | 点菜单栏图标 → Show Password… |
| 电台没反应 | 确认 USB 已插；菜单栏 → Edit Configuration… 看 `MRRC_SERIAL_PORT` |
| 频谱是假的 | 确认 `MRRC_FTDI_LIB_DIR=vendor/ftdi/macos` 且安装的是 v1.13.0 |
```

- [ ] **Step 3: Update `mac_pack.md`**

Edit these points in `mac_pack.md`:
- Header line: 构建 `MRRC-Modern-<ver>-arm64.dmg`（最新 v1.13.0）。
- §2.1: Python 3.13（本机 `.venv` 即 3.13）；`requirements-build.txt` 已锁 `pyinstaller==6.21.0` + rumps。
- §2.2: FTDI dylib 已就位 `vendor/ftdi/macos/`（自 `lib/` 拷贝，universal arm64），随包分发。
- §3 Step 0: 版本来源 CHANGELOG 顶部 `## [vX.Y.Z]`（顶部允许存在 `[Unreleased]`，grep -m1 会取到下一个已命名版本）。
- §3 之后新增一小节「首启自动配置」：`macos/first_run.py` 逻辑简述 + 冒烟验证（首次运行弹密码通知、登录页横幅、Show Password…）。
- 故障排查表补充：`MRRC_RADIO_MODEL` 空值安全回落 ft710（config.py）；探测不打断服务。

- [ ] **Step 4: Sanity-check version extraction**

Run: `grep -m1 -oE '## \[v[0-9]+\.[0-9]+\.[0-9]+\]' CHANGELOG.md`
Expected: `## [v1.13.0]`

- [ ] **Step 5: Commit**

```bash
git add CHANGELOG.md docs/MACOS_INSTALLER_GUIDE.md mac_pack.md
git commit -m "docs: v1.13.0 macOS novice installer changelog + guides"
```

---

### Task 9: Full test suite green

- [ ] **Step 1: Run the full suite**

Run: `.venv/bin/python -m unittest discover -s tests 2>&1 | tail -5`
Expected: `OK` (651 + the new tests; no failures).

- [ ] **Step 2: Commit any fixups**

If anything fails, fix it in the same style as the existing tests and commit.

---

### Task 10: Install build deps, build the DMG, verify

**Files:**
- Consumes: all prior tasks.
- Produces: `dist/macos/MRRC-Modern-v1.13.0-arm64.dmg`

- [ ] **Step 1: Install build-only deps into `.venv`**

Run:
```bash
.venv/bin/python -m pip install -r packaging/macos/requirements-build.txt
```
Expected: `pyinstaller==6.21.0` and `rumps` installed.

- [ ] **Step 2: Run the macOS build**

Run (from repo root):
```bash
PYTHON=.venv/bin/python packaging/macos/build.sh
```
Expected: syntax check → tests pass → 3 PyInstaller specs → `.app` assembled → ad-hoc codesign → `dist/macos/MRRC-Modern-v1.13.0-arm64.dmg` → size + MD5 + SHA-256 printed.

- [ ] **Step 3: Verify the artifact**

```bash
ls -lh dist/macos/MRRC-Modern-v1.13.0-arm64.dmg
codesign -dv dist/macos/MRRC-Modern.app 2>&1 | head -3
test -f dist/macos/MRRC-Modern.app/Contents/MacOS/vendor/ftdi/macos/libft4222.dylib && echo "FTDI bundled: yes"
test -f dist/macos/MRRC-Modern.app/Contents/MacOS/macos/default.env && echo "default.env bundled: yes"
test -f dist/macos/MRRC-Modern.app/Contents/MacOS/_internal/static/index.html && echo "static bundled: yes"
```

- [ ] **Step 4: Mount and smoke-test (optional; real radio needed for full check)**

```bash
hdiutil attach dist/macos/MRRC-Modern-v1.13.0-arm64.dmg
ls "/Volumes/MRRC Modern/"
hdiutil detach "/Volumes/MRRC Modern"
```

- [ ] **Step 5: Commit nothing further unless docs/artifacts changed and should be tracked**

The DMG is build output (check `.gitignore` for `dist/`); do not commit it unless the repo tracks dist.

---

## Self-Review Notes

- **Spec coverage:** §3.1 (first_run.py) → Task 1; §3.2 (launcher) → Task 3; §3.3 (server banner + /api/setup) → Tasks 4–5; §3.4 (specs, default.env, FTDI) → Tasks 6–7; §3.5 (docs) → Task 8; §3.6 (version) → Task 8; §5 (tests) → Tasks 1/4/9; §7 (acceptance: DMG + novice path + FTDI) → Task 10.
- **Placeholders:** none — every step carries concrete code/commands.
- **Type consistency:** `needs_first_run`/`apply_first_run`/`detect_serial_ports`/`probe_radio_model` signatures match between Task 1 (definition) and Task 3 (consumers). `_first_run_password_banner`/`_setup_status` defined in Task 4 and consumed by Task 5's JS via the `/api/setup` route. `RADIO_MODEL`/`SERIAL_PORT` imported in Task 4 tests from `config`.
