"""SDR 接收机的接线段：口令角色隔离、页面、WS 握手与容量。

Handler 直接调用（与 tests/test_server_box_window.py 同一套路），
WS 用一个够小的假对象，不需要真的起服务器。
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException
from starlette.websockets import WebSocketDisconnect

import server
from sdr_hub import SdrHub


class FakeUrl:
    def __init__(self, path):
        self.path = path


class FakeRequest:
    def __init__(self, path="/sdr", cookies=None, body=None, headers=None, client="10.0.0.9"):
        self.url = FakeUrl(path)
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.client = type("C", (), {"host": client})()
        self.query_params = {}
        self._body = body or {}

    async def json(self):
        return self._body


class FakeWS:
    def __init__(self, token=None, messages=(), cookie_name="mrrc_sdr"):
        self.cookies = {cookie_name: token} if token else {}
        self.headers = {}
        self.query_params = {}
        self.accepted = False
        self.close_code = None
        self.close_reason = ""
        self.sent = []
        self._messages = list(messages)

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.close_code = code
        self.close_reason = reason

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def send_bytes(self, data):
        self.sent.append(("bytes", len(data)))

    async def receive_text(self):
        if self._messages:
            return self._messages.pop(0)
        raise WebSocketDisconnect(code=1000)


class SdrWiringTests(unittest.TestCase):
    def setUp(self):
        # 令牌存储指向临时文件：否则本文件的登录会把 token 写进真实的
        # data/auth-tokens.json，污染下一个跑持久化断言的用例。
        tmp = Path(tempfile.mkdtemp()) / "auth-tokens.json"
        patcher = mock.patch.object(server, "_AUTH_TOKENS_FILE", tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tokens_file = tmp
        self.saved_issued = dict(server._token_issued)
        self.saved_auth = set(server._auth_tokens)
        self.addCleanup(self._restore_sets)
        server.SDR_PASSWORD = "sdr-secret"
        server.SDR_ENABLED = True
        server._sdr_hub = None
        server._sdr_runner = None
        server._sdr_tokens.clear()
        self.addCleanup(self._restore)

    def _restore_sets(self):
        server._auth_tokens.clear()
        server._auth_tokens.update(self.saved_auth)
        server._token_issued.clear()
        server._token_issued.update(self.saved_issued)

    def _restore(self):
        server.SDR_PASSWORD = ""
        server.SDR_ENABLED = True
        server._sdr_hub = None
        server._sdr_runner = None
        server._sdr_tokens.clear()

    # -- 纯函数 ---------------------------------------------------------
    def test_disabled_without_password(self):
        server.SDR_PASSWORD = ""
        self.assertFalse(server._sdr_login_enabled())

    def test_disabled_when_switched_off(self):
        server.SDR_ENABLED = False
        self.assertFalse(server._sdr_login_enabled())

    def test_password_compare(self):
        self.assertTrue(server._sdr_password_matches("sdr-secret"))
        self.assertFalse(server._sdr_password_matches("nope"))

    def test_sdr_cookie_is_not_the_radio_cookie(self):
        self.assertNotEqual(server.SDR_COOKIE, server.AUTH_COOKIE)

    # -- 登录 -----------------------------------------------------------
    def test_login_rejects_wrong_password_without_cookie(self):
        resp = asyncio.run(server.api_sdr_login(FakeRequest(body={"password": "x"})))
        self.assertEqual(resp.status_code, 401)
        self.assertNotIn("set-cookie", {k.lower() for k in resp.headers})

    def test_login_issues_an_sdr_only_token(self):
        with mock.patch.object(server, "_check_login_rate_limit", return_value=True):
            resp = asyncio.run(server.api_sdr_login(FakeRequest(body={"password": "sdr-secret"})))
        self.assertEqual(resp.status_code, 200)
        token = [v for k, v in resp.headers.items() if k.lower() == "set-cookie"][0]
        self.assertIn("mrrc_sdr=", token)
        self.assertEqual(len(server._sdr_tokens), 1)
        self.assertEqual(len(server._auth_tokens.intersection(server._sdr_tokens)), 0,
                         "sdr token must not enter the radio token set")

    # -- 页面 -----------------------------------------------------------
    def test_page_404_when_disabled(self):
        server.SDR_PASSWORD = ""
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(server.sdr_page(FakeRequest()))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_page_serves_its_own_login_without_a_cookie(self):
        resp = asyncio.run(server.sdr_page(FakeRequest()))
        self.assertEqual(resp.status_code, 200)
        body = resp.body.decode()
        self.assertIn("api/sdr/login", body)

    # -- WebSocket ------------------------------------------------------
    def test_ws_rejects_the_radio_token(self):
        radio = "radio-token"
        server._auth_tokens.add(radio)
        self.addCleanup(server._auth_tokens.discard, radio)
        ws = FakeWS(token=radio)
        asyncio.run(server.ws_sdr(ws))
        self.assertEqual(ws.close_code, 4001)
        self.assertFalse(ws.accepted)

    def test_ws_accepts_an_sdr_token_and_says_hello(self):
        token = "sdr-token"
        server._sdr_tokens.add(token)
        ws = FakeWS(token=token)
        with mock.patch.object(server.SdrPipeRunner, "ensure_started", new=mock.AsyncMock()):
            asyncio.run(server.ws_sdr(ws))
        self.assertTrue(ws.accepted)
        self.assertEqual(ws.sent[0]["event_type"], "hello")
        self.assertIn("AM", ws.sent[0]["value"]["modes"])

    def test_ws_reports_capacity_instead_of_hanging_up_silently(self):
        token = "sdr-token-2"
        server._sdr_tokens.add(token)
        server._sdr_hub = SdrHub(max_clients=0)
        ws = FakeWS(token=token)
        with mock.patch.object(server.SdrPipeRunner, "ensure_started", new=mock.AsyncMock()):
            asyncio.run(server.ws_sdr(ws))
        self.assertEqual(ws.close_code, 4003)
        self.assertIn("listeners", ws.close_reason)

    def test_ws_frames_have_the_documented_layout(self):
        kind, payload = server._sdr_ws_encode(("spectrum", 5, b"\x01\x02\x03"))
        self.assertEqual(kind, "bytes")
        self.assertEqual(payload[0], 0x01)
        self.assertEqual(payload[1:5], b"\x05\x00\x03\x00")
        kind, payload = server._sdr_ws_encode(("audio", 2, 7, 0.5, b"pkt"))
        self.assertEqual(kind, "bytes")
        self.assertEqual(payload[0], 0x02)
        self.assertEqual(payload[-3:], b"pkt")
        kind, payload = server._sdr_ws_encode(("state", {"center_hz": 1}))
        self.assertEqual(kind, "text")
        self.assertEqual(json.loads(payload)["event_type"], "state")


if __name__ == "__main__":
    unittest.main()
