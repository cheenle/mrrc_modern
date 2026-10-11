"""The setup hotspot is gone, and this is what keeps it gone.

The feature was retired on 2026-10-11 after failing its own hardware
acceptance test: the board's MT7663S brings a hotspot up and never beacons it
(spec §1). An onboarding path that cannot be reached is worse than none, so the
new path is a LAN page — and a half-removed hotspot leaves exactly the thing
this file forbids: a passwordless write route nobody is testing.

The list below grows as each artifact retires. A file still carrying the old
vocabulary is not a style problem; it means the route is still registered.
"""
from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: (path, forbidden substrings). Grows one entry per retirement task.
RETIRED = [
    ("server.py", ("setup_ap", "hotspot", "Hotspot", "SETUP_GATE_PATHS",
                   "SETUP_WRITABLE_KEYS", "/api/manage", "wizard")),
    ("packaging/box/box-overlay.sh", ("mrrc-setup-ap",)),
    ("packaging/box/verify.sh", ("mrrc-setup-ap",)),
]

#: Files that must be gone from the tree entirely.
#:
#: One entry per retirement task: the packaging/supervisor half of the hotspot is
#: retired here (the resident AP supervisor and the unit it shipped as), the
#: server half was retired the task before.
GONE = [
    "static/manage.html",
    "tests/test_server_manage.py",
    "tests/test_server_setup_wizard.py",
    "linux/setup_ap.py",
    "tests/test_setup_ap.py",
]


class RetiredVocabularyTests(unittest.TestCase):
    def test_no_surviving_file_speaks_the_old_vocabulary(self):
        for relative, words in RETIRED:
            text = (REPO / relative).read_text(encoding="utf-8")
            for word in words:
                with self.subTest(path=relative, word=word):
                    self.assertNotIn(
                        word, text,
                        f"{relative} still mentions {word!r} — the retired "
                        f"path is still wired up")

    def test_the_wizard_modules_are_deleted(self):
        for relative in GONE:
            with self.subTest(path=relative):
                self.assertFalse((REPO / relative).exists(),
                                 f"{relative} should have been deleted")


if __name__ == "__main__":
    unittest.main()
