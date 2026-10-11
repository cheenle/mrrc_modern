"""The settings page's buttons, at the handler boundary.

Two things every test here is really about:

* **The switch is ordered, not awaited.** `POST /api/box/wifi` returns before the
  network moves, because the request may be travelling over that very network
  (design §5.4 of the old design, §6.2 of the new one). A handler that awaited
  the switch would hang the operator's browser on a socket that is about to
  disappear.
* **A destructive action needs a second word.** Restarting the box drops
  everybody on it, so it takes an explicit `confirm` value rather than a button
  that fires on one click.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

import box_ops
import server
from tests.test_server_box_window import FakeRequest


def run(coro):
    return asyncio.run(coro)


def authed():
    return mock.patch.object(server, "_verify_auth", return_value=True)


class ScanTests(unittest.TestCase):
    def test_a_scan_is_the_same_list_the_onboarding_page_gets(self):
        with authed(), mock.patch.object(
                box_ops, "scan_networks",
                return_value=[{"ssid": "home", "signal": 61,
                               "security": "wpa2", "protected": True}]):
            payload = json.loads(run(server.api_box_wifi_scan(FakeRequest())).body)
        self.assertEqual(payload["networks"][0]["ssid"], "home")

    def test_it_needs_a_session(self):
        with mock.patch.object(server, "_verify_auth", return_value=False):
            response = run(server.api_box_wifi_scan(FakeRequest()))
        self.assertEqual(response.status_code, 401)


class SwitchRequestTests(unittest.TestCase):
    def setUp(self):
        box_ops._state.reset()

    def post(self, body):
        return run(server.api_box_wifi_connect(
            FakeRequest(path="/api/box/wifi", method="POST", body=body)))

    def test_it_hands_the_work_to_a_thread_and_returns_immediately(self):
        started = []

        def slow(ssid, password=""):
            started.append(ssid)
            return {"ok": True}

        with authed(), mock.patch.object(box_ops, "perform_wifi_switch",
                                         side_effect=slow):
            response = self.post({"ssid": "NewNet", "password": "pw"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.body)["switching"])

    def test_an_empty_ssid_is_refused(self):
        with authed():
            response = self.post({"ssid": "  "})
        self.assertEqual(response.status_code, 400)

    def test_a_second_switch_is_refused_while_one_is_running(self):
        box_ops._state.phase = box_ops.PHASE_SWITCHING
        with authed():
            response = self.post({"ssid": "NewNet"})
        self.assertEqual(response.status_code, 409)

    def test_the_password_never_appears_in_a_log_line(self):
        """A PSK in the journal is a PSK in every support bundle."""
        with authed(), mock.patch.object(box_ops, "perform_wifi_switch"), \
                mock.patch.object(server, "logger") as log:
            self.post({"ssid": "NewNet", "password": "hunter2secret"})
        for call in log.method_calls:
            self.assertNotIn("hunter2secret", str(call))

    def test_it_needs_a_session(self):
        with mock.patch.object(server, "_verify_auth", return_value=False):
            response = self.post({"ssid": "NewNet"})
        self.assertEqual(response.status_code, 401)


class SwitchStatusTests(unittest.TestCase):
    def test_it_reports_the_state_the_page_reconnects_to(self):
        box_ops._state.reset()
        box_ops._state.phase = box_ops.PHASE_FAILED
        box_ops._state.reason = "没拿到地址"
        with authed():
            payload = json.loads(run(server.api_box_wifi_status(FakeRequest())).body)
        self.assertEqual(payload["phase"], box_ops.PHASE_FAILED)
        self.assertEqual(payload["reason"], "没拿到地址")
        self.assertNotIn("password", payload)

    def test_it_needs_a_session(self):
        with mock.patch.object(server, "_verify_auth", return_value=False):
            response = run(server.api_box_wifi_status(FakeRequest()))
        self.assertEqual(response.status_code, 401)


class HostnameTests(unittest.TestCase):
    def post(self, body):
        return run(server.api_box_hostname(
            FakeRequest(path="/api/box/hostname", method="POST", body=body)))

    def test_a_good_name_is_applied_and_avahi_is_nudged(self):
        """avahi-daemon reads the hostname at start and on reload; it does not
        follow hostnamectl on its own, so without the nudge the new name is
        advertised whenever avahi next feels like it."""
        with authed(), \
                mock.patch.object(box_ops, "set_hostname",
                                  return_value={"ok": True}) as rename, \
                mock.patch.object(box_ops, "reload_avahi",
                                  return_value={"ok": True}) as reload_:
            response = self.post({"hostname": "MRRC-Modern"})
        self.assertEqual(response.status_code, 200)
        rename.assert_called_once()
        reload_.assert_called_once()

    def test_a_bad_name_is_refused_with_the_validation_message(self):
        with authed():
            response = self.post({"hostname": "-nope"})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(json.loads(response.body)["error"])

    def test_a_failed_rename_does_not_nudge_avahi(self):
        with authed(), \
                mock.patch.object(box_ops, "set_hostname",
                                  return_value={"ok": False,
                                                "error": "helper unavailable"}), \
                mock.patch.object(box_ops, "reload_avahi") as reload_:
            response = self.post({"hostname": "MRRC-Modern"})
        self.assertEqual(response.status_code, 400)
        reload_.assert_not_called()


class TimezoneTests(unittest.TestCase):
    def test_a_real_zone_goes_through(self):
        with authed(), mock.patch.object(box_ops, "set_timezone",
                                         return_value={"ok": True}):
            response = run(server.api_box_timezone(
                FakeRequest(method="POST", body={"timezone": "Asia/Shanghai"})))
        self.assertEqual(response.status_code, 200)

    def test_a_made_up_zone_is_refused_before_the_helper(self):
        with authed(), mock.patch.object(box_ops, "set_timezone") as call:
            response = run(server.api_box_timezone(
                FakeRequest(method="POST", body={"timezone": "Not/AZone"})))
        self.assertEqual(response.status_code, 400)
        call.assert_not_called()


class LinkTests(unittest.TestCase):
    def setUp(self):
        box_ops._state.reset()

    def post(self, body):
        return run(server.api_box_link(
            FakeRequest(path="/api/box/link", method="POST", body=body)))

    def test_a_link_change_is_ordered_like_the_switch(self):
        with authed(), mock.patch.object(box_ops, "perform_link_change",
                                         return_value={"ok": True}):
            response = self.post({"profile": "有线连接 2", "method": "manual",
                                  "address": "192.168.16.217/24"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.body)["changing"])

    def test_an_invalid_request_is_refused(self):
        with authed():
            response = self.post({"profile": "x", "method": "manual",
                                  "address": "nonsense"})
        self.assertEqual(response.status_code, 400)

    def test_a_missing_profile_is_refused(self):
        with authed():
            response = self.post({"method": "auto"})
        self.assertEqual(response.status_code, 400)


class RebootTests(unittest.TestCase):
    def post(self, body):
        return run(server.api_box_system_reboot(
            FakeRequest(path="/api/box/system/reboot", method="POST",
                        body=body)))

    def test_it_takes_an_explicit_confirmation(self):
        with authed(), mock.patch.object(box_ops, "reboot") as reboot:
            response = self.post({})
        self.assertEqual(response.status_code, 400)
        reboot.assert_not_called()

    def test_the_confirmation_is_the_word_reboot(self):
        with authed(), mock.patch.object(box_ops, "reboot",
                                         return_value={"ok": True}) as reboot:
            response = self.post({"confirm": "reboot"})
        self.assertEqual(response.status_code, 200)
        reboot.assert_called_once()

    def test_a_helper_that_cannot_do_it_is_reported(self):
        with authed(), mock.patch.object(
                box_ops, "reboot",
                return_value={"ok": False, "error": "helper unavailable"}):
            response = self.post({"confirm": "reboot"})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(json.loads(response.body)["error"])


if __name__ == "__main__":
    unittest.main()
