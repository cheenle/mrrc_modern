"""The menu footer shows the installed version (2026-10-16).

The element existed since the first release but its text was a literal
(`"MRRC Modern v1.0"`) that no release ever updated, so every build — v1.14
through v1.27 — advertised v1.0 to whoever opened the ☰ menu.  The fix makes
the number come from the same detector the support bundle manifest and the
update check use (`support_bundle.detect_version`: version.txt in a build
product, then MRRC-Modern.iss, then CHANGELOG.md), which is what makes it a
report of the running build rather than a fifth copy to keep in sync.

What these pin, in the order the number travels:
  1. server._full_state_message carries it (the WS payload the page already
     receives on connect — no new endpoint, no new auth surface);
  2. the payload value is the authority's value, not a literal;
  3. the markup still has the footer slot where it lands;
  4. ft710_main.js writes what fullState sent and keeps a sane fallback for a
     legacy server that sends no version at all;
  5. the asset cache moved, or installed users keep the old page forever
     (`index.html` is cache-first in sw.js and only reinstalls when sw.js
     itself changes).
"""
import json
import re
import unittest
import unittest.mock
from pathlib import Path

import server
import support_bundle

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "static"


def _changelog_version() -> str:
    """Top app version in CHANGELOG.md, or "" while it reads [未发布]."""
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(r"^## \[v([0-9]+\.[0-9]+\.[0-9]+)\]", text, re.MULTILINE)
    return match.group(1) if match else ""


class FullStateVersionTests(unittest.TestCase):
    """The version is in the payload the page already gets."""

    def test_fullstate_carries_the_version(self):
        msg = server._full_state_message({"vfo_a_freq": 14_200_000}, [None] * 6)
        self.assertIn("version", msg)
        self.assertRegex(msg["version"], r"^[0-9]+\.[0-9]+\.[0-9]+$")
        json.dumps(msg)                      # WS send path stays serializable

    def test_the_payload_version_is_the_authority(self):
        """Same detector as the support bundle / update check — not a literal.

        Patching the detector must move the payload value; if the field were a
        hardcoded string this assertion fails, which is the regression that
        produced the v1.0 footer.
        """
        with unittest.mock.patch.object(support_bundle, "detect_version",
                                        return_value="9.9.9"):
            msg = server._full_state_message({}, [])
        self.assertEqual(msg["version"], "9.9.9")

    @unittest.skipUnless(_changelog_version(), "CHANGELOG has no released version yet")
    def test_the_source_run_reports_the_released_version(self):
        """In a source tree there is no version.txt, so the .iss answers."""
        msg = server._full_state_message({}, [])
        self.assertEqual(msg["version"], _changelog_version())


class MenuFooterFrontendTests(unittest.TestCase):
    """The slot, the writer, and the fallback — a missing one is "no version"."""

    @classmethod
    def setUpClass(cls):
        cls.index = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.main = (STATIC / "ft710_main.js").read_text(encoding="utf-8")
        cls.sw = (STATIC / "sw.js").read_text(encoding="utf-8")

    def test_the_footer_slot_is_the_last_menu_element(self):
        self.assertIn('class="menu-footer"', self.index)
        footer = self.index.split('class="menu-footer"', 1)[1]
        self.assertIn('id="menu-version"', footer.split("</div>", 1)[0])

    def test_no_hardcoded_version_in_the_markup(self):
        """The shipped page must not claim a version before the server answers."""
        markup, _ = self.index.split('id="menu-version"', 1)
        shipped = markup.rsplit(">", 1)[1]
        self.assertNotRegex(shipped, r"v[0-9]")

    def test_fullstate_version_reaches_the_footer(self):
        self.assertIn("serverVersion = msg.version || null;", self.main)
        branding = self.main.split("function _applyRadioBranding()")[1].split("\nfunction ")[0]
        self.assertIn('"menu-version"', branding)
        self.assertIn("serverVersion", branding)

    def test_legacy_server_still_gets_a_sane_footer(self):
        """No version field (older server) must not print "undefined"."""
        branding = self.main.split("function _applyRadioBranding()")[1].split("\nfunction ")[0]
        self.assertNotIn('+ " v" + undefined', branding)
        self.assertIn('" v" + serverVersion', branding)
        self.assertIn(': ""', branding)          # 没有版本就是空串，不是 "undefined"

    def test_the_fix_pack_number_joins_the_version(self):
        """盒子线的 FP 号与版本号并列显示（`v1.28.5 · FP1`）；桌面版没有它就只剩版本号。"""
        self.assertIn('serverFixpack = msg.fixpack || "";', self.main)
        branding = self.main.split("function _applyRadioBranding()")[1].split("\nfunction ")[0]
        self.assertIn('serverFixpack ? " · " + serverFixpack : ""', branding)

    def test_the_page_cache_moved_with_the_edit(self):
        """index.html is cache-first: a stale sw.js means a stale footer."""
        self.assertIn("const CACHE = 'mrrc-v57';", self.sw)
        self.assertIn("'/'", self.sw)


if __name__ == "__main__":
    unittest.main()
