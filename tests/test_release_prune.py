"""Rule-based pruning of the published `downloads/` directory.

Origin: 2026-11-13, the portal's root filesystem hit **100 % full** and nginx had
been failing to write `access.log` for hours. 4.4 GB of the 14 GB was
`mrrc_modern/downloads`: eight Windows installers and eight DMGs from one busy day,
eighteen source tarballs — while the site and `latest.json` referenced three. The
cleanup itself was done by hand (and one judgement call was wrong: the frps daily
logs were cut to 3 days although `frps.toml` deliberately keeps 14, to make
"a tunnel that drops twice a day" distinguishable from "600 times a day"). These
tests pin the parts that must not depend on judgement:

* what the update manifest names is never deleted — a *previous* entry is what an
  older install is told to upgrade through, so "old" is not the same as "unused";
* what the site's own pages name is never deleted (the index links a source
  tarball two box fix packs old);
* a generic name is a symlink, so its *target* is the file a download resolves to;
* the newest N per family survive regardless of references (current + previous);
* anything unclassifiable is reported, never removed.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "prune_downloads",
    ROOT / ".agents/skills/dual-platform-release/harness/prune_downloads.py")
assert _spec is not None and _spec.loader is not None
prune = importlib.util.module_from_spec(_spec)
sys.modules["prune_downloads"] = prune
_spec.loader.exec_module(prune)

MANIFEST = {
    "latest": "1.28.58",
    "installer": {
        "version": "1.28.58",
        "url": "https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-v1.28.58-Windows-x64-Setup.exe",
        "sha256": "a" * 64, "size": 171397986,
    },
    "previous": {
        "version": "1.28.52",
        "url": "https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-v1.28.52-Windows-x64-Setup.exe",
        "sha256": "b" * 64, "size": 171355227,
    },
    "boxLine": {
        "version": "1.28.50",
        "url": "https://www.vlsc.net/mrrc_modern/downloads/mrrc-modern-src-1.28.50.tar.gz",
        "sha256": "c" * 64, "size": 22773196, "fixpack": "FP46",
        "patch": {"version": "1.28.50", "requires": "1.28.49",
                  "url": "https://www.vlsc.net/mrrc_modern/downloads/mrrc-modern-patch-1.28.50.tar.gz",
                  "sha256": "d" * 64, "size": 31505},
    },
}


class PruneTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "downloads"
        self.dir.mkdir()

    def touch(self, name, size=1024):
        path = self.dir / name
        path.write_bytes(b"x" * size)
        return path

    def versions(self, prefix, versions, suffix, size=1024):
        """Create `prefix<ver><suffix>` for each version, smallest version first."""
        for version in versions:
            self.touch(f"{prefix}{version}{suffix}", size)

    def plan(self, **over):
        kwargs = dict(directory=self.dir, manifest=MANIFEST, site_text="")
        kwargs.update(over)
        return prune.plan(**kwargs)

    def deleted(self, result):
        return sorted(row["name"] for row in result["delete"])


class ProtectionTests(PruneTestCase):
    """The three ways a file becomes untouchable."""

    def test_every_manifest_url_is_protected_including_previous_and_hot_fix(self):
        self.versions("MRRC-Modern-v", ["1.20.0", "1.28.52", "1.28.58"],
                      "-Windows-x64-Setup.exe")
        self.touch("mrrc-modern-src-1.28.50.tar.gz")
        self.touch("mrrc-modern-patch-1.28.50.tar.gz")
        result = self.plan(keep={"windows": 1, "source": 0, "patch": 0})
        self.assertIn("MRRC-Modern-v1.20.0-Windows-x64-Setup.exe", self.deleted(result))
        for name in ("MRRC-Modern-v1.28.52-Windows-x64-Setup.exe",
                     "MRRC-Modern-v1.28.58-Windows-x64-Setup.exe",
                     "mrrc-modern-src-1.28.50.tar.gz",
                     "mrrc-modern-patch-1.28.50.tar.gz"):
            self.assertNotIn(name, self.deleted(result), name)

    def test_a_page_link_protects_a_file_the_manifest_never_mentions(self):
        """The index links `mrrc-modern-src-1.28.47.tar.gz`; the manifest says 1.28.50."""
        self.touch("mrrc-modern-src-1.28.47.tar.gz")
        self.touch("mrrc-modern-src-1.28.45.tar.gz")
        index = Path(self.tmp.name) / "index.html"
        index.write_text('<a href="downloads/mrrc-modern-src-1.28.47.tar.gz">源码</a>',
                         encoding="utf-8")
        text = prune.site_references(self.dir.parent)      # every page next to downloads/
        self.assertIn("mrrc-modern-src-1.28.47.tar.gz", text)
        result = self.plan(site_text=text, keep={"source": 0, "patch": 0})
        self.assertIn("mrrc-modern-src-1.28.45.tar.gz", self.deleted(result))
        self.assertNotIn("mrrc-modern-src-1.28.47.tar.gz", self.deleted(result))

    def test_a_symlink_target_is_protected_by_the_link_that_sells_it(self):
        """`MRRC-Modern-Setup.exe` is a link; `cp` over it overwrites the archive."""
        self.touch("MRRC-Modern-v1.28.58-Windows-x64-Setup.exe")
        self.touch("MRRC-Modern-v1.28.50-Windows-x64-Setup.exe")
        os.symlink("MRRC-Modern-v1.28.58-Windows-x64-Setup.exe",
                   self.dir / "MRRC-Modern-Setup.exe")
        manifest = dict(MANIFEST)
        manifest.pop("installer")          # nothing else names the 1.28.58 archive
        manifest.pop("previous")
        result = self.plan(manifest=manifest, keep={"windows": 0, "source": 0, "patch": 0})
        self.assertNotIn("MRRC-Modern-v1.28.58-Windows-x64-Setup.exe", self.deleted(result))
        self.assertIn("MRRC-Modern-v1.28.50-Windows-x64-Setup.exe", self.deleted(result))
        self.assertTrue((self.dir / "MRRC-Modern-Setup.exe").is_symlink(),
                        "generic name must stay a link — cp over it overwrites the archive")

    def test_unclassifiable_files_are_reported_and_never_deleted(self):
        self.touch("MRRC-Update.cmd", 4096)
        self.touch("latest.json", 2048)
        self.touch("openssl-win64-portable-3.6.3.zip", 4096)
        (self.dir / "patches").mkdir()
        result = self.plan()
        self.assertEqual(self.deleted(result), [])
        names = {row["name"] for row in result["unclassified"]}
        self.assertEqual(names, {"MRRC-Update.cmd", "latest.json",
                                 "openssl-win64-portable-3.6.3.zip"})


class AgeTests(PruneTestCase):
    """Newest N survive even with no reference at all (the archive of the last release)."""

    def test_default_keeps_the_newest_two_installers(self):
        self.versions("MRRC-Modern-v", ["1.28.51", "1.28.53", "1.28.54", "1.28.55",
                                        "1.28.56", "1.28.57", "1.28.58"],
                      "-Windows-x64-Setup.exe")
        result = self.plan()
        self.assertEqual(self.deleted(result), [
            "MRRC-Modern-v1.28.51-Windows-x64-Setup.exe",
            "MRRC-Modern-v1.28.53-Windows-x64-Setup.exe",
            "MRRC-Modern-v1.28.54-Windows-x64-Setup.exe",
            "MRRC-Modern-v1.28.55-Windows-x64-Setup.exe",
            "MRRC-Modern-v1.28.56-Windows-x64-Setup.exe",
        ])

    def test_version_order_is_numeric_not_lexical(self):
        """`1.28.9` < `1.28.10` — a lexical sort would delete the newer one."""
        self.versions("mrrc-modern-patch-", ["1.28.9", "1.28.10", "1.28.11"],
                      ".tar.gz")
        result = self.plan(keep={"source": 0, "patch": 1})
        self.assertEqual(len(self.deleted(result)), 2)
        self.assertIn("mrrc-modern-patch-1.28.9.tar.gz", self.deleted(result))
        self.assertIn("mrrc-modern-patch-1.28.10.tar.gz", self.deleted(result))
        self.assertNotIn("mrrc-modern-patch-1.28.11.tar.gz", self.deleted(result))

    def test_the_families_are_independent(self):
        """Pruning installers must not authorize deleting the source archives.

        注意清单里 1.28.52 保护的是**安装包**（`.exe`），不是同版本的 dmg：保护集按文件名
        精确匹配，所以 dmg 家族只有「最新 N 个」这一条命——这正是 "previous" 不能替
        另一套制品作主的意思。
        """
        self.versions("MRRC-Modern-v", ["1.28.50", "1.28.51", "1.28.52", "1.28.58"],
                      "-arm64.dmg")
        self.versions("mrrc-modern-src-", ["1.28.45", "1.28.50"], ".tar.gz")
        result = self.plan(keep={"macos": 1, "source": 2})
        self.assertEqual(self.deleted(result), ["MRRC-Modern-v1.28.50-arm64.dmg",
                                                "MRRC-Modern-v1.28.51-arm64.dmg",
                                                "MRRC-Modern-v1.28.52-arm64.dmg"])
        self.assertEqual([r["name"] for r in result["kept"] if r["family"] == "source"],
                         ["mrrc-modern-src-1.28.45.tar.gz", "mrrc-modern-src-1.28.50.tar.gz"])


class SafetyTests(PruneTestCase):
    """Refusals: an unusable input must stop the run, not produce a big delete list."""

    def test_refuses_a_manifest_with_no_urls(self):
        self.touch("MRRC-Modern-v1.28.51-Windows-x64-Setup.exe")
        with self.assertRaises(prune.PlanError):
            self.plan(manifest={"latest": "1.28.58"})

    def test_refuses_to_empty_a_family(self):
        """`--keep-windows 0` with nothing else protecting them is a wipe, not a prune."""
        self.touch("MRRC-Modern-v1.28.50-Windows-x64-Setup.exe")
        with self.assertRaises(prune.PlanError):
            self.plan(manifest={"latest": "1.28.58", "installer": {
                "url": "https://x.invalid/other.exe"}}, keep={"windows": 0})

    def test_missing_directory_is_reported_by_the_cli(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = prune.main(["--dir", str(self.dir / "nope"),
                               "--manifest", "/nonexistent/latest.json"])
        self.assertEqual(code, 2)
        self.assertIn("latest.json", err.getvalue())


class CliTests(PruneTestCase):
    def test_dry_run_deletes_nothing_and_apply_deletes_exactly_the_plan(self):
        self.versions("MRRC-Modern-v", ["1.28.51", "1.28.52", "1.28.58"],
                      "-Windows-x64-Setup.exe")
        manifest_path = Path(self.tmp.name) / "latest.json"
        manifest_path.write_text(json.dumps(MANIFEST), encoding="utf-8")
        argv = ["--dir", str(self.dir), "--manifest", str(manifest_path),
                "--keep-windows", "1"]
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(prune.main(argv), 0)
        self.assertIn("1.28.51", out.getvalue())
        self.assertTrue((self.dir / "MRRC-Modern-v1.28.51-Windows-x64-Setup.exe").exists(),
                        "dry run must not delete")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(prune.main(argv + ["--apply"]), 0)
        self.assertFalse((self.dir / "MRRC-Modern-v1.28.51-Windows-x64-Setup.exe").exists())
        self.assertTrue((self.dir / "MRRC-Modern-v1.28.52-Windows-x64-Setup.exe").exists())
        self.assertTrue((self.dir / "MRRC-Modern-v1.28.58-Windows-x64-Setup.exe").exists())

    def test_json_output_is_a_plan_object(self):
        self.versions("MRRC-Modern-v", ["1.28.51", "1.28.58"], "-arm64.dmg")
        manifest_path = Path(self.tmp.name) / "latest.json"
        manifest_path.write_text(json.dumps(MANIFEST), encoding="utf-8")
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            prune.main(["--dir", str(self.dir), "--manifest", str(manifest_path),
                        "--keep-macos", "1", "--json"])
        payload = json.loads(out.getvalue())
        self.assertEqual([row["name"] for row in payload["delete"]],
                         ["MRRC-Modern-v1.28.51-arm64.dmg"])
        self.assertEqual(payload["delete"][0]["family"], "macos")


class LiveManifestTests(unittest.TestCase):
    """The repo's own `latest.json` must survive the planner unchanged."""

    def test_the_repository_manifest_parses_and_names_its_artifacts(self):
        path = ROOT / "website/downloads/latest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        names = prune.manifest_names(manifest)
        self.assertIn(f"MRRC-Modern-v{manifest['latest']}-Windows-x64-Setup.exe", names)
        self.assertIn("mrrc-modern-src-1.28.50.tar.gz", names)     # boxLine
        self.assertIn("mrrc-modern-patch-1.28.50.tar.gz", names)   # boxLine.patch
        self.assertEqual(prune.classify(f"MRRC-Modern-v{manifest['latest']}-arm64.dmg"),
                         ("macos", tuple(int(p) for p in manifest["latest"].split("."))))


if __name__ == "__main__":
    unittest.main()
