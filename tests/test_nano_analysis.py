"""S11 分析层的纯数学测试（无设备、无 I/O、无 HTTP）。"""
import unittest

from nano import analysis as A


class SwrTest(unittest.TestCase):
    def test_matched_load_is_one(self):
        self.assertAlmostEqual(A.swr(0.0, 0.0), 1.0)

    def test_open_is_capped_not_infinite(self):
        self.assertEqual(A.swr(1.0, 0.0), A.SWR_CAP)

    def test_short_is_capped(self):
        self.assertEqual(A.swr(-1.0, 0.0), A.SWR_CAP)

    def test_three_to_one(self):
        # |Γ| = 0.5 -> SWR 3.0
        self.assertAlmostEqual(A.swr(0.5, 0.0), 3.0, places=6)

    def test_impedance_of_50_ohm_load(self):
        r, x = A.to_z(0.0, 0.0)
        self.assertAlmostEqual(r, 50.0, places=3)
        self.assertAlmostEqual(x, 0.0, places=3)

    def test_impedance_of_open_is_huge(self):
        self.assertGreater(A.to_z(0.999999, 0.0)[0], 1e5)


class ResonanceTest(unittest.TestCase):
    def test_plateau_centre_is_reported(self):
        freqs = [6.30e6, 6.42e6, 6.50e6, 6.58e6, 6.70e6]
        swrs = [2.40, 1.02, 1.01, 1.03, 1.90]
        got = A.resonances(freqs, swrs)
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0]["f_hz"], 6.54e6, delta=1e5)
        self.assertAlmostEqual(got[0]["swr_min"], 1.01, places=3)

    def test_lone_one_is_suspect_not_resonance(self):
        freqs = [7.00e6, 7.01e6, 7.02e6]
        swrs = [2.50, 1.00, 2.40]
        self.assertEqual(A.resonances(freqs, swrs), [])
        self.assertEqual(A.suspect_points(swrs), [1])

    def test_no_resonance_when_everything_is_high(self):
        freqs = [1e6, 2e6, 3e6]
        swrs = [3.0, 4.0, 5.0]
        self.assertEqual(A.resonances(freqs, swrs), [])


class ExportTest(unittest.TestCase):
    def test_csv_header_and_row(self):
        out = A.to_csv([7.0e6], [(0.2, -0.1)])
        lines = out.strip().splitlines()
        self.assertEqual(lines[0], "freq_hz,s11_re,s11_im,swr,r_ohm,x_ohm")
        self.assertTrue(lines[1].startswith("7000000.000000,0.200000,-0.100000,"))

    def test_s1p_is_touchstone_v1(self):
        out = A.to_s1p([7.0e6], [(0.2, -0.1)])
        self.assertIn("# Hz S RI R 50", out)
        self.assertIn("7000000.000000 0.200000 -0.100000", out)


class CompareTest(unittest.TestCase):
    def test_same_grid_delta(self):
        a = {"freqs": [1e6, 2e6], "swr": [2.0, 3.0]}
        b = {"freqs": [1e6, 2e6], "swr": [1.5, 4.0]}
        got = A.compare(a, b)
        self.assertEqual(got["delta_swr"], [-0.5, 1.0])
        self.assertEqual(got["worst_hz"], 2e6)
        self.assertAlmostEqual(got["worst_delta"], 1.0)

    def test_different_point_counts_are_resampled(self):
        a = {"freqs": [1e6, 2e6, 3e6], "swr": [1.0, 2.0, 3.0]}
        b = {"freqs": [1e6, 3e6], "swr": [1.0, 3.0]}
        got = A.compare(a, b)
        self.assertEqual(len(got["delta_swr"]), 3)
        self.assertAlmostEqual(got["delta_swr"][1], 0.0, places=6)


class AnalyseTest(unittest.TestCase):
    def test_payload_shape(self):
        got = A.analyse([1e6, 2e6], [(0.0, 0.0), (1.0, 0.0)])
        self.assertEqual(got["swr"][0], 1.0)
        self.assertEqual(got["swr"][1], A.SWR_CAP)
        self.assertEqual(len(got["z"]), 2)
        self.assertIn("resonances", got)
        self.assertIn("suspect", got)


if __name__ == "__main__":
    unittest.main()
