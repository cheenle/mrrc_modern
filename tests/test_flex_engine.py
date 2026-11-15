"""Engine tests for the FlexRadio backend (P3, design §4.4).

The engine is driven with crafted VITA packets and status lines — no radio, no
sockets except the discovery case, which uses a real loopback broadcaster.
"""

import asyncio
import socket
import struct
import unittest

import numpy as np

from backends.flex import protocol, vita
from backends.flex.engine import (
    RX_QUEUE_SECONDS, SmartSDREngine, _first_int, _mhz, discover_radios,
)
from backends.flex.protocol import Status


def _fft_packet(bins, *, start=0, total=None, frame_index=1, head_len=12):
    total = len(bins) if total is None else total
    head = struct.pack(">HHHHH", start, len(bins), 1, total, frame_index)
    head += b"\x00" * (head_len - len(head))
    payload = head + struct.pack(f">{len(bins)}H", *bins)
    return payload


def _packet(pcc, payload, stream_id=0x40000000):
    word0 = ((3 << 28) | (1 << 27) | (3 << 22) | (1 << 20)
             | ((28 + len(payload) + 3) // 4))
    head = struct.pack(">IIIIIII", word0, stream_id, vita.OPUS_OUI,
                       (vita.ICC << 16) | pcc, 0, 0, 0)
    return head + payload


def _audio_packet(frames: np.ndarray, stream_id=0x04000000):
    payload = frames.astype(">f4").tobytes()
    return _packet(vita.PCC_RX_AUDIO, payload, stream_id=stream_id)


class HelpersTests(unittest.TestCase):
    def test_first_int_finds_hex_ids_and_decimals(self):
        self.assertEqual(_first_int("pan=0x40000000"), 0x40000000)
        self.assertEqual(_first_int("2"), 2)
        self.assertIsNone(_first_int("no numbers here"))

    def test_mhz_formats_six_decimals(self):
        self.assertEqual(_mhz(14_074_000), "14.074000")
        self.assertEqual(_mhz(100_000), "0.100000")


class SpectrumIngestTests(unittest.TestCase):
    def setUp(self):
        self.engine = SmartSDREngine("127.0.0.1")

    def test_ui_bytes_are_255_minus_pixel(self):
        bins = [0, 1, 128, 255] + [0] * 846
        self.engine._handle_packet(_packet(vita.PCC_FFT, _fft_packet(bins)))
        out = self.engine.spectrum_bins()
        assert out is not None
        self.assertEqual(out[:4], [255, 254, 127, 0])
        self.assertEqual(self.engine.spectrum_frames, 1)

    def test_a_short_frame_is_resampled_to_850(self):
        bins = list(range(400))
        self.engine._handle_packet(_packet(vita.PCC_FFT, _fft_packet(bins)))
        out = self.engine.spectrum_bins()
        assert out is not None
        self.assertEqual(len(out), 850)

    def test_no_spectrum_before_the_first_frame(self):
        self.assertIsNone(self.engine.spectrum_bins())

    def test_garbage_packets_are_counted_not_raised(self):
        self.engine._handle_packet(b"\x30")
        self.engine._handle_packet(_packet(vita.PCC_FFT, b"\x00\x01"))
        self.assertGreaterEqual(self.engine.spectrum_errors, 1)


class AudioIngestTests(unittest.TestCase):
    def setUp(self):
        self.engine = SmartSDREngine("127.0.0.1")

    def test_stereo_is_downmixed_to_mono(self):
        frames = np.asarray([[0.5, -0.5], [0.25, 0.25]], dtype=np.float32)
        self.engine._handle_packet(_audio_packet(frames))
        self.assertEqual(len(self.engine.rx_audio), 1)
        samples = self.engine.rx_audio[0]
        self.assertEqual(samples.size, 2)
        self.assertAlmostEqual(float(samples[0]), 0.0, places=6)
        self.assertAlmostEqual(float(samples[1]), 0.25, places=6)

    def test_ring_is_bounded_and_drops_the_oldest(self):
        chunk = np.zeros((24000, 2), dtype=np.float32)     # 1 s of 24 kHz
        for _ in range(3):
            self.engine._handle_packet(_audio_packet(chunk))
        self.assertLessEqual(self.engine.rx_samples,
                             int(24000 * RX_QUEUE_SECONDS) + 1)
        self.assertGreaterEqual(self.engine.rx_drops, 1)


class MeterTests(unittest.TestCase):
    def setUp(self):
        self.engine = SmartSDREngine("127.0.0.1")
        self.engine._handle_status(protocol.parse_status(
            "S0|meter 7.src=SLC#7.num=0#7.nam=LEVEL#7.unit=dBm"
            "#7.low=-150.0#7.hi=20.0#7.fps=10"
            "#15.src=TX-#15.num=1#15.nam=FWDPWR#15.unit=dBm"
            "#15.low=0.0#15.hi=53.0#15.fps=20"))

    def test_definitions_are_resolved_by_name(self):
        self.assertEqual(set(self.engine.meter_defs), {7, 15})

    def test_s_meter_and_power_are_scaled(self):
        self.engine._handle_packet(_packet(
            vita.PCC_METERS, struct.pack(">HhHh", 7, -9344, 15, 5120)))
        s_meter = self.engine.s_meter_dbm()
        assert s_meter is not None
        self.assertAlmostEqual(s_meter, -73.0, places=3)          # S9
        watts = self.engine.forward_power_watts()
        assert watts is not None
        self.assertAlmostEqual(watts, 10.0, delta=0.1)            # +40 dBm = 10 W

    def test_missing_meters_read_as_none(self):
        self.assertIsNone(self.engine.s_meter_dbm())
        self.assertIsNone(self.engine.meter_by_name("TX-", "SWR"))


class SliceStatusTests(unittest.TestCase):
    def setUp(self):
        self.engine = SmartSDREngine("127.0.0.1")
        self.broadcasts = []
        self.engine._on_broadcast = lambda field, value: self.broadcasts.append(
            (field, value))

    def test_slice_status_updates_frequency_and_mode(self):
        self.engine._handle_status(protocol.parse_status(
            "S0|slice 0 RF_frequency=14.074000 mode=USB filter_lo=50 "
            "filter_hi=3000 audio_level=35"))
        state = self.engine.slices[0]
        self.assertEqual(state["freq_hz"], 14_074_000)
        self.assertEqual(state["mode"], "USB")
        self.assertEqual(state["filter_lo"], 50)
        self.assertEqual(state["audio_level"], 35)

    def test_pan_bandwidth_and_center_are_tracked(self):
        self.engine._handle_status(protocol.parse_status(
            "S0|display pan 0x40000000 bandwidth=0.100000 center=14.074000"))
        self.assertEqual(self.engine.bandwidth_hz, 100_000)
        self.assertEqual(self.engine.pan_center_hz, 14_074_000)

    def test_tx_timeout_is_read_for_the_warning(self):
        self.engine._handle_status(protocol.parse_status(
            "S0|transmit timeout=30"))
        self.assertEqual(self.engine.tx_timeout_s, 30.0)

    def test_atu_status_marks_the_tuner_present(self):
        self.engine._handle_status(protocol.parse_status(
            "S0|atu 0 state=NOT_READY"))
        self.assertTrue(self.engine.has_atu)


class KeyingTests(unittest.IsolatedAsyncioTestCase):
    async def test_key_reports_false_without_a_session(self):
        engine = SmartSDREngine("127.0.0.1")
        self.assertFalse(await engine.key(True))
        self.assertFalse(await engine.key(False))

    async def test_stats_shape(self):
        engine = SmartSDREngine("127.0.0.1")
        stats = engine.stats()
        for key in ("model", "connected", "pan_id", "spectrum_frames",
                    "tx_drops", "has_atu"):
            self.assertIn(key, stats)


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_broadcast_is_discovered(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()

        text = (b"model=FLEX-6400 serial=1234-5678 version=4.1.5 "
                b"name=Shack_FLEX callsign=BG7XXX ip=127.0.0.1 port=4992")
        packet = _packet(0xFFFF, text + b"\x00" * 4,
                         stream_id=vita.DISCOVERY_STREAM_ID)

        task = asyncio.create_task(discover_radios(1.0, port=port))
        await asyncio.sleep(0.05)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(packet, ("127.0.0.1", port))
        sender.close()
        radios = await task

        self.assertEqual(len(radios), 1)
        self.assertEqual(radios[0]["model"], "FLEX-6400")
        self.assertEqual(radios[0]["name"], "Shack FLEX")

    async def test_a_shared_port_is_not_fatal(self):
        """SO_REUSEADDR lets a second box-side instance listen too; discovery
        returning nothing is fine — the caller falls back to MRRC_FLEX_HOST."""
        holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        try:
            self.assertEqual(await discover_radios(0.1, port=port), [])
        finally:
            holder.close()


if __name__ == "__main__":
    unittest.main()
