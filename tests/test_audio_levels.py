"""Box-level audio gains (AD-032) — the pure half.

Two invariants carry the whole feature:

* **0 dB is the identity**, byte for byte (the same bytes object, not merely
  equal ones).  A box that never touched the knob must send exactly what it
  sent before the feature existed, and a test can only prove that with `is`.
* **A bad value is refused, never clamped.**  Silently rewriting what an
  operator asked for is how a slider ends up disagreeing with the audio.

The store's tolerance is the third: a hand-edited or truncated JSON file must
fall back to 0 dB with a warning, because this runs at startup on a box nobody
can log into.
"""

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

import audio_levels as al


def _pcm(*values: int) -> bytes:
    return np.array(values, dtype="<i2").tobytes()


def _samples(pcm: bytes) -> list:
    return np.frombuffer(pcm, dtype="<i2").tolist()


class ApplyGainTests(unittest.TestCase):
    def test_zero_db_returns_the_callers_own_bytes(self):
        pcm = _pcm(0, 100, -100, 3000)
        out, clipped = al.apply_gain(pcm, 0.0)
        self.assertIs(out, pcm, "0 dB must not copy or re-quantise")
        self.assertEqual(clipped, 0)

    def test_zero_db_is_also_the_identity_for_the_silent_and_odd_cases(self):
        self.assertIs(al.apply_gain(b"", 6.0)[0], b"")
        pcm = _pcm(1, 2, 3)
        self.assertIs(al.apply_gain(pcm, -0.0)[0], pcm)

    def test_plus_six_db_is_twice(self):
        out, clipped = al.apply_gain(_pcm(0, 100, -100, 1000), 6.0206)
        self.assertEqual(_samples(out), [0, 200, -200, 2000])
        self.assertEqual(clipped, 0)

    def test_minus_six_db_is_half(self):
        out, clipped = al.apply_gain(_pcm(0, 100, -100, 1000), -6.0206)
        self.assertEqual(_samples(out), [0, 50, -50, 500])
        self.assertEqual(clipped, 0)

    def test_clipping_is_counted_not_hidden(self):
        out, clipped = al.apply_gain(_pcm(30000, -30000, 100), 6.0206)
        self.assertEqual(_samples(out)[:2], [32767, -32768])
        self.assertEqual(clipped, 2)
        self.assertEqual(_samples(out)[2], 200)

    def test_a_trailing_odd_byte_is_dropped_not_raised(self):
        """The 20 ms paths are always even; this only has to not crash the loop."""
        pcm = _pcm(1000, -1000) + b"\x7f"
        out, clipped = al.apply_gain(pcm, 6.0206)
        self.assertEqual(_samples(out), [2000, -2000])
        self.assertEqual(clipped, 0)

    def test_silence_stays_silent_at_any_gain(self):
        for db in (-20.0, -3.0, 25.0):
            out, clipped = al.apply_gain(_pcm(0, 0, 0), db)
            self.assertEqual(_samples(out), [0, 0, 0])
            self.assertEqual(clipped, 0)

    def test_db_to_linear_landmarks(self):
        self.assertAlmostEqual(al.db_to_linear(0.0), 1.0)
        self.assertAlmostEqual(al.db_to_linear(6.0206), 2.0, places=4)
        self.assertAlmostEqual(al.db_to_linear(-6.0206), 0.5, places=4)
        self.assertAlmostEqual(al.db_to_linear(20.0), 10.0)


class ParseUpdateTests(unittest.TestCase):
    def setUp(self):
        self.current = al.AudioLevels()

    def test_a_partial_update_keeps_the_other_value(self):
        levels, problem = al.parse_update({"rx_db": 6}, al.AudioLevels(tx_mic_db=3))
        self.assertEqual(problem, "")
        assert levels is not None
        self.assertEqual(levels.to_wire(), {"tx_mic_db": 3.0, "rx_db": 6.0})

    def test_boundaries_are_allowed(self):
        levels, problem = al.parse_update(
            {"tx_mic_db": 25, "rx_db": -20}, self.current)
        self.assertEqual(problem, "")
        assert levels is not None
        self.assertEqual(levels.tx_mic_db, 25.0)
        self.assertEqual(levels.rx_db, -20.0)

    def test_out_of_range_is_refused_with_the_range_in_the_message(self):
        for payload, needle in (({"rx_db": 40}, "-20…30"),
                                ({"tx_mic_db": 40}, "-20…25"),
                                ({"tx_mic_db": -30}, "-20…25")):
            levels, problem = al.parse_update(payload, self.current)
            self.assertIsNone(levels)
            self.assertIn(needle, problem)

    def test_a_silent_clamp_never_happens(self):
        """The old state survives a refusal — that is the point of refusing."""
        levels, _ = al.parse_update({"rx_db": 99}, self.current)
        self.assertIsNone(levels)
        self.assertEqual(self.current.to_wire(), {"tx_mic_db": 0.0, "rx_db": 0.0})

    def test_non_numbers_and_bools_are_refused(self):
        for value in ("6", None, [], {}, True, float("nan"), float("inf")):
            levels, problem = al.parse_update({"tx_mic_db": value}, self.current)
            self.assertIsNone(levels, f"{value!r} must be refused")
            self.assertTrue(problem)

    def test_unknown_keys_are_refused_so_a_typo_cannot_look_like_success(self):
        levels, problem = al.parse_update({"rx_gain_db": 6}, self.current)
        self.assertIsNone(levels)
        self.assertIn("rx_gain_db", problem)

    def test_an_empty_or_non_object_body_is_refused(self):
        for payload in ({}, None, [], "6"):
            levels, _ = al.parse_update(payload, self.current)
            self.assertIsNone(levels)

    def test_the_limit_table_matches_the_module_constants(self):
        table = al.AudioLevels.limit_table()
        self.assertEqual(table["tx_mic_db"], list(al.TX_MIC_LIMITS))
        self.assertEqual(table["rx_db"], list(al.RX_LIMITS))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "audio-levels.json"

    def test_a_missing_file_is_the_default_not_an_error(self):
        store = al.AudioLevelsStore(self.path)
        self.assertEqual(store.load().to_wire(), {"tx_mic_db": 0.0, "rx_db": 0.0})
        self.assertEqual(store.source, "default")

    def test_round_trip(self):
        store = al.AudioLevelsStore(self.path)
        self.assertTrue(store.save(al.AudioLevels(tx_mic_db=-3.5, rx_db=12)))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"tx_mic_db": -3.5, "rx_db": 12.0})
        again = al.AudioLevelsStore(self.path)
        self.assertEqual(again.load().to_wire(), {"tx_mic_db": -3.5, "rx_db": 12.0})
        self.assertEqual(again.source, "file")

    def test_corrupt_json_falls_back_with_a_warning(self):
        self.path.write_text("{not json", encoding="utf-8")
        store = al.AudioLevelsStore(self.path)
        with self.assertLogs("mrrc.audiolevels", level="WARNING"):
            levels = store.load()
        self.assertEqual(levels.to_wire(), {"tx_mic_db": 0.0, "rx_db": 0.0})
        self.assertEqual(store.source, "default")

    def test_a_json_array_is_not_a_level_file(self):
        self.path.write_text("[1, 2]", encoding="utf-8")
        store = al.AudioLevelsStore(self.path)
        with self.assertLogs("mrrc.audiolevels", level="WARNING"):
            self.assertEqual(store.load().to_wire(), {"tx_mic_db": 0.0, "rx_db": 0.0})

    def test_a_hand_edited_out_of_range_value_is_clamped_loudly(self):
        self.path.write_text(json.dumps({"tx_mic_db": 90, "rx_db": 6}),
                             encoding="utf-8")
        store = al.AudioLevelsStore(self.path)
        with self.assertLogs("mrrc.audiolevels", level="WARNING") as logs:
            levels = store.load()
        self.assertEqual(levels.tx_mic_db, al.TX_MIC_LIMITS[1])
        self.assertEqual(levels.rx_db, 6.0)
        self.assertTrue(any("clamped" in line for line in logs.output))

    def test_a_non_numeric_field_is_ignored_and_the_rest_kept(self):
        self.path.write_text(json.dumps({"tx_mic_db": "loud", "rx_db": 9}),
                             encoding="utf-8")
        store = al.AudioLevelsStore(self.path)
        with self.assertLogs("mrrc.audiolevels", level="WARNING"):
            levels = store.load()
        self.assertEqual(levels.to_wire(), {"tx_mic_db": 0.0, "rx_db": 9.0})

    def test_the_write_is_atomic_and_leaves_no_temp_files(self):
        store = al.AudioLevelsStore(self.path)
        self.assertTrue(store.save(al.AudioLevels(rx_db=6)))
        leftovers = [p.name for p in Path(self._dir.name).iterdir()
                     if p.name != self.path.name]
        self.assertEqual(leftovers, [], "a .tmp file survived the save")
        self.assertEqual(self.path.stat().st_mode & 0o077, 0,
                         "the file must not be group/world readable")

    def test_an_unwritable_path_is_reported_not_raised(self):
        store = al.AudioLevelsStore(Path(self._dir.name) / "nested" / "deep" / "x.json")
        # The parent is created for real; make the save fail by pointing at a
        # directory path instead of a file.
        blocked = al.AudioLevelsStore(Path(self._dir.name))
        with self.assertLogs("mrrc.audiolevels", level="WARNING"):
            self.assertFalse(blocked.save(al.AudioLevels(rx_db=6)))


class ClipMonitorTests(unittest.TestCase):
    def setUp(self):
        self.warnings: list = []
        self.monitor = al.ClipMonitor(warn=lambda *a, **k: self.warnings.append(a))

    def test_it_counts_both_directions(self):
        self.monitor.note("tx", 480, 0, gain_db=6, now=1.0)
        self.monitor.note("rx", 480, 5, gain_db=6, now=1.0)
        stats = self.monitor.snapshot()
        self.assertEqual(stats["tx"], {"samples": 480, "clipped": 0})
        self.assertEqual(stats["rx"], {"samples": 480, "clipped": 5})

    def test_zero_gain_never_warns_even_on_a_clipped_block(self):
        self.monitor.note("tx", 100, 90, gain_db=0.0, now=1.0)
        self.assertEqual(self.warnings, [])

    def test_a_small_ratio_never_warns(self):
        self.monitor.note("tx", 10_000, 5, gain_db=6, now=1.0)   # 0.05 %
        self.assertEqual(self.warnings, [])

    def test_the_warning_is_rate_limited_per_direction(self):
        for tick in range(5):
            self.monitor.note("tx", 100, 100, gain_db=6, now=float(tick))
        self.assertEqual(len(self.warnings), 1)
        self.monitor.note("rx", 100, 100, gain_db=6, now=1.0)
        self.assertEqual(len(self.warnings), 2, "directions are independent")
        self.monitor.note("tx", 100, 100, gain_db=6, now=40.0)
        self.assertEqual(len(self.warnings), 3, "the interval re-arms the warning")

    def test_the_warning_names_the_gain_and_the_counts(self):
        self.monitor.note("tx", 100, 100, gain_db=12.0, now=1.0)
        args = self.warnings[0]
        self.assertEqual(args[1], "TX")
        self.assertEqual(args[2], 12.0)
        self.assertEqual(args[3:5], (100, 100))


if __name__ == "__main__":
    unittest.main()
