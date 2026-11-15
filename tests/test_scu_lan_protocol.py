"""SCU-LAN10 wire format and spectrum bodies — pure bytes, no sockets, no radio.

Two things are checked here that a runtime test would never catch:

* the scramble table is compared against the **independently published** 256
  entries, not against itself — a wrong table still round-trips, and a wrong
  table means every packet the device sees is garbage;
* the five hardcoded init packets are decoded as the device decodes them, and
  their bodies must come out as four zero bytes.  The protocol write-up states
  ``04 04 04 04`` for those; under its own formulas they are zeros (and its
  keepalive section says zeros).  The bytes are the authority, so the
  assertion pins the bytes.
"""
import unittest

from backends.yaesu.scu_lan import protocol as p
from backends.yaesu.scu_lan import scope_format as sf

#: The published table for the first 256 positions, from the protocol's
#: reverse-engineering write-up.  Hexadecimal, space separated on purpose: a
#: failure prints the byte that disagrees.
PUBLISHED_GTABLE_256 = """
06 05 04 0B 0A 09 08 0F 0E 0D 0C 13 12 11 10 17
16 15 14 1B 1A 19 18 1F 1E 1D 1C 23 22 21 20 27
26 25 24 2B 2A 29 28 2F 2E 2D 2C 33 32 31 30 37
36 35 34 3B 3A 39 38 3F 3E 3D 3C 43 42 41 40 47
46 45 44 4B 4A 49 48 4F 4E 4D 4C 53 52 51 50 57
56 55 54 5B 5A 59 58 5F 5E 5D 5C 63 62 61 60 67
66 65 64 6B 6A 69 68 6F 6E 6D 6C 73 72 71 70 77
76 75 74 7B 7A 79 78 7F 7E 7D 7C 83 82 81 80 87
86 85 84 8B 8A 89 88 8F 8E 8D 8C 93 92 91 90 97
96 95 94 9B 9A 99 98 9F 9E 9D 9C A3 A2 A1 A0 A7
A6 A5 A4 AB AA A9 A8 AF AE AD AC B3 B2 B1 B0 B7
B6 B5 B4 BB BA B9 B8 BF BE BD BC C3 C2 C1 C0 C7
C6 C5 C4 CB CA C9 C8 CF CE CD CC D3 D2 D1 D0 D7
D6 D5 D4 DB DA D9 D8 DF DE DD DC E3 E2 E1 E0 E7
E6 E5 E4 EB EA E9 E8 EF EE ED EC F3 F2 F1 F0 F7
F6 F5 F4 FB FA F9 F8 FF FE FD FC 03 02 01 00 07
"""


def published_table() -> bytes:
    return bytes.fromhex(" ".join(PUBLISHED_GTABLE_256.split()))


def realistic_body(marker: int = 1900, size: int = 4096) -> bytearray:
    """A plausible sweep: signal floor, boundary marker, reference block."""
    body = bytearray([210] * size)          # noise floor, weak
    for i in range(marker):
        body[i] = 200 - (i * 37) % 150      # some structure, all > 1
    body[marker] = 1                        # the boundary marker
    for i in range(marker + 1, min(size, marker + 200)):
        body[i] = 233                       # reference / calibration block
    return body


class ScrambleTableTests(unittest.TestCase):
    def test_the_table_matches_the_published_256_entries(self):
        self.assertEqual(p.GTABLE[:256], published_table())

    def test_it_keeps_growing_without_changing_its_prefix(self):
        big = p.gtable(9000)
        self.assertEqual(len(big), 9000)
        self.assertEqual(big[:256], p.GTABLE[:256])


class InitPacketTests(unittest.TestCase):
    def test_the_five_init_packets_decode_as_keepalives_with_a_zero_body(self):
        self.assertEqual(len(p.INIT_PACKETS), 5)
        for index, raw in enumerate(p.INIT_PACKETS, start=1):
            packet = p.parse_wire(raw)
            self.assertEqual(packet.seq, index)
            self.assertEqual(packet.channel, p.CHANNEL_CTRL)
            self.assertEqual(packet.msg_type, p.MSG_KEEPALIVE)
            self.assertEqual(packet.body, b"\x00\x00\x00\x00",
                             f"packet {index}: the write-up says 04 04 04 04, "
                             f"the bytes say zero")

    def test_every_packet_is_exactly_one_datagram(self):
        for raw in p.INIT_PACKETS:
            self.assertEqual(len(raw), 18)
            self.assertEqual(raw[:4], b"\x5a\x5a\x0e\x00")


class FramingTests(unittest.TestCase):
    def test_a_round_trip_preserves_every_field(self):
        body = bytes(range(48))
        raw = p.frame(body, seq=0x2A, channel=p.CHANNEL_CAT,
                      msg_type=p.MSG_DATA, key=0x11)
        packet = p.parse_wire(raw)
        self.assertEqual((packet.seq, packet.channel, packet.msg_type,
                          packet.key, packet.body),
                         (0x2A, p.CHANNEL_CAT, p.MSG_DATA, 0x11, body))

    def test_an_empty_body_is_legal(self):
        raw = p.frame(b"", seq=1, channel=p.CHANNEL_SCOPE, msg_type=p.MSG_DATA)
        self.assertEqual(p.parse_wire(raw).body, b"")

    def test_a_4096_byte_scope_body_survives(self):
        body = bytes((i * 7) & 0xFF for i in range(4096))
        raw = p.frame(body, seq=3, channel=p.CHANNEL_SCOPE,
                      msg_type=p.MSG_DATA, key=0x5E)
        self.assertEqual(len(raw), 4096 + 14)
        self.assertEqual(p.parse_wire(raw).body, body)

    def test_a_bad_key_byte_rejects_the_packet(self):
        raw = bytearray(p.frame(b"\x01\x02", seq=4, channel=p.CHANNEL_CTRL,
                                msg_type=p.MSG_DATA, key=0x33))
        raw[11] ^= 0x01                     # b5 = key
        with self.assertRaises(p.MalformedPacket):
            p.parse_wire(bytes(raw))

    def test_tampering_with_any_validated_header_byte_is_rejected(self):
        raw = p.frame(b"\x01\x02\x03", seq=9, channel=p.CHANNEL_CTRL,
                      msg_type=p.MSG_KEEPALIVE, key=0x44)
        for offset in (7, 9, 12, 13):       # b1, b3, b6, b7
            damaged = bytearray(raw)
            damaged[offset] ^= 0x80
            with self.assertRaises(p.MalformedPacket,
                                   msg=f"offset {offset} accepted"):
                p.parse_wire(bytes(damaged))

    def test_magic_length_and_type_are_checked(self):
        raw = p.frame(b"\x01", seq=1, channel=p.CHANNEL_CTRL,
                      msg_type=p.MSG_DATA, key=0x01)
        for damaged in (b"\x00\x00" + raw[2:],     # magic
                        raw[:2] + b"\x70\x00" + raw[4:],   # length
                        raw[:4] + b"\x07\x00" + raw[6:]):  # type field
            with self.assertRaises(p.MalformedPacket):
                p.parse_wire(damaged)

    def test_a_truncated_datagram_is_rejected(self):
        raw = p.frame(b"\x01\x02\x03", seq=2, channel=p.CHANNEL_CTRL,
                      msg_type=p.MSG_DATA, key=0x02)
        with self.assertRaises(p.MalformedPacket):
            p.parse_wire(raw[:-1])

    def test_try_parse_swallows_the_error_for_a_receive_loop(self):
        self.assertIsNone(p.try_parse(b"nonsense"))
        self.assertIsNotNone(p.try_parse(p.INIT_PACKETS[0]))


class SequenceTests(unittest.TestCase):
    def test_the_counter_wraps_at_255(self):
        counter = p.SequenceCounter(0xFE)
        self.assertEqual([counter.next() for _ in range(4)], [0xFE, 0xFF, 0x00, 0x01])
        self.assertEqual(counter.value, 0x02)

    def test_a_wrapped_sequence_still_verifies_on_the_wire(self):
        raw = p.frame(b"\x00\x00\x00\x00", seq=0x00, channel=p.CHANNEL_CTRL,
                      msg_type=p.MSG_KEEPALIVE, key=0xC3)
        self.assertEqual(p.parse_wire(raw).seq, 0)


class AuthTests(unittest.TestCase):
    def test_the_auth_body_is_68_bytes_with_a_fixed_key_and_sequence(self):
        packet = p.parse_wire(p.build_auth("defaultuser", "defaultuser"))
        self.assertEqual(len(packet.body), 68)
        self.assertEqual(packet.key, p.AUTH_KEY)
        self.assertEqual(packet.seq, p.AUTH_SEQ)
        self.assertEqual(packet.msg_type, p.MSG_AUTH)
        self.assertEqual(packet.channel, p.CHANNEL_CTRL)
        self.assertEqual(packet.body[:2], b"\x01\x00")
        self.assertEqual(packet.body[2:35].rstrip(b"\x00"), b"defaultuser")
        self.assertEqual(packet.body[35:68].rstrip(b"\x00"), b"defaultuser")

    def test_credentials_are_padded_to_33_and_truncated_at_32(self):
        body = p.parse_wire(p.build_auth("ab", "c" * 40)).body
        self.assertEqual(body[2:35], b"ab" + b"\x00" * 31)
        self.assertEqual(len(body[35:68]), 33)
        self.assertEqual(body[35:68].rstrip(b"\x00"), b"c" * 32)

    def test_the_fixed_key_makes_auth_the_same_shape_every_session(self):
        first = p.build_auth("u", "p")
        second = p.build_auth("u", "p")
        # Sequence and key are fixed, so only the body bytes differ: the
        # ciphertext is deterministic and the two datagrams are identical.
        self.assertEqual(first, second)


class SessionPayloadTests(unittest.TestCase):
    def test_a_cat_request_round_trips_through_the_server_shape(self):
        body = p.cat_request_body(0x24, 0x01, "FA;")
        self.assertEqual(body, bytes((0x24, 0, 0, 0, 0x01, 0, 0, 0)) + b"FA;")
        self.assertEqual(p.cat_ascii(bytes((7, 0, 0, 0)) + b"FA007007000;"),
                         "FA007007000;")

    def test_a_short_server_body_decodes_to_an_empty_command(self):
        self.assertEqual(p.cat_ascii(b"\x01\x00\x00\x00"), "")
        self.assertEqual(p.cat_ascii(b""), "")

    def test_the_setup_bodies_carry_the_session_id_and_the_marks(self):
        self.assertEqual(p.port_init_body(0x28), bytes((0x28, 0, 0, 0)))
        self.assertEqual(p.continuation_body(0x28),
                         bytes((0x28, 0, 0, 0, 0x51, 0xC3, 0x00, 0x96, 0, 0)))
        self.assertEqual(p.cat_setup_body(0x28)[4:], b"\x52\xc3")
        self.assertEqual(p.audio_setup_body(0x28)[4:], b"\x53\xc3")

    def test_channel_names_cover_the_four_ports_and_degrade_for_unknown(self):
        self.assertEqual([p.channel_name(c) for c in p.CHANNELS],
                         ["ctrl", "cat", "audio", "scope"])
        self.assertEqual(p.channel_name(0x10), "0x10")


class SpectrumBodyTests(unittest.TestCase):
    def test_the_marker_is_the_first_low_bin_in_the_tail(self):
        body = realistic_body(marker=1900)
        self.assertEqual(sf.find_spectrum_end(body), 1900)

    def test_the_search_starts_at_500_so_a_strong_signal_cannot_fake_it(self):
        body = realistic_body(marker=1900)
        body[300] = 0
        self.assertEqual(sf.find_spectrum_end(body), 1900)

    def test_the_usable_count_is_half_the_marker(self):
        self.assertEqual(sf.usable_bin_count(realistic_body(1900)), 950)
        self.assertEqual(sf.usable_bin_count(realistic_body(800)), 400)

    def test_bins_are_inverted_so_higher_means_stronger(self):
        body = realistic_body(1000)
        body[0:500] = bytes([0] * 500)          # a strong signal
        bins = sf.signal_bins(body)
        self.assertEqual(len(bins), 500)
        self.assertEqual(bins[0], 255)          # raw 0 = strongest
        self.assertEqual(bins[-1], 0xFF - body[499])

    def test_the_scope_line_is_850_bins_and_keeps_the_peak(self):
        body = realistic_body(1900)
        body[0] = 0
        bins = sf.to_scope_bins(body)
        self.assertEqual(len(bins), sf.SPECTRUM_BINS)
        self.assertEqual(max(bins), 255)
        self.assertEqual(bins[0], 255)

    def test_a_body_without_a_marker_falls_back_to_the_reference_block(self):
        body = bytearray([240] * 4096)
        marker = sf.find_spectrum_end(body)
        self.assertEqual(marker, 400)           # start 500 - margin 100
        self.assertEqual(sf.usable_bin_count(body), 200)

    def test_an_empty_body_yields_no_bins_instead_of_a_flat_line(self):
        self.assertEqual(sf.to_scope_bins(b""), [])
        self.assertEqual(sf.usable_bin_count(b""), 0)


if __name__ == "__main__":
    unittest.main()
