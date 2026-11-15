"""VITA-49 codec for the FlexRadio backend (P1, design §5-§6).

Everything here runs from bytes: no socket, no radio, no audio device.
The three facts these tests exist to pin are the ones that fail silently in
the field:

* the Opus transmit packet's header words are byte-exact,
* FFT bins are **pixel Y**, and with the aperture we request a UI byte is
  exactly ``255 - pixel``,
* RX audio is big-endian float32 **stereo** at 24 kHz.
"""

import struct
import unittest

import numpy as np

from backends.flex import vita


def _header_word(packet_type=3, class_id=True, trailer=False, tsi=3, tsf=1,
                 count=0, size_words=0):
    return ((packet_type << 28)
            | (int(class_id) << 27)
            | (int(trailer) << 26)
            | (tsi << 22)
            | (tsf << 20)
            | ((count & 0xF) << 16)
            | (size_words & 0xFFFF))


def _packet(pcc, payload=b"", stream_id=0x40000000, trailer=b"", count=0):
    trailer_flag = bool(trailer)
    size_words = (vita.HEADER_BYTES + len(payload) + len(trailer) + 3) // 4
    word0 = _header_word(trailer=trailer_flag, count=count, size_words=size_words)
    head = struct.pack(">IIIIIII", word0, stream_id, vita.OPUS_OUI,
                       (vita.ICC << 16) | pcc, 0, 0, 0)
    return head + payload + trailer


class HeaderTests(unittest.TestCase):
    def test_fields_round_trip(self):
        packet = _packet(vita.PCC_FFT, b"\x00" * 16, count=5)
        head = vita.parse_header(packet)
        self.assertEqual(head.packet_type, 3)
        self.assertTrue(head.class_id_present)
        self.assertFalse(head.trailer_present)
        self.assertEqual(head.tsi, 3)
        self.assertEqual(head.tsf, 1)
        self.assertEqual(head.packet_count, 5)
        self.assertEqual(head.stream_id, 0x40000000)
        self.assertEqual((head.icc, head.pcc), (vita.ICC, vita.PCC_FFT))
        self.assertEqual(len(head.payload), 16)

    def test_trailer_is_stripped_before_the_payload(self):
        packet = _packet(vita.PCC_RX_AUDIO, b"\x00" * 8, trailer=b"\xde\xad\xbe\xef")
        head = vita.parse_header(packet)
        self.assertTrue(head.trailer_present)
        self.assertEqual(head.trailer, b"\xde\xad\xbe\xef")
        self.assertEqual(len(head.payload), 8)

    def test_short_packet_is_rejected(self):
        with self.assertRaises(vita.VitaError):
            vita.parse_header(b"\x30" * 12)

    def test_declared_size_larger_than_received_is_clamped(self):
        """The Opus builder declares ceil(bytes/4) words but never pads, so a
        parser that insists on the declared length rejects our own packets."""
        packet = _packet(vita.PCC_OPUS, b"\x01\x02\x03")
        self.assertEqual(len(packet) % 4, 3)
        head = vita.parse_header(packet)
        self.assertEqual(head.payload, b"\x01\x02\x03")

    def test_packet_pcc_helper(self):
        self.assertEqual(vita.packet_pcc(_packet(vita.PCC_METERS, b"\x00" * 8)),
                         vita.PCC_METERS)


class OpusPacketTests(unittest.TestCase):
    def test_header_bytes_are_exact(self):
        """Fixed layout from analysis §6 — any drift here transmits noise."""
        packet = vita.build_opus_packet(0x0B000002, b"\xaa\xbb")
        words = struct.unpack_from(">IIIIIII", packet, 0)
        self.assertEqual(words[0],
                         (3 << 28) | (1 << 27) | (3 << 22) | (1 << 20) | 8)
        self.assertEqual(words[1], 0x0B000002)
        self.assertEqual(words[2], 0x00001C2D)
        self.assertEqual(words[3], 0x534C8005)
        self.assertEqual(words[4:], (0, 0, 0))
        self.assertEqual(packet[vita.HEADER_BYTES:], b"\xaa\xbb")

    def test_round_trip_through_the_parser(self):
        head = vita.parse_header(vita.build_opus_packet(0x1234, b"\x01\x02\x03\x04"))
        self.assertEqual(head.pcc, vita.PCC_OPUS)
        self.assertEqual(head.stream_id, 0x1234)
        self.assertFalse(head.trailer_present)
        self.assertEqual(head.payload, b"\x01\x02\x03\x04")

    def test_empty_payload_is_refused(self):
        with self.assertRaises(vita.VitaError):
            vita.build_opus_packet(1, b"")


class FftTests(unittest.TestCase):
    @staticmethod
    def _segment_payload(start, bins, total, frame_index=1, head_len=12):
        head = struct.pack(">HHHHH", start, len(bins), 1, total, frame_index)
        head = head + b"\x00" * (head_len - len(head))
        return head + struct.pack(f">{len(bins)}H", *bins)

    def test_segment_round_trip(self):
        payload = self._segment_payload(0, [1, 2, 3], 3)
        seg = vita.parse_fft_segment(payload)
        self.assertEqual((seg.start_bin, seg.num_bins, seg.total_bins), (0, 3, 3))
        self.assertEqual(seg.bins, (1, 2, 3))

    def test_ten_byte_subheader_is_also_accepted(self):
        seg = vita.parse_fft_segment(
            self._segment_payload(0, [4, 5, 6], 3, head_len=10))
        self.assertEqual(seg.subheader_bytes, 10)
        self.assertEqual(seg.bins, (4, 5, 6))

    def test_inconsistent_subheader_falls_back_to_whole_frame(self):
        """A mis-read sub-header must degrade to "one packet = one frame",
        never to a plausible half-curve (design §6 / U3)."""
        bins = [10, 20, 30, 40]
        payload = b"\x00\x00\x00\x00\xff\xff" + struct.pack(">4H", *bins)
        seg = vita.parse_fft_segment(payload)
        self.assertEqual(seg.start_bin, 0)
        self.assertEqual(seg.total_bins, len(payload) // 2)

    def test_assembler_stitches_two_packets(self):
        asm = vita.FftAssembler()
        first = vita.parse_fft_segment(self._segment_payload(0, [1, 2], 4))
        second = vita.parse_fft_segment(self._segment_payload(2, [3, 4], 4))
        self.assertIsNone(asm.feed(first))
        frame = asm.feed(second)
        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertEqual(frame.tolist(), [1, 2, 3, 4])

    def test_assembler_discards_a_partial_frame_and_counts_it(self):
        asm = vita.FftAssembler()
        asm.feed(vita.parse_fft_segment(self._segment_payload(0, [1, 2], 4, 1)))
        # Next frame starts before the previous one completed.
        done = asm.feed(vita.parse_fft_segment(
            self._segment_payload(0, [7, 8, 9, 10], 4, 2)))
        self.assertEqual(asm.dropped_frames, 1)
        assert done is not None
        self.assertEqual(done.tolist(), [7, 8, 9, 10])

    def test_assembler_rejects_out_of_range_segment(self):
        """The parser cannot produce an overflowing segment (it would fall back
        to a whole frame), so the assembler's own guard is driven directly."""
        asm = vita.FftAssembler()
        bad = vita.FftSegment(start_bin=3, num_bins=2, bin_size=1,
                              total_bins=4, frame_index=1, bins=(1, 2))
        with self.assertRaises(vita.VitaError):
            asm.feed(bad)


class AudioTests(unittest.TestCase):
    def test_float32_stereo_is_decoded_big_endian(self):
        samples = np.asarray([[0.5, -0.5], [0.25, 0.0]], dtype=">f4").tobytes()
        frames = vita.parse_audio(samples)
        self.assertEqual(frames.shape, (2, 2))
        self.assertAlmostEqual(float(frames[0, 0]), 0.5, places=6)
        self.assertAlmostEqual(float(frames[0, 1]), -0.5, places=6)

    def test_odd_payload_is_rejected(self):
        with self.assertRaises(vita.VitaError):
            vita.parse_audio(b"\x00" * 12)

    def test_mono_downmix_averages_the_pair(self):
        frames = np.asarray([[1.0, 0.0], [0.0, 0.5]], dtype=np.float32)
        mono = vita.audio_mono(frames)
        self.assertEqual(mono.shape, (2,))
        self.assertAlmostEqual(float(mono[0]), 0.5, places=6)
        self.assertAlmostEqual(float(mono[1]), 0.25, places=6)

    def test_empty_audio_is_silence_not_an_error(self):
        self.assertEqual(vita.audio_mono(np.zeros((0, 2), dtype=np.float32)).size, 0)

    def test_dax_int16_mono_scales_to_unit_range(self):
        payload = struct.pack(">3h", 0, 16384, -32768)
        out = vita.parse_dax_audio(payload)
        self.assertAlmostEqual(float(out[1]), 0.5, places=6)
        self.assertAlmostEqual(float(out[2]), -1.0, places=6)


class MeterTests(unittest.TestCase):
    def test_pairs_include_negative_raw_values(self):
        payload = struct.pack(">HhHh", 7, 128, 17, -64)
        self.assertEqual(vita.parse_meters(payload), {7: 128, 17: -64})

    def test_bad_length_is_rejected(self):
        with self.assertRaises(vita.VitaError):
            vita.parse_meters(b"\x00\x01\x02")


class SpectrumMappingTests(unittest.TestCase):
    def test_ui_byte_is_255_minus_pixel_with_the_requested_aperture(self):
        """The whole point of asking for min=-120/max=0/ypixels=256."""
        pixels = np.arange(0, vita.PAN_YPIXELS, dtype=np.uint16)
        ui = vita.pixels_to_ui(pixels)
        self.assertTrue(np.array_equal(ui, (255 - pixels).astype(np.uint8)))

    def test_pixel_to_dbm_endpoints(self):
        db = vita.pixels_to_db(np.asarray([0, vita.PAN_YPIXELS - 1]))
        self.assertAlmostEqual(float(db[0]), vita.PAN_MAX_DBM, places=6)
        self.assertAlmostEqual(float(db[1]), vita.PAN_MIN_DBM, places=6)

    def test_other_apertures_still_map_correctly(self):
        # ypixels=128, min -140, max -20: top pixel is the max, bottom the min.
        # The UI window stays [-120, 0] dB: a radio asking for another aperture
        # still lands in it, clipped, rather than rescaling the axis per radio.
        ui = vita.pixels_to_ui(np.asarray([0, 127]), ypixels=128,
                               max_dbm=-20.0, min_dbm=-140.0)
        self.assertEqual([int(v) for v in ui], [212, 0])

    def test_db_to_ui_clips_outside_the_window(self):
        ui = vita.db_to_ui(np.asarray([-200.0, -120.0, 0.0, 50.0]))
        self.assertEqual(list(ui), [0, 0, 255, 255])


class DiscoveryTests(unittest.TestCase):
    def test_discovery_payload_is_returned_for_the_right_class(self):
        packet = _packet(0xFFFF, b"model=FLEX-6400 ip=10.0.0.5 port=4992\x00\x00",
                         stream_id=vita.DISCOVERY_STREAM_ID)
        payload = vita.parse_discovery_payload_bytes(packet)
        self.assertIsNotNone(payload)
        self.assertTrue(payload.startswith(b"model=FLEX-6400"))

    def test_other_streams_are_not_discovery(self):
        packet = _packet(vita.PCC_FFT, b"\x00" * 8)
        self.assertIsNone(vita.parse_discovery_payload_bytes(packet))

    def test_class_code_constant_matches_the_documented_value(self):
        self.assertEqual(vita.DISCOVERY_CLASS_ID, 0x534CFFFF)
        self.assertEqual(vita.DISCOVERY_STREAM_ID, 0x800)


class DaxIqRateTableTests(unittest.TestCase):
    def test_the_four_rates_are_registered(self):
        self.assertEqual(vita.DAX_IQ_RATES[0x02E3], 24000)
        self.assertEqual(vita.DAX_IQ_RATES[0x02E6], 192000)


if __name__ == "__main__":
    unittest.main()
