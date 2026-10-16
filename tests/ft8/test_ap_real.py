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


class FindPairsTests(unittest.TestCase):
    """``find_pairs`` runs on a cache, so it needs no radio and no corpus.

    It decides *whose* a-priori the experiment adopts, so its convention is worth
    pinning: FT8 text is ``<addressee> <sender> <info>``, and a8's ``getmsg``
    builds ``mycall hiscall <info>`` -- so ``my_call`` must be the **addressee**
    of the partner's transmissions.  Getting that backwards would aim the whole
    hypothesis list at a message nobody sends.
    """

    @staticmethod
    def _cache(*entries: tuple[int, str]) -> dict:
        cache: dict = {}
        for slot_id, text in entries:
            cache.setdefault(slot_id, []).append(
                ap_real.Sighting(slot_id, text, 1500.0, -10, 0, 1.0)
            )
        return cache

    def test_only_mutual_pairs_qualify(self) -> None:
        """One station calling CQ into a crowd is not a QSO."""

        cache = self._cache(
            (1, "ZL3BT JE2NRP PM95"),
            (2, "ZL3BT JE2NRP R-12"),
        )
        mutual, _directed, _grids = ap_real.find_pairs(cache)
        self.assertEqual(mutual, [], "no reply in the other direction was ever heard")

    def test_a_mutual_pair_is_ranked_by_message_count(self) -> None:
        cache = self._cache(
            (1, "ZL3BT JE2NRP PM95"), (2, "JE2NRP ZL3BT R-12"),
            (3, "ZL3BT JE2NRP R-12"), (4, "JE2NRP ZL3BT RR73"),
            (5, "VK3ABC ZL3BT PM95"), (6, "ZL3BT VK3ABC R-09"),
        )
        mutual, _directed, _grids = ap_real.find_pairs(cache)
        self.assertEqual([(c, a, b) for c, a, b in mutual][0][0], 4)
        self.assertEqual(len(mutual), 2)

    def test_cq_messages_are_not_pairs_but_do_supply_grids(self) -> None:
        cache = self._cache((1, "CQ JE2NRP PM95"), (2, "ZL3BT JE2NRP R-12"),
                            (3, "JE2NRP ZL3BT RR73"))
        mutual, _directed, grids = ap_real.find_pairs(cache)
        self.assertEqual(grids.get("JE2NRP"), "PM95")
        self.assertEqual(len(mutual), 1)

    def test_the_adopted_identity_matches_what_a8_would_synthesize(self) -> None:
        """``directed[(a, b)]`` holds messages *b sent to a* ⇒ my_call=a, dx_call=b.

        Verified against the parser rather than assumed: for the text the partner
        really transmits, to_call is the addressee and from_call the sender.
        """

        from ft8.engine.msgparse import parse_message

        text = "ZL3BT JE2NRP PM95"
        parsed = parse_message(text)
        self.assertEqual(parsed.to_call, "ZL3BT")
        self.assertEqual(parsed.from_call, "JE2NRP")

        cache = self._cache((1, text), (2, "JE2NRP ZL3BT R-12"))
        _mutual, directed, _grids = ap_real.find_pairs(cache)
        my_call, dx_call = ("ZL3BT", "JE2NRP")
        self.assertIn((my_call, dx_call), directed)
        # a8's getmsg builds "mycall hiscall <info>"; the real text has that shape.
        self.assertTrue(text.startswith(f"{my_call} {dx_call}"))


class SampleSlotsTests(unittest.TestCase):
    """The budget cap must sample across a QSO, not from its start."""

    def test_under_the_limit_everything_is_kept(self) -> None:
        self.assertEqual(ap_real.sample_slots([1, 2, 3], 10), [1, 2, 3])

    def test_over_the_limit_it_spreads_rather_than_taking_the_head(self) -> None:
        picked = ap_real.sample_slots(list(range(100)), 10)
        self.assertEqual(len(picked), 10)
        self.assertNotEqual(picked, list(range(10)), "must not just take the first N")
        self.assertGreater(max(picked), 80, "the tail of the QSO must be represented")

    def test_it_is_deterministic(self) -> None:
        items = list(range(50))
        self.assertEqual(ap_real.sample_slots(items, 12), ap_real.sample_slots(items, 12))

    def test_zero_budget_and_empty_input(self) -> None:
        self.assertEqual(ap_real.sample_slots([], 5), [])
        self.assertEqual(ap_real.sample_slots([1, 2, 3], 0), [])
