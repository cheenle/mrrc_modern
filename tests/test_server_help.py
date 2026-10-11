"""The three local endpoints of `call help`.

They are a **local** action by design: whoever can use this UI is the person who
decides whether a support door exists. What must still hold:

* anonymous callers get nothing (the web UI is reachable from the LAN and, for a
  connected instance, from the internet through the tunnel);
* the status reply carries the password **only while a session is open**, because
  that reply is what the customer reads out loud;
* a refusal (no Cloud Hub identity, tunnel reload failed, portal down) comes back
  as a message the UI can show, not as a 500.
"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import help_session
import server


class _FakeUrl:
    def __init__(self, path):
        self.path = path


class FakeRequest:
    def __init__(self, body=None, cookies=None):
        self.url = _FakeUrl("/api/help/status")
        self.method = "POST"
        self.client = None
        self.cookies = cookies or {}
        self.headers = {}
        self.query_params = {}
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


def run(coro):
    return asyncio.run(coro)


def _req(body=None):
    """A FakeRequest typed as the handler's parameter.

    The repo already calls handlers directly with a hand-rolled request (see
    tests/test_server_box_window.py); the single ignore lives here so the twenty-odd
    call sites below stay type-checked.
    """
    return FakeRequest(body)      # type: ignore[return-value]


def _reply(response) -> dict:
    import json
    return json.loads(bytes(response.body).decode())


class AuthorisationTests(unittest.TestCase):
    def test_anonymous_cannot_open_a_window(self):
        with mock.patch.object(server, "_verify_auth", return_value=False):
            resp = run(server.api_help_enable(_req({"minutes": 60})))
        self.assertEqual(resp.status_code, 401)

    def test_anonymous_cannot_read_the_status(self):
        with mock.patch.object(server, "_verify_auth", return_value=False):
            resp = run(server.api_help_status(_req()))
        self.assertEqual(resp.status_code, 401)

    def test_anonymous_cannot_close_a_window(self):
        with mock.patch.object(server, "_verify_auth", return_value=False):
            resp = run(server.api_help_disable(_req()))
        self.assertEqual(resp.status_code, 401)


class _WiredSession(unittest.TestCase):
    """A session whose three collaborators are fakes, installed as the singleton."""

    def setUp(self):
        self.portal_calls = []
        self.tunnel_calls = []
        session = help_session.build_session(
            portal_opener=self._opener, tunnel=self._tunnel, portal_closer=self._closer)
        self._patch = mock.patch.object(server, "_help_singleton", session)
        self._patch.start()
        self._patch_auth = mock.patch.object(server, "_verify_auth", return_value=True)
        self._patch_auth.start()
        self.session = session

    def tearDown(self):
        self.session.close("test")
        self._patch_auth.stop()
        self._patch.stop()

    def _opener(self, minutes):
        self.portal_calls.append(("request", minutes))
        return 19000

    def _tunnel(self, local_port, remote_port, add):
        self.tunnel_calls.append((local_port, remote_port, add))

    def _closer(self):
        self.portal_calls.append(("close", 0))


class EnableDisableTests(_WiredSession):
    def test_enable_opens_and_returns_the_password_to_this_caller(self):
        resp = run(server.api_help_enable(_req({"minutes": 30})))
        self.assertEqual(resp.status_code, 200)
        payload = _reply(resp)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["state"], help_session.OPEN)
        self.assertEqual(payload["port"], 19000)
        self.assertTrue(payload["password"])
        self.assertEqual(payload["username"], self.session.status()["username"])
        self.assertEqual([c[2] for c in self.tunnel_calls], [True])

    def test_status_carries_the_password_only_while_open(self):
        run(server.api_help_enable(_req({"minutes": 30})))
        open_reply = _reply(run(server.api_help_status(_req())))
        self.assertIn("password", open_reply)
        run(server.api_help_disable(_req()))
        closed_reply = _reply(run(server.api_help_status(_req())))
        self.assertNotIn("password", closed_reply)
        self.assertEqual(closed_reply["state"], help_session.CLOSED)
        self.assertEqual(closed_reply["close_reason"], "user")

    def test_disable_is_idempotent(self):
        run(server.api_help_enable(_req({"minutes": 30})))
        first = _reply(run(server.api_help_disable(_req())))
        second = _reply(run(server.api_help_disable(_req())))
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(second["close_reason"], "user", "第一条原因胜出")

    def test_the_customer_can_bring_their_own_credentials(self):
        resp = run(server.api_help_enable(_req(
            {"minutes": 30, "username": "zhang-san", "password": "long-enough-pw"})))
        payload = _reply(resp)
        self.assertEqual(payload["username"], "zhang-san")
        self.assertEqual(payload["password"], "long-enough-pw")

    def test_a_bad_username_comes_back_as_a_message_not_a_500(self):
        resp = run(server.api_help_enable(_req(
            {"minutes": 30, "username": "has space"})))
        self.assertEqual(resp.status_code, 400)
        payload = _reply(resp)
        self.assertFalse(payload["ok"])
        self.assertIn("username", payload["reason"])
        self.assertEqual(self.session.state, help_session.CLOSED)

    def test_a_failing_tunnel_is_reported_and_leaves_nothing_open(self):
        def boom(local_port, remote_port, add):
            raise RuntimeError("frpc reload returned HTTP 500")
        failing = help_session.build_session(portal_opener=lambda m: 19000, tunnel=boom)
        with mock.patch.object(server, "_help_singleton", failing):
            resp = run(server.api_help_enable(_req({"minutes": 30})))
            # 断言在 patch 块里读：块外读到的又是 setUp 那个会话。
            self.assertEqual(failing.state, help_session.CLOSED)
            self.assertFalse(failing.password_for_display())
            self.assertGreaterEqual(failing.status()["failures"], 0)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("frpc reload", _reply(resp)["reason"])

    def test_no_portal_identity_is_a_message_not_a_500(self):
        """没接入 Cloud Hub 的机器：说明白，而不是抛 500。

        这个会话必须用**真**的 opener（server._help_opener），否则测的是假货把
        portal 上下文替换掉这件事，而不是"没有身份时会怎样"。
        """
        real = help_session.build_session(
            portal_opener=server._help_opener,
            tunnel=lambda *a: None, portal_closer=None)
        with mock.patch.object(server, "_help_singleton", real), \
             mock.patch.object(
                 server, "_help_portal_context",
                 side_effect=server.cloud_hub.CloudHubError("这台机器还没接入 Cloud Hub")):
            resp = run(server.api_help_enable(_req({"minutes": 30})))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Cloud Hub", _reply(resp)["reason"])


class PortalProtocolTests(unittest.TestCase):
    """cloud_hub 的请求体字段：门户那边的白名单就照这个来（设计 §4.3）。"""

    def _capture(self, func, *args, **kw):
        seen = {}

        def fake_post(portal, route, payload, timeout=None):
            seen.update(route=route, payload=payload)
            return {"ok": True, "port": 18877}

        with mock.patch.object(server.cloud_hub, "_post", fake_post):
            func(*args, **kw)
        return seen

    def test_the_request_body_has_no_password_slot(self):
        import cloud_hub
        seen = self._capture(cloud_hub.help_request, "https://p", "BG7IFP", "tok", 60, "help-ab12")
        self.assertEqual(seen["route"], "/help/request")
        self.assertEqual(set(seen["payload"]),
                         {"callsign", "token", "minutes", "username"})

    def test_the_heartbeat_carries_what_the_portal_displays(self):
        import cloud_hub
        seen = self._capture(cloud_hub.help_heartbeat, "https://p", "BG7IFP", "tok", 18877, 3)
        self.assertEqual(set(seen["payload"]),
                         {"callsign", "token", "port", "connections"})

    def test_the_close_carries_no_port(self):
        import cloud_hub
        seen = self._capture(cloud_hub.help_close, "https://p", "BG7IFP", "tok")
        self.assertEqual(set(seen["payload"]), {"callsign", "token"})


if __name__ == "__main__":
    unittest.main()
