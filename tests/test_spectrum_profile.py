"""Spectrum wire-frame profile tiering (SDD AD-025).

A profile is two orthogonal factors — frame *shape* (1701 B full vs 851 B
wf1-only) and *frame-rate divider* — so each named tier roughly halves the
previous one on BOTH the payload and the on-the-wire axis.

Design: docs/superpowers/specs/2026-10-07-spectrum-bandwidth-profiles-design.md
"""

import json
import unittest

import spectrum_profile as sp


def _full_frame(wf1: bytes = b"\x11" * 850, wf2: bytes = b"\x00" * 850) -> bytes:
    return bytes([sp.WIRE_VERSION]) + wf1 + wf2


class ProfileTableTests(unittest.TestCase):
    def test_named_tiers_halve_payload_each_step(self):
        """high -> mid -> low must be 1/4 then 1/8 of high (payload axis)."""
        variants = sp.build_variants(_full_frame())

        def kbps(name: str) -> float:      # at 11 fps, the measured scope rate
            p = sp.PROFILES[name]
            return len(variants[p.shape]) * 11 / p.divider * 8 / 1000

        hi, mid, low = kbps("high"), kbps("mid"), kbps("low")
        # 1701 B x 11 fps x 8 = 149.7 kbps; the 150.8 kbps in the server log is
        # the same arithmetic at the measured 11.09 fps.  mid and low halve it
        # twice over because BOTH factors apply: 851 B *and* fewer ticks —
        # dropping the divider from this helper makes mid read as 1/2, not 1/4.
        self.assertAlmostEqual(hi, 149.7, delta=0.5)
        self.assertAlmostEqual(mid, 37.4, delta=0.5)
        self.assertAlmostEqual(low, 18.7, delta=0.5)
        self.assertAlmostEqual(mid / hi, 0.25, delta=0.01)
        self.assertAlmostEqual(low / hi, 0.125, delta=0.01)

    def test_shape_and_divider_of_every_tier(self):
        self.assertEqual((sp.PROFILES["high"].shape, sp.PROFILES["high"].divider),
                         (sp.SHAPE_FULL, 1))
        self.assertEqual((sp.PROFILES["mid"].shape, sp.PROFILES["mid"].divider),
                         (sp.SHAPE_WF1, 2))
        self.assertEqual((sp.PROFILES["low"].shape, sp.PROFILES["low"].divider),
                         (sp.SHAPE_WF1, 4))
        self.assertEqual((sp.PROFILES["listen"].shape, sp.PROFILES["listen"].divider),
                         (sp.SHAPE_FULL, 3))

    def test_defaults_and_client_whitelist(self):
        self.assertEqual(sp.DEFAULT_PROFILE, "high")
        self.assertEqual(sp.LISTEN_PROFILE, "listen")
        # "listen" is the server-side default for listener-password sockets
        # (today's /3 behaviour); a client must not be able to name it.
        self.assertEqual(sp.CLIENT_PROFILES, ("high", "mid", "low"))

    def test_frame_lengths(self):
        self.assertEqual(sp.FULL_FRAME_BYTES, 1701)
        self.assertEqual(sp.SHORT_FRAME_BYTES, 851)
        self.assertEqual(sp.WIRE_VERSION, 0x01)

    def test_divider_for_and_shape_for_fall_back_to_high(self):
        """An unknown name degrades to the byte-for-byte-compatible default."""
        self.assertEqual(sp.divider_for("nope"), 1)
        self.assertEqual(sp.shape_for("nope"), sp.SHAPE_FULL)
        self.assertEqual(sp.divider_for("low"), 4)
        self.assertEqual(sp.shape_for("mid"), sp.SHAPE_WF1)


class FrameDueTests(unittest.TestCase):
    def test_divider_one_is_every_tick(self):
        self.assertTrue(all(sp.frame_due(t, 1) for t in range(1, 13)))

    def test_divider_three_matches_the_listener_gate_today(self):
        """Same due-tick pattern server._spectrum_frame_due has had since 1.25.2."""
        self.assertEqual([t for t in range(1, 10) if sp.frame_due(t, 3)], [3, 6, 9])

    def test_divider_four_is_every_fourth_tick(self):
        self.assertEqual([t for t in range(1, 13) if sp.frame_due(t, 4)], [4, 8, 12])

    def test_non_positive_divider_is_treated_as_no_throttle(self):
        self.assertTrue(sp.frame_due(1, 0))
        self.assertTrue(sp.frame_due(1, -3))


class BuildVariantsTests(unittest.TestCase):
    def test_short_frame_is_a_slice_of_the_full_frame(self):
        variants = sp.build_variants(_full_frame())
        self.assertEqual(len(variants[sp.SHAPE_FULL]), 1701)
        self.assertEqual(len(variants[sp.SHAPE_WF1]), 851)
        self.assertEqual(variants[sp.SHAPE_FULL][:851], variants[sp.SHAPE_WF1])
        self.assertEqual(variants[sp.SHAPE_WF1][0], sp.WIRE_VERSION)

    def test_variant_for_picks_the_tier_shape(self):
        variants = sp.build_variants(_full_frame())
        self.assertEqual(len(sp.variant_for(variants, "high")), 1701)
        self.assertEqual(len(sp.variant_for(variants, "mid")), 851)
        self.assertEqual(len(sp.variant_for(variants, "low")), 851)
        self.assertEqual(len(sp.variant_for(variants, "listen")), 1701)

    def test_variant_for_falls_back_to_full_when_shape_unavailable(self):
        """A backend that hands over a non-standard frame must not lose data."""
        odd = b"\x01" + b"\x22" * 300
        variants = sp.build_variants(odd)
        self.assertNotIn(sp.SHAPE_WF1, variants)
        self.assertEqual(sp.variant_for(variants, "low"), odd)

    def test_variant_for_never_returns_none(self):
        self.assertIsNotNone(sp.variant_for({sp.SHAPE_FULL: b"\x01"}, "mid"))


class ParseCapsTests(unittest.TestCase):
    def test_accepts_each_client_tier(self):
        for name in ("high", "mid", "low"):
            with self.subTest(name):
                self.assertEqual(
                    sp.parse_caps(json.dumps({"type": "spectrumCaps", "profile": name})),
                    name)

    def test_rejects_the_internal_listen_tier(self):
        self.assertIsNone(
            sp.parse_caps(json.dumps({"type": "spectrumCaps", "profile": "listen"})))

    def test_rejects_wrong_type_and_unknown_names(self):
        self.assertIsNone(sp.parse_caps(json.dumps({"type": "ping"})))
        self.assertIsNone(sp.parse_caps(json.dumps({"type": "spectrumCaps"})))
        self.assertIsNone(sp.parse_caps('{"type":"spectrumCaps","profile":"ultra"}'))
        self.assertIsNone(sp.parse_caps(json.dumps({"type": "spectrumCaps", "profile": 12})))

    def test_never_raises_on_garbage(self):
        """A malformed text frame is a keepalive today; it must stay harmless."""
        for junk in ("", "not json", "[1,2,3]", "null", '{"type":"spectrumCaps"',
                     b"\x01binary"):
            with self.subTest(repr(junk)):
                self.assertIsNone(sp.parse_caps(junk))


if __name__ == "__main__":
    unittest.main()
