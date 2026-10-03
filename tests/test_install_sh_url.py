"""The macOS source installer must hand over a URL that actually answers.

install.sh writes MRRC_WEB_HOST=0.0.0.0 and never disables TLS, so the server only
answers on `https://` for that port — yet the script's closing summary printed
`http://localhost:8888`, which is the v1.24.5 black screen in miniature: the user is
sent to a scheme the server does not serve. This is a static check because the string
is what ships; nothing else in the suite reads install.sh.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"


class InstallShUrlTests(unittest.TestCase):
    def setUp(self):
        self.text = INSTALL_SH.read_text(encoding="utf-8")

    def test_install_sh_exists(self):
        self.assertTrue(INSTALL_SH.is_file(), "install.sh must ship in the repo root")

    def test_summary_url_is_https(self):
        line = self._server_url_line()
        self.assertIn("https://localhost:", line,
                      f"the summary must hand over the TLS URL, got: {line.strip()}")

    def test_no_plain_http_url_is_ever_printed(self):
        offenders = [
            f"{i}: {ln.strip()}"
            for i, ln in enumerate(self.text.splitlines(), 1)
            if re.search(r"http://localhost:\d", ln)
        ]
        self.assertEqual([], offenders,
                         "the server only answers on TLS; never print a plain-http local URL")

    def test_tls_is_not_disabled_behind_the_promise(self):
        # If someone ever switches the script to plain HTTP, the https claim above becomes
        # the bug instead. Guard the assumption instead of assuming it.
        for var in ("MRRC_SSL_DISABLED", "MRRC_HTTPS", "MRRC_TLS"):
            self.assertNotIn(var, self.text,
                             f"{var} would change the scheme install.sh promises")

    # helpers
    def _server_url_line(self):
        for ln in self.text.splitlines():
            if "Server URL:" in ln:
                return ln
        self.fail("install.sh no longer prints a Server URL summary line")


if __name__ == "__main__":
    unittest.main()
