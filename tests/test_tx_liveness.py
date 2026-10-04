"""TX-phase liveness gate (MRRC_REMOTE_SESSION_TX_HEARTBEAT_S, SDD ch15 §15.6).

The gap this closes: a connection that dies *without* a TCP close — NAT entry
dropped, Wi-Fi switched, packets silently discarded — never fires Layer 4's
dead-man switch, so nothing on the instance releases the carrier and only a cloud
lease TTL would, eventually. The hub design forbids depending on that.

Equally important are the releases that must NOT happen, each of which is a test
below: a client that never declared the capability must keep its carrier (older
native builds, LAN clients with their own watchdog), a single missing beat must
not unkey a live operator, and a stale listener must never drop someone else's
carrier (ch15 Layer 4 key-owner arbitration). Opt-in by default: 0 = off.
"""
import asyncio
import json
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import server

SERVER_SOURCE = Path("server.py").read_text(encoding="utf-8")
PTT_JS = Path("static/modules/ptt_manager.js").read_text(encoding="utf-8")
CONFIG_SOURCE = Path("config.py").read_text(encoding="utf-8")


class _FakeCat:
    def __init__(self):
        self.connected = True
        self.commands = []

    async def set_ptt(self, tx):
        self.commands.append("TX1" if tx else "TX0")


class _FakeRadio:
    is_transmitting = True

    def __init__(self):
        self.updates = []

    def update(self, **fields):
        self.updates.append(fields)
        return set(fields)


class _FakeClient:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


class _KeyUpFakeWS:
    """Control socket stand-in: only `send_text` is ever called on it."""

    def __init__(self):
        self.messages = []

    async def send_text(self, text):
        self.messages.append(json.loads(text))


class _LivenessTestBase(unittest.TestCase):
    def setUp(self):
        self.cat = _FakeCat()
        self.radio = _FakeRadio()
        self.client = _FakeClient()
        # Doubles are typed Any: the helpers are annotated with the real
        # WebSocket type, while these tests only need object identity.
        self.key_ws: Any = object()
        patchers = [
            patch.object(server, "cat", self.cat),
            patch.object(server, "radio", self.radio),
            patch.object(server, "scheduler", None),
            patch.object(server, "ctrl_clients", {self.client}),
            patch.object(server, "REMOTE_SESSION_TX_HEARTBEAT_S", 0.05),
            patch.object(server, "TX_LIVENESS_TICK_S", 0.01),
            patch.object(server, "_ptt_key_ws", self.key_ws),
            patch.object(server, "_tx_hb_capable", set()),
            patch.object(server, "_tx_hb_last", {}),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _run_watchdog(self, seconds=0.3):
        async def runner():
            task = asyncio.create_task(server._tx_liveness_watchdog())
            try:
                await asyncio.sleep(seconds)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        asyncio.run(runner())

    def _declare(self, ws: Any = None, age: float = 0.0) -> Any:
        ws = ws if ws is not None else self.key_ws
        server._tx_hb_capable.add(ws)
        server._tx_hb_last[ws] = time.monotonic() - age
        return ws


class ReleaseTests(_LivenessTestBase):
    def test_unkeys_when_the_keying_session_goes_stale(self):
        self._declare(age=5.0)
        self._run_watchdog()
        self.assertEqual(self.cat.commands, ["TX0"])

    def test_single_unkey_write_no_verify_loop(self):
        """Same V1.2 rule as the other release paths: fire-and-forget."""
        self._declare(age=5.0)
        self._run_watchdog(0.2)
        self.assertEqual(self.cat.commands.count("TX0"), 1)

    def test_tx_meters_are_zeroed_with_the_release(self):
        self._declare(age=5.0)
        self._run_watchdog()
        released = [u for u in self.radio.updates if u.get("tx_status") == 0]
        self.assertTrue(released)
        self.assertIn("power_meter", released[-1])

    def test_control_clients_are_told_why(self):
        self._declare(age=5.0)
        self._run_watchdog()
        self.assertTrue(self.client.sent)
        self.assertIn("PTT released locally", self.client.sent[0])

    def test_key_owner_is_cleared_and_capability_dropped(self):
        self._declare(age=5.0)
        self._run_watchdog()
        self.assertIsNone(server._ptt_key_ws)
        self.assertNotIn(self.key_ws, server._tx_hb_capable)


class NoReleaseTests(_LivenessTestBase):
    def test_silent_client_that_never_declared_is_not_gated(self):
        """An older native build (or a LAN client with its own watchdog) never
        sends `txhb`; a mechanism it does not implement must not unkey it."""
        self._run_watchdog(0.3)
        self.assertEqual(self.cat.commands, [])

    def test_single_missing_beat_is_not_a_release(self):
        with patch.object(server, "REMOTE_SESSION_TX_HEARTBEAT_S", 0.5):
            self._declare(age=0.1)
            self._run_watchdog(0.2)
        self.assertEqual(self.cat.commands, [])

    def test_stale_listener_cannot_drop_someone_elses_carrier(self):
        """The keying session never heartbeats; a *different* capable session
        went stale. Layer 4 arbitration says the listener must not unkey."""
        listener: Any = object()
        self._declare(listener, age=5.0)
        self._run_watchdog(0.3)
        self.assertEqual(self.cat.commands, [])
        self.assertIs(server._ptt_key_ws, self.key_ws)

    def test_capable_session_that_has_not_sent_yet_is_not_gated(self):
        server._tx_hb_capable.add(self.key_ws)   # declared, no timestamp
        self._run_watchdog(0.3)
        self.assertEqual(self.cat.commands, [])

    def test_no_keying_session_means_nothing_to_release(self):
        with patch.object(server, "_ptt_key_ws", None):
            self._declare(age=5.0)
            self._run_watchdog(0.3)
        self.assertEqual(self.cat.commands, [])

    def test_radio_not_transmitting_is_ignored(self):
        self._declare(age=5.0)
        with patch.object(self.radio, "is_transmitting", False):
            self._run_watchdog(0.3)
        self.assertEqual(self.cat.commands, [])

    def test_disabled_by_default_never_unkeys(self):
        self._declare(age=5.0)
        with patch.object(server, "REMOTE_SESSION_TX_HEARTBEAT_S", 0.0):
            self._run_watchdog(0.3)
        self.assertEqual(self.cat.commands, [])


class KeyUpRaceTests(unittest.IsolatedAsyncioTestCase):
    """A key-up must not be read as "this session went silent".

    Field log (2026-10-04, hub instance BA4EG — the first browser run of this
    gate): seven forced releases, every one of them 20-70 ms *after* a key-up,
    with ages of 9.3 s / 9.9 s / 81.6 s / 111 s / 234 s / 336 s / 654 s. Each
    age is exactly the gap since *the operator's own previous transmission*, so
    nothing was wrong with the link — the clock simply had not been refreshed
    yet.

    The browser sends `ptt:true` first and its key-time `txhb` second, and the
    receive loop cannot read that beat until the key-up handler returns (CAT
    write + `audio.start_tx`, tens of ms). A watchdog tick landing in that
    window therefore sees a session that is both the key owner *and* silent —
    and drops the carrier on the operator's own healthy press. Keying is itself
    proof of life, so a *capable* session's clock is refreshed by it; a session
    that never declared the heartbeat stays ungated (negative case above).
    """

    def setUp(self):
        from radio_state import RadioState
        self.saved = (server.cat, server.radio, server.scheduler, server.audio,
                      server.backend, server._ptt_key_ws, server._tx_hb_capable,
                      server._tx_hb_last, server.REMOTE_SESSION_TX_HEARTBEAT_S,
                      server.TX_LIVENESS_TICK_S)
        self.cat = _FakeCat()
        server.cat = self.cat
        server.radio = RadioState()
        server.scheduler = None
        server.audio = None
        server.backend = None
        server._ptt_key_ws = None
        server._tx_hb_capable = set()
        server._tx_hb_last = {}
        server.REMOTE_SESSION_TX_HEARTBEAT_S = 5.0
        server.TX_LIVENESS_TICK_S = 0.01
        self.ws: Any = _KeyUpFakeWS()

    def tearDown(self):
        (server.cat, server.radio, server.scheduler, server.audio,
         server.backend, server._ptt_key_ws, server._tx_hb_capable,
         server._tx_hb_last, server.REMOTE_SESSION_TX_HEARTBEAT_S,
         server.TX_LIVENESS_TICK_S) = self.saved

    def _declare_capable(self, age):
        server._tx_hb_capable.add(self.ws)
        server._tx_hb_last[self.ws] = time.monotonic() - age

    def _key_up(self):
        asyncio.run(server._execute_set_command("ptt", True, self.ws))

    def _run_watchdog(self, seconds=0.2):
        async def runner():
            task = asyncio.create_task(server._tx_liveness_watchdog())
            try:
                await asyncio.sleep(seconds)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        asyncio.run(runner())

    def test_key_up_of_a_capable_session_is_not_a_stale_session(self):
        """The press that follows a pause longer than the threshold (the
        "previous QSO was a minute ago" case) must transmit — today the tick
        lands between the key-up and its key-time beat and unkeys it."""
        self._declare_capable(age=60.0)
        self._key_up()                       # beat for *this* press not read yet
        self._run_watchdog()
        self.assertEqual(self.cat.commands, ["TX1"])   # no TX0
        self.assertIs(server._ptt_key_ws, self.ws)

    def test_capable_session_that_goes_silent_after_keying_is_still_released(self):
        """The refresh buys one threshold, not immunity: a session that stops
        proving life after its key-up is released exactly as before."""
        self._declare_capable(age=60.0)
        self._key_up()
        server._tx_hb_last[self.ws] = time.monotonic() - 5.0   # uplink died here
        self._run_watchdog()
        self.assertEqual(self.cat.commands.count("TX0"), 1)

    def test_key_up_does_not_declare_capability(self):
        """Keying is not a capability declaration: an older client that never
        sends `txhb` must stay ungated even after keying (and re-keying)."""
        self._key_up()
        self.assertNotIn(self.ws, server._tx_hb_capable)
        self._run_watchdog()
        self.assertEqual(self.cat.commands, ["TX1"])


class DecisionTests(unittest.TestCase):
    """Unit-level boundary on the decision helper the loop calls."""

    def setUp(self):
        self.ws: Any = object()
        patchers = [
            patch.object(server, "REMOTE_SESSION_TX_HEARTBEAT_S", 1.0),
            patch.object(server, "_ptt_key_ws", self.ws),
            patch.object(server, "_tx_hb_capable", {self.ws}),
            patch.object(server, "_tx_hb_last", {self.ws: time.monotonic()}),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_well_within_the_threshold_is_not_expired(self):
        # Boundary-by-equality is inherently racy with a monotonic clock, so the
        # meaningful assertion is "comfortably within".
        server._tx_hb_last[self.ws] = time.monotonic() - 0.5
        self.assertIsNone(server._tx_liveness_timeout())

    def test_past_the_threshold_expires(self):
        server._tx_hb_last[self.ws] = time.monotonic() - 1.5
        self.assertIs(server._tx_liveness_timeout(), self.ws)

    def test_disabled_threshold_never_expires(self):
        server._tx_hb_last[self.ws] = time.monotonic() - 999.0
        with patch.object(server, "REMOTE_SESSION_TX_HEARTBEAT_S", 0.0):
            self.assertIsNone(server._tx_liveness_timeout())


class SourceGuardTests(unittest.TestCase):
    def test_heartbeat_message_is_handled(self):
        self.assertIn('elif msg_type == "txhb":', SERVER_SOURCE)
        self.assertIn("_tx_hb_capable.add(ws)", SERVER_SOURCE)
        self.assertIn("_tx_hb_last[ws] = time.monotonic()", SERVER_SOURCE)

    def test_control_socket_disconnect_forgets_the_session(self):
        finally_block = SERVER_SOURCE.split("_ws_tokens.pop(ws, None)", 1)[1][:400]
        self.assertIn("_tx_hb_capable.discard(ws)", finally_block)
        self.assertIn("_tx_hb_last.pop(ws, None)", finally_block)

    def test_task_is_created_only_when_enabled_and_cancelled(self):
        self.assertIn("if REMOTE_SESSION_TX_HEARTBEAT_S > 0:", SERVER_SOURCE)
        self.assertIn('name="tx_liveness")', SERVER_SOURCE)
        self.assertIn("_tx_liveness_task.cancel()", SERVER_SOURCE)

    def test_env_knob_defaults_to_off(self):
        self.assertIn('_env_float("MRRC_REMOTE_SESSION_TX_HEARTBEAT_S", 0.0)',
                      CONFIG_SOURCE)

    def test_frontend_sends_and_stops_the_heartbeat(self):
        self.assertIn("sendMsg({ type: 'txhb' })", PTT_JS)
        self.assertIn("txHeartbeatTimer = setInterval(sendTXHeartbeat, 500)", PTT_JS)
        # Started by both keying paths (PTT, TUNE); stopped by every release
        # path (pttEnd, tuneEnd, forceRX) plus startTXHeartbeat's own
        # idempotent guard, so an overlapping key can never stack timers.
        self.assertEqual(PTT_JS.count("startTXHeartbeat();"), 2)
        self.assertEqual(PTT_JS.count("stopTXHeartbeat();"), 4)

    def test_cache_bust_covers_the_ptt_manager(self):
        index = Path("static/index.html").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        self.assertIn("modules/ptt_manager.js?v=17", index)
        self.assertIn("'/modules/ptt_manager.js?v=17'", sw)
        self.assertIn("const CACHE = 'mrrc-v45'", sw)


if __name__ == "__main__":
    unittest.main()
