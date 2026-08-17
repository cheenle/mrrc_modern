#!/usr/bin/env python3
"""SDD context harness for the mrrc_modern codebase.

Two knowledge sources, both living in this directory:
  constraints.json — machine-readable enforcement rules (block/warn/info)
  index.json       — routing index into the SDD knowledge base (requirements,
                     context, architecture decisions, risks, use cases...)

The index holds NO duplicated content: refs are resolved by slicing the live
SDD/*.md files, so this harness can never drift stale from the design record.

Commands:
  prime                          Compact session-start digest (golden rules).
  context [PATHS...] [--task T]  SDD refs + applicable constraints (fast view).
  brief [PATHS...] [--task T]    Full engineering brief: constraints PLUS the
                                 relevant SDD sections (ADs, NFRs, use cases,
                                 risks, issues) extracted live from SDD/.
  sdd <REF|term>                 Print one SDD item (AD-011, NFR-060, UC-005,
                                 R4, I6, SC8, A4, 9.6, ch15) or search terms.
  trace <spec-or-plan.md>        Spec/plan ↔ SDD traceability audit (advisory):
                                 files it references vs SDD refs it should cite.
  check [PATHS...] [--staged]    Pattern-scan content; exit 2 if any block-severity hit.
  hook                           PreToolUse hook mode: read event JSON on stdin,
                                 inspect the pending Edit/Write, exit 2 + reason on block.

Stdlib only — hook latency budget is ~5 s including interpreter startup.
"""
import fnmatch
import json
import re
import subprocess
import sys
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
REGISTRY_PATH = HARNESS_DIR / "constraints.json"
INDEX_PATH = HARNESS_DIR / "index.json"
PROJECT_ROOT = HARNESS_DIR.parents[3]

BRIEF_REF_LINE_CAP = 40     # per extracted SDD section
BRIEF_TOTAL_LINE_CAP = 300  # whole knowledge section


def load_registry() -> dict:
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_index() -> dict:
    with open(INDEX_PATH, encoding="utf-8") as f:
        return json.load(f)


def norm(path: str) -> str:
    """Normalise a path to project-relative POSIX form for glob matching."""
    p = str(path).replace("\\", "/")
    try:
        p = str(Path(p).resolve().relative_to(PROJECT_ROOT)).replace("\\", "/")
    except (ValueError, OSError):
        pass
    return p.lstrip("./")


def glob_match(path: str, pattern: str) -> bool:
    """fnmatch with pragmatic '**' handling: 'a/**/b' also matches 'a/b'."""
    if fnmatch.fnmatch(path, pattern):
        return True
    if "**/" in pattern and fnmatch.fnmatch(path, pattern.replace("**/", "")):
        return True
    return False


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(glob_match(path, g) for g in patterns)


def applicable_rules(reg: dict, path: str | None) -> list[dict]:
    """Rules whose scope matches path (path=None → all rules apply)."""
    out = []
    for rule in reg["rules"]:
        if path is not None:
            if rule["scope"] and not matches_any(path, rule["scope"]):
                continue
            if rule.get("exclude_scope") and matches_any(path, rule["exclude_scope"]):
                continue
        out.append(rule)
    return out


def _strip_comment(path: str | None, line: str) -> str:
    """Remove trailing comments so pattern rules don't fire on prose.

    Naive split (strings containing the comment marker can over-strip) —
    acceptable for a lint harness: patterns never match inside comments,
    and a violation hidden by over-stripping is caught at review.
    """
    if path and path.endswith(".py"):
        return line.split("#", 1)[0]
    if path and path.endswith((".js", ".css")):
        return line.split("//", 1)[0]
    return line


def scan_text(reg: dict, path: str | None, text: str) -> list[dict]:
    """Run block/warn pattern rules against text; return violation dicts."""
    violations = []
    rules = applicable_rules(reg, path)
    lines = text.splitlines()
    for rule in rules:
        if rule["severity"] not in ("block", "warn"):
            continue
        for pat in rule.get("patterns", []):
            rx = re.compile(pat)
            for lineno, line in enumerate(lines, 1):
                if rx.search(_strip_comment(path, line)):
                    violations.append({
                        "id": rule["id"],
                        "severity": rule["severity"],
                        "message": rule["message"],
                        "sdd_ref": rule["sdd_ref"],
                        "path": path or "<content>",
                        "line": lineno,
                        "text": line.strip()[:160],
                    })
    return violations


def print_violations(violations: list[dict], stream) -> None:
    for v in violations:
        print(
            f"[{v['severity'].upper()}] {v['id']} ({v['sdd_ref']}) "
            f"{v['path']}:{v['line']}: {v['text']}\n    → {v['message']}",
            file=stream,
        )


# ── SDD knowledge extraction (live slices from SDD/*.md) ───────────

def _read_chapter(idx: dict, num: str) -> tuple[str, list[str]] | None:
    ch = idx["chapters"].get(str(num))
    if not ch:
        return None
    p = PROJECT_ROOT / ch["file"]
    if not p.is_file():
        return None
    return ch["file"], p.read_text(encoding="utf-8").splitlines()


def _slice_heading(lines: list[str], heading_rx: str, level: str) -> list[str]:
    """Slice from the heading matching heading_rx to the next heading of the
    same or higher level (level='##' matches '## ' starts; '###' matches '### '
    and '## ' stops; '#' stops on any heading)."""
    start = None
    for i, line in enumerate(lines):
        if re.match(heading_rx, line):
            start = i
            break
    if start is None:
        return []
    stop = len(lines)
    for j in range(start + 1, len(lines)):
        l = lines[j]
        if level == "#":
            if l.startswith("#"):
                stop = j
                break
        elif level == "##":
            if l.startswith("## ") or l.startswith("# "):
                stop = j
                break
        else:  ### stops at ###, ##, #
            if l.startswith("### ") or l.startswith("## ") or l.startswith("# "):
                stop = j
                break
    return lines[start:stop]


def _extract_row(lines: list[str], row_id: str) -> list[str]:
    """Extract a table row by its first-column ID, with the enclosing
    section heading and table header for readability."""
    for i, line in enumerate(lines):
        if line.startswith(f"| {row_id} ") or line.startswith(f"| {row_id}|"):
            section = None
            header = None
            for j in range(i - 1, -1, -1):
                lj = lines[j]
                if header is None and lj.startswith("| ID"):
                    header = lj
                if lj.startswith("## ") and not lj.startswith("###"):
                    section = lj
                    break
            out = []
            if section:
                out.append(section)
            if header:
                out.append(header)
            out.append(line)
            return out
    return []


def resolve_ref(idx: dict, ref: str) -> dict | None:
    """Resolve a typed ref (ad:/nfr:/uc:/risk:/issue:/sc:/assume:/sec:/ch:)
    to extracted SDD content. Returns {ref, file, lines} or None."""
    if ":" not in ref:
        return None
    kind, ident = ref.split(":", 1)
    kind = kind.strip().lower()
    ident = ident.strip()
    chapter_for = {
        "ad": "8", "uc": "6", "nfr": "5", "risk": "13",
        "issue": "13", "assume": "13", "sc": "3",
    }
    if kind in ("sec", "ch"):
        num = ident.split(".")[0]
        got = _read_chapter(idx, num)
        if not got:
            return None
        file, lines = got
        if kind == "ch" or "." not in ident:
            body = lines  # whole chapter
        else:
            body = _slice_heading(lines, rf"^##\s+{re.escape(ident)}(\s|$)", "##")
            if not body:
                return None
        return {"ref": ref, "file": file, "lines": body}
    ch_num = chapter_for.get(kind)
    if not ch_num:
        return None
    got = _read_chapter(idx, ch_num)
    if not got:
        return None
    file, lines = got
    if kind == "ad":
        body = _slice_heading(lines, rf"^##\s+{re.escape(ident)}\b", "##")
    elif kind == "uc":
        body = _slice_heading(lines, rf"^###\s+{re.escape(ident)}\b", "###")
    else:  # nfr / risk / issue / assume / sc → table rows
        body = _extract_row(lines, ident)
    if not body:
        return None
    return {"ref": ref, "file": file, "lines": body}


def topics_for_path(idx: dict, path: str) -> list[dict]:
    return [t for t in idx["topics"] if t["globs"] and matches_any(path, t["globs"])]


def topics_for_task(idx: dict, task: str) -> list[dict]:
    words = {w.lower() for w in re.findall(r"[A-Za-z0-9_\-\u4e00-\u9fff]{2,}", task)}
    out = []
    for t in idx["topics"]:
        hay = {t["id"].lower()} | {k.lower() for k in t["keywords"]}
        if words & hay:
            out.append(t)
    return out


def _print_extracted(item: dict, cap: int = BRIEF_REF_LINE_CAP) -> int:
    """Print one resolved ref; returns lines printed."""
    lines = item["lines"]
    shown = lines[:cap]
    print(f"┌─ {item['ref']}  ({item['file']})")
    n = 0
    for l in shown:
        print("│ " + l)
        n += 1
    if len(lines) > cap:
        print(f"│ … ({len(lines) - cap} more lines — read {item['file']})")
        n += 1
    print("└────")
    return n


def _constraints_block(reg: dict, path: str) -> None:
    rules = applicable_rules(reg, path)
    if not rules:
        return
    print("  Constraints:")
    for r in rules:
        tag = {"block": "✗", "warn": "!", "info": "i"}[r["severity"]]
        print(f"    {tag} [{r['severity']}] {r['id']}: {r['message']}")


# ── Spec/plan ↔ SDD traceability (Superpowers bridge) ──────────────

_FILE_MENTION_RX = re.compile(r"[\w\-./]+\.(?:py|js|html|css|toml|json|sh)\b")


def _refs_present(text: str) -> set[str]:
    """SDD ref idents found in a document (AD-014, NFR-060, UC-005, R4, I6,
    SC8, A4, §9.6). Section refs count only in the explicit §X.Y form —
    a bare '9.6' is indistinguishable from a version number."""
    found: set[str] = set()
    for m in re.findall(r"\b(AD-\d{3}|NFR-\d{3}|UC-\d{3}|SC\d|R\d|I\d|A\d)\b", text):
        found.add(m)
    for m in re.findall(r"§\s*(\d{1,2}\.\d{1,2})", text):
        found.add(m)
    return found


def _expected_refs(idx: dict, repo_files: list[str]) -> list[str]:
    """Refs the mentioned files route to (idents only, deduped, order-stable)."""
    out: list[str] = []
    for f in repo_files:
        for t in topics_for_path(idx, f):
            for r in t["refs"]:
                ident = r.split(":", 1)[1]
                if ident not in out:
                    out.append(ident)
    return out


def cmd_trace(reg: dict, idx: dict, paths: list[str]) -> int:
    """Check a spec/plan document's SDD traceability (advisory).

    Superpowers specs/plans (docs/superpowers/) must cite the SDD refs of the
    areas they touch — this lists cited vs expected-but-missing refs.
    """
    if not paths:
        print("usage: trace <spec-or-plan.md> [...]", file=sys.stderr)
        return 1
    for raw in paths:
        p = Path(raw)
        if not p.is_file():
            print(f"── {raw}: not a file", file=sys.stderr)
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        # Resolve mentioned repo files (as-written path or top-level basename).
        repo_files: list[str] = []
        for m in dict.fromkeys(_FILE_MENTION_RX.findall(text)):
            if (PROJECT_ROOT / m).is_file():
                repo_files.append(norm(m))
            elif "/" not in m and (PROJECT_ROOT / m).is_file():
                repo_files.append(norm(m))
        cited = _refs_present(text)
        expected = _expected_refs(idx, repo_files)
        missing = [r for r in expected if r not in cited]
        print(f"── {norm(str(p))} ──")
        print(f"  files referenced: {', '.join(repo_files) or '(none resolved)'}")
        cited_sorted = sorted(cited)
        print(f"  SDD refs cited ({len(cited_sorted)}): {', '.join(cited_sorted) or '(none)'}")
        if missing:
            print(f"  expected but NOT cited ({len(missing)}): {', '.join(missing)}")
            print("  → resolve with: sdd <id>  (e.g. sdd AD-014) and cite the relevant ones")
        elif not expected:
            print("  (no routed SDD expectations — nothing to verify)")
        else:
            print("  ✓ all routed SDD refs are cited")
        print("")
    return 0


# ── Commands ────────────────────────────────────────────────────────

def cmd_prime(reg: dict) -> int:
    blocks = [r for r in reg["rules"] if r["severity"] == "block"]
    print("═══ SDD-GUARDIAN — mrrc_modern design harness (SDD " + reg["sdd_version"] + ") ═══")
    print("This repo is documented by SDD/ (IBM TeamSD, 15 chapters: requirements,")
    print("context, decisions, feasibility...). Before changing code, pull the full")
    print("engineering brief for the files you touch:")
    print("  python3 .agents/skills/sdd-guardian/harness/sdd_context.py brief <paths>")
    print("Validate a change before committing:")
    print("  python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged")
    print("")
    print("GOLDEN RULES (block-level, SDD-enforced):")
    for r in blocks:
        print(f"  ✗ {r['id']}: {r['title']}  [{r['sdd_ref']}]")
    print("")
    print("Lifecycle: brief → design (ADs, NFRs, risks, open issues I6/I7) → implement")
    print("(minimal diffs, module conventions) → test (unittest, no hardware, sync")
    print("tests/README) → doc-sync (SDD chapters + 14-version-history + AGENTS.md")
    print("+ README) → commit (imperative, scoped, ask before git mutations).")
    return 0


def cmd_context(reg: dict, idx: dict, paths: list[str], task: str | None) -> int:
    for raw in paths:
        p = norm(raw)
        print(f"── {p} ──")
        topics = topics_for_path(idx, p)
        refs: list[str] = []
        for t in topics:
            refs.extend(r for r in t["refs"] if r not in refs)
        if refs:
            print("  SDD: " + ", ".join(refs))
        _constraints_block(reg, p)
        if not refs and not applicable_rules(reg, p):
            print("  (no specific SDD mapping — general conventions apply)")
        print("")
    if task:
        topics = topics_for_task(idx, task)
        if topics:
            print(f"── SDD topics relevant to: {task!r} ──")
            for t in topics:
                print(f"  {t['id']}: " + ", ".join(t["refs"]))
            print("  → run `brief --task ...` to get the full extracted sections")
    return 0


def cmd_brief(reg: dict, idx: dict, paths: list[str], task: str | None) -> int:
    refs: list[str] = []
    if paths:
        for raw in paths:
            p = norm(raw)
            print(f"══ {p} ══")
            for t in topics_for_path(idx, p):
                for r in t["refs"]:
                    if r not in refs:
                        refs.append(r)
            _constraints_block(reg, p)
            print("")
    if task:
        for t in topics_for_task(idx, task):
            for r in t["refs"]:
                if r not in refs:
                    refs.append(r)
        print(f"══ task: {task} ══")
        print("")
    if not refs:
        print("No SDD topics matched — try `sdd <term>` to search, or read SDD/README.md.")
        return 0
    print(f"── SDD KNOWLEDGE ({len(refs)} refs, extracted live from SDD/) ──\n")
    total = 0
    for r in refs:
        item = resolve_ref(idx, r)
        if not item:
            print(f"┌─ {r}\n│ (unresolved — check index.json vs SDD/)\n└────")
            continue
        remaining = BRIEF_TOTAL_LINE_CAP - total
        if remaining <= 10:
            print(f"… budget reached; resolve remaining refs individually: `sdd {r}`")
            break
        total += _print_extracted(item, cap=min(BRIEF_REF_LINE_CAP, remaining))
        print("")
    return 0


def cmd_sdd(idx: dict, term: str) -> int:
    t = term.strip()
    # Exact ref forms
    m = re.match(r"^(AD-\d+|NFR-\d+|UC-\d+|R\d+|I\d+|SC\d+|A\d+)$", t, re.I)
    if m:
        ident = m.group(1).upper()
        kind = {"AD": "ad", "NFR": "nfr", "UC": "uc", "R": "risk",
                "I": "issue", "SC": "sc", "A": "assume"}[re.match(r"[A-Z]+", ident).group(0)]
        item = resolve_ref(idx, f"{kind}:{ident}")
        if item:
            _print_extracted(item, cap=200)
            return 0
        print(f"not found: {ident}", file=sys.stderr)
        return 1
    m = re.match(r"^(?:ch)?(\d{1,2})(?:\.(\d+))?$", t, re.I)
    if m:
        ref = f"sec:{t[2:]}" if t.startswith("sec:") else (
            f"ch:{m.group(1)}" if not m.group(2) else f"sec:{m.group(1)}.{m.group(2)}")
        item = resolve_ref(idx, ref)
        if item:
            _print_extracted(item, cap=200)
            return 0
        print(f"not found: {t}", file=sys.stderr)
        return 1
    # Free-text search over topics and refs
    topics = topics_for_task(idx, t)
    if topics:
        print(f"topics matching {t!r}:")
        for tp in topics:
            print(f"  {tp['id']}: " + ", ".join(tp["refs"]))
        print("\nresolve individually: sdd AD-014 | sdd NFR-060 | sdd 9.6 | brief --task ...")
        return 0
    print(f"no match for {t!r} — try an AD/NFR/UC id, a section like 9.6, or a keyword",
          file=sys.stderr)
    return 1


def cmd_check(reg: dict, paths: list[str], staged: bool) -> int:
    violations: list[dict] = []
    if staged:
        diff = subprocess.run(
            ["git", "diff", "--cached", "-U0", "--no-color"],
            capture_output=True, text=True, cwd=PROJECT_ROOT,
        ).stdout
        cur = None
        for line in diff.splitlines():
            if line.startswith("+++ b/"):
                cur = line[6:]
            elif line.startswith("+") and not line.startswith("+++"):
                violations.extend(scan_text(reg, cur, line[1:]))
    else:
        if not paths:
            print("usage: check <paths...> | --staged", file=sys.stderr)
            return 1
        for raw in paths:
            p = Path(raw)
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            violations.extend(scan_text(reg, norm(str(p)), text))
    blocks = [v for v in violations if v["severity"] == "block"]
    warns = [v for v in violations if v["severity"] == "warn"]
    if blocks:
        print("SDD-GUARDIAN: blocking violations found:", file=sys.stderr)
        print_violations(blocks, sys.stderr)
    if warns:
        print("SDD-GUARDIAN: warnings:")
        print_violations(warns, sys.stdout)
    if not violations:
        print("SDD-GUARDIAN: clean — no constraint violations.")
    return 2 if blocks else 0


def cmd_hook(reg: dict) -> int:
    """PreToolUse mode: inspect the pending edit; block on violations."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # fail-open
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input", {}) or {}
    path = ti.get("path") or ti.get("file_path")
    content = ti.get("content") if tool == "Write" else ti.get("new_string")
    if content is None and tool == "Bash":
        return 0  # shell commands are out of scope for source-pattern rules
    if content is None:
        return 0
    rel = norm(path) if path else None
    violations = scan_text(reg, rel, content)
    blocks = [v for v in violations if v["severity"] == "block"]
    warns = [v for v in violations if v["severity"] == "warn"]
    if blocks:
        print("SDD-GUARDIAN blocked this edit:", file=sys.stderr)
        print_violations(blocks, sys.stderr)
        return 2
    if warns:
        print("SDD-GUARDIAN warnings (allowed):")
        print_violations(warns, sys.stdout)
    return 0


def main(argv: list[str]) -> int:
    reg = load_registry()
    idx = load_index()
    args = argv[1:]
    if not args:
        print(__doc__)
        return 1
    cmd, rest = args[0], args[1:]
    if cmd == "prime":
        return cmd_prime(reg)
    if cmd in ("context", "brief"):
        task = None
        if "--task" in rest:
            i = rest.index("--task")
            task = " ".join(rest[i + 1:])
            rest = rest[:i]
        fn = cmd_context if cmd == "context" else cmd_brief
        return fn(reg, idx, rest, task)
    if cmd == "sdd":
        if not rest:
            print("usage: sdd <AD-xxx|NFR-xxx|UC-xxx|Rn|In|SCn|An|N.N|chNN|term>",
                  file=sys.stderr)
            return 1
        return cmd_sdd(idx, " ".join(rest))
    if cmd == "trace":
        return cmd_trace(reg, idx, rest)
    if cmd == "check":
        staged = "--staged" in rest
        return cmd_check(reg, [a for a in rest if not a.startswith("--")], staged)
    if cmd == "hook":
        return cmd_hook(reg)
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
