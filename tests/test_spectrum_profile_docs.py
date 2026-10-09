"""Doc-truth guards for the spectrum profile feature (SDD AD-025).

These pin the claims that were demonstrably false before this change.  Making
them tests is what stops the next reader from re-deriving the same wrong number:
"~51 KB/s", "~851 bytes/frame fallback" and "~10 Hz" all survived several
releases because nothing checked them, and a capacity decision (hub AD-H12) was
partly built on the 30 fps figure.

Design: docs/superpowers/specs/2026-10-07-spectrum-bandwidth-profiles-design.md §8
"""

import re
import unittest
from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


class SpectrumBandwidthClaimsTests(unittest.TestCase):
    def test_nfr003_states_both_axes_and_the_tiers(self):
        row = [ln for ln in _read("SDD/05-non-functional-requirements.md").splitlines()
               if ln.startswith("| NFR-003 ")][0]
        # "~30fps ... ~51KB/s" was the fallback state presented as the normal one,
        # and "~851 bytes/frame fallback" was never true at all (the fallback goes
        # through the same get_spectrum_binary(), so it is 1701 B too).
        self.assertNotIn("~30fps", row)
        self.assertNotIn("~851 bytes/frame fallback", row)
        self.assertIn("11", row)          # the measured real-scope rate
        self.assertIn("high", row)        # tiers are named
        self.assertIn("851", row)
        self.assertIn("1701", row)

    def test_frame_format_section_marks_v2_as_never_shipped(self):
        sec = _read("SDD/09-architecture-overview.md")
        sec = sec.split("### 9.2.4 /WSspectrum", 1)[1].split("###", 1)[0]
        self.assertIn("never shipped", sec)
        self.assertIn("spectrumCaps", sec)
        self.assertIn("0x01", sec)
        # The 0x02 re-tag must be recorded as forbidden, not merely absent:
        # iOS guards `version == 0x01` and would silently drop every frame.
        self.assertIn("0x02", sec)

    def test_listener_throttle_is_a_ratio_not_an_absolute_rate(self):
        sec = _read("SDD/09-architecture-overview.md")
        sec = sec.split("### 9.2.4 /WSspectrum", 1)[1].split("###", 1)[0]
        # The old text presented "~10 Hz" as *the* rate; it is only true in the
        # 30 Hz fallback state.  Both rates must appear with their condition.
        self.assertIn("3.7 Hz", sec)
        self.assertIn("ratio, not an absolute rate", sec)

    def test_no_unqualified_absolute_rate_claim_in_sdd(self):
        """Any '~10 Hz' mention must carry its condition on the same line.

        14-version-history.md is exempt: it is an immutable log and legitimately
        quotes the old figure inside a past release's entry.
        """
        for path in sorted(Path("SDD").glob("*.md")):
            if path.name.startswith("14-"):
                continue
            for n, line in enumerate(_read(str(path)).splitlines(), 1):
                if "~10 Hz" in line:
                    self.assertIn("fallback", line, f"{path}:{n}")

    def test_ad025_records_the_decision(self):
        dec = _read("SDD/08-architecture-decisions.md")
        self.assertIn("## AD-025", dec)
        block = dec.split("## AD-025", 1)[1]
        for needle in ("permessage-deflate", "OkHttp", "spectrumCaps",
                       "1701", "851", "151"):
            self.assertIn(needle, block)

    def test_ad023_carries_the_measured_correction(self):
        """AD-023 is a V2.62 record: annotate it, do not rewrite history.

        Its "408 kbps / 86%" figures were arithmetic on the 30 fps assumption.
        Deleting them would hide what the capacity decision was actually made
        from; leaving them unqualified would let someone re-derive them.  So the
        correction sits next to them.
        """
        dec = _read("SDD/08-architecture-decisions.md")
        block = dec.split("## AD-023", 1)[1].split("## AD-024", 1)[0]
        self.assertIn("AD-025", block)
        self.assertIn("151 kbps", block)
        self.assertIn("11.1 fps", block)

    def test_summary_and_context_pages_name_the_tiers(self):
        for path in ("SDD/01-executive-summary.md", "SDD/04-system-context.md"):
            text = _read(path)
            self.assertNotIn("v2=1701B", text, path)
            self.assertIn("AD-025", text, path)

    def test_service_model_row_is_not_a_fixed_rate(self):
        text = _read("SDD/10-service-model.md")
        self.assertNotIn("~30fps FT4222; ~10fps fallback", text)

    def test_pyinstaller_spec_names_the_new_module(self):
        """server.py imports it statically, so analysis would find it — name it
        anyway: v1.24.0 shipped without the cloud_hub import and every 云端申请
        answered 500 with NameError (see the comment in that spec)."""
        self.assertIn('"spectrum_profile"',
                      _read("packaging/pyinstaller/mrrc_modern_server.spec"))

    def test_agents_lists_the_new_module_and_the_real_test_filename(self):
        agents = _read("AGENTS.md")
        project_map = _read("docs/PROJECT_MAP.md")
        self.assertIn("spectrum_profile.py", agents)
        for text, name in ((agents, "AGENTS.md"), (project_map, "PROJECT_MAP.md")):
            # Both pointed at tests/test_ws_protocol.py, which does not exist;
            # the real file is tests/test_server_ws_protocol.py.
            self.assertIn("test_server_ws_protocol.py", text, name)
            self.assertNotIn("test_ws_protocol.py", text, name)

    def test_phantom_setopusbitrate_is_marked_unimplemented(self):
        """It was documented as a runtime command that does not exist anywhere."""
        marker = re.compile(r"not implemented|never implemented|NOT "
                            r"runtime-adjustable|未实现|不存在")
        for path in ("opus_rx.py", "docs/IOS_OPUS_INTEGRATION.md"):
            lines = _read(path).splitlines()
            hits = [i for i, ln in enumerate(lines) if "setOpusBitrate" in ln]
            self.assertTrue(hits, f"{path}: mention disappeared entirely")
            for i in hits:
                window = "\n".join(lines[max(i - 2, 0):i + 4])
                self.assertTrue(marker.search(window),
                                f"{path}:{i + 1} still claims it exists")

    def test_server_loop_docstring_documents_both_shapes(self):
        """It claimed a fixed 1701-byte frame and a 5 fps rate for years."""
        block = _read("server.py").split(
            "async def _broadcast_spectrum_loop()", 1)[1][:1400]
        self.assertNotIn("Runs at 5 fps", block)
        self.assertIn("851", block)
        self.assertIn("SPECTRUM_BROADCAST_FPS", block)


class VersionConsistencyTests(unittest.TestCase):
    def test_version_history_records_the_profile_work(self):
        """Stable across future bumps: history is append-only, so this cannot
        go stale the way a hardcoded version number would."""
        text = _read("SDD/14-version-history.md")
        self.assertIn("AD-025", text)
        self.assertIn("spectrum_profile", text)
        # The entry has to carry the two measured axes, or it is just a claim.
        self.assertIn("11.1 fps", text)
        self.assertIn("permessage-deflate", text)

    def test_app_version_bump_is_left_to_the_release_step(self):
        """The website download cards point at real artifact filenames
        (MRRC-Modern-v1.25.4-arm64.dmg).  Bumping them before an installer
        exists would hand users a 404, so CHANGELOG/.iss/cards move together at
        release time, not when a feature lands.
        """
        top = [ln for ln in _read("CHANGELOG.md").splitlines()
               if ln.startswith("## [v")][0]
        self.assertIn("v1.25.4", top)
        self.assertIn('"1.25.4"', _read("packaging/windows/MRRC-Modern.iss"))

    def test_tests_readme_matches_the_actual_count(self):
        """The number below is the real `unittest discover` output."""
        text = _read("tests/README.md")
        self.assertNotIn("1673", text)
        # 1751/92 was the count before the W103D box guards and the install.sh
        # variable scanner landed; a stale copy must not be left behind.
        self.assertNotIn("1751", text)
        # 1766/93 was the count before the W103D setup hotspot and its minimal
        # wizard landed (net_wifi, setup_ap, the wizard routes and the box guards).
        self.assertNotIn("1766", text)
        self.assertNotIn("92 test modules", text)
        self.assertNotIn("93 test modules", text)
        self.assertIn("96 test modules", text)
        self.assertNotIn("2029", text)
        self.assertNotIn("2036", text)
        self.assertNotIn("2042", text)
        self.assertIn("2024", text)
        self.assertNotIn("97 test modules", text)


if __name__ == "__main__":
    unittest.main()
