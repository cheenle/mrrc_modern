"""Support autopilot CLI (spec 2026-09-17-support-autopilot §3/§8).

No external network, no model, no git: `pi`, git and rsync are all faked, so
these tests run anywhere.  The receiver is faked for the unit tests, EXCEPT
DecisionEndToEndTests, which serves the real vendored stdlib receiver from a
temporary SUPPORT_DIR on 127.0.0.1 loopback HTTP (plan 2026-11-01 Task 7) —
the only seam still mocked there is _ship_page, so no fixture page can reach
the live server.  Loaded by path because dev_tools is not a package.
"""
import base64
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "support_autopilot", ROOT / "dev_tools" / "support_autopilot.py")
assert _spec is not None and _spec.loader is not None
autopilot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(autopilot)

# The end-to-end decision tests run the actual receiver (not a fake): load it
# by path like tests/test_support_receiver.py does, with its directory on
# sys.path so the receiver's sibling `import board_admin` resolves.  Append
# (never insert(0)): that directory also contains a server.py, which must
# never shadow the repo-root server.py for a later name-based import.
RECEIVER_PATH = ROOT / "tools" / "support_receiver" / "server.py"
if str(RECEIVER_PATH.parent) not in sys.path:
    sys.path.append(str(RECEIVER_PATH.parent))
_spec = importlib.util.spec_from_file_location(
    "support_receiver_e2e", RECEIVER_PATH)
assert _spec is not None and _spec.loader is not None
receiver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(receiver)

ANALYSIS = {
    "verdict": "录音写入端跟不上", "status": "needs_fix", "category": "音频",
    "diagnosis": ["日志出现 Recording writer is falling behind"],
    "solution": ["先结束本次录音，再重启 MRRC Modern"],
    "evidence": ["Recording dropped 950 block(s) so far"],
    "keys": ["录音"], "needs_code_change": True, "code_hint": "server.py:_ensure_rec_writer",
}


def _listing(ids):
    return ("<html>" + "".join(f"<li><b>{i}</b></li>" for i in ids) + "</html>").encode("utf-8")


def _zip_bytes(names=("problem.txt", "diagnostics/summary.txt")):
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, "2026-09-17 09:00:00 [INFO] mrrc: Server ready!\n")
    return buffer.getvalue()


class AutopilotFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        # Capture before patching: PageDeployTests re-enables the real ship.
        self.real_ship_page = autopilot._ship_page
        self.patches = [
            mock.patch.object(autopilot, "STATE_FILE", root / "state.json"),
            mock.patch.object(autopilot, "BOARD_STATE_FILE", root / "board_state.json"),
            mock.patch.object(autopilot, "BOARD_PAGE_PATH", root / "board" / "index.html"),
            mock.patch.object(autopilot, "INBOX", root / "inbox"),
            mock.patch.object(autopilot, "DRAFTS", root / "drafts"),
            mock.patch.object(autopilot, "ANSWERS_PAGE", root / "answers" / "index.html"),
            mock.patch.object(autopilot, "RUN_LOG", root / "run.log"),
            mock.patch.object(autopilot, "LOCK_FILE", root / "autopilot.lock"),
            # 2026-09-19 leak: patching only the local paths still shipped
            # fixture pages to the live server (the rsync/ssh targets were
            # unpatched).  The ship itself is now a single patchable seam.
            mock.patch.object(autopilot, "_ship_page", return_value=True),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.root = root

    def _fake_http(self, ids):
        def fake(path):
            if path == "/api/list":
                return _listing(ids)
            if path.endswith("/bundle"):
                return _zip_bytes()
            raise AssertionError(f"unexpected path {path}")
        return fake

    def _run(self, ids, analysis=None, publish=False, force=None, limit=2):
        with mock.patch.object(autopilot, "http_get", side_effect=self._fake_http(ids)), \
             mock.patch.object(autopilot, "run_pi", return_value=analysis or ANALYSIS) as pi, \
             mock.patch.object(autopilot, "git_commit", return_value=True) as commit, \
             mock.patch.object(autopilot, "rsync_page", return_value=True) as rsync:
            if force:
                result = autopilot.process_one(force, publish)
            else:
                result = autopilot.run_once(publish, limit=limit)
        return result, pi, commit, rsync


class IdempotencyTests(AutopilotFixture):
    def test_a_bundle_is_processed_once(self):
        ids = ["20260917-072530-ab12"]
        result, pi, _, _ = self._run(ids, publish=True)
        self.assertEqual(result, [("20260917-072530-ab12", "published")])
        self.assertEqual(pi.call_count, 1)
        # second run: nothing pending
        result2, pi2, _, _ = self._run(ids, publish=True)
        self.assertEqual(result2, [])
        self.assertEqual(pi2.call_count, 0)

    def test_per_run_limit_is_respected(self):
        ids = [f"20260917-0725{i:02d}-ab12" for i in range(5)]
        result, pi, _, _ = self._run(ids, publish=True, limit=2)
        self.assertEqual(len(result), 2)
        self.assertEqual(pi.call_count, 2)

    def test_half_bundle_is_skipped_not_fatal(self):
        def fake(path):
            if path == "/api/list":
                return _listing(["20260917-072530-ab12"])
            return b"not a zip"
        with mock.patch.object(autopilot, "http_get", side_effect=fake), \
             mock.patch.object(autopilot, "run_pi") as pi:
            result = autopilot.run_once(publish_it=True)
        self.assertEqual(result, [("20260917-072530-ab12", "failed")])
        self.assertEqual(pi.call_count, 0)


class PublishGateTests(AutopilotFixture):
    def test_need_more_info_never_publishes(self):
        analysis = dict(ANALYSIS, status="need_more_info")
        result, _, commit, rsync = self._run(["20260917-072530-ab12"], analysis=analysis,
                                            publish=True)
        self.assertEqual(result, [("20260917-072530-ab12", "need_more_info")])
        self.assertEqual(commit.call_count, 0)
        self.assertEqual(rsync.call_count, 0)
        self.assertFalse(autopilot.ANSWERS_PAGE.exists())

    def test_without_publish_only_a_draft_is_written(self):
        result, _, commit, rsync = self._run(["20260917-072530-ab12"], publish=False)
        self.assertEqual(result, [("20260917-072530-ab12", "drafted")])
        self.assertEqual(commit.call_count, 0)
        self.assertEqual(rsync.call_count, 0)
        self.assertTrue((autopilot.DRAFTS / "20260917-072530-ab12.card.html").exists())

    def test_published_run_writes_the_page_commits_and_rsyncs(self):
        result, _, commit, rsync = self._run(["20260917-072530-ab12"], publish=True)
        self.assertEqual(result, [("20260917-072530-ab12", "published")])
        self.assertEqual(commit.call_count, 1)
        self.assertEqual(rsync.call_count, 1)
        page = autopilot.ANSWERS_PAGE.read_text(encoding="utf-8")
        self.assertIn("20260917-072530-ab12", page)
        self.assertIn("已定位，待修复", page)

    def test_a_failed_ship_is_not_recorded_as_published(self):
        """A card that never reached the server must stay pending.  The first
        cron run after the 6-day outage lost 20261001-192738-30c2 exactly this
        way: state said "published", the live page had nothing, and `run_once`
        skips any id already in the state.
        """
        analysis = dict(ANALYSIS, status="answered")
        with mock.patch.object(autopilot, "git_commit", return_value=True), \
             mock.patch.object(autopilot, "rsync_page", return_value=False), \
             mock.patch.object(autopilot, "rebuild_board_page"):
            ok = autopilot.publish("20260917-072530-ab12", analysis, "问题", {})
        self.assertFalse(ok)
        state = json.loads(autopilot.STATE_FILE.read_text(encoding="utf-8"))
        self.assertNotIn("20260917-072530-ab12", state)   # next run retries it

    def test_bad_model_output_is_recorded_and_publishes_nothing(self):
        with mock.patch.object(autopilot, "http_get", side_effect=self._fake_http(
                ["20260917-072530-ab12"])), \
             mock.patch.object(autopilot, "run_pi",
                               side_effect=ValueError("缺少字段：solution")), \
             mock.patch.object(autopilot, "git_commit") as commit, \
             mock.patch.object(autopilot, "rsync_page") as rsync:
            result = autopilot.run_once(publish_it=True)
        self.assertEqual(result, [("20260917-072530-ab12", "failed")])
        self.assertEqual(commit.call_count, 0)
        self.assertEqual(rsync.call_count, 0)
        self.assertFalse(autopilot.ANSWERS_PAGE.exists())
        state = json.loads(autopilot.STATE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(state["20260917-072530-ab12"]["status"], "failed")

    def test_pi_timeout_is_a_recorded_failure(self):
        with mock.patch.object(autopilot, "http_get", side_effect=self._fake_http(
                ["20260917-072530-ab12"])), \
             mock.patch.object(autopilot, "run_pi",
                               side_effect=subprocess.TimeoutExpired("pi", 540)), \
             mock.patch.object(autopilot, "rsync_page") as rsync:
            result = autopilot.run_once(publish_it=True)
        self.assertEqual(result, [("20260917-072530-ab12", "failed")])
        self.assertEqual(rsync.call_count, 0)

    def test_force_reprocesses_a_known_bundle(self):
        self._run(["20260917-072530-ab12"], publish=True)
        result, pi, _, _ = self._run(["20260917-072530-ab12"], publish=True,
                                     force="20260917-072530-ab12")
        self.assertEqual(result, "published")
        self.assertEqual(pi.call_count, 1)

    def test_inspect_reads_the_digest_without_calling_the_model(self):
        with mock.patch.object(autopilot, "http_get", side_effect=self._fake_http(
                ["20260917-072530-ab12"])), \
             mock.patch.object(autopilot, "run_pi") as pi:
            code = autopilot.inspect_bundle("20260917-072530-ab12")
        self.assertEqual(code, 0)
        self.assertEqual(pi.call_count, 0)


class PromptTests(AutopilotFixture):
    """`run_pi` is faked everywhere else, so the prompt itself needs its own guard:
    a live run died with KeyError(' digest ') because str.format read the spaces
    as part of the field name."""

    def test_prompt_formats_and_carries_the_id_and_digest(self):
        prompt = autopilot.PROMPT_TEMPLATE.format(rid="20260917-072530-ab12",
                                                  digest="DigestMarker",
                                                  separator="---")
        self.assertIn("20260917-072530-ab12", prompt)
        self.assertIn("DigestMarker", prompt)
        self.assertIn("只输出一个 JSON 对象", prompt)
        # An unbounded agent loop is what timed out in the live smoke run.
        self.assertIn("探查预算", prompt)

    def test_prompt_requires_checking_the_repo_incident_history(self):
        """The first live card called a documented code defect an environment problem
        because the model never read our own version history (2026-09-17)."""
        prompt = autopilot.PROMPT_TEMPLATE.format(rid="x", digest="d", separator="-")
        self.assertIn("本仓历史", prompt)
        self.assertIn("CHANGELOG.md", prompt)
        self.assertIn("SDD/14-version-history.md", prompt)
        self.assertIn("按回归排查", prompt)
        self.assertIn("至少 1 次", prompt)

    def test_prompt_lists_the_status_and_category_vocabularies(self):
        prompt = autopilot.PROMPT_TEMPLATE.format(rid="x", digest="d", separator="-")
        for token in ("need_more_info", "needs_fix", "环境", "产品缺陷"):
            self.assertIn(token, prompt)


class PiInvocationTests(AutopilotFixture):
    """`pi` must never inherit stdin: a piped parent made it wait for EOF and the
    live smoke run burned the full 540 s timeout on it."""

    def test_run_pi_closes_stdin_and_parses_the_contract(self):
        import subprocess as sp
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"], captured["kwargs"] = cmd, kwargs
            return sp.CompletedProcess(cmd, 0, stdout=json.dumps(ANALYSIS), stderr="")

        with mock.patch.object(autopilot, "pi_binary", return_value="/fake/pi"), \
             mock.patch.object(autopilot.subprocess, "run", side_effect=fake_run):
            analysis = autopilot.run_pi("digest", "20260917-072530-ab12")
        self.assertEqual(analysis["status"], "needs_fix")
        self.assertEqual(captured["kwargs"]["stdin"], sp.DEVNULL)
        self.assertIn("--tools", captured["cmd"])
        self.assertIn("read,grep,find,ls", captured["cmd"])
        self.assertTrue(captured["cmd"][-1].startswith("你是 MRRC Modern 的支持工程师"))


class RetryTests(AutopilotFixture):
    """Bounded retry + single-flight: the two silent-loss shapes found while
    draining the 6-day backlog (2026-10-16)."""

    def test_a_flaky_pi_call_is_retried_next_round(self):
        # 20261007-083527-6536 died with `pi 退出码 1` and only a manual --force
        # brought it back; one bad call must not retire a bundle forever.
        with mock.patch.object(autopilot, "http_get", side_effect=self._fake_http(
                ["20260917-072530-ab12"])), \
             mock.patch.object(autopilot, "run_pi",
                               side_effect=ValueError("模型输出里没有 JSON 对象")):
            first = autopilot.run_once(publish_it=True)
        self.assertEqual(first, [("20260917-072530-ab12", "failed")])
        with mock.patch.object(autopilot, "http_get", side_effect=self._fake_http(
                ["20260917-072530-ab12"])), \
             mock.patch.object(autopilot, "run_pi", return_value=ANALYSIS):
            second = autopilot.run_once(publish_it=False)
        self.assertEqual(second, [("20260917-072530-ab12", "drafted")])

    def test_retries_stop_at_the_attempt_limit(self):
        for _ in range(autopilot.MAX_ATTEMPTS):
            with mock.patch.object(autopilot, "http_get", side_effect=self._fake_http(
                    ["20260917-072530-ab12"])), \
                 mock.patch.object(autopilot, "run_pi",
                                   side_effect=ValueError("模型输出里没有 JSON 对象")):
                autopilot.run_once(publish_it=True)
        with mock.patch.object(autopilot, "http_get", side_effect=self._fake_http(
                ["20260917-072530-ab12"])), \
             mock.patch.object(autopilot, "run_pi", return_value=ANALYSIS) as pi:
            result = autopilot.run_once(publish_it=False)
        self.assertEqual(result, [])
        self.assertEqual(pi.call_count, 0)

    @unittest.skipUnless(os.name == "posix",
                         "单飞锁是 fcntl.flock —— autopilot 本身只在 POSIX 上跑（"
                         "降级成假锁就等于放回它修的那个并发覆写）")
    def test_a_second_instance_skips_the_round(self):
        held = autopilot.acquire_single_flight_lock()
        self.assertIsNotNone(held)
        try:
            self.assertIsNone(autopilot.acquire_single_flight_lock())
        finally:
            held.close()


class CronTests(AutopilotFixture):
    def test_cron_line_has_absolute_paths_and_the_marker(self):
        line = autopilot.cron_line()
        self.assertIn(str(autopilot.REPO / "dev_tools" / "support_autopilot.py"), line)
        self.assertIn(autopilot.CRON_MARK, line)
        self.assertIn("--once --publish", line)
        self.assertIn("/opt/homebrew/bin", line)          # cron PATH is nearly empty
        self.assertIn(str(autopilot.RUN_LOG), line)

    @unittest.skipUnless(os.name == "posix",
                         "单飞锁是 fcntl.flock —— autopilot 本身只在 POSIX 上跑（"
                         "降级成假锁就等于放回它修的那个并发覆写）")
    def test_cron_command_still_polls_the_receiver(self):
        """2026-10-10→10-16 outage: a bare `--implement` in the cron command is
        truthy (argparse const="ALL"), so `main()` took the implement branch and
        returned before `--once` — the receiver was polled zero times for six
        days (run.log: 1728 lines, not one "没有新包").  Pin the order.
        """
        with mock.patch.object(autopilot, "run_once", return_value=[]) as poll, \
             mock.patch.object(autopilot, "run_implementations", return_value=[]), \
             mock.patch.object(autopilot, "sync_board_decisions", return_value=[]):
            autopilot.main(["--once", "--publish", "--implement"])
        poll.assert_called_once()

    def test_cron_line_does_not_shadow_once(self):
        # `--once` already runs the implementation sweep; a bare `--implement`
        # alongside it can only ever steal the branch again.
        self.assertNotIn("--implement", autopilot.cron_line())

    def test_cron_line_path_contains_the_system_bins(self):
        """git/rsync/ssh live in /usr/bin.  A PATH without it made every cron
        publish fail (2026-10-16 21:20: `FileNotFoundError: 'git'`) while the
        state still said "published" — the card that reached nobody.
        """
        path = autopilot.cron_line().split("PATH=", 1)[1].split(" ", 1)[0]
        for directory in ("/usr/bin", "/bin", "/usr/sbin", "/sbin"):
            self.assertIn(directory, path.split(":"))

    def test_install_replaces_its_own_line_idempotently(self):
        existing = "5 4 * * * /usr/bin/true\n" + autopilot.cron_line() + "\n"
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            if cmd[:2] == ["crontab", "-l"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=existing, stderr="")
            captured["input"] = kwargs.get("input", "")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(autopilot.subprocess, "run", side_effect=fake_run):
            self.assertEqual(autopilot.install_cron(), 0)
        written = captured["input"]
        self.assertEqual(written.count(autopilot.CRON_MARK), 1)
        self.assertIn("/usr/bin/true", written)            # other entries survive

    def _crontab_run(self, initial):
        """Fake `crontab -l`/`crontab -` pair remembering the last written table."""
        state = {"input": None}

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["crontab", "-l"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=initial, stderr="")
            state["input"] = kwargs.get("input", "")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        return fake_run, state

    def test_decision_cron_line_shape(self):
        line = autopilot.decision_cron_line()
        self.assertTrue(line.startswith("* * * * * "), line)   # every minute
        self.assertIn(sys.executable, line)
        self.assertIn(str(autopilot.REPO / "dev_tools" / "support_autopilot.py"), line)
        self.assertIn("--sync-decisions", line)
        self.assertIn(autopilot.DECISION_CRON_MARK, line)
        self.assertIn(str(autopilot.RUN_LOG), line)
        self.assertNotIn("--implement", line)
        self.assertNotIn("--once", line)
        self.assertNotIn("--publish", line)
        # 同一个显式 PATH：cron 的环境几乎是空的
        self.assertEqual(autopilot.cron_line().split("PATH=", 1)[1].split(" ", 1)[0],
                         line.split("PATH=", 1)[1].split(" ", 1)[0])

    def test_install_cron_installs_exactly_one_line_per_marker(self):
        existing = ("5 4 * * * /usr/bin/true\n"
                    "# 运维注释\n"
                    # 旧版安装留下的过时 autopilot 行（同标记）→ 被替换
                    + autopilot.cron_line().replace("--once --publish", "--once") + "\n"
                    + autopilot.decision_cron_line() + "\n")
        fake_run, state = self._crontab_run(existing)
        with mock.patch.object(autopilot.subprocess, "run", side_effect=fake_run):
            self.assertEqual(autopilot.install_cron(), 0)
        written = state["input"]
        self.assertEqual(written.count(autopilot.CRON_MARK), 1)
        self.assertEqual(written.count(autopilot.DECISION_CRON_MARK), 1)
        self.assertIn("--once --publish", written)      # stale 行被新版替换
        self.assertIn("/usr/bin/true", written)            # other entries survive
        self.assertIn("# 运维注释", written)                # 无关注释也保留
        # 重装进已经装好的表：两个标记仍然各恰好一行
        fake_run, state = self._crontab_run(written)
        with mock.patch.object(autopilot.subprocess, "run", side_effect=fake_run):
            self.assertEqual(autopilot.install_cron(), 0)
        self.assertEqual(state["input"].count(autopilot.CRON_MARK), 1)
        self.assertEqual(state["input"].count(autopilot.DECISION_CRON_MARK), 1)
        self.assertIn("/usr/bin/true", state["input"])

    def test_uninstall_cron_removes_both_markers(self):
        existing = ("5 4 * * * /usr/bin/true\n"
                    + autopilot.cron_line() + "\n"
                    + autopilot.decision_cron_line() + "\n")

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["crontab", "-l"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=existing, stderr="")
            captured.append(kwargs.get("input", ""))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        captured = []
        with mock.patch.object(autopilot.subprocess, "run", side_effect=fake_run):
            self.assertEqual(autopilot.uninstall_cron(), 0)
        self.assertNotIn(autopilot.CRON_MARK, captured[0])
        self.assertNotIn(autopilot.DECISION_CRON_MARK, captured[0])
        self.assertIn("/usr/bin/true", captured[0])        # other entries survive


@unittest.skipUnless(os.name == "posix",
                     "单飞锁是 fcntl.flock —— autopilot 本身只在 POSIX 上跑（"
                     "降级成假锁就等于放回它修的那个并发覆写）")
class SyncDecisionsCliTests(AutopilotFixture):
    """`--sync-decisions` runs alone under the existing single-flight lock.

    It must never touch the model, the poll, or the implementation sweep —
    a minute-cron sync that spun up `pi` would shadow the very autopilot
    round it is supposed to feed.
    """

    def test_sync_only_never_touches_model_poll_or_implementations(self):
        lock = mock.MagicMock()
        with mock.patch.object(autopilot, "acquire_single_flight_lock",
                               return_value=lock) as acquire, \
             mock.patch.object(autopilot, "sync_board_decisions",
                               return_value=[("dec-1", "applied")]) as sync, \
             mock.patch.object(autopilot, "run_once") as poll, \
             mock.patch.object(autopilot, "run_pi") as pi, \
             mock.patch.object(autopilot, "run_implementations") as impl:
            self.assertEqual(autopilot.main(["--sync-decisions"]), 0)
        acquire.assert_called_once_with()
        sync.assert_called_once_with()
        poll.assert_not_called()
        pi.assert_not_called()
        impl.assert_not_called()
        lock.close.assert_called_once_with()

    def test_sync_prints_each_result_like_other_commands(self):
        out = io.StringIO()
        with mock.patch.object(autopilot, "sync_board_decisions",
                               return_value=[("dec-1", "applied"),
                                             ("dec-2", "stale")]):
            with contextlib.redirect_stdout(out):
                self.assertEqual(autopilot.main(["--sync-decisions"]), 0)
        printed = out.getvalue()
        self.assertIn("dec-1", printed)
        self.assertIn("applied", printed)
        self.assertIn("dec-2", printed)
        self.assertIn("stale", printed)

    def test_sync_quietly_skips_when_the_lock_is_held(self):
        held = autopilot.acquire_single_flight_lock()
        self.assertIsNotNone(held)
        try:
            with mock.patch.object(autopilot, "sync_board_decisions") as sync:
                self.assertEqual(autopilot.main(["--sync-decisions"]), 0)
            sync.assert_not_called()
        finally:
            held.close()

    def test_sync_releases_the_lock_when_sync_raises(self):
        # sync_board_decisions() already contains its own errors; the finally
        # is the belt-and-braces that keeps a crash from parking the lock.
        with mock.patch.object(autopilot, "sync_board_decisions",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                autopilot.main(["--sync-decisions"])
        again = autopilot.acquire_single_flight_lock()
        self.assertIsNotNone(again)        # lock was closed, not leaked
        again.close()

    def test_once_syncs_decisions_before_polling_under_one_lock(self):
        calls = []
        with mock.patch.object(autopilot, "acquire_single_flight_lock",
                               wraps=autopilot.acquire_single_flight_lock) as acquire, \
             mock.patch.object(autopilot, "sync_board_decisions",
                               side_effect=lambda: calls.append("sync") or []) as sync, \
             mock.patch.object(autopilot, "run_once",
                               side_effect=lambda *a, **k: calls.append("poll") or []) as poll, \
             mock.patch.object(autopilot, "run_implementations",
                               side_effect=lambda *a, **k: calls.append("impl") or []) as impl:
            self.assertEqual(autopilot.main(["--once", "--publish"]), 0)
        self.assertEqual(calls, ["sync", "poll", "impl"])
        acquire.assert_called_once_with()          # 同一把锁，不嵌套
        sync.assert_called_once_with()
        poll.assert_called_once()
        impl.assert_called_once()


class PageDeployTests(AutopilotFixture):
    """The docroot belongs to www-data: a direct rsync is refused (and returns 0
    when nothing changed, which is how a broken path looked healthy)."""

    def test_upload_then_sudo_install(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr=b"")

        with mock.patch.object(autopilot, "_ship_page",
                               side_effect=self.real_ship_page), \
                mock.patch.object(autopilot.subprocess, "run", side_effect=fake_run):
            self.assertTrue(autopilot.rsync_page())
        self.assertEqual(calls[0][0], "rsync")
        self.assertIn(autopilot.STAGING, calls[0][-1])
        self.assertEqual(calls[1][0], "ssh")
        self.assertIn("sudo install", calls[1][-1])
        self.assertIn(autopilot.REMOTE_PAGE, calls[1][-1])

    def test_failure_is_reported_not_raised(self):
        def fake_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(12, cmd, stderr=b"Permission denied")

        with mock.patch.object(autopilot, "_ship_page",
                               side_effect=self.real_ship_page), \
                mock.patch.object(autopilot.subprocess, "run", side_effect=fake_run):
            self.assertFalse(autopilot.rsync_page())

    # ── board page + private state mirror ─────────────────────────────
    # The receiver's authenticated operations page reads the mirror, so
    # publishing ships the public page AND board_state.json — and a failure
    # on either side never skips the other.

    def _board_state(self):
        return {"20261101-000000-aa11": {"status": "scheduled", "kind": "bug",
                                         "title": "录音静音", "updated": "2026-11-01"}}

    def test_board_publish_ships_page_and_state_mirror(self):
        autopilot.board.save_state(autopilot.BOARD_STATE_FILE, self._board_state())
        with mock.patch.object(autopilot, "_ship_page", return_value=True) as ship:
            self.assertTrue(autopilot.publish_board_page(generated_at="2026-11-01 00:00"))
        self.assertEqual(
            [tuple(c.args) for c in ship.call_args_list],
            [(autopilot.BOARD_PAGE_PATH, autopilot.BOARD_STAGING,
              autopilot.REMOTE_BOARD_PAGE, "看板页"),
             (autopilot.BOARD_STATE_FILE, autopilot.BOARD_STATE_STAGING,
              autopilot.REMOTE_BOARD_STATE, "看板状态镜像")])

    def test_board_generated_html_is_written(self):
        autopilot.board.save_state(autopilot.BOARD_STATE_FILE, self._board_state())
        with mock.patch.object(autopilot, "_ship_page", return_value=True):
            autopilot.publish_board_page(generated_at="2026-11-01 00:00")
        html = autopilot.BOARD_PAGE_PATH.read_text(encoding="utf-8")
        self.assertEqual(html, autopilot.board.render_board(self._board_state(),
                                                            "2026-11-01 00:00"))
        self.assertIn("FDE 看板", html)

    def test_board_page_failure_still_ships_mirror_and_returns_false(self):
        with mock.patch.object(autopilot, "_ship_page",
                               side_effect=[False, True]) as ship:
            self.assertFalse(autopilot.publish_board_page())
        self.assertEqual(ship.call_count, 2)

    def test_board_mirror_failure_returns_false(self):
        with mock.patch.object(autopilot, "_ship_page",
                               side_effect=[True, False]) as ship:
            self.assertFalse(autopilot.publish_board_page())
        self.assertEqual(ship.call_count, 2)

    def test_board_both_failures_return_false(self):
        with mock.patch.object(autopilot, "_ship_page", return_value=False) as ship:
            self.assertFalse(autopilot.publish_board_page())
        self.assertEqual(ship.call_count, 2)

    def test_rebuild_board_page_wrapper_returns_path(self):
        with mock.patch.object(autopilot, "_ship_page", return_value=True) as ship:
            result = autopilot.rebuild_board_page(generated_at="2026-11-01 00:00")
        self.assertEqual(result, autopilot.BOARD_PAGE_PATH)
        self.assertEqual(ship.call_count, 2)


class ContextTests(AutopilotFixture):
    def test_digest_carries_problem_summary_and_log_tail(self):
        folder = self.root / "20260917-072530-ab12"
        (folder / "logs").mkdir(parents=True)
        (folder / "problem.txt").write_text("录音是空的，MP3 只有静音", encoding="utf-8")
        (folder / "diagnostics").mkdir()
        (folder / "diagnostics" / "summary.txt").write_text("启动次数：1 次", encoding="utf-8")
        (folder / "logs" / "server.log").write_text("Recording dropped 950 block(s)\n",
                                                    encoding="utf-8")
        digest = autopilot.collect_context(folder, "20260917-072530-ab12")
        for expected in ("录音是空的", "启动次数：1 次", "Recording dropped 950 block(s)",
                         "diagnostics/summary.txt"):
            self.assertIn(expected, digest)

    def test_digest_survives_a_minimal_bundle(self):
        folder = self.root / "empty"
        folder.mkdir()
        self.assertIn("编号", autopilot.collect_context(folder, "20260917-072530-ab12"))


class DecisionHttpTests(AutopilotFixture):
    """Exact HTTP methods and response validation for the decision queue.

    The queue endpoints are authenticated like everything else; the decision
    id in the DELETE path must be validated before any network happens.
    """

    UUID = "01234567-89ab-cdef-0123-456789abcdef"

    def _urlopen(self, body: bytes = b"{}"):
        import base64
        env = mock.patch.dict(os.environ, {"SUPPORT_PASSWORD": "s3cret"})
        env.start()
        self.addCleanup(env.stop)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = body
        opener = mock.patch.object(autopilot.urllib.request, "urlopen",
                                   return_value=response)
        self._opener = opener.start()
        self.addCleanup(opener.stop)
        self._auth = "Basic " + base64.b64encode(b"mrrc:s3cret").decode()
        return self._opener

    def _request(self, opener):
        self.assertEqual(opener.call_count, 1)
        return opener.call_args.args[0]

    def test_http_get_is_a_get_with_basic_auth(self):
        opener = self._urlopen(b"payload")
        self.assertEqual(autopilot.http_get("/api/list"), b"payload")
        request = self._request(opener)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, f"{autopilot.RECEIVER_URL}/api/list")
        self.assertEqual(request.get_header("Authorization"), self._auth)

    def test_http_request_supports_other_methods(self):
        opener = self._urlopen(b"{}")
        self.assertEqual(autopilot.http_request("/x", method="DELETE"), b"{}")
        self.assertEqual(self._request(opener).get_method(), "DELETE")

    def test_ack_sends_delete_to_the_decision_path(self):
        opener = self._urlopen(json.dumps({"ok": True,
                                           "decision_id": self.UUID}).encode())
        autopilot.ack_board_decision(self.UUID)
        request = self._request(opener)
        self.assertEqual(request.get_method(), "DELETE")
        self.assertEqual(request.full_url,
                         f"{autopilot.RECEIVER_URL}/api/board/decisions/{self.UUID}")

    def test_ack_refuses_a_non_canonical_uuid_without_network(self):
        opener = self._urlopen()
        for bad in ("NOT-A-UUID", "01234567-89AB-CDEF-0123-456789ABCDEF",
                    "01234567-89ab-cdef-0123-456789abcdef-extra"):
            with self.assertRaises(ValueError, msg=bad):
                autopilot.ack_board_decision(bad)
        self.assertEqual(opener.call_count, 0)

    def test_ack_rejects_a_bad_response_envelope(self):
        self._urlopen(json.dumps({"ok": False}).encode())
        with self.assertRaises(ValueError):
            autopilot.ack_board_decision(self.UUID)

    def test_list_uses_get_and_returns_validated_items(self):
        envelope = {"ok": True, "decisions": [
            {"decision_id": self.UUID, "bundle_id": "20261101-120000-ab12",
             "action": "schedule", "note": "网页排期",
             "created_at": "2026-11-01T12:00:00Z"}]}
        opener = self._urlopen(json.dumps(envelope).encode())
        items = autopilot.list_board_decisions()
        self.assertEqual([i["decision_id"] for i in items], [self.UUID])
        request = self._request(opener)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url,
                         f"{autopilot.RECEIVER_URL}/api/board/decisions")

    def _list_with(self, body: bytes):
        with mock.patch.object(autopilot, "http_get", return_value=body):
            return autopilot.list_board_decisions()

    def test_list_rejects_malformed_envelopes(self):
        for body in (b"not json", b"[1,2]", b"{}",
                     json.dumps({"ok": False, "decisions": []}).encode(),
                     json.dumps({"ok": True}).encode(),
                     json.dumps({"ok": True, "decisions": {}}).encode()):
            with self.assertRaises(ValueError, msg=body):
                self._list_with(body)

    def test_list_rejects_non_object_items(self):
        body = json.dumps({"ok": True, "decisions": ["nope"]}).encode()
        with self.assertRaises(ValueError):
            self._list_with(body)

    def test_list_rejects_non_canonical_or_missing_uuid(self):
        base = {"bundle_id": "20261101-120000-ab12", "action": "schedule",
                "note": "", "created_at": "2026-11-01T12:00:00Z"}
        for bad in ("01234567-89AB-CDEF-0123-456789ABCDEF",   # uppercase
                    "01234567-89ab-cdef-0123-456789abcde",     # 35 chars
                    "zzz", None):
            item = dict(base, decision_id=bad)
            body = json.dumps({"ok": True, "decisions": [item]}).encode()
            with self.assertRaises(ValueError, msg=bad):
                self._list_with(body)

    def test_list_rejects_wrong_field_types(self):
        base = {"decision_id": self.UUID, "bundle_id": "20261101-120000-ab12",
                "action": "schedule", "note": "", "created_at": "t"}
        for field, value in (("bundle_id", 42), ("action", None),
                             ("note", 7), ("created_at", 3.5),
                             ("created_at", None)):
            item = dict(base)
            item[field] = value
            body = json.dumps({"ok": True, "decisions": [item]}).encode()
            with self.assertRaises(ValueError, msg=f"{field}={value}"):
                self._list_with(body)


class DecisionSyncTests(AutopilotFixture):
    """Mac-side decision synchronization (plan 2026-11-01, Task 5).

    The receiver queue is not a second state machine: the local board state
    is authoritative, every transition goes through board.decide(), and an
    item is acknowledged only after the public page + private mirror publish.
    """

    UUID = "01234567-89ab-cdef-0123-456789abcdef"
    UUID2 = "aaaaaaaa-89ab-cdef-0123-456789abcdef"
    BUNDLE = "20261101-120000-ab12"
    BUNDLE2 = "20261101-120001-ab12"

    def _decision(self, decision_id=None, **over):
        item = {"decision_id": decision_id or self.UUID,
                "bundle_id": self.BUNDLE, "action": "schedule",
                "note": "网页排期", "created_at": "2026-11-01T12:00:00Z"}
        item.update(over)
        return item

    def _board_state(self, status="backlog", bundle=None):
        state = autopilot.board.load_state(autopilot.BOARD_STATE_FILE)
        state[bundle or self.BUNDLE] = {"status": status, "kind": "feature"}
        autopilot.board.save_state(autopilot.BOARD_STATE_FILE, state)

    def _local_status(self, bundle=None):
        state = autopilot.board.load_state(autopilot.BOARD_STATE_FILE)
        entry = state.get(bundle or self.BUNDLE) or {}
        return entry.get("status")

    def _marker(self):
        return autopilot._unpublished_marker_path()

    def _sync(self, decisions, publish=True):
        with mock.patch.object(autopilot, "list_board_decisions",
                               return_value=decisions), \
             mock.patch.object(autopilot, "publish_board_page",
                               return_value=publish) as publish_mock, \
             mock.patch.object(autopilot, "ack_board_decision") as ack_mock:
            result = autopilot.sync_board_decisions()
        return result, publish_mock, ack_mock

    def test_schedule_is_applied_published_then_acknowledged(self):
        self._board_state("backlog")
        result, publish, ack = self._sync([self._decision()])
        self.assertEqual(self._local_status(), "scheduled")
        self.assertEqual(result, [(self.UUID, "applied")])
        publish.assert_called_once_with()
        ack.assert_called_once_with(self.UUID)

    def test_reject_from_backlog_is_applied(self):
        self._board_state("backlog")
        result, _, ack = self._sync([self._decision(action="reject")])
        self.assertEqual(self._local_status(), "rejected")
        self.assertEqual(result, [(self.UUID, "applied")])
        ack.assert_called_once_with(self.UUID)

    def test_backlog_from_scheduled_and_rejected_is_applied(self):
        for start in ("scheduled", "rejected"):
            with self.subTest(start=start):
                self._board_state(start)
                result, publish, ack = self._sync(
                    [self._decision(action="backlog")])
                self.assertEqual(self._local_status(), "backlog")
                self.assertEqual(result, [(self.UUID, "applied")])
                publish.assert_called_once_with()
                ack.assert_called_once_with(self.UUID)

    def test_illegal_transition_is_stale_acknowledged_unchanged(self):
        """schedule on an already-scheduled card cannot re-run: the local
        state is the authority and the queue item is acknowledged so it
        cannot jam the queue."""
        self._board_state("scheduled")
        result, publish, ack = self._sync([self._decision()])
        self.assertEqual(result, [(self.UUID, "stale")])
        self.assertEqual(self._local_status(), "scheduled")
        publish.assert_not_called()
        ack.assert_called_once_with(self.UUID)

    def test_unknown_card_is_stale_acknowledged(self):
        self._board_state("backlog")
        result, publish, ack = self._sync(
            [self._decision(bundle_id="20261101-120099-ab12")])
        self.assertEqual(result, [(self.UUID, "stale")])
        publish.assert_not_called()
        ack.assert_called_once_with(self.UUID)

    def test_semantically_invalid_items_are_acknowledged_invalid(self):
        """A bad bundle id / action / over-long note / missing created_at is
        invalid — acknowledged off the queue, never touching local state."""
        self._board_state("backlog")
        bad_items = [
            self._decision(bundle_id="not-a-bundle-id"),
            self._decision(action="ship"),
            self._decision(note="x" * 201),
            self._decision(created_at=""),
        ]
        for item in bad_items:
            with self.subTest(item=item):
                result, publish, ack = self._sync([item])
                self.assertEqual(result, [(item["decision_id"], "invalid")])
                self.assertEqual(self._local_status(), "backlog")
                publish.assert_not_called()
                ack.assert_called_once_with(item["decision_id"])

    def test_publish_failure_leaves_applied_pending_and_unacknowledged(self):
        self._board_state("backlog")
        result, publish, ack = self._sync([self._decision()], publish=False)
        self.assertEqual(result, [(self.UUID, "pending")])
        self.assertEqual(self._local_status(), "scheduled")
        publish.assert_called_once_with()
        ack.assert_not_called()
        self.assertTrue(self._marker().exists())   # republish owed

    def test_publish_exception_is_treated_like_publish_failure(self):
        """I-1: publish_board_page() can raise (mkdir/write/render).  It must
        not escape sync and discard the batch — applied items return pending,
        are not acked, the local transition stands, and the unpublished
        marker makes the retry durable."""
        self._board_state("backlog")
        with mock.patch.object(autopilot, "list_board_decisions",
                               return_value=[self._decision()]), \
             mock.patch.object(autopilot, "publish_board_page",
                               side_effect=OSError("disk full")), \
             mock.patch.object(autopilot, "ack_board_decision") as ack:
            result = autopilot.sync_board_decisions()
        self.assertEqual(result, [(self.UUID, "pending")])
        self.assertEqual(self._local_status(), "scheduled")
        ack.assert_not_called()
        self.assertTrue(self._marker().exists())

    def test_unpublished_marker_republishes_before_stale_replay_is_acked(self):
        """I-2 repro, exact two rounds: round 1 saves the transition but the
        publish fails; round 2 the same decision is a stale replay — it may
        ONLY be acked after the marker forces a successful republish, else
        local state and the shipped mirror diverge forever."""
        self._board_state("backlog")
        with mock.patch.object(autopilot, "list_board_decisions",
                               return_value=[self._decision()]), \
             mock.patch.object(autopilot, "publish_board_page",
                               return_value=False), \
             mock.patch.object(autopilot, "ack_board_decision") as ack:
            first = autopilot.sync_board_decisions()
        self.assertEqual(first, [(self.UUID, "pending")])
        self.assertTrue(self._marker().exists())
        ack.assert_not_called()
        self.assertEqual(self._local_status(), "scheduled")
        with mock.patch.object(autopilot, "list_board_decisions",
                               return_value=[self._decision()]), \
             mock.patch.object(autopilot, "publish_board_page",
                               return_value=True) as publish, \
             mock.patch.object(autopilot, "ack_board_decision") as ack:
            second = autopilot.sync_board_decisions()
        self.assertEqual(second, [(self.UUID, "stale")])
        publish.assert_called_once_with()
        self.assertFalse(self._marker().exists())
        ack.assert_called_once_with(self.UUID)
        self.assertEqual(self._local_status(), "scheduled")

    def test_marker_triggers_publish_even_with_an_empty_queue(self):
        self._marker().write_text("stuck", encoding="utf-8")
        result, publish, ack = self._sync([])
        self.assertEqual(result, [])
        publish.assert_called_once_with()
        ack.assert_not_called()
        self.assertFalse(self._marker().exists())

    def test_marker_survives_a_failed_republish_with_an_empty_queue(self):
        self._marker().write_text("stuck", encoding="utf-8")
        result, publish, ack = self._sync([], publish=False)
        self.assertEqual(result, [])
        publish.assert_called_once_with()
        ack.assert_not_called()
        self.assertTrue(self._marker().exists())

    def test_stale_replay_is_not_acked_while_republish_fails(self):
        """The marker means a republish is owed; until it succeeds, even a
        stale classification stays un-acked (pending) so the queue keeps the
        retry obligation alive instead of forgetting it."""
        self._board_state("scheduled")
        self._marker().write_text("stuck", encoding="utf-8")
        result, publish, ack = self._sync([self._decision()], publish=False)
        self.assertEqual(result, [(self.UUID, "pending")])
        publish.assert_called_once_with()
        ack.assert_not_called()
        self.assertTrue(self._marker().exists())
        self.assertEqual(self._local_status(), "scheduled")

    def test_marker_write_failure_aborts_item_without_touching_state(self):
        """If the marker itself cannot be written, the decision must abort as
        pending BEFORE the durable state write — an unmarked state change is
        the crash-window divergence, so it must never land."""
        self._board_state("backlog")
        with mock.patch.object(autopilot, "list_board_decisions",
                               return_value=[self._decision()]), \
             mock.patch.object(autopilot, "publish_board_page",
                               return_value=False), \
             mock.patch.object(autopilot, "_mark_unpublished",
                               side_effect=OSError("read-only")), \
             mock.patch.object(autopilot, "ack_board_decision") as ack:
            result = autopilot.sync_board_decisions()
        self.assertEqual(result, [(self.UUID, "pending")])
        ack.assert_not_called()
        self.assertEqual(self._local_status(), "backlog")   # state untouched

    def test_marker_is_written_before_the_state_save(self):
        """Crash consistency: the marker must already exist at the moment the
        durable state write happens — created by _decision_outcome BEFORE
        save_state, not by the later publish attempt.  A crash in the old
        gap left state saved with no marker, and the next round stale-acked
        the change without ever publishing it."""
        self._board_state("backlog")
        real_save = autopilot.board.save_state
        observed = {"marker_at_save": None}

        def save_with_invariant(path, state):
            observed["marker_at_save"] = self._marker().exists()
            real_save(path, state)

        with mock.patch.object(autopilot, "list_board_decisions",
                               return_value=[self._decision()]), \
             mock.patch.object(autopilot, "publish_board_page",
                               return_value=False), \
             mock.patch.object(autopilot, "ack_board_decision") as ack, \
             mock.patch.object(autopilot.board, "save_state",
                               wraps=save_with_invariant):
            result = autopilot.sync_board_decisions()
        self.assertTrue(observed["marker_at_save"],
                        "marker must exist before the durable state write")
        self.assertEqual(result, [(self.UUID, "pending")])
        ack.assert_not_called()

    def test_restart_after_crash_republishes_before_any_stale_ack(self):
        """Disk exactly as a crash right after the outcome save leaves it:
        transition persisted, marker present, publish never attempted.  The
        next round must republish BEFORE the replayed (stale) decision is
        acked — no stale ack before recovery publication."""
        self._board_state("scheduled")
        self._marker().write_text("pre-save marker", encoding="utf-8")
        result, publish, ack = self._sync([self._decision()])
        self.assertEqual(result, [(self.UUID, "stale")])
        publish.assert_called_once_with()
        self.assertFalse(self._marker().exists())
        ack.assert_called_once_with(self.UUID)
        self.assertEqual(self._local_status(), "scheduled")

    def test_round_after_marker_write_failure_recovers(self):
        """A marker-write failure must not wedge the card: the item is
        pending with the authoritative state untouched, nothing acked; a
        later round applies, publishes, acks, and clears the marker."""
        self._board_state("backlog")
        with mock.patch.object(autopilot, "list_board_decisions",
                               return_value=[self._decision()]), \
             mock.patch.object(autopilot, "publish_board_page",
                               return_value=True), \
             mock.patch.object(autopilot, "ack_board_decision") as ack, \
             mock.patch.object(autopilot, "_mark_unpublished",
                               side_effect=OSError("read-only")):
            first = autopilot.sync_board_decisions()
        self.assertEqual(first, [(self.UUID, "pending")])
        self.assertEqual(self._local_status(), "backlog")   # state untouched
        ack.assert_not_called()
        result, publish, ack2 = self._sync([self._decision()])
        self.assertEqual(result, [(self.UUID, "applied")])
        self.assertEqual(self._local_status(), "scheduled")
        publish.assert_called_once_with()
        ack2.assert_called_once_with(self.UUID)
        self.assertFalse(self._marker().exists())

    def test_corrupt_state_with_marker_never_ships_or_clears_it(self):
        """publish_board_page reads the state strictly: a corrupt
        authoritative file fails the recovery attempt (nothing shipped,
        marker kept) instead of publishing an empty board and clearing the
        marker."""
        autopilot.BOARD_STATE_FILE.write_text("{not json", encoding="utf-8")
        self._marker().write_text("stuck", encoding="utf-8")
        with mock.patch.object(autopilot, "list_board_decisions",
                               return_value=[]), \
             mock.patch.object(autopilot, "_ship_page",
                               return_value=True) as ship, \
             mock.patch.object(autopilot, "ack_board_decision") as ack:
            result = autopilot.sync_board_decisions()
        self.assertEqual(result, [])
        ship.assert_not_called()
        ack.assert_not_called()
        self.assertTrue(self._marker().exists())

    def test_corrupt_board_state_fails_items_without_acking(self):
        """board.load_state() maps a corrupt file to {} — which would turn a
        real backlog into 'unknown card' and stale-ack every queued decision
        unapplied.  The strict per-item loader must fail the item instead,
        leaving the file untouched for repair."""
        autopilot.BOARD_STATE_FILE.write_text("{not json", encoding="utf-8")
        result, publish, ack = self._sync([self._decision()])
        self.assertEqual(result, [(self.UUID, "pending")])
        publish.assert_not_called()
        ack.assert_not_called()
        self.assertEqual(autopilot.BOARD_STATE_FILE.read_text(encoding="utf-8"),
                         "{not json")

    def test_one_failing_item_does_not_block_later_items(self):
        self._board_state("backlog", self.BUNDLE)
        self._board_state("backlog", self.BUNDLE2)
        decisions = [self._decision(), self._decision(self.UUID2, bundle_id=self.BUNDLE2)]
        with mock.patch.object(autopilot, "list_board_decisions",
                               return_value=decisions), \
             mock.patch.object(autopilot, "publish_board_page",
                               return_value=True) as publish, \
             mock.patch.object(autopilot, "ack_board_decision") as ack, \
             mock.patch.object(autopilot.board, "save_state",
                               wraps=autopilot.board.save_state,
                               side_effect=[OSError("disk full"), mock.DEFAULT]):
            result = autopilot.sync_board_decisions()
        self.assertEqual(result, [(self.UUID, "pending"),
                                  (self.UUID2, "applied")])
        self.assertEqual(self._local_status(), "backlog")     # item 1 untouched
        self.assertEqual(self._local_status(self.BUNDLE2), "scheduled")
        publish.assert_called_once_with()
        ack.assert_called_once_with(self.UUID2)

    def test_failed_ack_of_stale_item_marks_pending_and_continues(self):
        self._board_state("scheduled")
        decisions = [self._decision(),
                     self._decision(self.UUID2, bundle_id=self.BUNDLE2)]
        self._board_state("backlog", self.BUNDLE2)
        with mock.patch.object(autopilot, "list_board_decisions",
                               return_value=decisions), \
             mock.patch.object(autopilot, "publish_board_page",
                               return_value=True) as publish, \
             mock.patch.object(autopilot, "ack_board_decision",
                               side_effect=[RuntimeError("boom"), None]) as ack:
            result = autopilot.sync_board_decisions()
        self.assertEqual(result, [(self.UUID, "pending"),
                                  (self.UUID2, "applied")])
        self.assertEqual(ack.call_count, 2)
        publish.assert_called_once_with()

    def test_ack_failure_after_publish_replays_stale_without_reapplying(self):
        """Round 1: applied + published, but the acknowledgement fails → the
        item stays pending and the state transition stands.  Round 2: the
        same decision is re-delivered; decision_allowed() now refuses it, so
        it is acknowledged as stale — board.decide must not run twice."""
        self._board_state("backlog")
        with mock.patch.object(autopilot, "list_board_decisions",
                               return_value=[self._decision()]), \
             mock.patch.object(autopilot, "publish_board_page",
                               return_value=True), \
             mock.patch.object(autopilot, "ack_board_decision",
                               side_effect=RuntimeError("boom")):
            first = autopilot.sync_board_decisions()
        self.assertEqual(first, [(self.UUID, "pending")])
        self.assertEqual(self._local_status(), "scheduled")
        with mock.patch.object(autopilot, "list_board_decisions",
                               return_value=[self._decision()]), \
             mock.patch.object(autopilot, "publish_board_page",
                               return_value=True) as publish, \
             mock.patch.object(autopilot, "ack_board_decision") as ack, \
             mock.patch.object(autopilot.board, "decide",
                               wraps=autopilot.board.decide) as decide:
            second = autopilot.sync_board_decisions()
        self.assertEqual(second, [(self.UUID, "stale")])
        self.assertEqual(self._local_status(), "scheduled")
        decide.assert_not_called()          # the transition is never re-applied
        publish.assert_not_called()         # nothing changed → nothing to ship
        ack.assert_called_once_with(self.UUID)

    def test_publish_runs_once_for_a_batch_of_applied_items(self):
        self._board_state("backlog", self.BUNDLE)
        self._board_state("backlog", self.BUNDLE2)
        decisions = [self._decision(),
                     self._decision(self.UUID2, bundle_id=self.BUNDLE2,
                                    action="reject")]
        result, publish, ack = self._sync(decisions)
        self.assertEqual(result, [(self.UUID, "applied"),
                                  (self.UUID2, "applied")])
        self.assertEqual(self._local_status(), "scheduled")
        self.assertEqual(self._local_status(self.BUNDLE2), "rejected")
        publish.assert_called_once_with()
        self.assertEqual(ack.call_args_list,
                         [mock.call(self.UUID), mock.call(self.UUID2)])

    def test_no_publish_when_nothing_was_applied(self):
        self._board_state("scheduled")
        decisions = [self._decision(bundle_id="20261101-120099-ab12")]
        result, publish, ack = self._sync(decisions)
        self.assertEqual(result, [(self.UUID, "stale")])
        publish.assert_not_called()
        ack.assert_called_once_with(self.UUID)

    def test_listing_failure_refuses_the_round_without_side_effects(self):
        self._board_state("backlog")
        for exc in (ValueError("bad envelope"), OSError("receiver down")):
            with self.subTest(exc=exc), \
                 mock.patch.object(autopilot, "list_board_decisions",
                                   side_effect=exc), \
                 mock.patch.object(autopilot, "publish_board_page") as publish, \
                 mock.patch.object(autopilot, "ack_board_decision") as ack:
                result = autopilot.sync_board_decisions()
            self.assertEqual(result, [])
            self.assertEqual(self._local_status(), "backlog")
            publish.assert_not_called()
            ack.assert_not_called()


class DecisionEndToEndTests(AutopilotFixture):
    """Real local acceptance for the web-decision loop (plan 2026-11-01 Task 7).

    The actual vendored stdlib receiver serves a temporary SUPPORT_DIR (test
    mirror + password) on 127.0.0.1 loopback HTTP; a schedule decision is
    submitted over real HTTP and the real sync_board_decisions() runs against
    it — no list/ack/publish mocks.  The only mocked seam is _ship_page (the
    fixture's patch), so the page + mirror shipping is asserted by call, never
    executed against the live server.  One synthetic card id only; a second
    sync round proves the emptied queue is a quiet no-op.  Server threads and
    sockets are shut down and joined at cleanup.
    """

    BUNDLE = "20261101-120000-ab12"          # synthetic; never a real user card
    NOTE = "本地验收排期"
    PASSWORD = "s3cret"

    def setUp(self):
        super().setUp()
        # Ambient proxy config must not divert either side of the loop.
        # no_proxy/NO_PROXY keep the production sync's default-opener urllib
        # calls on loopback; any pre-existing value is merged, and patch.dict
        # restores the environment on cleanup.
        loopback = "127.0.0.1,localhost"
        existing = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
        merged = ",".join(x for x in (loopback, existing) if x)
        env = mock.patch.dict(os.environ, {"SUPPORT_PASSWORD": self.PASSWORD,
                                           "no_proxy": merged, "NO_PROXY": merged})
        env.start()
        self.addCleanup(env.stop)
        # Receiver storage: a test mirror with exactly one backlog card.
        self.receiver_dir = self.root / "receiver"
        self.receiver_dir.mkdir()
        mirror = {self.BUNDLE: {"status": "backlog", "kind": "feature",
                                "title": "本地验收合成卡片"}}
        (self.receiver_dir / "board_state.json").write_text(
            json.dumps(mirror, ensure_ascii=False), encoding="utf-8")
        for patch in (mock.patch.object(receiver, "DIR", str(self.receiver_dir)),
                      mock.patch.object(receiver, "PASSWORD", self.PASSWORD),
                      mock.patch.object(receiver, "USER", "mrrc"),
                      mock.patch.object(receiver, "_RATE", {})):
            patch.start()
            self.addCleanup(patch.stop)
        from http.server import ThreadingHTTPServer
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), receiver.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_receiver)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        url = mock.patch.object(autopilot, "RECEIVER_URL", self.base)
        url.start()
        self.addCleanup(url.stop)
        # Explicit no-proxy opener for this client's own requests (the
        # production sync side is covered by the no_proxy env patch above).
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        # The Mac-side authoritative state carries the same backlog card.
        state = autopilot.board.load_state(autopilot.BOARD_STATE_FILE)
        state[self.BUNDLE] = {"status": "backlog", "kind": "feature"}
        autopilot.board.save_state(autopilot.BOARD_STATE_FILE, state)
        # The fixture patched _ship_page; keep the mock handle for assertions.
        self.ship_mock = autopilot._ship_page

    def _stop_receiver(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise AssertionError("receiver serve_forever thread did not stop")

    def _authed(self, path):
        token = base64.b64encode(f"mrrc:{self.PASSWORD}".encode()).decode("ascii")
        return urllib.request.Request(self.base + path,
                                      headers={"Authorization": f"Basic {token}"})

    def _get_json(self, path):
        with self.opener.open(self._authed(path), timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_decision(self, action="schedule", note=NOTE):
        token = base64.b64encode(f"mrrc:{self.PASSWORD}".encode()).decode("ascii")
        payload = json.dumps({"bundle_id": self.BUNDLE, "action": action,
                              "note": note}).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/board/decisions", data=payload, method="POST",
            headers={"Authorization": f"Basic {token}",
                     "Content-Type": "application/json",
                     "X-MRRC-Decision": "1"})
        with self.opener.open(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_web_schedule_goes_pending_applied_published_acknowledged(self):
        # 1. The operator's schedule lands on the durable server queue (201),
        #    and the card is pending there while the Mac state is untouched.
        status, body = self._post_decision()
        self.assertEqual(status, 201)
        decision_id = body["decision_id"]
        pending = self._get_json("/api/board/decisions")
        self.assertEqual([d["decision_id"] for d in pending["decisions"]],
                         [decision_id])
        self.assertEqual(self._local_status(), "backlog")

        # 2. The real sync round: pull → apply → publish → acknowledge.
        result = autopilot.sync_board_decisions()

        # 3. Local scheduled: the Mac authority applied the transition + note.
        self.assertEqual(result, [(decision_id, "applied")])
        entry = self._local()
        self.assertEqual(entry["status"], "scheduled")
        self.assertEqual(entry["note"], self.NOTE)

        # 4. Rendered scheduled: the shipped page carries the card in that column.
        page = autopilot.BOARD_PAGE_PATH.read_text(encoding="utf-8")
        column = page.split("<div class='col' id='scheduled'>", 1)[1] \
                     .split("<div class='col'", 1)[0]
        self.assertIn(self.BUNDLE, column)

        # 5. Both shippings went through the mocked seam: page + state mirror.
        shipped = {(call.args[0], call.args[2])
                   for call in self.ship_mock.call_args_list}
        self.assertEqual(self.ship_mock.call_count, 2)
        self.assertIn((autopilot.BOARD_PAGE_PATH, autopilot.REMOTE_BOARD_PAGE),
                      shipped)
        self.assertIn((autopilot.BOARD_STATE_FILE, autopilot.REMOTE_BOARD_STATE),
                      shipped)

        # 6. Queue empty: the acknowledgement really deleted the server-side item.
        self.assertEqual(self._get_json("/api/board/decisions")["decisions"], [])
        self.assertFalse(autopilot._unpublished_marker_path().exists())

        # 7. A follow-up round over the same live receiver is a quiet no-op:
        #    nothing applied, nothing shipped, nothing acked.
        self.assertEqual(autopilot.sync_board_decisions(), [])
        self.assertEqual(self.ship_mock.call_count, 2)
        self.assertEqual(self._get_json("/api/board/decisions")["decisions"], [])

    def _local(self):
        state = autopilot.board.load_state(autopilot.BOARD_STATE_FILE)
        return state.get(self.BUNDLE) or {}

    def _local_status(self):
        return self._local().get("status")


if __name__ == "__main__":
    unittest.main()
