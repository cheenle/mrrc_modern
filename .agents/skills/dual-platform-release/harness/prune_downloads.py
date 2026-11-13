#!/usr/bin/env python3
"""Prune the published `downloads/` directory by rule, not by eyeball.

Why this exists: on 2026-11-13 the portal's root filesystem was **100 % full**
(`nginx: write() to access.log failed (28: No space left on device)`), and 4.4 GB
of it was `mrrc_modern/downloads` — eight Windows installers and eight DMGs from a
single busy day, plus eighteen source tarballs, of which the site and `latest.json`
referenced three. Deciding what may go took a human reading every page and every
manifest field, and the failure modes of guessing are asymmetric:

* delete something a page or the update manifest names → a published 404, or a box
  that "upgrades" into a dead link;
* delete too little → the disk fills again and the web server cannot log.

So the decision is a rule set (`plan()`), it is testable without a server
(`tests/test_release_prune.py`), and the tool prints its reasoning.

What is **never** deleted
-------------------------
1. any basename named anywhere in the manifest (`installer`, `previous`,
   `boxLine`, `boxLine.patch` … every `url` field, recursively);
2. any basename that appears in the site's own text (`--site`, repeatable);
3. symlink targets — the generic names (`MRRC-Modern-Setup.exe`) are links, and the
   link's target is what a download actually resolves to;
4. the newest `--keep-<family>` versions of each family (defaults: installers and
   DMGs 2 = current + previous, sources 2, hot-fix patches 4, APKs 2);
5. anything the rules cannot classify (drivers, `.cmd`/`.ps1` helpers, `latest.json`
   and its backups, `patches/`): listed as `unclassified` for a human to look at,
   never removed automatically.

Usage
-----
    # dry run (default): shows the plan, deletes nothing
    python3 .agents/skills/dual-platform-release/harness/prune_downloads.py \
        --dir /var/www/vlsc.net/mrrc_modern/downloads \
        --manifest /var/www/vlsc.net/mrrc_modern/downloads/latest.json \
        --site /var/www/vlsc.net/mrrc_modern

    # delete exactly what the plan printed
    ... --apply

Run it **on the server** (python3 is there; the script is stdlib only):

    scp harness/prune_downloads.py www.vlsc.net:/tmp/
    ssh www.vlsc.net 'python3 /tmp/prune_downloads.py --dir … --manifest … --site … --apply'

Exit codes: 0 = plan (or deletion) succeeded, 2 = the inputs are unusable and
nothing was deleted.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ── families ───────────────────────────────────────────────────────────────
# A family is "one product line's versioned artifacts". The newest N of each is
# kept; the rest are candidates. Order of the list is the order printed.
FAMILIES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("windows", re.compile(r"^MRRC-Modern-v(\d+\.\d+\.\d+)-Windows-x64-Setup\.exe$")),
    ("macos", re.compile(r"^MRRC-Modern-v(\d+\.\d+\.\d+)-arm64\.dmg$")),
    ("android", re.compile(r"^MRRC-Modern-v(\d+\.\d+\.\d+)-Android\.apk$")),
    ("source", re.compile(r"^mrrc-modern-src-(\d+\.\d+\.\d+)\.tar\.gz$")),
    ("patch", re.compile(r"^mrrc-modern-patch-(\d+\.\d+\.\d+)\.tar\.gz$")),
)

DEFAULT_KEEP = {"windows": 2, "macos": 2, "android": 2, "source": 2, "patch": 4}

# Files a page may link by name without the text living in an HTML tag we parse
# (`curl`-able). Cheap and explicit beats a clever crawler.
TEXT_SUFFIXES = (".html", ".htm", ".js", ".json", ".txt", ".md", ".xml", ".cmd", ".ps1", ".sh")


class PlanError(RuntimeError):
    """The inputs cannot produce a safe plan; nothing may be deleted."""


def _version_key(version: str) -> Tuple[int, int, int]:
    """`1.2.3` -> (1, 2, 3). Callers pass strings a family regex already matched,
    but a hand-edited version on disk must fail the plan, not the sort."""
    parts = version.split(".")
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except (IndexError, ValueError) as e:
        raise PlanError(f"版本号无法比较：{version!r}") from e


def classify(name: str) -> Optional[Tuple[str, Tuple[int, int, int]]]:
    """`(family, version)` for a versioned artifact, else None."""
    for family, pattern in FAMILIES:
        match = pattern.match(name)
        if match:
            return family, _version_key(match.group(1))
    return None


# ── protection sets ────────────────────────────────────────────────────────
def _urls(node, found: List[str]) -> List[str]:
    """Every `url` string anywhere in a manifest-shaped object."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "url" and isinstance(value, str):
                found.append(value)
            else:
                _urls(value, found)
    elif isinstance(node, list):
        for item in node:
            _urls(item, found)
    return found


def manifest_names(manifest: dict) -> Dict[str, str]:
    """basename -> the manifest path that names it, for every absolute URL."""
    names: Dict[str, str] = {}
    for url in _urls(manifest, []):
        base = os.path.basename(url.split("?", 1)[0].rstrip("/"))
        if base:
            names.setdefault(base, url)
    return names


def site_references(site: Path) -> str:
    """Concatenated text of everything under `site` that could mention a filename.

    Deliberately shallow in *understanding* and deep in *coverage*: any page that
    spells a filename is a page that would 404. Binaries are skipped by suffix.
    """
    if not site.exists():
        return ""
    chunks: List[str] = []
    paths: Iterable[Path] = [site] if site.is_file() else site.rglob("*")
    for path in paths:
        try:
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if path.stat().st_size > 8 * 1024 * 1024:      # a 20 MB JSON cannot be a page
                continue
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(chunks)


def symlink_targets(directory: Path) -> Dict[str, str]:
    """name of link -> name it points at (so the target can be protected)."""
    targets: Dict[str, str] = {}
    for entry in os.scandir(directory):
        if entry.is_symlink():
            try:
                targets[entry.name] = os.path.basename(os.readlink(entry.path))
            except OSError:
                continue
    return targets


# ── the plan ───────────────────────────────────────────────────────────────
def plan(directory: Path, manifest: dict, site_text: str = "",
         keep: Optional[Dict[str, int]] = None) -> dict:
    """Return `{"delete": [...], "kept": [...], "unclassified": [...]}`.

    Each row: `{"name", "path", "family", "version", "size", "reason"}`.
    Raises `PlanError` when the inputs would make the plan unsafe (no manifest
    references at all, or a family with nothing protected).
    """
    keep = dict(DEFAULT_KEEP, **(keep or {}))
    names = manifest_names(manifest)
    if not names:
        raise PlanError("清单里一个 url 都没有 —— 拒绝在没有保护集的情况下规划删除")
    links = symlink_targets(directory)

    rows: List[dict] = []
    by_family: Dict[str, List[dict]] = {}
    unclassified: List[dict] = []

    for entry in sorted(os.scandir(directory), key=lambda e: e.name):
        if not entry.is_file() or entry.is_symlink():
            continue
        name = entry.name
        size = entry.stat().st_size
        parsed = classify(name)
        if parsed is None:
            unclassified.append({"name": name, "path": entry.path, "size": size})
            continue
        family, version = parsed
        row = {"name": name, "path": entry.path, "family": family,
               "version": ".".join(str(p) for p in version), "size": size,
               "reason": ""}
        if name in names:
            row["reason"] = f"清单引用（{names[name]}）"
        elif name in site_text:
            row["reason"] = "站点页面引用"
        elif name in links.values():
            row["reason"] = f"符号链接目标（{[k for k, v in links.items() if v == name][0]}）"
        by_family.setdefault(family, []).append(row)
        rows.append(row)

    # newest N per family is protected *by age*; the sites/manifest always are.
    for family, group in by_family.items():
        n = keep.get(family, 0)
        newest = sorted(group, key=lambda r: _version_key(r["version"]), reverse=True)[:n]
        for row in newest:
            row["reason"] = row["reason"] or f"该家族最新 {n} 个版本之一"

    deletable, kept = [], []
    for row in rows:
        (kept if row["reason"] else deletable).append(row)

    for family, group in by_family.items():
        if group and not any(row["reason"] for row in group):
            raise PlanError(f"{family} 家族会被清空 —— 拒绝执行（从清单/页面/链接里"
                            f"一个引用都没有，先核对是不是清单指错了）")

    return {"delete": deletable, "kept": kept, "unclassified": unclassified}


def _human(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.1f} MB"
    return f"{size / 1024 ** 3:.1f} GB"


def report(result: dict, stream=None) -> int:
    """Print the plan; return the number of bytes it would free."""
    stream = stream or sys.stdout
    total = 0
    print("DELETE", file=stream)
    if not result["delete"]:
        print("  (nothing — every versioned artifact is referenced or recent)", file=stream)
    for row in sorted(result["delete"], key=lambda r: (-r["size"], r["name"])):
        total += row["size"]
        print(f"  {_human(row['size']):>9}  {row['family']:<8} {row['version']:<8} {row['name']}",
              file=stream)
    print(f"  {'-' * 9}  would free {_human(total)} ({len(result['delete'])} files)",
          file=stream)

    print("\nKEEP (versioned)", file=stream)
    for row in sorted(result["kept"], key=lambda r: (r["family"], _version_key(r["version"]))):
        print(f"  {_human(row['size']):>9}  {row['family']:<8} {row['version']:<8} "
              f"{row['name']}  ← {row['reason']}", file=stream)

    print("\nUNCLASSIFIED (kept — helpers, drivers, manifests; review by hand)", file=stream)
    for row in sorted(result["unclassified"], key=lambda r: r["name"]):
        print(f"  {_human(row['size']):>9}  {row['name']}", file=stream)
    return total


def apply_plan(result: dict, stream=None) -> int:
    """Delete the planned rows. Returns the number of bytes actually freed."""
    stream = stream or sys.stdout
    freed = 0
    for row in result["delete"]:
        try:
            os.unlink(row["path"])
        except OSError as e:                    # keep going: one locked file must not
            print(f"  ! {row['name']}: {e}", file=stream)   # stop the rest of the prune
            continue
        freed += row["size"]
        print(f"  removed {row['name']} ({_human(row['size'])})", file=stream)
    return freed


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Prune downloads/ by rule (dry run by default)")
    parser.add_argument("--dir", required=True, help="the downloads directory")
    parser.add_argument("--manifest", required=True, help="latest.json (the update manifest)")
    parser.add_argument("--site", action="append", default=[],
                        help="site root or page whose text protects a filename (repeatable)")
    for family, _pattern in FAMILIES:
        parser.add_argument(f"--keep-{family}", type=int, default=DEFAULT_KEEP[family],
                            help=f"how many newest {family} versions to keep "
                                 f"(default {DEFAULT_KEEP[family]})")
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    parser.add_argument("--json", action="store_true", help="machine-readable plan")
    args = parser.parse_args(argv)

    directory = Path(args.dir)
    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"读不到清单 {args.manifest}: {e}", file=sys.stderr)
        return 2
    if not directory.is_dir():
        print(f"不是目录：{directory}", file=sys.stderr)
        return 2

    keep = {family: getattr(args, f"keep_{family}") for family, _ in FAMILIES}
    text = "\n".join(site_references(Path(p)) for p in args.site) if args.site else ""
    try:
        result = plan(directory, manifest, text, keep)
    except PlanError as e:
        print(f"拒绝执行：{e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        report(result)

    if args.apply:
        freed = apply_plan(result)
        print(f"\nfreed {_human(freed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
