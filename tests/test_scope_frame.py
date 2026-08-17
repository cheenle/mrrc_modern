import struct
import unittest

from backends.ft710.scope_frame import (
    SCOPE_FRAME_SIZE,
    SYNC_TAIL,
    WF_SIZE,
    encode_pipe_payload,
    frame_quality,
    parse_pipe_payload,
    parse_scope_frame,
)


def make_frame() -> bytes:
    frame = bytearray(SCOPE_FRAME_SIZE)
    frame[0] = 0x00
    frame[1] = 0x7F
    frame[2] = 0xFF
    frame[WF_SIZE] = 0x10

    data_offset = 2900
    frame[data_offset + 17] = 3
    frame[data_offset + 27] = 0x06  # preamp=2, attenuator=1
    frame[data_offset + 32] = 7
    frame[data_offset + 60] = 0x01  # USB in scope mode table
    frame[data_offset + 64:data_offset + 69] = bytes.fromhex("0014200000")
    frame[data_offset + 110] = 123
    frame[data_offset + 144:data_offset + 148] = struct.pack(">I", 14_150_000)
    frame[-4:] = SYNC_TAIL
    return bytes(frame)


class ScopeFrameTests(unittest.TestCase):
    def test_parse_scope_frame_validates_sync_and_extracts_metadata(self):
        parsed = parse_scope_frame(make_frame())

        self.assertEqual(parsed.wf1[:3], [255, 128, 0])
        self.assertEqual(parsed.wf2[0], 239)
        self.assertEqual(parsed.scope_mode, 3)
        self.assertEqual(parsed.preamp, 2)
        self.assertEqual(parsed.attenuator, 1)
        self.assertEqual(parsed.scope_span, 7)
        self.assertEqual(parsed.mode, 0x02)
        self.assertEqual(parsed.vfoa_freq, 14_200_000)
        self.assertEqual(parsed.s_meter, 123)
        self.assertEqual(parsed.scope_start_freq, 14_150_000)

    def test_parse_scope_frame_rejects_unsynchronized_frame(self):
        bad = bytearray(make_frame())
        bad[-1] = 0x00

        with self.assertRaises(ValueError):
            parse_scope_frame(bytes(bad))

    def test_pipe_payload_round_trips_spectrum_and_metadata(self):
        parsed = parse_scope_frame(make_frame())
        payload = encode_pipe_payload(parsed)
        round_tripped = parse_pipe_payload(payload)

        self.assertEqual(round_tripped.wf1, parsed.wf1)
        self.assertEqual(round_tripped.wf2, parsed.wf2)
        self.assertEqual(round_tripped.scope_span, 7)
        self.assertEqual(round_tripped.scope_start_freq, 14_150_000)


class FrameQualityTests(unittest.TestCase):
    def test_empty_spectrum_is_all_zeros(self):
        q = frame_quality([])
        self.assertTrue(q["all_zeros"])
        self.assertFalse(q["all_ones"])
        self.assertEqual(q["dynamic_range"], 0)

    def test_all_zeros_spectrum(self):
        q = frame_quality([0] * 100)
        self.assertTrue(q["all_zeros"])
        self.assertEqual(q["max_val"], 0)

    def test_all_ones_spectrum(self):
        q = frame_quality([255] * 100)
        self.assertTrue(q["all_ones"])
        self.assertEqual(q["max_val"], 255)
        self.assertEqual(q["nonzero_pct"], 100.0)

    def test_normal_spectrum(self):
        wf = [0] * 50 + [100] * 25 + [200] * 25
        q = frame_quality(wf)
        self.assertFalse(q["all_zeros"])
        self.assertFalse(q["all_ones"])
        self.assertAlmostEqual(q["nonzero_pct"], 50.0)
        self.assertEqual(q["max_val"], 200)
        self.assertEqual(q["min_val"], 0)
        self.assertEqual(q["dynamic_range"], 200)


if __name__ == "__main__":
    unittest.main()
