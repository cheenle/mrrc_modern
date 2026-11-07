"""Pure-helper tests for the CI-V diagnostic script (spec §7).

The serial path itself is only exercised on hardware; everything that
decides what the report says is testable here.
"""
import unittest

from _diag_civ import (
    ScopeStats, evaluate_identity, format_report, summarize_scope,
)


class ScopeStatsTests(unittest.TestCase):
    def test_summarize_counts_lengths_amplitudes_and_segments(self):
        samples = [
            {"bins": [0, 160] * 40, "seq_max": 11},
            {"bins": [0, 200] * 40, "seq_max": 11},
            {"bins": [10] * 80, "seq_max": 11},
        ]
        stats = summarize_scope(samples)
        self.assertEqual(stats.waveforms, 3)
        self.assertEqual(stats.min_bins, 80)
        self.assertEqual(stats.max_bins, 80)
        self.assertEqual(stats.amp_min, 0)
        self.assertEqual(stats.amp_max, 200)
        self.assertEqual(stats.seq_max, 11)
        self.assertIn(200, stats.amplitude_histogram)

    def test_summarize_empty_is_explicit(self):
        stats = summarize_scope([])
        self.assertEqual(stats.waveforms, 0)
        self.assertIsNone(stats.max_bins)
        self.assertIsNone(stats.amp_max)
        self.assertIsNone(stats.seq_max)

    def test_summarize_689_bin_profile(self):
        samples = [{"bins": list(range(200)), "seq_max": 15}] * 3
        stats = summarize_scope(samples)
        self.assertEqual(stats.waveforms, 3)
        self.assertEqual(stats.max_bins, 200)
        self.assertEqual(stats.seq_max, 15)


class IdentityEvaluationTests(unittest.TestCase):
    def test_no_expectation_is_unknown(self):
        verdict, text = evaluate_identity(bytes((0xA4,)), None)
        self.assertEqual(verdict, "unknown")
        self.assertIn("A4", text)

    def test_match_and_mismatch(self):
        self.assertEqual(evaluate_identity(bytes((0xA4,)), (0xA4,))[0],
                         "match")
        verdict, text = evaluate_identity(bytes((0xB2,)), (0xA4,))
        self.assertEqual(verdict, "mismatch")
        self.assertIn("B2", text)

    def test_no_answer(self):
        self.assertEqual(evaluate_identity(None, (0xA4,))[0], "no-answer")
        self.assertEqual(evaluate_identity(None, None)[0], "no-answer")


class ReportFormattingTests(unittest.TestCase):
    def test_report_contains_measured_facts_and_boundary(self):
        stats = summarize_scope([{"bins": [5] * 475, "seq_max": 11}])
        report = format_report(
            model_key="ic705", display_name="Icom IC-705", port="/dev/null",
            baud=115200, civ_addr=0xA4, identity_text="unknown (no answer)",
            probe_results=[("frequency", "14074000")],
            scope_stats=stats, meter_results=[("po", 0)],
            tx_check="skipped (needs --tx-check --allow-tx)",
            profile_bins=475, profile_amp_max=160, profile_seq_max=11,
        )
        self.assertIn("ic705", report)
        self.assertIn("475", report)
        self.assertIn("scope_bins", report)
        self.assertIn("TX check", report)
        self.assertIn("14074000", report)
        # The report must state how to feed measurements back into the repo.
        self.assertIn("civ_profiles.py", report)

    def test_report_marks_profile_mismatch_as_actionable(self):
        stats = summarize_scope([{"bins": [5] * 689, "seq_max": 15}])
        report = format_report(
            model_key="ic7610", display_name="Icom IC-7610", port="COM5",
            baud=115200, civ_addr=0x98, identity_text="mismatch",
            probe_results=[], scope_stats=stats, meter_results=[],
            tx_check="PASS (PTT released and read back as RX)",
            profile_bins=475, profile_amp_max=160, profile_seq_max=11,
        )
        self.assertIn("689", report)
        self.assertIn("15", report)


class ArgumentParsingTests(unittest.TestCase):
    def test_model_choice_accepts_registry_keys(self):
        from backends.ic7300.civ_profiles import PROFILES
        from _diag_civ import build_parser
        parser = build_parser()
        args = parser.parse_args(["--model", "ic7760", "--port", "/dev/null"])
        self.assertEqual(args.model, "ic7760")
        self.assertEqual(sorted(PROFILES), ["ic705", "ic7300", "ic7300mk2",
                                            "ic7610", "ic7760"])

    def test_model_choice_covers_the_xiegu_family(self):
        """The G7250's facts live in its own registry, not the Icom one.

        Before this, `--model g7250` was rejected by argparse — the first
        hardware session would have had no instrument at all.
        """
        from _diag_civ import NO_CIV_SCOPE_MODELS, PROFILE_MODULE, build_parser
        parser = build_parser()
        args = parser.parse_args(["--model", "g7250", "--port", "/dev/null"])
        self.assertEqual(args.model, "g7250")
        self.assertFalse(args.no_scope)
        # No CI-V 0x27 on this radio: the scope half must skip itself.
        self.assertIn("g7250", NO_CIV_SCOPE_MODELS)
        # And the report must name the Xiegu profile module, not civ_profiles.
        self.assertEqual(PROFILE_MODULE["g7250"],
                         "backends/xiegu/xiegu_profiles.py")
        self.assertEqual(PROFILE_MODULE["ic7300"],
                         "backends/ic7300/civ_profiles.py")

    def test_address_probe_mirrors_the_backend(self):
        """An unpinned run walks the same addresses the server does.

        The G7250's address is unpublished, so a tool that only ever tried
        `profile.civ_addr` would report "no answer" on a radio that answers
        fine on 0x88 — and the first hardware session would be wasted.
        """
        from _diag_civ import address_candidates, get_profile
        self.assertEqual(address_candidates(get_profile("g7250"), None),
                         (0xA4, 0x88, 0x70))
        # A pinned address is the only attempt.
        self.assertEqual(address_candidates(get_profile("g7250"), 0x70), (0x70,))
        # Icom models keep their single profile address.
        self.assertEqual(address_candidates(get_profile("ic7300"), None), (0x94,))

    def test_no_scope_flag_is_accepted(self):
        from _diag_civ import build_parser
        args = build_parser().parse_args(
            ["--model", "ic7300", "--port", "/dev/null", "--no-scope"])
        self.assertTrue(args.no_scope)

    def test_tx_check_requires_allow_tx(self):
        from _diag_civ import tx_check_permitted
        self.assertFalse(tx_check_permitted(tx_check=True, allow_tx=False))
        self.assertFalse(tx_check_permitted(tx_check=False, allow_tx=True))
        self.assertTrue(tx_check_permitted(tx_check=True, allow_tx=True))


if __name__ == "__main__":
    unittest.main()
