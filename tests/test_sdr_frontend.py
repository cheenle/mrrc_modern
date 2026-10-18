"""SDR 页面的契约测试：控件、资源、前缀安全、凭据不进 URL。

页面是独立的一份（不加载 ft710_main.js / ft710_ui.js），所以这里的断言
就是它和那些受保护文件之间的边界。
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "static", "sdr.html")
JS = os.path.join(ROOT, "static", "sdr.js")


class SdrFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(HTML, encoding="utf-8") as f:
            cls.html = f.read()
        with open(JS, encoding="utf-8") as f:
            cls.js = f.read()

    def test_required_elements_exist(self):
        for eid in ("ruler", "wf", "center", "cursor", "offset", "presets", "modes",
                    "bw", "squelch", "volume", "levelBar", "banner", "toast",
                    "cListeners", "cLock", "cState"):
            with self.subTest(id=eid):
                self.assertIn(f'id="{eid}"', self.html)

    def test_reuses_the_existing_audio_stack(self):
        self.assertIn("modules/opus_wasm.js", self.html)
        self.assertIn("modules/opus_codec.js", self.html)
        self.assertIn("rx_worklet_processor.js", self.js)
        self.assertIn("new OpusDecoder(48000, 1)", self.js)

    def test_does_not_load_the_guarded_monoliths(self):
        self.assertNotIn("ft710_main.js", self.html)
        self.assertNotIn("ft710_ui.js", self.html)

    def test_talks_the_documented_protocol(self):
        for event in ("tune", "channel", "cursor", "squelch", "volume"):
            with self.subTest(event=event):
                self.assertIn(f'"{event}"', self.js)
        self.assertIn("/WSsdr", self.js)
        # 二进制帧的两个 tag
        self.assertIn("0x01", self.js)
        self.assertIn("0x02", self.js)

    def test_no_credentials_in_urls(self):
        for blob, name in ((self.html, "html"), (self.js, "js")):
            with self.subTest(file=name):
                self.assertNotIn("token=", blob)
                self.assertNotIn("?token", blob)

    def test_asset_urls_are_prefix_safe(self):
        """页面可能挂在 /mrrc_modern/<call>/sdr 下：资源必须相对。"""
        for attr in re.findall(r'(?:src|href)="([^"]+)"', self.html):
            if attr.startswith(("http://", "https://", "data:", "#")):
                continue
            with self.subTest(asset=attr):
                self.assertFalse(attr.startswith("/"), f"{attr} is root-absolute")

    def test_no_inline_logic_in_the_page(self):
        """与 index.html 同一条规矩：逻辑住在 .js 里。"""
        scripts = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", self.html, re.S)
        for body in scripts:
            self.assertLess(len(body.strip()), 200, "inline script too large")

    def test_page_is_mobile_first(self):
        self.assertIn('name="viewport"', self.html)
        self.assertIn("env(safe-area-inset-bottom", self.html)


if __name__ == "__main__":
    unittest.main()
