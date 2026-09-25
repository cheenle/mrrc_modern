"""Listen-only role (MRRC_LISTEN_PASSWORD) regression tests.

A listen session may tune frequency/mode (and recall memory channels, which
only write freq+mode) and receive audio/spectrum, but every transmit and
device-setting path must be refused server-side — the hidden UI is never
the enforcement. Mirrors the unverified-TX-gate contract: a refused command
must never reach the CAT layer.
"""
import inspect
import json
import unittest
from types import SimpleNamespace
from unittest import mock


# ── Fakes ───────────────────────────────────────────────────────────

class _FakeWS:
    """WebSocket double: send_text sink + close recorder."""

    def __init__(self, token=""):
        self.messages = []
        self.query_params = {"token": token} if token else {}
        self.closed = None
        self.accepted = False

    async def send_text(self, text):
        self.messages.append(json.loads(text))

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    def errors(self):
        return [m for m in self.messages if m.get("type") == "error"]


class _FakeCat:
    """Records the CAT calls the set-command branches make."""

    def __init__(self):
        self.connected = True
        self.mode_calls = []
        self.frequency_calls = []
        self.priority_calls = []
        self.tune_calls = []

    async def set_mode(self, mode_num):
        self.mode_calls.append(mode_num)
        return True

    async def set_frequency(self, freq_hz, vfo="A"):
        self.frequency_calls.append((freq_hz, vfo))
        return True

    async def send_priority_set_command(self, cmd):
        self.priority_calls.append(cmd)
        return True

    async def set_tune(self, tune):
        self.tune_calls.append(tune)
        return True


class _FakeRequest:
    """Starlette Request double for handler/middleware-level tests."""

    def __init__(self, path="/", method="GET", cookies=None,
                 query=None, body=None, client_host="127.0.0.1"):
        self.url = SimpleNamespace(path=path, query="")
        self.method = method
        self.cookies = cookies or {}
        self.query_params = query or {}
        self._body = body or {}
        self.client = SimpleNamespace(host=client_host)

    async def json(self):
        return self._body


class _ListenTestBase(unittest.IsolatedAsyncioTestCase):
    """Common server-module swapping + token-set cleanup."""

    def setUp(self):
        import server
        from radio_state import RadioState
        self.server = server
        self._old_cat = server.cat
        self._old_radio = server.radio
        self._old_scheduler = server.scheduler
        server.radio = RadioState()
        server.scheduler = None
        self._old_auth = set(server._auth_tokens)
        self._old_listen = set(server._listen_tokens)
        server._auth_tokens.clear()
        server._listen_tokens.clear()
        server._login_attempts.clear()

    def tearDown(self):
        self.server.cat = self._old_cat
        self.server.radio = self._old_radio
        self.server.scheduler = self._old_scheduler
        self.server._auth_tokens.clear()
        self.server._auth_tokens.update(self._old_auth)
        self.server._listen_tokens.clear()
        self.server._listen_tokens.update(self._old_listen)
        self.server._login_attempts.clear()

    def _install_cat(self):
        cat = _FakeCat()
        self.server.cat = cat
        return cat

    def _listen_ws(self):
        """Fake control socket authenticated with a listen-only token."""
        ws = _FakeWS()
        token = "listen-token"
        self.server._auth_tokens.add(token)
        self.server._listen_tokens.add(token)
        self.server._ws_tokens[ws] = token
        self.addCleanup(self.server._ws_tokens.pop, ws, None)
        return ws


# ── Password matching ───────────────────────────────────────────────

class ListenPasswordTests(unittest.TestCase):
    def test_empty_listen_password_never_matches(self):
        import server
        with mock.patch.object(server, "LISTEN_PASSWORD", ""):
            self.assertFalse(server._listen_password_matches(""))
            self.assertFalse(server._listen_password_matches("anything"))

    def test_match_and_mismatch(self):
        import server
        with mock.patch.object(server, "LISTEN_PASSWORD", "listener-secret"):
            self.assertTrue(server._listen_password_matches("listener-secret"))
            self.assertFalse(server._listen_password_matches("listener-secrex"))
            self.assertFalse(server._listen_password_matches(""))

    def test_comparison_is_constant_time(self):
        # Same ratchet as _password_matches (SDD I9): no plain == on secrets.
        import server
        src = inspect.getsource(server._listen_password_matches)
        self.assertIn("compare_digest", src)
        self.assertNotIn("==", src.replace("compare_digest", ""))


# ── Login flow ──────────────────────────────────────────────────────

class ListenLoginTests(_ListenTestBase):
    async def _login(self, password, ip="10.0.0.1"):
        req = _FakeRequest(path="/api/auth/login", method="POST",
                           body={"password": password}, client_host=ip)
        with mock.patch.object(self.server, "WEB_PASSWORD", "admin-secret"), \
             mock.patch.object(self.server, "LISTEN_PASSWORD", "listen-secret"):
            return await self.server.api_login(req)

    async def test_admin_password_gets_admin_role(self):
        resp = await self._login("admin-secret")
        self.assertEqual(resp.status_code, 200)
        body = json.loads(bytes(resp.body))
        self.assertEqual(body["role"], "admin")
        self.assertIn(body["token"], self.server._auth_tokens)
        self.assertNotIn(body["token"], self.server._listen_tokens)

    async def test_listen_password_gets_listen_role(self):
        resp = await self._login("listen-secret")
        self.assertEqual(resp.status_code, 200)
        body = json.loads(bytes(resp.body))
        self.assertEqual(body["role"], "listen")
        self.assertIn(body["token"], self.server._auth_tokens)
        self.assertIn(body["token"], self.server._listen_tokens)

    async def test_wrong_password_rejected(self):
        resp = await self._login("nope")
        self.assertEqual(resp.status_code, 401)

    async def test_listen_login_impossible_when_unconfigured(self):
        req = _FakeRequest(path="/api/auth/login", method="POST",
                           body={"password": "listen-secret"})
        with mock.patch.object(self.server, "WEB_PASSWORD", "admin-secret"), \
             mock.patch.object(self.server, "LISTEN_PASSWORD", ""):
            resp = await self.server.api_login(req)
        self.assertEqual(resp.status_code, 401)

    async def test_identical_passwords_resolve_to_admin(self):
        req = _FakeRequest(path="/api/auth/login", method="POST",
                           body={"password": "same-secret"})
        with mock.patch.object(self.server, "WEB_PASSWORD", "same-secret"), \
             mock.patch.object(self.server, "LISTEN_PASSWORD", "same-secret"):
            resp = await self.server.api_login(req)
        body = json.loads(bytes(resp.body))
        self.assertEqual(body["role"], "admin")

    async def test_logout_clears_both_token_sets(self):
        resp = await self._login("listen-secret")
        token = json.loads(bytes(resp.body))["token"]
        req = _FakeRequest(path="/api/auth/logout", method="POST",
                           cookies={self.server.AUTH_COOKIE: token})
        await self.server.api_logout(req)
        self.assertNotIn(token, self.server._auth_tokens)
        self.assertNotIn(token, self.server._listen_tokens)


# ── /WSradio command gate ───────────────────────────────────────────

class ListenWsGateTests(_ListenTestBase):
    async def test_freq_and_mode_are_allowed(self):
        cat = self._install_cat()
        ws = self._listen_ws()
        await self.server._handle_ws_message(
            ws, json.dumps({"type": "set", "field": "freq", "value": 7050000}))
        await self.server._handle_ws_message(
            ws, json.dumps({"type": "set", "field": "mode", "value": "LSB"}))
        self.assertEqual(cat.frequency_calls, [(7050000, "A")])
        self.assertEqual(len(cat.mode_calls), 1)
        self.assertEqual(ws.errors(), [])

    async def test_explicit_vfo_freqs_are_allowed(self):
        cat = self._install_cat()
        ws = self._listen_ws()
        await self.server._handle_ws_message(
            ws, json.dumps({"type": "set", "field": "vfo_b_freq", "value": 14270000}))
        self.assertEqual(cat.frequency_calls, [(14270000, "B")])

    async def test_mem_recall_is_allowed(self):
        # Memory recall writes frequency + mode only — squarely inside the
        # listen role.
        cat = self._install_cat()
        ws = self._listen_ws()
        await self.server._handle_ws_message(
            ws, json.dumps({"type": "memRecall", "freq": 7050000, "mode": "LSB"}))
        self.assertTrue(cat.frequency_calls)
        self.assertEqual(len(cat.mode_calls), 1)
        self.assertEqual(ws.errors(), [])

    async def test_transmit_and_settings_are_refused(self):
        cat = self._install_cat()
        ws = self._listen_ws()
        forbidden = [
            {"type": "set", "field": "ptt", "value": True},
            {"type": "set", "field": "tune", "value": True},
            {"type": "set", "field": "cq", "value": True},
            {"type": "set", "field": "recording", "value": True},
            {"type": "set", "field": "rf_power", "value": 50},
            {"type": "set", "field": "filter", "value": 10},
            {"type": "set", "field": "preamp", "value": 1},
            {"type": "set", "field": "power", "value": False},
            {"type": "set", "field": "af_gain", "value": 100},
            {"type": "memSave", "channels": []},
            {"type": "memDelete", "index": 0},
        ]
        for msg in forbidden:
            with self.subTest(msg=msg):
                await self.server._handle_ws_message(ws, json.dumps(msg))
        # Every one refused with the role message, and none reached the radio.
        self.assertEqual(len(ws.errors()), len(forbidden))
        for err in ws.errors():
            self.assertEqual(err["message"], self.server.LISTEN_ONLY_MESSAGE)
        self.assertEqual(cat.frequency_calls, [])
        self.assertEqual(cat.mode_calls, [])
        self.assertEqual(cat.priority_calls, [])
        self.assertEqual(cat.tune_calls, [])

    async def test_legacy_colon_format_is_gated(self):
        cat = self._install_cat()
        ws = self._listen_ws()
        await self.server._handle_ws_message(ws, "ptt:true")
        self.assertEqual(len(ws.errors()), 1)
        self.assertEqual(cat.priority_calls, [])
        await self.server._handle_ws_message(ws, "freq:7050000")
        self.assertEqual(cat.frequency_calls, [(7050000, "A")])

    async def test_readonly_messages_pass(self):
        self._install_cat()
        ws = self._listen_ws()
        await self.server._handle_ws_message(ws, json.dumps({"type": "ping"}))
        await self.server._handle_ws_message(
            ws, json.dumps({"type": "get", "field": "fullState"}))
        types = [m.get("type") for m in ws.messages]
        self.assertIn("pong", types)
        self.assertIn("fullState", types)
        self.assertEqual(ws.errors(), [])

    async def test_admin_session_is_unaffected(self):
        cat = self._install_cat()
        ws = _FakeWS()
        token = "admin-token"
        self.server._auth_tokens.add(token)   # not in _listen_tokens
        self.server._ws_tokens[ws] = token
        self.addCleanup(self.server._ws_tokens.pop, ws, None)
        await self.server._handle_ws_message(
            ws, json.dumps({"type": "set", "field": "freq", "value": 7050000}))
        self.assertEqual(cat.frequency_calls, [(7050000, "A")])


# ── Endpoint gates ──────────────────────────────────────────────────

class ListenEndpointGateTests(_ListenTestBase):
    def _listen_token(self):
        token = "listen-token"
        self.server._auth_tokens.add(token)
        self.server._listen_tokens.add(token)
        return token

    async def test_audio_tx_uplink_refused(self):
        ws = _FakeWS(token=self._listen_token())
        await self.server.ws_audio_tx(ws)
        self.assertEqual(ws.closed[0], 4003)
        self.assertFalse(ws.accepted)
        self.assertNotIn(ws, self.server.audio_tx_clients)

    async def test_atr1000_channel_refused(self):
        ws = _FakeWS(token=self._listen_token())
        await self.server.ws_atr1000(ws)
        self.assertEqual(ws.closed[0], 4003)
        self.assertFalse(ws.accepted)

    async def test_audio_rx_and_spectrum_stay_open(self):
        # Listening is the whole point — RX audio and spectrum must accept
        # listen tokens (drive them to first receive, then disconnect).
        token = self._listen_token()

        class _HangWS(_FakeWS):
            async def receive(self):
                from fastapi import WebSocketDisconnect
                raise WebSocketDisconnect()

            async def receive_text(self):
                from fastapi import WebSocketDisconnect
                raise WebSocketDisconnect()

        rx = _HangWS(token=token)
        await self.server.ws_audio_rx(rx)
        self.assertTrue(rx.accepted)
        self.assertIsNone(rx.closed)

        spec = _HangWS(token=token)
        await self.server.ws_spectrum(spec)
        self.assertTrue(spec.accepted)
        self.assertIsNone(spec.closed)

    async def test_middleware_blocks_api_writes(self):
        token = self._listen_token()

        async def call_next(request):
            from fastapi.responses import JSONResponse
            return JSONResponse({"ok": True})

        writes = ["/api/setup", "/api/restart", "/api/mem_channels",
                  "/api/support/bundle", "/api/support/upload"]
        for path in writes:
            with self.subTest(path=path):
                req = _FakeRequest(path=path, method="POST",
                                   cookies={self.server.AUTH_COOKIE: token})
                resp = await self.server.auth_middleware(req, call_next)
                self.assertEqual(resp.status_code, 403)

    async def test_middleware_allows_reads_and_logout(self):
        token = self._listen_token()

        async def call_next(request):
            from fastapi.responses import JSONResponse
            return JSONResponse({"ok": True})

        for path, method in [("/api/status", "GET"),
                             ("/api/recordings", "GET"),
                             ("/api/auth/logout", "POST")]:
            with self.subTest(path=path):
                req = _FakeRequest(path=path, method=method,
                                   cookies={self.server.AUTH_COOKIE: token})
                resp = await self.server.auth_middleware(req, call_next)
                self.assertEqual(resp.status_code, 200)

    async def test_listen_page_is_served(self):
        resp = await self.server.listen_page(
            _FakeRequest(path="/listen", method="GET"))
        self.assertEqual(resp.status_code, 200)

    def test_spectrum_throttle_gate(self):
        # Listen-role spectrum clients get every Nth frame; admins full rate.
        admin, listener = _FakeWS(), _FakeWS()
        self.server._listen_spectrum_clients.add(listener)
        self.addCleanup(self.server._listen_spectrum_clients.discard, listener)
        n = self.server.LISTEN_SPECTRUM_DIVIDER
        due = [t for t in range(1, n * 3 + 1)
               if self.server._spectrum_frame_due(listener, t)]
        self.assertEqual(due, [n, n * 2, n * 3])
        self.assertTrue(all(
            self.server._spectrum_frame_due(admin, t)
            for t in range(1, n * 3 + 1)))

    async def test_client_count_broadcast(self):
        old = set(self.server.ctrl_clients)
        self.server.ctrl_clients.clear()
        self.addCleanup(self.server.ctrl_clients.update, old)
        ws1, ws2 = _FakeWS(), _FakeWS()
        self.server.ctrl_clients.update({ws1, ws2})
        await self.server._broadcast_client_count()
        for ws in (ws1, ws2):
            self.assertIn({"type": "onlineUsers", "count": 2}, ws.messages)

    async def test_client_count_broadcast_prunes_dead_clients(self):
        old = set(self.server.ctrl_clients)
        self.server.ctrl_clients.clear()
        self.addCleanup(self.server.ctrl_clients.update, old)

        class _DeadWS(_FakeWS):
            async def send_text(self, text):
                raise RuntimeError("gone")

        live, dead = _FakeWS(), _DeadWS()
        self.server.ctrl_clients.update({live, dead})
        await self.server._broadcast_client_count()
        self.assertIn({"type": "onlineUsers", "count": 2}, live.messages)
        self.assertNotIn(dead, self.server.ctrl_clients)


# ── Proxy-path contract (www.vlsc.net/mrrc_modern/listen) ──────────

class ListenProxyContractTests(unittest.TestCase):
    """The listen page must work both at /listen (direct) and under the
    /mrrc_modern/ reverse-proxy prefix — every URL it builds derives from
    the page's own location, and the deploy script carries the matching
    nginx block (markers asserted like the support-receiver script's)."""

    def test_listen_js_is_prefix_aware(self):
        from pathlib import Path
        js = Path("static/listen.js").read_text(encoding="utf-8")
        self.assertIn("URL_BASE", js)
        self.assertIn("lastIndexOf('/listen')", js)
        # Anti-patterns that would bypass the prefix (endpoint paths as
        # arguments to the URL builders are fine — the base is added there).
        for bad in ("host + path", "location.replace('/login",
                    "fetch('/api"):
            self.assertNotIn(bad, js, f"root-absolute bypass: {bad}")

    def test_listen_js_has_ios_scriptprocessor_fallback(self):
        # iOS Safari: the main UI carries the same fallback — without it an
        # AudioWorklet failure leaves the listener with no audio at all.
        from pathlib import Path
        js = Path("static/listen.js").read_text(encoding="utf-8")
        self.assertIn("createScriptProcessor", js)
        self.assertIn("setupScriptProcessorFallback", js)
        self.assertIn("AudioWorklet unavailable", js)

    def test_every_js_element_id_exists_in_html(self):
        # listen.js does unguarded getElementById — a missing id aborts the
        # whole init silently (the main UI's initUI has the same contract).
        import re
        from pathlib import Path
        js = Path("static/listen.js").read_text(encoding="utf-8")
        html = Path("static/listen.html").read_text(encoding="utf-8")
        ids = set(re.findall(r"getElementById\('([^']+)'\)", js))
        el_map = re.search(r"const el = \{\};(.*?)\]\.forEach", js, re.DOTALL)
        self.assertIsNotNone(el_map)
        ids.update(re.findall(r"'([a-z][a-z0-9-]+)'", el_map.group(1)))
        for element_id in sorted(ids):
            self.assertIn(f'id="{element_id}"', html,
                          f"#{element_id} used by listen.js but missing")

    def test_listen_html_uses_relative_assets(self):
        from pathlib import Path
        html = Path("static/listen.html").read_text(encoding="utf-8")
        self.assertIn('src="listen.js?v=', html)
        self.assertNotIn('src="/', html)

    def test_login_page_uses_relative_urls(self):
        import server
        src = inspect.getsource(server.login_page)
        self.assertIn("fetch('api/auth/login'", src)
        self.assertIn("?'listen'", src)

    def test_deploy_script_carries_the_nginx_block(self):
        from pathlib import Path
        sh = Path("deploy_listen_proxy.sh").read_text(encoding="utf-8")
        self.assertIn("location = /mrrc_modern/listen", sh)
        # ^~ is load-bearing: the www site's regex locations (~* \.js$) would
        # otherwise outrank plain prefix matches and serve local 404s.
        self.assertIn("location ^~ /mrrc_modern/WS", sh)
        self.assertIn("location ^~ /mrrc_modern/modules/", sh)
        self.assertIn("location ^~ /mrrc_modern/api/", sh)
        self.assertIn("proxy_http_version 1.1", sh)
        self.assertIn("Upgrade $http_upgrade", sh)
        self.assertIn("proxy_ssl_verify off", sh)
        self.assertIn("sites-available/vlsc.net", sh)
        self.assertIn("nginx -t", sh)
        # DNS-name backend with per-location resolver → survives IPv6 changes.
        self.assertIn("resolver 1.1.1.1 8.8.8.8 valid=300s", sh)
        self.assertIn("set $mrrc_listen_be {backend}", sh)
        # Idempotence + backend-update path, same pattern as
        # deploy_support_receiver.sh.
        self.assertIn("already present", sh)
        self.assertIn("LISTEN_BACKEND", sh)


if __name__ == "__main__":
    unittest.main()
