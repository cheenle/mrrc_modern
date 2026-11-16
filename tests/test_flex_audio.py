"""Audio path tests for the FlexRadio backend (P3, design §5).

Two failure modes these tests exist to catch, both silent in the field:

* a resampler that drifts (24 kHz → 48 kHz must stay an exact 2:1 forever),
* a decimator without anti-aliasing (20 kHz at 48 kHz folds straight into the
  voice band once downsampled to 24 kHz).

The Opus/VITA hop is checked by packet counts and the header's PCC, not by
decoding: the codec's own round trip lives in the Opus tests.
"""

import asyncio
import unittest

import numpy as np

from backends.flex import vita
from backends.flex.audio import (
    DEVICE_INPUT_BLOCK, RX_CHUNK_BYTES, HalfBandResampler, FlexAudioHandler,
)
from backends.flex.engine import RX_SAMPLE_RATE, SmartSDREngine

from tests.flex_fake_radio import FakeRadio


def _tone(freq: float, rate: int, samples: int, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(samples) / float(rate)
    return (amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class ResamplerTests(unittest.TestCase):
    def test_up2_doubles_the_sample_count_exactly(self):
        resampler = HalfBandResampler(up=2, down=1)
        total_in = total_out = 0
        for _ in range(1000):                  # 1000 x 480 = 20 s of 24 kHz
            block = np.zeros(480, dtype=np.float32)
            total_in += block.size
            total_out += resampler.process(block).size
        self.assertEqual(total_in, 480_000)
        self.assertEqual(total_out, 960_000)

    def test_down2_halves_the_sample_count_exactly(self):
        resampler = HalfBandResampler(up=1, down=2)
        total_in = total_out = 0
        for _ in range(1000):
            block = np.zeros(960, dtype=np.float32)
            total_in += block.size
            total_out += resampler.process(block).size
        self.assertEqual(total_in, 960_000)
        self.assertEqual(total_out, 480_000)

    def test_up2_preserves_a_passband_tone(self):
        resampler = HalfBandResampler(up=2, down=1)
        tone = _tone(1000.0, RX_SAMPLE_RATE, 24_000, amplitude=0.5)
        out = resampler.process(tone)
        self.assertAlmostEqual(float(np.abs(out).max()), 0.5, delta=0.02)

    def test_down2_rejects_a_20khz_tone(self):
        """An un-anti-aliased 20 kHz tone folds to 4 kHz — this is the test."""
        resampler = HalfBandResampler(up=1, down=2)
        tone = _tone(20_000.0, 48_000, 48_000, amplitude=0.5)
        out = resampler.process(tone)
        peak = float(np.abs(out[200:]).max())
        attenuation_db = 20 * np.log10(max(peak, 1e-12) / 0.5)
        self.assertLess(attenuation_db, -40.0)

    def test_down2_passes_the_voice_band(self):
        resampler = HalfBandResampler(up=1, down=2)
        tone = _tone(3000.0, 48_000, 48_000, amplitude=0.5)
        out = resampler.process(tone)
        peak = float(np.abs(out[200:]).max())
        self.assertGreater(peak, 0.4)


class RxPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = SmartSDREngine("127.0.0.1")
        self.handler = FlexAudioHandler(self.engine)
        self.handler.start_rx()

    async def asyncTearDown(self):
        self.handler.close()

    def test_no_audio_yields_no_chunk(self):
        self.assertIsNone(self.handler.read_rx_chunk())

    def test_blocks_are_960_samples_at_48k(self):
        self.engine.rx_audio.append(np.zeros(480, dtype=np.float32))
        self.engine.rx_samples = 480
        chunk = self.handler.read_rx_chunk()
        assert chunk is not None
        self.assertEqual(len(chunk), RX_CHUNK_BYTES)
        self.assertEqual(len(np.frombuffer(chunk, dtype=np.int16)), 960)

    def test_sixty_seconds_does_not_drift(self):
        """24 kHz in, 48 kHz out: cumulative sample count is exactly 2x."""
        blocks = 60 * RX_SAMPLE_RATE // DEVICE_INPUT_BLOCK
        produced = 0
        for _ in range(blocks):
            self.engine.rx_audio.append(np.zeros(DEVICE_INPUT_BLOCK, dtype=np.float32))
            self.engine.rx_samples = DEVICE_INPUT_BLOCK
            chunk = self.handler.read_rx_chunk()
            if chunk is not None:
                produced += len(chunk) // 2
        expected = (2 * blocks * DEVICE_INPUT_BLOCK) // 960 * 960
        self.assertEqual(produced, expected)

    def test_a_tone_survives_the_upsampling(self):
        tone = _tone(1000.0, RX_SAMPLE_RATE, 4800, amplitude=0.5)
        for offset in range(0, tone.size, DEVICE_INPUT_BLOCK):
            block = tone[offset:offset + DEVICE_INPUT_BLOCK]
            if block.size < DEVICE_INPUT_BLOCK:
                break
            self.engine.rx_audio.append(block)
            self.engine.rx_samples = block.size
            self.handler.read_rx_chunk()
        while True:
            chunk = self.handler.read_rx_chunk()
            if chunk is None:
                break
            pcm = np.frombuffer(chunk, dtype=np.int16)
            self.assertGreater(int(np.abs(pcm.astype(np.int32)).max()), 8000)

    def test_restart_rx_drops_buffered_audio(self):
        self.engine.rx_audio.append(np.zeros(480, dtype=np.float32))
        self.engine.rx_samples = 480
        self.handler.restart_rx()
        self.assertEqual(len(self.engine.rx_audio), 0)
        self.assertIsNone(self.handler.read_rx_chunk())

    def test_one_read_drains_everything_that_is_waiting(self):
        """The consumer must not be limited to 20 ms per tick.

        Field 2026-11-16 (box `ba1rb`): pulling exactly one block per loop tick gave
        this path a duty cycle of exactly 100 % — the radio pushes one second of
        audio per second and the engine queue is capped at one second — so a
        standing ~0.5 s backlog never drained: the FT8 RX tap re-anchored its clock
        every ~3 s and the browser carried half a second of extra latency.
        """
        for _ in range(25):                        # 25 x 20 ms = 500 ms queued
            self.engine.rx_audio.append(np.zeros(DEVICE_INPUT_BLOCK, dtype=np.float32))
        self.engine.rx_samples = 25 * DEVICE_INPUT_BLOCK
        chunk = self.handler.read_rx_chunk()
        assert chunk is not None
        self.assertGreaterEqual(len(chunk), 960 * 2 * 25)      # all of it
        self.assertEqual(len(chunk) % RX_CHUNK_BYTES, 0)       # whole 20 ms blocks
        self.assertEqual(self.engine.rx_samples, 0)
        # Nothing is left over, so the next call reports "not ready".
        self.assertIsNone(self.handler.read_rx_chunk())

    def test_a_partial_block_is_kept_for_the_next_read(self):
        self.engine.rx_audio.append(np.zeros(DEVICE_INPUT_BLOCK, dtype=np.float32))
        self.engine.rx_samples = DEVICE_INPUT_BLOCK
        self.assertIsNotNone(self.handler.read_rx_chunk())
        # 480 samples in, 960 out, 20 ms block = 960 -> the 2nd input block leaves
        # nothing pending; a third one must therefore also produce a block.
        self.engine.rx_audio.append(np.zeros(DEVICE_INPUT_BLOCK, dtype=np.float32))
        self.engine.rx_samples = DEVICE_INPUT_BLOCK
        chunk = self.handler.read_rx_chunk()
        assert chunk is not None
        self.assertEqual(len(chunk) % RX_CHUNK_BYTES, 0)


class TxPathTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = SmartSDREngine("127.0.0.1")
        self.engine.tx_stream_ready = True
        self.engine.tx_stream_id = 0x0B000002
        self.engine.connected = True
        self.handler = FlexAudioHandler(self.engine)
        self.handler._loop = asyncio.get_running_loop()

    async def asyncTearDown(self):
        self.handler.close()

    def _feed_mono(self, rate=48000, ms=20, freq=1000.0):
        samples = rate * ms // 1000
        tone = _tone(freq, rate, samples, amplitude=0.5)
        pcm = np.clip(np.rint(tone * 32768.0), -32768, 32767).astype("<i2")
        self.handler.feed_tx_audio(pcm.tobytes())

    def test_twenty_ms_produces_two_opus_packets(self):
        self._feed_mono(ms=20)
        self.assertEqual(len(self.engine._tx_queue), 2)

    def test_packets_are_vita_opus_with_the_tx_stream_id(self):
        self._feed_mono(ms=10)
        self.assertEqual(len(self.engine._tx_queue), 1)
        header = vita.parse_header(self.engine._tx_queue[0])
        self.assertEqual(header.pcc, vita.PCC_OPUS)
        self.assertEqual(header.stream_id, 0x0B000002)
        self.assertGreater(len(header.payload), 0)

    def test_queue_overflow_drops_the_oldest_and_counts_it(self):
        for _ in range(30):
            self._feed_mono(ms=10)
        self.assertEqual(self.engine.tx_drops, 30 - 20)
        self.assertEqual(len(self.engine._tx_queue), 20)

    def test_tx_refused_without_a_stream(self):
        self.engine.tx_stream_ready = False
        self.assertFalse(self.handler.start_tx())

    def test_tx_stats_expose_the_queue_counters(self):
        self._feed_mono(ms=10)
        stats = self.handler.tx_stats()
        self.assertEqual(stats["written"], 1)
        self.assertIn("queue_drops", stats)
        self.assertGreater(stats["peak"], 0)


class PacerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.radio = await FakeRadio.create()
        self.engine = SmartSDREngine("127.0.0.1", self.radio.port)
        self.assertTrue(await self.engine.start())

    async def asyncTearDown(self):
        await self.engine.stop()
        self.radio.close()

    async def test_pacer_sends_queued_packets_to_the_registered_port(self):
        await self.engine.start_tx()
        for _ in range(5):
            self.engine.feed_opus(b"\x00" * 8)
        await asyncio.sleep(0.15)
        self.assertGreaterEqual(self.engine.tx_packets_sent, 3)
        await self.engine.stop_tx()
        self.assertFalse(self.engine.tx_active)

    async def test_key_is_refused_by_the_radio_then_unkeyed(self):
        """A radio that never reaches TRANSMITTING must not leave us keyed."""
        ok = await asyncio.wait_for(self.engine.key(True), timeout=3.0)
        self.assertFalse(ok)
        self.assertIn("xmit 1", [c for c in self.radio.commands])
        self.assertIn("xmit 0", [c for c in self.radio.commands])


if __name__ == "__main__":
    unittest.main()
