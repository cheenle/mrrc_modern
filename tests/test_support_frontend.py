"""Frontend contract for the report entry (spec 2026-09-17 §8).

The menu entry is deliberately a plain anchor: `ft710_ui.js` binds
`.menu-item[data-action]` only, so the two tooling-guarded files stay untouched
(AGENTS.md) and `target="_blank"` keeps the SPA's WebSocket — and any PTT
ownership — alive while the operator writes the report.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
PAGE = ROOT / "static" / "support.html"


class SupportEntryTests(unittest.TestCase):
    def setUp(self):
        self.index = INDEX.read_text(encoding="utf-8")
        self.page = PAGE.read_text(encoding="utf-8")

    def test_menu_has_a_support_link(self):
        self.assertIn('href="/support.html"', self.index)
        # Bound the match at </li>: the anchor's closing tag is itself wrapped
        # across lines by the file's formatter, so `</a>` is not a safe anchor.
        entry = re.search(r'<li>\s*<a[^>]*href="/support.html"[^>]*>(.*?)</li>',
                          self.index, re.S)
        if entry is None:
            self.fail("menu entry for /support.html not found in static/index.html")
        block = entry.group(0)
        self.assertIn("遇到问题", entry.group(1))
        self.assertIn('target="_blank"', block)
        self.assertIn('rel="noopener"', block)
        self.assertNotIn("data-action", block)     # must stay a plain anchor

    def test_menu_binding_still_ignores_plain_anchors(self):
        ui = (ROOT / "static" / "ft710_ui.js").read_text(encoding="utf-8")
        self.assertIn("document.querySelectorAll('.menu-item[data-action]')", ui)

    def test_page_calls_the_three_endpoints(self):
        for endpoint in ("/api/support/bundle", "/api/support/upload", "/api/support/save"):
            self.assertIn(endpoint, self.page)

    def test_page_is_xss_safe_and_collects_client_context(self):
        self.assertIn("userAgent", self.page)
        self.assertNotIn("innerHTML", self.page)
        self.assertIn("textContent", self.page)

    def test_page_reports_the_answers_url_with_the_bundle_id(self):
        self.assertIn("/mrrc_modern/answers/#", self.page)

    def test_page_handles_the_401_case(self):
        self.assertIn("401", self.page)
        self.assertIn("登录", self.page)


if __name__ == "__main__":
    unittest.main()
