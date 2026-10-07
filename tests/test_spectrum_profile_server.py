"""Per-socket spectrum profile wiring in server.py (SDD AD-025).

The gate must keep two promises:

1. a socket that never declared capability stays on ``high`` — full 1701 B at
   every broadcast tick, byte-for-byte today's behaviour;
2. a listener-password socket that never declared capability keeps the /3 full
   frames it has had since v1.25.2 (tests/test_listen_only.py:391 pins that).
"""

import unittest
from unittest import mock

import server
import session_metrics
import spectrum_profile as sp


class _FakeWS:
    """Minimal WebSocket double: records frames, never raises."""

    def __init__(self):
        self.frames: list[bytes] = []

    async def send_bytes(self, payload: bytes):
        self.frames.append(payload)


def _full_frame() -> bytes:
    return b"\x01" + b"\x11" * 850 + b"\x00" * 850


class ProfileTableTests(unittest.TestCase):
    def test_undeclared_socket_defaults_to_high(self):
        ws = _FakeWS()
        with mock.patch.object(server, "_spectrum_profiles", {}), \
             mock.patch.object(server, "_listen_spectrum_clients", set()):
            self.assertEqual(server._profile_for(ws), "high")
            self.assertTrue(server._spectrum_frame_due(ws, 7))

    def test_listener_role_default_is_the_listen_tier(self):
        ws = _FakeWS()
        with mock.patch.object(server, "_spectrum_profiles", {}), \
             mock.patch.object(server, "_listen_spectrum_clients", {ws}):
            self.assertEqual(server._profile_for(ws), "listen")
            self.assertEqual(
                [t for t in range(1, 10) if server._spectrum_frame_due(ws, t)],
                [3, 6, 9])

    def test_declared_caps_override_the_role_default(self):
        """A listener who opts into a tier must actually get fewer frames."""
        ws = _FakeWS()
        with mock.patch.object(server, "_spectrum_profiles", {ws: "low"}), \
             mock.patch.object(server, "_listen_spectrum_clients", {ws}):
            self.assertEqual(server._profile_for(ws), "low")
            self.assertEqual(
                [t for t in range(1, 13) if server._spectrum_frame_due(ws, t)],
                [4, 8, 12])

    def test_listen_divider_constant_still_reads_three(self):
        """tests/test_listen_only.py:397 reads this attribute by name."""
        self.assertEqual(server.LISTEN_SPECTRUM_DIVIDER, 3)
        self.assertEqual(server.LISTEN_SPECTRUM_DIVIDER,
                         sp.divider_for(sp.LISTEN_PROFILE))


class _FanoutTestBase(unittest.IsolatedAsyncioTestCase):
    """Shared fixture: real SessionMetrics + fake sockets + patched globals."""

    def setUp(self):
        self.metrics = session_metrics.SessionMetrics()
        self.high, self.mid, self.low = _FakeWS(), _FakeWS(), _FakeWS()
        self.clients = {self.high, self.mid, self.low}
        self.profiles = {self.mid: "mid", self.low: "low"}   # high = undeclared
        self._patches = [
            mock.patch.object(server, "metrics", self.metrics),
            mock.patch.object(server, "spectrum_clients", self.clients),
            mock.patch.object(server, "_spectrum_profiles", self.profiles),
            mock.patch.object(server, "_listen_spectrum_clients", set()),
        ]
        for p in self._patches:
            p.start()
        self.variants = sp.build_variants(_full_frame())

    def tearDown(self):
        for p in self._patches:
            p.stop()


class FanoutTests(_FanoutTestBase):
    async def test_dividers_over_twelve_ticks(self):
        for tick in range(1, 13):
            await server._spectrum_fanout(self.variants, tick)
        self.assertEqual(len(self.high.frames), 12)   # /1
        self.assertEqual(len(self.mid.frames), 6)     # /2 -> 2,4,6,8,10,12
        self.assertEqual(len(self.low.frames), 3)     # /4 -> 4,8,12

    async def test_frame_lengths_match_the_declared_shape(self):
        await server._spectrum_fanout(self.variants, 4)
        self.assertEqual({len(f) for f in self.high.frames}, {1701})
        self.assertEqual({len(f) for f in self.mid.frames}, {851})
        self.assertEqual({len(f) for f in self.low.frames}, {851})

    async def test_undeclared_socket_never_gets_a_short_frame(self):
        """The compatibility gate (D-3), stated as bytes on the wire."""
        for tick in range(1, 25):
            await server._spectrum_fanout(self.variants, tick)
        self.assertTrue(self.high.frames)
        self.assertEqual({len(f) for f in self.high.frames}, {1701})

    async def test_short_frame_is_the_prefix_of_the_full_frame(self):
        await server._spectrum_fanout(self.variants, 4)
        self.assertEqual(self.high.frames[0][:851], self.low.frames[0])

    async def test_metrics_count_the_bytes_actually_sent(self):
        for tick in range(1, 13):
            await server._spectrum_fanout(self.variants, tick)
        snap = self.metrics.snapshot()
        self.assertEqual(snap["uplink_bytes_total"]["spectrum"],
                         (12 * 1701) + (6 * 851) + (3 * 851))
        self.assertEqual(snap["spectrum_profiles"]["high"]["frames"], 12)
        self.assertEqual(snap["spectrum_profiles"]["mid"]["frames"], 6)
        self.assertEqual(snap["spectrum_profiles"]["low"]["frames"], 3)
        self.assertEqual(snap["spectrum_profiles"]["low"]["bytes"], 3 * 851)

    async def test_a_metering_bug_does_not_mark_the_socket_dead(self):
        """Regression: metering used to sit inside the send's try, so an
        AttributeError from the counters was laundered into "client went away"
        and the caller dropped a perfectly healthy socket.  Now it propagates
        (the broadcast loop logs it) and the client keeps its stream.
        """
        class _BrokenMetrics:
            def add_bytes(self, kind, nbytes):
                pass

            def add_spectrum_profile_frame(self, profile, nbytes):
                raise RuntimeError("counter bug")

        with mock.patch.object(server, "metrics", _BrokenMetrics()):
            with self.assertRaises(RuntimeError):
                await server._spectrum_fanout(self.variants, 1)
        self.assertEqual(len(self.high.frames), 1)   # the frame still went out

    async def test_dead_socket_is_reported_for_removal(self):
        class _Dead(_FakeWS):
            async def send_bytes(self, payload):
                raise RuntimeError("gone")

        dead_ws = _Dead()
        self.clients.add(dead_ws)
        dead = await server._spectrum_fanout(self.variants, 1)
        self.assertEqual(dead, {dead_ws})
        self.assertEqual(len(self.high.frames), 1)   # the rest still got served


class FallbackDividerTests(_FanoutTestBase):
    """The S-meter fallback path must obey the same divider (design §5.3).

    Today it is the *expensive* path: no frame-count gate, so it regenerates
    1701 B on every one of the 30 Hz ticks.  A tier that applied only on the
    healthy path would make the bad case cost more than the good one.
    """

    async def test_low_tier_gets_a_quarter_of_the_frames(self):
        for tick in range(1, 31):
            await server._spectrum_fanout(self.variants, tick)
        self.assertEqual(len(self.high.frames), 30)
        self.assertEqual(len(self.low.frames), 7)    # 4,8,...,28


if __name__ == "__main__":
    unittest.main()
