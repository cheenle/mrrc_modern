"""Vendored receiver + deployment invariants (spec 2026-09-17 §9).

Structural checks only: the deployment itself is verified by the script's own
probes (systemd is-active, 401 without password) and by the acceptance run.
"""
import base64
import importlib.util
import inspect
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
RECEIVER_PATH = ROOT / "tools" / "support_receiver" / "server.py"
RECEIVER_DIR = RECEIVER_PATH.parent

# Loaded by path, not as `import server`: the vendored file keeps the sibling
# project's name, which would collide with this repo's own server.py for every
# name-based resolver (pyright resolved the app's module and reported its
# attributes missing).  tests/test_release_artifacts.py uses the same loader.
# server.py imports its sibling board_admin.py, so the receiver directory must
# be importable before exec_module — same as `python tools/support_receiver/
# server.py`, where the script directory lands on sys.path[0] on its own.
if str(RECEIVER_DIR) not in sys.path:
    sys.path.insert(0, str(RECEIVER_DIR))
_spec = importlib.util.spec_from_file_location("support_receiver", RECEIVER_PATH)
assert _spec is not None and _spec.loader is not None
receiver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(receiver)

# DOM-behavior harness for the operations page JS: a minimal same-origin DOM
# stub (no jsdom dependency) executed by `node`, proving that typed note
# drafts survive timer refreshes and sibling submissions, that a successful
# submit clears only its own draft, that the 409 error path keeps the note,
# and that a bare /board path redirects to the trailing-slash form.
NODE_DOM_HARNESS = r"""
"use strict";
const fs = require("fs");

const jsPath = process.argv[2];
const scenario = process.argv[3];
const startPath = process.argv[4];
const source = fs.readFileSync(jsPath, "utf-8");

function buildDom(startPathname) {
  const roots = {};
  const registry = [];
  function matches(node, token) {
    if (token.charAt(0) === ".") {
      return (node.className || "").split(/\s+/).indexOf(token.slice(1)) >= 0;
    }
    return node.tagName === token.toUpperCase();
  }
  function queryAll(root, selector) {
    const tokens = selector.trim().split(/\s+/);
    const out = [];
    (function walk(node) {
      const kids = node.children || [];
      for (let i = 0; i < kids.length; i++) {
        const kid = kids[i];
        if (!kid.tagName) { continue; }
        let ok = matches(kid, tokens[tokens.length - 1]);
        if (ok && tokens.length > 1) {
          let ancestor = kid.parent;
          for (let t = tokens.length - 2; t >= 0; t--) {
            while (ancestor && !(ancestor.tagName && matches(ancestor, tokens[t]))) {
              ancestor = ancestor.parent;
            }
            if (!ancestor) { ok = false; break; }
            ancestor = ancestor.parent;
          }
        }
        if (ok) { out.push(kid); }
        walk(kid);
      }
    })(root);
    return out;
  }
  function makeEl(tag) {
    const el = {
      tagName: String(tag).toUpperCase(),
      children: [], parent: null,
      className: "", id: "", value: "", hidden: false, disabled: false,
      type: "", maxLength: 0, placeholder: "", _text: "", listeners: {},
    };
    Object.defineProperty(el, "textContent", {
      get() { return el._text; },
      set(v) { el._text = String(v); el.children = []; },
    });
    Object.defineProperty(el, "firstChild", {
      get() { return el.children.length ? el.children[0] : null; },
    });
    el.appendChild = function (child) {
      child.parent = el;
      el.children.push(child);
      return child;
    };
    el.removeChild = function (child) {
      const idx = el.children.indexOf(child);
      if (idx >= 0) { el.children.splice(idx, 1); }
      child.parent = null;
      return child;
    };
    el.addEventListener = function (ev, fn) {
      el.listeners[ev] = el.listeners[ev] || [];
      el.listeners[ev].push(fn);
    };
    el.closest = function (selector) {
      let node = el;
      while (node) {
        if (node.tagName && matches(node, selector)) { return node; }
        node = node.parent;
      }
      return null;
    };
    el.querySelectorAll = function (selector) { return queryAll(el, selector); };
    registry.push(el);
    return el;
  }
  ["status", "error", "board"].forEach(function (id) {
    const root = makeEl("div");
    root.id = id;
    roots[id] = root;
  });
  const location = {
    pathname: startPathname, search: "", hash: "",
    replace(path) { location.replacedWith = path; },
  };
  const documentStub = {
    getElementById(id) { return roots[id] || null; },
    createElement(tag) { return makeEl(tag); },
    createTextNode(text) {
      return { tagName: "", text: String(text), children: [], parent: null };
    },
    querySelectorAll(selector) {
      return queryAll({ children: [roots.status, roots.error, roots.board],
                        parent: null }, selector);
    },
  };
  function attached(node) {
    while (node) {
      if (node === roots.status || node === roots.error || node === roots.board) {
        return true;
      }
      node = node.parent;
    }
    return false;
  }
  return { roots: roots, registry: registry, location: location,
           document: documentStub, attached: attached };
}

function fail(message) {
  console.error("FAIL: " + message);
  process.exit(1);
}

async function main() {
  const dom = buildDom(startPath);
  const state = {
    A: { status: "backlog", kind: "feature", title: "卡片 A",
         updated: "2026-11-01 10:00" },
    B: { status: "backlog", kind: "bug", title: "卡片 B",
         updated: "2026-11-01 10:01" },
  };
  let fetchImpl = async function (path, init) {
    if (init && init.method === "POST") {
      return { ok: true, status: 201,
               json: async function () {
                 return { ok: true, decision_id: "d1", status: "pending" };
               } };
    }
    return { ok: true, status: 200,
             json: async function () {
               return { ok: true, board: state, pending: [] };
             } };
  };
  const fetch = function () { return fetchImpl.apply(null, arguments); };
  const setInterval = function () { return 0; };
  const location = dom.location;
  const document = dom.document;
  let hooks = null;
  const __hooks = function (h) { hooks = h; };
  eval(source + "\n;__hooks({ refresh: refresh, render: render, " +
       "onDecide: onDecide, noteDrafts: noteDrafts });");
  if (scenario === "redirect") {
    if (dom.location.replacedWith !== startPath + "/") {
      fail("redirect expected " + startPath + "/ got " + dom.location.replacedWith);
    }
    console.log("PASS redirect-to-slash");
    return;
  }
  if (dom.location.replacedWith) {
    fail("unexpected redirect on the canonical slash path");
  }
  const flush = function () {
    return new Promise(function (r) { setImmediate(r); });
  };
  await flush(); await flush();
  const inputOf = function (id) {
    let found = null;
    dom.registry.forEach(function (el) {
      if (el.tagName !== "INPUT") { return; }
      if ((el.className || "").split(/\s+/).indexOf("note-input") < 0) { return; }
      const card = el.closest(".card");
      if (card && card.id === id && dom.attached(el)) { found = el; }
    });
    return found;
  };
  const inA = inputOf("A");
  const inB = inputOf("B");
  if (!inA || !inB) { fail("note inputs missing after initial render"); }
  inA.value = "草稿 A";
  inB.value = "草稿 B";
  // 15 秒定时刷新：标题变化证明整树重建，两张卡的草稿都必须还在
  state.A.title = "卡片 A（改）";
  await hooks.refresh();
  await flush();
  const inA2 = inputOf("A");
  const inB2 = inputOf("B");
  if (!inA2 || inA2 === inA) { fail("timer refresh did not rebuild the board"); }
  if (inA2.value !== "草稿 A" || inB2.value !== "草稿 B") {
    fail("timer refresh wiped drafts: " + inA2.value + " / " + inB2.value);
  }
  console.log("PASS timer-refresh preserves drafts");
  // A 卡提交成功：只清 A 的草稿，B 的草稿保留
  fetchImpl = async function (path, init) {
    if (init && init.method === "POST") {
      return { ok: true, status: 201,
               json: async function () {
                 return { ok: true, decision_id: "d1", status: "pending" };
               } };
    }
    return { ok: true, status: 200,
             json: async function () {
               return { ok: true, board: state,
                        pending: [{ bundle_id: "A", decision_id: "d1" }] };
             } };
  };
  await hooks.onDecide("A", "schedule", inA2);
  await flush(); await flush();
  const inA3 = inputOf("A");
  const inB3 = inputOf("B");
  if (inA3.value !== "") { fail("submitted card draft not cleared: " + inA3.value); }
  if (inB3.value !== "草稿 B") {
    fail("sibling submit wiped the other draft: " + inB3.value);
  }
  if (hooks.noteDrafts.has("A")) { fail("draft map kept the submitted card"); }
  if (hooks.noteDrafts.get("B") !== "草稿 B") {
    fail("draft map lost the sibling card");
  }
  console.log("PASS sibling submit keeps other drafts");
  // 错误路径：409 不重建 DOM，备注保留，之后的定时刷新也不丢
  fetchImpl = async function (path, init) {
    if (init && init.method === "POST") {
      return { ok: false, status: 409,
               json: async function () { return { ok: false, reason: "conflict" }; } };
    }
    return { ok: true, status: 200,
             json: async function () {
               return { ok: true, board: state,
                        pending: [{ bundle_id: "A", decision_id: "d1" }] };
             } };
  };
  await hooks.onDecide("B", "reject", inB3);
  await flush();
  if (inputOf("B").value !== "草稿 B") { fail("error path lost the live note"); }
  if (!dom.roots.error.textContent) { fail("error area empty on 409"); }
  await hooks.refresh();
  await flush();
  if (inputOf("B").value !== "草稿 B") {
    fail("draft lost after error + later refresh");
  }
  console.log("PASS error preserves note across later refresh");
  console.log("ALL PASS");
}

main().catch(function (err) {
  console.error("HARNESS ERROR: " + (err && err.stack || err));
  process.exit(1);
});
"""


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


class _BoardServerFixture:
    """Shared fixture for the board HTTP tests: temp SUPPORT_DIR, patched
    auth, a written mirror, and a live ThreadingHTTPServer.  A plain mixin
    (not a TestCase) so each TestCase subclass collects only its own cases —
    subclassing BoardDecisionHttpTests would silently re-run its whole suite
    inside the page tests."""

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


class BoardDecisionHttpTests(_BoardServerFixture, unittest.TestCase):
    """Authenticated board-decision queue backed by files under SUPPORT_DIR
    (spec docs/superpowers/specs/2026-11-01-support-board-web-decisions-design.md)."""

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

    @staticmethod
    def _read_response(sock):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        headers, _, body = data.partition(b"\r\n\r\n")
        length = 0
        for line in headers.split(b"\r\n"):
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                try:
                    length = int(value.strip())
                except ValueError:
                    length = 0
        while len(body) < length:
            chunk = sock.recv(4096)
            if not chunk:
                break
            body += chunk
        return int(headers.split(b" ", 2)[1]), headers, body

    def test_negative_content_length_is_rejected(self):
        """Content-Length: -1 once slipped past the size check and made
        rfile.read(-1) read unboundedly, accepting an oversized body (201)."""
        payload = json.dumps({"bundle_id": self.BACKLOG, "action": "schedule",
                              "note": "n"}).encode("utf-8")
        token = base64.b64encode(b"mrrc:s3cret").decode("ascii")
        host, port = self.httpd.server_address
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.settimeout(5)
            sock.sendall(
                b"POST /api/board/decisions HTTP/1.1\r\n"
                + f"Host: {host}:{port}\r\n".encode("ascii")
                + f"Authorization: Basic {token}\r\n".encode("ascii")
                + b"Content-Type: application/json\r\n"
                + b"X-MRRC-Decision: 1\r\n"
                + b"Content-Length: -1\r\n"
                + b"\r\n" + payload)
            sock.shutdown(socket.SHUT_WR)  # 让服务端的无界 read(-1) 读到 EOF
            status, _, _ = self._read_response(sock)
        self.assertEqual(status, 413)
        self.assertEqual(self._queue_files(), [])

    def test_corrupt_queue_file_returns_500_on_both_list_endpoints(self):
        folder = self.dir / "board-decisions"
        folder.mkdir()
        (folder / "00000000-0000-0000-0000-000000000000.json").write_text(
            "{not json", encoding="utf-8")
        for path in ("/api/board/decisions", "/api/board/state"):
            code, body = self._call(path)
            self.assertEqual(code, 500, f"{path}: {body}")
            self.assertEqual(body.get("reason"), "queue_error", body)

    def test_queue_write_failure_returns_500_without_creating_an_item(self):
        with mock.patch.object(receiver.os, "replace",
                               side_effect=OSError(5, "I/O error")):
            code, body = self._create()
        self.assertEqual(code, 500, body)
        self.assertFalse(body.get("ok"), body)
        self.assertEqual(body.get("reason"), "queue_error", body)
        self.assertEqual(self._queue_files(), [])

    def test_queue_delete_failure_returns_500_and_keeps_the_item(self):
        code, created = self._create()
        self.assertEqual(code, 201, created)
        decision_id = created["decision_id"]
        with mock.patch.object(receiver.os, "remove",
                               side_effect=OSError(5, "I/O error")):
            code, body = self._call(f"/api/board/decisions/{decision_id}",
                                    method="DELETE")
        self.assertEqual(code, 500, body)
        self.assertFalse(body.get("ok"), body)
        self.assertTrue(
            (self.dir / "board-decisions" / f"{decision_id}.json").is_file())


class BoardAdminPageTests(_BoardServerFixture, unittest.TestCase):
    """GET /board/ serves the authenticated operations page
    (spec docs/superpowers/specs/2026-11-01-support-board-web-decisions-design.md)."""

    def _get(self, path, auth=True):
        req_headers = {}
        if auth:
            token = base64.b64encode(b"mrrc:s3cret").decode("ascii")
            req_headers["Authorization"] = f"Basic {token}"
        request = urllib.request.Request(self.base + path, headers=req_headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, response.headers, \
                    response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read().decode("utf-8")

    def test_board_page_requires_basic_auth(self):
        code, _, _ = self._get("/board/", auth=False)
        self.assertEqual(code, 401)

    def test_board_page_served_as_html_with_relative_api_paths(self):
        code, headers, html = self._get("/board/")
        self.assertEqual(code, 200, html[:200])
        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
        # 相对路径：公开入口 /mrrc_modern/support/board/ 必须把 API 解析到
        # /mrrc_modern/support/api/...，绝不能逃到站点根 /api/...。
        self.assertIn("../api/board/state", html)
        self.assertIn("../api/board/decisions", html)
        self.assertNotIn('"/api/board/', html)

    def test_board_page_js_uses_safe_dom_and_submission_guards(self):
        _, _, html = self._get("/board/")
        self.assertIn("textContent", html)      # 服务端字符串只经 textContent 进 DOM
        self.assertIn("createElement", html)
        self.assertIn("X-MRRC-Decision", html)
        self.assertIn("application/json", html)
        self.assertIn("same-origin", html)
        self.assertIn("15000", html)            # 15 秒轮询
        for state in ("backlog", "scheduled", "in_progress", "answered",
                      "done", "failed", "rejected"):
            self.assertIn(state, html)
        for action in ("排期", "不处理", "退回待决策", "已提交，等待 Mac 应用"):
            self.assertIn(action, html)
        # 页面不得把服务端数据拼进 innerHTML
        self.assertNotIn("innerHTML", html)

    def test_board_admin_module_renders_without_io(self):
        """渲染器纯函数：不读文件、不联网，任何环境都能出整页 HTML。"""
        import board_admin
        html = board_admin.render_board_admin()
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("../api/board/state", html)

    def test_board_without_trailing_slash_still_serves_the_page(self):
        """客户端会把裸 /board 重定向到尾部斜杠形式；服务端对两种路径
        都返回页面本身（重定向是浏览器内的同源跳转，不破坏旧入口）。"""
        code, headers, html = self._get("/board")
        self.assertEqual(code, 200, html[:200])
        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
        self.assertIn("../api/board/state", html)

    def test_board_page_has_trailing_slash_guard_and_draft_mechanism(self):
        _, _, html = self._get("/board/")
        self.assertIn('location.pathname.endsWith("/")', html)
        self.assertIn("location.replace", html)
        self.assertIn("noteDrafts", html)
        self.assertIn("snapshotDrafts", html)

    @unittest.skipUnless(shutil.which("node"), "node is required for the DOM harness")
    def test_board_js_note_drafts_and_trailing_slash_redirect(self):
        """Node DOM harness: drafts survive timer refresh and sibling
        submits; success clears only the submitted card; 409 keeps the note;
        bare /board redirects to the trailing-slash form."""
        _, _, html = self._get("/board/")
        script = re.search(r"<script>\n(.*?)\n</script>", html, re.S).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            harness = Path(tmp) / "board_harness.js"
            js = Path(tmp) / "board_page.js"
            harness.write_text(NODE_DOM_HARNESS, encoding="utf-8")
            js.write_text(script, encoding="utf-8")
            for extra, name in [
                    (["redirect", "/mrrc_modern/support/board"], "redirect"),
                    (["notes", "/mrrc_modern/support/board/"], "notes")]:
                result = subprocess.run(
                    ["node", str(harness), str(js), *extra],
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(
                    result.returncode, 0,
                    f"{name}:\n{result.stdout}\n{result.stderr}")


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

    def test_board_admin_deployed_beside_server_as_0644_and_staging_removed(self):
        self.assertIn("tools/support_receiver/board_admin.py", self.script)
        self.assertIn("/opt/mrrc-modern-support/board_admin.py", self.script)
        self.assertIn("install -m 644 /tmp/support-modern-board-admin.py "
                      "/opt/mrrc-modern-support/board_admin.py", self.script)
        self.assertIn("rm -f /tmp/support-modern-board-admin.py", self.script)

    def test_public_probes_cover_board_page_and_state_expecting_401(self):
        self.assertIn("https://www.vlsc.net/mrrc_modern/support/board/ "
                      "-> HTTP %{http_code} (want 401)", self.script)
        self.assertIn("https://www.vlsc.net/mrrc_modern/support/api/board/state "
                      "-> HTTP %{http_code} (want 401)", self.script)


if __name__ == "__main__":
    unittest.main()
