"""The two defects a real box found on 2026-11-01 (BG7MWR, W103D + FT-991A).

Both are "the product looked fine and was not":

1. A customer box shipped with two truncated wheel binaries, so
   ``import scipy.signal`` raised and the FT8 composition root was skipped — on
   50 of 50 startups, while the UI looked healthy.  ``/api/ft8/*`` answered an
   unhandled ``RuntimeError`` per request instead of a 503.
2. The FT-991A answers at 4800 baud, where a Yaesu query costs ~110 ms; the
   poll tiers were tuned for 38400 and saturated the port (measured: the 100 ms
   tier actually ran at 3.0 loops/s).  Poll intervals now scale with the link.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_verify_venv() -> Any:
    """Load dev_tools/verify_venv.py by path (dev_tools is not a package)."""
    path = REPO_ROOT / "dev_tools" / "verify_venv.py"
    spec = importlib.util.spec_from_file_location("verify_venv_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── /api/ft8 answers 503 when the composition root is absent ─────────────


class _FakeRequest:
    """Just enough of ``starlette.Request``: ``request.app.state``.

    ``get_state`` reads ``request.app.state.ft8_state`` (the late-bound proxy)
    and falls back to ``request.app.state.app_state``, so the fake has to carry
    that exact shape — a plain object standing in for the app state container.
    """

    def __init__(self, container):
        self.app = type("App", (), {"state": container})()


class _Container:
    """The two attributes ``get_state`` looks for on ``app.state``."""

    ft8_state: Any = None
    app_state: Any = None


def _container(ft8_state=None, app_state=None):
    holder = _Container()
    if ft8_state is not None:
        holder.ft8_state = ft8_state
    if app_state is not None:
        holder.app_state = app_state
    return holder


class _LateBoundStandIn:
    """Same contract as server._LateBound, without importing the server."""

    def __init__(self):
        self._impl: Any = None

    @property
    def bound(self):
        return self._impl is not None

    def __getattr__(self, name):
        if self._impl is None:
            raise RuntimeError("FT8 API state not bound yet")
        return getattr(self._impl, name)


class Ft8UnboundStateTests(unittest.TestCase):
    def test_unbound_state_is_a_503_not_a_runtime_error(self):
        from ft8.web import ft8_api

        proxy = _LateBoundStandIn()
        request = _FakeRequest(_container(ft8_state=proxy))
        with self.assertRaises(ft8_api._HttpReject) as caught:
            ft8_api.get_state(request)  # type: ignore[arg-type]
        response = caught.exception.args[0]
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body)["reason"],
                         ft8_api.REASON_UNAVAILABLE)

    def test_a_bound_state_passes_through(self):
        from ft8.web import ft8_api

        proxy = _LateBoundStandIn()
        proxy._impl = object()
        request = _FakeRequest(_container(ft8_state=proxy))
        self.assertIs(ft8_api.get_state(request), proxy)  # type: ignore[arg-type]

    def test_a_plain_state_object_is_untouched(self):
        """The test harness and the desktop app pass a real AppState directly."""
        from ft8.web import ft8_api

        state = object()
        request = _FakeRequest(_container(app_state=state))
        self.assertIs(ft8_api.get_state(request), state)  # type: ignore[arg-type]


# ── poll pacing follows the CAT link speed ────────────────────────────────


class _FakeCat:
    baudrate: Any = 38400

    def __init__(self, baudrate: Any = 38400):
        if baudrate is not None:
            self.baudrate = baudrate


class BaudPacingTests(unittest.TestCase):
    def _scheduler(self, baudrate: Any = 38400, idle: int = 1):
        from poll_scheduler import PollScheduler
        from radio_state import RadioState

        scheduler = PollScheduler(_FakeCat(baudrate), RadioState())  # type: ignore[arg-type]
        scheduler._idle_multiplier = idle
        return scheduler

    def test_reference_speed_is_byte_for_byte_unchanged(self):
        """38400 (FT-710, IC-7300 at 115200) must not move at all."""
        scheduler = self._scheduler(38400)
        self.assertEqual(scheduler._pace_factor(), 1.0)
        self.assertAlmostEqual(scheduler._interval(0.1), 0.1)

    def test_a_faster_link_is_also_unscaled(self):
        self.assertEqual(self._scheduler(115200)._pace_factor(), 1.0)

    def test_19200_doubles_the_interval(self):
        self.assertEqual(self._scheduler(19200)._pace_factor(), 2.0)
        self.assertAlmostEqual(self._scheduler(19200)._interval(0.1), 0.2)

    def test_9600_quadruples_the_interval(self):
        self.assertEqual(self._scheduler(9600)._pace_factor(), 4.0)

    def test_4800_hits_the_cap_instead_of_running_away(self):
        """38400/4800 is 8×; the cap keeps the settings tier usable."""
        self.assertEqual(self._scheduler(4800)._pace_factor(), 6.0)
        self.assertAlmostEqual(self._scheduler(4800)._interval(2.0), 12.0)

    def test_idle_multiplier_still_composes(self):
        scheduler = self._scheduler(9600, idle=4)
        self.assertAlmostEqual(scheduler._interval(0.1), 1.6)

    def test_a_controller_without_baudrate_is_treated_as_fast(self):
        """Every existing scheduler test injects a bare fake CAT."""
        from poll_scheduler import PollScheduler
        from radio_state import RadioState

        scheduler = PollScheduler(_FakeCat(None), RadioState())  # type: ignore[arg-type]
        self.assertEqual(scheduler._pace_factor(), 1.0)

    def test_junk_baudrate_does_not_raise(self):
        from poll_scheduler import PollScheduler
        from radio_state import RadioState

        cat = _FakeCat()
        cat.baudrate = "not-a-number"
        self.assertEqual(
            PollScheduler(cat, RadioState())._pace_factor(),  # type: ignore[arg-type]
            1.0)

    def test_the_transition_is_announced_once(self):
        """A 4800-baud link says so in the log — it is the operator's fix."""
        with self.assertLogs("mrrc.poll", level="WARNING") as logs:
            scheduler = self._scheduler(4800)
            scheduler._interval(0.1)
            scheduler._interval(0.1)
            scheduler._interval(0.1)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("4800", logs.records[0].getMessage())
        self.assertIn("CAT RATE", logs.records[0].getMessage())


# ── venv integrity: the defect that ate FT8 ──────────────────────────────


def _record_line(rel_path: str, digest: str) -> str:
    return f"{rel_path},sha256={digest},12\n"


class VerifyVenvTests(unittest.TestCase):
    def _site_packages(self, tmp: str, *, damage: bool = False) -> str:
        """A miniature site-packages: one dist, one good file, one binary."""
        import base64
        import hashlib

        site = os.path.join(tmp, "site-packages")
        os.makedirs(os.path.join(site, "pkg"), exist_ok=True)
        os.makedirs(os.path.join(site, "pkg", "__pycache__"), exist_ok=True)
        good = b"print('hello')\n"
        binary = b"\x7fELF" + b"\x00" * 64
        with open(os.path.join(site, "pkg", "__init__.py"), "wb") as handle:
            handle.write(good)
        with open(os.path.join(site, "pkg", "fast.so"), "wb") as handle:
            handle.write(binary if not damage else binary[:8])
        # A regenerated .pyc: its bytes legitimately differ from the wheel's.
        with open(os.path.join(site, "pkg", "__pycache__", "x.pyc"), "wb") as handle:
            handle.write(b"regenerated")

        def digest(blob: bytes) -> str:
            return base64.urlsafe_b64encode(hashlib.sha256(blob).digest()).rstrip(b"=").decode()

        info = os.path.join(site, "pkg-1.2.3.dist-info")
        os.makedirs(info, exist_ok=True)
        with open(os.path.join(info, "RECORD"), "w", encoding="utf-8") as handle:
            handle.write(_record_line("pkg/__init__.py", digest(good)))
            handle.write(_record_line("pkg/fast.so", digest(binary)))
            handle.write(_record_line("pkg/__pycache__/x.pyc", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"))
        return site

    def _verify(self, site: str, *args) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "dev_tools" / "verify_venv.py"),
             "--path", site, *args],
            capture_output=True, text=True, check=False,
        )

    def test_a_clean_tree_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._verify(self._site_packages(tmp))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)

    def test_a_truncated_binary_is_reported(self):
        """The field case: ELF magic intact, tail missing, pip said success."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._verify(self._site_packages(tmp, damage=True))
        self.assertEqual(result.returncode, 1)
        self.assertIn("pkg/fast.so", result.stderr)
        self.assertIn("hash mismatch", result.stderr)
        self.assertIn("--repair", result.stderr)

    def test_regenerated_pycache_is_not_damage(self):
        """A .pyc the interpreter re-wrote must not make every run dirty."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._verify(self._site_packages(tmp))
        self.assertNotIn("__pycache__", result.stderr)

    def test_a_missing_file_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = self._site_packages(tmp)
            os.unlink(os.path.join(site, "pkg", "fast.so"))
            result = self._verify(site)
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing", result.stderr)

    def test_missing_site_packages_is_a_usage_error(self):
        result = self._verify("/nonexistent/site-packages")
        self.assertEqual(result.returncode, 2)

    def test_damaged_distributions_names_each_package_once(self):
        verify_venv = _load_verify_venv()
        damage = [
            verify_venv.Damage("numpy", "2.2.6", "a.so", "hash mismatch"),
            verify_venv.Damage("numpy", "2.2.6", "b.so", "hash mismatch"),
            verify_venv.Damage("scipy", "1.14.1", "c.so", "missing"),
        ]
        self.assertEqual(verify_venv.damaged_distributions(damage),
                         [("numpy", "2.2.6"), ("scipy", "1.14.1")])

    def test_the_installer_gate_runs_both_checks(self):
        """install.sh must fail an image whose venv is damaged.

        The defect was invisible to `import numpy`, so the install gate has to
        carry the RECORD check and the lazily-imported scipy path — otherwise a
        broken wheel ships and only a customer notices.
        """
        text = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("dev_tools/verify_venv.py", text)
        self.assertIn("import scipy.signal, numpy.random", text)


if __name__ == "__main__":
    unittest.main()
