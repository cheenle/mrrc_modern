"""Support autopilot CLI (spec 2026-09-17-support-autopilot §3/§8).

No network, no model, no git: the receiver, `pi`, git and rsync are all faked, so
these tests run anywhere.  Loaded by path because dev_tools is not a package.
"""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "support_autopilot", ROOT / "dev_tools" / "support_autopilot.py")
assert _spec is not None and _spec.loader is not None
autopilot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(autopilot)

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
        self.patches = [
            mock.patch.object(autopilot, "STATE_FILE", root / "state.json"),
            mock.patch.object(autopilot, "INBOX", root / "inbox"),
            mock.patch.object(autopilot, "DRAFTS", root / "drafts"),
            mock.patch.object(autopilot, "ANSWERS_PAGE", root / "answers" / "index.html"),
            mock.patch.object(autopilot, "RUN_LOG", root / "run.log"),
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


class CronTests(AutopilotFixture):
    def test_cron_line_has_absolute_paths_and_the_marker(self):
        line = autopilot.cron_line()
        self.assertIn(str(autopilot.REPO / "dev_tools" / "support_autopilot.py"), line)
        self.assertIn(autopilot.CRON_MARK, line)
        self.assertIn("--once --publish", line)
        self.assertIn("/opt/homebrew/bin", line)          # cron PATH is nearly empty
        self.assertIn(str(autopilot.RUN_LOG), line)

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


if __name__ == "__main__":
    unittest.main()
