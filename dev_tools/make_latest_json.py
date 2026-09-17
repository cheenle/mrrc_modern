#!/usr/bin/env python3
"""Generate `website/downloads/latest.json` from the released artifacts (spec 2026-09-17-upgrade-channel §2 D1).

    python3 dev_tools/make_latest_json.py \
        --installer 1.18.0 https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-Setup.exe ~/Downloads/MRRC-Modern-Setup.exe \
        --previous  1.17.0 https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-v1.17.0-Windows-x64-Setup.exe ~/Downloads/old.exe \
        --notes "本版修复…" [--mandatory] [--min-supported 1.15.0] [--out website/downloads/latest.json]

The manifest is **never hand-written**: its size and SHA-256 come from the actual
files, and a missing/short file refuses the whole write.  The installer version
must equal the CHANGELOG's top entry — the release-time version authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import upgrade_core as up                                              # noqa: E402

CHANGELOG_VERSION_RE = re.compile(r"^##\s*\[?v?([0-9]+\.[0-9]+\.[0-9]+)", re.M)
DEFAULT_OUT = REPO / "website" / "downloads" / "latest.json"


class BuildError(RuntimeError):
    pass


def changelog_version(path: Path | None = None) -> str:
    text = (path or REPO / "CHANGELOG.md").read_text(encoding="utf-8", errors="replace")
    match = CHANGELOG_VERSION_RE.search(text)
    if not match:
        raise BuildError("CHANGELOG 顶部没有版本号（形如 '## [v1.2.3]'）")
    return match.group(1)


def digest_of(path: Path) -> tuple[str, int]:
    """SHA-256 + size of a real file; a missing/short artifact is a hard stop."""
    try:
        blob = path.read_bytes()
    except OSError as e:
        raise BuildError(f"读不到构建产物 {path}: {e}") from e
    if len(blob) < 1024:
        raise BuildError(f"{path} 只有 {len(blob)} 字节，不像是安装包")
    return hashlib.sha256(blob).hexdigest(), len(blob)


def build_manifest(installer: tuple, previous: tuple | None = None, notes: str = "",
                   mandatory: bool = False, min_supported: str = "",
                   released_at: str = "", expect_app: str = "") -> dict:
    """`installer`/`previous` are `(version, url, file_path)` triples."""
    version, url, path = installer
    if expect_app and up.compare_versions(version, expect_app) != 0:
        raise BuildError(f"installer 版本 {version} 与 CHANGELOG 顶版本 {expect_app} 不一致")
    sha, size = digest_of(Path(path).expanduser())
    manifest = {
        "latest": version,
        "installer": {"version": version, "url": url, "sha256": sha, "size": size},
        "minSupported": min_supported,
        "mandatory": bool(mandatory),
        "releasedAt": released_at or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "notes": notes,
    }
    if previous:
        prev_version, prev_url, prev_path = previous
        if up.compare_versions(prev_version, version) >= 0:
            raise BuildError(f"previous {prev_version} 必须比 latest {version} 更旧")
        prev_sha, prev_size = digest_of(Path(prev_path).expanduser())
        manifest["previous"] = {"version": prev_version, "url": prev_url,
                                "sha256": prev_sha, "size": prev_size}
    # The same validation the clients run, so a bad manifest can never be published.
    up.parse_manifest(manifest)
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate latest.json (update channel)")
    parser.add_argument("--installer", nargs=3, metavar=("VERSION", "URL", "FILE"), required=True)
    parser.add_argument("--previous", nargs=3, metavar=("VERSION", "URL", "FILE"))
    parser.add_argument("--notes", default="")
    parser.add_argument("--mandatory", action="store_true")
    parser.add_argument("--min-supported", default="")
    parser.add_argument("--released-at", default="")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        manifest = build_manifest(tuple(args.installer), tuple(args.previous) if args.previous else None,
                                  notes=args.notes, mandatory=args.mandatory,
                                  min_supported=args.min_supported, released_at=args.released_at,
                                  expect_app=changelog_version())
    except BuildError as e:
        print(f"拒绝生成：{e}", file=sys.stderr)
        return 2

    blob = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if args.dry_run:
        print(blob)
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(blob, encoding="utf-8")
    print(f"wrote {out} (latest {manifest['latest']}, installer {manifest['installer']['size']} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
