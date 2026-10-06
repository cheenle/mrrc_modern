"""Tests for the per-model Yaesu profiles (spec 2026-09-12 §5).

Every assertion is hardware-independent: it checks that the profile data is
internally consistent and matches the offline evidence recorded in
`provenance`, not that a radio behaves as documented.
"""
import unittest

from backends.yaesu import yaesu_profiles as yp

# Evidence: Hamlib 4.7.2, ~/hamlib/Hamlib-4.7.2/rigs/yaesu/
#   newcat.c:12043 newcat_mode_conv[]   (family mode characters)
#   ftx1/ftx1_mode.c header comment      (FTX-1 mode characters, E=PSK, H/I=C4FM)
#   ftdx10.c:256 /.filters               (FTDX10 filter widths)
#   ftdx101.c:299 /.filters              (FTDX101 filter widths)
#   newcat.c:339 yaesu_default_str_cal   (FTDX10 S-meter curve, 11 points)
#   ftdx101.h:164 FTDX101D_STR_CAL       (12 points)
#   ftx1/ftx1.h:105 FTX1_STR_CAL         (16 points)
#   ft891.h:88 FT891_STR_CAL             (16 points, marked /* TBC */)
#   newcat.c:10099-10153 / 10162-10208   (FT-891 filter slots, get side)
#   ft891.c:180,181,202-213              (FT-891 preamp/attenuator/ranges)
EXPECTED_KEYS = ("ftdx10", "ftdx101d", "ftdx101mp", "ftx1", "ft891")

# A provenance value must name an offline source file, or be an explicit
# TODO(hw-verify) marker — never a bare claim.
PROVENANCE_PATTERN = r"\.(c|h|md|txt)|manual|FTX-1_CAT|TODO\(hw-verify\)"


class ProfileRegistryTests(unittest.TestCase):
    def test_known_models(self):
        self.assertEqual(yp.known_models(), EXPECTED_KEYS)

    def test_get_profile_round_trip(self):
        for key in EXPECTED_KEYS:
            self.assertIs(yp.get_profile(key), yp.PROFILES[key])
        with self.assertRaises(KeyError):
            yp.get_profile("ft710")      # FT-710 is a separate, verified path


class ProfileInvariantTests(unittest.TestCase):
    def test_all_models_are_unverified_and_tx_gated(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertFalse(p.verified, key)
            self.assertTrue(p.tx_gated, key)

    def test_provenance_is_recorded_for_every_table(self):
        required = {"mode_numbers", "filter_widths", "s_meter_cal",
                    "bands", "power_format", "model_overall"}
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertEqual(required - set(p.provenance), set(), key)
            for table, source in p.provenance.items():
                self.assertTrue(source.strip(), f"{key}/{table}")
                self.assertRegex(source, PROVENANCE_PATTERN, f"{key}/{table}")

    def test_ui_modes_are_defined_in_mode_numbers(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertTrue(p.ui_modes, key)
            for name in p.ui_modes:
                self.assertIn(name, p.mode_numbers, f"{key}: {name}")

    def test_mode_registers_are_unique_and_have_a_cat_code(self):
        """Registers are ints (RadioState contract); H/I are not hex digits."""
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            registers = list(p.mode_numbers.values())
            self.assertEqual(len(registers), len(set(registers)), key)
            for name, num in p.mode_numbers.items():
                self.assertIsInstance(num, int, f"{key}/{name}")
                self.assertIn(num, p.mode_codes, f"{key}/{name}")
            self.assertEqual(len(set(p.mode_codes)), len(p.mode_codes), key)

    def test_filter_widths_are_ordered_and_positive(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertTrue(p.filter_widths, key)
            for mode, widths in p.filter_widths.items():
                self.assertTrue(widths, f"{key}/{mode}")
                indexes = [i for i, _ in widths]
                self.assertEqual(indexes, sorted(indexes), f"{key}/{mode}")
                self.assertEqual(len(indexes), len(set(indexes)), f"{key}/{mode}")
                for _, hz in widths:
                    self.assertGreater(hz, 0, f"{key}/{mode}")

    def test_attenuator_and_preamp_steps(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertEqual(p.att_steps[0], 0, key)          # 0 dB = off
            self.assertEqual(list(p.att_steps), sorted(p.att_steps), key)
            self.assertEqual(len(p.att_steps), len(set(p.att_steps)), key)
            self.assertTrue(p.preamp_labels, key)

    def test_s_meter_curve_is_monotonic_and_reaches_s9(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            points = p.s_meter_cal.points
            raws = [r for r, _ in points]
            dbms = [d for _, d in points]
            self.assertEqual(raws, sorted(raws), key)
            self.assertEqual(dbms, sorted(dbms), key)         # monotonic
            self.assertEqual(points[0][0], 0, key)            # raw 0 = S0
            self.assertEqual(points[-1][0], 255, key)         # full scale
            self.assertEqual(p.s_meter_cal.value(0), points[0][1], key)
            self.assertEqual(p.s_meter_cal.value(255), points[-1][1], key)
            self.assertEqual(p.s_meter_cal.value(999), points[-1][1], key)

    def test_bands_are_ascending_and_inside_the_models_range(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            lows = [low for _, low, _ in p.bands]
            self.assertEqual(lows, sorted(lows), key)
            for label, low, high in p.bands:
                self.assertLess(low, high, f"{key}/{label}")

    def test_power_format_and_limits(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertIn(p.power_format, ("PC1", "PC2", "auto"), key)
            self.assertGreater(p.power_max_w, 0, key)

    def test_audio_hints_are_present(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertGreater(p.audio_rx_rate, 0, key)
            self.assertGreater(p.audio_gain_boost, 0, key)
            if key == "ft891":
                # No USB sound card (design 2026-10-05 D3): the USB port is CAT
                # only, so there is no radio device to auto-match. The generic
                # USB-audio tier in audio_handler still finds an external
                # interface, and the operator can pick a device explicitly.
                self.assertEqual(p.audio_name_hints, (), key)
                self.assertIn("TODO(hw-verify)", p.provenance["audio_rates"], key)
            else:
                self.assertTrue(p.audio_name_hints, key)

    def test_unverified_meters_named_but_not_invented(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertIn("s_meter", p.unverified_meters, key)
            self.assertIn("power", p.unverified_meters, key)


class ModelSpecificDataTests(unittest.TestCase):
    def test_ftdx10_uses_the_documented_family_mode_table(self):
        p = yp.get_profile("ftdx10")
        self.assertEqual(p.mode_numbers["LSB"], 0x1)
        self.assertEqual(p.mode_numbers["USB"], 0x2)
        self.assertEqual(p.mode_numbers["CW-U"], 0x3)
        self.assertEqual(p.mode_numbers["DATA-U"], 0xC)
        self.assertEqual(p.mode_numbers["AM-N"], 0xD)
        self.assertEqual(p.mode_numbers["C4FM"], 0xE)
        self.assertEqual(p.mode_codes[0xE], "E")

    def test_ftx1_mode_codes_are_not_hex_formatted(self):
        """ftx1/ftx1_mode.c: E=PSK, H=C4FM-DN, I=C4FM-VW.

        H and I are not hex digits, so the CAT layer must look the character
        up instead of formatting the register as hex — `f"{0x11:X}"` would
        send `MD011`, which is not a mode at all.
        """
        p = yp.get_profile("ftx1")
        self.assertEqual(p.mode_numbers["PSK"], 0xE)
        self.assertEqual(p.mode_numbers["C4FM-DN"], 0x10)
        self.assertEqual(p.mode_numbers["C4FM-VW"], 0x11)
        self.assertEqual(p.mode_codes[0xE], "E")
        self.assertEqual(p.mode_codes[0x10], "H")
        self.assertEqual(p.mode_codes[0x11], "I")
        self.assertNotIn("C4FM", p.mode_numbers)

    def test_ftdx10_filter_table_matches_hamlib(self):
        p = yp.get_profile("ftdx10")
        self.assertEqual([hz for _, hz in p.filter_widths["CW-U"]],
                         [2400, 600, 300, 1200])
        self.assertEqual([hz for _, hz in p.filter_widths["USB"]],
                         [3000, 2400, 1800])

    def test_ftdx101mp_has_greater_power_than_ftdx101d(self):
        self.assertEqual(yp.get_profile("ftdx101d").power_max_w, 100)
        self.assertEqual(yp.get_profile("ftdx101mp").power_max_w, 200)

    def test_ftx1_is_the_only_multi_band_and_auto_power_model(self):
        self.assertEqual(yp.get_profile("ftx1").power_format, "auto")
        for key in ("ftdx10", "ftdx101d", "ftdx101mp", "ft891"):
            self.assertEqual(yp.get_profile(key).power_format, "PC1")
        labels = [b[0] for b in yp.get_profile("ftx1").bands]
        self.assertIn("2m", labels)
        self.assertIn("70cm", labels)

    def test_only_explicitly_configured_models_declare_an_id(self):
        self.assertEqual(yp.get_profile("ftx1").id_answer, "0840")
        for key in ("ftdx10", "ftdx101d", "ftdx101mp", "ft891"):
            # Unknown until real hardware answers (spec §3, §10). The FT-891's is
            # derivable (NC_RIGID_FT891=135) but deliberately not recorded as an
            # expectation (design 2026-10-05 D6).
            self.assertEqual(yp.get_profile(key).id_answer, "")

    def test_dual_rx_is_recorded_but_not_implemented(self):
        self.assertTrue(yp.get_profile("ftdx101d").dual_rx)
        self.assertTrue(yp.get_profile("ftdx101mp").dual_rx)
        self.assertTrue(yp.get_profile("ftx1").dual_rx)
        self.assertFalse(yp.get_profile("ftdx10").dual_rx)
        self.assertFalse(yp.get_profile("ft891").dual_rx)

    def test_ft891_filter_tables_match_hamlib(self):
        """Transcribed from the Hamlib get side, slot by slot.

        newcat.c:10099-10153 (CW/RTTY/PKT, slots 1-17) and 10162-10208 (SSB,
        slots 1-21). Slot 21 is 3200 Hz on the get side even though the set side
        comments it as 3000 (newcat.c:8919-8920) — the get side is authoritative
        for "what does this slot mean".
        """
        p = yp.get_profile("ft891")
        narrow_expected = [50, 100, 150, 200, 250, 300, 350, 400, 450, 500,
                           800, 1200, 1400, 1700, 2000, 2400, 3000]
        voice_expected = [200, 400, 600, 850, 1100, 1350, 1500, 1650, 1800,
                          1950, 2100, 2200, 2300, 2400, 2500, 2600, 2700,
                          2800, 2900, 3000, 3200]
        for mode in ("CW-U", "CW-L", "RTTY-U", "RTTY-L", "DATA-U", "DATA-L"):
            self.assertEqual([i for i, _ in p.filter_widths[mode]],
                             list(range(1, 18)), mode)
            self.assertEqual([hz for _, hz in p.filter_widths[mode]],
                             narrow_expected, mode)
        for mode in ("SSB", "USB", "LSB"):
            self.assertEqual([i for i, _ in p.filter_widths[mode]],
                             list(range(1, 22)), mode)
            self.assertEqual([hz for _, hz in p.filter_widths[mode]],
                             voice_expected, mode)

    def test_ft891_omits_am_fm_and_slot_zero(self):
        """AM/FM width is set through NA only, so no SH slot exists (design D5).

        newcat.c:8924-8936 sets AM/FM/PKTFM via newcat_set_narrow and never
        writes an index; 10217-10228 returns fixed widths (AM 9000, AM-N 6000,
        FM 16000). Slot 0 is narrow-dependent and this UI never sends it.
        Inventing either would be data without a source.
        """
        p = yp.get_profile("ft891")
        for mode in ("AM", "FM", "FM-N", "AM-N"):
            self.assertNotIn(mode, p.filter_widths, mode)
        for mode, widths in p.filter_widths.items():
            self.assertNotIn(0, [i for i, _ in widths], mode)

    def test_ft891_mode_set_excludes_the_modes_it_lacks(self):
        """ft891.h:35-36 FT891_ALL_RX_MODES has no C4FM, no PKT-FM, no AM-N."""
        p = yp.get_profile("ft891")
        for absent in ("C4FM", "DATA-FM", "AM-N", "DATA-FM-N"):
            self.assertNotIn(absent, p.mode_numbers, absent)
        for present in ("LSB", "USB", "CW-U", "CW-L", "AM", "FM", "FM-N",
                        "RTTY-U", "RTTY-L", "DATA-U", "DATA-L"):
            self.assertIn(present, p.mode_numbers, present)
        # The family registers are reused unchanged wherever the mode exists.
        family = yp.get_profile("ftdx10").mode_numbers
        for name, num in p.mode_numbers.items():
            self.assertEqual(family[name], num, name)

    def test_ft891_static_facts(self):
        """The offline-derived capability facts, each pinned to its source."""
        p = yp.get_profile("ft891")
        self.assertEqual(p.display_name, "Yaesu FT-891")
        self.assertEqual(p.default_baud, 38400)
        self.assertEqual(p.att_steps, (0, 12))                    # ft891.c:181
        self.assertEqual(p.preamp_labels, {0: "OFF", 1: "AMP1"})  # ft891.c:180
        self.assertEqual(p.power_format, "PC1")
        self.assertEqual(p.power_max_w, 100)                      # ft891.c:207-213
        self.assertFalse(p.has_atu)                               # no internal ATU
        self.assertEqual(p.tune_via, "tx2")
        self.assertFalse(p.has_vd_id_meters)                      # ID_METER, no VD
        self.assertTrue(p.vfo_b_direct)
        self.assertEqual(p.filter_width_prefix, "SH01")            # newcat.c:9661
        self.assertEqual(p.id_answer, "")                         # design D6
        self.assertFalse(p.dual_rx)
        self.assertFalse(p.verified)
        self.assertTrue(p.tx_gated)
        # No USB sound card (D3): nothing to auto-match, external interface 48k.
        self.assertEqual(p.audio_name_hints, ())
        self.assertEqual(p.audio_rx_rate, 48000)
        self.assertEqual(p.audio_tx_rate, 48000)
        # Bands: HF + 6 m only, no 2 m / 70 cm (ft891.c:202-213).
        labels = [b[0] for b in p.bands]
        self.assertEqual(labels, [b[0] for b in yp.get_profile("ftdx10").bands])
        self.assertNotIn("2m", labels)
        # The S curve is Hamlib's FT891_STR_CAL, point-identical to the FTX-1's.
        self.assertEqual(p.s_meter_cal.points,
                         yp.get_profile("ftx1").s_meter_cal.points)
        self.assertEqual(p.s_meter_cal.value(130), 0.0)            # S9 at raw 130


class MeterCalTests(unittest.TestCase):
    def test_interpolation_between_points(self):
        cal = yp.MeterCal(points=((0, -60.0), (100, 0.0), (255, 60.0)))
        self.assertAlmostEqual(cal.value(50), -30.0, places=3)
        self.assertAlmostEqual(cal.value(100), 0.0, places=3)

    def test_clamping(self):
        cal = yp.MeterCal(points=((0, -60.0), (255, 60.0)))
        self.assertEqual(cal.value(-5), -60.0)
        self.assertEqual(cal.value(300), 60.0)

    def test_s_unit_mapping(self):
        cal = yp.MeterCal(points=((0, -54.0), (130, 0.0), (255, 60.0)))
        # S9 = 0 dBm by the family convention (-54 dBm = S0).
        self.assertEqual(cal.s_unit(130), "S9+0")
        self.assertTrue(cal.s_unit(200).startswith("S9+"))
        self.assertTrue(cal.s_unit(60).startswith("S"))


if __name__ == "__main__":
    unittest.main()
