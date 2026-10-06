"""Guards for the host scorer: its bands, its math, and its workload's shape.

The scorer's value depends on measuring the *same* work the server does, and on
its reference point staying honest. Both are asserted here rather than left to
review: a workload that quietly drifts (a smaller state document, one waterfall
row instead of two) would still produce a number, and that number would be
wrong in a way nobody would notice until a box was bought.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "host_score", REPO / "dev_tools" / "host_score.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host_score = _load()


class VerdictBandTests(unittest.TestCase):
    """The bands are the script's opinion, so they are pinned."""

    def test_bands_are_ordered_and_cover_the_range(self):
        self.assertIn("comfortable", host_score.verdict(1))
        self.assertIn("comfortable", host_score.verdict(14.9))
        self.assertIn("one client", host_score.verdict(15))
        self.assertIn("one client", host_score.verdict(39.9))
        self.assertIn("tight", host_score.verdict(40))
        self.assertIn("tight", host_score.verdict(69.9))
        self.assertIn("too slow", host_score.verdict(70))
        self.assertIn("too slow", host_score.verdict(400))


class WorkloadShapeTests(unittest.TestCase):
    """The workload has to stay the shape the server actually pays for."""

    def test_two_waterfall_rows_of_the_real_width(self):
        rows = host_score._wf_pair()
        self.assertEqual(len(rows), 2, "a frame carries wf1 and wf2")
        for row in rows:
            self.assertEqual(len(row), 850)
            self.assertTrue(all(0 <= v <= 255 for v in row))

    def test_the_frame_is_the_size_the_parser_gets(self):
        self.assertEqual(len(host_score._raw_frame()), 4096)
        self.assertEqual(host_score.FRAME_SIZE, 4096)

    def test_the_frame_is_wide_enough_for_both_rows(self):
        """The parse slices [0:850] and [850:1700]; a shorter frame would
        silently measure a smaller slice."""
        self.assertGreaterEqual(len(host_score._raw_frame()), 2 * host_score.WF_SIZE)

    def test_rates_match_the_server(self):
        self.assertEqual(host_score.SPECTRUM_FRAMES, 30, "spectrum broadcasts at 30 fps")
        self.assertEqual(host_score.STATE_FRAMES, 10, "state broadcasts at ~10 fps")

    def test_the_state_document_has_the_real_nested_shape(self):
        """fullState carries objects in `bands`, not a flat list (design D-2).

        A flat document would serialise faster and understate the cost.
        """
        doc = host_score._state_document()
        self.assertIsInstance(doc["bands"], list)
        self.assertGreaterEqual(len(doc["bands"]), 10)
        for band in doc["bands"]:
            self.assertIsInstance(band, dict)
            self.assertEqual(
                set(band), {"name", "start", "end", "bsr", "default_freq"}
            )
        self.assertIsInstance(doc["capabilities"]["scope_spans"], list)


class ReferenceTests(unittest.TestCase):
    """An uncalibrated copy would report nonsense with full confidence."""

    def test_a_reference_measurement_is_baked_in(self):
        self.assertGreater(host_score.REFERENCE["pure_python_seconds"], 0)
        self.assertEqual(host_score.REFERENCE["hot_path_percent_of_one_core"], 1.53)
        self.assertIn("M2", host_score.REFERENCE["machine"])

    def test_the_reference_split_sums_to_the_total(self):
        total = host_score.REFERENCE["hot_path_percent_of_one_core"]
        self.assertAlmostEqual(sum(host_score.REFERENCE_SPLIT.values()), total, places=2)


class MeasurementTests(unittest.TestCase):
    """The workloads run, and they are fast enough to be worth running."""

    def test_one_second_of_python_is_positive_and_small(self):
        seconds = host_score.one_second_of_python()
        self.assertGreater(seconds, 0)
        self.assertLess(seconds, 5, "one second of work should not take seconds")

    def test_numpy_workload_is_optional_not_required(self):
        """Absent numpy is a missing row, not a failure."""
        seconds = host_score.one_second_of_numpy()
        self.assertTrue(seconds is None or seconds > 0)

    def test_json_output_parses_and_carries_the_estimate(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = host_score.main(["--json", "--runs", "2"])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertGreater(payload["estimated_hot_path_percent"], 0)
        self.assertIn("verdict", payload)
        self.assertIn("ratio_vs_reference", payload)


if __name__ == "__main__":
    unittest.main()
