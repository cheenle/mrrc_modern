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

from ft8.core.models import DecodeConfig
from ft8.core.protocol import _CONFIG_FIELDS, _RESULT_FIELDS
from tools.ft8 import ap_probe


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
