"""Tests for the per-model CI-V profiles (spec 2026-09-12 §4.1/§5).

Every assertion here is hardware-independent: it checks that the
profile data matches the offline evidence (wfview rig data + the
verified IC-7300 tables), not that a radio behaves as documented.
"""
import unittest

from backends.ic7300 import config_ic7300
from backends.ic7300.civ_codec import (
    SCOPE_AMPLITUDE_MAX, SCOPE_MAX_SEGMENTS, SCOPE_WAVEFORM_LEN,
)
from backends.ic7300.civ_controller import (
    SETMODE_CIV_TRANSCEIVE_MK2, SETMODE_CIV_TRANSCEIVE_ON,
)
from backends.ic7300.civ_profiles import PROFILES, get_profile, known_models

ICOM_KEYS = ("ic7300", "ic7300mk2", "ic705", "ic7610", "ic7760")
NEW_KEYS = ("ic705", "ic7610", "ic7760")


class ProfileRegistryTests(unittest.TestCase):
    def test_all_icom_models_registered(self):
        self.assertEqual(known_models(), ICOM_KEYS)
        self.assertEqual(tuple(PROFILES), ICOM_KEYS)

    def test_get_profile_normalizes_key(self):
        # Registry key, human-readable spelling and stray whitespace all
        # resolve to the same profile (the diagnostic CLI takes --model
        # from an operator, not from the registry).
        for raw in ("  IC705 \n", "IC-705", "ic-705", "IC_705"):
            with self.subTest(raw=raw):
                self.assertIs(get_profile(raw), PROFILES["ic705"])

    def test_get_profile_unknown_raises(self):
        with self.assertRaises(ValueError):
            get_profile("ic9999")


class VerifiedProfileFidelityTests(unittest.TestCase):
    """The IC-7300/MK2 profiles must equal today's hardcoded tables."""

    def test_ic7300_address_and_transceive_command(self):
        p = PROFILES["ic7300"]
        self.assertEqual(p.civ_addr, config_ic7300.CIV_ADDR)
        self.assertEqual(p.civ_addr, 0x94)
        self.assertEqual(p.transceive_cmd, SETMODE_CIV_TRANSCEIVE_ON)

    def test_ic7300mk2_address_and_transceive_command(self):
        p = PROFILES["ic7300mk2"]
        self.assertEqual(p.civ_addr, config_ic7300.MK2_CIV_ADDR)
        self.assertEqual(p.civ_addr, 0xB6)
        self.assertEqual(p.transceive_cmd, SETMODE_CIV_TRANSCEIVE_MK2)

    def test_ic7300_tables_equal_legacy_tables(self):
        p = PROFILES["ic7300"]
        self.assertEqual(p.mode_num_to_name, config_ic7300.MODE_NUM_TO_NAME)
        self.assertEqual(p.mode_name_to_num, config_ic7300.MODE_NAME_TO_NUM)
        self.assertEqual(p.scope_spans, config_ic7300.SCOPE_SPANS)
        self.assertEqual(list(p.bands), config_ic7300.BANDS)
        self.assertEqual(p.fil_default_widths_hz,
                         config_ic7300.FIL_DEFAULT_WIDTHS_HZ)
        self.assertEqual(p.preamp_labels, config_ic7300.PREAMP_LABELS)
        self.assertEqual(p.att_steps, (0, 20))

    def test_ic7300_meter_conversions_match_legacy_functions(self):
        cal = PROFILES["ic7300"].meter_cal
        for raw in (0, 10, 60, 120, 200, 241, 255):
            self.assertAlmostEqual(cal.raw_to_dbm(raw),
                                   config_ic7300.raw_to_dbm(raw), places=9)
            self.assertEqual(cal.raw_to_s_unit(raw),
                             config_ic7300.raw_to_s_unit(raw))
            self.assertAlmostEqual(cal.raw_to_power(raw),
                                   config_ic7300.raw_to_power(raw), places=9)
            self.assertAlmostEqual(cal.raw_to_swr(raw),
                                   config_ic7300.raw_to_swr(raw), places=9)
            self.assertAlmostEqual(cal.raw_to_alc_pct(raw),
                                   config_ic7300.raw_to_alc_pct(raw), places=9)
            self.assertAlmostEqual(cal.raw_to_voltage(raw),
                                   config_ic7300.raw_to_voltage(raw), places=9)
            self.assertAlmostEqual(cal.raw_to_current(raw),
                                   config_ic7300.raw_to_current(raw), places=9)

    def test_ic7300_filter_hz_matches_legacy_table(self):
        # Compared against the table itself (not the backend helper), so
        # task 6 can delete the now-redundant _ic7300_filter_hz.
        p = PROFILES["ic7300"]
        for mode in ("USB", "CW-U", "AM", "FM"):
            for fil in (1, 2, 3):
                self.assertEqual(
                    p.filter_hz(mode, fil),
                    config_ic7300.FIL_DEFAULT_WIDTHS_HZ[mode][fil - 1])
        self.assertIsNone(p.filter_hz("USB", 0))
        self.assertIsNone(p.filter_hz("USB", 4))
        self.assertIsNone(p.filter_hz("NO-SUCH-MODE", 1))


class NewModelProfileTests(unittest.TestCase):
    def test_ic705(self):
        p = PROFILES["ic705"]
        self.assertEqual(p.civ_addr, 0xA4)
        self.assertEqual(p.transceive_item, b"\x01\x12")
        self.assertEqual((p.scope_bins, p.scope_amp_max, p.scope_seq_max),
                         (475, 160, 11))
        self.assertEqual(p.att_steps, (0, 20))
        self.assertEqual(p.meter_cal.rated_power_w, 10.0)
        self.assertEqual(p.mode_num_to_name[6], "WFM")
        self.assertEqual(p.mode_num_to_name[17], "DV")
        names = [b["name"] for b in p.bands]
        self.assertIn("2m", names)
        self.assertIn("70cm", names)
        self.assertEqual(
            [b for b in p.bands if b["name"] == "70cm"][0]["start"],
            430_000_000)

    def test_ic7610(self):
        p = PROFILES["ic7610"]
        self.assertEqual(p.civ_addr, 0x98)
        self.assertEqual(p.transceive_item, b"\x01\x12")
        self.assertEqual((p.scope_bins, p.scope_amp_max, p.scope_seq_max),
                         (689, 200, 15))
        self.assertEqual(len(p.att_steps), 16)
        self.assertEqual(p.att_steps[:3], (0, 3, 6))
        self.assertEqual(p.att_steps[-1], 45)
        self.assertEqual(p.meter_cal.rated_power_w, 100.0)
        self.assertTrue(p.dual_rx)
        self.assertTrue(p.mainsub_vfo)
        self.assertEqual(p.mode_num_to_name[12], "PSK-U")
        self.assertEqual(p.mode_num_to_name[13], "PSK-L")

    def test_ic7760(self):
        p = PROFILES["ic7760"]
        self.assertEqual(p.civ_addr, 0xB2)
        self.assertEqual(p.transceive_item, b"\x01\x50")
        self.assertEqual((p.scope_bins, p.scope_amp_max, p.scope_seq_max),
                         (689, 200, 15))
        self.assertEqual(p.meter_cal.rated_power_w, 200.0)

    def test_scope_queue_segments_is_four_waveforms(self):
        for key, p in PROFILES.items():
            with self.subTest(model=key):
                self.assertEqual(p.scope_queue_segments, 4 * p.scope_seq_max)

    def test_new_models_are_unverified_and_carry_no_model_id(self):
        for key in NEW_KEYS:
            with self.subTest(model=key):
                p = PROFILES[key]
                self.assertFalse(p.verified)
                self.assertEqual(p.unverified_meters,
                                 ("power", "voltage", "current"))
                self.assertIsNone(p.model_id_bytes)

    def test_verified_models_have_no_unverified_meters(self):
        for key in ("ic7300", "ic7300mk2"):
            with self.subTest(model=key):
                self.assertTrue(PROFILES[key].verified)
                self.assertEqual(PROFILES[key].unverified_meters, ())

    def test_scope_geometry_defaults_match_module_constants(self):
        p = PROFILES["ic7300"]
        self.assertEqual(p.scope_bins, SCOPE_WAVEFORM_LEN)
        self.assertEqual(p.scope_amp_max, SCOPE_AMPLITUDE_MAX)
        self.assertEqual(p.scope_seq_max, SCOPE_MAX_SEGMENTS)


class ProfileInvariantTests(unittest.TestCase):
    def test_mode_tables_are_bijective(self):
        for key, p in PROFILES.items():
            with self.subTest(model=key):
                self.assertEqual(len(p.mode_num_to_name), len(p.mode_name_to_num))
                for num, name in p.mode_num_to_name.items():
                    self.assertEqual(p.mode_name_to_num[name], num)

    def test_bands_are_ordered_and_non_overlapping(self):
        for key, p in PROFILES.items():
            with self.subTest(model=key):
                last_end = 0
                for band in p.bands:
                    self.assertLess(band["start"], band["end"])
                    self.assertGreaterEqual(band["start"], last_end)
                    self.assertTrue(
                        band["start"] <= band["default_freq"] <= band["end"])
                    last_end = band["end"]

    def test_band_lookup_uses_profile_bands(self):
        p = PROFILES["ic705"]
        self.assertEqual(p.get_band_for_frequency(145_500_000)["name"], "2m")
        self.assertIsNone(p.get_band_for_frequency(100_000_000))

    def test_narrow_modes_intersect_profile_mode_names(self):
        self.assertEqual(PROFILES["ic7300"].narrow_modes(),
                         ["CW-L", "CW-U", "RTTY-L", "RTTY-U"])

    def test_attenuator_labels_from_steps(self):
        self.assertEqual(PROFILES["ic7300"].attenuator_labels(),
                         {0: "OFF", 1: "20dB"})
        labels = PROFILES["ic7760"].attenuator_labels()
        self.assertEqual(labels[0], "OFF")
        self.assertEqual(labels[15], "45dB")
        self.assertEqual(len(labels), 16)

    def test_transceive_command_layout(self):
        # 1a 05 <item> 01 — the item is a per-model set-mode number.
        self.assertEqual(PROFILES["ic7760"].transceive_cmd,
                         bytes((0x1A, 0x05, 0x01, 0x50, 0x01)))

    def test_profiles_are_frozen(self):
        p = PROFILES["ic705"]
        with self.assertRaises(Exception):
            p.civ_addr = 0x00


if __name__ == "__main__":
    unittest.main()
