"""Radio power command (CAT PS) guards — regression tests.

Field incident 2026-07-27/28: a PS0 landing seconds after PS1 (mid-boot)
wedged the FT-710's CAT MCU, and PS1 wake-up proved unreliable even with
retry+verify — so the web UI power switch was withdrawn (SDD V2.13).
The `power` WS command remains for the maintenance scripts
(_power_cycle*.py) with these guards under test:
  1. PS0 is rejected inside the boot window after PS1.
  2. PS1 is retried and verified by a real FA query.
  3. PS0 is refused while transmitting.
  4. PS0 is sent twice (first write occasionally lost).

Runs without hardware; `import server` follows the existing suite pattern.
"""
import asyncio
import json
import unittest
from unittest.mock import patch

import server


async def _no_sleep(_delay):
    return None


class _FakeCat:
    def __init__(self, fa_answer=True, fa_after_attempts=1):
        self.connected = True
        self.power_calls = []
        self.query_calls = 0
        self._fa_answer = fa_answer
        self._fa_after = fa_after_attempts

    async def set_power(self, on):
        self.power_calls.append(on)
        return True

    async def query(self, cmd, timeout=None):
        self.query_calls += 1
        if cmd == "FA" and self._fa_answer and len(self.power_calls) >= self._fa_after:
            return "FA007050000"
        return None


class _FakeWS:
    def __init__(self):
        self.messages = []

    async def send_text(self, text):
        self.messages.append(json.loads(text))


class PowerOnRadioTests(unittest.TestCase):
    def setUp(self):
        self._sleep_patch = patch.object(server.asyncio, "sleep", _no_sleep)
        self._sleep_patch.start()
        self._old_verify = server.POWER_ON_VERIFY_S
        server.POWER_ON_VERIFY_S = 0.01

    def tearDown(self):
        self._sleep_patch.stop()
        server.POWER_ON_VERIFY_S = self._old_verify

    def test_verified_on_first_attempt(self):
        cat = _FakeCat(fa_answer=True)
        ok = asyncio.run(server._power_on_radio(cat))
        self.assertTrue(ok)
        self.assertEqual(cat.power_calls, [True])
        self.assertGreater(cat.query_calls, 0)

    def test_retries_until_radio_answers(self):
        cat = _FakeCat(fa_answer=True, fa_after_attempts=3)
        ok = asyncio.run(server._power_on_radio(cat))
        self.assertTrue(ok)
        self.assertEqual(cat.power_calls, [True, True, True])

    def test_gives_up_after_all_attempts(self):
        cat = _FakeCat(fa_answer=False)
        ok = asyncio.run(server._power_on_radio(cat))
        self.assertFalse(ok)
        self.assertEqual(len(cat.power_calls), server.POWER_ON_ATTEMPTS)

    def test_boot_window_armed(self):
        cat = _FakeCat(fa_answer=True)
        asyncio.run(server._power_on_radio(cat))
        self.assertGreater(server._power_boot_until, 0)


class PowerSetCommandGuardTests(unittest.TestCase):
    def setUp(self):
        self._sleep_patch = patch.object(server.asyncio, "sleep", _no_sleep)
        self._sleep_patch.start()
        self._old_cat = server.cat
        self._old_boot = server._power_boot_until
        self._old_tx = server.radio.tx_status
        self._old_verify = server.POWER_ON_VERIFY_S
        server.POWER_ON_VERIFY_S = 0.01
        server.radio.update(tx_status=0)

    def tearDown(self):
        self._sleep_patch.stop()
        server.cat = self._old_cat
        server._power_boot_until = self._old_boot
        server.radio.update(tx_status=self._old_tx)
        server.POWER_ON_VERIFY_S = self._old_verify

    def _run(self, field, value, ws, cat):
        server.cat = cat
        asyncio.run(server._execute_set_command(field, value, ws))

    def test_power_off_rejected_during_boot_window(self):
        server._power_boot_until = server.time.monotonic() + 60
        cat, ws = _FakeCat(), _FakeWS()
        self._run("power", False, ws, cat)
        self.assertEqual(cat.power_calls, [])  # no PS0 sent
        errors = [m for m in ws.messages if m.get("type") == "error"]
        self.assertTrue(errors and "启动" in errors[0]["message"])

    def test_power_off_rejected_while_transmitting(self):
        server._power_boot_until = 0.0
        server.radio.update(tx_status=1)
        cat, ws = _FakeCat(), _FakeWS()
        self._run("power", False, ws, cat)
        self.assertEqual(cat.power_calls, [])
        errors = [m for m in ws.messages if m.get("type") == "error"]
        self.assertTrue(errors and "发射" in errors[0]["message"])

    def test_power_off_sent_twice_when_idle(self):
        server._power_boot_until = 0.0
        cat, ws = _FakeCat(), _FakeWS()
        self._run("power", False, ws, cat)
        self.assertEqual(cat.power_calls, [False, False])
        self.assertFalse(server.radio.power_on)

    def test_power_on_failure_reports_error(self):
        server._power_boot_until = 0.0
        cat, ws = _FakeCat(fa_answer=False), _FakeWS()
        self._run("power", True, ws, cat)
        self.assertFalse(server.radio.power_on)
        errors = [m for m in ws.messages if m.get("type") == "error"]
        self.assertTrue(errors and "无响应" in errors[0]["message"])

    def test_power_on_success_updates_state(self):
        server._power_boot_until = 0.0
        cat, ws = _FakeCat(fa_answer=True), _FakeWS()
        server.radio.update(power_on=False)
        self._run("power", True, ws, cat)
        self.assertTrue(server.radio.power_on)
        self.assertEqual([m for m in ws.messages if m.get("type") == "error"], [])


if __name__ == "__main__":
    unittest.main()
