"""Per-socket spectrum profile wiring in server.py (SDD AD-025).

The gate must keep two promises:

1. a socket that never declared capability stays on ``high`` — full 1701 B at
   every broadcast tick, byte-for-byte today's behaviour;
2. a listener-password socket that never declared capability keeps the /3 full
   frames it has had since v1.25.2 (tests/test_listen_only.py:391 pins that).
"""

import unittest
from pathlib import Path
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


class CapsHandlingTests(unittest.TestCase):
    """The handler's text-frame loop turns caps into a per-socket profile."""

    def _handler_block(self) -> str:
        src = Path("server.py").read_text(encoding="utf-8")
        block = src.split('@app.websocket("/WSspectrum")', 1)[1]
        return block.split("# ── Audio RX WebSocket", 1)[0]

    def test_handler_parses_caps_into_the_profile_table(self):
        block = self._handler_block()
        self.assertIn("spectrum_profile.parse_caps(await ws.receive_text())", block)
        self.assertIn("_spectrum_profiles[ws] = profile", block)

    def test_handler_drops_per_socket_state_on_disconnect(self):
        """Profile entries must go with the socket: three cleanups, not two."""
        block = self._handler_block()
        self.assertIn("spectrum_clients.discard(ws)", block)
        self.assertIn("_listen_spectrum_clients.discard(ws)", block)
        self.assertIn("_spectrum_profiles.pop(ws, None)", block)

    def test_handler_keeps_the_scope_producer_guard_markers(self):
        """tests/test_server_ws_protocol.py:334 slices on these two markers."""
        block = self._handler_block()
        self.assertIn("await _scope_producer.start()", block)
        self.assertIn("await _scope_producer.stop()", block)

    def test_handler_keeps_the_metrics_close_and_second_except(self):
        """The rewrite must not lose the session close or the catch-all except."""
        block = self._handler_block()
        self.assertIn('metrics.close(_role_for_token(token), "spectrum", token)', block)
        self.assertIn("except Exception:", block)

    def test_handler_logs_the_negotiated_tier(self):
        """Support triage needs to see which tier a socket ended up on."""
        self.assertIn('"Spectrum profile %s', self._handler_block())

    def test_listen_tier_is_not_client_selectable(self):
        self.assertIsNone(sp.parse_caps('{"type":"spectrumCaps","profile":"listen"}'))
        self.assertEqual(sp.parse_caps('{"type":"spectrumCaps","profile":"mid"}'), "mid")


class _CapsWS:
    """Fake upgrade socket that drives ws_spectrum for real.

    Source assertions can only prove the strings exist; this proves the whole
    path works — handshake auth, caps -> profile table, and the cleanup that
    must happen when the socket goes away.
    """

    def __init__(self, texts: list, token: str = "op-token"):
        self.headers = {"authorization": f"Bearer {token}"}
        self.cookies: dict = {}
        self.query_params: dict = {}
        self._texts = list(texts)
        self.accepted = False
        self.closed_with = None
        self.tiers_seen_while_open: list = []

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=None):
        self.closed_with = (code, reason)

    async def receive_text(self) -> str:
        # Record the negotiated tier while the socket is still open: this is the
        # only way to observe it before the finally-block pops the entry.
        self.tiers_seen_while_open.append(server._profile_for(self))
        if self._texts:
            return self._texts.pop(0)
        raise server.WebSocketDisconnect(code=1000)


class CapsNegotiationTests(unittest.IsolatedAsyncioTestCase):
    """End-to-end: a real call into ws_spectrum, no network."""

    def setUp(self):
        self._patches = [
            mock.patch.object(server, "_scope_producer", None),
            mock.patch.object(server, "metrics", session_metrics.SessionMetrics()),
            mock.patch.object(server, "spectrum_clients", set()),
            mock.patch.object(server, "_listen_spectrum_clients", set()),
            mock.patch.object(server, "_spectrum_profiles", {}),
            mock.patch.object(server, "_auth_tokens", {"op-token", "listen-token"}),
            mock.patch.object(server, "_listen_tokens", {"listen-token"}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    async def test_caps_set_the_tier_and_disconnect_clears_it(self):
        ws = _CapsWS(['{"type":"spectrumCaps","profile":"mid"}'])
        await server.ws_spectrum(ws)

        self.assertTrue(ws.accepted)
        # While the socket was open the negotiated tier was in force ... (the
        # first receive_text happens before the caps frame is processed, so the
        # socket starts on the default and moves to mid on the next one)
        self.assertEqual(ws.tiers_seen_while_open, ["high", "mid"])
        # ... and after it closed nothing per-socket survived.
        self.assertNotIn(ws, server._spectrum_profiles)
        self.assertNotIn(ws, server.spectrum_clients)
        self.assertNotIn(ws, server._listen_spectrum_clients)

    async def test_undeclared_socket_leaves_no_tier_entry(self):
        """No caps => no entry => the role default => today's 1701 B stream."""
        ws = _CapsWS(["", "not json"])
        await server.ws_spectrum(ws)
        self.assertEqual(server._spectrum_profiles, {})
        self.assertEqual(server._profile_for(ws), "high")

    async def test_unknown_tier_name_is_ignored_not_adopted(self):
        ws = _CapsWS(['{"type":"spectrumCaps","profile":"turbo"}'])
        await server.ws_spectrum(ws)
        self.assertEqual(server._spectrum_profiles, {})

    async def test_listener_socket_defaults_to_the_listen_tier(self):
        """A listener that declares nothing keeps the /3 full frames of v1.25.2."""
        ws = _CapsWS([], token="listen-token")
        await server.ws_spectrum(ws)
        self.assertEqual(ws.tiers_seen_while_open, ["listen"])
        self.assertIsNone(ws.closed_with)          # accepted, then clean disconnect
        self.assertNotIn(ws, server._listen_spectrum_clients)

    async def test_listener_can_opt_into_a_tier(self):
        ws = _CapsWS(['{"type":"spectrumCaps","profile":"low"}'], token="listen-token")
        await server.ws_spectrum(ws)
        self.assertEqual(ws.tiers_seen_while_open, ["listen", "low"])

    async def test_unauthenticated_socket_is_refused_before_any_tier_state(self):
        ws = _CapsWS([], token="nope")
        await server.ws_spectrum(ws)
        self.assertEqual(ws.closed_with, (4001, "Unauthorized"))
        self.assertFalse(ws.accepted)
        self.assertEqual(server._spectrum_profiles, {})


class BroadcastLoopSourceTests(unittest.TestCase):
    """The loop must hand its frame to the shared fan-out, not send inline."""

    def _loop_block(self) -> str:
        src = Path("server.py").read_text(encoding="utf-8")
        block = src.split("async def _broadcast_spectrum_loop()", 1)[1]
        return block.split("async def _spectrum_fanout(", 1)[0]

    def test_loop_uses_the_shared_fanout(self):
        block = self._loop_block()
        self.assertIn("spectrum_profile.build_variants(binary)", block)
        self.assertIn("await _spectrum_fanout(variants, _broadcast_tick)", block)

    def test_no_inline_send_survives_in_the_loop(self):
        """An inline send is exactly how the two paths drifted apart before."""
        block = self._loop_block()
        self.assertNotIn("await ws.send_bytes(binary)", block)
        self.assertNotIn('metrics.add_bytes("spectrum", len(binary))', block)
        self.assertNotIn("_spectrum_frame_due(ws, _broadcast_tick)", block)

    def test_docstring_no_longer_claims_five_fps(self):
        """It claimed 5 fps / 200 ms while the tick has been 30 Hz for years."""
        block = self._loop_block()[:1400]
        self.assertNotIn("Runs at 5 fps", block)
        self.assertIn("SPECTRUM_BROADCAST_FPS", block)

    def test_broadcast_fps_is_still_the_tick_source(self):
        """A profile divides the tick rate; it never changes it."""
        self.assertEqual(server.SPECTRUM_BROADCAST_FPS, 30)


class MetricsLineTests(unittest.TestCase):
    """The telemetry line must say which axis it reports (design §6.3).

    The kbps figures are metered inside Starlette, so they are *payload* bytes:
    permessage-deflate downstream is invisible here.  Quoting 151 kbps as a wire
    figure overstates a browser client by ~3.9x and an Android client by ~1x —
    the label is what keeps the two apart.
    """

    def _line(self, metrics) -> str:
        # Force a 60 s report window so the kbps math is deterministic.
        metrics._last_report = metrics._clock() - 60.0
        return server._session_metrics_line(metrics.take_report())

    def _metered(self):
        m = session_metrics.SessionMetrics()
        m.open("operator", "spectrum", "tok")
        for _ in range(12):
            m.add_bytes("spectrum", 1701)
            m.add_spectrum_profile_frame("high", 1701)
        for _ in range(3):
            m.add_bytes("spectrum", 851)
            m.add_spectrum_profile_frame("low", 851)
        return m

    def test_line_is_labelled_payload_and_lists_tier_frames(self):
        line = self._line(self._metered())
        self.assertIn("Session metrics:", line)
        self.assertIn("payload\u2248", line)
        self.assertIn("high=12", line)
        self.assertIn("low=3", line)

    def test_line_keeps_the_fields_support_triage_greps_for(self):
        """'uplink spectrum' is how the 150.8 kbps baseline was read out."""
        line = self._line(self._metered())
        for needle in ("listeners ", "operators ", "uplink spectrum ", "audio_rx ",
                       "kbps", "since last report"):
            self.assertIn(needle, line)

    def test_line_survives_an_idle_server(self):
        line = self._line(session_metrics.SessionMetrics())
        self.assertIn("Session metrics:", line)
        self.assertIn("spectrum frames -", line)

    def test_report_loop_calls_the_pure_formatter(self):
        """One renderer, no duplicated format string to drift out of sync."""
        src = Path("server.py").read_text(encoding="utf-8")
        loop = src.split("async def _session_metrics_loop():", 1)[1]
        loop = loop.split("\n\n\n", 1)[0]
        self.assertIn('logger.info("%s", _session_metrics_line(r))', loop)
        self.assertNotIn('"Session metrics: listeners %d', loop)


class WebClientCapsTests(unittest.TestCase):
    """Frontend contract, source-asserted like the repo's other UI guards."""

    def test_main_js_sends_caps_on_open(self):
        src = Path("static/ft710_main.js").read_text(encoding="utf-8")
        self.assertIn("function sendSpectrumCaps()", src)
        self.assertIn('type: "spectrumCaps"', src)
        # onopen must declare the tier before frames start arriving, and the
        # existing self-heal call has to survive the rewrite.
        block = src.split("wsSpectrum.onopen = () => {", 1)[1].split("};", 1)[0]
        self.assertIn("sendSpectrumCaps();", block)
        self.assertIn('subchannelConnected("spectrum");', block)

    def test_main_js_reads_the_stored_tier_with_a_safe_default(self):
        src = Path("static/ft710_main.js").read_text(encoding="utf-8")
        self.assertIn('getStored("scopeProfile", "high")', src)
        # ft710_ui.js may not have loaded yet (classic scripts, shared globals).
        self.assertIn('typeof getStored === "function"', src)

    def test_ui_js_persists_and_pushes_a_live_change(self):
        src = Path("static/ft710_ui.js").read_text(encoding="utf-8")
        self.assertIn("let scopeProfile = getStored('scopeProfile', 'high');", src)
        self.assertIn("setStored('scopeProfile', scopeProfile);", src)
        # Live switch: no socket reopen — the server re-gates on the next tick.
        self.assertIn("sendSpectrumCaps();", src)
        self.assertIn("scope-profile-select", src)

    def test_index_html_offers_exactly_the_three_client_tiers(self):
        html = Path("static/index.html").read_text(encoding="utf-8")
        self.assertIn('id="scope-profile-select"', html)
        for value in ("high", "mid", "low"):
            self.assertIn(f'value="{value}"', html)
        # The server-internal "listen" tier is not a user choice.
        self.assertNotIn('value="listen"', html)

    def test_asset_versions_were_bumped(self):
        html = Path("static/index.html").read_text(encoding="utf-8")
        self.assertIn("ft710_main.js?v=39", html)
        self.assertIn("ft710_ui.js?v=35", html)
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        self.assertIn("const CACHE = 'mrrc-v48';", sw)
        self.assertIn("'/ft710_main.js?v=39'", sw)
        self.assertIn("'/ft710_ui.js?v=35'", sw)

    def test_no_stale_cache_pins_anywhere(self):
        """A missed pin is how a user keeps running yesterday's bundle."""
        for path in ("static/index.html", "static/sw.js"):
            src = Path(path).read_text(encoding="utf-8")
            for stale in ("ft710_main.js?v=38", "ft710_ui.js?v=33", "mrrc-v46"):
                self.assertNotIn(stale, src, f"{path} still pins {stale}")

    def test_listen_page_is_untouched_this_phase(self):
        """P1 leaves the listen page on the server-side 'listen' tier (D-4)."""
        src = Path("static/listen.js").read_text(encoding="utf-8")
        self.assertNotIn("spectrumCaps", src)

    def test_probe_tool_exists_and_declares_caps(self):
        """The acceptance numbers in design §6 must be reproducible by command."""
        src = Path("dev_tools/spectrum_profile_probe.py").read_text(encoding="utf-8")
        self.assertIn("spectrumCaps", src)
        self.assertIn("/WSspectrum", src)
        self.assertIn("compression", src)   # the --no-deflate (Android) axis


if __name__ == "__main__":
    unittest.main()
