# Support Board Web Decisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an authenticated web operations page where the maintainer can schedule, reject, or return FDE board cards to backlog, with durable server-side queueing and Mac-side application/publishing.

**Architecture:** The existing Support receiver stores validated decisions and serves the authenticated operations UI. The Mac remains the board-state authority: `support_autopilot` polls each minute, applies legal transitions through `board.decide()`, republishes the public page plus a private state mirror, then acknowledges queue entries.

**Tech Stack:** Python 3 standard library, `http.server`, `urllib.request`, vanilla HTML/JavaScript, `unittest`, existing bash deployment scripts and cron.

**Approved design:** `docs/superpowers/specs/2026-11-01-support-board-web-decisions-design.md` (commit `84be4325`). After plan approval, save this execution plan as `docs/superpowers/plans/2026-11-01-support-board-web-decisions.md`.

---

## File map and working-tree guard

- Modify `tools/support_receiver/server.py`: authenticated board state/decision APIs and durable queue.
- Create `tools/support_receiver/board_admin.py`: dependency-free operations-page renderer; keeps UI out of the HTTP transport module.
- Modify `tools/support_receiver/support-receiver-modern.service`: no new write path is needed because the queue stays under `/var/www/support-modern`; verify this rather than changing it.
- Modify `deploy_support_receiver.sh`: deploy `board_admin.py`, probe the new authenticated routes.
- Modify `support_board.py`: add only the public page's `../support/board/` operations link.
- Modify `dev_tools/support_autopilot.py`: mirror shipping, decision pull/ack/apply, `--sync-decisions`, and the second cron entry.
- Modify `tests/test_support_receiver.py`, `tests/test_support_autopilot.py`, `tests/test_support_board.py`: TDD coverage.
- Modify `AGENTS.md`, `docs/FDE.md`, `README.md`, `tests/README.md`, `SDD/10-service-model.md`, `SDD/14-version-history.md`: behavior and operations documentation.
- `website/board/index.html` is already dirty with newer generated real-board content. Do not check it out or discard it. Regenerate it from the real local board state only after the renderer changes, and review the diff before staging.

## Task 1: Establish the red baseline

**Files:** none

- [ ] **Step 1: Run the focused existing suites**

```bash
python3 -m unittest tests.test_support_receiver tests.test_support_board tests.test_support_autopilot -v
```

Expected: all existing cases pass. If anything fails, record the failure and fix the baseline issue before adding feature code; do not label a pre-existing red test as caused by this feature.

- [ ] **Step 2: Run the repository engineering brief**

```bash
python3 .agents/skills/sdd-guardian/harness/sdd_context.py brief \
  tools/support_receiver/server.py tools/support_receiver/board_admin.py \
  dev_tools/support_autopilot.py support_board.py deploy_support_receiver.sh
```

Expected: constraints are printed; keep Basic Auth, secret handling, public-page redaction, atomic state writes, and single-flight behavior intact.

## Task 2: Durable decision queue and authenticated APIs

**Files:**
- Modify: `tools/support_receiver/server.py`
- Test: `tests/test_support_receiver.py`

- [ ] **Step 1: Add failing HTTP tests**

Extend `ReceiverHttpTests.setUp()` with a private mirror containing one card in each relevant state:

```python
mirror = {
    "20261101-120000-ab12": {"status": "backlog", "kind": "feature", "title": "测试功能"},
    "20261101-120001-ab12": {"status": "scheduled", "kind": "bug", "title": "已排期"},
    "20261101-120002-ab12": {"status": "rejected", "kind": "feature", "title": "已拒绝"},
    "20261101-120003-ab12": {"status": "done", "kind": "bug", "title": "已完成"},
}
(self.dir / "board_state.json").write_text(json.dumps(mirror), encoding="utf-8")
```

Add an authenticated request helper and tests with these exact assertions:

```python
def _auth(self):
    import base64
    return {"Authorization": "Basic " + base64.b64encode(b"mrrc:s3cret").decode(),
            "Content-Type": "application/json", "X-MRRC-Decision": "1"}
```

- unauthenticated `GET /board/`, `GET /api/board/state`, and `POST /api/board/decisions` return 401;
- missing `X-MRRC-Decision` returns 400 and creates no file;
- invalid id/action and notes longer than 200 characters return 400;
- scheduling a `done` card returns 409;
- `schedule` from `backlog` returns 201 and one queue file containing `decision_id`, `bundle_id`, `action`, `note`, `created_at`;
- `backlog` from `scheduled` and `rejected` is legal; `backlog` from `backlog` returns 409;
- `GET /api/board/decisions` returns the persisted item in creation order;
- `DELETE /api/board/decisions/<uuid>` removes exactly that item; an invalid UUID returns 400.

Run:

```bash
python3 -m unittest tests.test_support_receiver.ReceiverHttpTests -v
```

Expected: new cases fail with 404 because the routes do not exist.

- [ ] **Step 2: Implement queue primitives**

Add `uuid` import and these module constants:

```python
DECISION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DECISION_ACTIONS = {"schedule", "reject", "backlog"}
DECISION_QUEUE_NAME = "board-decisions"
BOARD_MIRROR_NAME = "board_state.json"
```

Implement standard-library-only helpers with these contracts:

```python
def load_board_mirror():
    with open(os.path.join(DIR, BOARD_MIRROR_NAME), encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise ValueError("board mirror must be an object")
    return value

def decision_allowed(status, action):
    if action in ("schedule", "reject"):
        return status == "backlog"
    if action == "backlog":
        return status in ("scheduled", "rejected")
    return False

def decision_path(decision_id):
    if not DECISION_ID_RE.fullmatch(decision_id):
        raise ValueError("bad decision id")
    return os.path.join(DIR, DECISION_QUEUE_NAME, decision_id + ".json")

def list_decisions():
    queue = os.path.join(DIR, DECISION_QUEUE_NAME)
    items = []
    for name in os.listdir(queue) if os.path.isdir(queue) else []:
        if not name.endswith(".json"):
            continue
        decision_id = name[:-5]
        if not DECISION_ID_RE.fullmatch(decision_id):
            continue
        with open(decision_path(decision_id), encoding="utf-8") as fh:
            item = json.load(fh)
        items.append(item)
    return sorted(items, key=lambda item: (item["created_at"], item["decision_id"]))
```

Create decisions with `uuid.uuid4()`, UTC timestamps, and atomic `tmp` + `os.replace()` writes. Validate the complete request before creating the queue directory. Never concatenate an unvalidated id into a path.

- [ ] **Step 3: Wire authenticated routes**

In `do_GET`, retain authentication first, then route `/api/board/state`, `/api/board/decisions`, and `/board/`. In `do_POST`, handle `/api/board/decisions` before `/api/create`; require both JSON content type and `X-MRRC-Decision: 1`. In `do_DELETE`, route `/api/board/decisions/<uuid>` separately from bundle deletion.

`GET /api/board/state` returns:

```python
self._json(200, {"ok": True, "board": load_board_mirror(),
                 "pending": list_decisions()})
```

A missing mirror returns 503 with `{"ok": False, "reason": "mirror_unavailable"}`. Successful creation returns 201. Queue read/write errors return 500 and are not presented as successful decisions.

- [ ] **Step 4: Run the receiver suite**

```bash
python3 -m unittest tests.test_support_receiver -v
```

Expected: all receiver tests pass, including existing upload/list/delete tests.

- [ ] **Step 5: Commit this vertical slice**

```bash
git add tools/support_receiver/server.py tests/test_support_receiver.py
git commit -m "feat(support): persist authenticated board decisions"
```

## Task 3: Authenticated operations UI and public entry

**Files:**
- Create: `tools/support_receiver/board_admin.py`
- Modify: `tools/support_receiver/server.py`
- Modify: `support_board.py`
- Modify: `deploy_support_receiver.sh`
- Test: `tests/test_support_receiver.py`
- Test: `tests/test_support_board.py`

- [ ] **Step 1: Add failing renderer and public-link tests**

Add receiver tests asserting `GET /board/` with auth returns 200, includes `../api/board/state`, `../api/board/decisions`, `X-MRRC-Decision`, `textContent`, and a 15-second refresh. Add to `RenderBoardTests`:

```python
def test_public_board_links_to_authenticated_operations(self):
    html = board.render_board({})
    self.assertIn('href="../support/board/"', html)
    self.assertIn("运营决策", html)
    self.assertNotIn("<form", html)
```

Run both test modules; expected failures are 404 for `/board/` and missing public link.

- [ ] **Step 2: Implement the operations-page renderer**

Create `board_admin.py` with `render_board_admin() -> str`. Return a complete HTML document with responsive columns matching the public board's seven states. Its JavaScript must:

```javascript
async function api(path, options = {}) {
  const response = await fetch(path, {credentials: "same-origin", ...options});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.reason || `HTTP ${response.status}`);
  return body;
}

async function decide(bundleId, action, note) {
  return api("../api/board/decisions", {
    method: "POST",
    headers: {"Content-Type": "application/json", "X-MRRC-Decision": "1"},
    body: JSON.stringify({bundle_id: bundleId, action, note})
  });
}
```

Build every card with `document.createElement()` and server text with `textContent`. Show `schedule`/`reject` only for `backlog`, `backlog` only for `scheduled`/`rejected`, and disable all actions for a card whose id appears in `pending`. Display `已提交，等待 Mac 应用` after a 201, preserve note input on errors, and refresh state after submission and every 15 seconds. Use relative API paths `../api/...`: from public `/mrrc_modern/support/board/` they resolve to `/mrrc_modern/support/api/...`, which nginx maps to receiver `/api/...`. Absolute `/api/...` would incorrectly escape the Support prefix.

- [ ] **Step 3: Serve and deploy the renderer**

Import `render_board_admin` in `server.py` and return it from authenticated `GET /board/` with `text/html; charset=utf-8`. In `deploy_support_receiver.sh`, rsync `board_admin.py` beside `server.py`, install it as `/opt/mrrc-modern-support/board_admin.py` mode 0644, and remove the staging copy. Add unauthenticated probes expecting 401 for `/mrrc_modern/support/board/` and `/mrrc_modern/support/api/board/state`.

- [ ] **Step 4: Add the public operations link**

In `support_board.render_board()`, add `<a href="../support/board/">运营决策</a>` beside the answers link. Do not add forms or decision buttons to the public static page.

- [ ] **Step 5: Test and commit**

```bash
python3 -m unittest tests.test_support_receiver tests.test_support_board -v
python3 - <<'PY'
import importlib.util
import re
from pathlib import Path
path = Path("tools/support_receiver/board_admin.py")
spec = importlib.util.spec_from_file_location("board_admin", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
script = re.search(r"<script>(.*?)</script>", module.render_board_admin(), re.S).group(1)
Path("/tmp/mrrc-board-admin.js").write_text(script, encoding="utf-8")
PY
node --check /tmp/mrrc-board-admin.js
rm /tmp/mrrc-board-admin.js
```

Expected: Python suites pass and Node reports no JavaScript syntax error.

```bash
git add tools/support_receiver/board_admin.py tools/support_receiver/server.py \
  support_board.py deploy_support_receiver.sh \
  tests/test_support_receiver.py tests/test_support_board.py
git commit -m "feat(board): add authenticated web operations page"
```

## Task 4: Publish the private board-state mirror

**Files:**
- Modify: `dev_tools/support_autopilot.py`
- Test: `tests/test_support_autopilot.py`

- [ ] **Step 1: Add failing mirror-deployment tests**

Patch `_ship_page` and call the new `publish_board_page()` with a temporary `BOARD_STATE_FILE`. Assert one call ships `BOARD_PAGE_PATH` to `REMOTE_BOARD_PAGE`, and another ships `BOARD_STATE_FILE` to `/var/www/support-modern/board_state.json`. Add cases for `(page=False, mirror=True)`, `(True, False)`, and `(False, False)`: `publish_board_page()` must respectively return False, False, False and must not acknowledge any decision in Task 5.

Run:

```bash
python3 -m unittest tests.test_support_autopilot.PageDeployTests -v
```

Expected: failure because `publish_board_page` does not exist.

- [ ] **Step 2: Implement mirror shipping without breaking existing callers**

Add:

```python
BOARD_STATE_STAGING = "~/mrrc_modern_board_state.json"
REMOTE_BOARD_STATE = "/var/www/support-modern/board_state.json"
```

Implement:

```python
def publish_board_page(generated_at=""):
    bstate = board.load_state(BOARD_STATE_FILE)
    BOARD_PAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BOARD_PAGE_PATH.write_text(board.render_board(bstate, generated_at), encoding="utf-8")
    page_ok = _ship_page(BOARD_PAGE_PATH, BOARD_STAGING, REMOTE_BOARD_PAGE, "看板页")
    mirror_ok = _ship_page(BOARD_STATE_FILE, BOARD_STATE_STAGING,
                           REMOTE_BOARD_STATE, "看板状态镜像")
    return page_ok and mirror_ok

def rebuild_board_page(generated_at=""):
    publish_board_page(generated_at)
    return BOARD_PAGE_PATH
```

This preserves every existing `rebuild_board_page()` caller while exposing the success value required for queue acknowledgement.

- [ ] **Step 3: Test and commit**

```bash
python3 -m unittest tests.test_support_autopilot.PageDeployTests -v
git add dev_tools/support_autopilot.py tests/test_support_autopilot.py
git commit -m "feat(board): ship private state mirror for web operations"
```

## Task 5: Apply decisions on the Mac with retry and stale-request protection

**Files:**
- Modify: `dev_tools/support_autopilot.py`
- Test: `tests/test_support_autopilot.py`

- [ ] **Step 1: Add failing synchronization tests**

Create `DecisionSyncTests(AutopilotFixture)` with a UUID constant and a helper that writes local board state. Mock `list_board_decisions`, `ack_board_decision`, and `publish_board_page`. Cover:

```python
def test_schedule_is_applied_published_then_acknowledged(self):
    board.save_state(autopilot.BOARD_STATE_FILE, {
        "20261101-120000-ab12": {"status": "backlog", "kind": "feature"}})
    decision = {"decision_id": self.decision_id,
                "bundle_id": "20261101-120000-ab12", "action": "schedule",
                "note": "网页排期", "created_at": "2026-11-01T12:00:00Z"}
    with mock.patch.object(autopilot, "list_board_decisions", return_value=[decision]), \
         mock.patch.object(autopilot, "publish_board_page", return_value=True) as publish, \
         mock.patch.object(autopilot, "ack_board_decision") as ack:
        result = autopilot.sync_board_decisions()
    self.assertEqual(board.load_state(autopilot.BOARD_STATE_FILE)
                     ["20261101-120000-ab12"]["status"], "scheduled")
    publish.assert_called_once_with()
    ack.assert_called_once_with(self.decision_id)
    self.assertEqual(result, [(self.decision_id, "applied")])
```

Also test all legal transitions, illegal/stale transitions acknowledged as `stale` without changing state, unknown/malformed entries acknowledged as `invalid`, publish failure returning `pending` without acknowledgement, one bad item not blocking later items, and acknowledgement failure leaving the applied item eligible for harmless replay.

Run the new class; expected failure because sync functions do not exist.

- [ ] **Step 2: Generalize authenticated HTTP and validate queue responses**

Refactor `http_get` through a method-aware helper without changing its existing behavior:

```python
def http_request(path, method="GET"):
    request = urllib.request.Request(f"{RECEIVER_URL}{path}",
                                    headers={"Authorization": _auth_header()}, method=method)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()

def http_get(path):
    return http_request(path)
```

`list_board_decisions()` parses one JSON object, requires `ok is True` and `decisions` to be a list, and validates the envelope plus each item's UUID and field types before returning any item. A malformed envelope or invalid UUID raises `ValueError` and prevents partial execution because there is no safe id to acknowledge. Semantically invalid bundle ids/actions/notes remain per-item `invalid` results and are acknowledged by `sync_board_decisions()` so they cannot block the queue. `ack_board_decision()` sends DELETE to `/api/board/decisions/<uuid>`.

- [ ] **Step 3: Implement the state transition and acknowledgement order**

Use the same `decision_allowed()` rule as the receiver, kept in `support_board.py` as the shared Mac-side rule:

```python
def decision_allowed(status, action):
    if action in ("schedule", "reject"):
        return status == "backlog"
    if action == "backlog":
        return status in ("scheduled", "rejected")
    return False
```

For each validated item, reload state. Apply legal decisions with `board.decide()` + `board.save_state()`. Acknowledge stale/invalid items without publishing. If any state changed, call `publish_board_page()` once; only when it returns True acknowledge those applied items. Catch per-item exceptions, log them, return `(decision_id, "pending")`, and continue with later entries. Never acknowledge before local save and successful page/mirror publication.

- [ ] **Step 4: Test and commit**

```bash
python3 -m unittest tests.test_support_autopilot.DecisionSyncTests \
  tests.test_support_board.DecideTests -v
git add dev_tools/support_autopilot.py support_board.py tests/test_support_autopilot.py \
  tests/test_support_board.py
git commit -m "feat(board): sync web decisions to the authoritative state"
```

## Task 6: CLI, single-flight lock, and minute cron

**Files:**
- Modify: `dev_tools/support_autopilot.py`
- Test: `tests/test_support_autopilot.py`

- [ ] **Step 1: Add failing CLI/cron tests**

Add tests asserting:

- `decision_cron_line()` starts with `* * * * *`, uses the absolute Python/script paths, contains `--sync-decisions` and `DECISION_CRON_MARK`, and does not contain `--implement`;
- `install_cron()` retains unrelated entries and installs exactly one of each marker;
- `uninstall_cron()` removes both markers;
- `main(["--sync-decisions"])` acquires the single-flight lock and calls sync without calling `run_once`, `run_pi`, or `run_implementations`;
- `main(["--once", "--publish"])` calls sync before polling, under the same lock.

- [ ] **Step 2: Implement the commands**

Add `DECISION_CRON_MARK = "# MRRC Modern board decisions"` and:

```python
def decision_cron_line():
    return (f"* * * * * PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:"
            f"{Path.home()}/.hermes/node/bin "
            f"{sys.executable} {REPO / 'dev_tools' / 'support_autopilot.py'} --sync-decisions "
            f">> {RUN_LOG} 2>&1 {DECISION_CRON_MARK}")
```

Filter both markers in install/uninstall. Add argparse `--sync-decisions`; its branch acquires `acquire_single_flight_lock()`, returns quietly when another instance holds it, calls `sync_board_decisions()` in `try`, and closes the lock in `finally`. In the existing `--once` branch, call sync before `run_once()` without adding a second lock.

- [ ] **Step 3: Test and commit**

```bash
python3 -m unittest tests.test_support_autopilot.CronTests \
  tests.test_support_autopilot.DecisionSyncTests -v
git add dev_tools/support_autopilot.py tests/test_support_autopilot.py
git commit -m "feat(board): sync web decisions every minute"
```

## Task 7: End-to-end verification, documentation, and deployment

**Files:**
- Modify: `AGENTS.md`, `docs/FDE.md`, `README.md`, `tests/README.md`, `SDD/10-service-model.md`, `SDD/14-version-history.md`
- Regenerate carefully: `website/board/index.html`
- Save plan: `docs/superpowers/plans/2026-11-01-support-board-web-decisions.md`

- [ ] **Step 1: Run a real local end-to-end acceptance**

Start `tools/support_receiver/server.py` against temporary `SUPPORT_DIR`/port and a temporary mirror. Submit an authenticated schedule request through real HTTP, point `support_autopilot.RECEIVER_URL` and state paths at the temporary environment, run `sync_board_decisions()`, and assert: local state becomes `scheduled`, rendered board contains the card in `scheduled`, mirror is published by the mocked ship seam, and the real queue is empty after acknowledgement. Do not use a real user card.

Expected: pending → applied → acknowledged works through actual HTTP and state transitions, not only imports.

- [ ] **Step 2: Run focused and complete gates**

```bash
python3 -m unittest tests.test_support_receiver tests.test_support_board tests.test_support_autopilot -v
python3 -m py_compile tools/support_receiver/server.py tools/support_receiver/board_admin.py \
  support_board.py dev_tools/support_autopilot.py
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
python3 -m unittest discover -s tests -v
```

Expected: all pass. Do not deploy with red tests. Re-read the final unittest total and update `tests/README.md` from that output rather than estimating it.

- [ ] **Step 3: Synchronize documentation**

Document the exact URLs and rules:

- public `/mrrc_modern/board/` remains read-only;
- authenticated `/mrrc_modern/support/board/` is the operations UI;
- Support credentials are unchanged;
- Mac is the sole state authority; server queue is not a second state machine;
- schedule/reject only from backlog; return to backlog only from scheduled/rejected;
- acknowledgement happens only after local save and successful public-page + mirror publication;
- CLI `fde_board.py decide` remains available.

Add SDD V3.07 dated 2026-11-01 to `SDD/14-version-history.md`, update the SupportService row in `SDD/10-service-model.md`, and record the new test totals. Update `docs/FDE.md` §5.4 so the operator instructions lead with the web UI and retain CLI examples.

- [ ] **Step 4: Regenerate and review the public page**

Run `python3 dev_tools/fde_board.py rebuild` only after focused tests pass. This publishes the real current board and mirror. Inspect `git diff -- website/board/index.html` and confirm the previously dirty 2026-11-01 real-board content is preserved apart from subsequent legitimate autopilot updates and the new operations link. Never use `git checkout --` on this file.

- [ ] **Step 5: Commit implementation and docs**

```bash
git add AGENTS.md docs/FDE.md README.md tests/README.md \
  SDD/10-service-model.md SDD/14-version-history.md \
  website/board/index.html docs/superpowers/plans/2026-11-01-support-board-web-decisions.md
git commit -m "docs(board): document authenticated web decisions"
```

- [ ] **Step 6: Deploy receiver and install synchronization cron**

```bash
./deploy_support_receiver.sh
python3 dev_tools/support_autopilot.py --install-cron
```

Expected: systemd service is active, new unauthenticated board/state probes return 401, and both cron markers are installed exactly once.

- [ ] **Step 7: Verify live behavior without changing real schedules**

Use the existing `support_autopilot` credential helper to make authenticated GET requests so the password is never printed or placed in a URL. Verify `/board/` and `/api/board/state` return 200 and the mirror matches the current published board. Verify unauthenticated decision POST returns 401 and an authenticated invalid id returns 400 without creating a queue item. Do not schedule/reject a real user card as a test.

Report the local full end-to-end evidence and live non-destructive probes separately. The remaining operator acceptance is one deliberate real-card decision at `https://www.vlsc.net/mrrc_modern/support/board/`; do not claim that final real-card transition was tested unless the operator chooses to perform it.
