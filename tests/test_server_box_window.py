"""The only unauthenticated write surface in the product, and its two guards.

The retired wizard's window was "the hotspot I created is up and you are on it".
The box is now reached over a LAN somebody else configured, so the window is
"nobody has ever set a password, and you are on a subnet I am also on" (design
§5.1). Two consequences this file exists to pin:

* **The predicate is the state.** There is no flag to clear, so there is no flag
  to forget to clear. Setting the password closes the window because setting the
  password is what the predicate reads.
* **An unknown network is a closed window.** If `nmcli` cannot say which subnets
  this box is on, the answer must be "not you" — the opposite reading puts an
  unterminated box on a shared LAN with no password at all.

Handlers are called directly (`asyncio.run(handler(FakeRequest(...)))`) rather
than through a TestClient: that is the pattern the retired wizard's tests used,
and it keeps the fake small enough to read.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import unittest
from unittest import mock

import box_ops
import server


class FakeUrl:
    def __init__(self, path, query=""):
        self.path = path
        self.query = query


class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeRequest:
    def __init__(self, path: str = "/api/box/onboarding", method: str = "GET",
                 client: str = "192.168.16.50", cookies: dict = None,
                 body: dict = None, headers: dict = None):
        self.url = FakeUrl(path)
        self.method = method
        self.client = FakeClient(client) if client else None
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.query_params = {}
        self._body = body if body is not None else {}

    async def json(self):
        return self._body


def run(coro):
    return asyncio.run(coro)


class UntouchedTests(unittest.TestCase):
    def test_the_auto_flag_means_untouched(self):
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": "1"}):
            self.assertTrue(server._password_is_untouched())

    def test_a_cleared_flag_with_a_real_password_means_taken(self):
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": ""}):
            with mock.patch.object(server, "WEB_PASSWORD", "a-real-password"):
                self.assertFalse(server._password_is_untouched())

    def test_the_factory_default_means_untouched(self):
        """A box whose first run never happened is still open: the password in
        the source is not a password anybody chose."""
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": ""}):
            with mock.patch.object(server, "WEB_PASSWORD",
                                   server.DEFAULT_WEB_PASSWORD):
                self.assertTrue(server._password_is_untouched())

    def test_an_empty_password_means_untouched(self):
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": ""}):
            with mock.patch.object(server, "WEB_PASSWORD", ""):
                self.assertTrue(server._password_is_untouched())


class WindowPredicateTests(unittest.TestCase):
    def open_box(self):
        return mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": "1"})

    def test_a_client_on_my_subnet_in_an_untouched_box_gets_in(self):
        with self.open_box(), \
                mock.patch.object(box_ops, "local_subnets",
                                  return_value=[__import__("ipaddress")
                                                .ip_network("192.168.16.0/24")]):
            self.assertTrue(server._first_window_open(
                FakeRequest(client="192.168.16.50")))

    def test_a_client_elsewhere_does_not(self):
        with self.open_box(), \
                mock.patch.object(box_ops, "local_subnets",
                                  return_value=[__import__("ipaddress")
                                                .ip_network("192.168.16.0/24")]):
            self.assertFalse(server._first_window_open(
                FakeRequest(client="10.42.0.57")))

    def test_an_unknown_local_network_closes_the_window(self):
        """Review Focus #1. nmcli down, or no address yet: "I cannot tell" must
        read as "not you"."""
        with self.open_box(), \
                mock.patch.object(box_ops, "local_subnets", return_value=[]):
            self.assertFalse(server._first_window_open(
                FakeRequest(client="192.168.16.50")))

    def test_no_peer_at_all_is_refused(self):
        with self.open_box(), \
                mock.patch.object(box_ops, "local_subnets",
                                  return_value=[__import__("ipaddress")
                                                .ip_network("192.168.16.0/24")]):
            self.assertFalse(server._first_window_open(FakeRequest(client=None)))

    def test_a_configured_box_is_closed_even_from_its_own_subnet(self):
        with mock.patch.dict(os.environ, {"MRRC_AUTO_PASSWORD": ""}), \
                mock.patch.object(server, "WEB_PASSWORD", "chosen-by-a-human"), \
                mock.patch.object(box_ops, "local_subnets",
                                  return_value=[__import__("ipaddress")
                                                .ip_network("192.168.16.0/24")]):
            self.assertFalse(server._first_window_open(
                FakeRequest(client="192.168.16.50")))


class OnboardingEndpointTests(unittest.TestCase):
    def call(self, request=None):
        return run(server.api_box_onboarding(request or FakeRequest()))

    def test_it_refuses_when_the_window_is_closed(self):
        with mock.patch.object(server, "_first_window_open",
                               return_value=False):
            response = self.call()
        self.assertEqual(response.status_code, 401)

    def test_it_answers_the_four_things_the_page_needs(self):
        with mock.patch.object(server, "_first_window_open", return_value=True), \
                mock.patch.object(server, "_password_is_untouched",
                                  return_value=True), \
                mock.patch.object(box_ops, "scan_networks",
                                  return_value=[{"ssid": "home", "signal": 61,
                                                 "security": "wpa2",
                                                 "protected": True}]), \
                mock.patch.object(box_ops, "identity_report",
                                  return_value={"hostname": "MRRC-Modern",
                                                "mdns": "MRRC-Modern.local",
                                                "addresses": ["192.168.16.217"],
                                                "suggested_name": "MRRC-Modern"}):
            payload = json.loads(self.call().body)
        self.assertTrue(payload["needs_password"])
        self.assertEqual(payload["hostname"], "MRRC-Modern")
        self.assertEqual(payload["mdns"], "MRRC-Modern.local")
        self.assertEqual(payload["networks"][0]["ssid"], "home")
        self.assertEqual(payload["min_password_len"], box_ops.MIN_PASSWORD_LEN)


class PasswordEndpointTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(server, "first_run")
        self.first_run = patcher.start()
        self.addCleanup(patcher.stop)
        self.path = mock.patch.object(server, "_config_file_path",
                                      return_value=__import__("pathlib").Path("/tmp/x"))
        self.path.start()
        self.addCleanup(self.path.stop)
        # _write_password mutates the module global and the environment on
        # purpose (that is the whole point — the value goes live without a
        # restart). Put them back, or the next module sees a WEB_PASSWORD and an
        # MRRC_AUTO_PASSWORD this file invented; the token sets likewise.
        saved_password = server.WEB_PASSWORD
        saved_env = {key: os.environ.get(key)
                     for key in ("MRRC_WEB_PASSWORD", "MRRC_AUTO_PASSWORD")}
        saved_auth = set(server._auth_tokens)
        saved_listen = set(server._listen_tokens)

        def restore():
            server.WEB_PASSWORD = saved_password
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            server._auth_tokens.clear()
            server._auth_tokens.update(saved_auth)
            server._listen_tokens.clear()
            server._listen_tokens.update(saved_listen)

        self.addCleanup(restore)

    def post(self, body, request=None):
        return run(server.api_box_password(
            request or FakeRequest(method="POST", body=body)))

    def open_window(self):
        return mock.patch.object(server, "_first_window_open", return_value=True)

    def test_it_writes_the_two_keys_and_clears_the_auto_flag(self):
        with self.open_window():
            self.post({"password": "a-good-one", "confirm": "a-good-one"})
        updates = self.first_run.update_env_file.call_args[0][1]
        self.assertEqual(updates, {"MRRC_WEB_PASSWORD": "a-good-one",
                                   "MRRC_AUTO_PASSWORD": ""})

    def test_it_makes_the_value_live_without_a_restart(self):
        """Restarting would drop the operator in the middle of onboarding, and
        the next step takes their network away — the retired wizard's comment,
        still true."""
        with self.open_window():
            self.post({"password": "a-good-one", "confirm": "a-good-one"})
        self.assertEqual(server.WEB_PASSWORD, "a-good-one")
        self.assertEqual(os.environ.get("MRRC_AUTO_PASSWORD"), "")

    def test_it_signs_a_session_so_the_operator_is_not_asked_twice(self):
        with self.open_window():
            payload = json.loads(
                self.post({"password": "a-good-one",
                           "confirm": "a-good-one"}).body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["role"], "admin")
        self.assertIn(payload["token"], server._auth_tokens)

    def test_a_short_password_is_refused_before_anything_is_written(self):
        with self.open_window():
            response = self.post({"password": "short", "confirm": "short"})
        self.assertEqual(response.status_code, 400)
        self.first_run.update_env_file.assert_not_called()

    def test_a_mismatch_is_refused(self):
        with self.open_window():
            response = self.post({"password": "a-good-one",
                                  "confirm": "a-good-two"})
        self.assertEqual(response.status_code, 400)
        self.first_run.update_env_file.assert_not_called()

    def test_a_closed_window_without_a_session_is_refused(self):
        with mock.patch.object(server, "_first_window_open",
                               return_value=False), \
                mock.patch.object(server, "_verify_auth", return_value=False):
            response = self.post({"password": "a-good-one",
                                  "confirm": "a-good-one"})
        self.assertEqual(response.status_code, 401)
        self.first_run.update_env_file.assert_not_called()

    def test_a_logged_in_session_may_change_the_password_too(self):
        """The settings page has to be able to, and it does not need the
        window: it has a session."""
        with mock.patch.object(server, "_first_window_open",
                               return_value=False), \
                mock.patch.object(server, "_verify_auth", return_value=True):
            response = self.post({"password": "a-good-one",
                                  "confirm": "a-good-one"})
        self.assertEqual(response.status_code, 200)

    def test_a_listen_only_session_can_never_set_the_first_password(self):
        """R-al. A listen-only session is *logged in*: /api/auth/login adds its
        token to `_auth_tokens` unconditionally and only then mirrors it into
        `_listen_tokens`, so `_verify_auth` (membership in `_auth_tokens`)
        returns True for it.

        The brief's guard, `not _verify_auth(request) and not
        _first_window_open(request)`, therefore evaluates `not True and ...`
        -> False, never fires its 401, and falls through to `_write_password`:
        a read-only session sets the box's first password. This session is not
        the onboarding operator (the window does not apply to it) and is not an
        admin session (the settings-page branch is not for it), so it must be
        refused — and refused with 403, not 401: a listen token *is* logged in,
        and 401 would tell the operator their session expired, which is not
        what is wrong.

        The token here is real (in both sets), so nothing but the guard's
        ordering is under test.
        """
        token = server._make_auth_token()
        server._auth_tokens.add(token)   # a listen token is also an auth token
        server._listen_tokens.add(token)
        self.addCleanup(server._auth_tokens.discard, token)
        self.addCleanup(server._listen_tokens.discard, token)
        request = FakeRequest(method="POST",
                              cookies={server.AUTH_COOKIE: token},
                              body={"password": "a-good-one",
                                    "confirm": "a-good-one"})
        with mock.patch.object(server, "_first_window_open", return_value=False):
            response = self.post({"password": "a-good-one",
                                  "confirm": "a-good-one"}, request=request)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(response.body)["error"],
                         server.LISTEN_ONLY_MESSAGE)
        self.first_run.update_env_file.assert_not_called()

    def test_a_failed_write_is_an_error_not_a_quiet_success(self):
        self.first_run.update_env_file.side_effect = OSError("read-only fs")
        before = server.WEB_PASSWORD
        with self.open_window():
            response = self.post({"password": "a-good-one",
                                  "confirm": "a-good-one"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(server.WEB_PASSWORD, before)


class SingleWriterTests(unittest.TestCase):
    def test_there_is_exactly_one_place_that_clears_the_auto_flag(self):
        """spec R-7. The auto-password flag is what closes the onboarding
        window, so a second writer is a second chance to close it early — or a
        second window that stays open. One writer, and the test in this file
        proves the reader agrees with it."""
        source = inspect.getsource(server)
        self.assertEqual(
            source.count('os.environ["MRRC_AUTO_PASSWORD"]'), 1,
            "the auto-password flag is written in more than one place")


class SetupRouteTests(unittest.TestCase):
    def test_a_configured_box_goes_to_the_settings_page(self):
        """Review Focus #4: a bookmarked /setup on a finished box must land on
        /settings, and must not render the onboarding page again."""
        with mock.patch.object(server, "_first_window_open",
                               return_value=False), \
                mock.patch.object(server, "_verify_auth", return_value=True), \
                mock.patch.object(server, "_password_is_untouched",
                                  return_value=False):
            response = run(server.setup_page(FakeRequest(path="/setup")))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/settings")

    def test_a_listen_only_session_at_setup_goes_to_settings_not_the_page(self):
        """R-al. The HTML branch that renders the onboarding page is for an
        admin session; a listen-only session is logged in but not entitled to
        it. It is redirected to /settings — the surface its token is for, and
        the same branch an already-configured box takes — rather than handed a
        403 JSON body out of an HTML route, which buys no security."""
        token = server._make_auth_token()
        server._auth_tokens.add(token)
        server._listen_tokens.add(token)
        self.addCleanup(server._auth_tokens.discard, token)
        self.addCleanup(server._listen_tokens.discard, token)
        request = FakeRequest(path="/setup", cookies={server.AUTH_COOKIE: token})
        with mock.patch.object(server, "_first_window_open", return_value=False), \
                mock.patch.object(server, "_password_is_untouched",
                                  return_value=True):
            response = run(server.setup_page(request))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/settings")

    def test_an_open_window_renders_the_page(self):
        with mock.patch.object(server, "_first_window_open", return_value=True):
            response = run(server.setup_page(FakeRequest(path="/setup")))
        self.assertEqual(getattr(response, "status_code", 200), 200)

    def test_a_closed_window_without_a_session_goes_to_login(self):
        with mock.patch.object(server, "_first_window_open",
                               return_value=False), \
                mock.patch.object(server, "_verify_auth", return_value=False):
            response = run(server.setup_page(FakeRequest(path="/setup")))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login?next=/setup")


if __name__ == "__main__":
    unittest.main()
