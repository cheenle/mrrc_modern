"""SDD documentation consistency guards.

These caught a real class of drift in the V2.46 release: the generated chapter
pages carried AD-018/AD-019 while (a) `build_sdd.py` still advertised V2.45
because it read the hand-maintained Quick Facts row, and (b) the hand-written
landing pages (`website/sdd.html`, `website/zh/sdd.html`) still advertised
V2.27 with an AD index ending at AD-016. Both cases are enforced here.

Hardware-free: pure file reads over the repository.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SDD = ROOT / "SDD"
WEBSITE = ROOT / "website"


def newest_history_version() -> str:
    text = (SDD / "14-version-history.md").read_text(encoding="utf-8")
    m = re.search(r"^\|\s*(SDD\s+)?(V[\d.]+)\s*\|", text, re.MULTILINE)
    return m.group(2) if m else ""


def readme_quick_facts_version() -> str:
    text = (SDD / "README.md").read_text(encoding="utf-8")
    m = re.search(r"\|\s*SDD Version\s*\|\s*(V[\d.]+)\s*\|", text)
    return m.group(1) if m else ""


def ad_ids() -> set:
    text = (SDD / "08-architecture-decisions.md").read_text(encoding="utf-8")
    return set(re.findall(r"^##\s+(AD-\d+)", text, re.MULTILINE))


class VersionConsistencyTests(unittest.TestCase):
    def test_readme_quick_facts_matches_the_version_history(self):
        self.assertEqual(readme_quick_facts_version(), newest_history_version())

    def test_generator_reports_the_version_history_version(self):
        import sys
        sys.path.insert(0, str(WEBSITE))
        import build_sdd
        self.assertEqual(build_sdd.sdd_version(), newest_history_version())

    def test_generated_pages_use_the_current_version(self):
        for page in ("sdd/index.html", "sdd/08-architecture-decisions.html"):
            html = (WEBSITE / page).read_text(encoding="utf-8")
            self.assertIn(newest_history_version(), html,
                          f"{page} does not mention {newest_history_version()}")

    def test_landing_pages_show_the_current_version(self):
        """The hand-written pages are not generated — they drifted to V2.27 once."""
        version = newest_history_version()
        for page in ("sdd.html", "zh/sdd.html"):
            html = (WEBSITE / page).read_text(encoding="utf-8")
            self.assertIn(f"SDD {version}", html, page)
            self.assertNotIn("SDD V2.27", html, page)

    def test_main_pages_show_the_current_sdd_version(self):
        version = newest_history_version()
        for page in ("index.html", "zh/index.html"):
            html = (WEBSITE / page).read_text(encoding="utf-8")
            self.assertIn(f"<strong>{version}</strong>", html, page)


class AdIndexConsistencyTests(unittest.TestCase):
    def test_landing_pages_list_every_decision(self):
        ids = ad_ids()
        self.assertGreaterEqual(len(ids), 19)
        for page in ("sdd.html", "zh/sdd.html"):
            html = (WEBSITE / page).read_text(encoding="utf-8")
            for ad in sorted(ids, key=lambda a: int(a.split("-")[1])):
                self.assertIn(ad, html, f"{page} is missing {ad}")


if __name__ == "__main__":
    unittest.main()
