"""Scope producer and RX audio adapter for the SCU-LAN10 backend.

Two traps get pinned here, both invisible until a real radio is attached:

* the scope producer must draw its frequency axis from **CAT** (`SS`/`FA`), not
  from the sweep body — and must leave the axis alone when an answer does not
  parse, rather than latching a misread number;
* RX audio arrives as **16 kHz stereo** and every consumer downstream speaks
  48 kHz mono, so the downmix and the rate conversion are each asserted with a
  signal whose answer is known by hand (L=1000/R=0 → 500; 100 ms stays 100 ms).
"""
import asyncio
import concurrent.futures
import time
import unittest
from unittest import mock

import numpy as np

from backends.yaesu.scu_lan import scope_format
from backends.yaesu.scu_lan import protocol as proto
from backends.yaesu.scu_lan.audio import (
    DEVICE_CHANNELS, DEVICE_RATE, RX_CHUNK_SAMPLES, ScuLanAudioHandler,
)
from backends.yaesu.scu_lan.scope import SS_SPAN_HZ, ScuLanScopeProducer


def stereo_body(left: int = 1000, right: int = 0, frames: int = 160) -> bytes:
    """One wire body's worth of PCM: interleaved stereo Int16 LE."""
    interleaved = np.zeros(frames * DEVICE_CHANNELS, dtype="<i2")
    interleaved[0::2] = left
    interleaved[1::2] = right
    return interleaved.tobytes()


def sweep_body(marker: int = 1900, size: int = 4096) -> bytes:
    body = bytearray([210] * size)
    for i in range(marker):
        body[i] = 200 - (i * 37) % 150
    body[marker] = 1
    return bytes(body)


class FakeScope:
    """The `ScopeHandler` fields the producer writes (nothing else)."""

    def __init__(self) -> None:
        self.spectrum_rx1 = []
        self.spectrum_rx2 = []
        self.scope_mode = 0
        self.scope_span = 0
        self.scope_start_freq = 0
        self._last_frame_time = 0.0
        self._fps = 0.0
        self._frame_count = 0
        self.last_update = 0.0
        self._connected = False


class FakeClient:
    def __init__(self) -> None:
        self.on_scope = None
        self.on_audio = None
        self.connected = True


class FakeCat:
    def __init__(self, answers=None) -> None:
        self.answers = answers or {}
        self.asked: list[str] = []

    async def query(self, cmd, timeout=0.4):
        self.asked.append(cmd)
        return self.answers.get(cmd)


class ConversionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = ScuLanAudioHandler(None)

    def test_the_downmix_is_the_mean_of_both_channels(self):
        mono = self.handler._downmix(stereo_body(left=1000, right=0))
        self.assertEqual(mono.size, 160)
        self.assertAlmostEqual(float(mono.mean()), 500.0, places=3)

    def test_a_mono_signal_survives_the_downmix_unchanged(self):
        mono = self.handler._downmix(stereo_body(left=800, right=800))
        self.assertAlmostEqual(float(mono.mean()), 800.0, places=3)

    def test_the_resampler_latency_is_constant_not_a_rate_error(self):
        """16 kHz → 48 kHz is ×3 in samples; a streaming resampler holds a
        fixed few-millisecond delay, and the rate must be exact after it."""
        for _ in range(10):                    # warm the resampler up
            self.handler.convert(stereo_body())
        produced = 0
        bodies = 100                           # 1 s of wire audio
        for _ in range(bodies):
            produced += self.handler.convert(stereo_body()).size
        self.assertEqual(produced, bodies * 160 * 3,
                         "steady state must be exactly ×3 samples")

    def test_an_empty_body_converts_to_nothing(self):
        self.assertEqual(self.handler.convert(b"").size, 0)

    def test_an_odd_byte_count_is_dropped_not_shifted(self):
        """A stray byte must not turn every later sample into a wrong channel."""
        mono = self.handler._downmix(stereo_body() + b"\x00")
        self.assertEqual(mono.size, 160)


class AudioRateTests(unittest.TestCase):
    """The official client offers PCM 48 kHz — the header wins over our default.

    Running a 48 kHz stream through a 16 kHz resampler is a 3x speed/pitch
    error, and it would look like "the audio is slow and low", which nobody
    would connect to a sample-rate constant.
    """

    def test_a_48k_unit_is_followed_and_not_resampled(self):
        from backends.yaesu.scu_lan.client import ScuLanClient
        client = ScuLanClient("127.0.0.1", "u", "p")
        handler = ScuLanAudioHandler(client)
        body = bytearray(24)
        body[4:8] = (48000).to_bytes(4, "little")
        body[22:24] = (1920).to_bytes(2, "little")   # 10 ms of 48 kHz stereo
        client._on_audio(proto.Packet(seq=1, channel=proto.CHANNEL_AUDIO,
                                     msg_type=proto.MSG_ACK_DATA, key=0,
                                     body=bytes(body) + bytes(1920)))
        self.assertEqual(client.audio_rate, 48000)
        self.assertTrue(handler.adopt_rate(client.audio_rate))
        self.assertIsNone(handler._resampler, "48 kHz needs no conversion")
        self.assertEqual(handler.convert(bytes(1920)).size, 480)

    def test_a_16k_unit_keeps_the_default(self):
        from backends.yaesu.scu_lan.client import ScuLanClient
        client = ScuLanClient("127.0.0.1", "u", "p")
        handler = ScuLanAudioHandler(client)
        body = bytearray(24)
        body[4:8] = (16000).to_bytes(4, "little")
        body[22:24] = (640).to_bytes(2, "little")
        client._on_audio(proto.Packet(seq=1, channel=proto.CHANNEL_AUDIO,
                                     msg_type=proto.MSG_ACK_DATA, key=0,
                                     body=bytes(body) + bytes(640)))
        self.assertEqual(client.audio_rate, 16000)
        self.assertFalse(handler.adopt_rate(16000), "no change, no rebuild")

    def test_nonsense_rates_are_ignored_not_adopted(self):
        from backends.yaesu.scu_lan.client import ScuLanClient
        self.assertEqual(ScuLanClient._declared_rate(b"\x00" * 4), 0)
        body = bytearray(8)
        body[4:8] = (7).to_bytes(4, "little")
        self.assertEqual(ScuLanClient._declared_rate(bytes(body)), 0)
        handler = ScuLanAudioHandler(None)
        self.assertFalse(handler.adopt_rate(0))
        self.assertFalse(handler.adopt_rate("nonsense"))  # type: ignore[arg-type]


class ChunkingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = ScuLanAudioHandler(None)
        self.handler._rx_running = True

    def test_a_quiet_stream_is_not_published_before_it_is_running(self):
        handler = ScuLanAudioHandler(None)
        handler._on_pcm(stereo_body())
        self.assertEqual(handler.chunks_out, 0)

    def test_bodies_are_reblocked_into_twenty_millisecond_chunks(self):
        for _ in range(10):                     # 100 ms of wire audio
            self.handler._on_pcm(stereo_body())
        self.assertGreaterEqual(self.handler.chunks_out, 4)
        chunk = self.handler.read_rx_chunk()
        assert chunk is not None
        self.assertEqual(len(chunk), RX_CHUNK_SAMPLES * 2,
                         "20 ms of 48 kHz mono Int16")

    def test_a_listener_that_arrives_late_does_not_hear_stale_audio(self):
        for _ in range(30):
            self.handler._on_pcm(stereo_body())
        self.handler.read_rx_chunk()
        self.assertLessEqual(len(self.handler._rx_audio), 5)
        self.assertGreater(self.handler.chunks_dropped, 0)

    def test_restart_rx_drops_what_buffered_during_the_over(self):
        self.handler._on_pcm(stereo_body())
        self.handler.restart_rx()
        self.assertIsNone(self.handler.read_rx_chunk())

    def test_stopping_rx_stops_publishing(self):
        self.handler.stop_rx()
        self.handler._on_pcm(stereo_body())
        self.assertIsNone(self.handler.read_rx_chunk())

    def test_the_encoder_tags_what_it_hands_to_the_server(self):
        packets = self.handler.encode_rx_audio(b"\x00\x00" * 960)
        self.assertTrue(packets)
        self.assertIsInstance(packets[0], bytes)


class StartupDeadlockTests(unittest.IsolatedAsyncioTestCase):
    """2026-11-16 field: `start_rx` must never wait for the event loop.

    The server submits `start_rx` to a worker thread and then blocks the *loop*
    on that future (`_audio_fut.result(timeout=8)` in the lifespan — written for
    a sound card that open()s inside the worker).  A network handler that waits
    for the loop from that thread deadlocks against it, startup reports
    "Audio RX init timed out", `_audio_ok` is False and `_audio_rx_loop` is
    never created: connected radio, live waterfall, **no RX audio at all**, and
    no counter anywhere that says why.
    """

    def make_handler(self, *, delay: float = 0.0):
        started = asyncio.Event()

        class SlowClient:
            on_audio = None
            connected = False
            audio_rate = DEVICE_RATE

            async def start(self):
                if delay:
                    await asyncio.sleep(delay)
                self.connected = True
                started.set()

        client = SlowClient()
        return client, ScuLanAudioHandler(client), started

    async def test_start_rx_returns_immediately_even_if_the_session_is_slow(self):
        client, handler, started = self.make_handler(delay=0.05)
        begin = time.monotonic()
        self.assertTrue(handler.start_rx())          # called ON the loop here
        elapsed = time.monotonic() - begin
        self.assertLess(elapsed, 0.2,
                        "start_rx blocked the caller waiting for the loop")
        await asyncio.wait_for(started.wait(), 2.0)
        self.assertTrue(handler._rx_running)

    def test_the_server_startup_pattern_no_longer_deadlocks(self):
        """The exact shape of the lifespan: worker thread + loop blocked on it."""
        async def run():
            _client, handler, _started = self.make_handler(delay=5.0)
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = pool.submit(handler.start_rx)
            try:
                begin = time.monotonic()
                ok = future.result(timeout=5)
                elapsed = time.monotonic() - begin
            finally:
                pool.shutdown(wait=False)
            return ok, elapsed

        ok, elapsed = asyncio.run(run())
        self.assertTrue(ok)
        self.assertLess(elapsed, 1.0,
                        "start_rx waited for a loop that was waiting for it")

    async def test_a_second_start_is_ignored_while_the_first_binds(self):
        """The CAT path and the audio adapter both call `start()` at startup.

        The first call awaits four `create_datagram_endpoint` calls before it
        has any `_tasks` to show for it, so a caller that only checks the task
        list binds a **second** set of sockets and clobbers `_sockets` — one
        session, two socket sets, and the ports the first one announced are
        dead.  Four streams, not eight.
        """
        from backends.yaesu.scu_lan import client as client_module

        made: list = []

        class CountingStream(client_module._Stream):
            def connection_made(self, transport):
                made.append(transport)
                super().connection_made(transport)

        instance = client_module.ScuLanClient("127.0.0.1", "u", "p",
                                              base_port=50000)
        with mock.patch.object(client_module, "_Stream", CountingStream):
            first = asyncio.create_task(instance.start())
            await asyncio.sleep(0)               # first one is mid-bind
            await instance.start()               # must not bind again
            await first
        self.assertEqual(len(made), 4, f"bound {len(made)} sockets, not 4")
        self.assertEqual(len(instance._tasks), 1)
        await instance.stop()


class TxGateTests(unittest.TestCase):
    def test_tx_audio_reports_unavailable_until_the_frame_is_known(self):
        handler = ScuLanAudioHandler(None)
        self.assertFalse(handler.tx_audio_available)
        self.assertFalse(handler.start_tx())
        self.assertTrue(handler.tx_stats()["unsupported"])

    def test_that_refusal_is_explained_once_not_every_keystroke(self):
        handler = ScuLanAudioHandler(None)
        with self.assertLogs("yaesu.scu_lan.audio", "WARNING") as logs:
            handler.start_tx()
            handler.start_tx()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("not implemented", logs.output[0])

    def test_a_supplied_sound_card_handler_is_used_verbatim(self):
        tx = mock.Mock()
        tx.start_tx.return_value = True
        handler = ScuLanAudioHandler(None, tx_handler=tx)
        self.assertTrue(handler.tx_audio_available)
        self.assertTrue(handler.start_tx())
        handler.feed_tx_audio(b"\x01\x02")
        tx.feed_tx_audio.assert_called_once_with(b"\x01\x02")


class ScopeProducerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = FakeClient()
        self.scope = FakeScope()
        self.frames: list[FakeScope] = []

        async def on_frame(handler) -> None:
            self.frames.append(handler)

        self.producer = ScuLanScopeProducer(
            self.client, cat=None, scope=self.scope, on_frame=on_frame)
        await self.producer.start()

    async def asyncTearDown(self) -> None:
        await self.producer.stop()

    async def feed(self, body: bytes) -> None:
        self.producer._on_body(body)
        await asyncio.sleep(0)                  # let the frame task run
        if self.producer._task is not None:
            await self.producer._task

    async def test_it_takes_over_the_scope_callback(self):
        self.assertIs(getattr(self.client.on_scope, "__self__", None),
                      self.producer)

    async def test_a_sweep_becomes_an_850_bin_line(self):
        await self.feed(sweep_body())
        self.assertEqual(len(self.scope.spectrum_rx1), scope_format.SPECTRUM_BINS)
        self.assertEqual(len(self.scope.spectrum_rx2), scope_format.SPECTRUM_BINS)
        self.assertEqual(set(self.scope.spectrum_rx2), {0},
                         "the SUB sweep is undecoded upstream — zeros, not a copy")
        self.assertEqual(len(self.frames), 1)
        self.assertTrue(self.scope._connected)

    async def test_an_undecodable_body_is_counted_not_drawn(self):
        await self.feed(b"")
        self.assertEqual(self.frames, [])
        self.assertEqual(self.producer.snapshot()["bad_bodies"], 1)

    async def test_it_keeps_running_when_the_last_waterfall_closes(self):
        """CAT and audio share this session; stopping it would silence them."""
        self.assertTrue(self.producer.keeps_running())

    async def test_stop_detaches_and_further_bodies_do_nothing(self):
        callback = self.client.on_scope
        assert callback is not None
        await self.producer.stop()
        callback(sweep_body())
        await asyncio.sleep(0)
        self.assertEqual(self.frames, [])

    async def test_the_axis_comes_from_cat(self):
        self.producer._take_span("SS0550000;")
        self.producer._take_mode("SS0600000;")
        self.producer._take_center("FA007007000;")
        await self.feed(sweep_body())
        self.assertEqual(self.scope.scope_span, 5)
        self.assertEqual(self.scope.scope_start_freq,
                         7_007_000 - SS_SPAN_HZ[5] // 2)

    async def test_an_answer_that_does_not_parse_leaves_the_axis_alone(self):
        self.producer._take_span("SS0550000;")
        self.producer._take_span("nonsense;")
        self.assertEqual(self.producer.snapshot()["span_hz"], SS_SPAN_HZ[5])


class GeometryRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_refresh_reads_span_mode_and_centre(self):
        cat = FakeCat({"SS05": "SS0550000;", "SS06": "SS0610000;", "FA": "FA014074000;"})
        producer = ScuLanScopeProducer(FakeClient(), cat=cat, scope=FakeScope())
        await producer._refresh_geometry()
        snap = producer.snapshot()
        self.assertEqual(snap["span_hz"], SS_SPAN_HZ[5])
        self.assertEqual(snap["center_hz"], 14_074_000)
        self.assertFalse(snap["centered"], "SS06=1 is a cursor mode")
        self.assertEqual(cat.asked, ["SS05", "SS06", "FA"])

    async def test_a_silent_cat_reports_zero_answers_for_the_backoff(self):
        producer = ScuLanScopeProducer(FakeClient(), cat=FakeCat(),
                                       scope=FakeScope())
        self.assertEqual(await producer._refresh_geometry(), 0)

    async def test_a_partly_answering_cat_still_counts(self):
        cat = FakeCat({"FA": "FA007007000;"})          # only the centre answers
        producer = ScuLanScopeProducer(FakeClient(), cat=cat, scope=FakeScope())
        self.assertEqual(await producer._refresh_geometry(), 1)
        self.assertEqual(producer.snapshot()["center_hz"], 7_007_000)

    async def test_a_silent_cat_leaves_everything_unset(self):
        producer = ScuLanScopeProducer(FakeClient(), cat=FakeCat(),
                                       scope=FakeScope())
        await producer._refresh_geometry()
        snap = producer.snapshot()
        self.assertIsNone(snap["span_hz"])
        self.assertIsNone(snap["center_hz"])
        self.assertTrue(snap["centered"], "default until a mode answer arrives")


if __name__ == "__main__":
    unittest.main()


class RxSourceTests(unittest.TestCase):
    """Which receiver the box plays — MAIN on L, SUB on R (2026-11-16).

    The unit sends the two receivers as the two stereo channels (measured
    `corr(L,R) = -0.06`), and the old downmix averaged them: two different QSOs
    in one stream, with the filter / S-meter / mode addressing MAIN only.
    """

    def handler(self, source="both"):
        h = ScuLanAudioHandler(None)
        h._rx_running = True
        h.set_rx_source(source)
        return h

    def body(self):
        """L = constant 2000, R = constant -2000 → mean is 0, so the channels are
        distinguishable after the resampler (its ~0 latency at DC is irrelevant)."""
        import numpy as np
        stereo = np.tile(np.array([2000, -2000], dtype="<i2"), 160)
        return stereo.tobytes()

    def test_both_averages_the_channels(self):
        block = self.handler("both").convert(self.body())
        self.assertLess(abs(float(block.mean())), 1.0)

    def test_main_takes_the_left_channel(self):
        block = self.handler("main").convert(self.body())
        self.assertGreater(float(block.mean()), 1500)

    def test_sub_takes_the_right_channel(self):
        block = self.handler("sub").convert(self.body())
        self.assertLess(float(block.mean()), -1500)

    def test_an_unknown_source_is_refused(self):
        h = ScuLanAudioHandler(None)
        self.assertFalse(h.set_rx_source("left"))
        self.assertEqual(h.rx_source, "both")

    def test_switching_drops_the_previous_receivers_buffer(self):
        h = self.handler("both")
        for _ in range(4):
            h._on_pcm(self.body())
        self.assertTrue(h.set_rx_source("sub"))
        self.assertIsNone(h.read_rx_chunk())
