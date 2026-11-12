"""Network audio adapter: 16 kHz PCM from the radio → 48 kHz to the server.

The seam is worth testing on its own because everything downstream (Opus, the
browser, FT8, the recorder) assumes 48 kHz mono, while the radio sends 16 kHz.
A wrong rate here is not a crash — it is audio at 1/3 speed, which nobody
notices until a human listens.  So the assertions are about **frequency**.
"""
import unittest

import numpy as np

from backends.icom_net.audio import IcomNetAudioHandler, RX_CHUNK_BYTES


class _StubClient:
    """The bit of IcomNetworkClient the adapter touches."""

    def __init__(self, rx_sample=16000):
        self.rx_sample = rx_sample
        self.on_audio = None
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


def _handler(**kw):
    client = _StubClient(**kw)
    handler = IcomNetAudioHandler(client, tx_handler=None)
    handler.start_rx()
    return client, handler


def _tone_16k(freq_hz=700.0, n=320):
    t = np.arange(n) / 16000.0
    return (np.sin(2 * np.pi * freq_hz * t) * 8000).astype("<i2").tobytes()


class ResampleTests(unittest.TestCase):
    def test_it_takes_the_client_callback(self):
        client, handler = _handler()
        # Bound methods are re-created per access; compare ownership instead.
        self.assertIs(client.on_audio.__self__, handler)
        self.assertEqual(client.on_audio.__func__, handler._on_pcm.__func__)

    def test_16k_packets_become_20ms_48k_blocks(self):
        """A 320-sample packet in, 960-sample blocks out (soxr holds a few
        samples of latency, so a single packet does not fill one block)."""
        client, handler = _handler()
        for _ in range(4):
            client.on_audio(_tone_16k(), 16000)
        chunk = handler.read_rx_chunk()
        self.assertIsNotNone(chunk)
        self.assertEqual(len(chunk), RX_CHUNK_BYTES)     # 960 samples @ 48 kHz

    def test_the_tone_keeps_its_frequency(self):
        client, handler = _handler()
        for _ in range(60):
            client.on_audio(_tone_16k(), 16000)
        pcm = b"".join(
            filter(None, (handler.read_rx_chunk() for _ in range(60))))
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        self.assertGreater(samples.size, 4000)
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
        peak_hz = np.argmax(spectrum) * 48000 / samples.size
        self.assertAlmostEqual(peak_hz, 700, delta=20)

    def test_nothing_is_produced_before_start_rx(self):
        client = _StubClient()
        handler = IcomNetAudioHandler(client, tx_handler=None)
        client.on_audio(_tone_16k(), 16000)
        self.assertIsNone(handler.read_rx_chunk())

    def test_the_queue_is_bounded_and_trimmed_on_read(self):
        client, handler = _handler()
        for _ in range(120):
            client.on_audio(_tone_16k(), 16000)
        self.assertGreater(handler.chunks_dropped, 0)
        handler.read_rx_chunk()
        self.assertLessEqual(len(handler._rx_audio), 5)

    def test_restart_rx_drops_the_stale_buffer(self):
        client, handler = _handler()
        for _ in range(5):
            client.on_audio(_tone_16k(), 16000)
        handler.restart_rx()
        self.assertIsNone(handler.read_rx_chunk())

    def test_a_matching_rate_skips_the_resampler(self):
        client = _StubClient(rx_sample=48000)
        handler = IcomNetAudioHandler(client, tx_handler=None)
        self.assertIsNone(handler._resampler)


class SurfaceTests(unittest.TestCase):
    def test_silence_watchdog(self):
        _client, handler = _handler()
        handler.note_rx_chunk(b"\x00\x00" * 960)
        self.assertGreaterEqual(handler.rx_silent_seconds(), 0.0)
        handler.note_rx_chunk((np.ones(960) * 500).astype("<i2").tobytes())
        self.assertEqual(handler.rx_silent_seconds(), 0.0)

    def test_opus_or_pcm_tagging(self):
        _client, handler = _handler()
        pcm = (np.sin(np.linspace(0, 200 * np.pi, 960)) * 8000).astype("<i2").tobytes()
        frames = handler.encode_rx_audio(pcm)
        self.assertTrue(frames)
        self.assertTrue({f[0] for f in frames} <= {0x00, 0x01})

    def test_tx_without_a_sound_card_refuses(self):
        _client, handler = _handler()
        self.assertFalse(handler.start_tx())
        self.assertEqual(handler.tx_stats()["written"], 0)

    def test_device_rates_report_48k_rx(self):
        _client, handler = _handler()
        self.assertEqual(handler.device_rates["rx"], 48000)

    def test_close_detaches_the_callback(self):
        client, handler = _handler()
        handler.close()
        self.assertIsNone(client.on_audio)


if __name__ == "__main__":
    unittest.main()
