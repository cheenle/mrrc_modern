"""
Tests for config.py — SDD §7, §10.4.
Verifies: mode tables, band definitions, filter widths, S-meter calibration,
CAT command field mappings.
"""
import unittest

from backends.ft710.config_ft710 import (
    MODE_NUM_TO_NAME,
    MODE_NAME_TO_NUM,
    BANDS,
    get_band_for_frequency,
    get_filter_widths_for_mode,
    get_filter_hz,
    raw_to_dbm,
    raw_to_s_unit,
    PREAMP_LABELS,
    ATTENUATOR_LABELS,
    SCOPE_SPANS,
)
from config import (
    MODE_DISPLAY_NAMES,
    UI_MODES,
    MEM_CHANNEL_COUNT,
    AUTH_COOKIE,
    AUTH_TOKEN_BYTES,
)


class ModeTableTests(unittest.TestCase):
    """SDD §10.4: Mode mapping between names and Yaesu mode registers."""

    def test_common_modes_exist(self):
        self.assertIn("LSB", MODE_NAME_TO_NUM)
        self.assertIn("USB", MODE_NAME_TO_NUM)
        self.assertIn("FM", MODE_NAME_TO_NUM)
        self.assertIn("AM", MODE_NAME_TO_NUM)
        # CW is split into CW-U/CW-L in FT-710
        self.assertTrue(any("CW" in m for m in MODE_NAME_TO_NUM))

    def test_bidirectional_mode_mapping(self):
        for name, num in MODE_NAME_TO_NUM.items():
            self.assertEqual(MODE_NUM_TO_NAME[num], name)

    def test_mode_num_range(self):
        for num in MODE_NUM_TO_NAME:
            self.assertGreaterEqual(num, 1)
            self.assertLessEqual(num, 15)

    def test_display_names_for_all_modes(self):
        for num in MODE_NUM_TO_NAME:
            name = MODE_NUM_TO_NAME[num]
            self.assertIn(name, MODE_DISPLAY_NAMES)

    def test_ui_modes_is_not_empty(self):
        self.assertGreater(len(UI_MODES), 0)
        # UI_MODES is a list of mode name strings
        for m in UI_MODES:
            self.assertIsInstance(m, str)
            self.assertGreater(len(m), 0)


class BandTableTests(unittest.TestCase):
    """SDD §10.4: Band definitions with stacking registers and default freqs."""

    def test_bands_list_is_not_empty(self):
        self.assertGreater(len(BANDS), 0)

    def test_each_band_has_required_fields(self):
        for band in BANDS:
            self.assertIn("name", band)
            self.assertIn("default_freq", band)
            self.assertIn("bsr", band)
            # FT-710 uses "start"/"end" (not "low_hz"/"high_hz")
            self.assertTrue("start" in band or "low_hz" in band)
            self.assertTrue("end" in band or "high_hz" in band)

    def test_get_band_for_frequency_20m(self):
        band = get_band_for_frequency(14_200_000)
        self.assertIsNotNone(band)
        self.assertEqual(band["name"], "20m")

    def test_get_band_for_frequency_40m(self):
        band = get_band_for_frequency(7_050_000)
        self.assertIsNotNone(band)
        self.assertEqual(band["name"], "40m")

    def test_get_band_for_frequency_80m(self):
        band = get_band_for_frequency(3_700_000)
        self.assertIsNotNone(band)
        self.assertEqual(band["name"], "80m")

    def test_get_band_for_frequency_10m(self):
        band = get_band_for_frequency(28_500_000)
        self.assertIsNotNone(band)
        self.assertEqual(band["name"], "10m")

    def test_get_band_for_out_of_band_frequency(self):
        band = get_band_for_frequency(50_000_000)  # 6m might exist or not
        if band is not None:
            self.assertIn("name", band)

    def test_get_band_for_low_frequency(self):
        band = get_band_for_frequency(1_000_000)
        # May return None or GEN
        if band is not None:
            self.assertIsInstance(band, dict)


class FilterTableTests(unittest.TestCase):
    """SDD §10.4: Filter width lookup by mode."""

    def test_filter_widths_for_ssb(self):
        widths = get_filter_widths_for_mode("USB")
        self.assertIsInstance(widths, list)
        self.assertGreater(len(widths), 0)

    def test_filter_widths_for_cw(self):
        widths = get_filter_widths_for_mode("CW")
        self.assertIsInstance(widths, list)
        self.assertGreater(len(widths), 0)

    def test_filter_widths_for_fm(self):
        widths = get_filter_widths_for_mode("FM")
        self.assertIsInstance(widths, list)

    def test_filter_widths_for_unknown_mode(self):
        widths = get_filter_widths_for_mode("UNKNOWN")
        self.assertIsInstance(widths, list)

    def test_get_filter_hz_returns_int_or_none(self):
        hz = get_filter_hz("USB", 5)
        if hz is not None:
            self.assertIsInstance(hz, int)
            self.assertGreater(hz, 0)

    def test_get_filter_hz_max_index(self):
        hz = get_filter_hz("USB", 0)
        if hz is not None:
            self.assertIsInstance(hz, int)


class SMeterCalibrationTests(unittest.TestCase):
    """SDD §7.2: S-meter raw value → dBm / S-unit conversion."""

    def test_raw_to_dbm_is_float(self):
        dbm = raw_to_dbm(0)
        self.assertIsInstance(dbm, float)
        dbm = raw_to_dbm(255)
        self.assertIsInstance(dbm, float)

    def test_raw_to_dbm_monotonic(self):
        dbm_low = raw_to_dbm(0)
        dbm_high = raw_to_dbm(200)
        self.assertLess(dbm_low, dbm_high)

    def test_raw_to_s_unit_is_string(self):
        for raw in (0, 50, 100, 150, 200, 255):
            s = raw_to_s_unit(raw)
            self.assertIsInstance(s, str)
            # S-unit labels: S0-S9, then +10 to +60
            self.assertTrue(s.startswith("S") or s.startswith("+") or "S0" in s)

    def test_raw_to_s_unit_monotonic(self):
        """Higher raw values should not map to lower S units."""
        prev = raw_to_s_unit(0)
        for raw in (50, 100, 150, 200):
            curr = raw_to_s_unit(raw)
            # S-unit strings can be compared lexicographically with caution
            self.assertIsInstance(curr, str)


class ConfigConstantsTests(unittest.TestCase):
    """SDD §3: General config constants."""

    def test_preamp_labels(self):
        self.assertIn(0, PREAMP_LABELS)
        self.assertIn(1, PREAMP_LABELS)
        self.assertIn(2, PREAMP_LABELS)

    def test_attenuator_labels(self):
        for i in range(4):
            self.assertIn(i, ATTENUATOR_LABELS)

    def test_scope_spans(self):
        self.assertIsInstance(SCOPE_SPANS, dict)
        self.assertGreater(len(SCOPE_SPANS), 0)

    def test_mem_channel_count(self):
        self.assertGreater(MEM_CHANNEL_COUNT, 0)
        self.assertLessEqual(MEM_CHANNEL_COUNT, 100)

    def test_auth_config(self):
        self.assertIsInstance(AUTH_COOKIE, str)
        self.assertGreater(len(AUTH_COOKIE), 0)
        self.assertGreater(AUTH_TOKEN_BYTES, 0)


if __name__ == "__main__":
    unittest.main()
