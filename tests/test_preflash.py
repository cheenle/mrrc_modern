"""出厂预置脚本：真跑它，读它写出来的文件。

规格 D-3 的两条硬要求都能用一次真实执行验掉 —— 固定口令写进 env、以及
`MRRC_PREFLASH` 没置 1 时一个字都不写。脚本是 shell，所以断言只能落在
它写出来的文件上；`--env-only` 模式存在的理由就是这个测试。
"""
from __future__ import annotations

import os
import shutil
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
    @classmethod
    def setUpClass(cls):
        # The script under test is POSIX shell, and the release gate runs this
        # suite on Windows too (2026-10-17: the Windows build gate collected
        # this class for the first time and got 8 FileNotFoundError on `bash`).
        # BuildWiringTests stays platform-independent on purpose — it reads the
        # sources, so it must run everywhere.
        if shutil.which("bash") is None:
            raise unittest.SkipTest("preflash.sh needs a POSIX shell (bash)")

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

    def test_a_leftover_tree_is_refused_not_reused(self):
        """`build/w103d/w103d.img` 是**上一版叠过的树**，不是临时文件。

        见它存在就跳过解包，等于在同一棵树上再叠一层，而 pip 只会加不会删：
        2026-10-26 因此产出过一个同时带 `scipy-1.14.1.dist-info` 和
        `scipy-1.17.1.dist-info` 的镜像 —— 旧的 native 模块还在盘上，刷这个镜像的
        每一台都在 `import scipy.signal` 段错误、web 起不来。守卫钉住的是
        "默认拒绝 + 显式开关"这个形状，而不是某句文案。
        """
        text = (REPO / "packaging" / "box" / "build-image.sh").read_text(encoding="utf-8")
        code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
        self.assertIn('if [ -f "$WORK/w103d.img" ]; then', code)
        self.assertIn("MRRC_BOX_REUSE_TREE", code)
        # 静默复用的形状就是"只有一句 if [ ! -f ... ]"：
        self.assertNotIn('if [ ! -f "$WORK/w103d.img" ]; then', code)
        # 默认路径必须 exit 1，否则守卫等于没有。
        start = code.index('if [ -f "$WORK/w103d.img" ]; then')
        block = code[start:code.index("\nfi", start)]
        self.assertIn("exit 1", block)

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


class RescueWifiTests(unittest.TestCase):
    """救急 Wi-Fi：什么网都不通时，客户用手机开个同名热点，盒子自己连上去。

    方向是**客户端**而不是 AP：这块 MT7663S 软件能开 AP、信标发不出去
    （2026-10-11 真机：手机与 Mac 都扫不到，tx_packets 60 秒 +2），所以救急只能
    反过来——盒子连手机。`--wifi-only` 存在的理由就是让这些断言真跑一次脚本。
    """

    @classmethod
    def setUpClass(cls):
        # 同 PreflashEnvTests：脚本是 POSIX shell，而 Windows 构建门禁也会跑全量套件。
        if shutil.which("bash") is None:
            raise unittest.SkipTest("preflash.sh needs a POSIX shell (bash)")

    def run_wifi(self, *, extra=None, flag: "str | None" = "1"):
        proc, text, mode, _ = self._run_wifi(extra=extra, flag=flag)
        return proc, text, mode

    def _run_wifi(self, *, extra=None, flag: "str | None" = "1", seed=None):
        """The 4-tuple form: also returns the pre-seeded keyfiles after the run.

        `seed` lets a test put a profile in the directory *before* preflash runs,
        which is the case that matters for the image (the base image ships
        connections of its own).
        """
        with tempfile.TemporaryDirectory() as tmp:
            for name, body in (seed or {}).items():
                seeded = Path(tmp) / name
                seeded.write_text(body, encoding="utf-8")
                seeded.chmod(0o600)
            env = {**os.environ, "MRRC_PREFLASH_WIFI_DIR": tmp, **(extra or {})}
            env.pop("MRRC_PREFLASH", None)
            if flag is not None:
                env["MRRC_PREFLASH"] = flag
            proc = subprocess.run(["bash", str(SCRIPT), "--wifi-only"],
                                  capture_output=True, text=True, env=env)
            path = Path(tmp) / "mrrc-rescue.nmconnection"
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            seeded_after = {name: (Path(tmp) / name).read_text(encoding="utf-8")
                            for name in (seed or {}) if (Path(tmp) / name).exists()}
            # 目录在 with 里就被删了，所以权限位要在这里读出来带回去。
            mode = (path.stat().st_mode & 0o777) if path.exists() else 0
            return proc, text, mode, seeded_after

    def test_it_writes_a_client_profile_not_an_access_point(self):
        proc, text, _ = self.run_wifi()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("mode=infrastructure", text)
        self.assertNotIn("mode=ap", text)

    def test_the_default_name_and_password_are_the_documented_ones(self):
        _, text, _ = self.run_wifi()
        self.assertIn("ssid=rescue", text)
        self.assertIn("psk=abcd1234", text)

    def test_it_is_armed_but_never_outranks_the_customers_own_network(self):
        """优先级 -10：家里网在的时候救急 profile 不抢（NM 取可用的最高优先级）。"""
        _, text, _ = self.run_wifi()
        self.assertIn("autoconnect=true", text)
        self.assertIn("autoconnect-priority=-10", text)
        self.assertIn("autoconnect-retries=0", text)

    def test_it_is_2_4ghz_and_hidden(self):
        """手机热点几乎都在 2.4 GHz；hidden 让广播的和不广播的都认。"""
        _, text, _ = self.run_wifi()
        self.assertIn("band=bg", text)
        self.assertIn("hidden=true", text)

    def test_the_keyfile_is_not_world_readable(self):
        _, _, mode = self.run_wifi()
        self.assertEqual(mode, 0o600)

    def test_the_rescue_profile_carries_the_fallback_resolvers(self):
        """路由器不发 DNS（或 DHCP 比首次解析还晚）时，盒子仍然解得开名字。

        2026-10-25 真机（BG8GGG）：解析器为空 → chrony 解析不到 NTP pool → 时钟停在
        镜像日期、慢 24 天 → 每个 HTTPS 都报 `certificate is not yet valid`，
        而页面只说「连不上门户」。出厂默认两条公共 DNS 把这条链的根断掉。
        """
        _, text, _ = self.run_wifi()
        self.assertIn("dns=114.114.114.114;8.8.8.8;", text)

    def test_an_existing_base_image_profile_gets_them_too(self):
        """基底镜像自带连接（有线/Wi-Fi）不会经过 write_profile，所以镜像构建期
        要把它们也补上 —— 否则出厂盒子的那一条连接仍然没有兜底解析器。"""
        wired = ("[connection]\nid=Wired connection 1\ntype=ethernet\n\n"
                 "[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n")
        proc, _, _, seeded = self._run_wifi(seed={"Wired.nmconnection": wired})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("dns=114.114.114.114;8.8.8.8;", seeded["Wired.nmconnection"])
        # 其余内容原样保留（method 不能被顺手改掉）。
        self.assertIn("method=auto", seeded["Wired.nmconnection"])

    def test_the_fallback_dns_step_is_idempotent(self):
        """重跑一次不能写出第二行 dns=，也不能把已有的服务器顶掉。"""
        wired = ("[connection]\nid=Wired connection 1\ntype=ethernet\n\n"
                 "[ipv4]\ndns=192.168.1.1;\nmethod=auto\n")
        _, _, _, first = self._run_wifi(seed={"Wired.nmconnection": wired})
        self.assertIn("dns=192.168.1.1;114.114.114.114;8.8.8.8;",
                      first["Wired.nmconnection"])
        self.assertEqual(first["Wired.nmconnection"].count("dns="), 1)

    def test_a_batch_can_change_the_name_and_the_password(self):
        _, text, _ = self.run_wifi(extra={
            "MRRC_PREFLASH_RESCUE_SSID": "clinic",
            "MRRC_PREFLASH_RESCUE_PSK": "longerpassword"})
        self.assertIn("ssid=clinic", text)
        self.assertIn("psk=longerpassword", text)

    def test_a_password_that_wpa2_cannot_take_is_refused(self):
        proc, text, _ = self.run_wifi(extra={"MRRC_PREFLASH_RESCUE_PSK": "1234567"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(text, "")

    def test_a_newline_in_the_name_is_refused(self):
        proc, _, _ = self.run_wifi(extra={"MRRC_PREFLASH_RESCUE_SSID": "a\nb"})
        self.assertNotEqual(proc.returncode, 0)

    def test_the_flag_still_gates_it(self):
        proc, text, _ = self.run_wifi(flag=None)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(text, "")

    def test_env_only_does_not_write_a_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "MRRC_PREFLASH": "1",
                   "MRRC_PREFLASH_WIFI_DIR": tmp}
            subprocess.run(["bash", str(SCRIPT), "--env-only", str(Path(tmp) / "x.env")],
                           capture_output=True, text=True, env=env)
            self.assertFalse((Path(tmp) / "mrrc-rescue.nmconnection").exists())

