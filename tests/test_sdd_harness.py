"""
Tests for the SDD-Guardian context harness (.agents/skills/sdd-guardian).

Validates the constraint registry is well-formed, the context map covers the
core modules, and the CLI enforces block-level rules (check + PreToolUse hook
modes). Runs without hardware — the harness is stdlib-only Python.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS = REPO_ROOT / ".agents" / "skills" / "sdd-guardian" / "harness"
CLI = HARNESS / "sdd_context.py"
REGISTRY = HARNESS / "constraints.json"


def run_cli(*args, stdin: str | None = None) -> subprocess.CompletedProcess:
    import os
    env = os.environ.copy()
    # The CLI prints Chinese SDD text; force UTF-8 on both ends so the tests
    # also pass on cp936/GBK-locale Windows (default encoding otherwise).
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        input=stdin, capture_output=True, text=True, cwd=REPO_ROOT,
        encoding="utf-8", env=env,
    )


class ConstraintRegistryTests(unittest.TestCase):
    """constraints.json must stay well-formed — the hook depends on it."""

    @classmethod
    def setUpClass(cls):
        cls.reg = json.loads(REGISTRY.read_text(encoding="utf-8"))

    def test_required_fields_and_unique_ids(self):
        ids = set()
        for rule in self.reg["rules"]:
            for field in ("id", "title", "severity", "sdd_ref", "scope", "message"):
                self.assertIn(field, rule, f"rule missing {field}: {rule.get('id')}")
            self.assertNotIn(rule["id"], ids, f"duplicate rule id {rule['id']}")
            ids.add(rule["id"])

    def test_severities_valid(self):
        for rule in self.reg["rules"]:
            self.assertIn(rule["severity"], ("block", "warn", "info"))

    def test_patterns_compile(self):
        import re
        for rule in self.reg["rules"]:
            for pat in rule.get("patterns", []):
                re.compile(pat)  # raises on invalid regex

    def test_context_map_covers_core_modules(self):
        globs = [g for entry in self.reg["context_map"] for g in entry["globs"]]
        for core in ("server.py", "cat_controller.py", "poll_scheduler.py",
                     "radio_state.py", "config.py", "audio_handler.py"):
            self.assertIn(core, globs, f"context map missing {core}")

    def test_every_rule_has_sdd_traceability(self):
        for rule in self.reg["rules"]:
            ref = rule["sdd_ref"]
            self.assertTrue(
                any(t in ref for t in ("AD-", "§", "Ch", "AGENTS", "V1.", "README")),
                f"{rule['id']} has no SDD trace: {ref!r}",
            )


class HarnessCliTests(unittest.TestCase):
    """CLI behavior: context lookup, check enforcement, hook blocking."""

    def test_prime_prints_golden_rules(self):
        r = run_cli("prime")
        self.assertEqual(r.returncode, 0)
        self.assertIn("GOLDEN RULES", r.stdout)
        self.assertIn("cat-no-dn", r.stdout)

    def test_context_for_server_py(self):
        r = run_cli("context", "server.py")
        self.assertEqual(r.returncode, 0)
        self.assertIn("AD-001", r.stdout)
        self.assertIn("ws-endpoint-auth", r.stdout)

    def test_context_for_cat_controller(self):
        r = run_cli("context", "cat_controller.py")
        self.assertIn("AD-014", r.stdout)
        self.assertIn("cat-sh-format", r.stdout)

    def test_check_blocks_dn_command(self):
        with tempfile_named("query(ser, \"DN\")\n") as p:
            r = run_cli("check", p)
        self.assertEqual(r.returncode, 2)
        self.assertIn("cat-no-dn", r.stderr)

    def test_check_blocks_sh0nn_format(self):
        with tempfile_named('cmd = f"SH0{idx:02d}"\n') as p:
            r = run_cli("check", p)
        self.assertEqual(r.returncode, 2)
        self.assertIn("cat-sh-format", r.stderr)

    def test_check_passes_clean_code(self):
        with tempfile_named('cmd = f"SH00{idx:02d}"\nawait cat.set(cmd)\n') as p:
            r = run_cli("check", p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("clean", r.stdout)

    def test_hook_blocks_dn_edit(self):
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {
                "path": str(REPO_ROOT / "cat_controller.py"),
                "new_string": 'resp = await self.query("DN", timeout=timeout)',
            },
        }
        r = run_cli("hook", stdin=json.dumps(payload))
        self.assertEqual(r.returncode, 2)
        self.assertIn("cat-no-dn", r.stderr)

    def test_hook_allows_clean_edit(self):
        payload = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Edit",
            "tool_input": {
                "path": str(REPO_ROOT / "cat_controller.py"),
                "new_string": 'cmd = f"SH00{index:02d}"',
            },
        }
        r = run_cli("hook", stdin=json.dumps(payload))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_hook_fail_open_on_garbage_stdin(self):
        r = run_cli("hook", stdin="not json")
        self.assertEqual(r.returncode, 0)

    def test_repo_core_files_are_clean(self):
        """The harness must not cry wolf: shipped core modules pass block rules."""
        core = ["server.py", "cat_controller.py", "poll_scheduler.py",
                "radio_state.py", "config.py", "audio_handler.py",
                "audio_resample.py", "opus_rx.py"]
        r = run_cli("check", *core)
        self.assertEqual(r.returncode, 0,
                         f"core files trip block rules:\n{r.stderr}")


class KnowledgeIndexTests(unittest.TestCase):
    """index.json routes into the SDD; every ref must resolve to live content."""

    @classmethod
    def setUpClass(cls):
        cls.idx = json.loads((HARNESS / "index.json").read_text(encoding="utf-8"))

    def test_all_chapter_files_exist(self):
        for num, ch in self.idx["chapters"].items():
            self.assertTrue((REPO_ROOT / ch["file"]).is_file(),
                            f"chapter {num} file missing: {ch['file']}")

    def test_every_topic_ref_resolves(self):
        """All routed refs (ad/nfr/uc/risk/issue/sc/assume/sec) slice real SDD text."""
        import subprocess as sp
        for topic in self.idx["topics"]:
            for ref in topic["refs"]:
                r = run_cli("sdd", ref.split(":", 1)[1])
                self.assertEqual(r.returncode, 0,
                                 f"topic {topic['id']} has dangling ref {ref}")

    def test_topics_have_keywords_or_globs(self):
        for topic in self.idx["topics"]:
            self.assertTrue(topic["globs"] or topic["keywords"],
                            f"topic {topic['id']} is unreachable")
            self.assertTrue(topic["refs"], f"topic {topic['id']} routes nowhere")

    def test_knowledge_coverage_of_core_areas(self):
        ids = {t["id"] for t in self.idx["topics"]}
        for area in ("cat-serial", "polling-state", "ptt-tx-safety",
                     "audio-pipeline", "scope-spectrum", "frontend-ui",
                     "auth-security", "project-scope"):
            self.assertIn(area, ids)


class KnowledgeCliTests(unittest.TestCase):
    """brief/sdd commands surface requirements, decisions, feasibility — not just rules."""

    def test_sdd_extracts_architecture_decision(self):
        r = run_cli("sdd", "AD-011")
        self.assertEqual(r.returncode, 0)
        self.assertIn("44.1", r.stdout)
        self.assertIn("Rationale", r.stdout)

    def test_sdd_extracts_nfr_row_with_context(self):
        r = run_cli("sdd", "NFR-060")
        self.assertEqual(r.returncode, 0)
        self.assertIn("NFR-060", r.stdout)
        self.assertIn("5.7", r.stdout)  # enclosing section header included

    def test_sdd_extracts_use_case(self):
        r = run_cli("sdd", "UC-005")
        self.assertEqual(r.returncode, 0)
        self.assertIn("UC-005", r.stdout)

    def test_sdd_extracts_open_issue(self):
        r = run_cli("sdd", "I6")
        self.assertIn("multi-client", r.stdout)

    def test_sdd_extracts_chapter_section(self):
        r = run_cli("sdd", "9.6")
        self.assertEqual(r.returncode, 0)
        self.assertIn("CAT Polling Architecture", r.stdout)

    def test_brief_includes_decisions_requirements_and_risks(self):
        r = run_cli("brief", "audio_handler.py")
        self.assertEqual(r.returncode, 0)
        self.assertIn("AD-011", r.stdout)     # architecture decision
        self.assertIn("NFR-060", r.stdout)    # requirement
        self.assertIn("R3", r.stdout)         # feasibility risk
        self.assertIn("Constraints", r.stdout)

    def test_brief_task_chinese_keywords(self):
        r = run_cli("brief", "--task", "增加一个新的 CAT 命令")
        self.assertEqual(r.returncode, 0)
        self.assertIn("AD-002", r.stdout)

    def test_brief_poll_scheduler_has_stale_guard_and_ad009(self):
        r = run_cli("brief", "poll_scheduler.py")
        self.assertIn("poll-stale-guard", r.stdout)
        self.assertIn("AD-009", r.stdout)


class TraceCommandTests(unittest.TestCase):
    """trace: spec/plan ↔ SDD citation audit (Superpowers bridge)."""

    def _write_spec(self, name: str, content: str) -> str:
        p = REPO_ROOT / name
        p.write_text(content, encoding="utf-8")
        self.addCleanup(p.unlink)
        return str(p)

    def test_trace_reports_missing_expected_refs(self):
        spec = self._write_spec(
            "_trace_test_spec.md",
            "# Filter redesign\n\nTouches cat_controller.py and poll_scheduler.py.\n",
        )
        r = run_cli("trace", spec)
        self.assertEqual(r.returncode, 0)
        self.assertIn("cat_controller.py", r.stdout)
        self.assertIn("expected but NOT cited", r.stdout)
        self.assertIn("AD-002", r.stdout)  # cat-serial topic ref

    def test_trace_acknowledges_cited_refs(self):
        spec = self._write_spec(
            "_trace_test_spec2.md",
            "# CAT change\n\nImplements AD-002 and AD-014 for cat_controller.py.\n",
        )
        r = run_cli("trace", spec)
        self.assertIn("AD-002", r.stdout)
        self.assertIn("AD-014", r.stdout)
        # Both cited → must not appear in the missing list
        missing = r.stdout.split("expected but NOT cited")[-1] \
            if "expected but NOT cited" in r.stdout else ""
        self.assertNotIn("AD-002", missing)

    def test_trace_handles_missing_file(self):
        r = run_cli("trace", "no/such/spec.md")
        self.assertEqual(r.returncode, 0)
        self.assertIn("not a file", r.stderr)

    def test_trace_ignores_version_numbers(self):
        """Version strings like 1.0 / V1.7 are not § citations — only §X.Y counts."""
        spec = self._write_spec(
            "_trace_test_spec3.md",
            "# Notes\n\nServer V1.7, config 1.0, runtime 3.12 — no SDD refs here.\n",
        )
        r = run_cli("trace", spec)
        self.assertIn("SDD refs cited (0)", r.stdout)
        self.assertIn("nothing to verify", r.stdout)


from contextlib import contextmanager

@contextmanager
def tempfile_named(content: str):
    """Write content to a temp .py file inside the repo (glob scope applies)."""
    p = REPO_ROOT / "_sdd_harness_test_tmp.py"
    try:
        p.write_text(content, encoding="utf-8")
        yield str(p)
    finally:
        p.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
