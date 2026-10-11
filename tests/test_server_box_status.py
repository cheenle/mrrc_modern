"""One read that fills the whole settings page.

The retired `/api/manage` reported two things behind a login; this reports those
plus the network and system state, and it is the settings page's only source.
It must be a *logged-in* surface — the safety fields alone (the transmit gate
among them) are not something an unauthenticated caller gets to enumerate.
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


class StatusEndpointTests(unittest.TestCase):
    def status(self, **patches):
        defaults = {
            "network_report": {"addresses": [], "gateways": [], "dns": [],
                              "devices": [], "connections": [],
                              "wifi": {"ssid": "", "signal": 0},
                              "nm_running": False},
            "system_report": {"timezone": "Asia/Shanghai", "uptime_s": 12.5,
                              "services": []},
            "identity_report": {"hostname": "MRRC-Modern",
                                "mdns": "MRRC-Modern.local",
                                "addresses": ["192.168.16.217"],
                                "suggested_name": "MRRC-Modern"},
            "safety_report": [{"key": "MRRC_ALLOW_UNVERIFIED_TX", "value": "0",
                               "read_only": True, "note": "发射门禁"}],
            "helper_available": True,
        }
        defaults.update(patches)
        # return_value=, not a replacement object: the code under test *calls*
        # these. Replacing the module attribute wholesale would work by accident
        # here and break the moment one of them is called with an argument.
        stack = [mock.patch.object(box_ops, name, return_value=value)
                 for name, value in defaults.items()]
        for patcher in stack:
            patcher.start()
            self.addCleanup(patcher.stop)
        # The handler is an authenticated surface, exactly like every other
        # /api/box read: without a session it returns 401 before the body runs
        # (which is what test_a_request_without_a_session_is_refused pins), so
        # every panel test has to arrive with a session. The brief's helper
        # omitted this and read the 401 body in place of the panels.
        auth = mock.patch.object(server, "_verify_auth", return_value=True)
        auth.start()
        self.addCleanup(auth.stop)
        return run(server.api_box_status(FakeRequest(path="/api/box/status")))

    def test_a_request_without_a_session_is_refused(self):
        with mock.patch.object(server, "_verify_auth", return_value=False):
            response = run(server.api_box_status(FakeRequest()))
        self.assertEqual(response.status_code, 401)

    def test_it_hands_back_every_panel_in_one_read(self):
        payload = json.loads(self.status().body)
        self.assertEqual(set(payload),
                         {"network", "system", "identity", "safety",
                          "helper_available", "first_window_open"})

    def test_the_safety_fields_come_with_their_values_and_notes(self):
        payload = json.loads(self.status().body)
        self.assertEqual(payload["safety"][0]["key"],
                         "MRRC_ALLOW_UNVERIFIED_TX")
        self.assertTrue(payload["safety"][0]["read_only"])

    def test_a_missing_helper_is_reported_not_raised(self):
        """Review Focus #3: the page greys the system panel out and keeps the
        other five working."""
        payload = json.loads(self.status(helper_available=False).body)
        self.assertFalse(payload["helper_available"])
        self.assertEqual(payload["identity"]["hostname"], "MRRC-Modern")

    def test_it_reports_whether_the_window_is_still_open(self):
        """The settings page shows that banner; it is the operator's only hint
        that the box is still open to its own subnet."""
        with mock.patch.object(server, "_first_window_open", return_value=True):
            payload = json.loads(self.status().body)
        self.assertTrue(payload["first_window_open"])


if __name__ == "__main__":
    unittest.main()
