"""Tests for the read-only Yaesu field diagnostic (spec §7)."""
import unittest

import _diag_yaesu as diag


class IdentityEvaluationTests(unittest.TestCase):
    def test_match(self):
        ok, note = diag.evaluate_identity("0840", "0840", "Yaesu FTX-1F")
        self.assertTrue(ok)
        self.assertIn("matches", note)

    def test_mismatch_names_both_values(self):
        ok, note = diag.evaluate_identity("0810", "0840", "Yaesu FTX-1F")
        self.assertFalse(ok)
        self.assertIn("0810", note)
        self.assertIn("0840", note)

    def test_no_expectation_is_not_a_failure(self):
        ok, note = diag.evaluate_identity("0810", "", "Yaesu FTDX10")
        self.assertTrue(ok)
        self.assertIn("no expectation", note)

    def test_silent_radio_is_reported(self):
        ok, note = diag.evaluate_identity(None, "0840", "Yaesu FTX-1F")
        self.assertFalse(ok)
        self.assertIn("no answer", note)


class TxCheckTests(unittest.TestCase):
    def test_tx_check_requires_the_opt_in(self):
        self.assertFalse(diag.tx_check_permitted(True, False))
        self.assertFalse(diag.tx_check_permitted(False, True))
        self.assertTrue(diag.tx_check_permitted(True, True))


class ReportTests(unittest.TestCase):
    def test_report_contains_the_model_and_observed_values(self):
        report = diag.format_report(
            model="ftx1", display_name="Yaesu FTX-1F", port="/dev/ttyUSB0",
            baud=38400, model_id="0840", identity_ok=True,
            identity_note="model ID matches the profile",
            probes=[("FA", "FA14074000", True), ("MD0", "MD02", True)],
            tx_check="skipped (needs --tx-check --allow-tx)",
            notes=["audio rate assumption: TODO(hw-verify)"])
        self.assertIn("Yaesu FTX-1F", report)
        self.assertIn("0840", report)
        self.assertIn("FA14074000", report)
        self.assertIn("| command | answer | ok |", report)

    def test_report_flags_failed_probes(self):
        report = diag.format_report(
            model="ftdx10", display_name="Yaesu FTDX10", port="/dev/null",
            baud=38400, model_id=None, identity_ok=False,
            identity_note="no answer to ID;", probes=[("FA", None, False)],
            tx_check="not requested", notes=[])
        self.assertIn("FAIL", report)
