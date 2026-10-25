"""`dev_tools/mrrc-remote-update.sh`：把发布好的源码树装到 `/opt/mrrc_modern`。

为什么需要它：2026-10-18 之前的构建没有自升级接口，而它们自带的 `mrrc-update`
（≤1.25.5）用 `rsync --delete` 同步 —— 会把 `env/`、`profiles/` 和录音删掉。
所以给老盒子"只更新程序、不刷机"这条路，需要一个**不删任何东西**的安装器；
它就是 `box_update.apply()` 的 shell 双胞胎：同一个 keep 列表、同样只覆盖不删除、
版本号与修复包编号直接来自包里。

这里的断言全部在一个假树上跑（没有盒子、没有 systemd）：
 ① 代码被换掉，`version.txt` / `fixpack.txt` 来自包
 ② **运行期目录一个字节都没动**（env / data / recordings / fleet / .local / venv）
 ③ **旧树里包中不存在的文件不会被删** —— 这正是老 `mrrc-update` 的事故形态
 ④ 覆盖前留了回滚包，且里面是**旧**代码
 ⑤ 没有 version.txt 的包直接拒绝（不装一棵自称不知道版本的树）
"""
from __future__ import annotations

import io
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "dev_tools" / "mrrc-remote-update.sh"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _tarball(dest: Path, version: str, *, fixpack: str = "", extra: dict | None = None,
             root: str = "mrrc-modern") -> Path:
    """A staged-tree tarball, shaped like the published one (one top directory)."""
    files = {
        "server.py": f"# new tree {version}\n",
        "version.txt": f"{version}\n",
        "static/app.js": "// new\n",
    }
    if fixpack:
        files["fixpack.txt"] = f"{fixpack}\n"
    files.update(extra or {})
    with tarfile.open(dest, "w:gz") as archive:
        for rel, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{root}-{version}/{rel}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return dest


class RemoteUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = Path(self.tmp.name) / "opt_mrrc_modern"
        _write(self.box / "server.py", "# old tree\n")
        _write(self.box / "version.txt", "1.24.8\n")
        _write(self.box / "static/old-only.js", "// dropped upstream, must survive\n")
        _write(self.box / "env/mrrc.env", "MRRC_WEB_PASSWORD=secret\n")
        _write(self.box / "data/auth-tokens.json", "{}\n")
        _write(self.box / "recordings/2026-10-01_qso.mp3", "ID3")
        _write(self.box / "fleet/frpc", "binary")
        (self.box / "venv/bin").mkdir(parents=True, exist_ok=True)
        pip = self.box / "venv/bin/pip"
        pip.write_text("#!/bin/sh\necho pip \"$@\" >>\"$(dirname \"$0\")/../pip.log\"\n")
        pip.chmod(0o755)
        _write(self.box / "requirements.txt", "fastapi\n")
        self.pkg = _tarball(Path(self.tmp.name) / "src.tgz", "1.28.5", fixpack="FP1")

    def _run(self, pkg: Path | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ, MRRC_RESTART_CMD="true")   # /bin/true 在 macOS 上不存在
        return subprocess.run(["bash", str(SCRIPT), str(pkg or self.pkg), str(self.box), "mrrc"],
                              capture_output=True, text=True, env=env, timeout=120)

    def test_it_replaces_the_code_and_stamps_the_version(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1.24.8 -> 1.28.5 (FP1)", result.stdout)
        self.assertIn("new tree 1.28.5", (self.box / "server.py").read_text())
        self.assertEqual((self.box / "version.txt").read_text().strip(), "1.28.5")
        self.assertEqual((self.box / "fixpack.txt").read_text().strip(), "FP1")
        self.assertTrue((self.box / "static/app.js").is_file(), "新文件应当落地")

    def test_runtime_state_is_untouched(self):
        self._run()
        self.assertEqual((self.box / "env/mrrc.env").read_text(),
                         "MRRC_WEB_PASSWORD=secret\n", "env/ 是配置，绝不能动")
        self.assertEqual((self.box / "data/auth-tokens.json").read_text(), "{}\n")
        self.assertEqual((self.box / "recordings/2026-10-01_qso.mp3").read_text(), "ID3")
        self.assertEqual((self.box / "fleet/frpc").read_text(), "binary")

    def test_nothing_is_deleted(self):
        """老 `mrrc-update` 的 `--delete` 就是在这里出的事：包外的文件被抹掉。"""
        self._run()
        self.assertTrue((self.box / "static/old-only.js").is_file(),
                        "包中没有的文件必须原地保留")

    def test_it_backs_up_the_old_tree_first(self):
        self._run()
        updates = self.box / ".local/share/MRRC-Modern/update"
        backups = sorted(updates.glob("rollback-manual-1.24.8-*.tar.gz"))
        self.assertEqual(len(backups), 1, f"应当留一个回滚包，实际 {list(updates.iterdir())}")
        with tarfile.open(backups[0], "r:gz") as archive:
            # 脚本用 `tar -C "$TARGET" .` 打包，成员名带 `./` 前缀（exclude 也按这个写）
            names = archive.getnames()
            member = archive.extractfile("./server.py" if "./server.py" in names else "server.py")
            self.assertIsNotNone(member)
            self.assertIn(b"old tree", member.read() if member else b"")

    def test_a_tree_without_version_is_refused(self):
        bad = _tarball(Path(self.tmp.name) / "bad.tgz", "X", extra={}, root="x")
        with tarfile.open(bad, "w:gz") as archive:            # 重建：去掉 version.txt
            info = tarfile.TarInfo("x/server.py")
            data = b"# no version\n"
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        result = self._run(bad)
        self.assertNotEqual(result.returncode, 0, "没有 version.txt 的树不许装")
        self.assertIn("no version.txt", result.stderr)
        self.assertIn("old tree", (self.box / "server.py").read_text(), "失败时不许动旧代码")

    def test_the_script_never_deletes_anything(self):
        """包外的文件必须原地保留 —— 老 `mrrc-update` 的 `--delete` 就是在这里出的事。

        只看**代码行**：注释里解释「为什么不用 --delete」是文档，不是行为。
        """
        code = "\n".join(line for line in SCRIPT.read_text().splitlines()
                          if not line.lstrip().startswith("#"))
        self.assertTrue(SCRIPT.read_text().startswith("#!/bin/bash"),
                        "她要能在盒子上直接 bash 运行")
        for banned in ("sed -i", "--delete", "-delete"):
            self.assertNotIn(banned, code, f"安装器里不许出现 {banned}")


class PublishedUpdaterTests(unittest.TestCase):
    def test_the_published_copy_matches_the_repo_one(self):
        """站点上那份是运维手敲/脚本拉取的那个文件 —— 两份必须一致。"""
        site = ROOT / "website" / "downloads" / "mrrc-remote-update.sh"
        repo = ROOT / "dev_tools" / "mrrc-remote-update.sh"
        self.assertTrue(site.is_file(), "网站上还没有 mrrc-remote-update.sh")
        self.assertEqual(site.read_bytes(), repo.read_bytes(),
                         "website/downloads 与 dev_tools 的那份不一致")


if __name__ == "__main__":
    unittest.main()
