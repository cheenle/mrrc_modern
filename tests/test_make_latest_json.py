"""latest.json generator (spec 2026-09-17-upgrade-channel §2 D1).

The manifest is generated from real artifacts, never hand-written: a stale SHA or
size here sends users to a broken download, so every refusal path is tested.
"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "make_latest_json", ROOT / "dev_tools" / "make_latest_json.py")
assert _spec is not None and _spec.loader is not None
maker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(maker)


class ManifestBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.installer = self._artifact("MRRC-Modern-Setup.exe", b"MZ" + b"x" * 4096)
        self.previous = self._artifact("MRRC-Modern-v1.17.0.exe", b"MZ" + b"y" * 4096)

    def _artifact(self, name, blob):
        path = self.root / name
        path.write_bytes(blob)
        return path

    def _build(self, **over):
        kwargs = dict(installer=("1.18.0", "https://example.invalid/MRRC-Modern-Setup.exe",
                                 self.installer),
                      previous=("1.17.0", "https://example.invalid/old.exe", self.previous),
                      notes="说明", expect_app="1.18.0")
        kwargs.update(over)
        return maker.build_manifest(**kwargs)

    def test_hashes_come_from_the_files(self):
        import hashlib
        manifest = self._build()
        self.assertEqual(manifest["installer"]["sha256"],
                         hashlib.sha256(self.installer.read_bytes()).hexdigest())
        self.assertEqual(manifest["installer"]["size"], self.installer.stat().st_size)
        self.assertEqual(manifest["previous"]["version"], "1.17.0")

    def test_produces_a_manifest_the_clients_accept(self):
        import upgrade_core as up
        manifest = self._build()
        self.assertEqual(up.parse_manifest(manifest)["latest"], "1.18.0")
        self.assertTrue(up.check("1.17.0", up.parse_manifest(manifest))["available"])

    def test_refuses_when_the_artifact_is_missing(self):
        with self.assertRaises(maker.BuildError):
            self._build(installer=("1.18.0", "https://example.invalid/x.exe",
                                   self.root / "nope.exe"))

    def test_refuses_a_suspiciously_small_artifact(self):
        small = self._artifact("tiny.exe", b"MZ")
        with self.assertRaises(maker.BuildError) as ctx:
            self._build(installer=("1.18.0", "https://example.invalid/x.exe", small))
        self.assertIn("字节", str(ctx.exception))

    def test_refuses_a_version_that_disagrees_with_the_changelog(self):
        with self.assertRaises(maker.BuildError) as ctx:
            self._build(expect_app="1.17.0")
        self.assertIn("CHANGELOG", str(ctx.exception))

    def test_refuses_a_previous_that_is_not_older(self):
        with self.assertRaises(maker.BuildError):
            self._build(previous=("1.18.0", "https://example.invalid/old.exe", self.previous))

    def test_previous_is_optional_but_first_release_has_none(self):
        manifest = self._build(previous=None)
        self.assertNotIn("previous", manifest)
        self.assertEqual(manifest["latest"], "1.18.0")

    def test_reads_the_changelog_version(self):
        changelog = self.root / "CHANGELOG.md"
        changelog.write_text("# Changelog\n\n## [v1.18.0] — 2026-10-01 — 说明\n", encoding="utf-8")
        self.assertEqual(maker.changelog_version(changelog), "1.18.0")
        changelog.write_text("# 没有版本\n", encoding="utf-8")
        with self.assertRaises(maker.BuildError):
            maker.changelog_version(changelog)


class CliTests(unittest.TestCase):
    def test_dry_run_prints_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "setup.exe"
            path.write_bytes(b"MZ" + b"z" * 4096)
            out = Path(tmp) / "latest.json"
            import contextlib
            import io
            buffer = io.StringIO()
            # the repo's CHANGELOG is at 1.17.0; this test is about the CLI, not the version
            with mock.patch.object(maker, "changelog_version", return_value="1.18.0"), \
                 contextlib.redirect_stdout(buffer):
                code = maker.main(["--installer", "1.18.0", "https://example.invalid/x.exe",
                                   str(path), "--out", str(out), "--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn('"latest": "1.18.0"', buffer.getvalue())
            self.assertFalse(out.exists())

    def test_bad_input_exits_two_with_a_reason(self):
        import contextlib
        import io
        errors = io.StringIO()
        with mock.patch.object(maker, "changelog_version", return_value="1.17.0"), \
             contextlib.redirect_stderr(errors):
            code = maker.main(["--installer", "1.18.0", "https://example.invalid/x.exe",
                               "/nonexistent/setup.exe", "--out", "/tmp/should-not-exist.json"])
        self.assertEqual(code, 2)
        self.assertIn("拒绝生成", errors.getvalue())

    def test_writes_a_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "setup.exe"
            path.write_bytes(b"MZ" + b"z" * 4096)
            out = Path(tmp) / "latest.json"
            import contextlib
            import io
            with mock.patch.object(maker, "changelog_version", return_value="1.18.0"), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = maker.main(["--installer", "1.18.0", "https://example.invalid/x.exe",
                                   str(path), "--out", str(out)])
            self.assertEqual(code, 0)
            data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["installer"]["size"], 4098)


if __name__ == "__main__":
    unittest.main()
