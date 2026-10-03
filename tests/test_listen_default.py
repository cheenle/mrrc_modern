"""Windows 的默认侦听地址必须与平台文档一致：所有接口，而不是只有回环。

SDD/12-operational-model.md 写的是 "Uvicorn on 0.0.0.0:8888 (configurable via
MRRC_WEB_HOST / MRRC_WEB_PORT)"；config.py 的兜底是 `::`（IPv6 双栈＝所有接口）；
macOS 模板是 `::`，Linux 的 install.sh 是 `0.0.0.0` —— **只有 Windows 这两处写着
127.0.0.1**，于是同一个产品在 Windows 上默认只能本机访问。

代价是现场实测出来的：按文档以为能从手机或局域网另一台机器打开，实际连不上
（Mac → `192.168.1.53:8888` 一直是 refused，直到把模板改成 0.0.0.0，随后直连立刻通）。

选 0.0.0.0 而不是 `::`：两者都能被 `local_url()` 映射成可打开的地址，但 Windows 上
真实走的是 IPv4（手机、浏览器书签里的 192.168.x.x）。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALL_INTERFACES = {"0.0.0.0", "::"}


def _template_value() -> str:
    path = ROOT / "windows" / "default.env"
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.startswith("MRRC_WEB_HOST=")]
    assert len(lines) == 1, f"模板里 MRRC_WEB_HOST 出现 {len(lines)} 次"
    return lines[0].split("=", 1)[1].strip()


def _fallback_value() -> str:
    """`ensure_config()` 在模板缺失时写下的那一份（只在这一个函数里找）。"""
    source = (ROOT / "windows" / "launcher.py").read_text(encoding="utf-8")
    body = source[source.index("def ensure_config("):source.index("def seed_mem_channels(")]
    found = re.findall(r"MRRC_WEB_HOST=([0-9a-fA-F.:]+)", body)
    assert len(found) == 1, f"ensure_config 里 MRRC_WEB_HOST 出现 {len(found)} 次"
    return found[0]


class WindowsListenDefaultTests(unittest.TestCase):
    def test_the_shipped_template_listens_on_every_interface(self):
        value = _template_value()
        self.assertIn(
            value, ALL_INTERFACES,
            f"windows/default.env 用 {value!r} 作侦听地址 —— 装完只有本机能打开，"
            "而 SDD 与另外两个平台都是所有接口")

    def test_the_missing_template_fallback_matches_the_template(self):
        self.assertIn(
            _fallback_value(), ALL_INTERFACES,
            "模板缺失时的兜底又回到只监听本机；这与随包模板不一致，"
            "同一个产品会出现两种可达性")

    def test_both_places_agree(self):
        self.assertEqual(_template_value(), _fallback_value(),
                         "随包模板与兜底写了不同的地址，行为会随模板是否存在而变")


if __name__ == "__main__":
    unittest.main()
