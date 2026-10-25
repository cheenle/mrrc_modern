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
        self.assertIn('href="support.html"', self.index)
        # Bound the match at </li>: the anchor's closing tag is itself wrapped
        # across lines by the file's formatter, so `</a>` is not a safe anchor.
        entry = re.search(r'<li>\s*<a[^>]*href="support.html"[^>]*>(.*?)</li>',
                          self.index, re.S)
        if entry is None:
            self.fail("menu entry for support.html not found in static/index.html")
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


class HelpPanelTests(unittest.TestCase):
    """The maintainer needs three facts, not two: username, password **and port**.

    2026-10-25 field: the panel showed 用户名 / 口令 / 剩余 / 已连接, so before he could
    connect (`ssh hub -W 127.0.0.1:<port>`) the supporter had to ask which port it was —
    the one number that cannot be guessed. `/api/help/status` has always carried `port`
    (`help_session.status()`); the panel simply never rendered it.
    """

    def setUp(self):
        self.page = PAGE.read_text(encoding="utf-8")

    def test_the_panel_has_a_port_row(self):
        self.assertIn('id="help-port"', self.page)
        self.assertIn("s.port", self.page)

    def test_an_unassigned_port_reads_as_a_dash_not_zero(self):
        """A window that is open but has no tunnel port yet must not show `0`:
        `0` is what a maintainer would paste, and it connects to nothing."""
        self.assertIn('s.port ? String(s.port) : "—"', self.page)

    def test_the_copy_button_carries_all_three_facts(self):
        self.assertIn("复制连接信息", self.page)
        for label in ('"用户名 "', '"\\n口令 "', '"\\n端口 "'):
            self.assertIn(label, self.page)

    def test_the_password_is_still_never_written_to_a_url_or_link(self):
        """The panel renders the password as text; it must not turn it into a
        `?token=` style link (AD-024 has the same rule for auth tokens)."""
        self.assertNotIn("help-pw\"></a>" , self.page)


if __name__ == "__main__":
    unittest.main()
