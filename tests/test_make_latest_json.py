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

    def test_a_box_fix_pack_is_carried_into_the_manifest(self):
        """`fixpack` 是**新键**：老解析器忽略它，新页面才显示。"""
        blob = self._artifact("src.tar.gz", b"\x1f\x8b" + b"z" * 4096)
        manifest = self._build(box=("1.18.0", "https://example.invalid/src.tar.gz", blob),
                               box_fixpack="FP3")
        self.assertEqual(manifest["boxLine"]["fixpack"], "FP3")

    def test_a_box_without_a_fix_pack_says_nothing_about_one(self):
        blob = self._artifact("src2.tar.gz", b"\x1f\x8b" + b"w" * 4096)
        manifest = self._build(box=("1.18.0", "https://example.invalid/src2.tar.gz", blob))
        self.assertEqual(manifest["boxLine"].get("fixpack", ""), "")

    def test_a_malformed_box_fix_pack_refuses_the_whole_manifest(self):
        blob = self._artifact("src3.tar.gz", b"\x1f\x8b" + b"q" * 4096)
        with self.assertRaises(maker.BuildError):
            self._build(box=("1.18.0", "https://example.invalid/src3.tar.gz", blob),
                        box_fixpack="fix3")

    def test_reads_the_changelog_version(self):
        changelog = self.root / "CHANGELOG.md"
        changelog.write_text("# Changelog\n\n## [v1.18.0] — 2026-10-01 — 说明\n", encoding="utf-8")
        self.assertEqual(maker.changelog_version(changelog), "1.18.0")
        changelog.write_text("# 没有版本\n", encoding="utf-8")
        with self.assertRaises(maker.BuildError):
            maker.changelog_version(changelog)


    def test_the_box_line_uses_the_key_the_field_boxes_parse(self):
        """`box` 是本模块曾经写出的键，而现场盒子的解析器要求 `box.version == latest`。

        盒子线比安装包线新（这是常态：Windows 构建机常常缺席），写 `box` 会让那些盒子
        的「检查更新」直接报错；`boxLine` 是新键，老解析器忽略未知键，所以只有它既能
        表达"盒子线更靠前"，又不把老盒子弄崩。
        """
        blob = self._artifact("src4.tar.gz", b"\x1f\x8b" + b"n" * 4096)
        manifest = self._build(box=("1.18.5", "https://example.invalid/src4.tar.gz", blob),
                               box_fixpack="FP2")
        self.assertIn("boxLine", manifest)
        self.assertNotIn("box", manifest)

    def test_box_notes_are_carried_into_the_box_line(self):
        blob = self._artifact("src5.tar.gz", b"\x1f\x8b" + b"m" * 4096)
        manifest = self._build(box=("1.18.5", "https://example.invalid/src5.tar.gz", blob),
                               box_notes="FP2：说明")
        self.assertEqual(manifest["boxLine"]["notes"], "FP2：说明")


class BoxOnlyTests(unittest.TestCase):
    """`--box-only`：换盒子段，安装包段一个字节都不动。

    交付盒子修复包时，那台机器上通常没有 80 MB 的安装包；重新哈希一个不存在的文件
    会让清单开始描述"别的东西"。所以这里只允许动盒子段，并且结果照样要过客户端的解析器。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = {
            "latest": "1.18.0",
            "installer": {"version": "1.18.0", "url": "https://example.invalid/setup.exe",
                          "sha256": "a" * 64, "size": 4096},
            "minSupported": "", "mandatory": False,
            "releasedAt": "2026-10-01T00:00:00",
            "notes": "桌面线说明",
            "previous": {"version": "1.17.0", "url": "https://example.invalid/old.exe",
                         "sha256": "b" * 64, "size": 4096},
        }
        self.blob = self.root / "src.tar.gz"
        self.blob.write_bytes(b"\x1f\x8b" + b"s" * 4096)

    def _run(self, *extra, out=None):
        out_path = out or (self.root / "latest.json")
        if out is None:
            out_path.write_text(json.dumps(self.base), encoding="utf-8")
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            code = maker.main(["--box-only", "--out", str(out_path), *extra])
        return code, out_path

    def test_it_replaces_only_the_box_line(self):
        code, out_path = self._run(
            "--box", "1.18.5", "https://example.invalid/src.tar.gz", str(self.blob),
            "--box-fixpack", "FP2", "--box-notes", "FP2：说明")
        self.assertEqual(code, 0)
        data = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(data["installer"], self.base["installer"])
        self.assertEqual(data["previous"], self.base["previous"])
        self.assertEqual(data["notes"], self.base["notes"])
        self.assertEqual(data["boxLine"]["version"], "1.18.5")
        self.assertEqual(data["boxLine"]["fixpack"], "FP2")
        self.assertEqual(data["boxLine"]["notes"], "FP2：说明")
        import hashlib
        self.assertEqual(data["boxLine"]["sha256"],
                         hashlib.sha256(self.blob.read_bytes()).hexdigest())

    def test_it_drops_a_legacy_box_key(self):
        self.base["box"] = {"version": "1.18.4", "url": "https://example.invalid/old.tar.gz",
                            "sha256": "c" * 64, "size": 1024}
        code, out_path = self._run(
            "--box", "1.18.5", "https://example.invalid/src.tar.gz", str(self.blob))
        self.assertEqual(code, 0)
        data = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertNotIn("box", data)

    def test_it_refuses_without_a_box_or_with_a_bad_fix_pack(self):
        code, out_path = self._run("--box-fixpack", "FP2")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out_path.read_text(encoding="utf-8")), self.base,
                         "一份被拒绝的清单不能落盘")
        code, _ = self._run(
            "--box", "1.18.5", "https://example.invalid/src.tar.gz", str(self.blob),
            "--box-fixpack", "fix2")
        self.assertEqual(code, 2)

    def test_it_refuses_a_missing_base_manifest(self):
        code, _ = self._run(
            "--box", "1.18.5", "https://example.invalid/src.tar.gz", str(self.blob),
            out=self.root / "nope.json")
        self.assertEqual(code, 2)
        self.assertFalse((self.root / "nope.json").exists())

    def test_it_refuses_to_be_combined_with_an_installer(self):
        code, _ = self._run(
            "--box", "1.18.5", "https://example.invalid/src.tar.gz", str(self.blob),
            "--installer", "1.18.0", "https://example.invalid/x.exe", str(self.blob))
        self.assertEqual(code, 2)


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
