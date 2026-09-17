"""Vendored receiver + deployment invariants (spec 2026-09-17 §9).

Structural checks only: the deployment itself is verified by the script's own
probes (systemd is-active, 401 without password) and by the acceptance run.
"""
import importlib.util
import inspect
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
RECEIVER_PATH = ROOT / "tools" / "support_receiver" / "server.py"

# Loaded by path, not as `import server`: the vendored file keeps the sibling
# project's name, which would collide with this repo's own server.py for every
# name-based resolver (pyright resolved the app's module and reported its
# attributes missing).  tests/test_release_artifacts.py uses the same loader.
_spec = importlib.util.spec_from_file_location("support_receiver", RECEIVER_PATH)
assert _spec is not None and _spec.loader is not None
receiver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(receiver)


class ReceiverVendoringTests(unittest.TestCase):
    def test_layout_defaults_are_this_product(self):
        self.assertEqual(receiver.PRODUCT, "mrrc_modern")
        self.assertIn("support-modern", receiver.DIR)
        self.assertEqual(receiver.ID_RE.pattern, r"^\d{8}-\d{6}-[0-9a-f]{4}$")

    def test_meta_records_the_product(self):
        self.assertIn('"product": PRODUCT', inspect.getsource(receiver))

    def test_port_default_is_8098_not_the_sibling_projects(self):
        source = inspect.getsource(receiver.main)
        self.assertIn('SUPPORT_PORT", "8098"', source)

    def test_id_regex_refuses_traversal(self):
        self.assertIsNone(receiver.ID_RE.match("../../etc/passwd"))
        self.assertIsNotNone(receiver.ID_RE.match("20260917-072530-ab12"))

    def test_list_requires_a_password(self):
        source = inspect.getsource(receiver)
        self.assertIn("_require_auth", source)
        self.assertIn("Basic", source)

    def test_refuses_to_start_without_a_password(self):
        with mock.patch.object(receiver, "PASSWORD", ""):
            self.assertEqual(receiver.main(), 2)


class ReceiverHttpTests(unittest.TestCase):
    """One round trip against the real handler: create -> PUT -> list."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(receiver, "DIR", str(self.dir)),
            mock.patch.object(receiver, "PASSWORD", "s3cret"),
            mock.patch.object(receiver, "_RATE", {}),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        from http.server import ThreadingHTTPServer
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), receiver.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _shutdown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _post(self, path, payload):
        import json
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_create_put_and_auth_gated_list(self):
        import json
        created = self._post("/api/create", {"problem": "测试", "version": "1.17.0"})
        self.assertTrue(created["ok"], created)
        bundle_id = created["id"]
        self.assertIsNotNone(receiver.ID_RE.match(bundle_id))

        put = urllib.request.Request(f"{self.base}/api/{bundle_id}/bundle",
                                     data=b"PK-fake", method="PUT")
        with urllib.request.urlopen(put, timeout=5) as response:
            self.assertTrue(json.loads(response.read().decode("utf-8"))["ok"])

        stored = self.dir / bundle_id
        self.assertTrue((stored / "bundle.zip").exists())
        meta = json.loads((stored / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["product"], "mrrc_modern")
        self.assertEqual(meta["version"], "1.17.0")

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"{self.base}/api/list", timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_rejects_a_bundle_id_that_is_not_whitelisted(self):
        put = urllib.request.Request(f"{self.base}/api/..%2fetc%2fpasswd/bundle",
                                     data=b"x", method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(put, timeout=5)
        self.assertIn(ctx.exception.code, (400, 404))


class DeployScriptTests(unittest.TestCase):
    def setUp(self):
        self.script = (ROOT / "deploy_support_receiver.sh").read_text(encoding="utf-8")

    def test_targets_the_dedicated_unit_port_and_storage(self):
        self.assertIn("support-receiver-modern", self.script)
        self.assertIn("SUPPORT_PORT=8098", self.script)
        self.assertIn("/var/www/support-modern", self.script)
        self.assertIn("/mrrc_modern/support/", self.script)

    def test_password_file_is_0600_and_never_echoed(self):
        self.assertIn("chmod 600", self.script)
        self.assertIn("umask 077", self.script)
        self.assertNotIn('echo "$PW"', self.script)

    def test_no_password_literal_is_committed(self):
        self.assertIsNone(re.search(r"SUPPORT_PASSWORD=[A-Za-z0-9]{8,}", self.script))

    def test_nginx_block_keeps_the_real_host_variable(self):
        """A `\\$host` would be written into the nginx config verbatim."""
        self.assertNotIn("\\$host", self.script)
        self.assertIn("proxy_set_header Host $host;", self.script)


if __name__ == "__main__":
    unittest.main()
