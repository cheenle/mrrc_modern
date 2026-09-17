"""Support REST endpoints (spec 2026-09-17 §7/§11).

No HTTP client: the handlers are called with a cast fake request exactly like
tests/test_server_setup does.  The live audio/radio snapshot is mocked, so these
tests never touch hardware.
"""
import asyncio
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any, cast
from unittest import mock

from starlette.requests import Request

import server
import support_bundle


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _req(body: dict) -> Request:
    """A request that only answers .json() — what the handlers actually use."""
    return cast(Request, _FakeRequest(body))


def _payload(response) -> dict:
    """JSON body of a JSONResponse (body is bytes | memoryview in Starlette)."""
    return cast(dict, json.loads(bytes(response.body).decode("utf-8")))


class SupportApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "server.log").write_text(
            "2026-09-17 07:00:00 [INFO] mrrc: Server ready!\nMRRC_WEB_PASSWORD=hunter2\n",
            encoding="utf-8")
        (self.root / "mrrc_modern.env").write_text(
            "MRRC_WEB_PASSWORD=hunter2\nMRRC_WEB_PORT=8888\n", encoding="utf-8")
        patches = [
            mock.patch.object(server, "LOG_DIR", logs),
            mock.patch.object(server, "SUPPORT_OUT_DIR", self.root / "support-out"),
            mock.patch.object(server, "_verify_auth", return_value=True),
            mock.patch.object(server, "_config_file_path",
                              return_value=self.root / "mrrc_modern.env"),
            mock.patch.object(server, "_support_env_snapshot", return_value={"version": "1.17.0"}),
            # hermetic: the repo's own logs/ must not leak into the test bundle
            mock.patch.object(server, "_runtime_dir", return_value=self.root),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _build(self):
        return asyncio.run(server.api_support_bundle(_req(
            {"problem": "接收有杂音", "contact": "BH1XXX", "client": {"ua": "iPhone"}})))

    def test_build_returns_files_and_redaction_count(self):
        payload = _payload(self._build())
        self.assertTrue(payload["ok"])
        self.assertGreater(payload["redactions"], 0)
        self.assertIn("logs/server.log", payload["files"])
        self.assertIn("diagnostics/client.json", payload["files"])
        self.assertTrue(Path(payload["path"]).exists())

    def test_bundle_excludes_secret_and_recordings(self):
        payload = _payload(self._build())
        with zipfile.ZipFile(payload["path"]) as archive:
            blob = b"".join(archive.read(n) for n in archive.namelist())
        self.assertNotIn(b"hunter2", blob)

    def test_build_is_single_flight(self):
        server._support_build_lock.acquire()
        try:
            payload = _payload(self._build())
        finally:
            server._support_build_lock.release()
        self.assertFalse(payload["ok"])
        self.assertIn("in progress", payload["reason"])

    def test_upload_forwards_and_reports_the_remote_id(self):
        built = _payload(self._build())
        with mock.patch.object(server, "_support_upload",
                               return_value="20260917-080000-abcd") as up:
            payload = _payload(asyncio.run(
                server.api_support_upload(_req({"id": built["id"]}))))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["remoteId"], "20260917-080000-abcd")
        self.assertEqual(Path(up.call_args[0][0]).name, f"support-{built['id']}.zip")

    def test_upload_failure_still_hands_back_the_local_path(self):
        built = _payload(self._build())
        with mock.patch.object(server, "_support_upload",
                               side_effect=OSError("no route to host")):
            payload = _payload(asyncio.run(
                server.api_support_upload(_req({"id": built["id"]}))))
        self.assertFalse(payload["ok"])
        self.assertIn("no route to host", payload["reason"])
        self.assertTrue(payload["localPath"].endswith(".zip"))

    def test_unknown_id_is_refused(self):
        payload = _payload(asyncio.run(
            server.api_support_upload(_req({"id": "../../etc/passwd"}))))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["localPath"], "")

    def test_save_copies_into_the_export_dir(self):
        built = _payload(self._build())
        payload = _payload(asyncio.run(
            server.api_support_save(_req({"id": built["id"]}))))
        self.assertTrue(payload["ok"])
        self.assertTrue(Path(payload["path"]).exists())
        self.assertEqual(Path(payload["path"]).name, f"support-{built['id']}.zip")

    def test_endpoints_are_auth_gated(self):
        with mock.patch.object(server, "_verify_auth", return_value=False):
            response = asyncio.run(server.api_support_bundle(_req({})))
        self.assertEqual(response.status_code, 401)


class SupportUploadTransportTests(unittest.TestCase):
    """`_support_upload` is the only network code: verify it against a stub."""

    class _Response:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _bundle(self, tmp):
        bundle = Path(tmp) / "support-20260917-080000-abcd.zip"
        bundle.write_bytes(b"PK-fake")
        return bundle

    def test_create_then_put_with_the_zip_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def fake_urlopen(request, timeout=None):
                calls.append(request)
                if request.get_method() == "PUT":
                    return self._Response({"ok": True})
                return self._Response({"ok": True, "id": "20260917-080000-abcd"})

            with mock.patch.object(server, "SUPPORT_URL", "https://example.invalid/support/"), \
                 mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
                remote_id = server._support_upload(self._bundle(tmp))

        self.assertEqual(remote_id, "20260917-080000-abcd")
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].full_url.endswith("/support/api/create"))
        self.assertEqual(calls[0].get_method(), "POST")
        self.assertTrue(calls[1].full_url.endswith("/support/api/20260917-080000-abcd/bundle"))
        self.assertEqual(calls[1].get_method(), "PUT")
        self.assertEqual(calls[1].data, b"PK-fake")

    def test_create_failure_raises_with_the_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(server, "SUPPORT_URL", "https://example.invalid/support/"), \
                 mock.patch("urllib.request.urlopen",
                            return_value=self._Response({"ok": False, "reason": "rate limited"})):
                with self.assertRaises(RuntimeError) as ctx:
                    server._support_upload(self._bundle(tmp))
        self.assertIn("rate limited", str(ctx.exception))

    def test_non_json_response_raises_a_readable_error(self):
        class _Html(self._Response):
            def read(self):
                return b"<html>502 Bad Gateway</html>"

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(server, "SUPPORT_URL", "https://example.invalid/support/"), \
                 mock.patch("urllib.request.urlopen", return_value=_Html({})):
                with self.assertRaises(RuntimeError) as ctx:
                    server._support_upload(self._bundle(tmp))
        self.assertIn("未返回 JSON", str(ctx.exception))


class _FakeCaps:
    model_name = "ft710"
    display_name = "Yaesu FT-710"
    scope_type = "ft4222"
    tx_gated = False


class _FakeBackend:
    capabilities = _FakeCaps()


class _FakeRadio:
    serial_connected = True


class SupportEnvSnapshotTests(unittest.TestCase):
    """The snapshot must really read the live objects.

    The per-block try/except in `_support_env_snapshot` is deliberate (a broken
    helper must not fail the bundle), but it would also hide a bug like calling
    `capabilities()` — it is a @property, so the TypeError would be swallowed
    into {"error": ...}.  These tests drive the real code path with stubs.
    """

    def test_reads_capabilities_without_error(self):
        with mock.patch.object(server, "backend", _FakeBackend()), \
             mock.patch.object(server, "radio", _FakeRadio()), \
             mock.patch.object(server, "audio", None), \
             mock.patch("server._list_audio_devices", return_value={"rx": [], "tx": []}):
            snapshot = server._support_env_snapshot()
        self.assertNotIn("error", snapshot["radio"], snapshot["radio"])
        self.assertEqual(snapshot["radio"]["model"], "ft710")
        self.assertEqual(snapshot["radio"]["scope_type"], "ft4222")
        self.assertTrue(snapshot["radio"]["serial_connected"])
        self.assertNotIn("error", snapshot["audio"], snapshot["audio"])

    def test_a_broken_helper_degrades_instead_of_failing(self):
        class _Boom:
            @property
            def capabilities(self):
                raise RuntimeError("backend exploded")

        with mock.patch.object(server, "backend", _Boom()), \
             mock.patch.object(server, "radio", _FakeRadio()), \
             mock.patch.object(server, "audio", None):
            snapshot = server._support_env_snapshot()
        self.assertIn("backend exploded", snapshot["radio"]["error"])
        self.assertEqual(
            snapshot["version"],
            support_bundle.detect_version(server._runtime_dir(), server._resource_dir()))


if __name__ == "__main__":
    unittest.main()
