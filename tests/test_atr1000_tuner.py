"""
Tests for atr1000_tuner.TunerStorage — LC-learning persistence.

Hardware-independent: each test uses a TemporaryDirectory for the store
file.  Covers the learn gate, needs_verify flag, overwrite policy,
find_best matching, tune params, persistence round-trip, atomic save,
and delete/clear/get_stats.
"""
import json
import os
import tempfile
import unittest

from atr1000_tuner import TunerStorage, get_storage, SWR_LEARN_MAX, SWR_LEARN_MIN


class TunerStorageTestBase(unittest.TestCase):
    """Base class providing a store in a fresh temp directory."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.store_path = os.path.join(self.tmpdir.name, 'atr1000_tuner.json')
        self.storage = TunerStorage(storage_file=self.store_path)


class LearnGateTests(TunerStorageTestBase):
    """learn() only records when SWR_LEARN_MIN <= swr <= SWR_LEARN_MAX
    and the LC parameters are not both zero."""

    def test_rejects_swr_above_max(self):
        self.assertFalse(self.storage.learn(7_053_000, 0, 45, 32, SWR_LEARN_MAX + 0.1))
        self.assertEqual(self.storage.get_all(), [])

    def test_rejects_swr_below_min(self):
        self.assertFalse(self.storage.learn(7_053_000, 0, 45, 32, SWR_LEARN_MIN - 0.05))
        self.assertEqual(self.storage.get_all(), [])

    def test_accepts_swr_at_boundaries(self):
        self.assertTrue(self.storage.learn(7_053_000, 0, 45, 32, SWR_LEARN_MIN))
        self.assertTrue(self.storage.learn(7_100_000, 0, 45, 32, SWR_LEARN_MAX))
        self.assertEqual(len(self.storage.get_all()), 2)

    def test_rejects_zero_ind_and_cap(self):
        self.assertFalse(self.storage.learn(7_053_000, 0, 0, 0, 1.20))
        self.assertEqual(self.storage.get_all(), [])

    def test_rejects_zero_ind_and_cap_even_with_force_update(self):
        self.assertFalse(self.storage.learn(7_053_000, 0, 0, 0, 1.20, force_update=True))
        self.assertEqual(self.storage.get_all(), [])

    def test_force_update_bypasses_swr_gate(self):
        self.assertTrue(self.storage.learn(7_053_000, 0, 45, 32, 2.50, force_update=True))
        self.assertEqual(len(self.storage.get_all()), 1)


class NeedsVerifyTests(TunerStorageTestBase):
    """SWR < 1.00 is physically impossible and flags needs_verify."""

    def test_needs_verify_set_for_swr_below_1(self):
        # Only reachable via force_update, since learn() rejects swr < 1.0
        self.storage.learn(7_053_000, 0, 45, 32, 0.95, force_update=True)
        record = self.storage.find_best(7_053_000)
        self.assertTrue(record['needs_verify'])

    def test_needs_verify_clear_at_exactly_1(self):
        self.storage.learn(7_053_000, 0, 45, 32, 1.00)
        record = self.storage.find_best(7_053_000)
        self.assertFalse(record['needs_verify'])


class OverwritePolicyTests(TunerStorageTestBase):
    """Relay params are overwritten only when the new SWR beats the
    historical average, or when force_update is set."""

    def test_better_swr_overwrites_params(self):
        self.storage.learn(7_053_000, 0, 45, 32, 1.20)
        self.storage.learn(7_053_000, 1, 50, 40, 1.10)
        record = self.storage.find_best(7_053_000)
        self.assertEqual((record['sw'], record['ind'], record['cap']), (1, 50, 40))

    def test_worse_swr_keeps_params(self):
        self.storage.learn(7_053_000, 0, 45, 32, 1.10)
        self.storage.learn(7_053_000, 1, 50, 40, 1.30)
        record = self.storage.find_best(7_053_000)
        self.assertEqual((record['sw'], record['ind'], record['cap']), (0, 45, 32))

    def test_worse_swr_still_updates_stats(self):
        self.storage.learn(7_053_000, 0, 45, 32, 1.10)
        self.storage.learn(7_053_000, 1, 50, 40, 1.30)
        record = self.storage.find_best(7_053_000)
        self.assertEqual(record['sample_count'], 2)
        self.assertAlmostEqual(record['swr_avg'], 1.20)
        self.assertAlmostEqual(record['swr_min'], 1.10)
        self.assertAlmostEqual(record['swr_max'], 1.30)

    def test_force_update_overwrites_despite_worse_swr(self):
        self.storage.learn(7_053_000, 0, 45, 32, 1.10)
        self.storage.learn(7_053_000, 1, 50, 40, 1.50, force_update=True)
        record = self.storage.find_best(7_053_000)
        self.assertEqual((record['sw'], record['ind'], record['cap']), (1, 50, 40))


class FindBestTests(TunerStorageTestBase):
    """find_best(): exact 1kHz key, else nearest within ±5kHz, else None."""

    def setUp(self):
        super().setUp()
        self.storage.learn(7_053_000, 0, 45, 32, 1.15)

    def test_exact_match(self):
        record = self.storage.find_best(7_053_000)
        self.assertIsNotNone(record)
        self.assertEqual(record['freq'], 7_053_000)

    def test_exact_match_same_khz_bucket(self):
        record = self.storage.find_best(7_053_700)
        self.assertIsNotNone(record)
        self.assertEqual(record['freq'], 7_053_000)

    def test_nearest_within_tolerance(self):
        record = self.storage.find_best(7_057_000)  # 4 kHz away
        self.assertIsNotNone(record)
        self.assertEqual(record['freq'], 7_053_000)

    def test_at_tolerance_boundary(self):
        record = self.storage.find_best(7_058_000)  # exactly 5 kHz away
        self.assertIsNotNone(record)
        self.assertEqual(record['freq'], 7_053_000)

    def test_outside_tolerance_returns_none(self):
        self.assertIsNone(self.storage.find_best(7_059_000))  # 6 kHz away
        self.assertIsNone(self.storage.find_best(14_000_000))

    def test_nearest_record_wins(self):
        self.storage.learn(7_060_000, 1, 50, 40, 1.20)
        record = self.storage.find_best(7_058_000)  # 2 kHz from 7060, 5 from 7053
        self.assertEqual(record['freq'], 7_060_000)

    def test_empty_store_returns_none(self):
        self.storage.clear()
        self.assertIsNone(self.storage.find_best(7_053_000))


class TuneParamsTests(TunerStorageTestBase):
    """get_tune_params() returns the (sw, ind, cap) tuple."""

    def test_tuple_shape_and_values(self):
        self.storage.learn(7_053_000, 1, 45, 32, 1.15)
        params = self.storage.get_tune_params(7_053_000)
        self.assertIsInstance(params, tuple)
        self.assertEqual(len(params), 3)
        self.assertEqual(params, (1, 45, 32))

    def test_returns_none_for_unknown_freq(self):
        self.assertIsNone(self.storage.get_tune_params(14_000_000))

    def test_params_come_from_best_record(self):
        self.storage.learn(7_053_000, 0, 45, 32, 1.20)
        self.storage.learn(7_053_000, 1, 60, 55, 1.10)  # better → overwrite
        self.assertEqual(self.storage.get_tune_params(7_053_000), (1, 60, 55))


class PersistenceTests(TunerStorageTestBase):
    """JSON schema and save/load round-trip."""

    def test_round_trip(self):
        self.storage.learn(7_053_000, 0, 45, 32, 1.15)
        self.storage.learn(14_200_000, 1, 60, 50, 1.25)

        reloaded = TunerStorage(storage_file=self.store_path)
        self.assertEqual(len(reloaded.get_all()), 2)
        record = reloaded.find_best(7_053_000)
        self.assertEqual(record['sw'], 0)
        self.assertEqual(record['ind'], 45)
        self.assertEqual(record['cap'], 32)
        self.assertAlmostEqual(record['swr_avg'], 1.15)

    def test_json_schema_version_and_records_list(self):
        self.storage.learn(7_053_000, 0, 45, 32, 1.15)
        with open(self.store_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        self.assertEqual(raw['version'], '2.0')
        self.assertIsInstance(raw['records'], list)
        self.assertEqual(len(raw['records']), 1)
        for field in ('freq', 'sw', 'ind', 'cap', 'swr_avg', 'swr_min',
                      'swr_max', 'sample_count', 'last_update'):
            self.assertIn(field, raw['records'][0])

    def test_load_missing_file_starts_empty(self):
        missing = os.path.join(self.tmpdir.name, 'does_not_exist.json')
        storage = TunerStorage(storage_file=missing)
        self.assertEqual(storage.get_all(), [])

    def test_load_corrupt_file_starts_empty(self):
        with open(self.store_path, 'w', encoding='utf-8') as f:
            f.write('not json{')
        storage = TunerStorage(storage_file=self.store_path)
        self.assertEqual(storage.get_all(), [])

    def test_atomic_save_leaves_no_temp_file(self):
        self.storage.learn(7_053_000, 0, 45, 32, 1.15)
        self.storage.delete(7_053_000)
        leftovers = [name for name in os.listdir(self.tmpdir.name)
                     if name.endswith('.tmp')]
        self.assertEqual(leftovers, [])
        self.assertEqual(os.listdir(self.tmpdir.name), ['atr1000_tuner.json'])


class DeleteClearStatsTests(TunerStorageTestBase):
    """delete(), clear(), get_all(), get_stats()."""

    def setUp(self):
        super().setUp()
        self.storage.learn(7_053_000, 0, 45, 32, 1.10)
        self.storage.learn(14_200_000, 1, 60, 50, 1.30)

    def test_delete_existing(self):
        self.assertTrue(self.storage.delete(7_053_000))
        self.assertIsNone(self.storage.find_best(7_053_000))
        self.assertEqual(len(self.storage.get_all()), 1)

    def test_delete_missing_returns_false(self):
        self.assertFalse(self.storage.delete(3_500_000))
        self.assertEqual(len(self.storage.get_all()), 2)

    def test_delete_persists(self):
        self.storage.delete(7_053_000)
        reloaded = TunerStorage(storage_file=self.store_path)
        self.assertEqual(len(reloaded.get_all()), 1)
        self.assertIsNone(reloaded.find_best(7_053_000))

    def test_clear(self):
        self.storage.clear()
        self.assertEqual(self.storage.get_all(), [])
        reloaded = TunerStorage(storage_file=self.store_path)
        self.assertEqual(reloaded.get_all(), [])

    def test_get_all_sorted_by_freq(self):
        records = self.storage.get_all()
        self.assertEqual([r['freq'] for r in records], [7_053_000, 14_200_000])

    def test_get_stats(self):
        stats = self.storage.get_stats()
        self.assertEqual(stats['count'], 2)
        self.assertAlmostEqual(stats['swr_avg'], 1.20)
        self.assertAlmostEqual(stats['swr_min'], 1.10)
        self.assertAlmostEqual(stats['swr_max'], 1.30)

    def test_get_stats_empty(self):
        self.storage.clear()
        self.assertEqual(self.storage.get_stats(), {'count': 0})


class SingletonTests(unittest.TestCase):
    """Module-level get_storage() singleton."""

    def test_get_storage_returns_same_instance(self):
        self.assertIs(get_storage(), get_storage())

    def test_get_storage_is_tuner_storage(self):
        self.assertIsInstance(get_storage(), TunerStorage)


if __name__ == '__main__':
    unittest.main()
