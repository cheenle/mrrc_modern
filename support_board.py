#!/usr/bin/env python3
"""FDE loop board (AD: field-driven engineering, loop practice 1).

The support pipeline (support_autopilot.py) classifies every field report as
bug / feature / noise.  This module owns the kanban that turns classified
reports into scheduled work and, after the unattended implementation stage,
into committed results:

    Backlog → Scheduled → In Progress → Done/Failed, plus Rejected

Lifecycle owners:
  - analysis      support_autopilot.py (records kind/severity from the model)
  - scheduling    the operator, via dev_tools/support_board.py decide
  - implementation support_autopilot.py run_implementation (pi proposes a
                  unified-diff patch as a JSON contract; the pipeline applies
                  it ONLY to a dedicated fde/<id> branch and commits only if
                  the full unittest suite passes — main is never touched by an
                  unattended process)

Stdlib only, no app imports beyond support_answers for redact_public — keeps
it unit-testable and hot-fixable like the rest of the support chain.
"""
import html
import json
import os
import subprocess
import time
from pathlib import Path

from support_answers import redact_public  # same privacy filter as the answers page

KINDS = ("bug", "feature", "noise")
STATUSES = ("backlog", "scheduled", "in_progress", "answered", "done", "failed", "rejected")
# Classification IS the scheduling decision (operator policy, 2026-09-19):
#   noise   → answered  (环境/误报：答复页已回，无代码工作，看板终态)
#   bug     → scheduled (自动排期，后台实施接管)
#   feature → backlog   (排期待决策：由操作员 schedule/reject)
# A closed outcome (answered/scheduled) requires the answer to be publishable
# (not need_more_info): an un-answered report stays in the backlog for the
# operator instead of pretending the loop closed.
PUBLISHABLE_STATUSES = ("answered", "needs_fix")
PROTECTED_PATHS = ("static/ft710_main.js", "static/ft710_ui.js",
                   "certs/", "win/", "packaging/", ".iss")
MAX_PATCH_FILES = 8

BOARD_STATE_NAME = "board_state.json"
BOARD_PAGE_NAME = "index.html"


def board_state_path(repo: Path) -> Path:
    return repo / "dist" / "support_autopilot" / BOARD_STATE_NAME


def load_state(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, path)


def normalize_kind(kind: "str | None") -> str:
    kind = str(kind or "").strip().lower()
    return kind if kind in KINDS else ""


def record(state: dict, bundle_id: str, analysis: dict) -> bool:
    """Create/refresh a board entry from a finished analysis.

    Returns True when the entry changed.  Classification IS the scheduling
    decision: noise closes as answered, bug auto-schedules into the
    unattended implementation queue, feature waits in the backlog for the
    operator.  A closed outcome additionally requires a publishable answer
    (need_more_info stays in the backlog) and operator-owned stages are
    never reset by re-analysis.
    """
    entry = state.get(bundle_id) or {}
    if entry and entry.get("status") != "backlog":
        return False  # past the decision point: scheduling/implementation results stand
    kind = normalize_kind(analysis.get("kind"))
    if not kind and analysis.get("needs_code_change"):
        kind = "bug"
    if not kind:
        kind = "noise"
    closed = str(analysis.get("status", "")) in PUBLISHABLE_STATUSES
    if kind == "noise":
        status = "answered" if closed else "backlog"
    elif kind == "bug":
        status = "scheduled" if closed else "backlog"
    else:
        status = "backlog"
    new = {
        "kind": kind,
        "severity": str(analysis.get("severity", "")).strip().lower(),
        "title": str(analysis.get("title", "") or analysis.get("verdict", ""))[:120],
        "status": status,
        "needs_code_change": bool(analysis.get("needs_code_change")),
        "code_hint": str(analysis.get("code_hint", ""))[:200],
        "updated": time.strftime("%Y-%m-%d %H:%M"),
        "note": str(entry.get("note", "")) or (
            "自动分类：环境/误报，答复页已回" if status == "answered" else ""),
    }
    if entry == new:
        return False
    state[bundle_id] = new
    return True


def decide(state: dict, bundle_id: str, action: str, note: str = "") -> str:
    """Operator scheduling decision.  action: schedule|reject|backlog."""
    entry = state.get(bundle_id)
    if not entry:
        return f"未知条目：{bundle_id}"
    if action == "schedule":
        entry["status"] = "scheduled"
    elif action == "reject":
        entry["status"] = "rejected"
    elif action == "backlog":
        entry["status"] = "backlog"
    else:
        return f"未知动作：{action}"
    entry["updated"] = time.strftime("%Y-%m-%d %H:%M")
    if note:
        entry["note"] = note[:200]
    return f"{bundle_id} → {entry['status']}"


def begin_implementation(state: dict, bundle_id: str) -> bool:
    entry = state.get(bundle_id)
    if not entry or entry.get("status") != "scheduled":
        return False
    entry["status"] = "in_progress"
    entry["updated"] = time.strftime("%Y-%m-%d %H:%M")
    return True


def finish_implementation(state: dict, bundle_id: str, ok: bool, detail: str,
                          branch: str = "", commit: str = "") -> None:
    entry = state.get(bundle_id)
    if not entry:
        return
    entry["status"] = "done" if ok else "failed"
    entry["result"] = detail[:400]
    entry["branch"] = branch
    entry["commit"] = commit
    entry["updated"] = time.strftime("%Y-%m-%d %H:%M")


# ── implementation gate helpers (called from support_autopilot) ────────────
def guard_check(patch: str) -> str:
    """Return a refusal reason, or '' when the patch may proceed.

    Blocks: protected files (tooling guards, certs, packaging), oversized
    changes, and anything the patch contract did not fill in.
    """
    if not patch.strip():
        return "patch 为空"
    files = []
    for line in patch.splitlines():
        if line.startswith(("--- a/", "--- ", "+++ b/", "+++ ")) \
                and "/dev/null" not in line:
            path = line.split(maxsplit=1)[-1]
            path = path[2:] if path.startswith(("a/", "b/")) else path
            if path not in files:
                files.append(path)
    if len(files) > MAX_PATCH_FILES:
        return f"改动 {len(files)} 个文件，超过单飞上限 {MAX_PATCH_FILES}"
    for f in files:
        for guard in PROTECTED_PATHS:
            if f.startswith(guard) or f == guard.rstrip("/"):
                return f"受保护路径：{f}"
    return ""


def repo_clean(repo: Path, branch_prefix: str = "fde/") -> tuple[bool, str]:
    """The unattended implementation only runs on a clean main worktree."""
    try:
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                               capture_output=True, text=True).stdout.strip()
        if dirty:
            return False, "工作区不干净（有未提交改动）——无人值守实施只在干净工作区运行"
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                cwd=repo, capture_output=True, text=True).stdout.strip()
        if branch != "main":
            return False, f"当前分支 {branch!r} 不是 main——实施只从 main 拉出 fde/ 分支"
        return True, ""
    except OSError as e:
        return False, f"git 不可用：{e}"


# ── rendering ──────────────────────────────────────────────────────────────
COLUMNS = ("backlog", "scheduled", "in_progress", "answered", "done", "failed", "rejected")
COLUMN_TITLES = {
    "backlog": "待决策（新需求）", "scheduled": "已排期（bug 自动）",
    "in_progress": "实施中", "answered": "已答复（无需代码）",
    "done": "已提交（fde/ 分支）",
    "failed": "实施失败（已回滚）", "rejected": "不处理",
}


def _card(bundle_id: str, e: dict) -> str:
    kind = e.get("kind", "")
    sev = e.get("severity", "")
    badge = f"<span class='k k-{kind}'>{kind or '未分类'}</span>"
    if sev:
        badge += f"<span class='s s-{sev}'>{sev}</span>"
    branch = e.get("branch", "")
    commit = e.get("commit", "")
    result = e.get("result", "")
    hint = e.get("code_hint", "")
    note = e.get("note", "")
    parts = [f"<div class='card' id='{html.escape(bundle_id)}'>"]
    parts.append(f"<div class='tags'>{badge}<code>{html.escape(bundle_id)}</code>"
                 f"<span class='t'>{html.escape(e.get('updated', ''))}</span></div>")
    parts.append(f"<p class='title'>{html.escape(redact_public(e.get('title', '')))}</p>")
    if hint:
        parts.append(f"<p class='hint'>{html.escape(redact_public(hint))}</p>")
    if result:
        parts.append(f"<p class='result'>{html.escape(redact_public(result))}</p>")
    if branch:
        parts.append(f"<p class='branch'><code>{html.escape(branch)}"
                     f"{('@' + commit[:8]) if commit else ''}</code></p>")
    if note:
        parts.append(f"<p class='note'>决策备注：{html.escape(redact_public(note))}</p>")
    parts.append("</div>")
    return "\n".join(parts)


def render_board(state: dict, generated_at: str = "") -> str:
    cols = []
    for col in COLUMNS:
        entries = sorted(((k, v) for k, v in state.items() if v.get("status") == col),
                         key=lambda kv: kv[1].get("updated", ""), reverse=True)
        cards = "\n".join(_card(k, v) for k, v in entries) or \
            "<p class='empty'>—</p>"
        cols.append(
            f"<div class='col' id='{col}'><h2>{COLUMN_TITLES[col]}"
            f"<span class='n'>{len(entries)}</span></h2>{cards}</div>")
    counts = {k: sum(1 for v in state.values() if v.get("status") == k)
              for k in COLUMNS}
    body = "\n".join(cols)
    stamp = generated_at or time.strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>MRRC Modern · FDE 看板</title>
<style>
body{{background:#101018;color:#e2e2e8;font-family:-apple-system,tahoma,sans-serif;margin:0;padding:20px;max-width:1400px}}
h1{{font-size:21px;margin:0 0 4px}}
.muted{{color:#8a8a96;font-size:13px;margin:0 0 18px}}
.board{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}
@media(max-width:640px){{.board{{grid-template-columns:1fr}}}}
.col{{background:#17171f;border:1px solid #262633;border-radius:10px;padding:10px;min-height:120px}}
.col h2{{font-size:13px;margin:2px 4px 10px;color:#c9c9d4}}
.col h2 .n{{float:right;color:#6f6f80}}
.card{{background:#1e1e29;border:1px solid #2c2c3a;border-radius:8px;padding:9px;margin-bottom:10px;font-size:13px}}
.card code{{color:#9fb4c7;font-size:11px}}
.tags{{margin-bottom:6px}}
.title{{margin:2px 0 4px;line-height:1.5}}
.hint,.result,.note,.branch{{margin:4px 0 0;color:#9a9aa8;font-size:12px;line-height:1.5}}
.result{{color:#c7b98a}}
.k,.s{{display:inline-block;border-radius:4px;padding:0 6px;font-size:11px;margin-right:4px}}
.k-bug{{background:#4a2230;color:#ff9db1}}.k-feature{{background:#1f3a2c;color:#8fe3b0}}
.k-noise{{background:#2b2b36;color:#9a9aa8}}.k-未分类{{background:#2b2b36;color:#9a9aa8}}
.s-high{{background:#4a2230;color:#ff9db1}}.s-med{{background:#46361c;color:#f2ce7b}}
.s-low{{background:#1f3a2c;color:#8fe3b0}}
.empty{{color:#5c5c68;text-align:center}}
</style>
</head>
<body>
<h1>🐞 FDE 看板 — 上报 → 分类 → 排期 → 后台迭代</h1>
<p class="muted">分类即决策：<b>noise</b> → 已答复闭环（无需代码）· <b>bug</b> → 自动排期进入无人值守实施（独立 <code>fde/</code> 分支 + 全量测试绿才提交，main 不受影响）· <b>feature</b> → 待决策（操作员排期）。
共 {len(state)} 条 · 待决 {counts['backlog']} · 已排期 {counts['scheduled']} · 已答复 {counts['answered']} · 已提交 {counts['done']} · 最后更新 {stamp}（自动生成，勿手改）</p>
<div class="board">
{body}
</div>
</body>
</html>"""
