"""The real-domain AP tool's pure parts: no radio, no corpus, no native library.

Two of these already caused a real failure, which is why they are pinned:

* ``decode`` requires an **int16** array.  The first version of the survey script
  passed ``float32`` (the form ``ml_simulate.slot_pcm`` returns, because that one
  is for synthesis) and every slot raised ``ValueError``.
* The parity argument -- "a station transmits on alternate slots, so a sighting in
  slot N tells us which other slots they transmitted in" -- is only valid if the
  slot list really is chronological.  Sorting by filename happens to work because
  the dumps are zero-padded, but the tool sorts by the parsed id explicitly.
"""

from __future__ import annotations

import unittest

import numpy as np

from tools.ft8 import ap_real


class SlotPathTests(unittest.TestCase):
    def _write(self, tmp, names: list[str]) -> None:
        for name in names:
            (tmp / name).write_bytes(b"\x00" * 360_000)

    def test_slots_come_back_in_chronological_order(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            # Deliberately out of order, and with ids that would sort wrongly as
            # text if they were not zero-padded.
            self._write(tmp, [
                "slot_000000000300.raw",
                "slot_000000000100.raw",
                "slot_000000000200.raw",
            ])
            ids = [slot_id for slot_id, _ in ap_real.slot_paths(tmp, None)]
            self.assertEqual(ids, [100, 200, 300])

    def test_limit_takes_the_earliest_slots(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            self._write(tmp, [f"slot_{i:012d}.raw" for i in (5, 3, 9, 1)])
            ids = [slot_id for slot_id, _ in ap_real.slot_paths(tmp, 2)]
            self.assertEqual(ids, [1, 3], "the limit must cut the *latest* slots")

    def test_unparsable_filenames_are_skipped_not_fatal(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            self._write(tmp, ["slot_000000000007.raw", "not_a_slot.raw", "slot_x.raw"])
            ids = [slot_id for slot_id, _ in ap_real.slot_paths(tmp, None)]
            self.assertEqual(ids, [7])


class LoadPcmTests(unittest.TestCase):
    def test_it_returns_the_int16_array_decode_requires(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "slot_000000000001.raw"
            path.write_bytes(np.arange(1000, dtype=np.int16).tobytes())
            pcm = ap_real.load_pcm(path)

        self.assertEqual(pcm.dtype, np.int16)
        self.assertTrue(pcm.flags.writeable, "frombuffer is read-only; decode needs a copy")

    def test_the_values_survive_the_round_trip(self) -> None:
        import tempfile
        from pathlib import Path

        original = np.array([-32768, -1, 0, 1, 32767], dtype=np.int16)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "slot_000000000002.raw"
            path.write_bytes(original.tobytes())
            self.assertTrue(np.array_equal(ap_real.load_pcm(path), original))


class ConfigTests(unittest.TestCase):
    def test_the_base_config_is_the_production_deep_path(self) -> None:
        """AP must be measured against what the host actually runs."""

        from ft8.core.models import DecodePath

        self.assertEqual(ap_real.BASE["path"], DecodePath.IMPROVED)
        self.assertEqual(ap_real.BASE["profile"], 1)
        self.assertEqual(ap_real.BASE["cycles"], 3)
        self.assertEqual(ap_real.BASE["sensitivity"], 3)
        self.assertTrue(ap_real.BASE["low_threshold"])

    def test_the_base_config_matches_the_synthetic_probe(self) -> None:
        """Two tools measuring the same thing must use the same decoder settings.

        If these drift apart, the synthetic numbers (§4.2/§4.4) and the real ones
        (§4.6) stop being comparable -- and comparing them is the whole point.
        """

        from tools.ft8 import ap_probe

        self.assertEqual(ap_real.BASE, ap_probe.BASE)


if __name__ == "__main__":
    unittest.main()
