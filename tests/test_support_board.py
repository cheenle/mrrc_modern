"""Tests for the FDE loop board (support_board.py).

Covers the classification/projection contract (record/decide/implementation
state machine), the unattended-implementation guard rails (protected paths,
size cap, clean-worktree gate) and the board page rendering (columns, counts,
HTML escaping, public-redaction).
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import support_board as board


def _analysis(**over):
    base = {"verdict": "结论", "status": "answered", "category": "其他",
            "needs_code_change": False, "code_hint": "server.py:do_x",
            "solution": ["做某事"], "diagnosis": ["依据"]}
    base.update(over)
    return base


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.state = {}

    def test_classification_is_the_decision(self):
        """Operator policy (2026-09-19): noise → answered (closed), bug →
        scheduled (auto-implement queue), feature → backlog (waits for the
        operator)."""
        state = {}
        board.record(state, "N", _analysis(kind="noise"))
        board.record(state, "B", _analysis(kind="bug"))
        board.record(state, "F", _analysis(kind="feature"))
        self.assertEqual(state["N"]["status"], "answered")
        self.assertEqual(state["B"]["status"], "scheduled")
        self.assertEqual(state["F"]["status"], "backlog")
        self.assertIn("自动分类", state["N"]["note"])

    def test_need_more_info_stays_in_backlog(self):
        """An un-answered report must not pretend the loop closed: without a
        publishable answer (need_more_info) even noise/bug stay in the backlog
        for the operator."""
        state = {}
        board.record(state, "N", _analysis(kind="noise", status="need_more_info"))
        board.record(state, "B", _analysis(kind="bug", status="need_more_info"))
        self.assertEqual(state["N"]["status"], "backlog")
        self.assertEqual(state["B"]["status"], "backlog")
        self.assertNotIn("答复页已回", state["N"]["note"])

    def test_kind_from_model_wins(self):
        self.assertTrue(board.record(self.state, "B1", _analysis(kind="feature")))
        self.assertEqual(self.state["B1"]["kind"], "feature")
        self.assertEqual(self.state["B1"]["status"], "backlog")

    def test_needs_code_change_defaults_to_bug(self):
        board.record(self.state, "B2", _analysis(needs_code_change=True))
        self.assertEqual(self.state["B2"]["kind"], "bug")

    def test_noise_fallback_when_no_signal(self):
        board.record(self.state, "B3", _analysis())
        self.assertEqual(self.state["B3"]["kind"], "noise")

    def test_unknown_kind_becomes_noise(self):
        board.record(self.state, "B4", _analysis(kind="foo"))
        self.assertEqual(self.state["B4"]["kind"], "noise")

    def test_operator_owned_stages_never_reset(self):
        for status in ("scheduled", "in_progress", "done", "answered", "failed",
                       "rejected"):
            state = {f"B-{status}": {"status": status, "kind": "bug"}}
            self.assertFalse(board.record(state, f"B-{status}",
                                         _analysis(kind="noise")))

    def test_re_record_with_same_analysis_is_idempotent(self):
        board.record(self.state, "B5", _analysis(kind="bug"))
        snapshot = json.dumps(self.state, sort_keys=True)
        self.assertFalse(board.record(self.state, "B5", _analysis(kind="bug")))


class DecideTests(unittest.TestCase):
    def setUp(self):
        self.state = {"B1": {"status": "backlog", "kind": "bug"}}

    def test_schedule_reject_backlog(self):
        for action, expected in (("schedule", "scheduled"),
                                 ("reject", "rejected"),
                                 ("backlog", "backlog")):
            board.decide(self.state, "B1", action)
            self.assertEqual(self.state["B1"]["status"], expected)

    def test_unknown_id_and_action(self):
        self.assertIn("未知", board.decide(self.state, "NOPE", "schedule"))
        self.assertIn("未知动作", board.decide(self.state, "B1", "ship"))

    def test_note_recorded(self):
        board.decide(self.state, "B1", "schedule", note="v1.19 一起走")
        self.assertEqual(self.state["B1"]["note"], "v1.19 一起走")


class ImplementationStateMachineTests(unittest.TestCase):
    def test_begin_only_from_scheduled(self):
        state = {"B1": {"status": "scheduled"}, "B2": {"status": "backlog"}}
        self.assertTrue(board.begin_implementation(state, "B1"))
        self.assertEqual(state["B1"]["status"], "in_progress")
        self.assertFalse(board.begin_implementation(state, "B2"))

    def test_finish_done_and_failed(self):
        state = {"B1": {"status": "in_progress"}}
        board.finish_implementation(state, "B1", True, "测试 OK",
                                    branch="fde/B1", commit="abc1234")
        self.assertEqual(state["B1"]["status"], "done")
        self.assertEqual(state["B1"]["commit"], "abc1234")
        board.finish_implementation(state, "B1", False, "测试未绿")
        self.assertEqual(state["B1"]["status"], "failed")


class GuardCheckTests(unittest.TestCase):
    def test_empty_patch_refused(self):
        self.assertTrue(board.guard_check(""))
        self.assertTrue(board.guard_check("   \n"))

    def test_protected_paths_refused(self):
        for path in ("static/ft710_main.js", "static/ft710_ui.js",
                     "certs/server.crt", "packaging/macos/build.sh",
                     "win/MRRC-Modern.iss"):
            patch = f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-x\n+y\n"
            self.assertIn("受保护", board.guard_check(patch))

    def test_file_cap(self):
        patch = "\n".join(
            f"--- a/f{i}.py\n+++ b/f{i}.py\n@@ -1 +1 @@\n-x\n+y\n" for i in range(9))
        self.assertIn("上限", board.guard_check(patch))

    def test_normal_patch_passes(self):
        patch = "--- a/server.py\n+++ b/server.py\n@@ -1 +1 @@\n-x\n+y\n"
        self.assertEqual(board.guard_check(patch), "")


class RepoCleanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The build VM ships without git; the worktree gate only ever runs on
        # a real checkout, so skip instead of failing the Windows build gate.
        if shutil.which("git") is None:
            raise unittest.SkipTest("git is not installed")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._run("git", "init", "-q")
        self._run("git", "config", "user.email", "t@t")
        self._run("git", "config", "user.name", "t")
        (self.tmp / "a.txt").write_text("x", encoding="utf-8")
        self._run("git", "add", "-A")
        self._run("git", "commit", "-qm", "init")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)   # POSIX `rm -rf` does not exist on Windows

    def _run(self, *args):
        subprocess.run(args, cwd=self.tmp, capture_output=True, check=True)

    def test_clean_main_ok(self):
        ok, why = board.repo_clean(self.tmp)
        self.assertTrue(ok, why)
        self.assertEqual(why, "")

    def test_dirty_worktree_refused(self):
        (self.tmp / "b.txt").write_text("y", encoding="utf-8")
        ok, why = board.repo_clean(self.tmp)
        self.assertFalse(ok)
        self.assertIn("不干净", why)

    def test_non_main_branch_refused(self):
        self._run("git", "checkout", "-qb", "feature")
        ok, why = board.repo_clean(self.tmp)
        self.assertFalse(ok)
        self.assertIn("main", why)


class RenderBoardTests(unittest.TestCase):
    def test_state_roundtrip_and_columns(self):
        state = {}
        board.record(state, "B1", _analysis(kind="bug", severity="high"))
        board.record(state, "B2", _analysis(kind="feature"))
        board.record(state, "N1", _analysis(kind="noise"))
        board.decide(state, "B1", "schedule")
        board.finish_implementation(state, "B1", True, "测试 OK",
                                    branch="fde/B1", commit="abc1234")
        html = board.render_board(state)
        for col in ("待决策（新需求）", "已排期（bug 自动）", "实施中",
                    "已答复（无需代码）", "已提交（fde/ 分支）",
                    "实施失败（已回滚）", "不处理"):
            self.assertIn(col, html)
        self.assertIn("fde/B1", html)
        self.assertIn("abc1234", html)
        # auto-scheduled bug must show in the queue, noise in answered
        self.assertIn("id='scheduled'", html)
        self.assertIn("id='answered'", html)

    def test_title_escaped_and_redacted(self):
        state = {}
        board.record(state, "B1", _analysis(kind="bug",
                                            title="手机 13812345678 <script>x</script>"))
        html = board.render_board(state)
        self.assertNotIn("<script>", html)
        self.assertNotIn("13812345678", html)

    def test_state_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "board_state.json"
            state = {}
            board.record(state, "B1", _analysis(kind="bug"))
            board.save_state(path, state)
            self.assertEqual(board.load_state(path)["B1"]["kind"], "bug")
            self.assertEqual(board.load_state(path.with_suffix(".missing.json")), {})


if __name__ == "__main__":
    unittest.main()
