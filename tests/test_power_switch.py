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
import time
import unittest
from unittest.mock import patch

import server

_REAL_SLEEP = asyncio.sleep


async def _no_sleep(_delay):
    return None


async def _yield_sleep(_delay):
    """Yield to the event loop without waiting.

    A no-op ``async def`` is *not* a suspension point, so a loop built on it
    monopolises the event loop and can never be cancelled — which is exactly
    what the rescue-cancellation tests have to exercise.
    """
    await _REAL_SLEEP(0)


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

    def test_uses_backend_boot_verify_when_provided(self):
        # With a backend present the boot check goes through its
        # boot_verify() — the IC-7300 must never be checked with the
        # Yaesu "FA" query (0xFA is the CI-V NG reply code).
        cat = _FakeCat(fa_answer=False)  # the FA path would fail
        calls = []

        class _Backend:
            async def boot_verify(self, cat, timeout):
                calls.append(timeout)
                return True

        ok = asyncio.run(server._power_on_radio(cat, backend=_Backend()))
        self.assertTrue(ok)
        self.assertEqual(calls, [0.4])

    def test_icom_backend_boot_verify_queries_civ_read(self):
        from backends.ic7300.backend import IC7300Backend
        backend = IC7300Backend("/dev/fake")
        queried = []

        class _Cat:
            async def query(self, cmd, timeout=None):
                queried.append(cmd)
                return b"freq-frame"

        self.assertTrue(asyncio.run(backend.boot_verify(_Cat(), 0.1)))
        self.assertEqual(queried, [0x03])


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


class PowerOffGuardTests(unittest.TestCase):
    """`_power_off()` is the one implementation of "turn it off" (2026-10-16).

    Extract of the old inline branch plus the hole it left behind: the power
    code predates FT8, so it only knew about ``radio.is_transmitting`` — an
    FT8 transmission would have been interrupted by a PS0.
    """

    def setUp(self):
        self._sleep_patch = patch.object(server.asyncio, "sleep", _no_sleep)
        self._sleep_patch.start()
        self._old_boot = server._power_boot_until
        self._old_tx = server.radio.tx_status
        server.radio.update(tx_status=0)
        server._power_boot_until = 0.0

    def tearDown(self):
        self._sleep_patch.stop()
        server._power_boot_until = self._old_boot
        server.radio.update(tx_status=self._old_tx)

    def test_refuses_while_ft8_is_transmitting(self):
        cat = _FakeCat()
        server.radio.update(power_on=True)
        with patch.object(server, "_ft8_transmitting", return_value=True):
            msg = asyncio.run(server._power_off(cat, server.radio))
        self.assertTrue(msg)                       # a refusal text, not ""
        self.assertEqual(cat.power_calls, [])      # not one PS0
        self.assertTrue(server.radio.power_on)

    def test_power_off_sends_ps0_twice(self):
        cat = _FakeCat()
        msg = asyncio.run(server._power_off(cat, server.radio))
        self.assertEqual(msg, "")
        self.assertEqual(cat.power_calls, [False, False])
        self.assertFalse(server.radio.power_on)

    def test_refuses_inside_boot_window_with_remaining_seconds(self):
        cat = _FakeCat()
        server._power_boot_until = time.monotonic() + 9.0
        msg = asyncio.run(server._power_off(cat, server.radio))
        self.assertIn("秒后再关机", msg)
        self.assertEqual(cat.power_calls, [])


class PowerRescueTests(unittest.TestCase):
    """「重开电台…」那一半：有界、可取消、只认回读。

    Each round is the already-tested `_power_on_radio`; these tests cover the
    three ways the loop can end (woke / timeout / cancelled) and the two rules
    that keep it from making things worse: never write while the port is gone,
    and never write again after a cancellation.

    The loop reads the module-level `cat` / `backend` every round (a power
    cycle re-enumerates USB, so the round must use whatever is current), which
    is why every test points `server.cat` at its fake.
    """

    def setUp(self):
        self._sleep_patch = patch.object(server.asyncio, "sleep", _yield_sleep)
        self._sleep_patch.start()
        self._old = (server.cat, server.backend,
                     server.POWER_RESCUE_INTERVAL_S, server.POWER_RESCUE_MAX_S,
                     server.POWER_ON_VERIFY_S, server._power_boot_until,
                     server._power_rescue_state, server._power_rescue_task,
                     server._power_rescue_attempts)
        self.cat = _FakeCat()
        server.cat = self.cat
        server.backend = None
        server.POWER_RESCUE_INTERVAL_S = 0.0
        server.POWER_RESCUE_MAX_S = 0.2
        server.POWER_ON_VERIFY_S = 0.02
        server._power_boot_until = 0.0
        server._power_rescue_state = ""
        server._power_rescue_task = None
        server._power_rescue_attempts = 0

    def tearDown(self):
        self._sleep_patch.stop()
        (server.cat, server.backend, server.POWER_RESCUE_INTERVAL_S,
         server.POWER_RESCUE_MAX_S, server.POWER_ON_VERIFY_S,
         server._power_boot_until, server._power_rescue_state,
         server._power_rescue_task, server._power_rescue_attempts) = self._old

    def test_woke_on_the_third_ps1(self):
        self.cat._fa_answer, self.cat._fa_after = True, 3
        result = asyncio.run(server._power_rescue())
        self.assertEqual(result, "woke")
        self.assertEqual(server._power_rescue_state, "woke")
        self.assertEqual(self.cat.power_calls.count(True), 3)
        self.assertFalse(server._power_rescue_running())

    def test_timeout_when_the_radio_stays_silent(self):
        self.cat._fa_answer = False
        result = asyncio.run(server._power_rescue())
        self.assertEqual(result, "timeout")
        self.assertEqual(server._power_rescue_state, "timeout")
        self.assertFalse(server._power_rescue_running())
        self.assertGreaterEqual(self.cat.power_calls.count(True), 3)

    def test_a_round_without_the_port_writes_nothing(self):
        self.cat._fa_answer = False
        self.cat.connected = False                 # this is what a PS0 looks like
        asyncio.run(server._power_rescue())
        self.assertEqual(self.cat.power_calls, [])
        self.assertEqual(server._power_rescue_attempts, 0)

    def test_cancel_stops_every_further_ps1(self):
        self.cat._fa_answer = False
        server.POWER_RESCUE_MAX_S = 30.0           # the budget must not end it

        async def scenario():
            server._power_rescue_start()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await asyncio.sleep(0)                 # let it complete a round
            calls_at_cancel = self.cat.power_calls.count(True)
            await server._power_rescue_cancel()
            await asyncio.sleep(0.01)              # give the loop chances to cheat
            return calls_at_cancel

        calls_at_cancel = asyncio.run(scenario())
        self.assertGreaterEqual(calls_at_cancel, 1)
        self.assertEqual(server._power_rescue_state, "cancelled")
        self.assertEqual(self.cat.power_calls.count(True), calls_at_cancel)

    def test_the_off_command_during_a_rescue_is_refused_by_the_boot_window(self):
        """After a PS1 the boot window rejects PS0 — that is the design (spec §5.2).

        The important half: the rescue really is cancelled first, and the
        refusal must not have turned into a PS0.
        """
        self.cat._fa_answer = False
        server.POWER_RESCUE_MAX_S = 30.0
        ws = _FakeWS()

        async def scenario():
            server._power_rescue_start()
            for _ in range(3):
                await asyncio.sleep(0)
            await server._execute_set_command("power", False, ws)

        asyncio.run(scenario())
        self.assertEqual(server._power_rescue_state, "cancelled")
        self.assertNotIn(False, self.cat.power_calls)          # no PS0 ...
        errors = [m for m in ws.messages if m.get("type") == "error"]
        self.assertTrue(errors and "秒后再关机" in errors[0]["message"])

    def test_the_off_command_with_an_open_window_sends_only_ps0(self):
        server._power_boot_until = 0.0                          # no PS1 was sent
        ws = _FakeWS()
        asyncio.run(server._execute_set_command("power", False, ws))
        self.assertEqual(self.cat.power_calls, [False, False])
        self.assertNotIn(True, self.cat.power_calls)
        self.assertFalse(server.radio.power_on)


class PowerCommandWiringTests(unittest.TestCase):
    """`set power` 的四个取值：true / false / restart / cancelRescue。"""

    def setUp(self):
        self._sleep_patch = patch.object(server.asyncio, "sleep", _yield_sleep)
        self._sleep_patch.start()
        self._old = (server.cat, server.POWER_ON_VERIFY_S, server._power_boot_until,
                     server._power_rescue_state, server._power_rescue_task)
        self._old_tx = server.radio.tx_status
        server.radio.update(tx_status=0)
        server.POWER_ON_VERIFY_S = 0.02
        server._power_boot_until = 0.0
        server._power_rescue_state = ""
        server._power_rescue_task = None

    def tearDown(self):
        self._sleep_patch.stop()
        (server.cat, server.POWER_ON_VERIFY_S, server._power_boot_until,
         server._power_rescue_state, server._power_rescue_task) = self._old
        server.radio.update(tx_status=self._old_tx)

    def test_restart_turns_it_off_then_starts_the_rescue(self):
        cat, ws = _FakeCat(fa_answer=False), _FakeWS()
        server.cat = cat
        try:
            with patch.object(server, "_power_rescue_start") as start:
                asyncio.run(server._execute_set_command("power", "restart", ws))
        finally:
            server.cat = None
        self.assertEqual(cat.power_calls, [False, False])      # off first ...
        self.assertFalse(server.radio.power_on)
        start.assert_called_once()                             # ... then the loop

    def test_restart_is_refused_while_transmitting(self):
        cat, ws = _FakeCat(), _FakeWS()
        server.cat = cat
        server.radio.update(tx_status=1)
        try:
            with patch.object(server, "_power_rescue_start") as start:
                asyncio.run(server._execute_set_command("power", "restart", ws))
        finally:
            server.radio.update(tx_status=0)
            server.cat = None
        self.assertEqual(cat.power_calls, [])                  # neither off nor rescue
        start.assert_not_called()
        self.assertTrue([m for m in ws.messages if m.get("type") == "error"])

    def test_cancel_rescue_value_never_touches_the_radio(self):
        cat, ws = _FakeCat(), _FakeWS()
        server.cat = cat
        try:
            asyncio.run(server._execute_set_command("power", "cancelRescue", ws))
        finally:
            server.cat = None
        self.assertEqual(cat.power_calls, [])
        self.assertNotEqual(server._power_rescue_state, "running")


if __name__ == "__main__":
    unittest.main()
