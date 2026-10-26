"""The resolver repair that rescues already-flashed boxes (2026-10-25).

Measured on a delivered box (MRRC-BG1SB, image built 2026-10-11)::

    -rw-r--r-- 1 root root 97 10月11日 11:58 /etc/resolv.conf
    # DNS requests are forwarded to the host. DHCP DNS options are ignored.
    nameserver 192.168.65.7

192.168.65.7 is Docker Desktop's internal resolver: the address of the machine the
**image was built on**. `packaging/box/build-image.sh` lends the chroot that file so
apt/pip can resolve, and the base image's `-> ../run/systemd/resolve/stub-resolv.conf`
symlink was never put back. glibc reads that file, waits 5 s per lookup and fails,
while `ping <ip>` keeps working — so the box looks networked and cannot resolve
anything (NTP pool, portal, update check, support upload).

`linux/fix_resolv_conf.sh` is the repair; `linux/mrrc_update.sh` runs it as root,
which is how boxes already in the field get it without a reflash. These tests run
the real script against a temporary directory (env-overridable paths), so the
promises below are measured, not asserted about the source text.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "linux" / "fix_resolv_conf.sh"
UPDATER = ROOT / "linux" / "mrrc_update.sh"

#: A `systemd-resolved` stand-in: the script only checks that the unit's binary
#: exists, so any real file is enough — and `/usr/bin/true` exists on macOS too
#: (`/bin/true` does not, which made the first version of this test vacuous).
RESOLVED_STAND_IN = "/usr/bin/true"

BAKED = ("# DNS requests are forwarded to the host. DHCP DNS options are ignored.\n"
         "nameserver 192.168.65.7\n")


class FixResolvConfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("bash") is None:
            raise unittest.SkipTest("fix_resolv_conf.sh needs a POSIX shell (bash)")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.conf = self.dir / "resolv.conf"
        self.stub = self.dir / "stub-resolv.conf"
        self.conf.write_text(BAKED, encoding="utf-8")
        self.stub.write_text("nameserver 127.0.0.53\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def run_script(self, *, args=(), unit=RESOLVED_STAND_IN, stub=None, state=None):
        env = {**os.environ,
               "MRRC_RESOLV_CONF": str(self.conf),
               "MRRC_RESOLV_STUB": str(stub or self.stub),
               "MRRC_RESOLVED_UNIT": unit,
               # 测试绝不能重启运行它们的机器上的 resolved（Linux CI 上它通常是活的）。
               "MRRC_RESOLV_RESTART": "0"}
        if state is not None:
            env["MRRC_RESOLVED_STATE"] = state
        return subprocess.run(["bash", str(SCRIPT), *args],
                              capture_output=True, text=True, env=env)

    def test_a_baked_static_file_becomes_the_stub_symlink(self):
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.conf.is_symlink(), "应该换成符号链接")
        # Relative, the way the distribution ships it.
        self.assertEqual(os.readlink(self.conf), "../run/systemd/resolve/stub-resolv.conf")
        self.assertIn("192.168.65.7", proc.stdout, "日志里要写清它换掉的是什么")

    def test_the_original_is_backed_up_once(self):
        self.run_script()
        backup = self.dir / "resolv.conf.mrrc-bak"
        self.assertEqual(backup.read_text(encoding="utf-8"), BAKED)
        # A second run must not overwrite the backup with the symlink's target.
        self.run_script()
        self.assertEqual(backup.read_text(encoding="utf-8"), BAKED)

    def test_it_is_idempotent(self):
        self.run_script()
        second = self.run_script()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already a symlink", second.stdout)

    def test_a_dry_run_changes_nothing(self):
        proc = self.run_script(args=("--dry-run",))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.conf.is_symlink())
        self.assertEqual(self.conf.read_text(encoding="utf-8"), BAKED)
        self.assertIn("would back it up", proc.stdout)

    def test_without_systemd_resolved_it_leaves_the_file_alone(self):
        """A box that resolves some other way must not have its configuration
        replaced with a stub nobody serves."""
        proc = self.run_script(unit=str(self.dir / "no-such-unit"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.conf.is_symlink())
        self.assertIn("no systemd-resolved", proc.stdout)

    def test_a_missing_stub_is_a_warning_and_no_change(self):
        """Resolved installed and running, stub missing: linking to a missing file
        would break DNS outright, so the repair backs off and says why."""
        proc = self.run_script(stub=self.dir / "absent-stub", state="active")
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(self.conf.is_symlink())
        self.assertIn("does not exist", proc.stderr)

    def test_a_machine_that_does_not_use_resolved_is_not_an_error(self):
        """树莓派的解析链不是 systemd-resolved（spec 2026-10-27 §2.2）。

        pi-gen 在导出阶段把 /etc/resolv.conf 写成 `nameserver 8.8.8.8`，之后由
        NetworkManager 接管重写。resolved 不跑 ⇒ /run 里没有 stub。把这种机器判成
        坏的，就是让一台 DNS 完全正常的机器去点“修复 DNS”，并让每次升级都打一行
        假警告。文件仍然不许动。
        """
        proc = self.run_script(stub=self.dir / "absent-stub", state="absent")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.conf.is_symlink())
        self.assertIn("NetworkManager", proc.stdout)
        self.assertEqual(self.conf.read_text(encoding="utf-8"), BAKED)

    def test_an_undecidable_resolved_state_stays_conservative(self):
        """问不到 systemd 就不装懂：仍然退出 1，仍然不动文件。"""
        proc = self.run_script(stub=self.dir / "absent-stub", state="unknown")
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(self.conf.is_symlink())

    def test_the_stale_global_server_is_dropped_on_a_real_box(self):
        """换符号链接只改变 glibc 读什么；resolved 自己在 foreign 模式下把旧文件的服务器
        吸进了**全局**列表，只有重启才会清掉（2026-10-25 实测：换完之后 `resolvectl
        status` 里 Global 还是 192.168.65.7）。测试里关掉重启，这里钉住它确实存在。"""
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("try-restart systemd-resolved", text)
        self.assertIn("MRRC_RESOLV_RESTART", text)

    def test_the_updater_calls_it(self):
        text = UPDATER.read_text(encoding="utf-8")
        self.assertIn("fix_resolv_conf.sh", text)
        # A failure there must not abort an upgrade: the box must still get its
        # new code even if this one file cannot be replaced.
        self.assertIn("if ! bash \"$MRRC_HOME/linux/fix_resolv_conf.sh\"", text)
    def test_the_upgrade_also_restarts_the_helper_that_serves_the_verb(self):
        """助手是常驻进程、动词表在启动时就定了：升级落地后不重启它，页面会一直
        答 `refused: fix-resolv-conf`，直到盒子重启（2026-10-25 自查发现的）。"""
        text = UPDATER.read_text(encoding="utf-8")
        self.assertIn("try-restart mrrc-box-helper", text)


if __name__ == "__main__":
    unittest.main()
