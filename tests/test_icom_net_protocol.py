"""Icom network-radio protocol: byte-level codecs (no sockets, no radio).

Every assertion here is a **field offset or an endianness**, which is what this
module exists to get right: a packet with one field in the wrong byte order is
accepted by nobody and rejected by nothing — the radio simply ignores it, which
is exactly how the G7250's waterfall stayed synthetic for three days while the
log said "listening".  The layouts follow wfview's public implementation (the
protocol facts, not its code — see the design record).
"""
import struct
import unittest

from backends.icom_net import protocol as p


class PasscodeTests(unittest.TestCase):
    def test_the_substitution_matches_a_hand_computed_vector(self):
        # 'G' = 0x47, index 0 -> the table's entry at 0x47 is 0x54; '7' = 0x37,
        # index 1 -> 0x38 -> 0x7D; '2' = 0x32, index 2 -> 0x34 -> 0x68.
        self.assertEqual(p.passcode("G7250").hex(), "547d687d68")

    def test_it_is_truncated_to_the_field_width(self):
        self.assertEqual(len(p.passcode("a" * 40)), 16)

    def test_high_ascii_wraps_into_the_printable_range(self):
        # index + char > 126 wraps to 32 + p % 127 — a table lookup, never a
        # crash and never a zero byte in the middle of a credential.
        out = p.passcode("~" * 8)
        self.assertEqual(len(out), 8)
        self.assertTrue(all(byte != 0 for byte in out))

    def test_empty_is_empty(self):
        self.assertEqual(p.passcode(""), b"")


class ControlTests(unittest.TestCase):
    def test_control_packet_layout(self):
        raw = p.build_control(p.T_ARE_YOU_THERE, seq=0, sent_id=0x11223344,
                              rcvd_id=0x55667788)
        self.assertEqual(len(raw), p.CONTROL_SIZE)
        length, ptype, seq, sent, rcvd = struct.unpack("<IHHII", raw)
        self.assertEqual((length, ptype, seq), (p.CONTROL_SIZE, 0x03, 0))
        self.assertEqual((sent, rcvd), (0x11223344, 0x55667788))
        # little-endian on the wire, byte for byte
        self.assertEqual(raw[8:12], b"\x44\x33\x22\x11")

    def test_ping_layout(self):
        raw = p.build_ping(seq=7, sent_id=1, rcvd_id=2, time_ms=0x1234)
        self.assertEqual(len(raw), p.PING_SIZE)
        self.assertEqual(raw[4:6], struct.pack("<H", p.T_PING))
        self.assertEqual(raw[0x11:0x15], struct.pack("<I", 0x1234))
        reply = p.parse_ping(raw[:0x10] + b"\x01" + raw[0x11:])
        assert reply is not None
        self.assertEqual(reply.reply, 1)
        self.assertEqual(reply.uptime_ms, 0x1234)


class LoginTests(unittest.TestCase):
    def _login(self):
        return p.build_login(sent_id=0xAABBCCDD, rcvd_id=0x01020304,
                             inner_seq=0x0031, tok_request=0x4242,
                             token=0, username="G7250", password="beta_tester",
                             client_name="mrrc")

    def test_layout(self):
        raw = self._login()
        self.assertEqual(len(raw), p.LOGIN_SIZE)
        self.assertEqual(raw[0x10:0x14], struct.pack(">I", p.LOGIN_SIZE - 0x10))
        self.assertEqual(raw[0x14], 0x01)
        self.assertEqual(raw[0x15], p.REQ_LOGIN)
        self.assertEqual(raw[0x16:0x18], struct.pack(">H", 0x0031))
        self.assertEqual(raw[0x1A:0x1C], struct.pack("<H", 0x4242))
        self.assertEqual(raw[0x40:0x45], p.passcode("G7250"))
        self.assertEqual(raw[0x50:0x50 + 11], p.passcode("beta_tester"))
        self.assertEqual(raw[0x60:0x64], b"mrrc")
        # the fixed-width string fields are NUL padded, not garbage
        self.assertEqual(raw[0x45:0x50], b"\x00" * 11)

    def test_reply_parsing_and_the_bad_credentials_marker(self):
        raw = bytearray(p.LOGIN_RESPONSE_SIZE)
        struct.pack_into("<IHHII", raw, 0, p.LOGIN_RESPONSE_SIZE, 0x01, 0,
                         0x0A0B0C0D, 0xAABBCCDD)
        struct.pack_into("<H", raw, 0x1A, 0x4242)
        struct.pack_into("<I", raw, 0x1C, 0xDEADBEEF)
        struct.pack_into("<i", raw, 0x30, -0x01000001)       # 0xfeffffff
        raw[0x40:0x4C] = b"WFVIEW\x00\x00\x00\x00\x00\x00"
        reply = p.parse_login_reply(bytes(raw))
        assert reply is not None
        self.assertEqual(reply.sent_id, 0x0A0B0C0D)
        self.assertEqual(reply.rcvd_id, 0xAABBCCDD)
        self.assertEqual(reply.tok_request, 0x4242)
        self.assertEqual(reply.token, 0xDEADBEEF)
        self.assertTrue(reply.bad_credentials)
        self.assertEqual(reply.connection_type, "WFVIEW")


class TokenTests(unittest.TestCase):
    def test_request_and_reply(self):
        raw = p.build_token(sent_id=1, rcvd_id=2, inner_seq=0x32,
                            tok_request=0x4242, token=0xCAFEBABE,
                            request_type=p.REQ_TOKEN_RENEW)
        self.assertEqual(len(raw), p.TOKEN_SIZE)
        self.assertEqual(raw[0x15], 0x05)
        self.assertEqual(raw[0x1C:0x20], struct.pack("<I", 0xCAFEBABE))
        self.assertEqual(raw[0x24:0x26], struct.pack(">H", 0x0798))

        reply = bytearray(p.TOKEN_SIZE)
        struct.pack_into("<IHHII", reply, 0, p.TOKEN_SIZE, 0x01, 0, 9, 1)
        reply[0x14] = 0x02
        reply[0x15] = 0x05
        struct.pack_into("<i", reply, 0x30, 0)
        parsed = p.parse_token_reply(bytes(reply))
        assert parsed is not None
        self.assertEqual(parsed.request_type, 0x05)
        self.assertEqual(parsed.reply, 0x02)
        self.assertTrue(parsed.accepted)
        self.assertFalse(parsed.rejected)


class CapabilityTests(unittest.TestCase):
    @staticmethod
    def _packet(name="IC-7300", civ=0xA0, rx=16000, tx=16000, count=1):
        body = bytearray(p.CAPABILITIES_HEADER_SIZE + count * p.RADIO_CAP_SIZE)
        struct.pack_into("<IHHII", body, 0, len(body), 0x01, 0, 5, 6)
        struct.pack_into(">H", body, 0x40, count)
        for i in range(count):
            off = p.CAPABILITIES_HEADER_SIZE + i * p.RADIO_CAP_SIZE
            body[off + 0x07:off + 0x09] = struct.pack(">H", 0x8010)
            body[off + 0x0A:off + 0x10] = bytes.fromhex("546c50ab521d")
            body[off + 0x10:off + 0x10 + len(name)] = name.encode()
            body[off + 0x30:off + 0x30 + 5] = b"VAUDI"
            body[off + 0x50:off + 0x52] = struct.pack(">H", 0x0009)
            body[off + 0x52] = civ
            body[off + 0x53:off + 0x55] = struct.pack(">H", rx)
            body[off + 0x55:off + 0x57] = struct.pack(">H", tx)
            body[off + 0x5A:off + 0x5E] = struct.pack(">I", 115200)
            body[off + 0x5E:off + 0x60] = struct.pack(">H", 0x5001)
        return bytes(body)

    def test_single_radio(self):
        caps = p.parse_capabilities(self._packet())
        self.assertEqual(len(caps), 1)
        radio = caps[0]
        self.assertEqual(radio.name, "IC-7300")
        self.assertEqual(radio.audio, "VAUDI")
        self.assertEqual(radio.civ_address, 0xA0)
        self.assertEqual(radio.rx_sample_rate, 16000)
        self.assertEqual(radio.tx_sample_rate, 16000)
        self.assertEqual(radio.baud_rate, 115200)
        self.assertEqual(radio.capf, 0x5001)

    def test_two_radios_and_a_truncated_packet(self):
        self.assertEqual(len(p.parse_capabilities(self._packet(count=2))), 2)
        self.assertEqual(p.parse_capabilities(b"\x00" * 10), [])


class StreamRequestTests(unittest.TestCase):
    def test_layout_announces_our_ports_big_endian(self):
        raw = p.build_stream_request(
            sent_id=1, rcvd_id=2, inner_seq=0x33, tok_request=0x4242,
            token=0xCAFEBABE, client_name="mrrc", username="G7250",
            rx_codec=p.CODEC_PCM16, rx_sample=16000, civ_port=50111,
            audio_port=50112, mac_address=bytes.fromhex("001122334455"))
        self.assertEqual(len(raw), p.CONNINFO_SIZE)
        self.assertEqual(raw[0x15], p.REQ_STREAM)
        self.assertEqual(raw[0x2A:0x30], bytes.fromhex("001122334455"))
        self.assertEqual(raw[0x70], 0x01)              # rxenable
        self.assertEqual(raw[0x71], 0x00)              # tx disabled for now
        self.assertEqual(raw[0x72], p.CODEC_PCM16)
        self.assertEqual(raw[0x74:0x78], struct.pack(">I", 16000))
        self.assertEqual(raw[0x7C:0x80], struct.pack(">I", 50111))
        self.assertEqual(raw[0x80:0x84], struct.pack(">I", 50112))
        self.assertEqual(raw[0x88], 0x01)              # convert
        self.assertEqual(raw[0x60:0x65], p.passcode("G7250"))

    def test_status_reply_gives_the_radio_side_ports(self):
        raw = bytearray(p.STATUS_SIZE)
        struct.pack_into("<IHHII", raw, 0, p.STATUS_SIZE, 0x01, 0, 9, 1)
        struct.pack_into("<i", raw, 0x30, 0)
        raw[0x42:0x46] = struct.pack(">I", 50002)
        raw[0x46:0x4A] = struct.pack(">I", 50003)
        st = p.parse_stream_status(bytes(raw))
        assert st is not None
        self.assertTrue(st.ok)
        self.assertEqual((st.civ_port, st.audio_port), (50002, 50003))

    def test_conninfo_busy_and_owner(self):
        raw = bytearray(p.CONNINFO_SIZE)
        struct.pack_into("<IHHII", raw, 0, p.CONNINFO_SIZE, 0x01, 0, 9, 1)
        struct.pack_into("<I", raw, 0x60, 1)
        raw[0x64:0x6A] = b"LAPTOP"
        struct.pack_into("<I", raw, 0x84, 0xC0A86E32)   # 192.168.110.50, LE
        info = p.parse_conninfo(bytes(raw))
        assert info is not None
        self.assertEqual(info.busy, 1)
        self.assertEqual(info.computer, "LAPTOP")
        self.assertEqual(info.ip_address, "50.110.168.192")  # as the struct stores it


class AudioTests(unittest.TestCase):
    @staticmethod
    def _packet(pcm: bytes, *, ptype=0x00, ident=0x0000, send_seq=1):
        buf = bytearray(p.AUDIO_HEADER_SIZE + len(pcm))
        struct.pack_into("<IHHII", buf, 0, len(buf), ptype, 0, 7, 8)
        struct.pack_into("<H", buf, 0x10, ident)
        struct.pack_into("<H", buf, 0x12, send_seq)
        struct.pack_into(">H", buf, 0x16, len(pcm))
        buf[p.AUDIO_HEADER_SIZE:] = pcm
        return bytes(buf)

    def test_payload_starts_at_0x18(self):
        pcm = struct.pack("<8h", 1, -2, 3, -4, 5, -6, 7, -8)
        pkt = p.parse_audio(self._packet(pcm))
        assert pkt is not None
        self.assertEqual(pkt.payload, pcm)
        self.assertEqual(pkt.declared_len, len(pcm))
        self.assertEqual(pkt.send_seq, 1)
        self.assertEqual(pkt.missing_tail, 0)

    def test_a_control_packet_is_not_audio(self):
        self.assertIsNone(p.parse_audio(self._packet(b"\x00" * 32, ptype=0x01)))

    def test_short_datagram_is_ignored(self):
        self.assertIsNone(p.parse_audio(b"\x00" * 8))

    def test_short_payload_is_counted_not_raised(self):
        pkt = p.parse_audio(self._packet(b"\x01\x02"))
        assert pkt is not None
        self.assertEqual(pkt.missing_tail, 0)   # declared matches what arrived
        # A header promising more than it delivered is reported, not fatal.
        raw = bytearray(self._packet(b"\x01\x02"))
        struct.pack_into(">H", raw, 0x16, 960)
        short = p.parse_audio(bytes(raw))
        assert short is not None
        self.assertEqual(short.missing_tail, 958)


if __name__ == "__main__":
    unittest.main()
