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
        """Publish what the supervisor publishes while the window is open."""
        net_wifi.write_state(net_wifi.ApState(
            mode=net_wifi.MODE_HOTSPOT, ssid="MRRC-Setup",
            gateway="10.42.0.1/24", network=network,
            url="https://10.42.0.1:8888/setup",
            heartbeat=time.time() - age), self.state_dir)

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


if __name__ == "__main__":
    unittest.main()
