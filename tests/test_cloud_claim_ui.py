"""The enrolment secret must be reachable from the panel a user actually sees.

Reported on 1.24.7: someone applied, the operator sent a one-time secret, and the dialog offered only
Refresh - the secret box lived in the form panel, which is hidden as soon as an application exists.
The button added in the pending panel is the fix; this test keeps both halves present.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ClaimUiTests(unittest.TestCase):
    def test_pending_panel_has_a_secret_field_and_button(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="cloud-secret-pending"', html)
        self.assertIn('id="cloud-claim"', html)
        pending = html.index('id="cloud-pending"')
        self.assertGreater(html.index('id="cloud-secret-pending"'), pending,
                           "the secret field must live inside the pending panel")

    def test_javascript_wires_the_button(self):
        js = (ROOT / "static" / "modules" / "cloud_hub.js").read_text(encoding="utf-8")
        self.assertIn("cloud-claim", js)
        self.assertIn("lastState", js)

    def test_form_field_says_what_it_is_for(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("登记口令", html)


if __name__ == "__main__":
    unittest.main()
