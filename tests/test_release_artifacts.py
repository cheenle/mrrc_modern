"""Release-artifact completeness guards.

The V2.46 cycle shipped three drifts that no test noticed: the hand-written SDD
landing pages still advertised V2.27, the generated pages' footer advertised
V2.45 (the generator read a hand-maintained row), and `docs/OPERATION_GUIDE.md`
still pointed at the v1.13.0 DMG. The release harness now knows every version-
and artifact-bearing file (`release-artifacts.json`) and this module runs that
check as part of the suite, so a stale document fails the build instead of
being discovered by a reader.

Hardware-free and offline: pure file reads (the published-site check is opt-in
via `release_check.py --online`).
"""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
CHECKER = REPO / ".agents/skills/dual-platform-release/harness/release_check.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("release_check", CHECKER)
    if spec is None or spec.loader is None:      # pragma: no cover - defensive
        raise unittest.SkipTest(f"cannot load {CHECKER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rc = _load_checker()


class RepositoryStateTests(unittest.TestCase):
    """The repository itself must be release-consistent right now."""

    def test_offline_release_check_is_clean(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = rc.main([])
        self.assertEqual(code, 0, "release check reported failures:\n" + out.getvalue())

    def test_registry_lists_the_current_facing_artifacts(self):
        registry = json.loads(rc.REGISTRY_PATH.read_text(encoding="utf-8"))
        governed = {rule["path"] for rule in registry["rules"]}
        for expected in ("packaging/windows/MRRC-Modern.iss",
                         "website/index.html", "website/zh/index.html",
                         "website/sdd.html", "website/zh/sdd.html",
                         "SDD/README.md", "docs/MACOS_INSTALLER_GUIDE.md",
                         "docs/WINDOWS_INSTALLER_GUIDE.md"):
            self.assertIn(expected, governed, expected)

    def test_history_files_are_not_version_governed(self):
        """Dated records must never be "updated" to the current version."""
        registry = json.loads(rc.REGISTRY_PATH.read_text(encoding="utf-8"))
        governed = {rule["path"] for rule in registry["rules"]}
        for history in ("CHANGELOG.md", "SDD/14-version-history.md"):
            self.assertNotIn(history, governed, history)


class RuleEngineTests(unittest.TestCase):
    """Unit coverage for the checker with a synthetic repository."""

    def _synthetic(self, changelog="## [v9.9.9] - 2030-01-01\n",
                   iss="1.9.9", page="MRRC-Modern-v9.9.9-Windows-x64-Setup.exe"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
        (root / "packaging/windows").mkdir(parents=True, exist_ok=True)
        (root / "packaging/windows/MRRC-Modern.iss").write_text(
            f'#define MyAppVersion "{iss}"\n', encoding="utf-8")
        (root / "website").mkdir(exist_ok=True)
        (root / "website/index.html").write_text(page + "\n", encoding="utf-8")
        (root / "SDD").mkdir(exist_ok=True)
        (root / "SDD/14-version-history.md").write_text(
            "| SDD V3.14 | 2030-01-01 | T | row |\n", encoding="utf-8")
        (root / "SDD/README.md").write_text("| SDD Version | V3.14 |\n", encoding="utf-8")
        return root

    def test_app_and_sdd_version_sources(self):
        root = self._synthetic()
        with mock.patch.object(rc, "ROOT", root):
            registry = json.loads(rc.REGISTRY_PATH.read_text(encoding="utf-8"))
            self.assertEqual(rc.app_version(registry), "9.9.9")
            self.assertEqual(rc.sdd_version(registry), "V3.14")

    def test_stale_iss_version_is_a_failure(self):
        root = self._synthetic(iss="1.0.0")
        registry = json.loads(rc.REGISTRY_PATH.read_text(encoding="utf-8"))
        with mock.patch.object(rc, "ROOT", root):
            results = rc.check_rules(registry, "9.9.9", "V3.14")
        iss = [r for r in results if r[1] == "iss-version"][0]
        self.assertEqual(iss[0], rc.FAIL)
        self.assertIn("1.0.0", iss[2])

    def test_stale_download_link_is_a_failure(self):
        root = self._synthetic(
            page="MRRC-Modern-v1.0.0-Windows-x64-Setup.exe")
        registry = json.loads(rc.REGISTRY_PATH.read_text(encoding="utf-8"))
        with mock.patch.object(rc, "ROOT", root):
            results = rc.check_stale_tokens(registry, "9.9.9")
        self.assertTrue(any(r[0] == rc.FAIL for r in results))

    def test_missing_optional_rule_is_skipped_not_failed(self):
        registry = json.loads(rc.REGISTRY_PATH.read_text(encoding="utf-8"))
        rule = {"id": "pi-guide", "path": "docs/RASPBERRY_PI_GUIDE.md",
                "pattern": "MRRC-Modern-v([0-9.]+)-rpi64\\.img\\.xz",
                "expect": "app", "min_count": 1, "optional": True}
        with mock.patch.object(rc, "ROOT", Path("/nonexistent")):
            results = rc.check_rules({"rules": [rule]}, "9.9.9", "V3.14")
        self.assertEqual(results[0][0], rc.SKIP)


class DiagramRuleTests(unittest.TestCase):
    """Diagrams are governed like chapters: marker + depicted keywords.

    Caught in the wild: three of the SDD diagrams were from a *different*
    project (SunMRRC/SunSDR2 — TX EQ chain, 0x1F00 telemetry) and the rest were
    a month behind the capabilities they drew.
    """

    def test_every_registered_diagram_passes(self):
        registry = json.loads(rc.REGISTRY_PATH.read_text(encoding="utf-8"))
        results = rc.check_diagrams(registry, "V2.46")
        failures = [r for r in results if r[0] == rc.FAIL]
        self.assertEqual(failures, [], failures)

    def test_missing_marker_is_a_failure(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SDD/diagrams").mkdir(parents=True)
            (root / "SDD/diagrams/x.svg").write_text("<svg>nothing</svg>", encoding="utf-8")
            registry = {"diagrams": {"files": [
                {"path": "SDD/diagrams/x.svg", "require": ["NOPE"]}],
                "source_dirs": {}, "max_versions_behind": 2},
                "sdd_version_source": {"path": "SDD/14-version-history.md",
                                       "pattern": r"^\|\s*(?:SDD\s+)?(V[0-9.]+)\s*\|"}}
            with mock.patch.object(rc, "ROOT", root):
                results = rc.check_diagrams(registry, "V2.46")
        self.assertEqual(results[0][0], rc.FAIL)
        self.assertIn("marker", results[0][2])
        self.assertIn("NOPE", results[0][2])

    def test_a_lagging_marker_is_accepted_within_the_window(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "SDD/diagrams").mkdir(parents=True)
            (root / "SDD/14-version-history.md").write_text(
                "| SDD V2.47 | x | | |\n| SDD V2.46 | x | | |\n| SDD V2.45 | x | | |\n",
                encoding="utf-8")
            (root / "SDD/diagrams/x.svg").write_text(
                "<!-- diagram-version: V2.45 -->\n<svg></svg>", encoding="utf-8")
            registry = {"diagrams": {"files": [{"path": "SDD/diagrams/x.svg", "require": []}],
                                     "source_dirs": {}, "max_versions_behind": 2},
                        "sdd_version_source": {"path": "SDD/14-version-history.md",
                                               "pattern": r"^\|\s*(?:SDD\s+)?(V[0-9.]+)\s*\|"}}
            with mock.patch.object(rc, "ROOT", root):
                results = rc.check_diagrams(registry, "V2.47")
        self.assertEqual(results[0][0], rc.PASS, results)

    def test_copies_must_match_their_source(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir(); (root / "b").mkdir()
            (root / "a/x.svg").write_text("one", encoding="utf-8")
            (root / "b/x.svg").write_text("two", encoding="utf-8")
            registry = {"diagrams": {"files": [], "source_dirs": {"a": "b"},
                                     "max_versions_behind": 2}}
            with mock.patch.object(rc, "ROOT", root):
                results = rc.check_diagrams(registry, "V2.46")
        self.assertTrue(any(r[0] == rc.FAIL and "differs" in r[2] for r in results), results)


class ReportTests(unittest.TestCase):
    def test_report_lists_failures_and_manual_review(self):
        registry = json.loads(rc.REGISTRY_PATH.read_text(encoding="utf-8"))
        self.assertIn("manual_review", registry)
        self.assertTrue(registry["manual_review"]["paths"])
        self.assertTrue(registry["history_only"]["paths"])


if __name__ == "__main__":
    unittest.main()
