"""Sanity tests for the IC-7300 configuration tables (no hardware)."""
import unittest

from backends.ic7300 import config_ic7300 as cfg
import config


class ConnectionTests(unittest.TestCase):
    def test_civ_defaults(self):
        self.assertEqual(cfg.CIV_ADDR, 0x94)
        self.assertEqual(cfg.CIV_BAUD_RATE, 115200)


class ModeTableTests(unittest.TestCase):
    def test_civ_mode_codes(self):
        self.assertEqual(cfg.MODE_NUM_TO_NAME[0x00], "LSB")
        self.assertEqual(cfg.MODE_NUM_TO_NAME[0x01], "USB")
        self.assertEqual(cfg.MODE_NUM_TO_NAME[0x02], "AM")
        self.assertEqual(cfg.MODE_NUM_TO_NAME[0x03], "CW-U")
        self.assertEqual(cfg.MODE_NUM_TO_NAME[0x04], "RTTY-L")
        self.assertEqual(cfg.MODE_NUM_TO_NAME[0x05], "FM")
        self.assertEqual(cfg.MODE_NUM_TO_NAME[0x06], "WFM")
        self.assertEqual(cfg.MODE_NUM_TO_NAME[0x07], "CW-L")
        self.assertEqual(cfg.MODE_NUM_TO_NAME[0x08], "RTTY-U")

    def test_inverse_map_consistent(self):
        for num, name in cfg.MODE_NUM_TO_NAME.items():
            self.assertEqual(cfg.MODE_NAME_TO_NUM[name], num)

    def test_ui_mode_names_reused(self):
        # Every mapped name except receive-only WFM exists in the shared
        # UI mode tables (config.MODE_DISPLAY_NAMES keys).
        for name in cfg.MODE_NUM_TO_NAME.values():
            if name == "WFM":
                continue
            self.assertIn(name, config.MODE_DISPLAY_NAMES)


class BandTests(unittest.TestCase):
    def test_bands_shape_matches_ft710_minus_bsr(self):
        for band in cfg.BANDS:
            self.assertEqual(
                set(band), {"name", "start", "end", "default_freq"}, band)
            self.assertLess(band["start"], band["end"])
            self.assertGreaterEqual(band["default_freq"], band["start"])
            self.assertLessEqual(band["default_freq"], band["end"])

    def test_bands_within_radio_coverage(self):
        # IC-7300: HF + 50 MHz + 70 MHz (4m, regional)
        for band in cfg.BANDS:
            self.assertGreaterEqual(band["start"], 1_800_000)
            self.assertLessEqual(band["end"], 70_500_000)

    def test_expected_bands_present(self):
        names = [b["name"] for b in cfg.BANDS]
        self.assertEqual(
            names,
            ["160m", "80m", "60m", "40m", "30m", "20m",
             "17m", "15m", "12m", "10m", "6m", "4m"],
        )

    def test_get_band_for_frequency(self):
        self.assertEqual(cfg.get_band_for_frequency(14_200_000)["name"], "20m")
        self.assertEqual(cfg.get_band_for_frequency(50_150_000)["name"], "6m")
        self.assertEqual(cfg.get_band_for_frequency(70_250_000)["name"], "4m")
        self.assertIsNone(cfg.get_band_for_frequency(144_000_000))


class FilterTableTests(unittest.TestCase):
    def test_filter_model(self):
        self.assertEqual(cfg.FILTER_MODEL, "fil123")

    def test_defaults_cover_ui_modes(self):
        for name in ("LSB", "USB", "CW-U", "CW-L", "RTTY-L", "RTTY-U", "AM", "FM"):
            widths = cfg.FIL_DEFAULT_WIDTHS_HZ[name]
            self.assertEqual(len(widths), 3)
            self.assertGreater(widths[0], widths[1])
            self.assertGreater(widths[1], widths[2])


def _assert_monotonic_table(testcase, table):
    raws = [r for r, _ in table]
    vals = [v for _, v in table]
    testcase.assertEqual(raws, sorted(raws))
    testcase.assertEqual(vals, sorted(vals))
    testcase.assertEqual(raws[0], 0)


class MeterCalTests(unittest.TestCase):
    def test_tables_monotonic(self):
        for table in (cfg.S_METER_CAL, cfg.POWER_CAL, cfg.SWR_CAL,
                      cfg.ALC_CAL, cfg.COMP_CAL, cfg.VOLTAGE_CAL,
                      cfg.CURRENT_CAL):
            _assert_monotonic_table(self, table)

    def test_s_meter_reference_points(self):
        self.assertEqual(cfg.raw_to_dbm(120), 0.0)    # S9
        self.assertEqual(cfg.raw_to_dbm(241), 60.0)   # S9+60dB
        self.assertEqual(cfg.raw_to_dbm(0), -54.0)

    def test_s_unit_display(self):
        self.assertEqual(cfg.raw_to_s_unit(0), "S0")
        self.assertEqual(cfg.raw_to_s_unit(120), "S9")
        self.assertEqual(cfg.raw_to_s_unit(241), "+60")
        self.assertEqual(cfg.raw_to_s_unit(10), "S1")

    def test_power_reference_points(self):
        self.assertEqual(cfg.raw_to_power(143), 50.0)
        self.assertEqual(cfg.raw_to_power(213), 100.0)
        self.assertEqual(cfg.raw_to_power(0), 0.0)

    def test_swr_reference_points(self):
        self.assertEqual(cfg.raw_to_swr(0), 1.0)
        self.assertEqual(cfg.raw_to_swr(80), 2.0)
        self.assertEqual(cfg.raw_to_swr(241), 6.0)

    def test_interp_between_points(self):
        # Halfway between raw 120 (S9) and 241 (S9+60) -> ~+30 dB
        self.assertAlmostEqual(cfg.raw_to_dbm(180), 29.75, places=2)

    def test_meter_sub_codes(self):
        self.assertEqual(cfg.METER_SUB_S, 0x02)
        self.assertEqual(cfg.METER_SUB_PO, 0x11)
        self.assertEqual(cfg.METER_SUB_SWR, 0x12)
        self.assertEqual(cfg.METER_SUB_ALC, 0x13)
        self.assertEqual(cfg.METER_SUB_COMP, 0x14)
        self.assertEqual(cfg.METER_SUB_VD, 0x15)
        self.assertEqual(cfg.METER_SUB_ID, 0x16)


class ScopeTableTests(unittest.TestCase):
    def test_span_codes(self):
        self.assertEqual(len(cfg.SCOPE_SPANS), 8)
        freqs = [cfg.SCOPE_SPANS[i]["freq"] for i in range(8)]
        self.assertEqual(freqs, sorted(freqs))
        self.assertEqual(freqs[0], 2500)
        self.assertEqual(freqs[-1], 500000)

    def test_span_hz_matches_spans(self):
        for code, hz in cfg.SCOPE_SPAN_HZ.items():
            self.assertEqual(cfg.SCOPE_SPANS[code]["freq"], hz)

    def test_fixed_edges_valid(self):
        self.assertEqual(len(cfg.SCOPE_FIXED_EDGES), 4)
        for low, high in cfg.SCOPE_FIXED_EDGES:
            self.assertLess(low, high)
            self.assertIsNotNone(cfg.get_band_for_frequency((low + high) // 2))


class PreampAttTests(unittest.TestCase):
    def test_preamp_labels(self):
        self.assertEqual(cfg.PREAMP_LABELS, {0: "OFF", 1: "AMP1", 2: "AMP2"})

    def test_attenuator(self):
        self.assertEqual(cfg.ATT_STEPS_DB, (0, 20))
        self.assertIn(0, cfg.ATTENUATOR_LABELS)
        self.assertIn(20, cfg.ATTENUATOR_LABELS)


if __name__ == "__main__":
    unittest.main()
