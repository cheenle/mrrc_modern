"""助手路由：角色门、开关、降级 —— 与 /api/perf 同一套规矩。

不测 HTTP 层（那要起整个 app）；这里测的是三条路由**自己的**决策：谁能过、关掉之后什么样、
助手挂掉时会不会连累主链路。
"""
import asyncio
import unittest
from contextlib import ExitStack
from unittest import mock

import server


def _request(headers=None):
    req = mock.Mock()
    req.headers = headers or {}
    req.client = mock.Mock(host="127.0.0.1")
    return req


class AssistantGateTests(unittest.TestCase):
    def test_disabled_returns_404(self):
        with mock.patch.object(server, "ASSISTANT_ENABLED", False):
            resp = asyncio.run(server.api_assistant_status(_request()))
        self.assertEqual(resp.status_code, 404)

    def test_listen_session_is_forbidden(self):
        with mock.patch.object(server, "ASSISTANT_ENABLED", True), \
                mock.patch.object(server, "_verify_auth", return_value=True), \
                mock.patch.object(server, "_perf_forbidden", return_value=(403, "listen-only session")):
            resp = asyncio.run(server.api_assistant_status(_request()))
        self.assertEqual(resp.status_code, 403)

    def test_unauthenticated_is_401(self):
        with mock.patch.object(server, "ASSISTANT_ENABLED", True), \
                mock.patch.object(server, "_verify_auth", return_value=False):
            resp = asyncio.run(server.api_assistant_status(_request()))
        self.assertEqual(resp.status_code, 401)

    def test_status_reports_whether_a_gateway_is_configured(self):
        with mock.patch.object(server, "ASSISTANT_ENABLED", True), \
                mock.patch.object(server, "_verify_auth", return_value=True), \
                mock.patch.object(server, "_perf_forbidden", return_value=None), \
                mock.patch.object(server, "ASSISTANT_URL", ""):
            resp = asyncio.run(server.api_assistant_status(_request()))
        self.assertEqual(resp.status_code, 200)


class AssistantAskTests(unittest.IsolatedAsyncioTestCase):
    async def _ask(self, **patches):
        req = mock.Mock()
        req.headers = {}
        req.client = mock.Mock(host="127.0.0.1")
        req.json = mock.AsyncMock(return_value={"message": "问一句"})
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(server, "ASSISTANT_ENABLED", True))
            stack.enter_context(mock.patch.object(server, "_verify_auth", return_value=True))
            stack.enter_context(mock.patch.object(server, "_perf_forbidden", return_value=None))
            for key, value in patches.items():
                stack.enter_context(mock.patch.object(server, key, value))
            resp = await server.api_assistant_ask(req)
        return resp.status_code, resp.body

    async def test_without_a_gateway_it_says_so_instead_of_failing(self):
        status, _body = await self._ask(ASSISTANT_URL="")
        self.assertEqual(status, 200)

    async def test_gateway_exception_still_returns_200_and_degrades(self):
        class Boom:
            def __init__(self, *a, **k):
                pass

            def ask(self, *a, **k):
                raise RuntimeError("boom")

        status, body = await self._ask(ASSISTANT_URL="https://gw/v1",
                                       assistant_client=mock.Mock(AssistantClient=Boom,
                                                                  offline=server.assistant_client.offline))
        self.assertEqual(status, 200)
        self.assertIn(b"degraded", body)


class PromptStateTests(unittest.TestCase):
    def test_summary_never_carries_identity_or_frequency(self):
        state = server._assistant_prompt_state()
        self.assertEqual(set(state), set(server.assistant_client.ALLOWED_KEYS))
        self.assertNotIn("callsign", state)
        self.assertNotIn("vfo_a_freq", state)

    def test_recent_errors_are_bounded_and_normalized(self):
        with mock.patch.object(server, "_ERROR_RING", server.deque(maxlen=8)):
            for i in range(10):
                server._note_error("audio", f"error   {i}\nwith newline")
            self.assertEqual(len(server._recent_error_codes(3)), 3)
            self.assertTrue(server._recent_error_codes(3)[0].startswith("audio:error"))


class AssistantPageTests(unittest.TestCase):
    """面板是静态文件 —— 断言它真的调了那两条路由，且菜单能点到。"""

    @classmethod
    def setUpClass(cls):
        cls.static = server.STATIC_DIR

    def test_page_and_script_call_the_routes(self):
        html = (self.static / "assistant.html").read_text(encoding="utf-8")
        js = (self.static / "assistant.js").read_text(encoding="utf-8")
        self.assertIn("assistant.js", html)
        self.assertIn("/api/assistant/ask", js)
        self.assertIn("/api/assistant/clear", js)
        self.assertIn("待确认", js)
        self.assertIn("本机不留知识库", js)

    def test_menu_links_the_page(self):
        html = (self.static / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="assistant.html"', html)
