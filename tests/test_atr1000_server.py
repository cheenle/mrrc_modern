"""
ATR1000 Server-Integration Tests
================================
Covers the server.py side of the optional ATR1000 tuner feature:

- Linkage hooks in _broadcast_state (freq → notify_freq, TX → notify_tx),
  including the disabled no-op path (atr is None).
- The server-side tune-assist state machine (skipped / success / rollback /
  no-meter-data) with fake cat + tuner, verifying the carrier is always
  dropped.
- Source-level guards: /WSatr1000 auth + disabled close, fullState flag,
  lazy import in lifespan, config default.

Runs without hardware; `import server` follows the existing suite pattern
(test_memory_recall, test_windows_packaging_paths).
"""
import asyncio
import time
import unittest
from pathlib import Path

import server

SERVER_SOURCE = Path("server.py").read_text(encoding="utf-8")
CONFIG_SOURCE = Path("config.py").read_text(encoding="utf-8")


class _FakeCat:
    def __init__(self):
        self.connected = True
        self.tune_calls = []

    async def set_tune(self, on):
        self.tune_calls.append(bool(on))


class _FakeATR:
    """Fake tuner with the ATR1000Client interface used by server.py."""

    def __init__(self, swr_before=1.2, swr_after=None, meter_fresh=True):
        self.swr_before = swr_before
        self.swr_after = swr_after if swr_after is not None else swr_before
        self.meter_fresh = meter_fresh
        self._tune_done = False
        self.tune_modes = []
        self.set_relay_calls = []
        self.relay = (0, 3, 32)

    def read_state(self):
        swr = self.swr_after if self._tune_done else self.swr_before
        return {
            "connected": True,
            "power": 20,
            "swr": swr,
            "sw": self.relay[0],
            "ind": self.relay[1],
            "cap": self.relay[2],
            "tuning": False,
            "tx": True,
            "last_update": time.time() if self.meter_fresh else 0,
        }

    async def start_tune(self, mode=2):
        self.tune_modes.append(mode)
        self._tune_done = True

    async def set_relay(self, sw, ind, cap):
        self.set_relay_calls.append((sw, ind, cap))


class _FakeStorage:
    def __init__(self):
        self.learn_calls = []

    def learn(self, freq, sw, ind, cap, swr, force_update=False):
        self.learn_calls.append((freq, sw, ind, cap, swr, force_update))
        return True


class _TuneAssistTestBase(unittest.TestCase):
    """Run _atr_tune_assist with fakes and shrunk timing constants."""

    def setUp(self):
        self._saved = {
            "cat": server.cat,
            "atr": server.atr,
            "storage": server._atr_storage,
            "settle": server.ATR_TUNE_CARRIER_SETTLE_S,
            "min": server.ATR_TUNE_MIN_S,
            "deadline": server.ATR_TUNE_DEADLINE_S,
            "compare": server.ATR_TUNE_COMPARE_SETTLE_S,
            "meter": server.ATR_TUNE_METER_WAIT_S,
        }
        server.ATR_TUNE_CARRIER_SETTLE_S = 0.01
        server.ATR_TUNE_MIN_S = 0.01
        server.ATR_TUNE_DEADLINE_S = 5.0
        server.ATR_TUNE_COMPARE_SETTLE_S = 0.01
        server.ATR_TUNE_METER_WAIT_S = 0.05
        self.cat = _FakeCat()
        server.cat = self.cat

    def tearDown(self):
        server.cat = self._saved["cat"]
        server.atr = self._saved["atr"]
        server._atr_storage = self._saved["storage"]
        server.ATR_TUNE_CARRIER_SETTLE_S = self._saved["settle"]
        server.ATR_TUNE_MIN_S = self._saved["min"]
        server.ATR_TUNE_DEADLINE_S = self._saved["deadline"]
        server.ATR_TUNE_COMPARE_SETTLE_S = self._saved["compare"]
        server.ATR_TUNE_METER_WAIT_S = self._saved["meter"]
        server.radio.update(tx_status=0)

    def _run(self, fake_atr, fake_storage=None):
        server.atr = fake_atr
        server._atr_storage = fake_storage
        asyncio.run(server._atr_tune_assist())


class TuneAssistSkippedTests(_TuneAssistTestBase):
    def test_low_swr_skips_tune_and_drops_carrier(self):
        fake = _FakeATR(swr_before=1.2)
        self._run(fake)
        self.assertEqual(fake.tune_modes, [])          # never tuned
        self.assertEqual(fake.set_relay_calls, [])     # no rollback
        self.assertEqual(self.cat.tune_calls, [True, False])  # carrier on→off
        self.assertEqual(server.radio.tx_status, 0)


class TuneAssistSuccessTests(_TuneAssistTestBase):
    def test_improved_swr_keeps_result_and_learns(self):
        fake = _FakeATR(swr_before=2.5, swr_after=1.4)
        storage = _FakeStorage()
        self._run(fake, storage)
        self.assertEqual(fake.tune_modes, [2])         # full tune
        self.assertEqual(fake.set_relay_calls, [])     # kept, no rollback
        self.assertEqual(self.cat.tune_calls, [True, False])
        self.assertEqual(len(storage.learn_calls), 1)
        freq, sw, ind, cap, swr, force = storage.learn_calls[0]
        self.assertEqual((sw, ind, cap), (0, 3, 32))
        self.assertAlmostEqual(swr, 1.4)
        self.assertTrue(force)


class TuneAssistRollbackTests(_TuneAssistTestBase):
    def test_unimproved_swr_rolls_back_relays(self):
        fake = _FakeATR(swr_before=2.5, swr_after=2.6)
        storage = _FakeStorage()
        self._run(fake, storage)
        self.assertEqual(fake.tune_modes, [2])
        self.assertEqual(fake.set_relay_calls, [(0, 3, 32)])  # snapshot restored
        self.assertEqual(storage.learn_calls, [])             # nothing learned
        self.assertEqual(self.cat.tune_calls, [True, False])


class TuneAssistNoMeterTests(_TuneAssistTestBase):
    def test_stale_meter_aborts_but_still_drops_carrier(self):
        fake = _FakeATR(swr_before=2.5, meter_fresh=False)
        self._run(fake)
        self.assertEqual(fake.tune_modes, [])
        self.assertEqual(self.cat.tune_calls, [True, False])


class LinkageHookTests(unittest.TestCase):
    """_broadcast_state linkage hooks: no-op when disabled, fire when enabled."""

    def tearDown(self):
        server.atr = None
        server.radio.update(tx_status=0)

    def test_hooks_noop_when_disabled(self):
        server.atr = None
        server.radio.update(vfo_a_freq=7_100_000)
        asyncio.run(server._broadcast_state())  # must not raise

    def test_hooks_fire_on_freq_and_tx_change(self):
        calls = {"freq": [], "tx": []}

        class FakeNotify:
            def notify_freq(self, f):
                calls["freq"].append(f)

            def notify_tx(self, on):
                calls["tx"].append(on)

        server.atr = FakeNotify()
        server.radio.update(vfo_a_freq=7_074_000, active_vfo="A")
        server.radio.update(tx_status=1)
        asyncio.run(server._broadcast_state())
        self.assertIn(7_074_000, calls["freq"])
        self.assertIn(True, calls["tx"])

    def test_hook_exception_is_contained(self):
        class BadATR:
            def notify_freq(self, f):
                raise RuntimeError("boom")

        server.atr = BadATR()
        server.radio.update(vfo_a_freq=14_074_000)
        asyncio.run(server._broadcast_state())  # exception must not escape


class SourceGuardTests(unittest.TestCase):
    """Source-level assertions for the optional-feature guards."""

    def test_fullstate_includes_enabled_flag(self):
        self.assertIn('"atr1000Enabled": atr is not None', SERVER_SOURCE)

    def test_ws_endpoint_auth_and_disabled_close(self):
        ep = SERVER_SOURCE.split('@app.websocket("/WSatr1000")', 1)[1]
        self.assertIn('code=4001', ep)
        self.assertIn('code=4000', ep)
        self.assertIn('"ATR1000 disabled"', ep)

    def test_lazy_import_in_lifespan(self):
        lifespan = SERVER_SOURCE.split("async def lifespan", 1)[1]
        self.assertIn("from atr1000_client import ATR1000Client", lifespan)
        self.assertIn("if ATR1000_HOST:", lifespan)
        # Startup priming: current freq/TX pushed to the tuner at boot
        self.assertIn("atr.notify_freq(radio.active_freq)", lifespan)
        self.assertIn("atr.notify_tx(bool(radio.is_transmitting))", lifespan)

    def test_config_default_disabled(self):
        self.assertIn('_env("MRRC_ATR1000_HOST"', CONFIG_SOURCE)
        self.assertIn('_env_int("MRRC_ATR1000_PORT"', CONFIG_SOURCE)

    def test_store_path_env_override_and_frozen_launcher(self):
        tuner_src = Path("atr1000_tuner.py").read_text(encoding="utf-8")
        self.assertIn("_env(", tuner_src)
        self.assertIn("'MRRC_ATR1000_STORE'", tuner_src)
        launcher_src = Path("windows/launcher.py").read_text(encoding="utf-8")
        self.assertIn('env.setdefault("MRRC_ATR1000_STORE"', launcher_src)
        spec_src = Path("packaging/pyinstaller/mrrc_modern_server.spec").read_text(encoding="utf-8")
        self.assertIn('"atr1000_client"', spec_src)
        self.assertIn('"atr1000_tuner"', spec_src)

    def test_frontend_module_inert_unless_enabled(self):
        src = Path("static/modules/atr1000.js").read_text(encoding="utf-8")
        self.assertIn("enabled = atrEnabled === true;", src)
        self.assertIn("if (!enabled) return;", src)
        main_src = Path("static/ft710_main.js").read_text(encoding="utf-8")
        self.assertIn("window.ATR1000.init(msg.atr1000Enabled === true);", main_src)
        index_src = Path("static/index.html").read_text(encoding="utf-8")
        self.assertIn('id="atr-row" hidden', index_src)


if __name__ == "__main__":
    unittest.main()
