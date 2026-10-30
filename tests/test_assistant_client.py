"""白名单摘要 + 转发 + 降级 —— 出网用注入的 opener，测试不联网。"""
import io
import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import assistant_client as ac  # noqa: E402 - 仓库根入 sys.path 之后再导入

STATE = {
    "model": "ft710", "version": "1.28.22", "fixpack": "FP18",
    "serial_connected": True, "audio_rx_open": True, "audio_tx_open": False,
    "last_errors": ["audio:-9999", "cat:timeout", "ft8:stack", "extra:dropped"],
    "platform": "linux-arm64", "ui_lang": "zh",
    # 下面这些**永远不该**被发出去
    "callsign": "BG8GGG", "vfo_a_freq": 7050000, "recording": "/var/x.mp3",
}


class SummaryTests(unittest.TestCase):
    def test_exact_key_set(self):
        self.assertEqual(set(ac.state_summary(STATE)), {
            "model", "version", "fixpack", "serial_connected", "audio_rx_open",
            "audio_tx_open", "last_errors", "platform", "ui_lang"})

    def test_no_forbidden_value_leaks(self):
        blob = json.dumps(ac.state_summary(STATE), ensure_ascii=False)
        for needle in ("BG8GGG", "7050000", ".mp3", "/dev/"):
            self.assertNotIn(needle, blob)

    def test_size_budget(self):
        self.assertLessEqual(len(json.dumps(ac.state_summary(STATE))), 2048)

    def test_last_errors_capped_at_three_and_truncated(self):
        summary = ac.state_summary(dict(STATE, last_errors=["e" * 500] * 10))
        self.assertEqual(len(summary["last_errors"]), 3)
        self.assertLessEqual(len(summary["last_errors"][0]), 120)

    def test_missing_fields_are_omitted_not_invented(self):
        self.assertEqual(set(ac.state_summary({"model": "ft710"})), {"model", "last_errors"})


class AskTests(unittest.TestCase):
    def _client(self, opener):
        return ac.AssistantClient("https://gw/v1", "tok", opener=opener)

    def test_answer_is_forwarded_back_verbatim(self):
        payload = {"answer": "好", "citations": [], "mode": "kb", "needs_confirm": False,
                   "degraded": ""}

        def opener(req, timeout):
            self.assertTrue(req.full_url.endswith("/assistant/ask"))
            self.assertEqual(req.get_header("X-mrrc-instance"), "tok")
            body = json.loads(req.data.decode())
            self.assertEqual(set(body["state"]), set(ac.ALLOWED_KEYS) - {"ui_lang"} | {"ui_lang"})
            return io.BytesIO(json.dumps(payload).encode())

        self.assertEqual(self._client(opener).ask(None, "问", STATE)["answer"], "好")

    def test_network_failure_returns_offline_copy(self):
        def opener(req, timeout):
            raise OSError("no route")

        answer = self._client(opener).ask(None, "问", STATE)
        self.assertEqual(answer["degraded"], "offline")
        self.assertIn("需要联网", answer["answer"])

    def test_non_json_response_also_degrades(self):
        def opener(req, timeout):
            return io.BytesIO(b"<html>502</html>")

        self.assertEqual(self._client(opener).ask(None, "问", STATE)["mode"], "offline")

    def test_clear_uses_delete(self):
        seen = {}

        def opener(req, timeout):
            seen["method"] = req.get_method()
            seen["url"] = req.full_url
            return io.BytesIO(json.dumps({"deleted": True}).encode())

        self.assertTrue(self._client(opener).clear("c-1")["deleted"])
        self.assertEqual(seen["method"], "DELETE")
        self.assertTrue(seen["url"].endswith("/assistant/conversation"))
