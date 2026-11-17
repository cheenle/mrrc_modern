"""PCM16 TX through the network audio adapters (AD-038).

A negotiated PCM16 socket says what rate its bytes are (`feed_tx_audio(pcm,
input_rate=16000)`), and every adapter has to honour that without moving the
rate on the wire: Icom and SCU-LAN delegate to a sound-card handler, Flex
resamples 16 → 24 kHz (the radio only ever takes Opus 24 kHz stereo), SunSDR
resamples 16 → 48 kHz before its existing DSP engine. The legacy default stays
byte-for-byte the pre-feature call — these tests are exactly the four shapes,
plus the rules they share about the streaming bridge's lifetime: it belongs to
one keystroke, every path that ends one (failed start, stop, close) retires it
under the session lock, and a frame that arrives after that must not quietly
build a new one.

No radio, no socket, no sound card. `Pcm16BridgeShapeTests` runs the **real**
soxr bridge on purpose — sample conservation and pitch through a streaming SRC
are exactly the things a mocked resampler cannot show.
"""
import importlib
import threading
import unittest
from unittest import mock

import numpy as np

from audio_resample import StreamingPcmResampler
from backends.flex.audio import FlexAudioHandler
from backends.icom_net.audio import IcomNetAudioHandler
from backends.sunsdr.audio import SunSDRAudioHandler
from backends.yaesu.scu_lan.audio import ScuLanAudioHandler

FRAME_MS = 20
PCM16_FRAME_SAMPLES = 16000 * FRAME_MS // 1000        # 320 @ 16 kHz
SILENCE16 = b"\x00\x00" * PCM16_FRAME_SAMPLES


def _tone_pcm16(freq_hz=1000.0, samples=PCM16_FRAME_SAMPLES, rate=16000):
    t = np.arange(samples) / float(rate)
    return (0.4 * 32767.0 * np.sin(2 * np.pi * freq_hz * t)).astype("<i2").tobytes()


def _dominant_hz(pcm_bytes, rate, skip_samples=2000):
    """Peak bin of a real FFT over the settled part of a stream."""
    samples = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float64)[skip_samples:]
    if samples.size < 512:
        raise AssertionError("stream too short to measure a pitch")
    spec = np.abs(np.fft.rfft(samples))
    freqs = np.fft.rfftfreq(samples.size, 1.0 / rate)
    return float(freqs[int(spec.argmax())])


class Pcm16BackendTests(unittest.TestCase):
    def test_delegates_preserve_legacy_and_forward_pcm16(self):
        for cls in (IcomNetAudioHandler, ScuLanAudioHandler):
            with self.subTest(cls=cls.__name__):
                tx = mock.Mock()
                handler = cls.__new__(cls)
                handler._tx = tx
                handler.feed_tx_audio(b"48")
                tx.feed_tx_audio.assert_called_once_with(b"48")
                handler.feed_tx_audio(b"16", input_rate=16000)
                tx.feed_tx_audio.assert_called_with(b"16", input_rate=16000)

    def test_flex_pcm16_uses_a_16_to_24k_stream(self):
        handler = FlexAudioHandler.__new__(FlexAudioHandler)
        handler._closed = False
        handler._engine = mock.Mock()
        handler._engine.tx_stream_ready = True
        handler._tx_encoder = mock.Mock()
        handler._tx_encoder.push.return_value = []
        handler._down2 = mock.Mock()
        handler._tx_pcm16_resampler = None
        handler._tx_lock = threading.Lock()
        handler._tx_written = handler._tx_write_err = handler._tx_peak = 0
        pcm = np.zeros(320, dtype="<i2").tobytes()
        with mock.patch("backends.flex.audio.StreamingPcmResampler") as cls:
            cls.return_value.process.return_value = np.zeros(480, dtype="<i2").tobytes()
            handler.feed_tx_audio(pcm, input_rate=16000)
        cls.assert_called_once_with(16000, 24000)
        handler._tx_encoder.push.assert_called_once()
        handler._down2.process.assert_not_called()

    def test_sunsdr_pcm16_converts_before_engine(self):
        handler = SunSDRAudioHandler.__new__(SunSDRAudioHandler)
        handler._closed = False
        handler._engine = mock.Mock()
        handler._tx_pcm16_resampler = None
        handler._tx_lock = threading.Lock()
        handler._tx_written = handler._tx_peak = 0
        converted = b"\x01\x00" * 960
        with mock.patch("backends.sunsdr.audio.StreamingPcmResampler") as cls:
            cls.return_value.process.return_value = converted
            handler.feed_tx_audio(SILENCE16, input_rate=16000)
        cls.assert_called_once_with(16000, 48000)
        handler._engine.feed_tx_audio.assert_called_once_with(converted)

    def test_network_tx_resampler_reset_helper(self):
        for cls in (FlexAudioHandler, SunSDRAudioHandler):
            with self.subTest(cls=cls.__name__):
                handler = cls.__new__(cls)
                handler._tx_pcm16_resampler = mock.Mock()
                handler._reset_tx_pcm16_resampler()
                self.assertIsNone(handler._tx_pcm16_resampler)


class Pcm16BridgeLifecycleTests(unittest.TestCase):
    """The bridge is session state: every path that ends a keystroke retires
    it under the session lock, and a frame that arrives after that must not
    quietly build a new one (AD-038)."""

    def _handler(self, cls):
        """A bare adapter whose engine looks like a live, accepted TX session.

        ``_run`` answers truthfully because the engines disagree on purpose:
        ``SunSDREngine.start_tx()`` returns False for a mode with no verified
        TX path, ``SmartSDREngine.start_tx()`` returns None on success — both
        must read as "this keystroke owns a session".  The Flex pacer's own
        state is real (a plain deque), because its TX path reads
        ``tx_stream_id`` and clears the queue through the engine.

        ``tx_active``/``tx_stream_ready`` are set by the tests, never by the
        stub: Flex's pacer is an asyncio task that flips ``tx_active`` when it
        starts but not when the radio later refuses the interlock, so treating
        it as synchronous here would model something the engine does not do.
        """
        handler = cls.__new__(cls)
        handler._closed = False
        handler._engine = mock.Mock()
        handler._engine.tx_active = True
        handler._engine.tx_stream_ready = True
        handler._tx_lock = threading.Lock()
        handler._tx_written = handler._tx_write_err = handler._tx_peak = 0
        handler._tx_pcm16_resampler = None
        if cls is FlexAudioHandler:
            from collections import deque
            handler._engine._tx_queue = deque()
            handler._engine.tx_drops = 0
            handler._engine.tx_stream_id = 0x0B000002
            handler._engine.feed_opus = lambda packet: (
                handler._engine._tx_queue.append(packet))
            handler._tx_encoder = mock.Mock()
            handler._tx_encoder.push.return_value = []
            handler._down2 = mock.Mock()
            handler._down2.process.return_value = np.zeros(0, dtype=np.float64)
            handler._up2 = mock.Mock()
            handler._run = mock.Mock(return_value=None)
        else:
            handler._run = mock.Mock(return_value=True)
        return handler

    def _prepare_close(self, handler, cls):
        """close() reaches the engine only through _run/to_thread and touches
        the RX side plus both encoders; none of that is under test here (the
        encoders stay harmless Mocks, whose ``close()`` close() may call)."""
        handler.stop_rx = mock.Mock()
        handler.opus_encoder = mock.Mock()
        handler._run = mock.Mock(return_value=None)

    def _lock_probe(self, handler):
        """Count acquisitions and nesting depth of ``_tx_lock``.

        A C lock's ``acquire``/``release`` cannot be patched on the instance,
        and patching them would make ``with`` call an unbound function — so the
        probe wraps the real lock and the handler is pointed at the wrapper.
        """
        state = {"depth": 0, "max_depth": 0, "acquires": 0}

        class Probe:
            def __init__(self, inner):
                self._inner = inner

            def acquire(self, blocking=True, timeout=-1.0):
                ok = self._inner.acquire(blocking, timeout)
                if ok:
                    state["acquires"] += 1
                    state["depth"] += 1
                    state["max_depth"] = max(state["max_depth"], state["depth"])
                return ok

            def release(self):
                state["depth"] -= 1
                return self._inner.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *exc):
                self.release()
                return False

        handler._tx_lock = Probe(handler._tx_lock)
        return state

    def test_session_end_takes_the_lock_and_clears_the_bridge(self):
        for cls in (FlexAudioHandler, SunSDRAudioHandler):
            with self.subTest(cls=cls.__name__):
                handler = self._handler(cls)
                handler._tx_pcm16_resampler = mock.Mock()
                state = self._lock_probe(handler)
                handler._tx_session_end()
                self.assertGreaterEqual(state["acquires"], 1,
                                        "_tx_session_end did not take _tx_lock")
                self.assertEqual(state["depth"], 0, "lock left held")
                self.assertIsNone(handler._tx_pcm16_resampler)

    def test_start_tx_never_nests_the_non_reentrant_lock(self):
        """``_tx_lock`` is NOT reentrant: the locked stats block inside
        start_tx must call the bare reset helper, never _tx_session_end — a
        nested acquire would hang the keystroke, not merely fail."""
        for cls in (FlexAudioHandler, SunSDRAudioHandler):
            with self.subTest(cls=cls.__name__):
                handler = self._handler(cls)
                handler._run = mock.Mock(return_value=True)
                state = self._lock_probe(handler)
                self.assertTrue(handler.start_tx())
                self.assertEqual(state["max_depth"], 1, "start_tx nests _tx_lock")
                self.assertEqual(state["depth"], 0, "lock left held")

    def _stop_pacer(self, handler):
        """A stop_tx whose pacer release really happens: the engines flip
        ``tx_active`` synchronously inside ``stop_tx()``, and that flag is what
        an uplink frame reads to decide whether it still owns a session."""
        def release(coro):
            handler._engine.tx_active = False
            return None

        handler._run = mock.Mock(side_effect=release)

    @staticmethod
    def _rebuilt(handler, cls):
        """Bridge objects a feed constructs while ``handler`` is in its current
        state — empty means the uplink was refused before any conversion."""
        built = []

        def track(*args, **kwargs):
            bridge = StreamingPcmResampler(*args, **kwargs)
            built.append(bridge)
            return bridge

        with mock.patch.object(importlib.import_module(cls.__module__),
                               "StreamingPcmResampler", track):
            handler.feed_tx_audio(SILENCE16, input_rate=16000)
        return built

    def test_failed_start_paths_drop_the_bridge(self):
        for cls in (FlexAudioHandler, SunSDRAudioHandler):
            with self.subTest(cls=cls.__name__, path="exception"):
                handler = self._handler(cls)
                # A keystroke can fail while audio is already flowing: the
                # server keys PTT on the event loop and opens the session from
                # a worker thread.  start_tx's own locked reset runs before that
                # await, so a bridge built during the attempt must not survive
                # its failure.
                handler.feed_tx_audio(SILENCE16, input_rate=16000)
                self.assertIsNotNone(handler._tx_pcm16_resampler)
                handler._run = mock.Mock(side_effect=RuntimeError("loop down"))
                self.assertFalse(handler.start_tx())
                self.assertIsNone(handler._tx_pcm16_resampler)

            with self.subTest(cls=cls.__name__, path="no pacer when it lands"):
                handler = self._handler(cls)
                # The server had already released PTT by the time this landed, so
                # the keystroke never owned a session: whatever a frame built
                # around it has to go.  SunSDR additionally learns "no session"
                # from the engine's own answer (an unverified mode such as
                # AM/FM/CW); Flex's engine returns None on success, so its stub
                # does too — and Flex's uplink gate keys off the TX stream rather
                # than the pacer task, which is why only SunSDR has to refuse late
                # frames here.
                def drop(coro):
                    coro.close()
                    handler._engine.tx_active = False
                    return False if cls is SunSDRAudioHandler else None

                handler._run = mock.Mock(side_effect=drop)
                handler.feed_tx_audio(SILENCE16, input_rate=16000)
                self.assertFalse(handler.start_tx())
                self.assertIsNone(handler._tx_pcm16_resampler)
                if cls is SunSDRAudioHandler:
                    self.assertEqual(self._rebuilt(handler, cls), [],
                                     "a dead keystroke rebuilt its bridge")

            with self.subTest(cls=cls.__name__, path="refused before engine"):
                handler = self._handler(cls)
                # The adapter refuses without reaching the engine: Flex through
                # ``tx_stream_ready`` (which its uplink gate also reads, so no
                # frame gets a bridge at all), SunSDR through connectivity —
                # which its pacer-based gate cannot see, and that is exactly why
                # start_tx clears streaming state **before** the await rather
                # than after: a refusal discovered later would otherwise strand
                # whatever a loop-thread frame had already converted.  What both
                # shapes guarantee is that the next key-up starts clean.
                if cls is FlexAudioHandler:
                    handler._engine.tx_stream_ready = False
                    handler.feed_tx_audio(SILENCE16, input_rate=16000)
                    self.assertFalse(handler.start_tx())
                    self.assertIsNone(handler._tx_pcm16_resampler)
                    self.assertEqual(self._rebuilt(handler, cls), [],
                                     "a refused keystroke rebuilt its bridge")
                else:
                    handler.feed_tx_audio(SILENCE16, input_rate=16000)
                    handler._engine.connected = False
                    self.assertFalse(handler.start_tx())
                    handler._engine.connected = True
                    self.assertTrue(handler.start_tx())
                    self.assertIsNone(handler._tx_pcm16_resampler,
                                      "the next key-up inherited the last one's "
                                      "filter tail")

    def test_stop_and_close_drop_a_live_bridge_even_mid_session(self):
        for cls in (FlexAudioHandler, SunSDRAudioHandler):
            for closer in ("stop_tx", "close"):
                with self.subTest(cls=cls.__name__, closer=closer):
                    handler = self._handler(cls)
                    if closer == "close":
                        self._prepare_close(handler, cls)
                    else:
                        self._stop_pacer(handler)
                    handler.feed_tx_audio(SILENCE16, input_rate=16000)
                    self.assertIsNotNone(handler._tx_pcm16_resampler)
                    getattr(handler, closer)()
                    self.assertIsNone(handler._tx_pcm16_resampler)

    def test_feed_after_stop_does_not_rebuild_the_bridge(self):
        """SunSDR's uplink gate reads the pacer, so a frame that races the unkey
        must be refused rather than start a fresh session's bridge.  (Flex gates
        on its TX stream instead — see ``test_stop_and_close_drop_a_live_bridge_
        even_mid_session`` for the Flex side of the same rule.)"""
        handler = self._handler(SunSDRAudioHandler)
        self._stop_pacer(handler)
        handler.feed_tx_audio(SILENCE16, input_rate=16000)
        self.assertIsNotNone(handler._tx_pcm16_resampler)
        handler.stop_tx()
        self.assertFalse(handler._engine.tx_active)
        self.assertEqual(self._rebuilt(handler, SunSDRAudioHandler), [],
                         "a stopped session built a bridge")
        self.assertIsNone(handler._tx_pcm16_resampler)

    def test_close_refuses_later_uplink_frames_entirely(self):
        """``close()`` is the permanent end: not one byte may reach the engine
        afterwards, whatever the input rate."""
        for cls in (FlexAudioHandler, SunSDRAudioHandler):
            with self.subTest(cls=cls.__name__):
                handler = self._handler(cls)
                encoder = handler._tx_encoder if cls is FlexAudioHandler else None
                self._prepare_close(handler, cls)
                handler.feed_tx_audio(SILENCE16, input_rate=16000)
                self.assertIsNotNone(handler._tx_pcm16_resampler)
                handler.close()
                self.assertIsNone(handler._tx_pcm16_resampler)
                handler.feed_tx_audio(SILENCE16, input_rate=16000)
                handler.feed_tx_audio(b"\x00\x00" * 960)     # legacy 48 kHz too
                self.assertIsNone(handler._tx_pcm16_resampler)
                # close() itself releases the encoders, so capture "did any
                # audio get encoded after the shutdown" before that happens.
                pushed = encoder.push.call_count \
                    if cls is FlexAudioHandler else None
                fed = handler._engine.feed_tx_audio.call_count \
                    if cls is SunSDRAudioHandler else None
                if cls is SunSDRAudioHandler:
                    self.assertEqual(fed, 0, "audio reached the engine post-close")
                else:
                    self.assertEqual(pushed, 0, "audio was encoded post-close")

    def test_stop_still_drops_the_bridge_when_the_engine_is_not_active(self):
        """The early return on ``tx_active`` must not skip the cleanup — an
        async interlock stop leaves the adapter holding the last session."""
        for cls in (FlexAudioHandler, SunSDRAudioHandler):
            with self.subTest(cls=cls.__name__):
                handler = self._handler(cls)
                handler._engine.tx_active = False
                handler._run = mock.Mock()
                handler._tx_pcm16_resampler = mock.Mock()
                handler.stop_tx()
                handler._run.assert_not_called()
                self.assertIsNone(handler._tx_pcm16_resampler)

    def test_stop_retires_the_session_without_holding_the_lock_across_the_await(self):
        """The pacer release is an await on the event loop. It must not run
        while ``_tx_lock`` is held, or the PTT release path would sit behind a
        frame that is merely converting."""
        for cls in (FlexAudioHandler, SunSDRAudioHandler):
            with self.subTest(cls=cls.__name__):
                handler = self._handler(cls)
                order = []
                state = self._lock_probe(handler)

                def run(coro):
                    order.append("run")
                    if state["depth"]:
                        order.append("RUNNING_UNDER_LOCK")
                    handler._engine.tx_active = False
                    return None

                handler._run = mock.Mock(side_effect=run)
                handler._tx_pcm16_resampler = mock.Mock()
                handler.stop_tx(graceful=True)
                self.assertIn("run", order)
                self.assertNotIn("RUNNING_UNDER_LOCK", order)
                self.assertEqual(state["depth"], 0, "lock left held")
                self.assertIsNone(handler._tx_pcm16_resampler)

    def test_feed_and_lifecycle_cleanup_are_mutually_exclusive(self):
        """Hammer feed from one thread while another retires the session: the
        bridge is never observed half-built, and neither side wedges."""
        for cls in (FlexAudioHandler, SunSDRAudioHandler):
            with self.subTest(cls=cls.__name__):
                handler = self._handler(cls)
                stop = threading.Event()
                errors = []
                conversions = []
                module = importlib.import_module(cls.__module__)

                class Counting(StreamingPcmResampler):
                    def process(self, pcm):
                        out = super().process(pcm)
                        conversions.append(len(out))
                        return out

                def feeder():
                    try:
                        while not stop.is_set():
                            handler.feed_tx_audio(SILENCE16, input_rate=16000)
                    except Exception as exc:                       # noqa: BLE001
                        errors.append(exc)

                with mock.patch.object(module, "StreamingPcmResampler", Counting):
                    thread = threading.Thread(target=feeder, daemon=True)
                    thread.start()
                    try:
                        for _ in range(300):
                            handler._tx_session_end()
                    finally:
                        stop.set()
                        thread.join(timeout=10.0)
                self.assertFalse(thread.is_alive(), "feeder wedged on the lock")
                self.assertEqual(errors, [])
                self.assertTrue(conversions, "no conversion ever ran")


class Pcm16BridgeShapeTests(unittest.TestCase):
    """Real soxr, no mocks — the numbers a field report turns on.

    A streaming SRC hands back nothing (or only part of a frame) until its
    filter is primed and catches up on the next one, so the honest contract is
    **conservation across the whole session**, not a fixed count per frame.  And
    because production never flushes a TX bridge (the keystroke simply stops),
    the outstanding tail is asserted separately: it must be exactly what a flush
    would have released, which is what proves nothing was invented or lost.
    """

    FRAMES = 50                                   # 1 s of voice band
    TONE_HZ = 1000.0

    def test_16_to_24k_conserves_samples_and_keeps_the_pitch(self):
        self._check(24000)

    def test_16_to_48k_conserves_samples_and_keeps_the_pitch(self):
        self._check(48000)

    def _check(self, out_rate):
        src = _tone_pcm16(self.TONE_HZ)
        bridge = StreamingPcmResampler(16000, out_rate)
        chunks = [bridge.process(src) for _ in range(self.FRAMES)]
        streamed = sum(len(c) // 2 for c in chunks)
        ideal = self.FRAMES * PCM16_FRAME_SAMPLES * out_rate // 16000
        steady = PCM16_FRAME_SAMPLES * out_rate // 16000      # samples per 20 ms
        # Onset shape, as Task 5 measured it: the first frame is short or empty
        # and the second pays it back, so a keystroke does not lose audio — it
        # arrives ~one group delay later.
        self.assertLessEqual(len(chunks[0]) // 2, steady,
                             f"{out_rate} Hz: priming frame over-produced")
        self.assertGreater(len(chunks[1]) // 2, steady,
                           f"{out_rate} Hz: second frame did not catch up")
        flushed = len(bridge.flush()) // 2
        # process() + flush() == the exact ideal count (±2 samples).
        self.assertLessEqual(abs(streamed + flushed - ideal), 2,
                             f"{out_rate} Hz: process+flush = {streamed + flushed}, "
                             f"ideal {ideal}")
        # What is still outstanding when a keystroke ends without a flush is
        # exactly the tail the flush releases — no sample evaporates, and none
        # is fabricated to round a frame off.
        self.assertEqual(ideal - streamed, flushed,
                         f"{out_rate} Hz: {ideal - streamed} samples outstanding, "
                         f"flush gave {flushed}")
        # Steady state is rate-exact **cumulatively**: soxr's granular mode does
        # not promise a fixed count per call, so the honest invariant is that
        # the total tracks N x ratio and never drifts away from it.  The gap at
        # frame N is exactly the group delay — the samples still inside the
        # filter, which a flush would release — so bounding it bounds the drift
        # over an hour-long call.
        settled = chunks[2:]
        cumulative = sum(len(c) // 2 for c in settled)
        self.assertLessEqual(
            abs(len(settled) * steady - cumulative), flushed,
            f"{out_rate} Hz: {len(settled)} settled frames produced "
            f"{cumulative} samples, expected {len(settled) * steady} "
            f"(flush tail is {flushed})")
        pitch = _dominant_hz(b"".join(chunks), out_rate)
        self.assertAlmostEqual(pitch, self.TONE_HZ, delta=40.0,
                               msg=f"{out_rate} Hz: pitch moved to {pitch:.0f} Hz")
        # Nothing from above the input Nyquist folds into the voice band.
        near = StreamingPcmResampler(16000, out_rate)
        folded = b"".join(near.process(_tone_pcm16(7500.0))
                          for _ in range(self.FRAMES))
        self.assertAlmostEqual(_dominant_hz(folded, out_rate), 7500.0, delta=60.0,
                               msg=f"{out_rate} Hz: near-Nyquist tone folded")


if __name__ == "__main__":
    unittest.main()
