#!/usr/bin/env python3
"""Release completeness checker — "did we miss anything?" as an executable check.

Why this exists: three drifts shipped unnoticed in the v1.15.0/V2.46 cycle —
the hand-written SDD landing pages still advertised V2.27, the generated pages'
footer advertised V2.45 because the generator read a hand-maintained row, and
the release skill's own checklist did not mention either of them. A prose
checklist cannot be trusted for that; this script can, because it reads a
machine-readable registry of every version- and artifact-bearing file
(`release-artifacts.json`) and fails loudly when one is stale.

Usage
-----
    python3 .agents/skills/dual-platform-release/harness/release_check.py            # offline rules
    python3 .agents/skills/dual-platform-release/harness/release_check.py --online    # + published site
    python3 .agents/skills/dual-platform-release/harness/release_check.py --json

Exit codes: 0 = clean, 2 = violations (same convention as the SDD guardian).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HARNESS = Path(__file__).resolve().parent
SKILL = HARNESS.parent
ROOT = SKILL.parent.parent.parent          # repository root
REGISTRY_PATH = SKILL / "release-artifacts.json"
SITE = "https://www.vlsc.net/mrrc_modern"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


# ── Sources of truth ────────────────────────────────────────────────

def _first_match(rel: str, pattern: str) -> Optional[str]:
    path = ROOT / rel
    if not path.exists():
        return None
    m = re.search(pattern, path.read_text(encoding="utf-8", errors="replace"),
                  re.MULTILINE)
    return m.group(1).lstrip("v") if m else None


def app_version(registry: dict) -> Optional[str]:
    src = registry["app_version_source"]
    return _first_match(src["path"], src["pattern"])


def sdd_version(registry: dict) -> Optional[str]:
    src = registry["sdd_version_source"]
    return _first_match(src["path"], src["pattern"])


def recent_sdd_versions(registry: dict, count: int = 3) -> List[str]:
    """The newest ``count`` SDD versions from the version-history table.

    Diagrams may lag a release or two behind (redrawing all of them for a
    documentation-only change is busywork), but "19 versions stale" is what this
    check exists to prevent.
    """
    src = registry.get("sdd_version_source")
    if not src:
        return []
    path = ROOT / src["path"]
    if not path.exists():
        return []
    found = re.findall(src["pattern"], path.read_text(encoding="utf-8",
                                                     errors="replace"),
                       re.MULTILINE)
    return [v if str(v).startswith("V") else f"V{v}" for v in found[:count]]


# ── Rule evaluation ─────────────────────────────────────────────────

def check_rules(registry: dict, app: str, sdd: str) -> List[Tuple[str, str, str]]:
    """[(status, rule_id, message)] for every version rule in the registry."""
    out: List[Tuple[str, str, str]] = []
    for rule in registry["rules"]:
        expected = app if rule["expect"] == "app" else sdd
        path = ROOT / rule["path"]
        if not path.exists():
            status = SKIP if rule.get("optional") else FAIL
            out.append((status, rule["id"], f"{rule['path']} does not exist"))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        found = [m.lstrip("v") for m in re.findall(rule["pattern"], text)]
        if not found:
            status = SKIP if rule.get("optional") else FAIL
            out.append((status, rule["id"],
                        f"{rule['path']}: pattern not found ({rule['pattern'][:50]})"))
            continue
        if "count" in rule and len(found) != rule["count"]:
            out.append((FAIL, rule["id"],
                        f"{rule['path']}: expected {rule['count']} occurrence(s), "
                        f"found {len(found)}"))
            continue
        if "min_count" in rule and len(found) < rule["min_count"]:
            out.append((FAIL, rule["id"],
                        f"{rule['path']}: expected at least {rule['min_count']}, "
                        f"found {len(found)}"))
            continue
        stale = sorted({v for v in found if v != expected})
        if stale:
            out.append((FAIL, rule["id"],
                        f"{rule['path']}: stale version(s) {stale} — expected {expected}"))
        else:
            out.append((PASS, rule["id"],
                        f"{rule['path']}: {expected} ({len(found)} occurrence(s))"))
    return out


def check_artifact_facts(registry: dict, app: str) -> List[Tuple[str, str, str]]:
    """Card bytes/SHA must describe the artifact that was actually built."""
    out: List[Tuple[str, str, str]] = []
    for fact in registry.get("artifact_facts", []):
        page = ROOT / fact["page"]
        artifact = ROOT / fact["artifact"].format(app=app)
        if not page.exists():
            out.append((FAIL, fact["id"], f"{fact['page']} is missing"))
            continue
        text = page.read_text(encoding="utf-8", errors="replace")
        if not artifact.exists():
            out.append((SKIP, fact["id"],
                        f"{fact['artifact'].format(app=app)} not built locally — "
                        "cannot verify the card's bytes/SHA"))
            continue
        data = artifact.read_bytes()
        size = f"{len(data):,}"
        sha = hashlib.sha256(data).hexdigest()
        problems = []
        if size not in text:
            problems.append(f"bytes {size} not on the card")
        if sha[:8] not in text:
            problems.append(f"SHA-256 prefix {sha[:8]} not on the card")
        out.append((FAIL, fact["id"], f"{fact['page']}: " + "; ".join(problems))
                   if problems else
                   (PASS, fact["id"], f"{fact['page']}: {size} bytes, {sha[:8]}…"))
    return out


def check_stale_tokens(registry: dict, app: str) -> List[Tuple[str, str, str]]:
    pattern = registry["stale_tokens"]["pattern"]
    out: List[Tuple[str, str, str]] = []
    for rel in registry["stale_tokens"]["scope"]:
        path = ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        stale = sorted({v for v in re.findall(pattern, text) if v != app})
        if stale:
            out.append((FAIL, f"stale-tokens:{rel}",
                        f"{rel} still points at {stale} (current {app})"))
    if not out:
        out.append((PASS, "stale-tokens",
                    f"{len(registry['stale_tokens']['scope'])} current-facing files "
                    "reference only the current release"))
    return out


def check_diagrams(registry: dict, sdd: str) -> List[Tuple[str, str, str]]:
    """Design diagrams: version marker + the keywords of what they depict.

    A picture is a design artifact like a chapter: when a capability changes,
    the diagram must change with it.  The per-file keyword lists are what makes
    that automatic — a new backend family or a removed feature fails the check
    until the diagram is redrawn.
    """
    spec = registry.get("diagrams")
    if not spec:
        return []
    out: List[Tuple[str, str, str]] = []
    for entry in spec["files"]:
        path = ROOT / entry["path"]
        if not path.exists():
            out.append((FAIL, f"diagram:{path.name}", f"{entry['path']} is missing"))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        problems = []
        accepted = recent_sdd_versions(registry,
                                       registry.get("diagrams", {}).get("max_versions_behind", 2) + 1)
        marker = re.search(r"diagram-version:\s*(V[0-9.]+)", text)
        if marker is None:
            problems.append("no diagram-version marker")
        elif accepted and marker.group(1) not in accepted:
            problems.append(f"diagram-version {marker.group(1)} is older than {accepted}")
        missing = [kw for kw in entry["require"] if kw not in text]
        if missing:
            problems.append(f"does not mention {missing}")
        out.append((FAIL, f"diagram:{path.name}", f"{entry['path']}: " + "; ".join(problems))
                   if problems else
                   (PASS, f"diagram:{path.name}",
                    f"{entry['path']}: {sdd} marker + {len(entry['require'])} keyword(s)"))

    # generated copies must match their source tree byte for byte
    for src_rel, dst_rel in spec["source_dirs"].items():
        src, dst = ROOT / src_rel, ROOT / dst_rel
        if not src.is_dir():
            continue
        for svg in sorted(src.glob("*.svg")):
            copy = dst / svg.name
            if not copy.exists():
                out.append((FAIL, f"diagram-copy:{svg.name}",
                            f"{dst_rel}/{svg.name} is missing (run the site builders)"))
            elif copy.read_bytes() != svg.read_bytes():
                out.append((FAIL, f"diagram-copy:{svg.name}",
                            f"{dst_rel}/{svg.name} differs from {src_rel}/{svg.name} "
                            "— regenerate instead of editing the copy"))
    if not [r for r in out if r[0] == FAIL]:
        out.append((PASS, "diagram-copies",
                    "every generated diagram copy matches its source"))
    return out


# ── Online (publish) verification ───────────────────────────────────

def _http(url: str, method: str = "GET") -> Tuple[int, Dict[str, str], bytes]:
    req = urllib.request.Request(url, method=method,
                                 headers={"User-Agent": "mrrc-release-check"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (fixed host)
            body = resp.read() if method == "GET" else b""
            return resp.status, dict(resp.headers), body
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), b""
    except Exception as exc:                                   # noqa: BLE001
        return 0, {"error": str(exc)}, b""


def check_online(registry: dict, app: str, sdd: str,
                 deep: bool = False) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []

    # Published pages must carry the current versions.
    for rel, needle, label in (
        ("index.html", f"<strong>{sdd}</strong>", f"main page badges SDD {sdd}"),
        ("sdd.html", f"SDD {sdd} ·", f"SDD landing badges {sdd}"),
        ("zh/sdd.html", f"SDD {sdd} ·", f"ZH SDD landing badges {sdd}"),
        ("index.html", f"v{app}", f"main page mentions v{app}"),
    ):
        status, headers, body = _http(f"{SITE}/{rel}")
        if status != 200:
            out.append((FAIL, f"online:{rel}", f"HTTP {status} {headers.get('error','')}"))
            continue
        text = body.decode("utf-8", errors="replace")
        out.append((PASS, f"online:{rel}", f"{label} ✓" if needle in text
                    else f"MISSING {needle!r}") if needle in text else
                   (FAIL, f"online:{rel}", f"{rel} does not contain {needle!r}"))

    # The published downloads must exist and match the shipped bytes.
    for fact in registry.get("artifact_facts", []):
        artifact = ROOT / fact["artifact"].format(app=app)
        url = f"{SITE}/downloads/{fact['artifact'].format(app=app).split('/')[-1]}"
        status, headers, body = _http(url, method="HEAD")
        if status != 200:
            out.append((FAIL, f"online:{fact['id']}", f"{url} -> HTTP {status}"))
            continue
        published = headers.get("Content-Length")
        if artifact.exists():
            local = str(artifact.stat().st_size)
            out.append((PASS, f"online:{fact['id']}", f"{url} ({published} bytes)")
                       if published == local else
                       (FAIL, f"online:{fact['id']}",
                        f"published {published} bytes != built {local} bytes"))
        else:
            out.append((SKIP, f"online:{fact['id']}",
                        f"{url} published ({published} bytes); no local build to compare"))
        if deep and artifact.exists():
            status, _, body = _http(url)
            if status == 200:
                sha = hashlib.sha256(body).hexdigest()
                local_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
                out.append((PASS, f"online-sha:{fact['id']}", f"{url} sha256 matches")
                           if sha == local_sha else
                           (FAIL, f"online-sha:{fact['id']}",
                            f"published sha {sha[:12]}… != built {local_sha[:12]}…"))

    # The tag must be pushed (a release without a tag on origin is not published).
    if (ROOT / ".git").exists():
        try:
            res = subprocess.run(["git", "ls-remote", "--tags", "origin", f"v{app}"],
                                 cwd=ROOT, capture_output=True, text=True, timeout=90)
            tagged = f"refs/tags/v{app}" in res.stdout
            out.append((PASS, "online:tag", f"v{app} exists on origin") if tagged else
                       (FAIL, "online:tag", f"tag v{app} not found on origin"))
        except Exception as exc:                                # noqa: BLE001
            out.append((SKIP, "online:tag", f"could not query origin: {exc}"))
    return out


# ── Report ──────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--online", action="store_true",
                    help="also verify the published site and downloads")
    ap.add_argument("--deep", action="store_true",
                    help="with --online: download the artifacts and compare SHA-256")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    try:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read the release registry {REGISTRY_PATH}: {exc}",
              file=sys.stderr)
        return 2
    app, sdd = app_version(registry), sdd_version(registry)
    if not app or not sdd:
        print("cannot determine the app/SDD version from the registry sources",
              file=sys.stderr)
        return 2

    results = (check_rules(registry, app, sdd)
               + check_artifact_facts(registry, app)
               + check_stale_tokens(registry, app)
               + check_diagrams(registry, sdd))
    if args.online:
        results += check_online(registry, app, sdd, deep=args.deep)

    failures = [r for r in results if r[0] == FAIL]
    if args.json:
        print(json.dumps({"app_version": app, "sdd_version": sdd,
                          "results": [{"status": s, "id": i, "message": m}
                                      for s, i, m in results],
                          "failures": len(failures)}, ensure_ascii=False, indent=2))
    else:
        print(f"release check — app v{app}, SDD {sdd}"
              f"{' (including the published site)' if args.online else ''}\n")
        for status, rule_id, message in results:
            mark = {"PASS": "✓", "FAIL": "✗", "SKIP": "·"}[status]
            print(f"  {mark} [{rule_id}] {message}")
        print(f"\n  {len(results) - len(failures)} ok, {len(failures)} failing")
        if registry.get("manual_review"):
            print("\n  review by hand (not machine-checkable): "
                  + ", ".join(registry["manual_review"]["paths"]))
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
