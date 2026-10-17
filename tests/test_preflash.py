"""出厂预置脚本：真跑它，读它写出来的文件。

规格 D-3 的两条硬要求都能用一次真实执行验掉 —— 固定口令写进 env、以及
`MRRC_PREFLASH` 没置 1 时一个字都不写。脚本是 shell，所以断言只能落在
它写出来的文件上；`--env-only` 模式存在的理由就是这个测试。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "packaging" / "common" / "preflash.sh"

BASE_ENV = """\
# MRRC Modern Configuration
MRRC_BAUD_RATE=38400
MRRC_WEB_PORT=8888
MRRC_WEB_HOST=0.0.0.0
MRRC_PTT_MAX_TX_SECONDS=120
MRRC_SSL_CERT=/var/lib/mrrc/certs/server.crt
"""


def run_preflash(env_file: Path, *, flag: str | None = "1", extra: dict | None = None):
    env = {**os.environ, **(extra or {})}
    env.pop("MRRC_PREFLASH", None)
    if flag is not None:
        env["MRRC_PREFLASH"] = flag
    return subprocess.run(["bash", str(SCRIPT), "--env-only", str(env_file)],
                          capture_output=True, text=True, env=env)


def key_count(text: str, key: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith(key + "="))


class PreflashEnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_file = Path(self.tmp.name) / "mrrc.env"
        self.env_file.write_text(BASE_ENV, encoding="utf-8")

    def test_it_writes_the_fixed_password_and_clears_the_auto_flag(self):
        result = run_preflash(self.env_file)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("MRRC_WEB_PASSWORD=abcd!1234", text)
        self.assertEqual(key_count(text, "MRRC_WEB_PASSWORD"), 1)
        self.assertIn("MRRC_AUTO_PASSWORD=\n", text,
                      "AUTO 标志必须显式清空，否则页面会当它从未被设过")

    def test_it_keeps_every_other_key(self):
        run_preflash(self.env_file)
        text = self.env_file.read_text(encoding="utf-8")
        for line in BASE_ENV.splitlines():
            if not line or line.startswith("#"):
                continue
            with self.subTest(line=line):
                self.assertIn(line, text)

    def test_it_does_not_preset_a_radio_model_or_port(self):
        """型号与串口由设备上的探测决定（规格 D-3 / NFR-067）。"""
        run_preflash(self.env_file)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertNotIn("MRRC_RADIO_MODEL=", text)
        self.assertNotIn("MRRC_SERIAL_PORT=", text)

    def test_it_is_idempotent(self):
        run_preflash(self.env_file)
        once = self.env_file.read_bytes()
        run_preflash(self.env_file)
        self.assertEqual(once, self.env_file.read_bytes())

    def test_it_replaces_an_existing_password_instead_of_appending(self):
        self.env_file.write_text(BASE_ENV + "MRRC_WEB_PASSWORD=old-one\n", encoding="utf-8")
        run_preflash(self.env_file)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertEqual(key_count(text, "MRRC_WEB_PASSWORD"), 1)
        self.assertIn("MRRC_WEB_PASSWORD=abcd!1234", text)
        self.assertNotIn("old-one", text)

    def test_the_environment_can_override_the_batch_values(self):
        run_preflash(self.env_file, extra={"MRRC_PREFLASH_WEB_PASSWORD": "batch-two"})
        self.assertIn("MRRC_WEB_PASSWORD=batch-two",
                      self.env_file.read_text(encoding="utf-8"))

    def test_the_shell_metacharacter_in_the_default_password_survives(self):
        """口令里的 `!` 是构建→env→systemd→chpasswd 四层里的一个真风险点。"""
        run_preflash(self.env_file)
        line = [l for l in self.env_file.read_text(encoding="utf-8").splitlines()
                if l.startswith("MRRC_WEB_PASSWORD=")][0]
        self.assertEqual(line, "MRRC_WEB_PASSWORD=abcd!1234")

    def test_it_refuses_to_write_anything_without_the_flag(self):
        before = self.env_file.read_bytes()
        result = run_preflash(self.env_file, flag=None)
        self.assertNotEqual(result.returncode, 0,
                           "开关没置 1 也照写 == 默认镜像被悄悄改了口令")
        self.assertEqual(before, self.env_file.read_bytes())
        self.assertIn("MRRC_PREFLASH", result.stderr + result.stdout)


class BuildWiringTests(unittest.TestCase):
    """构建侧的两条纪律：默认关，且系统层的几行只在开关里执行。"""

    def test_the_ntp_step_does_not_assume_an_implementation(self):
        """基底用哪个 NTP 服务不由我们决定（Armbian 是 chrony，没有 systemd-timesyncd）。

        第一版无条件 `systemctl enable systemd-timesyncd.service`，在盒子上必然失败，
        留给构日志的只是一句“check the base image”的废话。现在的形状是“不挑实现、
        但必须有一个、否则构建失败”——而这一条守卫就是防止那句 enable 回来。
        """
        text = SCRIPT.read_text(encoding="utf-8")
        # 只看代码（注释里提到那个错误写法是有价值的，不该被守卫逼着删掉）。
        code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
        self.assertNotIn("enable systemd-timesyncd", code)
        self.assertIn("no NTP unit in this base image", code)

    def test_the_build_flag_defaults_to_off(self):
        text = (REPO / "packaging" / "box" / "build-image.sh").read_text(encoding="utf-8")
        self.assertIn("--preflash", text)
        self.assertIn("PREFLASH=0", text)

    def test_the_service_layer_calls_it_only_under_the_flag(self):
        text = (REPO / "packaging" / "common" / "mrrc-service-layer.sh").read_text(
            encoding="utf-8")
        start = text.index('if [ "${MRRC_PREFLASH:-0}" = "1" ]; then')
        block = text[start:text.index("\nfi", start)]
        self.assertIn("preflash.sh", block,
                      "调用点不在 MRRC_PREFLASH 分支里 ⇒ 默认镜像也会被写入口令")


if __name__ == "__main__":
    unittest.main()
