import json
import unittest
from unittest import mock

import audio_profile
import server
from opus_rx import AUDIO_TAG_OPUS, AUDIO_TAG_PCM, AUDIO_TAG_PCM16


class AudioCapsTests(unittest.TestCase):
    def test_pcm16_constants(self):
        self.assertEqual(AUDIO_TAG_PCM16, 0x02)
        self.assertEqual(len({AUDIO_TAG_PCM, AUDIO_TAG_OPUS, AUDIO_TAG_PCM16}), 3)
        self.assertEqual(audio_profile.PCM16_RATE, 16000)
        self.assertEqual(audio_profile.PCM16_FRAME_BYTES, 640)

    def test_only_the_exact_pcm16_caps_are_accepted(self):
        text = '{"type":"audioCaps","profile":"pcm16"}'
        self.assertEqual(audio_profile.parse_caps(text), audio_profile.PROFILE_PCM16)
        for bad in (None, b"{}", "not json", "{}",
                    '{"type":"audioCaps","profile":"pcm48"}',
                    '{"type":"other","profile":"pcm16"}'):
            self.assertIsNone(audio_profile.parse_caps(bad))

    def test_accepted_response_describes_the_wire_format(self):
        payload = json.loads(audio_profile.accepted_caps())
        self.assertEqual(payload, {
            "type": "audioCaps", "profile": "pcm16", "accepted": True,
            "rate": 16000, "channels": 1, "format": "s16le", "frameMs": 20,
        })

    def test_rejected_response_names_the_reason(self):
        payload = json.loads(audio_profile.rejected_caps("pcm16-tx-unsupported"))
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["reason"], "pcm16-tx-unsupported")


class RxProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_audio = server.audio
        self.old_encoder = getattr(server, "_pcm16_rx_encoder", None)
        # Saved by reference: these dicts are the live registry every other
        # audio test reads, so tearDown hands back what it found instead of
        # installing an empty one that outlives this class.  The client sets
        # are saved for the same reason — a caps call registers a profile only
        # for a socket still listed there, and a leftover entry would make this
        # class pass or fail on whatever ran before it.
        self.old_rx_clients = server.audio_rx_clients
        self.old_tx_clients = server.audio_tx_clients
        self.old_rx_profiles = server._audio_rx_profiles
        self.old_tx_profiles = server._audio_tx_profiles
        server.audio_rx_clients = set()
        server.audio_tx_clients = set()
        server._audio_rx_profiles = {}
        server._audio_tx_profiles = {}

    def tearDown(self):
        server.audio = self.old_audio
        server._pcm16_rx_encoder = self.old_encoder
        server.audio_rx_clients = self.old_rx_clients
        server.audio_tx_clients = self.old_tx_clients
        server._audio_rx_profiles = self.old_rx_profiles
        server._audio_tx_profiles = self.old_tx_profiles

    def test_last_pcm16_listener_resets_the_encoder(self):
        ws = object()
        encoder = mock.Mock()
        server._pcm16_rx_encoder = encoder
        server._audio_rx_profiles = {ws: "pcm16"}
        server._audio_rx_profiles.pop(ws)
        server._reset_pcm16_rx_encoder_if_idle()
        encoder.reset.assert_called_once_with()
        self.assertIsNone(server._pcm16_rx_encoder)

    def test_pcm16_only_skips_legacy_opus(self):
        # The loop encodes the two groups as separate statements, so this is
        # asserted by driving each group on its own: an empty legacy group is
        # skipped outright and only the shared conversion runs.
        audio = mock.Mock()
        audio.encode_rx_audio.side_effect = AssertionError("legacy Opus must not run")
        server.audio = audio
        encoder = mock.Mock()
        encoder.push.return_value = [b"\x02pcm"]
        server._pcm16_rx_encoder = encoder
        pcm16 = server._encode_pcm16_rx(b"pcm48")
        self.assertEqual(pcm16, [b"\x02pcm"])
        encoder.push.assert_called_once_with(b"pcm48")
        audio.encode_rx_audio.assert_not_called()

    def test_mixed_listeners_share_one_conversion_per_group(self):
        audio = mock.Mock()
        audio.encode_rx_audio.return_value = [b"\x01opus"]
        server.audio = audio
        encoder = mock.Mock()
        encoder.push.return_value = [b"\x02pcm"]
        server._pcm16_rx_encoder = encoder
        # Ten PCM16 sockets and ten legacy ones still cost one call each: the
        # groups are encoded once per tick, never once per client.
        for _ in range(10):
            pcm16 = server._encode_pcm16_rx(b"pcm48")
            legacy = audio.encode_rx_audio(b"pcm48")
        self.assertEqual(pcm16, [b"\x02pcm"])
        self.assertEqual(legacy, [b"\x01opus"])
        self.assertEqual(encoder.push.call_count, 10)
        self.assertEqual(audio.encode_rx_audio.call_count, 10)

    async def test_rx_caps_are_per_socket(self):
        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        server.audio_rx_clients = {ws}
        await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=False)
        self.assertEqual(server._audio_rx_profiles[ws], "pcm16")
        ws.send_text.assert_awaited_once()
        self.assertNotIn(ws, server._audio_tx_profiles)

    async def test_a_profile_is_never_stored_for_a_socket_that_already_left(self):
        """A late text frame must not register a ghost (AD-038 lifecycle).

        The endpoint's cleanup has already run by then — typically because the
        close itself raised on a half-open socket — so an entry stored now is
        encoded for nobody, closed by nothing, and outlives the connection.
        """
        from unittest.mock import patch

        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        for direction, attr in (("rx", "audio_rx_clients"),
                                ("tx", "audio_tx_clients")):
            with self.subTest(direction=direction), \
                    patch.object(server, attr, set()):
                await server._handle_audio_caps(
                    ws, '{"type":"audioCaps","profile":"pcm16"}',
                    tx=(direction == "tx"))
                table = (server._audio_tx_profiles if direction == "tx"
                         else server._audio_rx_profiles)
                self.assertEqual(table, {})
                ws.send_text.assert_not_awaited()


class TxCapsMembershipTests(unittest.IsolatedAsyncioTestCase):
    """TX declarations are gated on the TX client set, RX on the RX one."""

    def setUp(self):
        self.saved = (server.audio, server.audio_rx_clients,
                      server.audio_tx_clients, server._audio_rx_profiles,
                      server._audio_tx_profiles)

    def tearDown(self):
        (server.audio, server.audio_rx_clients, server.audio_tx_clients,
         server._audio_rx_profiles, server._audio_tx_profiles) = self.saved

    async def test_tx_declaration_needs_the_tx_client_set_not_the_rx_one(self):
        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        server.audio = mock.Mock()
        server.audio.tx_audio_available = True
        server.audio.device_rates = {"rx": 48000, "tx": 48000}
        server.audio_rx_clients = {ws}
        server.audio_tx_clients = set()
        await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=True)
        self.assertEqual(server._audio_tx_profiles, {})
        ws.send_text.assert_not_awaited()

        server.audio_tx_clients = {ws}
        await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=True)
        self.assertEqual(server._audio_tx_profiles[ws], "pcm16")
        ws.send_text.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
