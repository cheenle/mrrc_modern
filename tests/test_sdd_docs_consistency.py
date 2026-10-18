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


def summary_table_ids() -> set:
    """The `8.16 Decision Summary` rows inside chapter 8 itself."""
    text = (SDD / "08-architecture-decisions.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\|\s*(AD-\d+)\s*\|", text, re.MULTILINE))


def version_history_index_ids() -> set:
    """The `Key Architecture Decisions` index at the end of chapter 14."""
    text = (SDD / "14-version-history.md").read_text(encoding="utf-8")
    start = text.find("## Key Architecture Decisions")
    if start < 0:
        return set()
    return set(re.findall(r"^\|\s*(AD-\d+)\s*\|", text[start:], re.MULTILINE))


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

    def test_landing_pages_advertise_no_decision_that_does_not_exist(self):
        """The reverse of the check above, and the one that was missing.

        The setup hotspot's AD-026 was added to both landing pages while its
        entry was never written into chapter 8, so the site advertised a
        decision the design record did not contain. A one-directional check
        cannot see that; this one can.
        """
        ids = ad_ids()
        for page in ("sdd.html", "zh/sdd.html"):
            html = (WEBSITE / page).read_text(encoding="utf-8")
            for found in sorted(set(re.findall(r"AD-\d+", html)) - ids):
                self.fail(f"{page} advertises {found}, which chapter 8 does not define")

    def test_the_summary_table_lists_every_decision(self):
        """§8.16 drifted four decisions behind (AD-020/023/024/025)."""
        ids, summary = ad_ids(), summary_table_ids()
        self.assertTrue(summary, "chapter 8 has no Decision Summary table")
        self.assertEqual(sorted(ids - summary), [], "§8.16 is missing decisions")

    def test_the_version_history_index_lists_every_decision(self):
        """Chapter 14's index drifted nine decisions behind (AD-017…AD-025)."""
        ids, index = ad_ids(), version_history_index_ids()
        self.assertTrue(index, "chapter 14 has no decision index")
        self.assertEqual(sorted(ids - index), [], "chapter 14's index is missing decisions")


def version_history_rows() -> list[str]:
    """The data rows of chapter 14's version table, in file order."""
    text = (SDD / "14-version-history.md").read_text(encoding="utf-8")
    rows: list[str] = []
    for line in text.splitlines():
        if line.startswith("|"):
            rows.append(line)
        elif rows:
            break  # the table ends; the AD index at the chapter's end is separate
    return rows[2:]  # drop the header row and the |---|---| separator


def cell_boundaries(row: str) -> int:
    """Count the pipes pandoc will treat as column boundaries.

    Pipes inside an inline code span or escaped as ``\\|`` are *text* — the
    table already relies on this (`systemctl ... | grep -q` and
    `GET | POST /api/setup/wizard*` are one cell each, not two). Every other
    pipe splits the row, and pandoc **discards** cells past the header's four
    without an error or a warning.
    """

    count, in_code, escaped = 0, False, False
    for char in row:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "`":
            in_code = not in_code
        elif char == "|" and not in_code:
            count += 1
    return count


def cells_in_row(row: str) -> int:
    """How many cells pandoc sees in one table row.

    A trailing pipe is optional (`| SDD V2.59 | … | … | …）。` is a valid
    four-cell row with no closing pipe), so the boundary count is the cell
    count only when the row does not close itself.
    """

    return cell_boundaries(row) - 1 if row.rstrip().endswith("|") else cell_boundaries(row)


class VersionHistoryTableTests(unittest.TestCase):
    def test_every_version_row_keeps_the_columns_the_header_declares(self):
        """A stray pipe is silent data loss, not a cosmetic slip.

        Adding the V2.85 row glued the V2.84 row's cells behind an extra pipe:
        the version, the date and the author vanished and the Changes text
        landed in a fifth cell that `website/build_sdd.py`'s pandoc pass threw
        away — the v1.27.0 release record, four artifact sizes and their
        SHA-256s left the generated chapter page. Every other guard stayed
        green, because they all check that a version *is* mentioned, never
        that a row is still parseable. This test is that missing half.
        """

        rows = version_history_rows()
        self.assertGreater(len(rows), 1, "chapter 14 has no version table")
        for row in rows:
            version = row.split("|")[1].strip()
            self.assertEqual(
                cells_in_row(row), 4,
                f"the SDD {version} row parses as {cells_in_row(row)} cells, "
                f"not the header's 4 — pandoc will drop the extra ones",
            )


class DocumentIndexTests(unittest.TestCase):
    """Every document in ``docs/`` is findable from the index.

    The index is hand-maintained, and its failure mode is silent: the file
    exists, nobody can find it, and the index still looks complete. When this
    guard was written the W103D guide and the hardware matrix were both missing
    from it, and both are documents users are handed.

    Deliberately loose about *how* a document is linked (``docs/name.md`` or a
    bare ``name.md``): the check is "does the index mention it", not "does it
    match one link style".
    """

    def test_every_doc_is_mentioned_in_the_index(self):
        index = (ROOT / "docs" / "DOCUMENT_INDEX.md").read_text(encoding="utf-8")
        missing = sorted(
            path.name for path in (ROOT / "docs").glob("*.md")
            if path.name != "DOCUMENT_INDEX.md" and path.name not in index
        )
        self.assertEqual(
            [], missing,
            "these documents exist but docs/DOCUMENT_INDEX.md never names them",
        )


if __name__ == "__main__":
    unittest.main()
