"""SmartSDR text codec tests (P1, design §4.2).

Each case comes from a wire line that a naive parser gets wrong: multi-word
object names, the ``#``-separated ``meter`` object, the comma-separated
``info`` reply with quoted values, and the discovery string's underscore-escaped
radio name.
"""

import unittest

from backends.flex import protocol


class CommandTests(unittest.TestCase):
    def test_command_is_ascii_with_a_line_feed(self):
        self.assertEqual(protocol.build_command(7, "slice list"),
                         b"C7|slice list\n")

    def test_empty_command_is_refused(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.build_command(1, "   ")

    def test_embedded_newline_is_refused(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.build_command(1, "xmit 1\nxmit 0")

    def test_bad_sequence_is_refused(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.build_command("nope", "xmit 1")  # type: ignore[arg-type]


class ResponseTests(unittest.TestCase):
    def test_success_with_empty_body(self):
        r = protocol.parse_response("R41|0|")
        self.assertTrue(r.ok)
        self.assertEqual((r.seq, r.body, r.debug), (41, "", ""))

    def test_slice_create_body_carries_the_slice_number(self):
        r = protocol.parse_response("R21|0|2|OK Slice receiver 2 created")
        self.assertEqual(r.body, "2")

    def test_error_code_and_debug_are_kept(self):
        r = protocol.parse_response("R19|50000016|Malformed|debug text|more")
        self.assertFalse(r.ok)
        self.assertEqual(r.code, protocol.ERROR_UNKNOWN_COMMAND)
        self.assertEqual(r.debug, "debug text|more")

    def test_non_hex_code_is_a_parse_error(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_response("R19|zz|")

    def test_missing_code_field_is_a_parse_error(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_response("R19")

    def test_non_response_line_is_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_response("S1|slice 0 mode=USB")


class StatusTests(unittest.TestCase):
    def test_multi_word_object_name(self):
        st = protocol.parse_status(
            "S1A2B3C4D|slice 0 RF_frequency=14.074000 mode=USB "
            "audio_level=35 client_handle=0x1A2B3C4D")
        self.assertEqual(st.obj, "slice 0")
        self.assertEqual(st.handle, 0x1A2B3C4D)
        self.assertEqual(st.get("RF_frequency"), "14.074000")
        self.assertEqual(st.get("mode"), "USB")
        self.assertEqual(st.get("audio_level"), "35")
        freq = st.number("RF_frequency")
        assert freq is not None
        self.assertAlmostEqual(freq, 14.074, places=6)

    def test_pan_object_keeps_its_hex_id(self):
        st = protocol.parse_status(
            "S0|display pan 0x40000000 bandwidth=0.100000 min_dbm=-120.0 "
            "max_dbm=0.0 ypixels=256")
        self.assertEqual(st.obj, "display pan 0x40000000")
        self.assertEqual(st.get("ypixels"), "256")

    def test_interlock_object_has_three_words(self):
        st = protocol.parse_status("S0|interlock band 9 state=READY reason=")
        self.assertEqual(st.obj, "interlock band 9")
        self.assertEqual(st.get("state"), "READY")

    def test_quoted_values_are_unquoted(self):
        st = protocol.parse_status('S0|radio model="FLEX-6400" serial="1234-5678"')
        self.assertEqual(st.get("model"), "FLEX-6400")

    def test_status_without_body_is_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_status("S0")


class SplitObjectTests(unittest.TestCase):
    def test_no_equals_returns_the_whole_object(self):
        self.assertEqual(protocol.split_object("client connected"),
                         ("client connected", ""))

    def test_boundary_is_the_last_space_before_the_first_equals(self):
        obj, kv = protocol.split_object("display panafall 0x40000000 waterfall=0x42000000")
        self.assertEqual(obj, "display panafall 0x40000000")
        self.assertEqual(kv, "waterfall=0x42000000")


class PrologueTests(unittest.TestCase):
    def test_version_and_handle_lines(self):
        self.assertEqual(protocol.parse_hello("V1.4.0.0"), ("version", "1.4.0.0"))
        self.assertEqual(protocol.parse_hello("H6F4EC23D"), ("handle", "6F4EC23D"))

    def test_anything_else_is_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_hello("R1|0|")


class MeterTests(unittest.TestCase):
    LINE = ("S0|meter 7.src=SLC#7.num=0#7.nam=LEVEL#7.unit=dBm#7.low=-150.0"
            "#7.hi=20.0#7.fps=10#13.src=RAD#13.num=208#13.nam=+13.8A"
            "#13.unit=Volts#13.low=10.5#13.hi=15.0#13.fps=0")

    def test_status_keeps_the_hash_body_raw(self):
        st = protocol.parse_status(self.LINE)
        self.assertEqual(st.obj, "meter")
        self.assertIn("7.src=SLC", protocol.meter_text(st))

    def test_definitions_are_parsed_per_id(self):
        defs = protocol.parse_meter_definitions(protocol.meter_text(
            protocol.parse_status(self.LINE)))
        self.assertEqual(set(defs), {7, 13})
        self.assertEqual(defs[7].src, "SLC")
        self.assertEqual(defs[7].name, "LEVEL")
        self.assertEqual(defs[7].unit, "dBm")
        self.assertEqual(defs[7].fps, 10)
        self.assertEqual(defs[13].name, "+13.8A")

    def test_junk_tokens_are_skipped_not_fatal(self):
        defs = protocol.parse_meter_definitions("x.y=1##7.nam=LEVEL#8=bogus")
        self.assertEqual(set(defs), {7})
        self.assertEqual(defs[7].name, "LEVEL")

    def test_unit_scaling_matches_the_documented_divisors(self):
        self.assertAlmostEqual(protocol.scale_meter(128, "dBm"), 1.0)
        self.assertAlmostEqual(protocol.scale_meter(1280, "Volts"), 1.25)
        self.assertAlmostEqual(protocol.scale_meter(64, "degC"), 1.0)
        # Unknown unit: pass through, do not invent a scale.
        self.assertAlmostEqual(protocol.scale_meter(42, "furlongs"), 42.0)


class InfoTests(unittest.TestCase):
    def test_comma_separated_pairs_with_quotes(self):
        info = protocol.parse_info(
            'model=FLEX-6400,chassis_serial="1234-5678",software_ver=4.1.5.39511,'
            'callsign=BG7XXX,name="My_Radio",region=2')
        self.assertEqual(info["model"], "FLEX-6400")
        self.assertEqual(info["chassis_serial"], "1234-5678")
        self.assertEqual(info["software_ver"], "4.1.5.39511")
        self.assertEqual(info["region"], "2")

    def test_unknown_keys_survive(self):
        """Firmware adds fields; a settings page must not need a parser change."""
        self.assertEqual(protocol.parse_info("future_field=1")["future_field"], "1")

    def test_backslash_escapes_are_removed(self):
        self.assertEqual(protocol.parse_info('name="A\\B"')["name"], "AB")


class SliceListTests(unittest.TestCase):
    def test_list_of_ids(self):
        self.assertEqual(protocol.parse_slice_list("0 1 3 4"), [0, 1, 3, 4])

    def test_empty_answer_is_an_empty_list(self):
        self.assertEqual(protocol.parse_slice_list(""), [])

    def test_junk_tokens_are_skipped(self):
        self.assertEqual(protocol.parse_slice_list("0 x 2"), [0, 2])


class DiscoveryPayloadTests(unittest.TestCase):
    def test_status_style_string_with_underscored_name(self):
        payload = protocol.parse_discovery_payload(
            "model=FLEX-6400 serial=1234-5678 version=4.1.5 "
            "name=Shack_FLEX callsign=BG7XXX ip=192.168.1.10 port=4992")
        self.assertEqual(payload["model"], "FLEX-6400")
        self.assertEqual(payload["name"], "Shack FLEX")
        self.assertEqual(payload["ip"], "192.168.1.10")
        self.assertEqual(payload["port"], "4992")

    def test_nul_padding_is_tolerated(self):
        payload = protocol.parse_discovery_payload("model=FLEX-6600\x00\x00")
        self.assertEqual(payload["model"], "FLEX-6600")


class ErrorNameTests(unittest.TestCase):
    def test_known_codes_are_named(self):
        self.assertEqual(protocol.error_name(protocol.ERROR_OK), "ok")
        self.assertIn("udpport", protocol.error_name(protocol.ERROR_UDPPORT_QUIRK))

    def test_unknown_codes_print_as_hex(self):
        self.assertEqual(protocol.error_name(0x1234), "0x00001234")


if __name__ == "__main__":
    unittest.main()
