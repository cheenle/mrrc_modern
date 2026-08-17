"""
Tests for RadioState — SDD §7.2, §9.7 (AD-003 dirty-field broadcasting).
Verifies: field mutation, dirty tracking, derived properties,
serialization, from_sync_result deserialization.
"""
import unittest

from radio_state import RadioState
from backends.ft710.config_ft710 import MODE_NUM_TO_NAME
from backends.ft710.backend import parse_ft710_sync
from config import MODE_DISPLAY_NAMES


class RadioStateFieldMutationTests(unittest.TestCase):
    """SDD AD-003: Dirty-field tracking for efficient partial broadcasts."""

    def setUp(self):
        self.state = RadioState()

    def test_initial_state_has_defaults(self):
        self.assertEqual(self.state.vfo_a_freq, 14_200_000)
        self.assertEqual(self.state.vfo_b_freq, 7_050_000)
        self.assertEqual(self.state.active_vfo, "A")
        self.assertEqual(self.state.tx_status, 0)
        self.assertEqual(self.state.af_gain, 128)
        self.assertEqual(self.state.rf_gain, 255)
        self.assertEqual(self.state.rf_power, 100)
        self.assertFalse(self.state.serial_connected)
        self.assertFalse(self.state.is_transmitting)

    def test_update_returns_changed_fields_only(self):
        changed = self.state.update(vfo_a_freq=14_250_000, mode=2)
        self.assertEqual(changed, {"vfo_a_freq", "mode"})

    def test_update_ignores_unchanged_values(self):
        changed = self.state.update(vfo_a_freq=14_200_000)  # Same as default
        self.assertEqual(changed, set())

    def test_update_ignores_unknown_fields(self):
        changed = self.state.update(nonexistent_field=42)
        self.assertEqual(changed, set())

    def test_dirty_fields_accumulate_across_updates(self):
        self.state.update(vfo_a_freq=14_250_000)
        self.state.update(mode=3)
        dirty = self.state.get_and_clear_dirty()
        self.assertEqual(dirty, {"vfo_a_freq", "mode"})
        # After clear, dirty set is empty
        self.assertEqual(self.state.get_and_clear_dirty(), set())

    def test_mark_dirty_explicitly(self):
        self.state.mark_dirty("s_meter", "tx_status")
        dirty = self.state.get_and_clear_dirty()
        self.assertIn("s_meter", dirty)
        self.assertIn("tx_status", dirty)

    def test_last_update_changes_on_mutation(self):
        self.assertAlmostEqual(self.state.last_update, 0.0)
        self.state.update(vfo_a_freq=14_250_000)
        self.assertGreater(self.state.last_update, 0)

    def test_multiple_fields_batched_update(self):
        changed = self.state.update(
            vfo_a_freq=14_250_000,
            mode=2,
            s_meter=150,
            preamp=1,
        )
        self.assertEqual(len(changed), 4)
        self.assertEqual(self.state.vfo_a_freq, 14_250_000)
        self.assertEqual(self.state.mode, 2)
        self.assertEqual(self.state.s_meter, 150)
        self.assertEqual(self.state.preamp, 1)


class RadioStateDerivedPropertiesTests(unittest.TestCase):
    """SDD §7.2: Derived properties (mode_name, s_unit, band_name, etc.)."""

    def setUp(self):
        self.state = RadioState()

    def test_active_freq_returns_vfo_a_when_a_active(self):
        self.state.active_vfo = "A"
        self.state.vfo_a_freq = 14_200_000
        self.assertEqual(self.state.active_freq, 14_200_000)

    def test_active_freq_returns_vfo_b_when_b_active(self):
        self.state.active_vfo = "B"
        self.state.vfo_b_freq = 7_050_000
        self.assertEqual(self.state.active_freq, 7_050_000)

    def test_mode_name_lookup(self):
        self.state.mode = 1
        self.assertEqual(self.state.mode_name, "LSB")
        self.state.mode = 2
        self.assertEqual(self.state.mode_name, "USB")

    def test_mode_display(self):
        self.state.mode = 2
        display = self.state.mode_display
        self.assertIsInstance(display, str)
        self.assertGreater(len(display), 0)

    def test_band_name_for_known_band(self):
        self.state.vfo_a_freq = 14_200_000
        self.state.active_vfo = "A"
        self.assertEqual(self.state.band_name, "20m")

    def test_band_name_for_edge_case(self):
        self.state.vfo_a_freq = 7_050_000
        self.state.active_vfo = "A"
        self.assertEqual(self.state.band_name, "40m")

    def test_is_transmitting_during_tx(self):
        self.state.tx_status = 1
        self.assertTrue(self.state.is_transmitting)

    def test_is_transmitting_during_tune(self):
        self.state.tx_status = 2
        self.assertTrue(self.state.is_transmitting)

    def test_is_not_transmitting_in_rx(self):
        self.state.tx_status = 0
        self.assertFalse(self.state.is_transmitting)

    def test_s_meter_dbm_is_numeric(self):
        self.state.s_meter = 150
        dbm = self.state.s_meter_dbm
        self.assertIsInstance(dbm, float)

    def test_s_unit_is_string(self):
        self.state.s_meter = 120
        s_unit = self.state.s_unit
        self.assertIsInstance(s_unit, str)
        self.assertTrue(s_unit.startswith("S") or s_unit == "S0")

    def test_preamp_label(self):
        self.state.preamp = 0
        self.assertEqual(self.state.preamp_label, "OFF")
        self.state.preamp = 1
        self.assertEqual(self.state.preamp_label, "AMP1")
        self.state.preamp = 2
        self.assertEqual(self.state.preamp_label, "AMP2")

    def test_attenuator_label(self):
        for i in range(4):
            self.state.attenuator = i
            self.assertIsInstance(self.state.attenuator_label, str)


class RadioStateSerializationTests(unittest.TestCase):
    """SDD §9.7: to_dict / to_dirty_dict serialization for WS broadcast."""

    def setUp(self):
        self.state = RadioState()

    def test_to_dict_includes_all_core_fields(self):
        d = self.state.to_dict(include_derived=False)
        self.assertIn("vfo_a_freq", d)
        self.assertIn("vfo_b_freq", d)
        self.assertIn("mode", d)
        self.assertIn("tx_status", d)
        self.assertIn("s_meter", d)
        self.assertIn("af_gain", d)
        self.assertIn("rf_power", d)
        self.assertIn("filter_width", d)
        self.assertIn("preamp", d)
        self.assertIn("noise_blanker", d)
        self.assertIn("scope_span", d)
        self.assertIn("serial_connected", d)

    def test_to_dict_includes_derived_fields_when_requested(self):
        d = self.state.to_dict(include_derived=True)
        self.assertIn("mode_name", d)
        self.assertIn("band_name", d)
        self.assertIn("s_unit", d)
        self.assertIn("is_transmitting", d)
        self.assertIn("filter_hz", d)

    def test_to_dirty_dict_only_returns_requested_fields(self):
        self.state.update(vfo_a_freq=14_250_000, mode=2, s_meter=100)
        dirty = self.state.to_dirty_dict({"vfo_a_freq", "s_meter"})
        self.assertEqual(
            set(dirty.keys()),
            {
                "vfo_a_freq",
                "active_freq",
                "band_name",
                "s_meter",
                "s_meter_dbm",
                "s_unit",
            },
        )
        self.assertEqual(dirty["vfo_a_freq"], 14_250_000)
        self.assertEqual(dirty["s_meter"], 100)
        self.assertIsInstance(dirty["s_meter_dbm"], float)
        self.assertIsInstance(dirty["s_unit"], str)

    def test_to_dirty_dict_includes_meter_derived_fields(self):
        self.state.update(
            power_meter=205,
            swr_meter=52,
            vd_meter=192,
            id_meter=53,
            alc_meter=128,
        )
        dirty = self.state.to_dirty_dict({
            "power_meter",
            "swr_meter",
            "vd_meter",
            "id_meter",
            "alc_meter",
        })
        self.assertEqual(dirty["power_meter"], 205)
        self.assertAlmostEqual(dirty["power_watts"], 100.0)
        self.assertEqual(dirty["swr_meter"], 52)
        self.assertAlmostEqual(dirty["swr_ratio"], 1.5)
        self.assertEqual(dirty["vd_meter"], 192)
        self.assertAlmostEqual(dirty["vd_volts"], 13.8)
        self.assertEqual(dirty["id_meter"], 53)
        self.assertAlmostEqual(dirty["id_amps"], 5.0)
        self.assertEqual(dirty["alc_meter"], 128)
        self.assertAlmostEqual(dirty["alc_pct"], 128 / 255 * 100)

    def test_to_dirty_dict_ignores_unknown_fields(self):
        d = self.state.to_dirty_dict({"nonexistent"})
        self.assertEqual(d, {})

    def test_dict_values_match_object_attributes(self):
        self.state.vfo_a_freq = 14_250_000
        self.state.mode = 2
        d = self.state.to_dict(include_derived=False)
        self.assertEqual(d["vfo_a_freq"], 14_250_000)
        self.assertEqual(d["mode"], 2)
        self.assertEqual(d["active_vfo"], "A")
        self.assertEqual(d["active_freq"], 14_250_000)


class RadioStateFromSyncResultTests(unittest.TestCase):
    """SDD §9.6: backend initial_state_sync → RadioState.

    Phase 2b contract: sync_data is ALREADY PARSED by the backend
    ({RadioState field: value}); from_sync_result is a validated plain
    update.  The Yaesu raw-string parsers live in
    backends.ft710.backend.parse_ft710_sync (tested below).
    """

    def test_from_sync_result_applies_parsed_values(self):
        sync_data = {
            "vfo_a_freq": 14_200_000,
            "vfo_b_freq": 7_050_000,
            "mode": 2,
            "tx_status": 0,
            "s_meter": 120,
            "filter_width": 5,
            "power_on": True,
        }
        state = RadioState.from_sync_result(sync_data)
        self.assertEqual(state.vfo_a_freq, 14_200_000)
        self.assertEqual(state.vfo_b_freq, 7_050_000)
        self.assertEqual(state.mode, 2)
        self.assertEqual(state.tx_status, 0)
        self.assertEqual(state.s_meter, 120)
        self.assertEqual(state.filter_width, 5)
        self.assertTrue(state.power_on)

    def test_from_sync_result_handles_empty_data(self):
        state = RadioState.from_sync_result({})
        # Should return default state
        self.assertEqual(state.vfo_a_freq, 14_200_000)
        self.assertFalse(state.serial_connected)

    def test_from_sync_result_skips_none_and_unknown_fields(self):
        sync_data = {
            "vfo_a_freq": None,          # failed query — keep default
            "mode": 1,
            "not_a_field": 42,           # unknown — ignored
            "active_freq": 1,            # derived property, not a field
        }
        state = RadioState.from_sync_result(sync_data)
        self.assertIsNotNone(state)
        self.assertEqual(state.vfo_a_freq, 14_200_000)  # default preserved
        self.assertEqual(state.mode, 1)
        self.assertFalse(hasattr(state, "not_a_field"))

    def test_from_sync_result_parses_boolean_fields(self):
        sync_data = {
            "noise_blanker": True,
            "noise_reduction": False,
            "auto_notch": True,
        }
        state = RadioState.from_sync_result(sync_data)
        self.assertTrue(state.noise_blanker)
        self.assertFalse(state.noise_reduction)
        self.assertTrue(state.auto_notch)

    def test_from_sync_result_applies_tuner_status(self):
        self.assertEqual(
            RadioState.from_sync_result({"tuner_status": 1}).tuner_status, 1)
        self.assertEqual(
            RadioState.from_sync_result({"tuner_status": 2}).tuner_status, 2)
        self.assertEqual(
            RadioState.from_sync_result({"tuner_status": 0}).tuner_status, 0)

    def test_from_sync_result_maps_af_gain_raw_alias(self):
        state = RadioState.from_sync_result({"af_gain_raw": 200})
        self.assertEqual(state.af_gain, 200)


class FT710SyncParsingTests(unittest.TestCase):
    """The raw FT-710 CAT string parsers moved to parse_ft710_sync —
    these are the pre-refactor from_sync_result fixtures, asserting the
    FT-710 output is byte-identical after the move."""

    def test_parses_known_fields(self):
        raw = {
            "vfo_a_freq": "FA014200000",
            "vfo_b_freq": "FB007050000",
            "mode": "MD02",
            "tx_status": "TX0",
            "s_meter": "SM00120",
            "filter_width": "SH005",
            "power_on": "PS1",
        }
        parsed = parse_ft710_sync(raw)
        self.assertEqual(parsed["vfo_a_freq"], 14_200_000)
        self.assertEqual(parsed["vfo_b_freq"], 7_050_000)
        self.assertEqual(parsed["mode"], 2)
        self.assertEqual(parsed["tx_status"], 0)
        self.assertEqual(parsed["s_meter"], 120)
        self.assertEqual(parsed["filter_width"], 5)
        self.assertTrue(parsed["power_on"])
        # And the parsed dict feeds the new from_sync_result directly.
        state = RadioState.from_sync_result(parsed)
        self.assertEqual(state.vfo_a_freq, 14_200_000)
        self.assertEqual(state.mode, 2)
        self.assertEqual(state.s_meter, 120)

    def test_handles_malformed_responses(self):
        raw = {
            "vfo_a_freq": "FA",   # Too short — len <= 2, parser returns 0
            "mode": "MD",          # Too short
            "s_meter": "SM",       # Too short
        }
        parsed = parse_ft710_sync(raw)
        self.assertEqual(parsed["vfo_a_freq"], 0)
        state = RadioState.from_sync_result(parsed)
        self.assertIsNotNone(state)

    def test_parses_boolean_fields(self):
        parsed = parse_ft710_sync({
            "noise_blanker": "NB01",
            "noise_reduction": "NR00",
            "auto_notch": "BC1",
        })
        self.assertTrue(parsed["noise_blanker"])
        self.assertFalse(parsed["noise_reduction"])
        self.assertTrue(parsed["auto_notch"])

    def test_parses_preamp_attenuator(self):
        parsed = parse_ft710_sync({"preamp": "PA02", "attenuator": "RA03"})
        self.assertEqual(parsed["preamp"], 2)
        self.assertEqual(parsed["attenuator"], 3)

    def test_parses_tuner(self):
        # AC P1P2P3 format per FT-710 CAT spec:
        # P1=0, P2=0 (standard tuner), P3=0=OFF, P3=1=ON, P3=3=Tuning
        self.assertEqual(parse_ft710_sync({"tuner_status": "AC001"})["tuner_status"], 1)
        self.assertEqual(parse_ft710_sync({"tuner_status": "AC003"})["tuner_status"], 2)
        self.assertEqual(parse_ft710_sync({"tuner_status": "AC000"})["tuner_status"], 0)

    def test_parses_ri_into_individual_fields(self):
        # RI0 + 7 single-char fields: P2 hi-swr, P3 rec, P4 rx/tx, P5=0,
        # P6 tuner-tuning, P7 scan, P8 squelch-open
        parsed = parse_ft710_sync({"ri": "RI01010111"})
        self.assertTrue(parsed["hi_swr"])
        self.assertEqual(parsed["recording_status"], 0)
        self.assertEqual(parsed["rx_tx_status"], 1)
        self.assertTrue(parsed["tuner_tuning"])
        self.assertEqual(parsed["scan_status"], 1)
        self.assertTrue(parsed["squelch_open"])

    def test_maps_af_gain_raw_to_af_gain(self):
        parsed = parse_ft710_sync({"af_gain_raw": "AG128"})
        self.assertEqual(parsed["af_gain"], 128)


class RadioStateConfigureTests(unittest.TestCase):
    """Phase 2b: per-backend table injection into RadioState."""

    def test_defaults_are_ft710_tables(self):
        state = RadioState()
        state.mode = 2
        self.assertEqual(state.mode_name, "USB")
        state.vfo_a_freq = 14_200_000
        self.assertEqual(state.band_name, "20m")

    def test_configure_switches_mode_and_band_tables(self):
        from backends.ic7300.config_ic7300 import (
            MODE_NUM_TO_NAME as IC_MODE_NUM_TO_NAME,
            get_band_for_frequency as ic_get_band,
        )
        state = RadioState()
        state.configure(
            mode_num_to_name=IC_MODE_NUM_TO_NAME,
            get_band_for_frequency=ic_get_band,
        )
        state.mode = 0x01            # CI-V USB
        self.assertEqual(state.mode_name, "USB")
        state.mode = 0x00            # CI-V LSB
        self.assertEqual(state.mode_name, "LSB")
        state.vfo_a_freq = 14_200_000
        self.assertEqual(state.band_name, "20m")

    def test_configure_rejects_unknown_table(self):
        state = RadioState()
        with self.assertRaises(KeyError):
            state.configure(bogus_table={})

    def test_ic7300_backend_state_tables_are_complete(self):
        """Every key IC7300Backend injects must be a known table."""
        from backends.ic7300.backend import IC7300Backend
        backend = IC7300Backend("/dev/null")
        state = RadioState()
        state.configure(**backend.state_tables())  # must not raise
        state.mode = 0x01
        self.assertEqual(state.mode_name, "USB")
        # fil123 filter model: FIL1 on USB defaults to 3000 Hz
        state.filter_width = 1
        self.assertEqual(state.filter_hz, 3000)


if __name__ == "__main__":
    unittest.main()
