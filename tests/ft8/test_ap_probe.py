"""The AP probe's research contract, testable without a radio or a corpus.

``tools/ft8/ap_probe.py`` exists to answer one question: does feeding the
decoder the a-priori it is already entitled to buy recall at the cliff?  Three
things have to stay true for its answer to mean anything, and all three are
pure enough to test here:

1. The ``ap_starved`` arm must reproduce what production actually sends.  If it
   drifts, the probe measures a configuration nobody runs.
2. The a8 arming condition must be mirrored exactly.  ``ltry_a8`` in
   ``ft8/dsp/wsjt_improved.f90:178`` is the reason AP is "on but starved", and
   the probe's whole framing rests on it.
3. AP decodes must be *marked* in the result schema.  The precision argument
   (an AP decode is a hypothesis test, so a wrong a-priori could invent a
   message) is only manageable if the caller can tell an AP decode apart.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from ft8.core.models import DecodeConfig
from ft8.core.protocol import _CONFIG_FIELDS, _RESULT_FIELDS
from tools.ft8 import ap_probe

REPO_ROOT = Path(__file__).resolve().parents[2]


class ApProbeArmTests(unittest.TestCase):
    def test_starved_arm_is_exactly_what_production_sends(self) -> None:
        """``runtime.py`` builds ``DecodeConfig(profile=…, threads=…)`` and nothing else.

        The probe's ``ap_starved`` arm has to be that same object, otherwise the
        comparison is against a configuration no station runs.
        """

        production = DecodeConfig(
            **{k: v for k, v in ap_probe.BASE.items() if k in ("profile", "threads")}
        )
        starved_overrides = dict(next(o for l, _n, o in ap_probe.ARMS if l == "ap_starved"))
        starved = ap_probe._config(**starved_overrides)

        for field in ("ap", "my_call", "dx_call", "dx_grid", "qso_progress", "rx_frequency"):
            self.assertEqual(
                getattr(starved, field),
                getattr(production, field),
                f"ap_starved disagrees with production on {field}",
            )

    def test_ap_is_on_by_default_but_has_no_a_priori(self) -> None:
        """The precise statement of the finding: not "AP is off", but "AP is starved"."""

        config = DecodeConfig()
        self.assertTrue(config.ap, "ap defaults to True -- the flag is not the problem")
        self.assertEqual(config.my_call, "")
        self.assertEqual(config.dx_call, "")
        self.assertEqual(config.dx_grid, "")
        self.assertEqual(config.qso_progress, 0)

    def test_fed_arm_supplies_every_field_the_a8_gate_reads(self) -> None:
        fed_overrides = dict(next(o for l, _n, o in ap_probe.ARMS if l == "ap_fed"))
        fed = ap_probe._config(**fed_overrides)
        self.assertTrue(fed.ap)
        self.assertGreaterEqual(len(fed.dx_call.strip()), 3)
        self.assertGreaterEqual(len(fed.dx_grid.strip()), 4)

    def test_off_arm_really_turns_ap_off(self) -> None:
        off_overrides = dict(next(o for l, _n, o in ap_probe.ARMS if l == "ap_off"))
        self.assertFalse(ap_probe._config(**off_overrides).ap)

    def test_the_three_arms_are_distinct(self) -> None:
        labels = [label for label, _note, _o in ap_probe.ARMS]
        self.assertEqual(labels, ["ap_off", "ap_starved", "ap_fed"])


class A8ArmingGateTests(unittest.TestCase):
    """Mirror of ``ltry_a8 = ap_enabled .and. len_trim(hiscall)>=3 .and. len_trim(hisgrid4)>=4``.

    Kept as a Python mirror on purpose: it is the single condition that decides
    whether the one AP path able to find a *new* signal runs at all.  If the
    Fortran gate ever changes, this test is the reminder to re-read it.
    """

    @staticmethod
    def ltry_a8(config: DecodeConfig) -> bool:
        return config.ap and len(config.dx_call.strip()) >= 3 and len(config.dx_grid.strip()) >= 4

    def test_production_config_never_arms_a8(self) -> None:
        self.assertFalse(self.ltry_a8(DecodeConfig()))

    def test_fed_config_arms_a8(self) -> None:
        fed = DecodeConfig(ap=True, dx_call=ap_probe.DX_CALL, dx_grid=ap_probe.DX_GRID)
        self.assertTrue(self.ltry_a8(fed))

    def test_a_short_grid_does_not_arm_a8(self) -> None:
        """``hisgrid4`` needs four characters -- a two-letter grid is not enough."""

        self.assertFalse(
            self.ltry_a8(DecodeConfig(ap=True, dx_call=ap_probe.DX_CALL, dx_grid="QN"))
        )

    def test_ap_off_never_arms_a8_even_with_calls(self) -> None:
        self.assertFalse(
            self.ltry_a8(
                DecodeConfig(ap=False, dx_call=ap_probe.DX_CALL, dx_grid=ap_probe.DX_GRID)
            )
        )


class ApResultSchemaTests(unittest.TestCase):
    def test_ap_decodes_are_marked_in_the_result_schema(self) -> None:
        """The precision argument requires callers to tell an AP decode apart."""

        self.assertIn("ap_type", _RESULT_FIELDS)
        self.assertIn("quality", _RESULT_FIELDS)

    def test_every_a_priori_field_is_part_of_the_request_schema(self) -> None:
        """Feeding AP must not require a protocol change -- the fields already exist."""

        for field in (
            "ap",
            "ap_width",
            "my_call",
            "dx_call",
            "dx_grid",
            "qso_progress",
            "rx_frequency",
            "utc_hhmmss",
        ):
            self.assertIn(field, _CONFIG_FIELDS, f"{field} missing from the config schema")


if __name__ == "__main__":
    unittest.main()


class A8HypothesisSpaceTests(unittest.TestCase):
    """Mirror of getmsg() (vendor/ft8/ft8_a7.f90:377) and a8's lag window.

    The first probe found no AP gain, and reading the source explained why: it
    scattered dt over [0, 2.5] s while ft8_a8d only searches lag in [-200, +200]
    at its internal 200 Hz rate with ``j = i + lag + 100`` -- a reach of
    dt in [-0.5, +1.5] s.  These tests keep the paired probe inside a8's actual
    hypothesis space, so a null result can no longer be explained by the probe
    asking a question a8 was never able to answer.
    """

    @staticmethod
    def getmsg(i: int, mycall: str, hiscall: str, hisgrid: str) -> str:
        """Python mirror of the Fortran getmsg, including the abs(isnr)>30 drop.

        Fortran ``write(msg(j0:j0+2),'(i3.2)') isnr`` renders -12 as "-12" and +5
        as " 05", whose leading space the next line turns into "+05".  The even
        indices get the same three characters behind an "R".
        """

        base = f"{mycall} {hiscall}"
        if i == 1:
            return base
        if i == 2:
            return f"{base} RRR"
        if i == 3:
            return f"{base} RR73"
        if i == 4:
            return f"{base} 73"
        if i == 5:
            return f"CQ {hiscall} {hisgrid[:4]}"
        if i == 6:
            return f"{base} {hisgrid[:4]}"
        if 7 <= i <= 206:
            isnr = -50 + (i - 7) // 2  # Fortran integer division
            if abs(isnr) > 30:
                return ""
            report = f"{'+' if isnr >= 0 else '-'}{abs(isnr):02d}"
            return f"{base} {report}" if i % 2 == 1 else f"{base} R{report}"
        return base

    def test_the_probed_message_is_in_a8_hypothesis_space(self) -> None:
        """The probe must synthesize a message a8 is actually able to return."""

        generated = {
            self.getmsg(i, ap_probe.MY_CALL, ap_probe.DX_CALL, ap_probe.DX_GRID)
            for i in range(1, 207)
        }
        generated.discard("")
        self.assertIn(ap_probe.A8_MESSAGE, generated)

    def test_the_hypothesis_space_is_about_seven_bits(self) -> None:
        """log2(~127) ~ 7.0 bits -- the number the speculative notes cite."""

        import math

        generated = {
            self.getmsg(i, ap_probe.MY_CALL, ap_probe.DX_CALL, ap_probe.DX_GRID)
            for i in range(1, 207)
        }
        generated.discard("")
        self.assertGreater(len(generated), 100, "expected the report ladder to dominate")
        self.assertLess(len(generated), 207, "abs(isnr)>30 must drop part of the ladder")
        self.assertAlmostEqual(math.log2(len(generated)), 7.0, delta=0.4)

    def test_the_probe_stays_inside_a8_lag_window(self) -> None:
        """lag in [-200, +200] at 200 Hz with a +100 sample offset => dt in [-0.5, +1.5]."""

        low = (-200 + 100) / 200.0
        high = (200 + 100) / 200.0
        self.assertAlmostEqual(low, -0.5)
        self.assertAlmostEqual(high, 1.5)
        self.assertLessEqual(ap_probe.A8_DT_MAX, high)
        self.assertGreaterEqual(ap_probe.A8_DT_MAX, 1.0, "keep most of the window usable")

    def test_the_first_probe_was_blind_to_part_of_its_own_trials(self) -> None:
        """Document the flaw this mode exists to fix: DT_MAX exceeds a8's reach."""

        from tools.ft8.ml_simulate import DT_MAX

        self.assertGreater(DT_MAX, 1.5, "the unpaired probe could place dt outside a8")


class ApProbeCliTests(unittest.TestCase):
    """The CLI has to be importable *and* runnable -- the tests above only prove the first.

    A real failure this catches: appending a new function below the
    ``if __name__ == "__main__"`` guard makes every unit test pass while the
    script itself dies with NameError, because the guard runs before the
    definition is executed.  ``--help`` exercises the same top-to-bottom path
    without needing a corpus or the native library.
    """

    def test_the_script_runs_standalone(self) -> None:
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "tools/ft8/ap_probe.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        self.assertIn("--paired", result.stdout)

    def test_the_main_guard_is_the_last_statement(self) -> None:
        source = (REPO_ROOT / "tools" / "ft8" / "ap_probe.py").read_text()
        self.assertTrue(
            source.rstrip().endswith('if __name__ == "__main__":\n    raise SystemExit(main())'),
            "the main guard must stay at the end, or appended helpers are undefined at run time",
        )


class ProbeMatchingTests(unittest.TestCase):
    """A hit means *this* signal, not merely something in its neighbourhood.

    The first paired run reported 4/20 recovered at -30 dB, which is impossible
    for a signal that deep: the probe had hand-written a 25 Hz / 1.5 s window
    (ml_simulate validates 15 Hz / 0.25 s), so other stations in the real bed
    were credited to the synthesized signal.  These tests pin the criterion to
    the text, using a fake batch -- no radio, no corpus, no native library.
    """

    class _Result:
        def __init__(self, text, frequency=1000.0, dt=0.5, ap_type=0, quality=1.0):
            self.text = text
            self.frequency = frequency
            self.dt = dt
            self.ap_type = ap_type
            self.quality = quality

    class _Batch:
        def __init__(self, *results):
            self.results = list(results)

    def setUp(self):
        self.signal = ap_probe.SyntheticSignal(
            message=ap_probe.A8_MESSAGE, frequency=1000.0, dt=0.5, snr_db=-26.0
        )

    def test_exact_text_at_the_right_place_is_a_hit(self) -> None:
        hit, ap_type, _ = ap_probe._hit(
            self._Batch(self._Result(ap_probe.A8_MESSAGE, ap_type=8)), self.signal
        )
        self.assertTrue(hit)
        self.assertEqual(ap_type, 8)

    def test_a_neighbouring_report_is_not_a_hit(self) -> None:
        """a8 can return "-14" for "-12" and still pass CRC -- that is not a decode."""

        hit, _, _ = ap_probe._hit(
            self._Batch(self._Result("BA4EG JA1VAT -14", ap_type=8)), self.signal
        )
        self.assertFalse(hit)

    def test_another_station_in_the_window_is_not_a_hit(self) -> None:
        hit, _, _ = ap_probe._hit(
            self._Batch(self._Result("CQ VK3ABC QF22", frequency=1005.0, dt=0.6)),
            self.signal,
        )
        self.assertFalse(hit)

    def test_the_wrong_text_detector_names_the_offender(self) -> None:
        bad = ap_probe._wrong_text_at_signal(
            self._Batch(self._Result("BA4EG JA1VAT -14", ap_type=8)), self.signal
        )
        self.assertEqual(bad, "BA4EG JA1VAT -14")

    def test_the_wrong_text_detector_is_quiet_on_a_clean_slot(self) -> None:
        self.assertEqual(
            ap_probe._wrong_text_at_signal(self._Batch(self._Result(ap_probe.A8_MESSAGE)), self.signal),
            "",
        )

    def test_tolerances_come_from_the_validated_module(self) -> None:
        """Never hand-write these again: 1.5 s against a validated 0.25 s is 6x too loose."""

        from tools.ft8.ml_simulate import DT_TOLERANCE, FREQUENCY_TOLERANCE

        self.assertEqual(ap_probe.DT_TOLERANCE, DT_TOLERANCE)
        self.assertEqual(ap_probe.FREQUENCY_TOLERANCE, FREQUENCY_TOLERANCE)
        self.assertLessEqual(DT_TOLERANCE, 0.25)
        source = (REPO_ROOT / "tools" / "ft8" / "ap_probe.py").read_text()
        self.assertNotIn("<= 25.0", source, "hand-written frequency tolerance is back")
        self.assertNotIn("<= 1.5\n", source, "hand-written dt tolerance is back")
