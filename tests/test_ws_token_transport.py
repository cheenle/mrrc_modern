"""Session-token transport: cookie for browsers, header for native clients (AD-024).

Why this module exists: the web client used to carry a 30-day session token in
`?token=`. That put the credential into every access log, the browser history and
any Referer — and the instance runs uvicorn's default access log, so the public
`/mrrc_modern/listen` proxy would have logged it too. The interesting tests are
therefore about what must *not* happen (no token in any URL the frontend builds)
and about the resolution order that keeps installed native clients working.

Runs without hardware; `import server` follows the existing suite pattern
(test_atr1000_server).
"""
import re
import unittest
from pathlib import Path
from unittest import mock

import server

SERVER_SOURCE = Path("server.py").read_text(encoding="utf-8")


class _FakeRequest:
    """Minimal stand-in for a Starlette Request/WebSocket handshake."""

    def __init__(self, *, headers=None, cookies=None, query=None):
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.query_params = query or {}


class BearerParsingTests(unittest.TestCase):
    def test_bearer_scheme_is_case_insensitive_and_trimmed(self):
        self.assertEqual(server._bearer_token("Bearer abc123"), "abc123")
        self.assertEqual(server._bearer_token("bearer   abc123  "), "abc123")
        self.assertEqual(server._bearer_token("BEARER abc123"), "abc123")

    def test_only_bearer_is_accepted(self):
        self.assertEqual(server._bearer_token("Basic abc123"), "")
        self.assertEqual(server._bearer_token("abc123"), "")
        self.assertEqual(server._bearer_token("Bearer"), "")

    def test_missing_header_is_empty(self):
        self.assertEqual(server._bearer_token(None), "")
        self.assertEqual(server._bearer_token(""), "")


class ResolutionOrderTests(unittest.TestCase):
    def setUp(self):
        server._query_token_warned = False
        self.addCleanup(setattr, server, "_query_token_warned", False)

    def test_header_wins_over_cookie_and_query(self):
        req = _FakeRequest(headers={"authorization": "Bearer from-header"},
                           cookies={"mrrc_auth": "from-cookie"},
                           query={"token": "from-query"})
        self.assertEqual(server._token_from_request(req), "from-header")

    def test_cookie_wins_over_query(self):
        req = _FakeRequest(cookies={"mrrc_auth": "from-cookie"},
                           query={"token": "from-query"})
        self.assertEqual(server._token_from_request(req), "from-cookie")

    def test_query_is_the_last_resort(self):
        req = _FakeRequest(query={"token": "from-query"})
        self.assertEqual(server._token_from_request(req), "from-query")

    def test_nothing_present_yields_empty(self):
        self.assertEqual(server._token_from_request(_FakeRequest()), "")

    def test_websocket_handshake_uses_the_same_order(self):
        ws = _FakeRequest(cookies={"mrrc_auth": "cookie-token"})
        self.assertEqual(server._token_from_ws_handshake(ws), "cookie-token")
        ws = _FakeRequest(headers={"authorization": "Bearer header-token"})
        self.assertEqual(server._token_from_ws_handshake(ws), "header-token")
        ws = _FakeRequest(query={"token": "query-token"})
        self.assertEqual(server._token_from_ws_handshake(ws), "query-token")


class QueryDeprecationTests(unittest.TestCase):
    """An already-installed native client must keep working — but the operator
    has to learn about it, once, not once per reconnect."""

    def setUp(self):
        server._query_token_warned = False
        self.addCleanup(setattr, server, "_query_token_warned", False)

    def test_query_token_logs_one_warning_naming_the_cookie(self):
        with mock.patch.object(server.logger, "warning") as warn:
            server._token_from_request(_FakeRequest(query={"token": "t"}))
            server._warn_query_token_once()
            server._token_from_request(_FakeRequest(query={"token": "t"}))
        self.assertEqual(warn.call_count, 1)
        message = warn.call_args[0][0]
        self.assertIn("deprecated", message)
        self.assertIn("Authorization: Bearer", message)
        self.assertIn("%s", message)          # cookie name is a parameter
        self.assertIn(server.AUTH_COOKIE, warn.call_args[0])

    def test_header_and_cookie_do_not_warn(self):
        with mock.patch.object(server.logger, "warning") as warn:
            server._token_from_request(_FakeRequest(cookies={"mrrc_auth": "t"}))
            server._token_from_request(
                _FakeRequest(headers={"authorization": "Bearer t"}))
        warn.assert_not_called()


class VerificationTests(unittest.TestCase):
    def setUp(self):
        server._query_token_warned = False
        self.addCleanup(setattr, server, "_query_token_warned", False)
        self.token = "unit-test-token-abc"
        server._auth_tokens.add(self.token)
        server._listen_tokens.add(self.token)
        self.addCleanup(server._auth_tokens.discard, self.token)
        self.addCleanup(server._listen_tokens.discard, self.token)

    def test_verify_auth_accepts_every_transport(self):
        self.assertTrue(server._verify_auth(
            _FakeRequest(cookies={"mrrc_auth": self.token})))
        self.assertTrue(server._verify_auth(
            _FakeRequest(headers={"authorization": f"Bearer {self.token}"})))
        self.assertTrue(server._verify_auth(
            _FakeRequest(query={"token": self.token})))

    def test_verify_auth_rejects_unknown_and_empty(self):
        self.assertFalse(server._verify_auth(_FakeRequest()))
        self.assertFalse(server._verify_auth(
            _FakeRequest(cookies={"mrrc_auth": "nope"})))
        self.assertFalse(server._verify_auth(
            _FakeRequest(headers={"authorization": "Bearer nope"})))

    def test_listen_request_recognised_from_header(self):
        self.assertTrue(server._is_listen_request(
            _FakeRequest(headers={"authorization": f"Bearer {self.token}"})))
        self.assertFalse(server._is_listen_request(_FakeRequest()))


class SourceGuardTests(unittest.TestCase):
    @staticmethod
    def _js_code(src: str) -> str:
        """Strip JS comments before scanning.

        The files explain *why* they no longer append `?token=`, so the literal
        appears in prose — only code counts.
        """
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        return re.sub(r"//[^\n]*", "", src)

    def test_no_websocket_endpoint_reads_the_query_param_directly(self):
        """Only the shared resolver may touch the deprecated query param."""
        self.assertEqual(SERVER_SOURCE.count('ws.query_params.get("token"'), 1)
        self.assertIn('ws.query_params.get("token", "")',
                      SERVER_SOURCE.split("def _token_from_ws_handshake", 1)[1]
                      .split("def _token_from_request", 1)[0])
        self.assertEqual(SERVER_SOURCE.count("_token_from_ws_handshake(ws)"), 5)

    def test_http_paths_route_through_the_shared_resolver(self):
        self.assertIn("return _token_from_request(request) in _auth_tokens",
                      SERVER_SOURCE)
        self.assertIn("return _token_from_request(request) in _listen_tokens",
                      SERVER_SOURCE)

    def test_frontend_builds_no_token_urls(self):
        for path in ("static/ft710_main.js", "static/listen.js",
                     "static/modules/atr1000.js"):
            code = self._js_code(Path(path).read_text(encoding="utf-8"))
            self.assertNotIn("?token=", code, path)
            self.assertNotIn("'token=' + encodeURIComponent", code, path)
            self.assertNotIn('"token=" + encodeURIComponent', code, path)

    def test_frontend_still_guards_on_a_session_being_present(self):
        """Dropping the URL token must not drop the login check with it."""
        self.assertIn("if (!getAuthToken())", Path("static/listen.js").read_text(encoding="utf-8"))
        self.assertIn("if (!FT710Settings.getAuthToken()) return;",
                      Path("static/modules/atr1000.js").read_text(encoding="utf-8"))

    def test_cache_bust_covers_the_assets_that_changed(self):
        index = Path("static/index.html").read_text(encoding="utf-8")
        sw = Path("static/sw.js").read_text(encoding="utf-8")
        listen = Path("static/listen.html").read_text(encoding="utf-8")
        self.assertIn("/ft710_main.js?v=36", index)
        self.assertIn("/modules/atr1000.js?v=3", index)
        self.assertIn("const CACHE = 'mrrc-v40'", sw)   # SW cache moves whenever an asset does
        self.assertIn("'/ft710_main.js?v=36'", sw)
        self.assertIn("'/modules/atr1000.js?v=3'", sw)
        self.assertIn("listen.js?v=14", listen)


if __name__ == "__main__":
    unittest.main()
