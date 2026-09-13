"""Server wiring for the CQ key (spec 2026-09-13 §3/§6).

Covers the command surface (gate, conflict checks, start/abort), the safety
interlocks (microphone exclusivity, disconnect abort, forced RX), the key/unkey
sequence and the state broadcast — all without hardware.
"""

import json
import unittest
from unittest import mock

import server
from cq_player import CQUnavailable


class _FakeWS:
    def __init__(self):
        self.messages = []

    async def send_text(self, text):
        self.messages.append(json.loads(text))

    def errors(self):
        return [m.get("message", "") for m in self.messages if m.get("type") == "error"]


class _FakeCaps:
    def __init__(self, gated=False):
        self.tx_gated = gated
        self.tune_via = "tx2"


class _FakeBackend:
    def __init__(self, gated=False):
        self.capabilities = _FakeCaps(gated)


class _FakeRadio:
    def __init__(self, tx_status=0):
        self.tx_status = tx_status
        self.updates = []

    @property
    def is_transmitting(self):
        return self.tx_status != 0

    def update(self, **kw):
        self.updates.append(kw)
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeCat:
    def __init__(self, connected=True):
        self.calls = []
        self.connected = connected

    async def set_ptt(self, on):
        self.calls.append(("ptt", on))

    async def set_tune(self, on):
        self.calls.append(("tune", on))


class _FakeAudio:
    def __init__(self, start_ok=True):
        self.calls = []
        self.start_ok = start_ok
        self.frames = []

    def start_tx(self):
        self.calls.append(("start_tx",))
        return self.start_ok

    def stop_tx(self, graceful=False):
        self.calls.append(("stop_tx", graceful))

    def feed_tx_audio(self, pcm):
        self.frames.append(pcm)

    def tx_queue_frames(self):
        return 0

    def tx_stats(self):
        return {"written": len(self.frames), "write_err": 0, "queue_drops": 0}


class _CqServerTestCase(unittest.IsolatedAsyncioTestCase):
    """Common patch/restore for the module globals the CQ path touches."""

    async def asyncSetUp(self):
        self.ws = _FakeWS()
        self.saved = {name: getattr(server, name)
                      for name in ("backend", "radio", "cat", "audio", "_cq_player")}
        self.radio = _FakeRadio()
        self.cat = _FakeCat()
        self.audio = _FakeAudio()
        server.backend = _FakeBackend()
        server.radio = self.radio
        server.cat = self.cat
        server.audio = self.audio
        self.real_broadcast_cq_state = server._broadcast_cq_state
        self.broadcast = mock.AsyncMock()
        patch = mock.patch.object(server, "_broadcast_cq_state", self.broadcast)
        patch.start()
        self.addCleanup(patch.stop)
        patch2 = mock.patch.object(server, "_broadcast_recording_state", mock.AsyncMock())
        patch2.start()
        self.addCleanup(patch2.stop)

    async def asyncTearDown(self):
        for name, value in self.saved.items():
            setattr(server, name, value)

    async def _set_cq(self, value):
        await server._execute_set_command("cq", value, self.ws)


class CqCommandTests(_CqServerTestCase):
    async def test_gated_model_refuses_with_the_gate_message(self):
        server.backend = _FakeBackend(gated=True)
        real = server._cq_player
        server._cq_player = server.CQPlayer(asset_path=None)
        try:
            await self._set_cq(True)
        finally:
            server._cq_player = real
        self.assertTrue(any("not hardware-verified" in e for e in self.ws.errors()),
                        self.ws.errors())
        self.assertEqual(self.cat.calls, [])                 # nothing keyed

    async def test_start_reaches_the_player(self):
        player = mock.MagicMock()
        player.is_calling = False
        player.start = mock.AsyncMock()
        server._cq_player = player
        await self._set_cq(True)
        player.start.assert_awaited_once()

    async def test_abort_reaches_the_player(self):
        player = mock.MagicMock()
        player.is_calling = True
        player.abort = mock.AsyncMock()
        server._cq_player = player
        await self._set_cq(False)
        player.abort.assert_awaited_once()

    async def test_second_start_broadcasts_instead_of_calling_again(self):
        player = mock.MagicMock()
        player.is_calling = True
        player.start = mock.AsyncMock()
        server._cq_player = player
        await self._set_cq(True)
        player.start.assert_not_awaited()
        self.assertTrue(self.broadcast.await_count >= 1)

    async def test_unavailable_player_reports_its_reason(self):
        player = mock.MagicMock()
        player.is_calling = False
        player.start = mock.AsyncMock(
            side_effect=CQUnavailable("CQ asset unusable: CQ asset not found: /x"))
        server._cq_player = player
        await self._set_cq(True)
        self.assertTrue(any("not found" in e for e in self.ws.errors()), self.ws.errors())

    async def test_ptt_held_by_another_client_refuses_cq(self):
        self.radio.tx_status = 1                              # someone is keyed
        player = mock.MagicMock()
        player.is_calling = False
        player.start = mock.AsyncMock()
        server._cq_player = player
        await self._set_cq(True)
        player.start.assert_not_awaited()
        self.assertTrue(any("transmitting" in e.lower() for e in self.ws.errors()))

    async def test_tune_carrier_refuses_cq(self):
        self.radio.tx_status = 2                              # TUNE is running
        player = mock.MagicMock()
        player.is_calling = False
        player.start = mock.AsyncMock()
        server._cq_player = player
        await self._set_cq(True)
        player.start.assert_not_awaited()
        self.assertTrue(any("tune" in e.lower() for e in self.ws.errors()))

    async def test_cq_blocks_a_tune_request(self):
        player = mock.MagicMock()
        player.is_calling = True
        server._cq_player = player
        await server._execute_set_command("tune", True, self.ws)
        self.assertEqual(self.cat.calls, [])                  # no carrier started
        self.assertTrue(any("cq" in e.lower() for e in self.ws.errors()), self.ws.errors())


class CqKeyingTests(_CqServerTestCase):
    """The key/unkey callbacks must mirror the PTT release architecture."""

    async def test_key_claims_and_starts_tx_audio(self):
        ok = await server._cq_key("tok-1")
        self.assertTrue(ok)
        self.assertEqual(self.cat.calls, [("ptt", True)])
        self.assertIn(("start_tx",), self.audio.calls)
        self.assertEqual(self.radio.tx_status, 1)

    async def test_key_releases_when_the_audio_device_fails(self):
        server.audio = _FakeAudio(start_ok=False)
        ok = await server._cq_key("tok-1")
        self.assertFalse(ok)
        self.assertEqual(self.cat.calls, [("ptt", True), ("ptt", False)])
        self.assertEqual(self.radio.tx_status, 0)

    async def test_unkey_drains_gracefully_then_zeros_the_meters(self):
        await server._cq_unkey(True)
        self.assertIn(("stop_tx", True), self.audio.calls)
        self.assertEqual(self.cat.calls, [("ptt", False)])
        self.assertEqual(self.radio.tx_status, 0)
        self.assertEqual(self.radio.power_meter, 0)
        self.assertEqual(self.radio.swr_meter, 0)


class CqUplinkExclusivityTests(_CqServerTestCase):
    async def test_mic_frames_are_dropped_while_calling(self):
        player = mock.MagicMock()
        player.is_calling = True
        server._cq_player = player
        server._tx_cq_mic_drops = 0
        await server._feed_tx_from_uplink(b"\x00\x00" * 960)
        self.assertEqual(self.audio.frames, [])
        self.assertEqual(server._tx_cq_mic_drops, 1)

    async def test_mic_frames_flow_without_a_call(self):
        player = mock.MagicMock()
        player.is_calling = False
        server._cq_player = player
        await server._feed_tx_from_uplink(b"\x00\x00" * 960)
        self.assertEqual(len(self.audio.frames), 1)

    async def test_initiator_disconnect_aborts_the_call(self):
        player = mock.MagicMock()
        player.is_calling = True
        player.abort = mock.AsyncMock()
        player.status.return_value = {"started_by": server._ws_client_id(self.ws)}
        server._cq_player = player
        await server._cq_abort_if_client_gone(self.ws)
        player.abort.assert_awaited_once()

    async def test_other_client_disconnect_does_not_abort(self):
        player = mock.MagicMock()
        player.is_calling = True
        player.abort = mock.AsyncMock()
        player.status.return_value = {"started_by": "deadbe"}
        server._cq_player = player
        await server._cq_abort_if_client_gone(self.ws)
        player.abort.assert_not_awaited()


class CqStateTests(_CqServerTestCase):
    def test_asset_path_defaults_to_the_packaged_file(self):
        import config
        self.assertTrue(str(config.CQ_ASSET_PATH).endswith("static/audio/cq.wav"))

    def test_full_state_carries_the_cq_snapshot(self):
        player = mock.MagicMock()
        player.status.return_value = {"state": "calling", "frames_sent": 3}
        server._cq_player = player
        server.backend = None                                # table fallbacks
        msg = server._full_state_message({"freq": 1}, [])
        self.assertEqual(msg["cq"]["state"], "calling")

    async def test_bind_attaches_live_dependencies_and_loads_the_asset(self):
        with mock.patch.object(server, "CQ_ASSET_PATH", server.STATIC_DIR / "audio" / "cq.wav"):
            server._bind_cq_player()
        self.assertTrue(server._cq_player.ready)
        self.assertIs(server._cq_player._audio, self.audio)
        self.assertIsNotNone(server._cq_player._key)

    async def test_broadcast_is_a_noop_without_clients(self):
        with mock.patch.object(server, "ctrl_clients", set()):
            await self.real_broadcast_cq_state()                # real function

    async def test_ptt_is_refused_while_calling(self):
        player = mock.MagicMock()
        player.is_calling = True
        server._cq_player = player
        await server._execute_set_command("ptt", True, self.ws)
        self.assertEqual(self.cat.calls, [])                  # nothing keyed
        self.assertTrue(any("cq" in e.lower() for e in self.ws.errors()), self.ws.errors())


class CqFrontendContractTests(unittest.TestCase):
    """The browser side of the CQ key (spec 2026-09-13 §5).

    Source assertions, not DOM tests: the UI is classic <script> files with
    cross-file globals, so behaviour is pinned through the contracts the CQ
    state machine depends on (button wiring, ingestion, rendering).
    """

    def _read(self, *parts):
        from pathlib import Path
        return (Path(__file__).resolve().parents[1].joinpath(*parts)
                .read_text(encoding="utf-8"))

    def test_cq_button_sits_next_to_tune_and_record(self):
        source = self._read("static", "index.html")
        footer = source[source.index('<footer class="ptt-footer">'):
                        source.index("</footer>", source.index('<footer class="ptt-footer">'))]
        self.assertIn('id="btn-cq"', footer)
        self.assertLess(footer.index('id="btn-cq"'), footer.index('id="btn-tune"'))

    def test_button_sends_the_cq_command(self):
        ui = self._read("static", "ft710_ui.js")
        self.assertIn("btn-cq", ui)
        self.assertIn("sendCommand('cq'", ui)

    def test_main_js_ingests_cq_state_and_full_state(self):
        main = self._read("static", "ft710_main.js")
        self.assertIn('case "cqState"', main)
        self.assertIn("radioState.cq", main)
        self.assertIn("msg.cq", main)                 # fullState snapshot
        self.assertIn("renderCqState", main)

    def test_renderer_has_the_four_states(self):
        ui = self._read("static", "ft710_ui.js")
        body = ui[ui.index("function renderCqState("):]
        body = body[:body.index("\nfunction ")]
        self.assertIn("cq-active", body)
        self.assertIn("calling", body)
        self.assertIn("complete", body)
        self.assertIn("aborted", body)

    def test_button_is_disabled_on_a_gated_model(self):
        ui = self._read("static", "ft710_ui.js")
        self.assertIn("btn-cq", ui)
        self.assertIn("tx_gated", ui)

    def test_styles_exist(self):
        css = self._read("static", "ft710.css")
        self.assertIn(".cq-button", css)
        self.assertIn(".cq-button.cq-active", css)
