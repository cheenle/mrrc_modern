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

def check_rules(registry: dict, app: str, sdd: str,
                box: str = "") -> List[Tuple[str, str, str]]:
    """[(status, rule_id, message)] for every version rule in the registry.

    `expect` is one of three vocabularies, because this repository ships three
    independently numbered things: `app` (desktop installers), `sdd` (the design
    document) and `box` (the box/Raspberry Pi image line, from `box_version_source`).
    A `box` rule whose artifact has not been built locally is SKIPped rather than
    failed — the image lines are independent, and `dist/` is not in the repository.
    """
    out: List[Tuple[str, str, str]] = []
    for rule in registry["rules"]:
        if rule["expect"] == "box":
            # A rule may name the line it documents (`box_line`); without it the
            # expectation is the newest artifact of either line.
            expected = (box_version(registry, rule["box_line"])
                        if rule.get("box_line") else box)
            if not expected:
                out.append((SKIP, rule["id"],
                            f"{rule['path']}: no locally built "
                            f"{rule.get('box_line') or 'box-line'} artifact "
                            "— cannot check the version"))
                continue
        else:
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
        if rule["expect"] == "box":
            # 比对方向：页面**落后于**本地产物才算漂移。页面写了比本地更新的版本，
            # 说明那一份是在别的构建机上出的（2026-11-23：树莓派镜像线上是 1.28.62，
            # 本机 dist/rpi 只有 1.28.23）——本地文件无法裁定，只能 SKIP。
            newer = sorted((v for v in set(found) if v != expected
                            and _vtuple(v) > _vtuple(expected)), reverse=True)
            if newer:
                out.append((SKIP, rule["id"],
                            f"{rule['path']}: names {newer[0]}, newer than the locally "
                            f"built {expected} — that image was built elsewhere"))
                continue
        stale = sorted({v for v in found if v != expected})
        if stale:
            out.append((FAIL, rule["id"],
                        f"{rule['path']}: stale version(s) {stale} — expected {expected}"))
        else:
            out.append((PASS, rule["id"],
                        f"{rule['path']}: {expected} ({len(found)} occurrence(s))"))
    return out


def _vtuple(text: str) -> tuple:
    try:
        return tuple(int(x) for x in str(text).split("."))
    except ValueError:
        return (0,)


def box_version(registry: dict, line: str = "") -> str:
    """The version of the box/Raspberry Pi image line, read from its artifact.

    Empty when no image has been built locally: the line is independent of the
    desktop installer, so a missing artifact must not invalidate anything else.

    ``line`` restricts the answer to one build directory (``"dist/rpi"``).  The
    two image lines share a numbering but are built on different days, so "the
    newest artifact of either line" is the wrong expectation for a page that
    documents one of them: on 2026-11-23 a fresh W103D image made the *Raspberry
    Pi* guide red for naming the Pi image that is actually published (1.28.62).
    A page that names an artifact gets that artifact's line, and SKIPs when that
    line has not been built.
    """
    spec = registry.get("box_version_source")
    if not spec:
        return ""
    # `sources` (a list of {path, pattern}) covers both image lines: the W103D
    # line and the Raspberry Pi line are built into different directories but are
    # the same independently-numbered line. A single `path`/`pattern` is still
    # accepted so an older registry keeps working.
    sources = spec.get("sources") or [spec]
    found = []
    for source in sources:
        if line and source.get("path") != line:
            continue
        out_dir = ROOT / source["path"]
        if not out_dir.is_dir():
            continue
        for path in out_dir.iterdir():
            m = re.match(source["pattern"], path.name)
            if m:
                found.append((tuple(int(x) for x in m.group(1).split(".")), m.group(1)))
    return max(found)[1] if found else ""


def _fact_version(fact: dict, registry: dict, app: str, box: str) -> str:
    """`{box}` for one artifact fact — from the line that fact ships, when it says so.

    `website-rpi-card` describes the Raspberry Pi image, yet it used to inherit
    "the newest artifact of either image line": once a fresh W103D image existed,
    the rule looked for `MRRC-Modern-v<new box>-rpi64.img.xz`, which has never
    been built (2026-11-23, online check 404). A fact that names its line gets
    that line's version.
    """
    line = fact.get("box_line")
    if line:
        return box_version(registry, line)
    return box


def check_artifact_facts(registry: dict, app: str,
                         box: str = "") -> List[Tuple[str, str, str]]:
    """Card bytes/SHA must describe the artifact that was actually built."""
    out: List[Tuple[str, str, str]] = []
    for fact in registry.get("artifact_facts", []):
        page = ROOT / fact["page"]
        fact_box = _fact_version(fact, registry, app, box) or app
        artifact = ROOT / fact["artifact"].format(app=app, box=fact_box)
        if not page.exists():
            out.append((FAIL, fact["id"], f"{fact['page']} is missing"))
            continue
        text = page.read_text(encoding="utf-8", errors="replace")
        if not artifact.exists():
            out.append((SKIP, fact["id"],
                        f"{fact['artifact'].format(app=app, box=fact_box)} not built locally — "
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


def check_box_image_names(registry: dict) -> List[Tuple[str, str, str]]:
    """Current-facing W103D pages must name the image by its **alias**, not a version.

    Measured twice: the guide's download button advertised `1.25.5` (404), was
    changed to read the version out of the markdown — and then went on
    advertising `1.28.1` after `1.28.22` had replaced it on the site (404 again,
    2026-11-15). A version inside a document rots on the day the next image is
    built; the site keeps `MRRC-Modern-w103d.img.gz` pointing at whatever is
    current, so that is the only spelling these pages may use. `w103d_pack.md`
    is deliberately out of scope — it is the release manual and keeps the
    per-release records on purpose.
    """
    spec = registry.get("box_image_names")
    if not spec:
        return []
    pattern = spec["pattern"]
    out: List[Tuple[str, str, str]] = []
    scanned = 0
    for rel in spec["scope"]:
        path = ROOT / rel
        if not path.exists():
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        stale = sorted(set(re.findall(pattern, text)))
        if stale:
            out.append((FAIL, f"box-image-name:{rel}",
                        f"{rel} names a versioned image {stale} — link "
                        f"{spec['alias']} instead (the site's alias always "
                        "points at the current one)"))
    if not out:
        out.append((PASS, "box-image-names",
                    f"{scanned} current-facing W103D pages use the alias "
                    f"{spec['alias']}"))
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


def check_published_completeness() -> List[Tuple[str, str, str]]:
    """Every page/asset the site is built from must be published, byte for byte.

    Catches the "generated but never deployed" direction of the drift problem:
    the site builders write ~40 files (16 SDD pages, 10 diagrams, the UI
    diagrams, the landing pages), and a forgotten deploy used to be invisible
    until a reader noticed an old page.
    """
    site = ROOT / "website"
    targets: List[Path] = []
    for rel_dir in ("sdd", "images"):
        d = site / rel_dir
        if d.is_dir():
            targets += [f for f in sorted(d.rglob("*")) if f.is_file()
                        and f.suffix in (".html", ".svg", ".css", ".js")]
    for name in ("index.html", "zh/index.html", "guide.html", "zh/guide.html",
                 "sdd.html", "zh/sdd.html"):
        f = site / name
        if f.exists():
            targets.append(f)

    out: List[Tuple[str, str, str]] = []
    published = mismatched = 0
    for path in targets:
        rel = path.relative_to(site).as_posix()
        status, headers, _ = _http(f"{SITE}/{rel}", method="HEAD")
        if status != 200:
            out.append((FAIL, f"published:{rel}", f"{rel} -> HTTP {status}"))
            continue
        length = headers.get("Content-Length")
        if length is not None and length != str(path.stat().st_size):
            out.append((FAIL, f"published:{rel}",
                        f"{rel}: published {length} bytes != built {path.stat().st_size}"))
            mismatched += 1
        else:
            published += 1
    if not [r for r in out if r[0] == FAIL]:
        out.append((PASS, "published-complete",
                    f"{published} generated pages/assets published with matching sizes"))
    return out


def check_online(registry: dict, app: str, sdd: str,
                 deep: bool = False, box: str = "") -> List[Tuple[str, str, str]]:
    """Published bytes must match the built bytes.

    `box` is passed through to the artifact templates: an image-line artifact is
    named after the box/镜像线 version, not the desktop app version (2026-10-27).
    """
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
        if fact.get("optional"):
            out.append((SKIP, f"online:{fact['id']}",
                        fact.get("why", "optional artifact — published on its own stream")))
            continue
        name = fact["artifact"].format(
            app=app, box=_fact_version(fact, registry, app, box) or app)
        artifact = ROOT / name
        url = f"{SITE}/downloads/{name.split('/')[-1]}"
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
    #
    # Both lines are checked: the desktop line (`v<app>`) and, when an image-line
    # artifact was built, the mirror line (`v<box>`). They are separate numbers, and
    # `git push --follow-tags` silently skips a **lightweight** tag — measured on
    # 2026-10-27, when v1.28.12 was created without `-a` and the push reported success
    # while origin had no such tag. An annotated tag is the convention here.
    if (ROOT / ".git").exists():
        for label, version in (("", app), ("box:", box)) if box else (("", app),):
            if not version:
                continue
            rule = f"online:tag-{label}{version}" if label else "online:tag"
            try:
                ref = f"v{version}"
                res = subprocess.run(["git", "ls-remote", "--tags", "origin", ref],
                                     cwd=ROOT, capture_output=True, text=True, timeout=90)
                tagged = f"refs/tags/{ref}" in res.stdout
                out.append((PASS, rule, f"{ref} exists on origin") if tagged else
                           (FAIL, rule, f"tag {ref} not found on origin"))
            except Exception as exc:                            # noqa: BLE001
                out.append((SKIP, rule, f"could not query origin: {exc}"))
    return out


# ── Report ──────────────────────────────────────────────────────────

def _make_output_encoding_safe() -> None:
    """Never let a check die on an unprintable character.

    The Windows build VM runs with a GBK console code page: the ✓/·/✗ marks and
    the em-dash in a message raised UnicodeEncodeError *inside* the reporter,
    which turns a reportable violation into a traceback (and, worse, into a test
    failure that looks like a rules violation).  backslashreplace keeps the
    console's own encoding and degrades just those characters to ASCII escapes,
    so the captured output stays decodable by any parent.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            # pyright types these as TextIO, which has no `reconfigure`; the real
            # objects (including pytest's captured streams) do, and the except
            # below is the runtime guard for the ones that do not.
            stream.reconfigure(errors="backslashreplace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


def main(argv: Optional[List[str]] = None) -> int:
    _make_output_encoding_safe()
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

    box = box_version(registry)
    results = (check_rules(registry, app, sdd, box)
               + check_artifact_facts(registry, app, box)
               + check_stale_tokens(registry, app)
               + check_box_image_names(registry)
               + check_diagrams(registry, sdd))
    if args.online:
        results += check_online(registry, app, sdd, deep=args.deep, box=box)
        results += check_published_completeness()

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
