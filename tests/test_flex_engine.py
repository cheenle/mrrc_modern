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

    def test_bare_hex_stream_ids_are_not_read_as_decimal(self):
        """A FLEX-6600 answers ``stream create`` with ``4000008`` / ``84000000``:
        hex digits, leading zero stripped, no prefix (measured in the field,
        2026-11-16).  Decimal gave 4000008 instead of 0x04000008 — RX audio
        happened to work because the radio pushes it anyway, but TX addressed a
        stream that does not exist."""
        self.assertEqual(_first_int("4000008"), 0x04000008)
        self.assertEqual(_first_int("84000000"), 0x84000000)
        self.assertEqual(_first_int("0x40000002,0x42000002"), 0x40000002)

    def test_mhz_formats_six_decimals(self):
        self.assertEqual(_mhz(14_074_000), "14.074000")
        self.assertEqual(_mhz(100_000), "0.100000")


class SpectrumIngestTests(unittest.TestCase):
    def setUp(self):
        self.engine = SmartSDREngine("127.0.0.1")

    def test_ui_bytes_come_from_the_pan_aperture_and_the_ui_window(self):
        """Bin → byte goes through dBm, so the *UI window* sets the brightness.

        The old identity ``255 - pixel`` only held while the pan aperture and the
        UI window were both [-120, 0] dB — and that window rendered a real HF
        noise floor as UI 1-3/255, i.e. a black waterfall (field 2026-11-16).
        """
        bins = [128] * 850
        bins[30], bins[31], bins[32] = 0, 128, 255     # beyond the neutralised head
        self.engine._handle_packet(_packet(vita.PCC_FFT, _fft_packet(bins)))
        out = self.engine.spectrum_bins()
        assert out is not None
        self.assertEqual(out[30], 255)         # top of the aperture saturates
        self.assertEqual(out[32], 0)           # bottom of the aperture is black
        self.assertTrue(60 <= out[31] <= 200)  # the middle is mid-grey, not crushed
        self.assertEqual(self.engine.spectrum_frames, 1)

    def test_the_radios_edge_artifacts_are_flattened(self):
        """Bins at both ends of every frame are a fixed artefact, not RF.

        Measured 2026-11-16: bins 5-10 and 18-23 carried identical values at
        7.074 / 9.850 / 14.270 MHz, i.e. two "ghost signals" parked at the left
        edge of every waterfall.  They are replaced by the interior median.
        """
        bins = [200] * 850                  # pixel 200 = a quiet floor
        bins[5:11] = [105, 148, 192, 237, 255, 91]     # fixed artifacts
        bins[18:24] = [253, 255, 236, 208, 180, 151]
        self.engine._handle_packet(_packet(vita.PCC_FFT, _fft_packet(bins)))
        out = self.engine.spectrum_bins()
        assert out is not None
        # The ghosts are gone: no bin in the head is brighter than the floor.
        self.assertEqual(set(out[:26]), {out[400]})    # flattened to the floor
        self.assertEqual(set(out[-12:]), {out[400]})

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

    def test_another_clients_panadapter_is_ignored(self):
        """The radio streams the FFT of every pan we subscribed to; a second
        client's pan must not be assembled into our bins (field 2026-11-16:
        238 of our pan's segments and 198 of a foreign pan's in 8 s)."""
        self.engine.pan_id = 0x40000002
        self.engine._handle_packet(_packet(vita.PCC_FFT, _fft_packet([7] * 850),
                                           stream_id=0x40000000))
        self.assertEqual(self.engine.spectrum_frames, 0)
        self.assertEqual(self.engine.foreign_packets, 1)

        self.engine._handle_packet(_packet(vita.PCC_FFT, _fft_packet([7] * 850),
                                           stream_id=0x40000002))
        self.assertEqual(self.engine.spectrum_frames, 1)
        self.assertEqual(self.engine.foreign_packets, 1)


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

    def test_the_s_meter_belongs_to_our_own_slice(self):
        """Each slice has its own SLC/LEVEL meter; the first one is not ours.

        Field 2026-11-16 (box `ba1rb`, four slices): the UI read slice 0 — another
        operator's slice — so switching *our* slice's antenna could never move it.
        """
        self.engine._handle_status(protocol.parse_status(
            "S0|meter 7.src=SLC#7.num=0#7.nam=LEVEL#7.unit=dBm"
            "#7.low=-150.0#7.hi=20.0#7.fps=10"
            "#8.src=SLC#8.num=2#8.nam=LEVEL#8.unit=dBm"
            "#8.low=-150.0#8.hi=20.0#8.fps=10"))
        self.engine._handle_packet(_packet(
            vita.PCC_METERS, struct.pack(">HhHh", 7, -9344, 8, -4000)))
        self.engine.own_slice = 2
        assert self.engine.s_meter_dbm() is not None
        # Two different raw values -> two different levels; the point is *which*
        # meter is read, not the calibration.
        ours = self.engine.s_meter_dbm()
        self.engine.own_slice = 0
        theirs = self.engine.s_meter_dbm()
        self.assertNotEqual(ours, theirs)
        self.assertAlmostEqual(theirs, -73.0, places=2)                       # meter 7
        self.engine.own_slice = 2
        self.assertAlmostEqual(self.engine.s_meter_dbm(), ours, places=3)

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

    def test_slice_status_carries_the_antenna_selection_and_the_radios_list(self):
        """A FLEX-6600 reports `rxant` + `ant_list` per slice (field 2026-11-16).

        The old backend sent `ant=ANT1`, a key this firmware does not have, so a
        slice could sit on a connector with no antenna on it: connected, tuned
        and completely silent.
        """
        self.engine.own_slice = 2
        self.engine._handle_status(protocol.parse_status(
            "S0|slice 2 rxant=ANT2 txant=ANT2 "
            "ant_list=ANT1,ANT2,RX_A,RX_B,XVTA,XVTB "
            "tx_ant_list=ANT1,ANT2,XVTA,XVTB mode=USB"))
        state = self.engine.slices[2]
        self.assertEqual(state["rxant"], "ANT2")
        self.assertEqual(state["txant"], "ANT2")
        self.assertEqual(state["antenna_list"],
                         ["ANT1", "ANT2", "RX_A", "RX_B", "XVTA", "XVTB"])
        self.assertIn(("antenna_name", "ANT2"), self.broadcasts)
        self.assertIn(("antenna_list",
                       ["ANT1", "ANT2", "RX_A", "RX_B", "XVTA", "XVTB"]),
                      self.broadcasts)

    def test_the_antenna_snapshot_is_grouped_by_panadapter(self):
        """The panel needs the pan/slice split, because the radio scopes RX to the
        panadapter and TX to the slice (SmartSDR User Guide §27)."""
        self.engine.own_slice = 2
        for body in ("S0|slice 0 RF_frequency=14.074000 mode=USB pan=0x40000001 "
                     "active=0 client_handle=0x650F1147 rxant=RX_B txant=ANT1 "
                     "ant_list=ANT1,ANT2,RX_A,RX_B tx_ant_list=ANT1,ANT2",
                     "S0|slice 2 RF_frequency=7.074000 mode=LSB pan=0x40000002 "
                     "active=1 client_handle=0x5C097BF7 rxant=ANT2 txant=ANT1 "
                     "ant_list=ANT1,ANT2,RX_A,RX_B tx_ant_list=ANT1,ANT2"):
            self.engine._handle_status(protocol.parse_status(body))
        snap = self.engine.antennas_snapshot()
        self.assertEqual(snap["rx"], ["ANT1", "ANT2", "RX_A", "RX_B"])
        self.assertEqual(snap["tx"], ["ANT1", "ANT2"])
        self.assertEqual([p["id"] for p in snap["pans"]], ["0x40000001", "0x40000002"])
        ours = [p for p in snap["pans"] if p["ours"]]
        self.assertEqual(len(ours), 1)
        self.assertEqual(ours[0]["id"], "0x40000002")
        self.assertEqual(ours[0]["rxant"], "ANT2")
        self.assertEqual([s["index"] for s in ours[0]["slices"]], [2])
        self.assertTrue(ours[0]["slices"][0]["ours"])
        self.assertTrue(ours[0]["slices"][0]["active"])
        self.assertEqual(ours[0]["slices"][0]["txant"], "ANT1")
        theirs = [p for p in snap["pans"] if not p["ours"]][0]
        self.assertEqual(theirs["id"], "0x40000001")
        self.assertFalse(theirs["slices"][0]["ours"])

    def test_the_snapshot_is_broadcast_only_when_it_changes(self):
        self.engine.own_slice = 2
        line = ("S0|slice 2 RF_frequency=7.074000 mode=LSB pan=0x40000002 active=1 "
                "rxant=ANT1 ant_list=ANT1,ANT2")
        self.engine._handle_status(protocol.parse_status(line))
        first = len([b for b in self.broadcasts if b[0] == "antennas"])
        self.engine._handle_status(protocol.parse_status(line))     # 同样的状态再来一次
        second = len([b for b in self.broadcasts if b[0] == "antennas"])
        self.assertGreaterEqual(first, 1)
        self.assertEqual(first, second)

    def test_another_clients_slice_does_not_broadcast_its_antenna(self):
        self.engine.own_slice = 2
        self.engine._handle_status(protocol.parse_status(
            "S0|slice 1 rxant=ANT1 ant_list=ANT1,ANT2 mode=USB"))
        self.assertEqual(self.engine.slices[1]["rxant"], "ANT1")
        self.assertEqual([f for f, _ in self.broadcasts
                          if f in ("antenna_name", "antenna_list")], [])

    def test_pan_bandwidth_and_center_are_tracked(self):
        self.engine.pan_id = 0x40000000      # 认定是自己的 pan 之后才采纳
        self.engine._handle_status(protocol.parse_status(
            "S0|display pan 0x40000000 bandwidth=0.100000 center=14.074000"))
        self.assertEqual(self.engine.bandwidth_hz, 100_000)
        self.assertEqual(self.engine.pan_center_hz, 14_074_000)

    def test_another_clients_pan_never_touches_our_axis_or_aperture(self):
        """`sub pan all` 会把同机另一台 GUI 的 pan 状态也推过来。

        2026-11-16 现场：`0x40000001` 在 3 ms 内被写成 -140/-50（我们）与
        -135/-40（SmartSDR），因为 `_update_pan` 不分对象地改全局状态 ——
        横轴、带宽、像素→dBm 的孔径全部跟着**别人的** pan 走，于是声音正常
        而频谱里找不到信号。守卫：别人的 pan 一律不得改这些字段。
        """
        self.engine.pan_id = 0x40000001
        self.engine.pan_center_hz = 14_100_000
        self.engine.bandwidth_hz = 100_000
        self.engine.pan_aperture = {"min_dbm": -140.0, "max_dbm": -50.0,
                                    "ypixels": 256.0}
        self.engine._handle_status(protocol.parse_status(
            "S0|display pan 0x40000000 bandwidth=0.500000 center=7.074000 "
            "min_dbm=-135.0 max_dbm=-40.0 ypixels=100 xpixels=100"))
        self.assertEqual(self.engine.pan_center_hz, 14_100_000)
        self.assertEqual(self.engine.bandwidth_hz, 100_000)
        self.assertEqual(self.engine.pan_aperture["min_dbm"], -140.0)
        self.assertEqual(self.engine.pan_aperture["ypixels"], 256.0)

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
