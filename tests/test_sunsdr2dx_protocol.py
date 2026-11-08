"""SunSDR2 DX native backend — Task 1: pure UDP protocol layer.

Hardware-independent coverage, byte-for-byte against the reference
implementation (/Users/cheenle/HAM/sunsdr) and PROTOCOL.md:

- control packet framing (14-byte LE header + payload + trailing word);
- the command builders the backend will need (PTT/DRIVE/frequencies/
  HW_INIT sample-rate selector/STREAM_CTRL), including the two field
  placements that were real bugs in the field (drive byte in the
  TRAILING word, not the payload; STREAM_CTRL word[1] must stay 0);
- the captured boot sequence as pure data (fixed frames + builder that
  splices in the dynamic HW_INIT/frequency packets), no socket/asyncio;
- 24-bit interleaved IQ pack/unpack (vectorized) and a defensive
  RX packet parser that returns None on anything malformed;
- TX telemetry (0x1F00 power/volts/temp, 0x1F01 PTT-confirm flag) with
  strict length checks and no fabricated SWR field.
"""
import struct
import unittest

import numpy as np

from backends.sunsdr import protocol as sdp


def _header_fields(pkt: bytes):
    return struct.unpack_from("<HHIIH", pkt, 0)


def _trailing(pkt: bytes) -> int:
    return struct.unpack_from("<I", pkt, len(pkt) - 4)[0]


class BuildPacketTests(unittest.TestCase):
    def test_header_layout_and_lengths(self):
        pkt = sdp.build_packet(sdp.CmdID.HEARTBEAT, b"")
        self.assertEqual(len(pkt), 18)  # 14 header + 4 trailing
        magic, cmd, data_len, index, reserved = _header_fields(pkt)
        self.assertEqual(magic, 0xFF32)
        self.assertEqual(cmd, sdp.CmdID.HEARTBEAT)
        self.assertEqual(data_len, 0)
        self.assertEqual(index, 0x00010000)
        self.assertEqual(reserved, 0)
        self.assertEqual(_trailing(pkt), 0)

    def test_payload_length_field_and_trailing(self):
        pkt = sdp.build_packet(0x1234, b"\x01\x02\x03", trailing=0xDEADBEEF)
        self.assertEqual(len(pkt), 14 + 3 + 4)
        _, cmd, data_len, _, _ = _header_fields(pkt)
        self.assertEqual(cmd, 0x1234)
        self.assertEqual(data_len, 3)
        self.assertEqual(pkt[14:17], b"\x01\x02\x03")
        self.assertEqual(_trailing(pkt), 0xDEADBEEF)

    def test_custom_index(self):
        pkt = sdp.build_packet(0x0001, b"", index=0x00020000)
        self.assertEqual(_header_fields(pkt)[3], 0x00020000)

    def test_heartbeat_matches_capture_hex(self):
        # PROTOCOL.md §16.2 / sunsdr_direct.py comment — 22 bytes.
        self.assertEqual(
            sdp.build_packet(sdp.CmdID.HEARTBEAT, struct.pack("<I", 0)).hex(),
            "32ff1800040000000000010000000000000000000000")


class CmdIdTests(unittest.TestCase):
    def test_required_constants(self):
        self.assertEqual(sdp.CmdID.HW_INIT, 0x0001)
        self.assertEqual(sdp.CmdID.START_OPS, 0x0002)
        self.assertEqual(sdp.CmdID.SET_PARAM_5, 0x0005)
        self.assertEqual(sdp.CmdID.PTT, 0x0006)
        self.assertEqual(sdp.CmdID.RX_FREQ, 0x0008)
        self.assertEqual(sdp.CmdID.TX_FREQ, 0x0009)
        self.assertEqual(sdp.CmdID.DRIVE, 0x0017)
        self.assertEqual(sdp.CmdID.HEARTBEAT, 0x0018)
        self.assertEqual(sdp.CmdID.STREAM_CTRL, 0x0020)


class PttTests(unittest.TestCase):
    def test_tx_golden_hex(self):
        # 22 bytes: 14-byte header (len=4) + 4 zero payload + trailing=1.
        # PROTOCOL.md §16.2 prints a 46-char string with a corrupted
        # trailing region ("...000001000000"); that literal cannot occur
        # on the wire — the trailing word of a TX packet is 0x00000001.
        self.assertEqual(
            sdp.build_ptt(True).hex(),
            "32ff0600040000000000010000000000000001000000")

    def test_rx_golden_hex(self):
        self.assertEqual(
            sdp.build_ptt(False).hex(),
            "32ff0600040000000000010000000000000000000000")

    def test_payload_is_four_zero_bytes_state_in_trailing(self):
        for tx in (True, False):
            pkt = sdp.build_ptt(tx)
            magic, cmd, data_len, index, _ = _header_fields(pkt)
            self.assertEqual((magic, cmd, data_len, index),
                             (0xFF32, sdp.CmdID.PTT, 4, 0x00010000))
            self.assertEqual(pkt[14:18], b"\x00\x00\x00\x00")
            self.assertEqual(_trailing(pkt), 1 if tx else 0)


class DriveTests(unittest.TestCase):
    def test_sqrt_taper_golden_values(self):
        # Formula from PROTOCOL.md §18: round(255 * sqrt(pct/100)).
        self.assertEqual(_trailing(sdp.build_drive(0)), 0x00)
        self.assertEqual(_trailing(sdp.build_drive(50)), 0xB4)
        self.assertEqual(_trailing(sdp.build_drive(100)), 0xFF)
        self.assertEqual(_trailing(sdp.build_drive(30)), round(255 * 0.3 ** 0.5))

    def test_drive_byte_lives_in_trailing_not_payload(self):
        # The original "low TX power" bug put the byte in the payload.
        pkt = sdp.build_drive(100)
        self.assertEqual(_trailing(pkt), 0xFF)
        self.assertEqual(pkt[14:18], b"\x00\x00\x00\x00")
        _, cmd, data_len, _, _ = _header_fields(pkt)
        self.assertEqual((cmd, data_len), (sdp.CmdID.DRIVE, 4))

    def test_out_of_range_is_clamped(self):
        self.assertEqual(_trailing(sdp.build_drive(-5)), 0)
        self.assertEqual(_trailing(sdp.build_drive(250)), 0xFF)

    def test_golden_boot_drive_hex(self):
        self.assertEqual(
            sdp.build_packet(sdp.CmdID.DRIVE, struct.pack("<I", 0),
                             trailing=0xDC).hex(),
            "32ff17000400000000000100000000000000dc000000")


class FrequencyTests(unittest.TestCase):
    def test_rx_dds_includes_if_offset(self):
        pkt = sdp.build_rx_frequency(7_074_000)
        _, cmd, data_len, _, _ = _header_fields(pkt)
        self.assertEqual((cmd, data_len), (sdp.CmdID.RX_FREQ, 8))
        index, dds = struct.unpack_from("<II", pkt, 14)
        self.assertEqual(index, 0)
        self.assertEqual(dds, (7_074_000 + 30500) * 10)

    def test_tx_vfo_exact(self):
        pkt = sdp.build_tx_frequency(7_074_000)
        _, cmd, data_len, _, _ = _header_fields(pkt)
        self.assertEqual((cmd, data_len), (sdp.CmdID.TX_FREQ, 8))
        index, vfo = struct.unpack_from("<II", pkt, 14)
        self.assertEqual(index, 0)
        self.assertEqual(vfo, 7_074_000 * 10)

    def test_fractional_hz_truncates_to_integer(self):
        _, dds = struct.unpack_from("<II", sdp.build_rx_frequency(7_074_000.7), 14)
        self.assertIsInstance(dds, int)
        self.assertEqual(dds, int((7_074_000.7 + 30500.0) * 10))

    def test_trailing_is_zero(self):
        self.assertEqual(_trailing(sdp.build_rx_frequency(1e6)), 0)
        self.assertEqual(_trailing(sdp.build_tx_frequency(1e6)), 0)


class SampleRateTests(unittest.TestCase):
    def test_rate_tables(self):
        self.assertEqual(sorted(sdp.SAMPLE_RATES.values()),
                         [39062, 78125, 156250, 312500])
        self.assertEqual(sdp.HW_INIT_RATE_INDEX["39k"], 0)
        self.assertEqual(sdp.HW_INIT_RATE_INDEX["78k"], 1)
        self.assertEqual(sdp.HW_INIT_RATE_INDEX["156k"], 2)
        self.assertEqual(sdp.HW_INIT_RATE_INDEX["312k"], 3)

    def test_hw_init_structure_all_four_rates(self):
        for key, index in sdp.HW_INIT_RATE_INDEX.items():
            pkt = sdp.build_hw_init(key)
            _, cmd, data_len, _, _ = _header_fields(pkt)
            self.assertEqual((cmd, data_len), (sdp.CmdID.HW_INIT, 50))
            payload = pkt[14:64]
            self.assertEqual(len(payload), 50)
            words = struct.unpack_from("<12I", payload, 0)
            self.assertEqual(words[1:9], (50,) * 8)  # calibration params
            self.assertEqual(words[9], 0)
            self.assertEqual(words[10], index * 65536 + 1)
            self.assertEqual(words[11], index)
            self.assertEqual(payload[48:50], b"\x00\x00")  # 2-byte pad
            self.assertEqual(_trailing(pkt), 0x37E00000)

    def test_hw_init_accepts_integer_rate(self):
        self.assertEqual(sdp.build_hw_init(78125), sdp.build_hw_init("78k"))
        self.assertEqual(sdp.build_hw_init(39062), sdp.build_hw_init("39k"))

    def test_hw_init_default_is_78k(self):
        self.assertEqual(sdp.build_hw_init(), sdp.build_hw_init("78k"))

    def test_hw_init_unknown_rate_raises(self):
        with self.assertRaises(ValueError):
            sdp.build_hw_init("44k")
        with self.assertRaises(ValueError):
            sdp.build_hw_init(44100)

    def test_stream_ctrl_golden(self):
        pkt = sdp.build_stream_ctrl()
        _, cmd, data_len, _, _ = _header_fields(pkt)
        self.assertEqual((cmd, data_len), (sdp.CmdID.STREAM_CTRL, 52))
        words = struct.unpack_from("<13I", pkt, 14)
        self.assertEqual(list(words), [0, 0, 1, 0, 0, 100, 0, 0, 30, 700, 7, 100, 300])
        self.assertEqual(words[1], 0)  # word[1]=1 kills SunSDR2 TX power
        self.assertEqual(_trailing(pkt), 100)
        # Byte-for-byte equal to the PROTOCOL.md §4.5 capture line.
        self.assertEqual(pkt.hex(),
                         "32ff20003400000000000100000000000000000000000100"
                         "000000000000000000006400000000000000000000001e00"
                         "0000bc02000007000000640000002c01000064000000")


class BootSequenceTests(unittest.TestCase):
    def test_fixed_frames_match_reference_capture(self):
        # Byte-for-byte against the reference client's boot hex
        # (sunsdr_direct.py) — a transcription slip in ANY fixed frame
        # must fail here, not on the air. This table is the independent
        # copy; do not "fix" odd-looking lengths without a new capture.
        ref_phase1 = (
            "32ff0200040000000000010000000000000000000000",
            "32ff5f000600000000000100000000000000000000000000",
            "32ff5f000600000000000100000000000000000000000000",
            "32ff5f000600000000000100000000000000000000000000",
            "32ff1d00040000000000010000000000000000000000",
            "32ff1b00040000000000010000000000000000000000",
            "32ff0500040000000000010000000000000001000000",
            "32ff1800040000000000010000000000000000000000",
            "32ff19000400000000000100000000000000bb000000",
            "32ff2100040000000000010000000000000001000000",
        )
        ref_status = ("32ff5a000000000000000100000000000000",) * 5
        ref_pre = (
            "32ff17000400000000000100000000000000dc000000",
            "32ff1e00040000000000010000000000000000000000",
            "32ff1500040000000000010000000000000001000000",
            "32ff07001a0000000000010000000000000000000000"
            "00000000000000000000000000000000000000000000",
            "32ff2400040000000000010000000000000000000000",
        )
        ref_post = (
            "32ff1800040000000000010000000000000000000000",
            "32ff2600040000000000010000000000000000000000",
            "32ff27001000000000000100000000000000dc460300b6d20000dc460300b6d20000",
            "32ff22000c00000000000100000000000000000000000084d71700000000",
        )
        groups = (
            ("phase1", sdp.BOOT_PHASE1_HEX, ref_phase1),
            ("status", sdp.BOOT_STATUS_HEX, ref_status),
            ("stream-pre", sdp.BOOT_STREAM_PRE_HEX, ref_pre),
            ("stream-post", sdp.BOOT_STREAM_POST_HEX, ref_post),
        )
        for name, mine, ref in groups:
            self.assertEqual(tuple(mine), ref,
                             f"{name} fixed frames drifted from the capture")
        # Structural check: total length must be 14 + data_len + 4 for
        # every fixed frame (catches a dropped/added payload byte even if
        # someone edits both copies of the table the same wrong way).
        all_frames = [h for _, mine, _ in groups for h in mine]
        self.assertEqual(len(all_frames), 24)
        for h in all_frames:
            raw = bytes.fromhex(h)
            data_len = struct.unpack_from("<I", raw, 4)[0]
            self.assertEqual(len(raw), 14 + data_len + 4, h)

    def test_set_param_7_full_golden(self):
        # 0x0007 carries a 26-byte all-zero payload: 14 + 26 + 4 = 44 B.
        # A second concatenated literal must not lose a byte (it did once).
        raw = bytes.fromhex(sdp.BOOT_STREAM_PRE_HEX[3])
        self.assertEqual(len(raw), 44)
        self.assertEqual(struct.unpack_from("<I", raw, 4)[0], 26)
        self.assertEqual(raw.hex(),
                         "32ff07001a0000000000010000000000000000000000"
                         "00000000000000000000000000000000000000000000")

    def test_phase1_order(self):
        cmds = [struct.unpack_from("<H", bytes.fromhex(h), 2)[0]
                for h in sdp.BOOT_PHASE1_HEX]
        self.assertEqual(cmds, [
            sdp.CmdID.START_OPS, sdp.CmdID.PRE_CONFIG, sdp.CmdID.PRE_CONFIG,
            sdp.CmdID.PRE_CONFIG, sdp.CmdID.SET_PARAM_1D,
            sdp.CmdID.SET_PARAM_1B, sdp.CmdID.SET_PARAM_5,
            sdp.CmdID.HEARTBEAT, sdp.CmdID.SET_PARAM_19,
            sdp.CmdID.SET_PARAM_21,
        ])
        # §21.1 correction: SET_PARAM_5 trailing is 1 (matches ExpertSDR3).
        p5 = bytes.fromhex(sdp.BOOT_PHASE1_HEX[6])
        self.assertEqual(_trailing(p5), 1)

    def test_start_ops_is_first_and_power_on(self):
        first = bytes.fromhex(sdp.BOOT_PHASE1_HEX[0])
        self.assertEqual(struct.unpack_from("<H", first, 2)[0],
                         sdp.CmdID.START_OPS)

    def test_build_boot_sequence_order_and_count(self):
        seq = sdp.build_boot_sequence(7_074_000, rate="78k")
        self.assertEqual(len(seq), 29)  # 10+5+1+3+10 per reference client
        cmds = [struct.unpack_from("<H", p, 2)[0] for p in seq]
        self.assertEqual(cmds[:15], [
            sdp.CmdID.START_OPS, sdp.CmdID.PRE_CONFIG, sdp.CmdID.PRE_CONFIG,
            sdp.CmdID.PRE_CONFIG, sdp.CmdID.SET_PARAM_1D,
            sdp.CmdID.SET_PARAM_1B, sdp.CmdID.SET_PARAM_5,
            sdp.CmdID.HEARTBEAT, sdp.CmdID.SET_PARAM_19,
            sdp.CmdID.SET_PARAM_21,
            sdp.CmdID.STATUS, sdp.CmdID.STATUS, sdp.CmdID.STATUS,
            sdp.CmdID.STATUS, sdp.CmdID.STATUS,
        ])
        self.assertEqual(cmds[15], sdp.CmdID.HW_INIT)
        self.assertEqual(cmds[16], sdp.CmdID.TX_FREQ)
        self.assertEqual(cmds[17], sdp.CmdID.RX_FREQ)
        self.assertEqual(cmds[18], sdp.CmdID.RX_FREQ)  # sent twice on purpose
        self.assertEqual(cmds[24], sdp.CmdID.STREAM_CTRL)

    def test_build_boot_sequence_splices_dynamic_packets(self):
        seq = sdp.build_boot_sequence(14_200_000, rate="156k")
        self.assertEqual(seq[15], sdp.build_hw_init("156k"))
        self.assertEqual(seq[16], sdp.build_tx_frequency(14_200_000))
        self.assertEqual(seq[17], sdp.build_rx_frequency(14_200_000))
        self.assertEqual(seq[24], sdp.build_stream_ctrl())
        # Boot drive byte is the captured 0xDC.
        self.assertEqual(_trailing(seq[19]), 0xDC)

    def test_fixed_frames_match_reference_hex(self):
        self.assertEqual(sdp.BOOT_PHASE1_HEX[0],
                         "32ff0200040000000000010000000000000000000000")
        self.assertEqual(sdp.BOOT_PHASE1_HEX[1],
                         "32ff5f000600000000000100000000000000000000000000")
        self.assertEqual(sdp.BOOT_STATUS_HEX[0],
                         "32ff5a000000000000000100000000000000")
        self.assertEqual(sdp.BOOT_STREAM_PRE_HEX[0],
                         "32ff17000400000000000100000000000000dc000000")
        self.assertEqual(sdp.BOOT_STREAM_POST_HEX[0],
                         "32ff1800040000000000010000000000000000000000")


class IqPackTests(unittest.TestCase):
    def test_golden_bytes(self):
        out = sdp.pack_iq_24bit(np.array([0.5 - 0.25j], dtype=np.complex64))
        # I = 0.5*2^23 = 0x400000 -> 00 00 40
        # Q = -0.25*2^23 = 0xFFE00000 -> low 3 bytes 00 00 E0 (two's complement)
        self.assertEqual(out, bytes.fromhex("0000400000e0"))

    def test_two_hundred_samples_is_1200_bytes(self):
        iq = np.zeros(200, dtype=np.complex64)
        self.assertEqual(len(sdp.pack_iq_24bit(iq)), 1200)

    def test_negative_full_scale_roundtrips_exactly(self):
        out = sdp.unpack_iq_24bit(sdp.pack_iq_24bit(np.array([-1.0 + 0j],
                                                             dtype=np.complex64)))
        self.assertEqual(out.dtype, np.complex64)
        self.assertEqual(float(out[0].real), -1.0)
        self.assertEqual(float(out[0].imag), 0.0)

    def test_positive_full_scale_saturates_to_max_code(self):
        out = sdp.unpack_iq_24bit(sdp.pack_iq_24bit(np.array([1.0 + 1.0j],
                                                             dtype=np.complex64)))
        self.assertAlmostEqual(float(out[0].real), 8388607 / 8388608.0, places=9)
        self.assertAlmostEqual(float(out[0].imag), 8388607 / 8388608.0, places=9)

    def test_nan_and_inf_are_deterministic(self):
        # NaN -> code 0 (nan_to_num), +/-inf saturate to the int24 rails.
        # (np.inf*1j in Python yields NaN real via 0*inf — build via complex()
        # so the input really is what the name says.)
        iq = np.array([complex(np.nan, 0.0), complex(np.inf, -np.inf),
                       complex(-np.inf, 0.5)], dtype=np.complex64)
        out = sdp.unpack_iq_24bit(sdp.pack_iq_24bit(iq))
        self.assertEqual(complex(out[0]), 0 + 0j)
        self.assertAlmostEqual(float(out[1].real), 8388607 / 8388608.0, places=9)
        self.assertAlmostEqual(float(out[1].imag), -1.0)
        self.assertAlmostEqual(float(out[2].real), -1.0)
        self.assertAlmostEqual(float(out[2].imag), 0.5, places=6)
        # and the same input packs to the same bytes every time
        self.assertEqual(sdp.pack_iq_24bit(iq), sdp.pack_iq_24bit(iq))

    def test_roundtrip_quantization_error(self):
        rng = np.random.default_rng(20261108)
        iq = (rng.uniform(-1.0, 1.0, 200) + 1j * rng.uniform(-1.0, 1.0, 200)
              ).astype(np.complex64)
        out = sdp.unpack_iq_24bit(sdp.pack_iq_24bit(iq))
        self.assertEqual(out.shape, (200,))
        # rounding to the 2^23 grid: max error is one half code (plus f32 repr)
        max_err = float(np.max(np.abs(out - iq)))
        self.assertLess(max_err, 2e-7)

    def test_unpack_rejects_non_multiple_of_six(self):
        for n in (1, 2, 3, 4, 5, 7, 11):
            with self.assertRaises(ValueError):
                sdp.unpack_iq_24bit(b"\x00" * n)

    def test_unpack_empty_is_empty_array(self):
        out = sdp.unpack_iq_24bit(b"")
        self.assertEqual(out.dtype, np.complex64)
        self.assertEqual(out.shape, (0,))

    def test_pack_two_samples_interleaves_i_then_q(self):
        out = sdp.pack_iq_24bit(np.array([1.0 + 0j, 0 + 1.0j], dtype=np.complex64))
        # sample0: I=max code (FF FF 7F), Q=0 (00 00 00)
        # sample1: I=0, Q=max code
        self.assertEqual(out[:6], bytes.fromhex("ffff7f000000"))
        self.assertEqual(out[6:], bytes.fromhex("000000ffff7f"))


class RxPacketTests(unittest.TestCase):
    def _iq_packet(self, samples, counter=0x04B0, flags=0x0001):
        hdr = struct.pack("<HHIH", 0xFF32, 0xFFFE, counter, flags)
        return hdr + sdp.pack_iq_24bit(samples)

    def test_valid_iq_packet(self):
        iq = (np.arange(200, dtype=np.float32) / 200.0
              + 1j * np.arange(200, dtype=np.float32) / 400.0).astype(np.complex64)
        raw = self._iq_packet(iq, counter=0x04B0, flags=0x0001)
        self.assertEqual(len(raw), 1210)
        pkt = sdp.parse_rx_packet(raw)
        self.assertIsNotNone(pkt)
        self.assertEqual(pkt["kind"], "iq")
        self.assertEqual(pkt["counter"], 0x04B0)
        self.assertEqual(pkt["flags"], 0x0001)
        self.assertEqual(pkt["samples"].shape, (200,))
        self.assertEqual(pkt["samples"].dtype, np.complex64)
        np.testing.assert_allclose(pkt["samples"], iq, atol=2e-7)

    def test_truncated_header_returns_none(self):
        self.assertIsNone(sdp.parse_rx_packet(b"\x32\xff\xfe\xff\xb0\x04\x00\x01\x00"))
        self.assertIsNone(sdp.parse_rx_packet(b""))

    def test_bad_magic_returns_none(self):
        raw = bytearray(self._iq_packet(np.zeros(4, dtype=np.complex64)))
        raw[0] = 0x33
        self.assertIsNone(sdp.parse_rx_packet(bytes(raw)))

    def test_unknown_subtype_returns_none(self):
        raw = struct.pack("<HHIH", 0xFF32, 0x1234, 0, 0) + b"\x00" * 1200
        self.assertIsNone(sdp.parse_rx_packet(raw))

    def test_iq_payload_not_multiple_of_six_returns_none(self):
        hdr = struct.pack("<HHIH", 0xFF32, 0xFFFE, 0, 0)
        self.assertIsNone(sdp.parse_rx_packet(hdr + b"\x00" * 7))
        self.assertIsNone(sdp.parse_rx_packet(hdr))  # zero samples

    def test_telemetry_subtypes_route_through(self):
        pwr = struct.pack("<HHIH", 0xFF32, 0x1F00, 0, 0) + b"\x00" * 24
        pkt = sdp.parse_rx_packet(pwr)
        self.assertIsNotNone(pkt)
        self.assertEqual(pkt["kind"], "telemetry")
        st = struct.pack("<HHIH", 0xFF32, 0x1F01, 0, 0) + b"\x00" * 12
        pkt = sdp.parse_rx_packet(st)
        self.assertIsNotNone(pkt)
        self.assertEqual(pkt["kind"], "telemetry")

    def test_garbage_never_raises(self):
        rng = np.random.default_rng(7)
        for _ in range(300):
            n = int(rng.integers(0, 80))
            raw = bytes(rng.integers(0, 256, n, dtype=np.uint8))
            try:
                sdp.parse_rx_packet(raw)
            except Exception as exc:  # pragma: no cover - failure path
                self.fail(f"parse_rx_packet raised on {raw!r}: {exc}")


class TelemetryTests(unittest.TestCase):
    def _power_packet(self, forward_w=101.0, avg_w=33.0, volts_raw=136,
                      temp=42.5, size=34):
        raw = bytearray(struct.pack("<HHIH", 0xFF32, 0x1F00, 0, 0)
                        + b"\x00" * 24)
        struct.pack_into("<H", raw, 16, volts_raw)
        struct.pack_into("<f", raw, 18, temp)
        struct.pack_into("<f", raw, 22, avg_w)
        struct.pack_into("<f", raw, 30, forward_w)
        raw += b"\x00" * (size - 34)
        return bytes(raw[:size])

    def test_power_fields_offsets(self):
        t = sdp.parse_telemetry(self._power_packet())
        self.assertIsNotNone(t)
        self.assertAlmostEqual(t["forward_w"], 101.0, places=5)
        self.assertAlmostEqual(t["avg_w"], 33.0, places=5)
        self.assertAlmostEqual(t["volts"], 13.6, places=5)
        self.assertAlmostEqual(t["pa_temp_c"], 42.5, places=5)
        self.assertNotIn("swr", t)  # the device sends no reverse-power field

    def test_power_requires_34_bytes(self):
        self.assertIsNone(sdp.parse_telemetry(self._power_packet(size=33)))
        self.assertIsNone(sdp.parse_telemetry(self._power_packet(size=10)))

    def test_status_ptt_confirm_bit(self):
        # Trailing u32 bit 0x10000 toggles during TX (verified 2026-06-25).
        hdr = struct.pack("<HHIH", 0xFF32, 0x1F01, 0, 0)
        rx_only = hdr + b"\x00" * 12
        self.assertEqual(sdp.parse_telemetry(rx_only)["ptt"], False)
        tx = bytearray(rx_only)
        struct.pack_into("<I", tx, 18, 0x00010000)
        self.assertEqual(sdp.parse_telemetry(bytes(tx))["ptt"], True)

    def test_status_requires_22_bytes(self):
        hdr = struct.pack("<HHIH", 0xFF32, 0x1F01, 0, 0)
        self.assertIsNone(sdp.parse_telemetry(hdr + b"\x00" * 11))

    def test_unknown_subtype_and_bad_magic_return_none(self):
        hdr = struct.pack("<HHIH", 0xFF32, 0x1F02, 0, 0) + b"\x00" * 40
        self.assertIsNone(sdp.parse_telemetry(hdr))
        raw = bytearray(self._power_packet())
        raw[0] = 0x33
        self.assertIsNone(sdp.parse_telemetry(bytes(raw)))

    def test_telemetry_never_raises_on_garbage(self):
        rng = np.random.default_rng(11)
        for _ in range(200):
            n = int(rng.integers(0, 60))
            raw = bytes(rng.integers(0, 256, n, dtype=np.uint8))
            try:
                sdp.parse_telemetry(raw)
            except Exception as exc:  # pragma: no cover - failure path
                self.fail(f"parse_telemetry raised on {raw!r}: {exc}")


if __name__ == "__main__":
    unittest.main()
