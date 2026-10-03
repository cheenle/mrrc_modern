"""The enrolment secret must be reachable from the panel a user actually sees.

Reported on 1.24.7: someone applied, the operator sent a one-time secret, and the dialog offered only
Refresh - the secret box lived in the form panel, which is hidden as soon as an application exists.
The button added in the pending panel is the fix; this test keeps both halves present.
"""
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ClaimUiTests(unittest.TestCase):
    def test_pending_panel_has_a_secret_field_and_button(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="cloud-secret-pending"', html)
        self.assertIn('id="cloud-claim"', html)
        pending = html.index('id="cloud-pending"')
        self.assertGreater(html.index('id="cloud-secret-pending"'), pending,
                           "the secret field must live inside the pending panel")

    def test_javascript_wires_the_button(self):
        js = (ROOT / "static" / "modules" / "cloud_hub.js").read_text(encoding="utf-8")
        self.assertIn("cloud-claim", js)
        self.assertIn("lastState", js)

    def test_form_field_says_what_it_is_for(self):
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("登记口令", html)



class _Controls(HTMLParser):
    """把一段 HTML 里的 button / input 及其内联样式挑出来（正则数不清跨行的 style）。"""

    def __init__(self):
        super().__init__()
        self.buttons, self.inputs = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "button":
            self.buttons.append(a)
        elif tag == "input":
            self.inputs.append(a)


def _background(attrs: dict) -> str:
    for decl in (attrs.get("style") or "").split(";"):
        key, _, value = decl.partition(":")
        if key.strip() == "background":
            return value.strip()
    return ""


class CloudButtonAffordanceTests(unittest.TestCase):
    """待办面板里的按钮必须看起来是能按的 —— 因为那里没有别的出路。

    2026-10-03 实测：租户看着待批面板说"刷新状态是灰色的"，于是没有点它。它其实能点
    （没有 disabled、带 cursor:pointer），但底色和它上面的输入框**一模一样**（都是 #333），
    读起来像被关掉了。同一天 hub 的 nginx 日志显示：这个按钮发出的 /status 从来没到过。

    为什么值得一条守卫：这个面板没有后备通道。v1.24.0 起接入只在应用里完成，租户没有任何
    命令可跑 —— portal 自己在 granted 记录里写的 next_step 就是"点刷新状态即自动完成"。
    所以一个看起来像禁用的按钮，等于把已经批准的人永远留在"等待批准"上。

    范围只取 cloud-form 与 cloud-pending（即 cloud-done 之前的那两个面板）：那是租户必须
    按下去才能往前走的地方。对话框右上角的关闭按钮不在此列 —— 它长得像输入框没有同等后果。
    """

    def _panels(self) -> _Controls:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        start = html.index('id="cloud-apply"')          # cloud-form 的最后一个控件
        start = html.rindex('id="cloud-form"', 0, start)
        end = html.index('id="cloud-done"')
        probe = _Controls()
        probe.feed(html[start:end])
        return probe

    def test_the_slice_really_holds_the_panels_it_claims_to(self):
        probe = self._panels()
        ids = [b.get("id") for b in probe.buttons]
        for expected in ("cloud-apply", "cloud-claim", "cloud-refresh"):
            self.assertIn(expected, ids)
        self.assertGreaterEqual(len(probe.inputs), 3,
                                "取到的片段里没有输入框 —— 断言会空跑")

    def test_no_way_forward_is_disabled(self):
        for button in self._panels().buttons:
            self.assertNotIn("disabled", button,
                             f"{button.get('id')} 被禁用了，而这个面板没有别的出路")

    def test_every_way_forward_looks_clickable(self):
        """不许和输入框共用底色：那正是"看起来像禁用了"的来源。"""
        probe = self._panels()
        inert = {_background(i) for i in probe.inputs}
        self.assertNotIn("", inert, "输入框应当有底色，否则这条断言形同虚设")
        for button in probe.buttons:
            style = button.get("style") or ""
            self.assertIn("cursor: pointer", style,
                          f"{button.get('id')} 没有 cursor:pointer，看不出能点")
            self.assertNotIn(_background(button), inert,
                             f"{button.get('id')} 的底色和输入框相同（{_background(button)}）"
                             " —— 看起来会像被关掉的")


if __name__ == "__main__":
    unittest.main()
