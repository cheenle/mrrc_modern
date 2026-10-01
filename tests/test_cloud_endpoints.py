"""Regression coverage for the Cloud Hub endpoints in ``server.py``.

On 2026-10-01 a packaged v1.24.0 instance answered the settings dialog's
云端申请 with ``500 Internal Server Error`` and this traceback::

    File "server.py", line 4035, in api_cloud_apply
    File "server.py", line 3987, in _cloud_portal
    NameError: name 'cloud_hub' is not defined

Two independent breakages produced it: ``server.py`` called ``cloud_hub.*``
without ever importing the module, and the refresh endpoint asked ``config``
for ``WEB_PORT`` while this module only binds the bare name
(``from config import ..., WEB_PORT, ...``). Both slipped past
``test_cloud_hub.py`` because that file exercises the module in isolation -
``import cloud_hub`` at the top of a test is exactly what masked the missing
import in the server. These tests drive the HTTP endpoints themselves, so a
missing binding can no longer hide behind a passing unit test.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cloud_hub
import server


class _Request:
    """Only what the endpoints touch: auth and the JSON body (auth is patched)."""

    def __init__(self, body=None):
        self._body = body or {}
        self.headers = {"cookie": f"{server.AUTH_COOKIE}=test-token"}

    async def json(self):
        return self._body


class CloudApplyEndpointTests(unittest.TestCase):
    """POST /api/cloud/apply must reach the portal and persist the request token."""

    def test_apply_uses_the_cloud_hub_module_and_keeps_the_token(self):
        request = _Request({"callsign": "bg9zzz", "contact": "op@example.com"})
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "mrrc_modern.env"
            with patch.object(server, "_verify_auth", return_value=True), \
                 patch.object(server, "_config_file_path", return_value=env), \
                 patch.object(cloud_hub, "apply",
                              return_value={"request_token": "req-token",
                                            "status": "applied"}) as apply_mock:
                reply = asyncio.run(server.api_cloud_apply(request))

            payload = json.loads(bytes(reply.body))
            self.assertEqual(reply.status_code, 200)
            self.assertTrue(payload["submitted"])
            self.assertEqual(payload["callsign"], "BG9ZZZ")
            apply_mock.assert_called_once_with(cloud_hub.PORTAL_DEFAULT, "BG9ZZZ",
                                               "op@example.com", "mrrc_modern")
            written = env.read_text(encoding="utf-8")
            self.assertIn("MRRC_CLOUD_TOKEN=req-token", written)
            self.assertIn("MRRC_CLOUD_CALLSIGN=BG9ZZZ", written)


class CloudRefreshEndpointTests(unittest.TestCase):
    """POST /api/cloud/refresh must connect an approved instance using the app's port."""

    def test_refresh_connects_with_the_real_web_port_and_records_the_entry(self):
        request = _Request()
        grant = {"connected": True, "status": "granted", "label": "bg9zzz",
                 "port": 18899, "fqdn": "bg9zzz.mrrc.vlsc.net",
                 "entry": "https://bg9zzz.mrrc.vlsc.net:9988/",
                 "cert": "C:/certs/bg9zzz.crt", "tunnel_config": "C:/fleet/frpc-bg9zzz.toml",
                 "tunnel_started": True}
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "mrrc_modern.env"
            env.write_text("MRRC_CLOUD_CALLSIGN=BG9ZZZ\nMRRC_CLOUD_TOKEN=req-token\n",
                           encoding="utf-8")
            with patch.object(server, "_verify_auth", return_value=True), \
                 patch.object(server, "_config_file_path", return_value=env), \
                 patch.object(cloud_hub, "status", return_value={"status": "granted"}), \
                 patch.object(cloud_hub, "connect", return_value=grant) as connect_mock:
                reply = asyncio.run(server.api_cloud_refresh(request))

            payload = json.loads(bytes(reply.body))
            self.assertEqual(reply.status_code, 200)
            self.assertTrue(payload["connected"])
            # server.py imports WEB_PORT as a bare name; asking for config.WEB_PORT
            # raises NameError and turns an approved instance into a 500.
            self.assertEqual(connect_mock.call_args.kwargs["local_port"], server.WEB_PORT)
            # The entry is written to the launcher's config file so the tunnel
            # survives the next restart.
            written = env.read_text(encoding="utf-8")
            self.assertIn("MRRC_CLOUD_LABEL=bg9zzz", written)


class ServerRouteOrderTests(unittest.TestCase):
    """The SPA fallback must not swallow API routes.

    ``serve_static`` matches *every* GET path. In v1.24.0 it was registered (by decorator, in the
    middle of the module) before the Cloud Hub endpoints, so a frozen build answered
    ``GET /api/cloud/state`` with 200 + index.html - the settings dialog could never read its own
    state, while the POST endpoints beside it reached their handlers and 500'd for the missing
    import. Route order is the only thing that keeps this honest, so it is asserted here.
    """

    @staticmethod
    def _paths() -> list[str]:
        return [getattr(route, "path", "") for route in server.app.router.routes]

    def test_the_state_route_is_registered_before_the_spa_fallback(self):
        paths = self._paths()
        self.assertIn("/api/cloud/state", paths)
        self.assertLess(paths.index("/api/cloud/state"), paths.index("/{path:path}"))

    def test_no_api_route_is_registered_after_the_spa_fallback(self):
        paths = self._paths()
        catch_all = paths.index("/{path:path}")
        shadowed = [p for p in paths[catch_all + 1:] if p.startswith("/api/")]
        self.assertEqual(shadowed, [],
                         "these API routes would be answered with index.html: " + repr(shadowed))


if __name__ == "__main__":
    unittest.main()
