"""The management page: one place to see the things you cannot otherwise see.

It is read-only on purpose. The transmit gate, the PTT cap and the heartbeat
timeout are the few security invariants this system has (AD-019 / NFR-067), and
a web switch is the shape that turns an invariant into an incident. Showing the
current value is the half that helps; changing it goes through the config layer
when that lands, with its own confirmations.

The other half is the hotspot. AP mode is the one premise that could not be
checked without the hardware, so the day the box arrives the first question is
"is it up", and this page is where that question gets an answer.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server


class _ApState:
    """The parts of net_wifi.ApState this page reads."""

    def __init__(self, mode="hotspot", ssid="MRRC-Setup",
                 url="https://10.42.0.1:8888", deadline=1000.0, heartbeat=400.0):
        self.mode = mode
        self.ssid = ssid
        self.url = url
        self.deadline = deadline
        self.heartbeat = heartbeat


class FakeRequest:
    """A request with a peer address and nothing else.

    The handlers are called directly rather than through TestClient, which is
    what the rest of the suite does: starlette's test client needs httpx, and
    the call convention here is `asyncio.run(handler(FakeRequest()))`.
    """

    def __init__(self, host="192.168.1.5"):
        self.client = type("Peer", (), {"host": host})() if host else None
        self.headers = {}


def _manage(self, authenticated=True):
    """Call GET /api/manage and return (status, body)."""
    with mock.patch.object(server, "_verify_auth", return_value=authenticated), \
         mock.patch.object(server, "_setup_ap_state_dir", return_value=Path("/nonexistent")), \
         mock.patch.object(server.net_wifi, "read_state",
                           return_value=self._ap_state):
        resp = asyncio.run(server.api_manage(FakeRequest()))
    return resp.status_code, json.loads(resp.body)


class ManageStatusTests(unittest.TestCase):
    def setUp(self):
        self._ap_state = _ApState()

    def _get(self, authenticated=True):
        status, body = _manage(self, authenticated)
        return status, body

    def test_it_requires_a_session(self):
        """The passwordless window is for the wizard only. This page reports
        what the safety gates are set to, and that is not for a passer-by."""
        status, _body = self._get(authenticated=False)
        self.assertEqual(status, 401)

    def test_it_reports_what_the_supervisor_published(self):
        _status, body = self._get()
        self.assertEqual(body["hotspot"]["mode"], "hotspot")
        self.assertEqual(body["hotspot"]["ssid"], "MRRC-Setup")

    def test_the_hotspot_is_reported_as_off_when_it_is(self):
        self._ap_state = _ApState(mode="off", ssid=None)
        _status, body = self._get()
        self.assertEqual(body["hotspot"]["mode"], "off")

    def test_remaining_time_is_computed_on_the_box_not_in_the_browser(self):
        """Both numbers come from the box's clock in the same publish. A phone's
        clock can be minutes out, and subtracting in the browser would show a
        window that is already closed or one that never closes."""
        _status, body = self._get()
        self.assertAlmostEqual(body["hotspot"]["remaining_s"], 600.0, places=1)

    def test_every_safety_field_is_reported_read_only(self):
        _status, body = self._get()
        keys = {f["key"] for f in body["safety"]}
        self.assertEqual(keys, {
            "MRRC_ALLOW_UNVERIFIED_TX",
            "MRRC_PTT_MAX_TX_SECONDS",
            "MRRC_REMOTE_SESSION_TX_HEARTBEAT_S",
        })
        self.assertTrue(all(f["read_only"] for f in body["safety"]),
                        "nothing on this page writes; changing a gate is not a switch")

    def test_each_safety_field_says_what_it_means(self):
        """A number without a consequence next to it is a number nobody
        respects."""
        _status, body = self._get()
        for field in body["safety"]:
            self.assertTrue(field["note"].strip(), f"{field['key']} has no explanation")

    def test_the_transmit_gate_shows_what_the_radio_would_use(self):
        with mock.patch.dict(os.environ, {"MRRC_ALLOW_UNVERIFIED_TX": "0"}):
            _status, body = self._get()
        gate = next(f for f in body["safety"] if f["key"] == "MRRC_ALLOW_UNVERIFIED_TX")
        self.assertEqual(gate["value"], "0")


class ManagePageTests(unittest.TestCase):
    def test_the_page_loads(self):
        page = Path(server.STATIC_DIR) / "manage.html"
        self.assertTrue(page.exists(), "the route serves this file; it has to exist")
        html = page.read_text(encoding="utf-8")
        self.assertIn("/api/manage", html, "the page has to fetch its own data")

    def test_the_page_does_not_pretend_to_write(self):
        html = (Path(server.STATIC_DIR) / "manage.html").read_text(encoding="utf-8")
        self.assertNotIn('method="post"', html.lower())
        self.assertNotIn("PUT", html)


if __name__ == "__main__":
    unittest.main()
