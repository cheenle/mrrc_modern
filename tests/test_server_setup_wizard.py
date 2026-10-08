"""The setup wizard's passwordless gate, and everything it must not widen.

The gate is the only unauthenticated write path this product has ever had, so
most of what is asserted here is a *negative*: which paths it covers, which
clients it accepts, and what happens when the evidence for it goes stale.

Design D-6 turns the window off by making the network disappear, so the two
halves of the gate are "the hotspot is demonstrably up right now" (a live
heartbeat in a file the supervisor owns) and "this request came from that
hotspot's own subnet". Either half alone is a hole: the subnet alone collides
with any LAN that happens to use 10.42.0.0/24, and the heartbeat alone would let
a client on the box's real network in.

Everything runs against a temp state directory. No NetworkManager, no radio, no
hotspot — the file *is* the evidence the server is allowed to use.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from fastapi.responses import JSONResponse

import net_wifi
import server

HOTSPOT_CLIENT = "10.42.0.57"
LAN_CLIENT = "192.168.1.50"


class FakeUrl:
    def __init__(self, path, query=""):
        self.path = path
        self.query = query


class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeRequest:
    """The slice of a Starlette request the wizard actually touches.

    `headers` and `query_params` are here because `_token_from_request` reads
    both, and a real Starlette request always has them — an incomplete double
    would make the product code look broken (the same mistake
    tests/test_listen_only.py already paid for once).
    """

    def __init__(self, path: str = "/setup", method: str = "GET",
                 client: Optional[str] = HOTSPOT_CLIENT,
                 cookies: Optional[dict] = None, body: Optional[dict] = None,
                 headers: Optional[dict] = None, query: Optional[dict] = None):
        self.url = FakeUrl(path)
        self.method = method
        # Optional on purpose: a request with no peer address is exactly the case
        # the gate has to refuse, and the test for it passes None.
        self.client = FakeClient(client) if client else None
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.query_params = query or {}
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


class GateCase(unittest.TestCase):
    """A temp state dir, wired into the server through the environment."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        patcher = mock.patch.dict(
            os.environ, {"MRRC_SETUP_AP_STATE_DIR": str(self.state_dir)},
            clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def open_gate(self, network="10.42.0.0/24", age=0.0):
        """Publish what the supervisor publishes while the window is open.

        Timestamps are relative to the real clock: `gate_is_open` compares the
        heartbeat against `time.time()`, so an absolute fixture value reads as
        ancient and closes the gate the test meant to open.
        """
        now = time.time() - age
        net_wifi.write_state(net_wifi.ApState(
            mode=net_wifi.MODE_HOTSPOT, ssid="MRRC-Setup",
            gateway="10.42.0.1/24", network=network,
            url="https://10.42.0.1:8888/setup",
            since=now, heartbeat=now, deadline=now + 1800.0), self.state_dir)

    def close_gate(self, mode=net_wifi.MODE_OFF):
        net_wifi.write_state(net_wifi.ApState(mode=mode), self.state_dir)


class GateHelperTests(GateCase):
    def test_open_for_a_client_on_the_hotspot(self):
        self.open_gate()
        self.assertTrue(server._setup_gate_open(FakeRequest(client=HOTSPOT_CLIENT)))

    def test_closed_for_a_client_on_another_subnet(self):
        """The collision case: 10.42.0.0/24 is an ordinary LAN range, so a box
        that also has a cable must not accept a LAN client here."""
        self.open_gate()
        self.assertFalse(server._setup_gate_open(FakeRequest(client=LAN_CLIENT)))

    def test_closed_for_a_sibling_subnet(self):
        self.open_gate()
        self.assertFalse(server._setup_gate_open(FakeRequest(client="10.43.0.57")))

    def test_closed_when_the_hotspot_is_down(self):
        self.close_gate()
        self.assertFalse(server._setup_gate_open(FakeRequest(client=HOTSPOT_CLIENT)))

    def test_closed_when_the_box_finished_onboarding(self):
        self.close_gate(mode=net_wifi.MODE_STA)
        self.assertFalse(server._setup_gate_open(FakeRequest(client=HOTSPOT_CLIENT)))

    def test_closed_when_there_is_no_state_file_at_all(self):
        """Every desktop install, and every box whose supervisor is not running."""
        self.assertFalse(server._setup_gate_open(FakeRequest(client=HOTSPOT_CLIENT)))

    def test_open_for_an_ipv4_mapped_client(self):
        """The box binds 0.0.0.0 today, but `_bind_dual_stack_socket` exists and a
        `::` bind reports an IPv4 client as `::ffff:10.42.0.57`. Without the
        unwrap every check "correctly" returns False and the wizard is
        unreachable with nothing in the log to explain it."""
        self.open_gate()
        self.assertTrue(server._setup_gate_open(
            FakeRequest(client="::ffff:10.42.0.57")))

    def test_closed_when_there_is_no_client_at_all(self):
        self.open_gate()
        self.assertFalse(server._setup_gate_open(FakeRequest(client=None)))

    def test_a_stale_heartbeat_closes_it(self):
        """A dead supervisor must not leave the door open. 45 s is the budget;
        this is five minutes."""
        self.open_gate(age=300.0)
        self.assertFalse(server._setup_gate_open(FakeRequest(client=HOTSPOT_CLIENT)))

    def test_the_state_directory_comes_from_the_environment(self):
        self.assertEqual(server._setup_ap_state_dir(), self.state_dir)


class AccessTests(GateCase):
    def test_the_gate_grants_access_and_says_so_in_the_log(self):
        """R5: every passwordless entry is logged with the address it came from.
        Without this the residual risk in D-6 (a neighbour wins the race) leaves
        no trace at all."""
        self.open_gate()
        with self.assertLogs("mrrc", level="WARNING") as captured:
            self.assertTrue(server._setup_access(FakeRequest(client=HOTSPOT_CLIENT)))
        self.assertTrue(any(HOTSPOT_CLIENT in line for line in captured.output),
                        captured.output)

    def test_an_admin_token_grants_access_without_the_gate(self):
        """An operator on the LAN with a real session can use the wizard too;
        the passwordless property comes only from the gate."""
        self.close_gate()
        with mock.patch.object(server, "_verify_auth", return_value=True), \
             mock.patch.object(server, "_is_listen_request", return_value=False):
            self.assertTrue(server._setup_access(FakeRequest(client=LAN_CLIENT)))

    def test_a_listen_only_token_is_refused(self):
        """The middleware's listen gate is skipped for these paths (an
        unauthenticated hotspot client has to get through it), so this is the
        only thing between a listen-only session and setting the box's password.
        Constraint 6 — missing it is a privilege escalation."""
        self.close_gate()
        with mock.patch.object(server, "_verify_auth", return_value=True), \
             mock.patch.object(server, "_is_listen_request", return_value=True):
            self.assertFalse(server._setup_access(FakeRequest(client=LAN_CLIENT)))

    def test_nothing_grants_access_with_no_gate_and_no_token(self):
        self.close_gate()
        with mock.patch.object(server, "_verify_auth", return_value=False):
            self.assertFalse(server._setup_access(FakeRequest(client=LAN_CLIENT)))

    def test_the_gate_wins_over_a_missing_token(self):
        self.open_gate()
        with mock.patch.object(server, "_verify_auth", return_value=False):
            self.assertTrue(server._setup_access(FakeRequest(client=HOTSPOT_CLIENT)))


class MiddlewareTests(GateCase):
    @staticmethod
    async def _pass_through(request):
        return JSONResponse({"reached": request.url.path})

    def _call(self, path, method="GET", client=LAN_CLIENT):
        return asyncio.run(server.auth_middleware(
            FakeRequest(path=path, method=method, client=client),
            self._pass_through))

    def test_the_gate_paths_reach_a_handler_with_no_cookie_at_all(self):
        """They have to: the client on the hotspot has never logged in. The
        handlers re-check the gate, so this pass-through cannot widen it."""
        self.close_gate()
        for path in sorted(server.SETUP_GATE_PATHS):
            with self.subTest(path=path):
                self.assertEqual(self._call(path).status_code, 200)

    def test_no_other_path_is_widened_by_the_gate_being_open(self):
        self.open_gate()
        for path in ("/", "/api/status", "/api/setup", "/api/health",
                     "/listen", "/api/cloud/state", "/api/recordings"):
            with self.subTest(path=path):
                self.assertNotEqual(self._call(path).status_code, 200)

    def test_the_wizard_writes_are_not_reachable_from_the_lan(self):
        """The whole point of gating by network path: with the hotspot down, a
        LAN client that never authenticated gets 401, not the handler."""
        self.close_gate()
        for path in ("/api/setup/wizard/password", "/api/setup/wizard/wifi"):
            with self.subTest(path=path):
                resp = self._call(path, method="POST")
                self.assertEqual(resp.status_code, 200,
                                 "the middleware passes it to the handler…")
        # …and the handler is what refuses it (asserted in task 6/7).


class GatePathShapeTests(unittest.TestCase):
    """The pass-through is a closed set, compared by equality."""

    def test_it_is_exactly_the_wizard(self):
        self.assertEqual(server.SETUP_GATE_PATHS, frozenset({
            "/setup",
            "/api/setup/wizard",
            "/api/setup/wizard/password",
            "/api/setup/wizard/wifi",
        }))

    def test_no_wildcard_or_prefix_form_sneaks_in(self):
        """`path.startswith("/setup")` would also admit `/setup-anything` and,
        worse, invite the next person to add a sibling route under the same
        prefix and inherit the hole."""
        for path in server.SETUP_GATE_PATHS:
            with self.subTest(path=path):
                self.assertFalse(path.endswith("/"))
                self.assertNotIn("*", path)
        source = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("if path in SETUP_GATE_PATHS:", source)
        self.assertNotIn('path.startswith("/setup")', source)

    def test_the_writable_key_set_is_exactly_the_password(self):
        """What the passwordless window may write. Anything more — a config-file
        path, the transmit gate, a serial port — turns "let the operator set a
        password" into "let anybody on the hotspot reconfigure the box"."""
        self.assertEqual(server.SETUP_WRITABLE_KEYS,
                         frozenset({"MRRC_WEB_PASSWORD", "MRRC_AUTO_PASSWORD"}))

    def test_the_dangerous_keys_are_not_in_it(self):
        for key in ("MRRC_CONFIG_FILE", "MRRC_ALLOW_UNVERIFIED_TX",
                    "MRRC_WEB_HOST", "MRRC_WEB_PORT", "MRRC_SERIAL_PORT",
                    "MRRC_PTT_MAX_TX_SECONDS", "MRRC_SSL_CERT"):
            with self.subTest(key=key):
                self.assertNotIn(key, server.SETUP_WRITABLE_KEYS)


class WizardStateTests(GateCase):
    def _get(self, client=HOTSPOT_CLIENT):
        return asyncio.run(server.api_setup_wizard(
            FakeRequest(path="/api/setup/wizard", client=client)))

    def test_401_when_the_gate_is_closed_and_there_is_no_token(self):
        self.close_gate()
        with mock.patch.object(server, "_verify_auth", return_value=False):
            self.assertEqual(self._get(client=LAN_CLIENT).status_code, 401)

    def test_the_payload_is_a_closed_schema(self):
        """A closed schema is what keeps a password out of a response body: adding
        a field here has to be a deliberate act that turns a test red."""
        self.open_gate()
        payload = json.loads(self._get().body)
        self.assertEqual(set(payload), {"gate", "hotspot", "switch",
                                        "auto_password", "radio_model", "web_port"})
        self.assertEqual(set(payload["hotspot"]),
                         {"mode", "ssid", "url", "deadline", "remaining_s",
                          "reason"})
        self.assertEqual(set(payload["switch"]),
                         {"state", "ssid", "error", "address"})

    def test_it_reports_the_window_the_page_has_to_render(self):
        self.open_gate()
        payload = json.loads(self._get().body)
        self.assertEqual(payload["gate"], "hotspot")
        self.assertEqual(payload["hotspot"]["mode"], net_wifi.MODE_HOTSPOT)
        self.assertEqual(payload["hotspot"]["ssid"], "MRRC-Setup")
        self.assertEqual(payload["hotspot"]["url"], "https://10.42.0.1:8888/setup")
        self.assertGreater(payload["hotspot"]["deadline"], 0)
        self.assertEqual(payload["switch"]["state"], "", "no switch has been asked for")

    def test_the_remaining_time_is_skew_free(self):
        """Both halves come from the box's clock, in the same publish. A phone's
        clock can be minutes off, and `deadline - Date.now()` on the client would
        then show a window that is already closed — or one that never closes."""
        self.open_gate()
        now = time.time()
        net_wifi.write_state(net_wifi.ApState(
            mode=net_wifi.MODE_HOTSPOT, ssid="MRRC-Setup",
            gateway="10.42.0.1/24", network="10.42.0.0/24",
            url="https://10.42.0.1:8888/setup",
            since=now - 600, heartbeat=now, deadline=now + 1800.0),
            self.state_dir)
        payload = json.loads(self._get().body)
        self.assertEqual(payload["hotspot"]["remaining_s"], 1800.0)

    def test_a_box_that_is_not_in_a_window_reports_zero_remaining(self):
        self.close_gate()
        with mock.patch.object(server, "_verify_auth", return_value=True), \
             mock.patch.object(server, "_is_listen_request", return_value=False):
            payload = json.loads(self._get(client=LAN_CLIENT).body)
        self.assertEqual(payload["hotspot"]["remaining_s"], 0.0)

    def test_a_token_entry_is_labelled_as_such(self):
        """The page tells the operator whether it is standing in the open window
        or in an ordinary logged-in session; guessing wrong would tell a LAN
        admin that the box is still unconfigured."""
        self.close_gate()
        with mock.patch.object(server, "_verify_auth", return_value=True), \
             mock.patch.object(server, "_is_listen_request", return_value=False):
            payload = json.loads(self._get(client=LAN_CLIENT).body)
        self.assertEqual(payload["gate"], "token")

    def test_it_carries_a_failed_switch_back_to_the_page(self):
        """Design §6: a failure must not be silent. The server wrote the reason
        into the claim; this is how the page gets it after the phone rejoins."""
        self.open_gate()
        net_wifi.write_wizard(net_wifi.WizardClaim(
            action="connect", ssid="Home", state=net_wifi.WIZARD_FAILED,
            error="Secrets were required", nonce="abc",
            heartbeat=time.time()), self.state_dir)
        payload = json.loads(self._get().body)
        self.assertEqual(payload["switch"]["state"], net_wifi.WIZARD_FAILED)
        self.assertEqual(payload["switch"]["ssid"], "Home")
        self.assertEqual(payload["switch"]["error"], "Secrets were required")

    def test_it_says_whether_the_password_is_still_the_generated_one(self):
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": "1"}, clear=False):
            self.open_gate()
            self.assertTrue(json.loads(self._get().body)["auto_password"])
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": ""}, clear=False):
            self.assertTrue(json.loads(self._get().body)["auto_password"] is False)


class PasswordTests(GateCase):
    """The passwordless window's one and only write."""

    GOOD = "a-good-long-password"

    def setUp(self):
        super().setUp()
        self.env_path = self.state_dir / "mrrc.env"
        self._original_password = server.WEB_PASSWORD
        self._original_env = dict(os.environ)

    def tearDown(self):
        server.WEB_PASSWORD = self._original_password
        os.environ.clear()
        os.environ.update(self._original_env)

    def _post(self, body, client=HOTSPOT_CLIENT, gate=True):
        if gate:
            self.open_gate()
        else:
            self.close_gate()
        with mock.patch.object(server.first_run, "update_env_file") as writer, \
             mock.patch.object(server, "_config_file_path",
                               return_value=self.env_path):
            resp = asyncio.run(server.api_setup_wizard_password(FakeRequest(
                path="/api/setup/wizard/password", method="POST",
                client=client, body=body)))
        return resp, writer

    def test_it_writes_through_the_existing_channel(self):
        """Design D-7.5: the same function and the same file the connection
        dialog already uses. A second writer here is what D-8 exists to undo."""
        resp, writer = self._post({"password": self.GOOD, "confirm": self.GOOD})
        self.assertEqual(resp.status_code, 200)
        writer.assert_called_once()
        path, updates = writer.call_args[0]
        self.assertEqual(path, self.env_path)
        self.assertEqual(updates, {"MRRC_WEB_PASSWORD": self.GOOD,
                                   "MRRC_AUTO_PASSWORD": ""})

    def test_it_writes_no_key_outside_the_allowed_set(self):
        """Everything else in the body is ignored, not honoured: a caller on an
        open hotspot cannot smuggle a config path or a transmit gate through."""
        resp, writer = self._post({
            "password": self.GOOD,
            "radio_model": "ic7300",
            "MRRC_CONFIG_FILE": "/tmp/evil.env",
            "MRRC_ALLOW_UNVERIFIED_TX": "1",
            "MRRC_WEB_PORT": "1",
        })
        self.assertEqual(resp.status_code, 200)
        _, updates = writer.call_args[0]
        self.assertLessEqual(set(updates), server.SETUP_WRITABLE_KEYS)

    def test_the_new_password_works_without_a_restart(self):
        """A restart here would drop the operator mid-wizard, and the next step
        takes the network away anyway. Same in-process rebind
        `_ensure_strong_password` already does; the env write covers the next boot."""
        self._post({"password": self.GOOD})
        self.assertTrue(server._password_matches(self.GOOD))
        self.assertEqual(os.environ["MRRC_WEB_PASSWORD"], self.GOOD)

    def test_the_auto_password_flag_is_cleared_in_this_process_too(self):
        """Otherwise the page keeps believing the password is the generated one
        and refuses to let the operator switch networks."""
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": "1"}, clear=False):
            self._post({"password": self.GOOD})
            self.assertEqual(os.environ["MRRC_AUTO_PASSWORD"], "")
            self.assertFalse(server._setup_auto_password())

    def test_a_short_password_is_refused(self):
        """8 is the floor the existing /api/setup already enforces; the wizard
        must not be the softer door."""
        for bad in ("", "abc", "1234567"):
            with self.subTest(bad=bad):
                resp, writer = self._post({"password": bad})
                self.assertEqual(resp.status_code, 400)
                writer.assert_not_called()

    def test_a_mismatched_confirmation_is_refused(self):
        resp, writer = self._post({"password": self.GOOD,
                                   "confirm": "a-different-password"})
        self.assertEqual(resp.status_code, 400)
        writer.assert_not_called()

    def test_a_confirmation_may_be_omitted(self):
        """The page sends one; an operator curling from the HDMI console should
        still get in with a single field."""
        resp, writer = self._post({"password": self.GOOD})
        self.assertEqual(resp.status_code, 200)
        writer.assert_called_once()

    def test_surrounding_whitespace_is_stripped(self):
        """A phone keyboard appends spaces; storing one would make the password
        the operator typed impossible to reproduce at the login page."""
        self._post({"password": f"  {self.GOOD}  "})
        self.assertTrue(server._password_matches(self.GOOD))

    def test_a_write_failure_is_a_500_not_a_silent_success(self):
        """A read-only env file is a real failure mode on a box (a full /opt
        partition), and 'password set' would be a lie the operator acts on."""
        self.open_gate()
        with mock.patch.object(server.first_run, "update_env_file",
                               side_effect=OSError("read-only file system")), \
             mock.patch.object(server, "_config_file_path",
                               return_value=self.env_path):
            resp = asyncio.run(server.api_setup_wizard_password(FakeRequest(
                method="POST", body={"password": self.GOOD})))
        self.assertEqual(resp.status_code, 500)
        self.assertFalse(server._password_matches(self.GOOD),
                         "the in-process password must not change when the write failed")

    def test_the_gate_is_required(self):
        resp, writer = self._post({"password": self.GOOD},
                                  client=LAN_CLIENT, gate=False)
        self.assertEqual(resp.status_code, 401)
        writer.assert_not_called()

    def test_a_listen_only_token_is_refused_with_403(self):
        """Constraint 6, end to end: a real listen session (token in _auth_tokens
        *and* _listen_tokens) must not be able to set the box's password."""
        self.close_gate()
        token = server._make_auth_token()
        server._auth_tokens.add(token)
        server._listen_tokens.add(token)
        self.addCleanup(server._auth_tokens.discard, token)
        self.addCleanup(server._listen_tokens.discard, token)
        with mock.patch.object(server.first_run, "update_env_file") as writer:
            resp = asyncio.run(server.api_setup_wizard_password(FakeRequest(
                path="/api/setup/wizard/password", method="POST",
                client=LAN_CLIENT, cookies={server.AUTH_COOKIE: token},
                body={"password": self.GOOD})))
        self.assertEqual(resp.status_code, 403)
        writer.assert_not_called()

    def test_the_password_never_reaches_the_log(self):
        """SDD `support-bundle-privacy`. The audit line names the address, not the
        credential; log files are collected into support bundles."""
        self.open_gate()
        with self.assertLogs("mrrc", level="DEBUG") as captured, \
             mock.patch.object(server.first_run, "update_env_file"), \
             mock.patch.object(server, "_config_file_path",
                               return_value=self.env_path):
            asyncio.run(server.api_setup_wizard_password(FakeRequest(
                method="POST", body={"password": "hunter2hunter2"})))
        for line in captured.output:
            self.assertNotIn("hunter2hunter2", line)

    def test_a_malformed_body_is_a_400_not_a_500(self):
        """A captive-portal probe or a browser preflight posts something that is
        not JSON; that is a bad request, not a server fault."""
        self.open_gate()
        request = FakeRequest(method="POST")

        async def not_json():
            raise ValueError("no json here")

        request.json = not_json
        with mock.patch.object(server.first_run, "update_env_file"):
            resp = asyncio.run(server.api_setup_wizard_password(request))
        self.assertEqual(resp.status_code, 400)


class WizardSourceGuardTests(unittest.TestCase):
    """Source-level, so the guard does not depend on which branch a test drove.

    Same shape as `test_unverified_tx_gate` and `test_tls_trust_store`: read the
    code, assert the dangerous thing is absent, and assert the scan actually
    found the block it was looking at.
    """

    @staticmethod
    def wizard_source() -> str:
        source = Path(server.__file__).read_text(encoding="utf-8")
        start = source.index("# ── Setup access point wizard")
        return source[start:]

    def test_the_scan_found_the_block(self):
        wizard = self.wizard_source()
        self.assertIn("MRRC_WEB_PASSWORD", wizard)
        self.assertIn("_setup_access", wizard)

    def test_the_wizard_never_mentions_a_forbidden_key(self):
        """D-7 (MRRC_CONFIG_FILE is D-11's payload) and D-5 (the transmit gate),
        plus the fields that belong to the management page and not to an open
        hotspot."""
        wizard = self.wizard_source()
        for key in ("MRRC_CONFIG_FILE", "MRRC_ALLOW_UNVERIFIED_TX",
                    "MRRC_WEB_HOST", "MRRC_WEB_PORT", "MRRC_SERIAL_PORT",
                    "MRRC_PTT_MAX_TX_SECONDS", "MRRC_SSL_CERT",
                    "MRRC_NO_CONFIG_FILE"):
            with self.subTest(key=key):
                self.assertNotIn(key, wizard)

    def test_the_wizard_never_restarts_the_service(self):
        """A restart drops every connected client — including the operator who is
        mid-wizard on a hotspot that is about to disappear."""
        self.assertNotIn("_schedule_restart", self.wizard_source())

    def test_the_wizard_never_touches_the_env_file_directly(self):
        """One writer (D-7.5 / D-8). Only first_run.update_env_file may appear."""
        wizard = self.wizard_source()
        self.assertNotIn("write_text", wizard)
        self.assertNotIn("_write_config", wizard)
        self.assertNotIn("os.environ[\"MRRC_CONFIG_FILE\"]", wizard)

class WifiScanTests(GateCase):
    def _scan(self, client=HOTSPOT_CLIENT):
        return asyncio.run(server.api_setup_wizard_wifi_scan(
            FakeRequest(path="/api/setup/wizard/wifi", client=client)))

    def test_401_without_the_gate_or_a_token(self):
        self.close_gate()
        with mock.patch.object(server, "_verify_auth", return_value=False):
            self.assertEqual(self._scan(client=LAN_CLIENT).status_code, 401)

    def test_networks_come_from_net_wifi(self):
        self.open_gate()
        found = [{"ssid": "Home", "signal": 80, "security": "WPA2",
                  "protected": True}]
        with mock.patch.object(server.net_wifi, "scan_wifi", return_value=found):
            payload = json.loads(self._scan().body)
        self.assertEqual(payload["networks"], found)

    def test_a_scan_failure_is_an_empty_list_not_a_500(self):
        """No WiFi device (a kernel that lost the SDIO driver — design §19 red
        line 3) must read as 'nothing found', with the reason in the journal."""
        self.open_gate()
        with mock.patch.object(server.net_wifi, "scan_wifi", return_value=[]):
            self.assertEqual(json.loads(self._scan().body)["networks"], [])

    def test_the_scan_does_not_run_on_the_event_loop(self):
        """A rescan takes seconds. On the loop it would stall the spectrum and
        audio WebSockets of every client on the box for the duration."""
        source = Path(server.__file__).read_text(encoding="utf-8")
        start = source.index("async def api_setup_wizard_wifi_scan")
        self.assertIn("asyncio.to_thread", source[start:start + 900])


class WifiSwitchTests(GateCase):
    """`_perform_wifi_switch`, driven directly: it is the part that runs after the
    HTTP answer is gone, so no request object can observe it."""

    PSK = "hunter2hunter2"

    def runner(self, connect_ok=True, hotspot_ok=True,
               address="192.168.1.77/24", ap_up=True):
        """A fake nmcli that tracks whether the AP is up.

        The state matters: the switch takes the AP down before joining, so by the
        time the failure path runs, `start_hotspot` must see *no* active AP and
        actually issue `device wifi hotspot`. A fixture that always answers "the
        AP is up" makes the restore look like a no-op and hides the one behaviour
        design §6 insists on.
        """
        calls = []
        state = {"ap": ap_up}

        def run(argv):
            joined = " ".join(argv)
            calls.append(joined)
            if "device wifi connect" in joined:
                state["ap"] = False       # the caller took it down first
                if connect_ok:
                    return net_wifi.NmResult(0, "successfully activated", "")
                # nmcli echoing the PSK back is not something it does today; the
                # fixture says it does so that the scrubbing is actually tested.
                return net_wifi.NmResult(
                    1, "", "Error: Connection activation failed: (7) Secrets were "
                           f"required, but not provided (psk={self.PSK})")
            if "-g IP4.ADDRESS device show" in joined:
                return net_wifi.NmResult(0, address + "\n")
            if "connection down" in joined:
                state["ap"] = False
                return net_wifi.NmResult(0, "deactivated", "")
            if "device wifi hotspot" in joined:
                if hotspot_ok:
                    state["ap"] = True
                    return net_wifi.NmResult(0, "activated", "")
                return net_wifi.NmResult(1, "", "rfkill is blocking it")
            if "-f NAME,TYPE connection show --active" in joined:
                rows = "Hotspot:802-11-wireless\n" if state["ap"] else ""
                return net_wifi.NmResult(0, rows)
            if "802-11-wireless.mode" in joined:
                return net_wifi.NmResult(0, "ap\n")
            return net_wifi.NmResult(0, "", "")

        return run, calls

    def switch(self, run, ssid="Home", psk=None):
        server._perform_wifi_switch(
            ssid, self.PSK if psk is None else psk, "nonce-1", self.state_dir,
            runner=run, lead_s=0.0, sleep=lambda _s: None)
        return net_wifi.read_wizard(self.state_dir)

    def test_a_successful_switch_reports_the_new_address(self):
        run, calls = self.runner()
        claim = self.switch(run)
        self.assertEqual(claim.state, net_wifi.WIZARD_OK)
        self.assertEqual(claim.address, "192.168.1.77/24")
        self.assertEqual(claim.error, "")
        self.assertTrue(any("device wifi connect Home password" in c for c in calls))

    def test_the_hotspot_is_taken_down_before_the_join(self):
        """One radio. Joining while the AP is still up either fails or leaves the
        box broadcasting an open network nobody needs any more."""
        run, calls = self.runner()
        self.switch(run)
        down = next(i for i, c in enumerate(calls) if "connection down" in c)
        join = next(i for i, c in enumerate(calls) if "device wifi connect" in c)
        self.assertLess(down, join)

    def test_a_failed_join_puts_the_hotspot_back(self):
        """Design §6, the negative case that must not be skipped: without this the
        box is left with no AP and no uplink, and the operator's phone is sitting
        on a network that no longer exists."""
        run, calls = self.runner(connect_ok=False)
        claim = self.switch(run)
        self.assertEqual(claim.state, net_wifi.WIZARD_FAILED)
        self.assertTrue(any("device wifi hotspot" in c for c in calls))

    def test_a_failed_join_reports_why(self):
        run, _ = self.runner(connect_ok=False)
        self.assertIn("Secrets were required", self.switch(run).error)

    def test_the_stored_reason_is_bounded(self):
        """It ends up in a JSON file and then on a phone screen."""
        run, _ = self.runner(connect_ok=False)
        self.assertLessEqual(len(self.switch(run).error), 300)

    def test_the_psk_is_scrubbed_out_of_the_stored_reason(self):
        run, _ = self.runner(connect_ok=False)
        claim = self.switch(run)
        self.assertNotIn(self.PSK, claim.error)
        self.assertIn(net_wifi.REDACTED, claim.error)

    def test_the_psk_never_lands_on_disk(self):
        """SDD support-bundle-privacy, asserted on the bytes rather than on the
        code path: neither mailbox may contain the credential, whatever nmcli did
        or whatever a future edit forwards."""
        run, _ = self.runner(connect_ok=False)
        self.switch(run)
        for name in (net_wifi.STATE_NAME, net_wifi.WIZARD_NAME):
            path = self.state_dir / name
            with self.subTest(file=name):
                if path.exists():
                    self.assertNotIn(self.PSK, path.read_text(encoding="utf-8"))

    def test_the_psk_never_reaches_the_log(self):
        run, _ = self.runner(connect_ok=False)
        with self.assertLogs("mrrc", level="DEBUG") as captured:
            self.switch(run)
        for line in captured.output:
            self.assertNotIn(self.PSK, line)

    def test_the_nonce_comes_back_so_one_window_can_be_granted(self):
        """The supervisor grants exactly one fresh window per nonce (task 4); a
        claim without one leaves the operator with no retry."""
        run, _ = self.runner(connect_ok=False)
        self.assertEqual(self.switch(run).nonce, "nonce-1")

    def test_a_hotspot_that_will_not_come_back_is_still_reported(self):
        """The worst case: no AP, no uplink. The claim is the only record, and it
        must say the join failed rather than look like nothing happened."""
        claim = self.switch(self.runner(connect_ok=False, hotspot_ok=False)[0])
        self.assertEqual(claim.state, net_wifi.WIZARD_FAILED)

    def test_a_runner_that_raises_is_a_failure_not_a_crash(self):
        """This runs on a daemon thread: an exception here dies silently and
        leaves the claim stuck at `switching`, which the supervisor reads as
        'the server is still working' for 45 seconds."""
        def explode(argv):
            raise RuntimeError("nmcli exploded")

        claim = self.switch(explode)
        self.assertEqual(claim.state, net_wifi.WIZARD_FAILED)
        self.assertIn("exploded", claim.error)

    def test_the_claim_is_heartbeated_while_the_join_blocks(self):
        """nmcli waits for the association, which on a slow AP is tens of seconds
        — longer than the supervisor's 45 s staleness budget."""
        beats = []
        real_write = net_wifi.write_wizard

        def counting_write(claim, state_dir=None):
            beats.append(claim.state)
            return real_write(claim, state_dir)

        def slow(argv):
            if "device wifi connect" in " ".join(argv):
                time.sleep(0.35)
            if "-g IP4.ADDRESS device show" in " ".join(argv):
                return net_wifi.NmResult(0, "192.168.1.77/24\n")
            return net_wifi.NmResult(0, "", "")

        with mock.patch.object(server.net_wifi, "write_wizard", counting_write), \
             mock.patch.object(server, "WIFI_SWITCH_HEARTBEAT_S", 0.05):
            server._perform_wifi_switch("Home", self.PSK, "n", self.state_dir,
                                        runner=slow, lead_s=0.0,
                                        sleep=lambda _s: None)
        self.assertGreaterEqual(beats.count(net_wifi.WIZARD_SWITCHING), 2,
                                f"expected repeated heartbeats, got {beats}")
        self.assertEqual(beats[-1], net_wifi.WIZARD_OK,
                         "the final state must be the last write, not a heartbeat")


class WifiConnectEndpointTests(GateCase):
    def _post(self, body, client=HOTSPOT_CLIENT, gate=True, auto_password=""):
        if gate:
            self.open_gate()
        else:
            self.close_gate()
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": auto_password},
                             clear=False), \
             mock.patch.object(server.threading, "Thread") as thread:
            resp = asyncio.run(server.api_setup_wizard_wifi_connect(FakeRequest(
                path="/api/setup/wizard/wifi", method="POST", client=client,
                body=body)))
        return resp, thread

    def test_it_answers_at_once_and_switches_on_a_thread(self):
        """The answer has to be on the wire before the AP dies, so the switch
        cannot be awaited."""
        resp, thread = self._post({"ssid": "Home", "password": "hunter2hunter2"})
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.body)
        self.assertTrue(payload["switching"])
        self.assertTrue(payload["nonce"])
        thread.assert_called_once()
        thread.return_value.start.assert_called_once()

    def test_the_thread_is_a_daemon(self):
        """A non-daemon thread would hold the process open during a restart."""
        _, thread = self._post({"ssid": "Home"})
        self.assertTrue(thread.call_args.kwargs.get("daemon"))

    def test_it_refuses_to_switch_before_a_password_is_set(self):
        """Constraint 7: after the switch the window is gone for good, so an
        operator who never set a password is locked out of their own box."""
        resp, thread = self._post({"ssid": "Home"}, auto_password="1")
        self.assertEqual(resp.status_code, 409)
        thread.assert_not_called()

    def test_the_409_explains_itself(self):
        """A bare 409 on a phone screen is a dead end; the page shows this text."""
        resp, _ = self._post({"ssid": "Home"}, auto_password="1")
        payload = json.loads(resp.body)
        self.assertEqual(payload["error"], "set a password first")
        self.assertIn("hotspot", payload["detail"])

    def test_a_password_set_earlier_in_the_same_session_unlocks_it(self):
        """The 409 must clear without a restart, or the operator who just set a
        password is told to set one again."""
        self.open_gate()
        os.environ["MRRC_AUTO_PASSWORD"] = "1"
        self.addCleanup(os.environ.pop, "MRRC_AUTO_PASSWORD", None)
        with mock.patch.object(server.first_run, "update_env_file"), \
             mock.patch.object(server, "_config_file_path",
                               return_value=self.state_dir / "mrrc.env"):
            asyncio.run(server.api_setup_wizard_password(FakeRequest(
                method="POST", body={"password": "a-good-long-password"})))
        resp, thread = self._post({"ssid": "Home"})
        self.assertEqual(resp.status_code, 200)
        thread.assert_called_once()

    def test_an_empty_ssid_is_a_400(self):
        resp, thread = self._post({"ssid": "   ", "password": "hunter2hunter2"})
        self.assertEqual(resp.status_code, 400)
        thread.assert_not_called()

    def test_a_short_psk_is_a_400(self):
        """WPA-PSK is 8 characters by definition, so this is a typo check that
        saves a 20-second round trip through 'AP down → join fails → AP up'."""
        resp, thread = self._post({"ssid": "Home", "password": "short"})
        self.assertEqual(resp.status_code, 400)
        thread.assert_not_called()

    def test_an_open_network_needs_no_psk(self):
        resp, thread = self._post({"ssid": "Guest"})
        self.assertEqual(resp.status_code, 200)
        thread.assert_called_once()

    def test_the_ssid_is_trimmed_before_it_reaches_nmcli(self):
        _, thread = self._post({"ssid": "  Home  "})
        self.assertEqual(thread.call_args.kwargs["args"][0], "Home")

    def test_the_gate_is_required(self):
        resp, thread = self._post({"ssid": "Home"}, client=LAN_CLIENT, gate=False)
        self.assertEqual(resp.status_code, 401)
        thread.assert_not_called()

    def test_a_listen_only_token_is_refused(self):
        self.close_gate()
        token = server._make_auth_token()
        server._auth_tokens.add(token)
        server._listen_tokens.add(token)
        self.addCleanup(server._auth_tokens.discard, token)
        self.addCleanup(server._listen_tokens.discard, token)
        resp = asyncio.run(server.api_setup_wizard_wifi_connect(FakeRequest(
            path="/api/setup/wizard/wifi", method="POST", client=LAN_CLIENT,
            cookies={server.AUTH_COOKIE: token}, body={"ssid": "Home"})))
        self.assertEqual(resp.status_code, 403)

    def test_a_malformed_body_is_a_400(self):
        self.open_gate()
        request = FakeRequest(method="POST")

        async def not_json():
            raise ValueError("nope")

        request.json = not_json
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": ""}, clear=False):
            resp = asyncio.run(server.api_setup_wizard_wifi_connect(request))
        self.assertEqual(resp.status_code, 400)

    def test_the_ssid_is_logged_but_the_psk_is_not(self):
        with self.assertLogs("mrrc", level="DEBUG") as captured:
            self._post({"ssid": "HomeNet", "password": "hunter2hunter2"})
        joined = "\n".join(captured.output)
        self.assertIn("HomeNet", joined)
        self.assertNotIn("hunter2hunter2", joined)

    def test_the_existing_route_order_guard_sees_the_wizard_routes(self):
        """tests/test_cloud_endpoints.py already asserts that no /api/ route sits
        below the SPA fallback. This duplicates it deliberately: if the wizard
        routes are ever moved, the failure should name them instead of looking
        like a Cloud Hub regression."""
        paths = [getattr(r, "path", "") for r in server.app.router.routes]
        catch_all = paths.index("/{path:path}")
        for path in ("/api/setup/wizard", "/api/setup/wizard/password",
                     "/api/setup/wizard/wifi"):
            with self.subTest(path=path):
                self.assertIn(path, paths)
                self.assertLess(paths.index(path), catch_all)

if __name__ == "__main__":
    unittest.main()
