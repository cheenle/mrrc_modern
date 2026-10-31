"""Vendored receiver + deployment invariants (spec 2026-09-17 §9).

Structural checks only: the deployment itself is verified by the script's own
probes (systemd is-active, 401 without password) and by the acceptance run.
"""
import base64
import importlib.util
import inspect
import json
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


class BoardDecisionHttpTests(unittest.TestCase):
    """Authenticated board-decision queue backed by files under SUPPORT_DIR
    (spec docs/superpowers/specs/2026-11-01-support-board-web-decisions-design.md)."""

    BACKLOG = "20261024-120317-2e05"
    BACKLOG2 = "20261024-120321-2e09"
    SCHEDULED = "20261024-120318-2e06"
    REJECTED = "20261024-120319-2e07"
    DONE = "20261024-120320-2e08"
    UNKNOWN = "20261024-120322-2e0a"

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
        self.mirror = {
            self.BACKLOG: {"status": "backlog", "kind": "feature", "title": "待决策 A"},
            self.BACKLOG2: {"status": "backlog", "kind": "feature", "title": "待决策 B"},
            self.SCHEDULED: {"status": "scheduled", "kind": "bug", "title": "已排期"},
            self.REJECTED: {"status": "rejected", "kind": "feature", "title": "不处理"},
            self.DONE: {"status": "done", "kind": "bug", "title": "已完成"},
        }
        self._write_mirror(self.mirror)
        from http.server import ThreadingHTTPServer
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), receiver.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _shutdown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @staticmethod
    def _stop(httpd):
        httpd.shutdown()
        httpd.server_close()

    def _write_mirror(self, state):
        (self.dir / "board_state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def _call(self, path, method="GET", payload=None, raw_body=None, headers=None,
              auth=True):
        req_headers = {}
        if auth:
            token = base64.b64encode(b"mrrc:s3cret").decode("ascii")
            req_headers["Authorization"] = f"Basic {token}"
        body = raw_body
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        if headers:
            req_headers.update(headers)
        request = urllib.request.Request(self.base + path, data=body, method=method,
                                         headers=req_headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8")
            try:
                return exc.code, json.loads(text)
            except ValueError:
                return exc.code, {}

    def _create(self, bundle_id=BACKLOG, action="schedule", note="排期",
                headers=None, **kwargs):
        payload = {"bundle_id": bundle_id, "action": action, "note": note}
        decision_headers = {"X-MRRC-Decision": "1"}
        decision_headers.update(headers or {})
        return self._call("/api/board/decisions", method="POST", payload=payload,
                          headers=decision_headers, **kwargs)

    def _queue_files(self):
        folder = self.dir / "board-decisions"
        return sorted(folder.glob("*.json")) if folder.is_dir() else []

    def test_state_returns_mirror_and_pending_decisions(self):
        code, body = self._call("/api/board/state")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["board"], self.mirror)
        self.assertEqual(body["pending"], [])

        code, created = self._create()
        self.assertEqual(code, 201, created)
        code, body = self._call("/api/board/state")
        self.assertEqual(code, 200, body)
        self.assertEqual(len(body["pending"]), 1)
        self.assertEqual(body["pending"][0]["decision_id"], created["decision_id"])
        self.assertEqual(body["board"], self.mirror)  # mirror never rewritten

    def test_all_board_routes_require_basic_auth(self):
        cases = [
            ("GET", "/api/board/state", None),
            ("GET", "/api/board/decisions", None),
            ("POST", "/api/board/decisions",
             {"payload": {"bundle_id": self.BACKLOG, "action": "schedule", "note": "n"},
              "headers": {"X-MRRC-Decision": "1"}}),
            ("DELETE", f"/api/board/decisions/{'0' * 8}-{'0' * 4}-{'0' * 4}-"
                       f"{'0' * 4}-{'0' * 12}", None),
        ]
        for method, path, extra in cases:
            kwargs = extra or {}
            code, _ = self._call(path, method=method, auth=False, **kwargs)
            self.assertEqual(code, 401, f"{method} {path}")

    def test_create_requires_json_content_type_and_decision_header(self):
        payload = {"bundle_id": self.BACKLOG, "action": "schedule", "note": "n"}
        code, body = self._call("/api/board/decisions", method="POST", payload=payload)
        self.assertEqual(code, 400, body)
        code, body = self._call("/api/board/decisions", method="POST", payload=payload,
                                headers={"X-MRRC-Decision": "1",
                                         "Content-Type": "text/plain"})
        self.assertEqual(code, 400, body)
        code, body = self._call("/api/board/decisions", method="POST", payload=payload,
                                headers={"X-MRRC-Decision": "0"})
        self.assertEqual(code, 400, body)
        self.assertEqual(self._queue_files(), [])

    def test_create_rejects_malformed_json(self):
        code, body = self._call("/api/board/decisions", method="POST",
                                raw_body=b"{not json",
                                headers={"Content-Type": "application/json",
                                         "X-MRRC-Decision": "1"})
        self.assertEqual(code, 400, body)
        code, body = self._call("/api/board/decisions", method="POST",
                                raw_body=b"[1, 2]",
                                headers={"Content-Type": "application/json",
                                         "X-MRRC-Decision": "1"})
        self.assertEqual(code, 400, body)
        self.assertEqual(self._queue_files(), [])

    def test_create_rejects_bad_id_action_and_long_note(self):
        for bundle_id, action, note in [
                ("not-a-bundle-id", "schedule", "n"),
                ("../../etc/passwd", "schedule", "n"),
                (self.BACKLOG, "delete", "n"),
                (self.BACKLOG, "SCHEDULE", "n"),
                (self.BACKLOG, "schedule", "x" * 201),
                (self.BACKLOG, "schedule", 42)]:
            code, body = self._create(bundle_id=bundle_id, action=action, note=note)
            self.assertEqual(code, 400, f"{bundle_id} {action} {note!r}: {body}")
        self.assertEqual(self._queue_files(), [])

    def test_create_accepts_legal_transitions(self):
        cases = [
            (self.BACKLOG, "schedule"),
            (self.BACKLOG2, "reject"),
            (self.SCHEDULED, "backlog"),
            (self.REJECTED, "backlog"),
        ]
        for bundle_id, action in cases:
            code, body = self._create(bundle_id=bundle_id, action=action)
            self.assertEqual(code, 201, body)
            self.assertTrue(body["ok"], body)
            self.assertEqual(body["status"], "pending")
            self.assertTrue(body["decision_id"], body)
        self.assertEqual(len(self._queue_files()), len(cases))

    def test_create_rejects_illegal_transitions_and_unknown_cards(self):
        cases = [
            (self.SCHEDULED, "schedule"),
            (self.DONE, "schedule"),
            (self.REJECTED, "reject"),
            (self.BACKLOG, "backlog"),
            (self.DONE, "backlog"),
            (self.UNKNOWN, "schedule"),
        ]
        for bundle_id, action in cases:
            code, body = self._create(bundle_id=bundle_id, action=action)
            self.assertEqual(code, 409, f"{bundle_id} {action}: {body}")
        self.assertEqual(self._queue_files(), [])

    def test_decisions_list_is_sorted_and_shaped(self):
        created = []
        for bundle_id, action in [(self.BACKLOG, "schedule"), (self.BACKLOG2, "reject"),
                                  (self.SCHEDULED, "backlog")]:
            code, body = self._create(bundle_id=bundle_id, action=action)
            self.assertEqual(code, 201, body)
            created.append(body["decision_id"])

        code, body = self._call("/api/board/decisions")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"], body)
        decisions = body["decisions"]
        self.assertEqual(len(decisions), 3)
        self.assertEqual(
            [(d["created_at"], d["decision_id"]) for d in decisions],
            sorted((d["created_at"], d["decision_id"]) for d in decisions))
        for item in decisions:
            self.assertEqual(
                set(item), {"decision_id", "bundle_id", "action", "note", "created_at"})
        self.assertEqual(set(d["decision_id"] for d in decisions), set(created))

    def test_decision_file_matches_the_contract(self):
        code, body = self._create(note="确认要做")
        self.assertEqual(code, 201, body)
        files = self._queue_files()
        self.assertEqual(len(files), 1)
        item = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(item["decision_id"], body["decision_id"])
        self.assertEqual(item["bundle_id"], self.BACKLOG)
        self.assertEqual(item["action"], "schedule")
        self.assertEqual(item["note"], "确认要做")
        self.assertTrue(item["created_at"], item)

    def test_queue_survives_a_restart(self):
        code, body = self._create()
        self.assertEqual(code, 201, body)
        decision_id = body["decision_id"]

        from http.server import ThreadingHTTPServer
        httpd2 = ThreadingHTTPServer(("127.0.0.1", 0), receiver.Handler)
        thread2 = threading.Thread(target=httpd2.serve_forever, daemon=True)
        thread2.start()
        self.addCleanup(self._stop, httpd2)
        base2 = f"http://127.0.0.1:{httpd2.server_address[1]}"

        token = base64.b64encode(b"mrrc:s3cret").decode("ascii")
        request = urllib.request.Request(
            base2 + "/api/board/decisions",
            headers={"Authorization": f"Basic {token}"})
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual([d["decision_id"] for d in payload["decisions"]], [decision_id])
        self.assertTrue((self.dir / "board-decisions" / f"{decision_id}.json").is_file())

    def test_delete_removes_a_pending_decision(self):
        code, created = self._create()
        self.assertEqual(code, 201, created)
        decision_id = created["decision_id"]
        code, body = self._call(f"/api/board/decisions/{decision_id}", method="DELETE")
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(self._queue_files(), [])
        code, body = self._call("/api/board/decisions")
        self.assertEqual(body["decisions"], [])

    def test_delete_rejects_invalid_and_unknown_ids(self):
        code, body = self._call("/api/board/decisions/not-a-uuid", method="DELETE")
        self.assertEqual(code, 400, body)
        code, body = self._call(
            f"/api/board/decisions/{'0' * 8}-{'0' * 4}-{'0' * 4}-{'0' * 4}-{'0' * 12}",
            method="DELETE")
        self.assertEqual(code, 404, body)
        self.assertEqual(self._queue_files(), [])

    def test_missing_or_invalid_mirror_is_503(self):
        (self.dir / "board_state.json").unlink()
        code, body = self._call("/api/board/state")
        self.assertEqual(code, 503, body)
        self.assertEqual(body.get("reason"), "mirror_unavailable")
        code, body = self._create()
        self.assertEqual(code, 503, body)
        self.assertEqual(self._queue_files(), [])

        self._write_mirror([])  # valid JSON, wrong shape
        code, body = self._call("/api/board/state")
        self.assertEqual(code, 503, body)
        (self.dir / "board_state.json").write_text("{not json", encoding="utf-8")
        code, body = self._call("/api/board/state")
        self.assertEqual(code, 503, body)

    def test_listing_ignores_temp_and_non_uuid_files(self):
        code, created = self._create()
        self.assertEqual(code, 201, created)
        folder = self.dir / "board-decisions"
        (folder / "junk.tmp").write_text("{}", encoding="utf-8")
        (folder / "not-a-uuid.json").write_text("{}", encoding="utf-8")
        code, body = self._call("/api/board/decisions")
        self.assertEqual(code, 200, body)
        self.assertEqual([d["decision_id"] for d in body["decisions"]],
                         [created["decision_id"]])


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
