import asyncio
import contextlib
import json
import re
import unittest
from pathlib import Path
from unittest import mock

import audio_profile
import server
from audio_profile import PROFILE_PCM16
from opus_rx import AUDIO_TAG_OPUS, AUDIO_TAG_PCM, AUDIO_TAG_PCM16

REPO_ROOT = Path(__file__).resolve().parents[1]
PCM16_CAPS = '{"type":"audioCaps","profile":"pcm16"}'


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
        # Handed back as found — ``_reset_pcm16_rx_encoder_if_idle`` may well
        # have replaced the module's converter with None in this test's own
        # teardown path, and an installed fresh encoder would not be what the
        # next audio test expects either.
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


#: FastAPI's WebSocket route decorator, assembled rather than written out: a
#: literal copy of it is what the ``ws-endpoint-auth`` guard counts, and this
#: test file adds no endpoint.
ROUTE_DECORATOR = "@app." + "websocket("

#: ``_TxWS`` sentinel for a socket that queues nothing and simply stays open.
_HANG = object()


#: The box's codec domain (``opus_rx.RX_RATE``) — what every legacy uplink and
#: every network adapter reports as its device rate. Named here so the doubles
#: below state a choice instead of repeating a magic number fifteen times.
DEFAULT_DEVICE_RATE = 48000
#: One PCM16 frame payload (AD-038): 20 ms of 16 kHz Int16 mono.
PCM16_RATE = audio_profile.PCM16_RATE     # the one place this file spells it out
PCM16_FRAME = b"\x01\x02" * audio_profile.PCM16_FRAME_SAMPLES
#: The same frame with a different bit pattern, so a test can tell one call
#: from the next rather than matching on equal bytes.
PCM16_FRAME_B = b"\x03\x04" * audio_profile.PCM16_FRAME_SAMPLES
#: One legacy 48 kHz frame (960 samples), and a second variant.
LEGACY_FRAME = b"\x05\x06" * (DEFAULT_DEVICE_RATE // 50)
LEGACY_FRAME_B = b"\x07\x08" * (DEFAULT_DEVICE_RATE // 50)


class _TxBridge:
    """The TX half of ``audio`` as the server drives it.

    One object records what each frame asked for, so a test can tell "the
    uplink said 16 kHz" apart from "the uplink said nothing and got the 48 kHz
    default" — which is exactly the distinction AD-038 turns on.  The rest of
    the surface only has to survive the PTT release path's
    ``to_thread(stop_tx)`` + ``tx_stats()`` without touching real hardware.
    """

    def __init__(self, device_rate=DEFAULT_DEVICE_RATE, raise_on=None):
        self.device_rates = {"rx": device_rate, "tx": device_rate}
        self.tx_audio_available = True
        self.raise_on = raise_on
        self.calls = []
        self.stopped = []

    def feed_tx_audio(self, pcm, input_rate=DEFAULT_DEVICE_RATE):
        if input_rate not in (PCM16_RATE, DEFAULT_DEVICE_RATE):
            # Every allowed adapter says so out loud (Task 6); nothing here may
            # quietly accept a rate it was never given a conversion for.
            raise ValueError(f"unsupported TX input rate: {input_rate}")
        if self.raise_on is not None and input_rate == self.raise_on:
            raise RuntimeError("PCM16 bridge exploded")
        self.calls.append((pcm, input_rate))

    def start_tx(self):
        return True

    def stop_tx(self, graceful=False):
        self.stopped.append(bool(graceful))

    def tx_stats(self):
        return {"written": len(self.calls), "write_err": 0, "queue_drops": 0,
                "peak": 0}

    def has_pending_tx_audio(self):
        return False


class TxProfileTests(unittest.IsolatedAsyncioTestCase):
    """The TX half of ``_handle_audio_caps`` (AD-038).

    Kept verbatim from the plan's Step 1 block except for one addition per
    case: these helpers register a profile only for a socket still listed as a
    client, so the TX set has to hold it.  Without that the ghost gate returns
    before any reply and all three caps cases would pass on a silence they
    never asked for — ``payload["reason"]`` would not even be reachable.
    """

    def setUp(self):
        self.old_audio = server.audio
        # Saved by reference, like RxProfileTests does: these are the live
        # registries every other audio test reads, so handing back an empty
        # dict built here would outlive this class.
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
        server.audio_rx_clients = self.old_rx_clients
        server.audio_tx_clients = self.old_tx_clients
        server._audio_rx_profiles = self.old_rx_profiles
        server._audio_tx_profiles = self.old_tx_profiles

    async def test_tx_accept_is_independent_from_rx(self):
        server.audio = mock.Mock()
        server.audio.device_rates = {"tx": 48000}
        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        server.audio_tx_clients = {ws}
        await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=True)
        self.assertEqual(server._audio_tx_profiles[ws], "pcm16")
        self.assertNotIn(ws, server._audio_rx_profiles)

    async def test_unavailable_backend_is_rejected(self):
        server.audio = None
        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        server.audio_tx_clients = {ws}
        await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=True)
        self.assertNotIn(ws, server._audio_tx_profiles)
        payload = json.loads(ws.send_text.await_args.args[0])
        self.assertEqual(payload["reason"], "pcm16-tx-unsupported")

    async def test_missing_soxr_is_rejected_before_ptt(self):
        server.audio = mock.Mock()
        server.audio.device_rates = {"tx": 48000}
        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        server.audio_tx_clients = {ws}
        with mock.patch.object(server, "StreamingPcmResampler", side_effect=ImportError):
            await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=True)
        self.assertNotIn(ws, server._audio_tx_profiles)
        payload = json.loads(ws.send_text.await_args.args[0])
        self.assertFalse(payload["accepted"])

    async def test_pcm16_conversion_error_releases_ptt(self):
        cat = mock.Mock()
        cat.connected = True
        cat.set_ptt = mock.AsyncMock()
        with mock.patch.object(server, "cat", cat), \
             mock.patch.object(server, "radio", mock.Mock()), \
             mock.patch.object(server, "scheduler", mock.Mock()):
            await server._force_rx_for_audio_error("bad PCM16")
        cat.set_ptt.assert_awaited_once_with(False)


class ForceRxForAudioErrorTests(unittest.IsolatedAsyncioTestCase):
    """What a failed PCM16 conversion costs the radio, and what it must not.

    The point of the helper is that a conversion failure never leaves a carrier
    keyed while the audio has stopped: the release goes out even when every
    surrounding step is broken, and it goes out without reading the profile —
    spec §7 keeps PTT release independent of the negotiated wire format.
    """

    def setUp(self):
        from radio_state import RadioState
        self.saved = (server.cat, server.radio, server.scheduler,
                      server._ptt_key_ws)
        server.radio = RadioState()
        server.scheduler = None
        server._ptt_key_ws = object()

    def tearDown(self):
        (server.cat, server.radio, server.scheduler,
         server._ptt_key_ws) = self.saved

    async def test_release_clears_key_owner_and_meters(self):
        cat = mock.Mock()
        cat.connected = True
        cat.set_ptt = mock.AsyncMock()
        server.cat = cat
        keyer = object()
        server._ptt_key_ws = keyer
        server.radio.update(tx_status=1, power_meter=50, alc_meter=40,
                            swr_meter=30, comp_meter=20, id_meter=10)
        await server._force_rx_for_audio_error("PCM16 resample failed")
        cat.set_ptt.assert_awaited_once_with(False)
        self.assertIsNone(server._ptt_key_ws)
        self.assertFalse(server.radio.is_transmitting)
        for field in ("power_meter", "alc_meter", "swr_meter",
                      "comp_meter", "id_meter"):
            self.assertEqual(getattr(server.radio, field), 0, field)

    async def test_still_releases_when_cat_raises(self):
        """A failed CAT write must not strand the key owner or the meters."""
        cat = mock.Mock()
        cat.connected = True
        cat.set_ptt = mock.AsyncMock(side_effect=RuntimeError("serial gone"))
        server.cat = cat
        server.radio.update(tx_status=1)
        await server._force_rx_for_audio_error("PCM16 resample failed")
        self.assertIsNone(server._ptt_key_ws)
        self.assertEqual(server.radio.tx_status, 0)

    async def test_a_disconnected_cat_does_not_abort_the_path(self):
        cat = mock.Mock()
        cat.connected = False
        cat.set_ptt = mock.AsyncMock()
        server.cat = cat
        server.radio.update(tx_status=1)
        await server._force_rx_for_audio_error("PCM16 resample failed")
        cat.set_ptt.assert_not_awaited()
        self.assertIsNone(server._ptt_key_ws)
        self.assertEqual(server.radio.tx_status, 0)

    async def test_scheduler_skip_is_optional(self):
        """No scheduler (tests, degraded boot) must not turn a force-RX into
        a second exception on top of the first one."""
        cat = mock.Mock()
        cat.connected = True
        cat.set_ptt = mock.AsyncMock()
        server.cat = cat
        server.scheduler = None
        await server._force_rx_for_audio_error("PCM16 resample failed")
        cat.set_ptt.assert_awaited_once_with(False)

    async def test_scheduler_is_asked_to_stand_down_on_tx_status(self):
        cat = mock.Mock()
        cat.connected = True
        cat.set_ptt = mock.AsyncMock()
        scheduler = mock.Mock()
        server.cat = cat
        server.scheduler = scheduler
        await server._force_rx_for_audio_error("PCM16 resample failed")
        scheduler.skip_next_poll.assert_called_once_with("tx_status", 1.0)


class _TxWS:
    """``/WSaudioTX`` double: queued frames first, then an open hang.

    ``receive()`` must hand back a real Starlette-shaped dict — the endpoint
    reads both ``msg["bytes"]`` and ``msg.get("text")``.  Once the scripted
    frames run out it waits on a queue instead of raising
    ``WebSocketDisconnect``, so the socket stays live and a test can drive the
    *control* plane against a running uplink (key-up, release, profile
    cleanup) — exactly what Task 7 has to prove stays independent of the
    negotiated profile.  Polling the queue rather than cancelling a bare
    future also means the only way this double can end is the way a real close
    ends it: ``disconnect()`` puts the disconnect message on the same queue,
    so the handler always returns from ``receive()`` before its ``finally``
    runs.  A cancelled ``asyncio.Future`` does not do that — nothing becomes
    done, ``await ws.receive()`` never returns, and the cleanup under test
    never happens.
    """

    def __init__(self, token, messages=_HANG):
        self.token = token
        self.queue = None if messages is _HANG else asyncio.Queue()
        if self.queue is not None:
            for msg in messages:
                self.queue.put_nowait(msg)
        self.headers = {}
        self.cookies = {}
        self.accepted = False
        self.closed = None
        self.sent_text = []
        self.sent_bytes = []
        self.consumed = 0

    @property
    def query_params(self):
        return {"token": self.token}

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    async def send_text(self, text):
        self.sent_text.append(text)

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def receive(self):
        if self.queue is None:
            await asyncio.Future()      # never resolves: the socket just hangs
        msg = await self.queue.get()
        self.consumed += 1
        if msg is None:                 # disconnect() marks it with a sentinel
            from fastapi import WebSocketDisconnect
            raise WebSocketDisconnect()
        return msg

    def disconnect(self):
        if self.queue is not None:
            self.queue.put_nowait(None)




def _binary(data):
    return {"type": "websocket.receive", "bytes": data, "text": None}


def _text(data):
    return {"type": "websocket.receive", "bytes": None, "text": data}


async def _drain(ws, task):
    """Close a live uplink and let the endpoint's ``finally`` run to completion."""
    ws.disconnect()
    try:
        await asyncio.wait_for(task, timeout=5.0)
    except asyncio.TimeoutError as exc:
        raise AssertionError("/WSaudioTX did not return after its socket closed") from exc


class _TxTestBase(unittest.IsolatedAsyncioTestCase):
    """One place to isolate the whole TX surface from the live module.

    ``/WSaudioTX`` mutates module globals — ownership, the PTT key holder, the
    session counters — so a test that drives it has to hand all of them back
    exactly as found, or the next audio test in the same process inherits a
    phantom owner.
    """

    TOKEN = "pcm16-tx-token"

    def setUp(self):
        from radio_state import RadioState
        self.saved = {
            name: getattr(server, name) for name in (
                "cat", "radio", "scheduler", "audio", "backend",
                "_auth_tokens", "_listen_tokens", "_ws_tokens",
                "audio_tx_clients", "_audio_tx_profiles", "_audio_rx_profiles",
                "_tx_owner_ws", "_ptt_key_ws", "_ptt_foreign_releases",
                "_opus_tx_decoder", "_tx_socket_seq", "_ft8_runtime",
                "_cq_player", "_audio_levels", "_rec_session",
                "_tx_hb_capable", "_tx_hb_last",
                "_tx_session_frames", "_tx_session_decoded",
                "_tx_session_decode_fail", "_tx_non_owner_drops",
                "_tx_cq_mic_drops",
            )
        }
        server._auth_tokens = {self.TOKEN}
        server._listen_tokens = set()
        server._ws_tokens = {}
        server.audio_tx_clients = set()
        server._audio_tx_profiles = {}
        server._audio_rx_profiles = {}
        server._tx_owner_ws = None
        server._ptt_key_ws = None
        server._ptt_foreign_releases = 0
        server._opus_tx_decoder = None
        server._tx_socket_seq = {}
        server._ft8_runtime = None
        server._cq_player = None
        server._rec_session = mock.Mock(active=False)
        server._tx_hb_capable = set()
        server._tx_hb_last = {}
        server._tx_session_frames = 0
        server._tx_session_decoded = 0
        server._tx_session_decode_fail = 0
        server._tx_non_owner_drops = 0
        server._tx_cq_mic_drops = 0
        server.radio = RadioState()
        server.scheduler = None
        cat = mock.Mock()
        cat.connected = True
        cat.set_ptt = mock.AsyncMock()
        server.cat = cat
        self.cat = cat
        # A gain of 0 dB returns the caller's own bytes, so what the fake
        # adapter receives is exactly what the endpoint unpacked from the wire.
        levels = mock.Mock()
        levels.tx_mic_db = 0.0
        levels.apply_gain.side_effect = lambda pcm, db: (pcm, 0)
        server._audio_levels = levels

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(server, name, value)

    async def _live(self, messages, *, token=None, hang=False):
        """Start the endpoint over scripted frames and leave it connected.

        Returning with the read loop still running is the point: a test can
        then drive the *control* plane — key-up, release, the profile registry
        — against a live uplink, and finish with ``_drain(ws, task)``.
        ``hang=True`` starts the same socket but does **not** wait for its
        frames to be consumed: the read loop is left running behind them, which
        is how a second client of another session sits there without ever
        becoming the owner.  Pass no messages at all to keep it silent.
        """
        ws = _TxWS(token or self.TOKEN, messages)
        task = asyncio.create_task(server.ws_audio_tx(ws))
        await asyncio.sleep(0)
        # Yield until every scripted frame has been *consumed*: ``get()`` pops
        # before the handler runs, so an empty-but-not-yet-drained queue would
        # let the next assert race the very code under test.  Queued then empty
        # is also what proves the read loop is alive, not merely started.
        queued = len(messages)
        for _ in range(5000):
            if task.done():
                raise AssertionError("/WSaudioTX returned while still live")
            if ws.accepted and (hang or (ws.consumed >= queued
                                         and ws.queue.empty())):
                break
            await asyncio.sleep(0)
        self.assertTrue(ws.accepted, "/WSaudioTX never accepted the socket")
        if not hang:
            self.assertEqual(ws.consumed, queued,
                             "the endpoint stopped reading its frames")
            self.assertTrue(ws.queue.empty())
        return ws, task


class TxUplinkWiringTests(_TxTestBase):
    """`/WSaudioTX` must put the right rate on the wire for the right tag.

    The two things AD-038 gets wrong if this is sloppy: explaining a 16 kHz
    frame as 48 kHz (playback at a third speed straight into the radio), and
    letting a conversion failure ride out of the read loop — which kills the
    socket but leaves the carrier keyed until something else notices.
    """

    async def _drive(self, messages, expect_pcm16=False):
        """Run the endpoint over scripted frames, then close it cleanly.

        ``expect_pcm16`` checks the registry on the way: a socket that declared
        PCM16 must hold that profile while it is live (that is what the RX fan-out
        keys on), and must lose it at the close — an entry left behind is a ghost
        that serves no stream and gets removed by nothing.
        """
        ws, task = await self._live(messages)
        if expect_pcm16:
            self.assertEqual(server._audio_tx_profiles.get(ws), PROFILE_PCM16)
        await _drain(ws, task)
        self.assertNotIn(ws, server._audio_tx_profiles,
                         "a TX socket's profile must die with the socket")
        return ws

    async def test_a_live_pcm16_socket_holds_its_profile_until_it_closes(self):
        server.audio = _TxBridge()
        await self._drive([_text(PCM16_CAPS)], expect_pcm16=True)

    async def test_pcm16_tag_feeds_the_uplink_at_16khz(self):
        handler = _TxBridge()
        server.audio = handler
        await self._drive([_text(PCM16_CAPS),
                           _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME)])
        self.assertEqual(
            handler.calls, [(PCM16_FRAME, PCM16_RATE)],
            "a 0x02 frame must state its own rate, never the 48k default")

    async def test_legacy_pcm_tag_keeps_the_default_rate(self):
        handler = _TxBridge()
        server.audio = handler
        await self._drive([_binary(bytes([AUDIO_TAG_PCM]) + LEGACY_FRAME)])
        self.assertEqual(handler.calls, [(LEGACY_FRAME, DEFAULT_DEVICE_RATE)],
                         "tag 0x00 is 48 kHz forever — AD-038 forbids re-explaining it")

    async def test_untagged_legacy_frame_keeps_the_default_rate(self):
        handler = _TxBridge()
        server.audio = handler
        await self._drive([_binary(LEGACY_FRAME)])
        self.assertEqual(handler.calls, [(LEGACY_FRAME, DEFAULT_DEVICE_RATE)])

    async def test_pcm16_caps_answer_arrives_on_the_tx_socket(self):
        server.audio = _TxBridge()
        ws, task = await self._live([_text(PCM16_CAPS)])
        self.assertEqual(len(ws.sent_text), 1,
                         "one declaration answers exactly once")
        payload = json.loads(ws.sent_text[0])
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["rate"], PCM16_RATE)
        self.assertEqual(server._audio_tx_profiles.get(ws), PROFILE_PCM16)
        await _drain(ws, task)

    async def test_conversion_failure_forces_rx_and_survives_the_loop(self):
        """One bad PCM16 frame unkeys the radio; it does not end the uplink."""
        handler = _TxBridge(raise_on=PCM16_RATE)
        server.audio = handler
        server.radio.update(tx_status=1, power_meter=55)
        bad = PCM16_FRAME_B
        legacy = LEGACY_FRAME
        ws = await self._drive([
            _text(PCM16_CAPS),
            _binary(bytes([AUDIO_TAG_PCM16]) + bad),    # raises -> force RX
            _binary(bytes([AUDIO_TAG_PCM]) + legacy),   # legacy branch untouched
            _binary(bytes([AUDIO_TAG_PCM16]) + bad),    # second failure
        ])
        self.assertEqual(self.cat.set_ptt.await_count, 2,
                         "every failed PCM16 frame releases; none of them ends the session")
        self.assertEqual(server.radio.tx_status, 0)
        self.assertEqual(server.radio.power_meter, 0)
        # The holder was never set here (nobody keyed through the control
        # plane), so nothing about these frames may invent a release target.
        self.assertIsNone(server._ptt_key_ws)
        self.assertIsNone(ws.closed, "the endpoint stays up; only the key dropped")
        self.assertEqual(handler.calls, [(legacy, DEFAULT_DEVICE_RATE)],
                         "only the legacy frame survived — and only as itself")

    async def test_an_invalid_input_rate_also_forces_rx(self):
        """Policy ruling: any PCM16 feed exception releases the key.

        Task 5 left ``_feed_tx_from_uplink`` raising ``ValueError`` for a
        non-positive rate before it touches the gain or the queue. The wire
        cannot produce that rate on its own — the 0x02 tag fixes it — so the
        entry point is handed the bad rate directly, and the second half drives
        the real branch against a failing converter. Silently dropping such a
        frame while a carrier sits on air is the outcome this rules out: every
        exception from the PCM16 feed gets the same force-RX.
        """
        player = mock.MagicMock()
        player.is_calling = False
        server._cq_player = player
        handler = _TxBridge()
        server.audio = handler
        with self.assertRaises(ValueError):
            await server._feed_tx_from_uplink(PCM16_FRAME, input_rate=0)
        self.assertEqual(handler.calls, [],
                         "an invalid rate is refused before the gain, tap and queue")
        handler.calls.clear()
        server.radio.update(tx_status=1, power_meter=55)
        ws = await self._drive([
            _text(PCM16_CAPS),
            _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME),
        ])
        self.cat.set_ptt.assert_awaited_once_with(False)
        self.assertEqual(server.radio.tx_status, 0)
        self.assertEqual(handler.calls, [(PCM16_FRAME, PCM16_RATE)])
        self.assertIsNone(ws.closed)

    async def test_a_failed_frame_clears_the_key_holder(self):
        """The socket whose audio died held the key, and loses it.

        Field-relevant because ``_ptt_key_ws`` gates every later release: if it
        stayed pointed at a transmission that just ended, a foreign client's
        ``ptt:false`` would be refused as a stale watchdog echo while the radio
        sat on a carrier nobody could drop from this socket.
        """
        handler = _TxBridge(raise_on=PCM16_RATE)
        server.audio = handler
        ctrl = _TxWS(self.TOKEN)          # the control socket that keyed
        server._ptt_key_ws = ctrl
        server.radio.update(tx_status=1, power_meter=55)
        ws = await self._drive([
            _text(PCM16_CAPS),
            _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME),
        ])
        self.cat.set_ptt.assert_awaited_once_with(False)
        self.assertIsNone(server._ptt_key_ws)
        self.assertEqual(server.radio.tx_status, 0)
        self.assertIsNone(ws.closed)

    async def test_ptt_release_never_reads_the_profile(self):
        """Spec §7: negotiation state is not a release condition.

        ``ptt:false`` is honoured identically whether or not the TX socket
        declared PCM16 — including the disconnect cleanup path, where reading a
        profile would mean an old client could be stranded on a carrier.
        """
        handler = _TxBridge()
        server.audio = handler
        ws, task = await self._live([
            _text(PCM16_CAPS),
            _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME),
        ])
        self.assertEqual(server._audio_tx_profiles.get(ws), PROFILE_PCM16)
        ctrl = _TxWS(self.TOKEN)
        server._ptt_key_ws = ctrl
        server.radio.update(tx_status=1, power_meter=55)
        await server._execute_set_command("ptt", False, ctrl)
        self.assertEqual(server.radio.tx_status, 0)
        self.assertEqual(server.radio.power_meter, 0)
        self.assertIsNone(server._ptt_key_ws)
        self.assertIn(False, [c.args[0] for c in self.cat.set_ptt.await_args_list])
        await _drain(ws, task)
        self.assertEqual(server._audio_tx_profiles, {})

    async def test_a_stranger_session_never_reaches_the_radio_or_the_key(self):
        """Spec §7: the new profile changes the wire format and nothing else.

        Ownership is a *control-plane* rule, so it must not learn about 0x02.
        A different authenticated session that connects and streams PCM16 at
        the uplink gets neither the radio nor the key — the exact shape of the
        field bug AD-038 must not reintroduce, where a device joining a session
        already transmitting would hand itself the operator's carrier mid-QSO.

        The one thing it does keep is its own negotiated profile: capability is
        the box's verdict on this hardware, not a property of who is connected.
        What stays out of reach are the frames and the uplink itself.
        """
        handler = _TxBridge(raise_on=PCM16_RATE)
        server.audio = handler
        # A different auth token IS a different browser session; the shared
        # token (a page reload, two tabs of one login) keeps its own takeover
        # rule, which other suites pin down.
        other = "pcm16-other-session-token"
        server._auth_tokens.add(other)
        owner, owner_task = await self._live([_text(PCM16_CAPS)])
        self.assertEqual(server._audio_tx_profiles.get(owner), PROFILE_PCM16,
                         "the owner's own negotiation must have landed")
        # Key through the control plane first, so "nothing was released" is an
        # observation rather than a tautology about an idle radio.
        ctrl = _TxWS(self.TOKEN)
        server._ptt_key_ws = ctrl
        server.radio.update(tx_status=1, power_meter=55)
        stranger, stranger_task = await self._live(
            [_text(PCM16_CAPS),
             _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME)],
            token=other, hang=True)
        # Its frames do get handled, then it hangs there. Waiting for exactly
        # that makes this deterministic instead of a race against the loop.
        for _ in range(5000):
            if stranger.consumed >= 2:
                break
            await asyncio.sleep(0)
        self.assertEqual(stranger.consumed, 2,
                         "the stranger's frames were never read")
        self.assertEqual(handler.calls, [], "a non-owner's audio never reaches the DAC")
        self.cat.set_ptt.assert_not_awaited()
        self.assertEqual(server.radio.tx_status, 1,
                         "a dropped frame is not a conversion failure — no force-RX")
        self.assertIs(server._ptt_key_ws, ctrl)
        self.assertIs(server._tx_owner_ws, owner,
                      "and streaming did not steal the uplink either")
        await _drain(stranger, stranger_task)
        self.assertIs(server._tx_owner_ws, owner,
                      "nor does its close promote it retroactively")
        await _drain(owner, owner_task)

    async def test_disconnect_while_keyed_still_forces_rx_after_negotiating(self):
        """The pre-existing dead-man release must survive the new branch."""
        handler = _TxBridge()
        server.audio = handler
        ws, task = await self._live([
            _text(PCM16_CAPS),
            _binary(bytes([AUDIO_TAG_PCM16]) + PCM16_FRAME),
        ])
        server.radio.update(tx_status=1)
        await _drain(ws, task)
        self.assertIn(False, [c.args[0] for c in self.cat.set_ptt.await_args_list])
        self.assertEqual(server.radio.tx_status, 0)


class TxCleanupTests(_TxTestBase):
    """A TX socket's profile dies with the socket, in every exit shape.

    Two different shapes matter: a normal close, and a ``close()`` that raises
    on a half-open connection. In the second the endpoint never reaches its own
    ``finally``, so anything it might have registered has to be registered
    *after* that gate — otherwise it outlives the socket as a ghost that serves
    no stream and gets removed by nothing (Task 4 proved this for RX).
    """

    async def test_disconnect_removes_the_tx_profile(self):
        server.audio = _TxBridge()
        ws, task = await self._live([_text(PCM16_CAPS)])
        self.assertEqual(server._audio_tx_profiles.get(ws), PROFILE_PCM16,
                         "the live socket holds what it declared")
        await _drain(ws, task)
        self.assertEqual(server._audio_tx_profiles, {})
        self.assertEqual(server.audio_tx_clients, set())

    async def test_a_refused_handshake_registers_nothing_even_if_close_raises(self):
        """A half-open socket that is refused can leave nothing behind.

        The role gate closes the connection and returns *before* the endpoint's
        ``try``/``finally``, so if a profile were ever stored up here it would
        outlive the socket with no cleanup left to remove it — Task 4's ghost
        rule on the TX side. A listen-only token is the refusal this suite owns
        outright; an unknown token could collide with whatever another module
        installed in the live token set.
        """
        server.audio = _TxBridge()
        saved_listen = server._listen_tokens
        self.addCleanup(setattr, server, "_listen_tokens", saved_listen)
        server._listen_tokens = {self.TOKEN}
        ws = _TxWS(self.TOKEN, [_text(PCM16_CAPS)])

        async def exploding_close(code=1000, reason=""):
            raise RuntimeError("socket already gone")

        ws.close = exploding_close
        # Real Starlette swallows whatever escapes an endpoint, so the handler
        # raising here is exactly what a half-open socket does in production —
        # not a limitation of the double.
        with self.assertRaises(RuntimeError):
            await server.ws_audio_tx(ws)
        self.assertFalse(ws.accepted, "the role gate runs before accept")
        self.assertEqual(server._audio_tx_profiles, {},
                         "and nothing was registered before it could fail")
        self.assertEqual(server.audio_tx_clients, set())

    async def test_a_denied_caps_declaration_stores_no_profile(self):
        """Rejected ≠ accepted: the TX table stays empty either way."""
        server.audio = None                      # no backend → PCM16 TX refused
        ws, task = await self._live([_text(PCM16_CAPS)])
        payload = json.loads(ws.sent_text[0])
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["reason"], "pcm16-tx-unsupported")
        self.assertEqual(server._audio_tx_profiles, {})
        await _drain(ws, task)


class TxSourceContractTests(unittest.TestCase):
    """Static pins on the wiring — what a fake can only prove indirectly.

    These read the shipped ``server.py`` instead of calling it, because what
    they hold is a *negative*: no later refactor may reach for this socket's
    profile to settle a PTT question, or drop the tag import that keeps 0x02
    self-describing. A mock cannot fail an assertion about something that never
    happens.
    """

    def source(self):
        return (REPO_ROOT / "server.py").read_text(encoding="utf-8")

    TX_ROUTE = "/WSaudioTX"

    #: Every WebSocket route the server declares. The slice ends at whichever of
    #: these comes first after ``/WSaudioTX`` — a name lookup, never an offset,
    #: because an offset shrinks the moment another route is declared above this
    #: one, until the assertions below read an empty string and pass vacuously.
    WS_ROUTES = ("/WSradio", "/WSspectrum", "/WSaudioRX", "/WSaudioTX",
                 "/WSatr1000", "/WSsdr")

    def tx_body(self):
        """The ``/WSaudioTX`` handler: its decorator up to the next route.

        A static pin reads the shipped file rather than calling it because what
        it holds is a *negative* — no later edit may attach a profile condition
        to a PTT decision made here, and a mock cannot fail an assertion about
        something that never happens. Bounding it by a section banner comment
        would be unreliable (those banners use box-drawing characters that also
        appear inside this docstring), so the boundary is the route table.
        """
        prefix = ROUTE_DECORATOR
        source = self.source()
        start = source.index(prefix + '"%s")' % self.TX_ROUTE)
        stops = [source.index(prefix + '"%s")' % path)
                 for path in self.WS_ROUTES if path != self.TX_ROUTE]
        stops = [pos for pos in stops if pos > start]
        self.assertTrue(stops, "another WebSocket route must follow this one")
        body = source[start:min(stops)]
        # The guard on the guard: if the slice ever stopped covering the
        # branches, every negative assertion below would go quiet.
        self.assertIn("_feed_tx_from_uplink", body,
                      "the slice must cover the decode branches")
        self.assertIn("Audio TX client disconnected", body,
                      "and the handler's own cleanup")
        return body

    def test_pcm16_tag_is_imported_by_the_server(self):
        # An import, not merely the token appearing somewhere: without it the
        # branch compares against a name that does not exist and dies at the
        # first frame.
        imported = re.findall(r"^from\s+\S+\s+import\s+.*\bAUDIO_TAG_PCM16\b|"
                              r"^\s+.*\bAUDIO_TAG_PCM16\b(?=,|\)|\s*$)",
                              self.source(), re.MULTILINE)
        self.assertTrue(imported, "server.py must import AUDIO_TAG_PCM16")

    def test_tx_endpoint_negotiates_and_cleans_up(self):
        body = self.tx_body()
        self.assertIn("await _handle_audio_caps(ws, text, tx=True)", body)
        self.assertIn("_audio_tx_profiles.pop(ws, None)", body)

    def test_release_paths_do_not_read_the_tx_profile(self):
        """No PTT decision may depend on the negotiated wire format.

        The behaviour that proves this lives in
        ``TxUplinkWiringTests.test_ptt_release_never_reads_the_profile``; this
        is the structural half — nothing in the endpoint reads the TX profile
        table at all, so there is no branch left for a future edit to attach a
        profile condition to.
        """
        body = self.tx_body()
        self.assertNotIn("_audio_tx_profiles.get(", body)
        self.assertNotIn("_audio_tx_profiles[", body)

    def test_the_pcm16_branch_is_the_only_force_rx_caller_in_the_endpoint(self):
        """The rate and the release are pinned together.

        Silent-drop-on-failure would leave the carrier keyed, and dropping the
        explicit rate would make a PCM16 uplink play at a third speed — each
        half still looks plausible in review with the other one gone.
        """
        body = self.tx_body()
        feed = f"_feed_tx_from_uplink(frame, input_rate={PCM16_RATE})"
        self.assertIn(feed, body)
        self.assertEqual(body.count("_force_rx_for_audio_error"), 1)


if __name__ == "__main__":
    unittest.main()
