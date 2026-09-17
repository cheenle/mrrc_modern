"""Answers logic for the support chain's second half (spec 2026-09-17-support-autopilot §4–§7).

The page is public, so most of these tests are about what must NOT appear.
"""
import json
import tempfile
import unittest
from pathlib import Path

import support_answers as sa


def _analysis(**over):
    base = {
        "verdict": "录音写入端跟不上，音频被整段丢弃",
        "status": "needs_fix",
        "category": "音频",
        "diagnosis": ["日志出现 Recording writer is falling behind，且录音队列满"],
        "solution": ["先按停止键结束本次录音，再重启 MRRC Modern 让写入端重建"],
        "evidence": ["2026-09-17 22:26:53 [WARNING] mrrc: Recording dropped 950 block(s) so far"],
        "keys": ["录音", "Recording"],
        "needs_code_change": True,
        "code_hint": "server.py:_ensure_rec_writer",
    }
    base.update(over)
    return base


class ParseAnalysisTests(unittest.TestCase):
    def test_parses_a_json_object_wrapped_in_prose(self):
        raw = "分析如下：\n```json\n" + json.dumps(_analysis()) + "\n```\n以上。"
        parsed = sa.parse_analysis(raw)
        self.assertEqual(parsed["status"], "needs_fix")
        self.assertEqual(parsed["code_hint"], "server.py:_ensure_rec_writer")
        self.assertTrue(parsed["needs_code_change"])

    def test_rejects_missing_fields(self):
        raw = json.dumps({"verdict": "x", "status": "answered"})
        with self.assertRaises(ValueError) as ctx:
            sa.parse_analysis(raw)
        self.assertIn("缺少字段", str(ctx.exception))

    def test_rejects_unknown_status_and_category(self):
        for bad in ({"status": "也许"}, {"category": "玄学"}):
            with self.assertRaises(ValueError):
                sa.parse_analysis(json.dumps(_analysis(**bad)))

    def test_rejects_empty_solution(self):
        """A conclusion that does not tell the operator what to do is not an answer."""
        with self.assertRaises(ValueError) as ctx:
            sa.parse_analysis(json.dumps(_analysis(solution=[])))
        self.assertIn("solution", str(ctx.exception))

    def test_rejects_non_json(self):
        with self.assertRaises(ValueError):
            sa.parse_analysis("我不确定该怎么回答")


class PublishGateTests(unittest.TestCase):
    def test_only_answered_and_needs_fix_are_publishable(self):
        self.assertTrue(sa.should_publish(_analysis(status="answered")))
        self.assertTrue(sa.should_publish(_analysis(status="needs_fix")))
        self.assertFalse(sa.should_publish(_analysis(status="need_more_info")))


class PrivacyTests(unittest.TestCase):
    SENSITIVE = ("呼号 BH1XXX，邮箱 op@example.com，手机 13800138000；"
                 "串口 /dev/cu.usbserial-0121DB3A0 与 COM7；"
                 "内网 192.168.1.42；配置在 /Users/chen/Library/Application Support/MRRC-Modern")

    def test_every_identifying_class_is_removed(self):
        cleaned = sa.redact_public(self.SENSITIVE)
        for leaked in ("op@example.com", "13800138000", "usbserial-0121DB3A0", "COM7",
                       "192.168.1.42", "/Users/chen"):
            self.assertNotIn(leaked, cleaned, leaked)
        self.assertIn(sa.REDACTED_MARK, cleaned)

    def test_privacy_hits_reports_the_classes(self):
        hits = sa.privacy_hits(self.SENSITIVE)
        for label in ("邮箱", "手机号", "设备路径", "串口", "内网地址", "本机路径"):
            self.assertIn(label, hits)

    def test_card_and_page_never_contain_operator_identifiers(self):
        """The real gate: leak-free output, not a leak-free input."""
        analysis = _analysis(
            diagnosis=[f"操作员的描述：{self.SENSITIVE}"],
            evidence=[f"串口 /dev/cu.usbserial-0121DB3A0 打不开；来自 10.0.0.9"],
            code_hint="/Users/chen/repo/server.py",
        )
        card = sa.render_card(analysis, "20260917-072530-ab12", problem=self.SENSITIVE,
                             at="2026-09-17 09:00")
        page = sa.render_page([card])
        for leaked in ("op@example.com", "13800138000", "usbserial-0121DB3A0", "COM7",
                       "192.168.1.42", "10.0.0.9", "/Users/chen"):
            self.assertNotIn(leaked, page, leaked)


class CardTitleTests(unittest.TestCase):
    """The live card was titled "# 问题描述": problem.txt opens with a markdown header."""

    def test_title_skips_markdown_headers_and_blanks(self):
        problem = "# 问题描述\n\n录音是空的，MP3 只有静音\n\n# 联系方式\nsomeone@example.com\n"
        card = sa.render_card(_analysis(), "20260917-072530-ab12", problem=problem)
        self.assertIn("录音是空的", card)
        self.assertNotIn("问题描述", card)
        self.assertNotIn("someone@example.com", card)

    def test_title_falls_back_to_the_verdict(self):
        card = sa.render_card(_analysis(), "20260917-072530-ab12", problem="# 问题描述\n")
        self.assertIn("录音写入端跟不上", card)


class TempPathPrivacyTests(unittest.TestCase):
    def test_macos_temp_paths_are_filtered(self):
        """Found in the live card: /var/folders/... reached the drafted page."""
        cleaned = sa.redact_public("Recording ready: /var/folders/9x/abc/T/tmp1/recordings")
        self.assertNotIn("/var/folders", cleaned)
        self.assertIn(sa.REDACTED_MARK, cleaned)

    def test_linux_tmp_paths_are_filtered(self):
        self.assertNotIn("/tmp/x", sa.redact_public("dir=/tmp/x/recordings"))


class RenderTests(unittest.TestCase):
    def test_card_carries_the_label_and_the_evidence(self):
        card = sa.render_card(_analysis(), "20260917-072530-ab12", problem="录音是空的",
                             at="2026-09-17 09:00")
        for expected in ("20260917-072530-ab12", "录音是空的", "已定位，待修复", "诊断", "你要做的",
                         "原始证据", "Recording dropped 950 block(s)", "server.py:_ensure_rec_writer"):
            self.assertIn(expected, card)

    def test_card_id_is_the_anchor_for_deep_links(self):
        card = sa.render_card(_analysis(), "20260917-072530-ab12")
        self.assertIn('id="20260917-072530-ab12"', card)

    def test_page_has_search_deep_link_and_newest_first(self):
        older = sa.render_card(_analysis(verdict="旧"), "20260917-000000-aaaa", at="2026-09-16")
        newer = sa.render_card(_analysis(verdict="新"), "20260917-235959-bbbb", at="2026-09-17")
        page = sa.render_page([newer, older], generated_at="2026-09-17 10:00")
        self.assertLess(page.index("20260917-235959-bbbb"), page.index("20260917-000000-aaaa"))
        self.assertIn('id="search"', page)
        self.assertIn("location.hash", page)
        self.assertIn("最后更新：2026-09-17 10:00", page)

    def test_page_without_cards_explains_the_situation(self):
        page = sa.render_page([])
        self.assertIn("现在还没有答复", page)
        self.assertNotIn('class="card"', page)

    def test_rendering_escapes_html(self):
        card = sa.render_card(_analysis(verdict="<script>alert(1)</script>"), "20260917-000000-aaaa")
        self.assertNotIn("<script>alert(1)</script>", card)
        self.assertIn("&lt;script&gt;", card)


class StateTests(unittest.TestCase):
    def test_round_trip_and_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.assertEqual(sa.load_state(path), {})
            state = sa.record_result({}, "20260917-000000-aaaa", _analysis(), published=True,
                                    at="2026-09-17T09:00:00")
            sa.save_state(path, state)
            loaded = sa.load_state(path)
        self.assertEqual(loaded["20260917-000000-aaaa"]["status"], "needs_fix")
        self.assertTrue(loaded["20260917-000000-aaaa"]["published"])

    def test_corrupt_state_is_empty_not_an_exception(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(sa.load_state(path), {})

    def test_answered_cards_are_published_only_and_newest_first(self):
        state = {}
        sa.record_result(state, "a", _analysis(), published=True, at="2026-09-16T10:00:00")
        sa.record_result(state, "b", _analysis(status="need_more_info"), published=False,
                         at="2026-09-17T10:00:00")
        sa.record_result(state, "c", _analysis(), published=True, at="2026-09-17T11:00:00")
        rows = sa.answered_cards(state)
        self.assertEqual([bid for bid, _ in rows], ["c", "a"])


if __name__ == "__main__":
    unittest.main()
