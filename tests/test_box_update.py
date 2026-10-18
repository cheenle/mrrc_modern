"""盒子自更新（`box_update` + 清单的 `box` 段）：给没有 SSH 能力的用户准备的那条路。

用户只有 `/settings` 一页；所以升级必须是「点一次 → 下载 → 校验 → 覆盖 → 重启」，
而且**失败要能自己退回去**。这里测的都是那条链路上会出错的地方：包不完整、
包里有越界路径、覆盖时把 venv/配置一起冲掉、重启后没起来却没人回滚。
"""
from __future__ import annotations

import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import box_update
import upgrade_core


def _tree(root: Path, files: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _targz(src: Path, dest: Path, *, prefix: str = "") -> Path:
    with tarfile.open(dest, "w:gz") as archive:
        for path in sorted(src.rglob("*")):
            arcname = str(path.relative_to(src))
            if prefix:
                arcname = f"{prefix}/{arcname}"
            archive.add(path, arcname=arcname)
    return dest


class ManifestTests(unittest.TestCase):
    BASE = {
        "latest": "1.28.2",
        "installer": {"version": "1.28.2", "url": "https://x/y.exe",
                      "sha256": "a" * 64, "size": 10},
    }

    def test_a_box_section_is_parsed(self):
        data = dict(self.BASE, box={"version": "1.28.2",
                                    "url": "https://x/tree.tar.gz",
                                    "sha256": "b" * 64, "size": 1234})
        parsed = upgrade_core.parse_manifest(data)
        self.assertEqual(parsed["box"]["size"], 1234)
        self.assertEqual(parsed["box"]["sha256"], "b" * 64)

    def test_a_manifest_without_one_still_parses(self):
        self.assertIsNone(upgrade_core.parse_manifest(self.BASE)["box"])

    def test_a_box_version_that_disagrees_is_refused(self):
        data = dict(self.BASE, box={"version": "1.28.1", "url": "https://x/t.sql.gz",
                                    "sha256": "b" * 64, "size": 1})
        with self.assertRaises(upgrade_core.ManifestError):
            upgrade_core.parse_manifest(data)

    def test_a_plain_http_box_url_is_refused(self):
        data = dict(self.BASE, box={"version": "1.28.2", "url": "http://x/t.sql.gz",
                                    "sha256": "b" * 64, "size": 1})
        with self.assertRaises(upgrade_core.ManifestError):
            upgrade_core.parse_manifest(data)

    def test_check_box_compares_versions(self):
        manifest = upgrade_core.parse_manifest(
            dict(self.BASE, box={"version": "1.28.2", "url": "https://x/t.gz",
                                 "sha256": "b" * 64, "size": 1}))
        self.assertTrue(upgrade_core.check_box("1.28.1", manifest)["available"])
        self.assertFalse(upgrade_core.check_box("1.28.2", manifest)["available"])
        self.assertFalse(upgrade_core.check_box("1.28.9", manifest)["available"])

    def test_check_box_without_a_package_says_so(self):
        result = upgrade_core.check_box("1.28.1", upgrade_core.parse_manifest(self.BASE))
        self.assertFalse(result["available"])
        self.assertIn("没有盒子的更新包", result["reason"])


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_truncated_package_is_refused_by_size(self):
        path = self.root / "p.tar.gz"
        path.write_bytes(b"x" * 10)
        self.assertIn("大小不对", box_update.verify(path, "", 99))

    def test_a_wrong_hash_is_refused(self):
        path = self.root / "p.tar.gz"
        path.write_bytes(b"x" * 10)
        self.assertIn("SHA-256", box_update.verify(path, "0" * 64, 10))

    def test_the_right_package_passes(self):
        path = self.root / "p.tar.gz"
        path.write_bytes(b"x" * 10)
        self.assertEqual(box_update.verify(path, box_update.sha256_of(path), 10), "")

    def test_an_escaping_path_is_refused(self):
        evil = self.root / "evil.tar.gz"
        data = b"pwned"
        with tarfile.open(evil, "w:gz") as archive:
            info = tarfile.TarInfo("../../etc/shadow")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        with self.assertRaises(ValueError):
            box_update.extract(evil, self.root / "out")
        self.assertFalse((self.root / "etc" / "shadow").exists())

    def test_a_normal_package_unpacks(self):
        src = _tree(self.root / "src", {"server.py": "print(1)\n"})
        package = _targz(src, self.root / "ok.tar.gz")
        count = box_update.extract(package, self.root / "out")
        self.assertEqual(count, 1)
        self.assertTrue((self.root / "out" / "server.py").is_file())


class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = _tree(self.root / "home", {"server.py": "old\n", "venv/bin/python": "x",
                                               "env/mrrc.env": "SECRET=1\n"})
        self.src = _tree(self.root / "src", {"server.py": "new\n", "net_bt.py": "added\n",
                                             "venv/bin/python": "x", "env/mrrc.env": "x\n"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_code_is_replaced_but_state_is_not(self):
        copied = box_update.stage_over(self.src, self.home)
        self.assertGreaterEqual(copied, 2)
        self.assertEqual((self.home / "server.py").read_text(), "new\n")
        self.assertTrue((self.home / "net_bt.py").is_file())
        self.assertEqual((self.home / "env" / "mrrc.env").read_text(), "SECRET=1\n",
                         "升级不能覆盖本机的 env（口令与 Cloud Hub 凭据都在里面）")
        self.assertEqual((self.home / "venv" / "bin" / "python").read_text(), "x")

    def test_runtime_dumps_are_not_copied_or_backed_up(self):
        """`ft8-slots` 是现场诊断留下的槽音频，实测能到 750 MB —— 不能进备份。"""
        (self.home / "ft8-slots").mkdir()
        (self.home / "ft8-slots" / "slot_1.raw").write_bytes(b"x" * 32)
        box_update.stage_over(self.src, self.home)
        self.assertTrue((self.home / "ft8-slots" / "slot_1.raw").is_file())

    def test_nothing_is_deleted(self):
        (self.home / "legacy.py").write_text("keep me\n")
        box_update.stage_over(self.src, self.home)
        self.assertTrue((self.home / "legacy.py").is_file())


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_version_txt_wins(self):
        tree = _tree(self.root / "t", {"version.txt": "9.9.9\n", "CHANGELOG.md": "## [v1.0.0]\n"})
        self.assertEqual(box_update.version_in(tree), "9.9.9")

    def test_the_changelog_is_the_fallback(self):
        tree = _tree(self.root / "t", {"CHANGELOG.md": "# x\n\n## [v1.2.3] — today\n"})
        self.assertEqual(box_update.version_in(tree), "1.2.3")

    def test_write_version_stamps_both_files(self):
        home = _tree(self.root / "home", {})
        box_update.write_version(home, "1.2.3")
        self.assertEqual((home / "version.txt").read_text().strip(), "1.2.3")
        self.assertEqual((home / "VERSION").read_text().strip(), "1.2.3")


class DiscoverabilityTests(unittest.TestCase):
    """用户找不到入口就等于没做。卡在 `/settings`，菜单里得有一步直达。"""

    ROOT = Path(__file__).resolve().parents[1]

    def test_the_menu_links_straight_to_the_card(self):
        import re
        index = (self.ROOT / "static" / "index.html").read_text(encoding="utf-8")
        match = re.search(r'<li>\s*<a[^>]*href="settings\.html#card-update"[^>]*>',
                          index, re.S)
        self.assertIsNotNone(match, "☰ 菜单里没有「软件更新」这一步")
        block = match.group(0)
        self.assertIn('target="_blank"', block)
        self.assertNotIn("data-action", block, "它必须是普通链接，不是 JS 菜单项")

    def test_the_settings_page_has_the_card(self):
        page = (self.ROOT / "static" / "settings.html").read_text(encoding="utf-8")
        for hook in ('id="card-update"', 'id="up-check"', 'id="up-apply"',
                     'id="up-pill"', 'id="up-info"'):
            self.assertIn(hook, page)
        self.assertIn("/api/update/apply", page)
        self.assertIn("/api/update/state", page)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = _tree(self.root / "home", {"server.py": "old\n", "version.txt": "1.0.0\n"})
        self.src = _tree(self.root / "src", {"server.py": "new\n", "version.txt": "1.1.0\n"})
        self.package = _targz(self.src, self.root / "pkg.tar.gz", prefix="tree")
        self.plan = {"version": "1.1.0", "url": "https://example/pkg.tar.gz",
                     "sha256": box_update.sha256_of(self.package),
                     "size": self.package.stat().st_size}
        box_update.reset()

    def tearDown(self):
        self.tmp.cleanup()
        box_update.reset()

    def _opener(self, *_a, **_k):
        blob = self.package.read_bytes()
        response = mock.MagicMock()
        response.__enter__.return_value = io.BytesIO(blob)
        response.__exit__.return_value = False
        return response

    def test_the_whole_flow_stages_versions_and_restarts(self):
        restarted = []
        spawned = []
        with mock.patch.dict(os.environ, {"MRRC_UPDATE_DIR": str(self.root / "state")}):
            state = box_update.apply(self.plan, self.home, opener=self._opener,
                                     spawn=lambda home, rollback: spawned.append(rollback),
                                     runner=lambda: restarted.append(True))
            self.assertEqual((self.home / "server.py").read_text(), "new\n")
            self.assertEqual((self.home / "version.txt").read_text().strip(), "1.1.0")
            rollback = spawned[0]
            self.assertTrue(Path(rollback).is_file(), "回滚包必须先存在")
            self.assertEqual(restarted, [True])
            self.assertEqual(state["state"], box_update.STATE_DONE)
            # 回滚包里是**旧**代码
            with tarfile.open(rollback, "r:gz") as archive:
                member = archive.extractfile("server.py")
                self.assertEqual(member.read().decode(), "old\n")

    def test_a_bad_hash_fails_before_touching_anything(self):
        plan = dict(self.plan, sha256="0" * 64)
        with mock.patch.dict(os.environ, {"MRRC_UPDATE_DIR": str(self.root / "state")}):
            state = box_update.apply(plan, self.home, opener=self._opener,
                                     spawn=lambda *a: self.fail("不该重启"),
                                     runner=lambda: self.fail("不该重启"))
            self.assertEqual(state["state"], box_update.STATE_FAILED)
            self.assertEqual((self.home / "server.py").read_text(), "old\n")

    def test_the_watchdog_restores_and_restarts_without_root(self):
        command = box_update.watchdog_command(Path("/opt/mrrc_modern"),
                                              Path("/tmp/rollback.tar.gz"),
                                              after=1, poll=1)
        self.assertIn("tar -xzf /tmp/rollback.tar.gz -C /opt/mrrc_modern", command)
        self.assertIn("api/health", command)
        self.assertIn("pkill -f 'venv/bin/python server.py'", command,
                      "回滚后的重启不能依赖 root helper")


if __name__ == "__main__":
    unittest.main()
