"""Tests for the pure-function Icom CI-V codec (no hardware)."""
import unittest

from backends.ic7300.civ_codec import (
    CONTROLLER_ADDR,
    END_OF_MESSAGE,
    NG,
    OK,
    PREAMBLE,
    RADIO_ADDR,
    CivFrame,
    CivFrameParser,
    ScopeAssembler,
    ScopeSegment,
    build_frame,
    decode_freq_bcd,
    decode_level_bcd,
    encode_freq_bcd,
    encode_level_bcd,
    is_echo,
    parse_scope_segment,
    scale_scope_bins,
    upsample_bins,
)


def _bcd(n: int) -> int:
    """Encode a small decimal value as one BCD byte (11 -> 0x11)."""
    return ((n // 10) << 4) | (n % 10)


def _scope_frame(seq: int, seq_max: int = 11, payload: bytes = b"") -> bytes:
    """Build an on-the-wire scope chunk as the radio would send it."""
    data = bytes((0x00, 0x00, _bcd(seq), _bcd(seq_max))) + payload
    return build_frame(0x27, data, to=CONTROLLER_ADDR, from_addr=RADIO_ADDR)


# Real IC-7300 captures (documented in wfview icomudpcivdata.cpp):
# fixed-mode info chunk (14.000-14.350 MHz), chunk 7 (50 bins), chunk 11 (25 bins).
CHUNK1_INFO = bytes.fromhex(
    "00 00 01 11 01 00 00 00 14 00 00 00 35 14 00 00"
)
CHUNK7_BINS = bytes.fromhex(
    "27 13 15 01 00 22 21 09 08 06 19 0e 20 23 25 2c 2d 17 27 29 16 14 1b 1b"
    " 21 27 1a 18 17 1e 21 1b 24 21 22 23 13 19 23 2f 2d 25 25 0a 0e 1e 20"
    " 1f 1a 0c"
)
CHUNK11_BINS = bytes.fromhex(
    "0b 13 21 23 1a 1b 22 1e 1a 1d 13 21 1d 26 28 1f 19 1a 18 09 2c 2c 2c"
    " 1a 1b"
)


class FramingTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(PREAMBLE, b"\xfe\xfe")
        self.assertEqual(END_OF_MESSAGE, 0xFD)
        self.assertEqual(CONTROLLER_ADDR, 0xE0)
        self.assertEqual(RADIO_ADDR, 0x94)
        self.assertEqual(OK, 0xFB)
        self.assertEqual(NG, 0xFA)

    def test_build_frame_layout(self):
        raw = build_frame(0x03)
        self.assertEqual(raw, bytes((0xFE, 0xFE, 0x94, 0xE0, 0x03, 0xFD)))

    def test_build_frame_with_data_and_addresses(self):
        raw = build_frame(0x15, b"\x02", to=0x94, from_addr=0xE0)
        self.assertEqual(raw, bytes((0xFE, 0xFE, 0x94, 0xE0, 0x15, 0x02, 0xFD)))

    def test_civframe_to_bytes_matches_build_frame(self):
        f = CivFrame(to=0x94, from_addr=0xE0, command=0x1A, data=b"\x05\x01")
        self.assertEqual(bytes(f), f.to_bytes())
        self.assertEqual(bytes(f), build_frame(0x1A, b"\x05\x01"))


class ParserTests(unittest.TestCase):
    def test_single_frame(self):
        p = CivFrameParser()
        frames = p.feed(build_frame(0x03))
        self.assertEqual(len(frames), 1)
        f = frames[0]
        self.assertEqual((f.to, f.from_addr, f.command, f.data), (0x94, 0xE0, 0x03, b""))

    def test_round_trip_with_data(self):
        p = CivFrameParser()
        raw = build_frame(0x15, b"\x02\x01\x20", to=0xE0, from_addr=0x94)
        (f,) = p.feed(raw)
        self.assertEqual(f.data, b"\x02\x01\x20")
        self.assertEqual(f.from_addr, 0x94)

    def test_frame_split_across_feeds(self):
        p = CivFrameParser()
        raw = build_frame(0x06, b"\x01\x02")
        out = []
        for i in range(len(raw)):
            out.extend(p.feed(raw[i : i + 1]))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].data, b"\x01\x02")

    def test_back_to_back_frames(self):
        p = CivFrameParser()
        raw = build_frame(0x03) + build_frame(0x04) + build_frame(0x05, b"\x00")
        frames = p.feed(raw)
        self.assertEqual([f.command for f in frames], [0x03, 0x04, 0x05])

    def test_garbage_prefix_discarded(self):
        p = CivFrameParser()
        frames = p.feed(b"\x00\x11\x22\x33" + build_frame(0x03))
        self.assertEqual(len(frames), 1)
        self.assertEqual(p.discarded_bytes, 4)

    def test_split_preamble_across_feeds(self):
        p = CivFrameParser()
        self.assertEqual(p.feed(b"\xfe"), [])
        frames = p.feed(b"\xfe\x94\xe0\x03\xfd")
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].command, 0x03)

    def test_triple_fe_overlapping_preambles(self):
        # Garbage FE immediately followed by a real frame: FE FE FE 94 ...
        p = CivFrameParser()
        frames = p.feed(b"\xfe" + build_frame(0x03))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].command, 0x03)
        self.assertEqual(p.discarded_bytes, 1)

    def test_partial_frame_then_new_preamble_resyncs(self):
        p = CivFrameParser()
        raw = b"\xfe\xfe\x94\xe0" + build_frame(0x04)  # first frame lacks FD
        frames = p.feed(raw)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].command, 0x04)
        self.assertEqual(p.discarded_bytes, 4)

    def test_malformed_short_frame_discarded(self):
        p = CivFrameParser()
        frames = p.feed(b"\xfe\xfe\xfd" + build_frame(0x03))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].command, 0x03)
        self.assertEqual(p.discarded_bytes, 3)

    def test_oversize_frame_reset(self):
        p = CivFrameParser()
        junk = b"\xfe\xfe" + bytes(100)  # no FD, exceeds 64-byte guard
        frames = p.feed(junk + build_frame(0x03))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].command, 0x03)
        self.assertGreaterEqual(p.discarded_bytes, 66)

    def test_incomplete_frame_waits_for_more_data(self):
        p = CivFrameParser()
        self.assertEqual(p.feed(b"\xfe\xfe\x94"), [])
        self.assertEqual(p.discarded_bytes, 0)
        frames = p.feed(b"\xe0\x03\xfd")
        self.assertEqual(len(frames), 1)


class EchoTests(unittest.TestCase):
    def test_own_frame_is_echo(self):
        sent = build_frame(0x05, encode_freq_bcd(14_074_000))
        p = CivFrameParser()
        (f,) = p.feed(sent)
        self.assertTrue(is_echo(f, sent))

    def test_radio_reply_is_not_echo(self):
        sent = build_frame(0x03)
        reply = build_frame(0x03, encode_freq_bcd(14_074_000),
                            to=CONTROLLER_ADDR, from_addr=RADIO_ADDR)
        p = CivFrameParser()
        (f,) = p.feed(reply)
        self.assertFalse(is_echo(f, sent))

    def test_different_content_is_not_echo(self):
        sent = build_frame(0x05, encode_freq_bcd(14_074_000))
        other = build_frame(0x05, encode_freq_bcd(7_100_000))
        p = CivFrameParser()
        (f,) = p.feed(other)
        self.assertFalse(is_echo(f, sent))


class FreqBcdTests(unittest.TestCase):
    VECTORS = {
        7_100_000: bytes.fromhex("00 00 10 07 00"),
        1_800_000: bytes.fromhex("00 00 80 01 00"),
        52_000_000: bytes.fromhex("00 00 00 52 00"),
        14_074_000: bytes.fromhex("00 40 07 14 00"),
        14_350_000: bytes.fromhex("00 00 35 14 00"),
        0: bytes(5),
        9_999_999_999: b"\x99" * 5,
    }

    def test_encode_vectors(self):
        for hz, want in self.VECTORS.items():
            self.assertEqual(encode_freq_bcd(hz), want, hz)

    def test_decode_vectors(self):
        for hz, raw in self.VECTORS.items():
            self.assertEqual(decode_freq_bcd(raw), hz, hz)

    def test_round_trip_sweep(self):
        for hz in (10, 100, 455_000, 3_573_000, 28_074_000, 70_250_000,
                   144_800_000, 1_296_000_000, 9_999_999_990):
            self.assertEqual(decode_freq_bcd(encode_freq_bcd(hz)), hz)

    def test_decode_rejects_over_5_bytes(self):
        with self.assertRaises(ValueError):
            decode_freq_bcd(bytes(6))

    def test_encode_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            encode_freq_bcd(10_000_000_000)
        with self.assertRaises(ValueError):
            encode_freq_bcd(-1)


class LevelBcdTests(unittest.TestCase):
    def test_encode_255_high_byte_first(self):
        # On the wire: hundreds+tens byte first, units second.
        self.assertEqual(encode_level_bcd(255), b"\x02\x55")

    def test_known_values(self):
        self.assertEqual(encode_level_bcd(0), b"\x00\x00")
        self.assertEqual(encode_level_bcd(9), b"\x00\x09")
        self.assertEqual(encode_level_bcd(100), b"\x01\x00")
        self.assertEqual(encode_level_bcd(120), b"\x01\x20")

    def test_round_trip_full_range(self):
        for v in range(256):
            self.assertEqual(decode_level_bcd(encode_level_bcd(v)), v)

    def test_decode_single_byte(self):
        self.assertEqual(decode_level_bcd(b"\x55"), 55)

    def test_encode_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            encode_level_bcd(256)


class ScopeSegmentTests(unittest.TestCase):
    def _parse(self, data: bytes) -> ScopeSegment:
        p = CivFrameParser()
        raw = build_frame(0x27, data, to=CONTROLLER_ADDR, from_addr=RADIO_ADDR)
        (frame,) = p.feed(raw)
        seg = parse_scope_segment(frame)
        self.assertIsNotNone(seg)
        return seg

    def test_info_chunk(self):
        seg = self._parse(CHUNK1_INFO)
        self.assertEqual(seg.sequence, 1)
        self.assertEqual(seg.sequence_max, 11)
        self.assertTrue(seg.is_division_start)
        self.assertFalse(seg.is_last)
        self.assertEqual(seg.scope_mode, 1)  # fixed mode
        self.assertEqual(seg.low_edge_hz, 14_000_000)
        self.assertEqual(seg.high_edge_hz, 14_350_000)
        self.assertFalse(seg.out_of_range)
        self.assertEqual(seg.bins, b"")

    def test_data_chunk(self):
        seg = self._parse(bytes((0x00, 0x00, 0x07, 0x11)) + CHUNK7_BINS)
        self.assertEqual(seg.sequence, 7)
        self.assertEqual(seg.sequence_max, 11)
        self.assertFalse(seg.is_division_start)
        self.assertFalse(seg.is_last)
        self.assertEqual(len(seg.bins), 50)
        self.assertEqual(seg.bins[0], 0x27)

    def test_final_chunk(self):
        seg = self._parse(bytes((0x00, 0x00, 0x11, 0x11)) + CHUNK11_BINS)
        self.assertEqual(seg.sequence, 11)
        self.assertTrue(seg.is_last)
        self.assertEqual(len(seg.bins), 25)

    def test_non_scope_frame_returns_none(self):
        p = CivFrameParser()
        (f,) = p.feed(build_frame(0x03))
        self.assertIsNone(parse_scope_segment(f))
        (f,) = p.feed(build_frame(0x27, b"\x11\x00"))  # wrong sub command
        self.assertIsNone(parse_scope_segment(f))


class ScopeAssemblerTests(unittest.TestCase):
    def _feed_waveform(self, asm: ScopeAssembler, skip=(), duplicate=()):
        """Feed one full 11-segment, 475-bin fixed-mode waveform."""
        results = []
        segments = [None]  # 1-based
        segments.append(ScopeSegment(sequence=1, sequence_max=11, bins=b"",
                                     is_division_start=True, scope_mode=1,
                                     low_edge_hz=14_000_000,
                                     high_edge_hz=14_350_000))
        for seq in range(2, 12):
            n = 25 if seq == 11 else 50
            segments.append(ScopeSegment(sequence=seq, sequence_max=11,
                                         bins=bytes([seq] * n)))
        for seq in range(1, 12):
            if seq in skip:
                continue
            results.append(asm.feed(segments[seq]))
            if seq in duplicate:
                results.append(asm.feed(segments[seq]))
        return results

    def test_full_waveform_assembly(self):
        asm = ScopeAssembler()
        results = self._feed_waveform(asm)
        complete = results[-1]
        self.assertTrue(all(r is None for r in results[:-1]))
        self.assertEqual(len(complete), 475)
        self.assertEqual(complete[0], 2)     # first bin of segment 2
        self.assertEqual(complete[-1], 11)   # last bin of segment 11

    def test_gap_drops_waveform(self):
        asm = ScopeAssembler()
        results = self._feed_waveform(asm, skip={5})
        self.assertTrue(all(r is None for r in results))

    def test_duplicate_drops_waveform(self):
        asm = ScopeAssembler()
        results = self._feed_waveform(asm, duplicate={4})
        self.assertTrue(all(r is None for r in results))

    def test_recovery_after_drop(self):
        asm = ScopeAssembler()
        self._feed_waveform(asm, skip={5})           # dropped
        results = self._feed_waveform(asm)           # next one is clean
        self.assertEqual(len(results[-1]), 475)

    def test_mid_waveform_segment_without_start_dropped(self):
        asm = ScopeAssembler()
        seg = ScopeSegment(sequence=5, sequence_max=11, bins=bytes(50))
        self.assertIsNone(asm.feed(seg))

    def test_lan_single_segment_waveform(self):
        asm = ScopeAssembler()
        seg = ScopeSegment(sequence=1, sequence_max=1, bins=bytes(475),
                           is_division_start=True)
        self.assertEqual(len(asm.feed(seg)), 475)

    def test_end_to_end_through_parser(self):
        """Full pipeline: wire bytes -> parser -> segments -> waveform."""
        p = CivFrameParser()
        asm = ScopeAssembler()
        raw = _scope_frame(1, payload=CHUNK1_INFO[4:])
        for seq in range(2, 12):
            raw += _scope_frame(seq, payload=bytes([seq] * (25 if seq == 11 else 50)))
        complete = None
        for frame in p.feed(raw):
            seg = parse_scope_segment(frame)
            self.assertIsNotNone(seg)
            complete = asm.feed(seg) or complete
        self.assertIsNotNone(complete)
        self.assertEqual(len(complete), 475)


class ScaleUpsampleTests(unittest.TestCase):
    def test_scale_defaults(self):
        self.assertEqual(scale_scope_bins([0, 32, 160]), [0, 51, 255])

    def test_scale_clamps(self):
        self.assertEqual(scale_scope_bins([0, 200, 255]), [0, 255, 255])

    def test_scale_custom_out_max(self):
        self.assertEqual(scale_scope_bins([0, 160], out_max=100), [0, 100])

    def test_upsample_length(self):
        out = upsample_bins(list(range(475)))
        self.assertEqual(len(out), 850)
        self.assertEqual(out[0], 0)
        self.assertEqual(out[-1], 474)

    def test_upsample_nearest_neighbor(self):
        self.assertEqual(upsample_bins([0, 100], 4), [0, 0, 100, 100])

    def test_upsample_empty(self):
        self.assertEqual(upsample_bins([]), [])


if __name__ == "__main__":
    unittest.main()
