"""The two pages, and the one rule they share.

`/setup` and `/settings` are separate HTML by decision (the onboarding page has
to work with no session at all, and the settings page must never be reachable
without one). They share a backend and a client script — this file keeps the
backend from growing a second implementation for one of them, and keeps the
onboarding page from acquiring anything a passwordless caller should not read.
"""
from __future__ import annotations

import asyncio
import re
import unittest
from pathlib import Path

import net_wifi
import server
from tests.test_server_box_window import FakeRequest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def run(coro):
    return asyncio.run(coro)


class SourceTests(unittest.TestCase):
    def test_both_pages_load_the_shared_client(self):
        for name in ("setup.html", "settings.html"):
            text = (STATIC / name).read_text(encoding="utf-8")
            with self.subTest(page=name):
                self.assertIn("box_api.js", text)

    def test_the_shared_client_is_not_versionless(self):
        """sw.js is cache-first, and a script served under an unchanged key is
        corrected on disk while the browser runs the old one."""
        for name in ("setup.html", "settings.html"):
            text = (STATIC / name).read_text(encoding="utf-8")
            with self.subTest(page=name):
                self.assertRegex(text, r"box_api\.js\?v=\d+")

    def test_the_onboarding_page_reads_only_these_three_endpoints(self):
        """spec §5.3. Two of the three are in the window; the third —
        /api/box/wifi — is not, and is reachable only because saving the
        password signs a session on the spot. So the Wi-Fi card must stay locked
        until that write succeeds, which is what `Box.disabled('#scan-btn',
        false)` inside `unlockWifi()` is for.

        Nothing else: the safety fields and the service list are not things an
        open page gets to enumerate."""
        text = (STATIC / "setup.html").read_text(encoding="utf-8")
        called = set(re.findall(r"Box\.api\('([^']+)'", text))
        self.assertEqual(called, {"/api/box/onboarding", "/api/box/password",
                                  "/api/box/wifi"})

    def test_the_settings_page_does_not_read_the_onboarding_endpoint(self):
        """That endpoint is the one thing a passwordless caller may reach. A page
        that already has a session has no business reading it."""
        text = (STATIC / "settings.html").read_text(encoding="utf-8")
        self.assertNotIn("/api/box/onboarding", text)

    def test_the_reboot_button_carries_its_confirmation(self):
        """A box reboot drops every connection and takes a minute to come back,
        and it sits directly above a button that restarts the service instead.
        Two buttons that read almost the same, doing very different things — so
        the page asks twice, and the second ask is the one that reaches the
        server (which checks for the literal string)."""
        text = (STATIC / "settings.html").read_text(encoding="utf-8")
        self.assertIn("confirm: 'reboot'", text)
        self.assertIn("window.confirm", text)

    def test_the_onboarding_page_warns_about_a_shared_network(self):
        """spec §5.4: the window's predicate is weaker than the retired
        hotspot's, and the honest mitigation is to say so where the operator can
        read it before they rely on it."""
        text = (STATIC / "setup.html").read_text(encoding="utf-8")
        self.assertIn("信任的网络", text)


class SettingsPageDeviceKeyTests(unittest.TestCase):
    """The page and `net_wifi.devices()` must agree on a device row's keys.

    The page once read `device.interface` while the producer emits the key
    `device`, so every adapter row rendered literally `网卡 undefined: connected
    → …`. Nothing caught it, because the page's keys and the producer's keys
    were never compared. This parses the `device.<key>` reads out of the page's
    own source and asserts each is a key `net_wifi.devices()` actually emits —
    an invariant over both sides, so a rename on either side fails here.
    """

    def emitted_keys(self):
        def runner(argv):
            return net_wifi.NmResult(
                0, "eth0:ethernet:connected:有线连接 2\n"
                   "wlan0:wifi:disconnected:\n", "")
        rows = net_wifi.devices(runner)
        self.assertTrue(rows, "the canned listing produced no device rows")
        return set(rows[0])

    def test_the_page_reads_only_keys_the_producer_emits(self):
        text = (STATIC / "settings.html").read_text(encoding="utf-8")
        read = set(re.findall(r"\bdevice\.([A-Za-z_$][\w$]*)", text))
        self.assertTrue(read, "the page reads no device fields at all")
        self.assertEqual(read - self.emitted_keys(), set())

    def test_the_error_was_the_interface_key_not_the_device_key(self):
        """Pin the exact regression: the render now reads `device.device`."""
        text = (STATIC / "settings.html").read_text(encoding="utf-8")
        self.assertIn("device.device", text)
        self.assertNotIn("device.interface", text)


class UnauthenticatedClientScriptTests(unittest.TestCase):
    """R-at. /setup renders before any session exists and loads its client with
    ``<script src="box_api.js">``, which the browser resolves — from /setup — to
    ``GET /box_api.js``. Two gates stand between that request and the script:
    the auth middleware's ``PUBLIC_PATHS`` set and ``serve_static``'s own
    exemption tuple. An unauthenticated caller must pass *both*, or the browser
    is handed the login page's HTML where it expected JavaScript: ``window.Box``
    is never defined, the inline handler throws, and every control on the
    onboarding page is dead — including the password form, which is the only
    thing that closes the first window.

    These call the real handlers rather than asserting a string is in a set,
    because the failure mode is a redirect and only running the code shows it.
    """

    def unauthenticated(self):
        # client=None, no cookies/headers/query_params — a caller who has never
        # logged in.
        return FakeRequest(path="/box_api.js", client=None)

    def test_the_middleware_passes_the_client_script_through(self):
        async def call_next(request):
            return "served"
        response = run(server.auth_middleware(self.unauthenticated(), call_next))
        self.assertEqual(response, "served")

    def test_serve_static_serves_the_client_script_without_a_session(self):
        response = run(server.serve_static("box_api.js", self.unauthenticated()))
        self.assertEqual(getattr(response, "status_code", 200), 200)
        self.assertEqual(str(response.media_type), "application/javascript")


class RouteTests(unittest.TestCase):
    def test_the_settings_page_is_served(self):
        response = run(server.settings_page(None))
        self.assertEqual(getattr(response, "status_code", 200), 200)

    def test_both_page_routes_sit_above_the_spa_fallback(self):
        """A GET route registered below the catch-all is answered with
        index.html instead of its own handler — that is how /api/cloud/state came
        back as a web page in v1.24.0."""
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        fallback = source.index('app.add_api_route("/{path:path}", serve_static')
        for route in ('@app.get("/setup"', '@app.get("/settings"'):
            with self.subTest(route=route):
                self.assertLess(source.index(route), fallback)

    def test_no_document_names_the_retired_manage_page(self):
        for relative in ("server.py", "static/index.html", "static/listen.html"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertNotIn('"/manage"', text)
                self.assertNotIn("manage.html", text)


if __name__ == "__main__":
    unittest.main()
