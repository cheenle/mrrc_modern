"""Box-level audio gains (AD-032) — the server wiring.

The contract these pin is the one an operator cannot see and would notice only
as damage:

* the RX level reaches **the copy encoded for listeners** and nothing else —
  a recording, the FT8 decoder and a local speaker all take the capture as it
  is, because a software gain cannot add information and clipping them corrupts
  a decode or a QSO log;
* the TX mic level reaches the radio *and* the TX recording (what the radio was
  actually given), while CQ playback and the FT8 waveform — which do not pass
  through the microphone path at all — are untouched.
"""

import asyncio
import contextlib
import json
import tempfile
from typing import Any, cast
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import audio_levels as al
import server

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def _pcm(*values: int) -> bytes:
    return np.array(values, dtype="<i2").tobytes()


def _samples(pcm: bytes) -> list:
    return np.frombuffer(pcm, dtype="<i2").tolist()


class _FakeRequest:
    """Just enough of a Starlette Request for the two handlers."""

    def __init__(self, body=None, token: str = "op-token"):
        self._body = body
        self.headers = {"authorization": f"Bearer {token}"}
        self.cookies: dict = {}
        self.query_params: dict = {}

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _request(body=None) -> Any:
    """A stand-in for Starlette's Request, typed Any like the socket fakes.

    The handlers only touch ``.json()`` and the token resolver's ``.headers`` /
    ``.cookies`` / ``.query_params``; casting once here keeps the call sites
    honest about what is being exercised (a real Request needs a scope and a
    receive channel, which would test Starlette rather than the endpoint).
    """
    return cast(Any, _FakeRequest(body))


class _FakeWS:
    def __init__(self):
        self.texts: list = []

    async def send_text(self, payload: str):
        self.texts.append(payload)


class _LevelsCase(unittest.TestCase):
    """Owns the module-level store/levels so a test never writes the real file."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._saved = (server._audio_levels, server._audio_levels_store)
        server._audio_levels_store = al.AudioLevelsStore(
            Path(self._dir.name) / "audio-levels.json")
        server._audio_levels = al.AudioLevels()
        self.addCleanup(self._restore)

    def _restore(self):
        server._audio_levels, server._audio_levels_store = self._saved


class RxLevelTests(_LevelsCase):
    """Drive the real RX loop: who gets the gained block, and who must not."""

    def _run_loop(self, pcm: bytes):
        collected = {"encoded": [], "recorded": [], "speaker": [], "tap": []}

        class Capture:
            _rx_running = True
            _rx_dev_rate = 44100
            _rx_last_peak = 0
            opus_enabled = True

            def read_rx_chunk(self):
                return pcm

            def note_rx_chunk(self, block):
                pass

            def encode_rx_audio(self, block):
                collected["encoded"].append(block)
                return []

            def rx_silent_seconds(self):
                return 0.0

        class Speaker:
            active = True

            def feed(self, block):
                collected["speaker"].append(block)

        class Tap:
            def feed(self, block, timestamp):
                collected["tap"].append(block)

        class Session:
            active = False

        saved = (server.audio, server.audio_rx_clients, server._local_speaker,
                 server._ft8_runtime, server._rec_session, server._rec_tap_rx)
        server.audio = Capture()
        server.audio_rx_clients = {object()}
        server._local_speaker = Speaker()
        server._ft8_runtime = types.SimpleNamespace(rx_tap=Tap())
        server._rec_session = Session()
        server._rec_tap_rx = lambda block, rate: collected["recorded"].append(block)

        async def drive():
            task = asyncio.create_task(server._audio_rx_loop())
            try:
                await asyncio.sleep(0.25)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        try:
            asyncio.run(drive())
        finally:
            (server.audio, server.audio_rx_clients, server._local_speaker,
             server._ft8_runtime, server._rec_session,
             server._rec_tap_rx) = saved
        return collected

    def test_listeners_get_the_gained_block_and_the_taps_get_native_bytes(self):
        pcm = _pcm(0, 100, -100, 1000)
        server._audio_levels = al.AudioLevels(rx_db=6.0206)
        got = self._run_loop(pcm)

        self.assertTrue(got["encoded"], "the loop must encode for its listener")
        self.assertEqual(_samples(got["encoded"][0]), [0, 200, -200, 2000],
                         "listeners get the box-level level")
        for name in ("recorded", "speaker", "tap"):
            self.assertTrue(got[name], f"{name} was not fed at all")
            self.assertIs(got[name][0], pcm,
                          f"{name} must get the native block, untouched")

    def test_the_default_is_the_identity_end_to_end(self):
        pcm = _pcm(0, 100, -100, 1000)
        got = self._run_loop(pcm)
        self.assertIs(got["encoded"][0], pcm,
                      "0 dB must hand the encoder the caller's own bytes")


class TxLevelTests(_LevelsCase, unittest.IsolatedAsyncioTestCase):
    """The mic path: gain is applied once, and the TX recording sees it."""

    def setUp(self):
        _LevelsCase.setUp(self)
        self.fed: list = []
        self.recorded: list = []
        self._saved_tx = (server.audio, server._ft8_transmitting,
                          server._cq_player, server._rec_tap_tx)
        self.audio = types.SimpleNamespace(feed_tx_audio=self.fed.append)
        server.audio = self.audio
        server._ft8_transmitting = lambda: False
        server._cq_player = None
        server._rec_tap_tx = self.recorded.append
        self.addCleanup(self._restore_tx)

    def _restore_tx(self):
        (server.audio, server._ft8_transmitting, server._cq_player,
         server._rec_tap_tx) = self._saved_tx

    async def test_the_gain_reaches_the_radio_and_the_tx_recording(self):
        server._audio_levels = al.AudioLevels(tx_mic_db=6.0206)
        await server._feed_tx_from_uplink(_pcm(0, 100, -100))
        self.assertEqual(_samples(self.fed[0]), [0, 200, -200])
        self.assertIs(self.recorded[0], self.fed[0],
                      "the TX recording must hold what the radio was given")

    async def test_zero_db_forwards_the_same_object(self):
        pcm = _pcm(10, -10)
        await server._feed_tx_from_uplink(pcm)
        self.assertIs(self.fed[0], pcm)

    async def test_a_cq_call_still_owns_tx_and_the_mic_is_not_recorded(self):
        server._audio_levels = al.AudioLevels(tx_mic_db=12.0)
        server._cq_player = types.SimpleNamespace(is_calling=True)
        saved_drops = server._tx_cq_mic_drops
        try:
            await server._feed_tx_from_uplink(_pcm(1, 2))
        finally:
            server._tx_cq_mic_drops = saved_drops
        self.assertEqual(self.fed, [])
        self.assertEqual(self.recorded, [],
                         "a dropped mic frame never reached the radio, so it "
                         "must not appear in the TX recording either")

    async def test_an_ft8_transmission_does_not_take_mic_audio(self):
        server._ft8_transmitting = lambda: True
        await server._feed_tx_from_uplink(_pcm(1, 2))
        self.assertEqual(self.fed, [])
        self.assertEqual(self.recorded, [])


class RestApiTests(_LevelsCase, unittest.IsolatedAsyncioTestCase):
    async def test_get_reports_both_levels_limits_and_counters(self):
        response = await server.api_audio_levels_get()
        payload = json.loads(bytes(response.body))
        self.assertEqual((payload["tx_mic_db"], payload["rx_db"]), (0.0, 0.0))
        self.assertEqual(payload["limits"]["rx_db"], list(al.RX_LIMITS))
        self.assertEqual(set(payload["stats"]), {"tx", "rx"})

    async def test_post_saves_and_returns_the_full_state(self):
        published: list = []
        saved_clients = server.ctrl_clients
        server.ctrl_clients = {_FakeWS()}
        saved_broadcast = server._broadcast_audio_levels
        try:
            response = await server.api_audio_levels_set(
                _request({"tx_mic_db": 6, "rx_db": 3}))
        finally:
            server.ctrl_clients = saved_clients
            server._broadcast_audio_levels = saved_broadcast
        payload = json.loads(bytes(response.body))
        self.assertEqual((payload["tx_mic_db"], payload["rx_db"]), (6.0, 3.0))
        self.assertEqual(server._audio_levels.to_wire(),
                         {"tx_mic_db": 6.0, "rx_db": 3.0})
        on_disk = json.loads(server._audio_levels_store.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, {"tx_mic_db": 6.0, "rx_db": 3.0})
        self.assertEqual(server._audio_levels_store.source, "default",
                         "a fresh store keeps saying where it started")
        del published

    async def test_a_partial_post_leaves_the_other_value_alone(self):
        server._audio_levels = al.AudioLevels(tx_mic_db=4, rx_db=2)
        response = await server.api_audio_levels_set(_request({"rx_db": 9}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(server._audio_levels.to_wire(),
                         {"tx_mic_db": 4.0, "rx_db": 9.0})

    async def test_out_of_range_is_a_400_and_changes_nothing(self):
        response = await server.api_audio_levels_set(_request({"rx_db": 60}))
        self.assertEqual(response.status_code, 400)
        self.assertIn("-20…30", json.loads(bytes(response.body))["error"])
        self.assertEqual(server._audio_levels.to_wire(),
                         {"tx_mic_db": 0.0, "rx_db": 0.0})
        self.assertFalse(server._audio_levels_store.path.exists(),
                         "a refused request must not create the file")

    async def test_a_broken_body_is_a_400_not_a_500(self):
        response = await server.api_audio_levels_set(
            _request(ValueError("not json")))
        self.assertEqual(response.status_code, 400)

    async def test_the_push_carries_the_same_keys_as_the_rest_surface(self):
        server._audio_levels = al.AudioLevels(tx_mic_db=1, rx_db=2)
        client = _FakeWS()
        saved = server.ctrl_clients
        server.ctrl_clients = {client}
        try:
            await server._broadcast_audio_levels()
        finally:
            server.ctrl_clients = saved
        message = json.loads(client.texts[0])
        self.assertEqual(message["type"], "audioLevels")
        self.assertEqual((message["tx_mic_db"], message["rx_db"]), (1.0, 2.0))
        self.assertIn("limits", message)

    async def test_full_state_carries_the_levels(self):
        server._audio_levels = al.AudioLevels(tx_mic_db=2, rx_db=5)
        message = server._full_state_message({}, [])
        self.assertEqual(message["audioLevels"]["tx_mic_db"], 2.0)
        self.assertEqual(message["audioLevels"]["rx_db"], 5.0)


class FrontendContractTests(unittest.TestCase):
    """The sliders a user must actually find (a missing id looks like no feature)."""

    @classmethod
    def setUpClass(cls):
        cls.index = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.main = (STATIC / "ft710_main.js").read_text(encoding="utf-8")
        cls.ui = (STATIC / "ft710_ui.js").read_text(encoding="utf-8")
        cls.settings = (STATIC / "settings.html").read_text(encoding="utf-8")

    def test_the_menu_has_both_box_level_sliders_with_the_server_ranges(self):
        import re
        tx = re.search(r'id="slider-txdb"[^>]*', self.index, re.S)
        rx = re.search(r'id="slider-rxdb"[^>]*', self.index, re.S)
        self.assertIsNotNone(tx, "菜单里没有 TX 麦克风滑块")
        self.assertIsNotNone(rx, "菜单里没有 RX 滑块")
        assert tx is not None and rx is not None
        self.assertIn('min="-20"', tx.group(0))
        self.assertIn('max="25"', tx.group(0))
        self.assertIn('min="-20"', rx.group(0))
        self.assertIn('max="30"', rx.group(0))
        self.assertIn('id="val-txdb"', self.index)
        self.assertIn('id="val-rxdb"', self.index)

    def test_the_menu_sliders_are_wired_to_the_server_value(self):
        self.assertIn("initAudioLevelSliders", self.ui)
        self.assertIn("renderAudioLevels", self.ui)
        self.assertIn("postAudioLevels", self.main)
        self.assertIn('case "audioLevels"', self.main)
        self.assertIn('fetch(basePath() + "/api/audio/levels"', self.main)
        self.assertIn("radioState.audioLevels = msg.audioLevels || null;", self.main)

    def test_the_box_settings_page_offers_the_same_two_levels(self):
        self.assertIn('id="aud-tx"', self.settings)
        self.assertIn('id="aud-rx"', self.settings)
        self.assertIn("/api/audio/levels", self.settings)
        self.assertIn('id="aud-reset"', self.settings)

    def test_the_browser_mic_trim_now_reaches_25_db(self):
        """The field ask: 2× (+6 dB) was not enough for a quiet headset."""
        self.assertIn("ft710_micVolDb", self.main)
        self.assertIn("Math.max(-20, Math.min(25, db))", self.main)
        self.assertIn("addSlider('slider-micvol', 'val-micvol', 'Mic Vol', -20, 25",
                      self.ui)


if __name__ == "__main__":
    unittest.main()
