"""Website link integrity and generated-page freshness.

Two kinds of drift a reader cannot distinguish from a broken site:

* **An in-site anchor that does not exist.** The download page said "download the cleanup
  script" and the cloud guide said "install guide"; both pointed at `guide.html#clean-slate`
  and `#launcher-trouble`, and the guide had neither section. A third one came from the
  generator: it drops the markdown h1 (the hero header carries the title) while pandoc's
  TOC keeps listing the title as its first entry with that anchor, so the sidebar's top
  link was dead in every build. Every fragment is resolved here.

* **A generated page older than its source.** `website/guide.html` and its zh copy are
  built from `docs/OPERATION_GUIDE.md` by `website/build_guide.py`, so editing the guide
  without rebuilding leaves the site describing behaviour the shipped build no longer has
  — and it is the installer's access instructions that live there.

Hardware-free: file reads, plus one pandoc invocation when pandoc is installed.
"""
import importlib.util
import re
import shutil
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBSITE = ROOT / "website"
GUIDE_SOURCE = ROOT / "docs" / "OPERATION_GUIDE.md"


def site_pages():
    """Every page of the deployed tree (downloads/ holds binaries, not pages)."""
    return sorted(p for p in WEBSITE.rglob("*.html") if "downloads" not in p.parts)


def ids_of(path: Path):
    return set(re.findall(r'id="([^"]+)"', path.read_text(encoding="utf-8")))


class AnchorTests(unittest.TestCase):
    def test_every_in_site_anchor_resolves(self):
        ids = {p.resolve(): ids_of(p) for p in site_pages()}
        broken = []
        for page in site_pages():
            html = page.read_text(encoding="utf-8")
            for href in re.findall(r'href="([^"#]*#[^"]+)"', html):
                target, _, frag = href.partition("#")
                frag = urllib.parse.unquote(frag)          # ids carry CJK, hrefs are encoded
                dest = (page.parent / target).resolve() if target else page.resolve()
                rel = page.relative_to(WEBSITE)
                if dest not in ids:
                    broken.append(f"{rel} -> {href} (page not in the site tree)")
                elif frag not in ids[dest]:
                    broken.append(f"{rel} -> {href} (no such anchor)")
        self.assertEqual([], broken,
                         "broken in-site links:\n  " + "\n  ".join(broken))

    def test_no_page_offers_a_plain_http_local_url(self):
        """The server answers only on TLS: a plain-http local URL is the v1.24.5 black screen."""
        offenders = []
        pattern = re.compile(r"http://(?:127\.0\.0\.1|localhost):8888")
        for page in site_pages():
            for m in pattern.finditer(page.read_text(encoding="utf-8")):
                offenders.append(f"{page.relative_to(WEBSITE)}: {m.group(0)}")
        self.assertEqual([], offenders,
                         "the site must not hand out a URL the server cannot answer:\n  "
                         + "\n  ".join(offenders))


class GeneratedPageTests(unittest.TestCase):
    """`build_guide.py` is the only writer of guide.html; the committed copy must match it."""

    def _build(self):
        if shutil.which("pandoc") is None:
            self.skipTest("pandoc is not installed, so the guide cannot be rebuilt here")
        module_path = WEBSITE / "build_guide.py"
        spec = importlib.util.spec_from_file_location("build_guide", module_path)
        if spec is None or spec.loader is None:
            self.skipTest(f"cannot load {module_path}")
        build_guide = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build_guide)
        toc, body, title_id = build_guide.convert(GUIDE_SOURCE)
        return build_guide, toc, body, title_id

    def test_guide_pages_are_not_older_than_the_document(self):
        build_guide, toc, body, title_id = self._build()
        for lang, rel in (("en", "guide.html"), ("zh", "zh/guide.html")):
            expected = build_guide.build_page(toc, body, lang, title_id)
            actual = (WEBSITE / rel).read_text(encoding="utf-8")
            self.assertEqual(expected, actual,
                             f"{rel} is stale - rebuild with `python3 website/build_guide.py`")

    def test_the_document_title_anchor_is_rehomed_into_the_hero(self):
        """Dropping the markdown h1 must not orphan the TOC's first link."""
        build_guide, toc, body, title_id = self._build()
        self.assertIn('href="#', toc, "pandoc produced no TOC")
        match = re.search(r'href="#([^"]+)"', toc)
        if match is None:
            self.fail("pandoc's TOC has no anchor to compare against")
        first = match.group(1)
        self.assertTrue(title_id, "the document has no <h1 id=...> to re-home")
        self.assertEqual(first, title_id,
                         "the TOC's first entry is not the title anchor any more")


if __name__ == "__main__":
    unittest.main()
