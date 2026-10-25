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


def _box_block(box: tuple, fixpack: str = "", notes: str = "") -> dict:
    """The `boxLine` object, with every number taken from the real tarball.

    The key is `boxLine`, **not** `box` (2026-10-19): a box already in the field
    parses `box.version == latest` and refuses the manifest otherwise, so shipping
    a box line that is newer than the desktop line under the old key turns that
    box's update page into "检查失败". Old parsers ignore unknown keys, which is
    what makes the new name the backward-compatible way to say "the box is ahead".
    """
    box_version, box_url, box_path = box
    # Not required to equal `latest`: the box line and the installer line are built
    # on different machines (2026-10-19: the Windows builder was absent), and one
    # being late must not freeze the other.
    if re.fullmatch(r"\d+\.\d+\.\d+", box_version) is None:
        raise BuildError(f"box 版本不是版本号：{box_version!r}")
    sha, size = digest_of(Path(box_path).expanduser())
    block = {"version": box_version, "url": box_url, "sha256": sha, "size": size}
    # 修复包编号（2026-10-22）：与版本号并列、不拼进版本字符串。老的解析器忽略这个
    # 新键，所以带上它不会让现场盒子读清单失败。
    if fixpack:
        if re.fullmatch(r"FP\d+", fixpack) is None:
            raise BuildError(f"box 修复包编号不合法：{fixpack!r}")
        block["fixpack"] = fixpack
    if notes:
        block["notes"] = notes
    return block


def replace_box_line(base: dict, box: tuple, box_fixpack: str = "",
                     box_notes: str = "") -> dict:
    """Return `base` with a freshly computed `boxLine` and nothing else changed.

    A box fix pack ships from a machine that usually has no 80 MB installer on it,
    and re-hashing an artifact we do not have is how a manifest starts describing
    something other than what is published. So the desktop block is carried over
    untouched; everything that *does* change (version, sha256, size, fixpack) comes
    from the tarball, and the result goes through the clients' own parser.
    """
    manifest = dict(base)
    manifest.pop("box", None)          # the pre-2026-10-19 key; `boxLine` replaced it
    manifest["boxLine"] = _box_block(box, box_fixpack, box_notes)
    up.parse_manifest(manifest)        # the same gate the boxes run
    return manifest


def build_manifest(installer: tuple, previous: tuple | None = None, notes: str = "",
                   mandatory: bool = False, min_supported: str = "",
                   released_at: str = "", expect_app: str = "",
                   box: tuple | None = None, box_fixpack: str = "",
                   box_notes: str = "") -> dict:
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
    if box:
        manifest["boxLine"] = _box_block(box, box_fixpack, box_notes)
    elif box_fixpack:
        raise BuildError("给了 --box-fixpack 却没有 --box：修复包编号没有宿主")
    # The same validation the clients run, so a bad manifest can never be published.
    up.parse_manifest(manifest)
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generate latest.json (update channel)")
    parser.add_argument("--installer", nargs=3, metavar=("VERSION", "URL", "FILE"),
                        help="安装包（版本、URL、本机文件）；--box-only 时可省略")
    parser.add_argument("--previous", nargs=3, metavar=("VERSION", "URL", "FILE"))
    parser.add_argument("--notes", default="")
    parser.add_argument("--mandatory", action="store_true")
    parser.add_argument("--min-supported", default="")
    parser.add_argument("--released-at", default="")
    parser.add_argument("--box", nargs=3, metavar=("VERSION", "URL", "FILE"),
                        help="盒子用的源码包（dev_tools/make_box_source.py 产出）")
    parser.add_argument("--box-fixpack", default="", metavar="FPn",
                        help="盒子线的修复包编号（FP1、FP2…）；可省略")
    parser.add_argument("--box-notes", default="",
                        help="写进 boxLine.notes 的一句话（更新卡上给操作员看）")
    parser.add_argument("--box-only", action="store_true",
                        help="只换清单里的盒子段：保留 --out 里现有的安装包/回退段，"
                             "不去重新哈希一个不在本机的安装包")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.box_only:
            if not args.box:
                raise BuildError("--box-only 需要 --box：至少要说清楚新的盒子包是什么")
            if args.installer:
                raise BuildError("--box-only 与 --installer 不能同时给：前者刻意不碰安装包段")
            base_path = Path(args.out)
            try:
                base = json.loads(base_path.read_text(encoding="utf-8"))
            except OSError as e:
                raise BuildError(f"读不到现有清单 {base_path}: {e}") from e
            except json.JSONDecodeError as e:
                raise BuildError(f"现有清单不是 JSON（{base_path}）: {e}") from e
            if not isinstance(base, dict):
                raise BuildError(f"现有清单不是一个 JSON 对象：{base_path}")
            manifest = replace_box_line(base, tuple(args.box),
                                        box_fixpack=args.box_fixpack,
                                        box_notes=args.box_notes)
        else:
            if not args.installer:
                raise BuildError("缺少 --installer（或者用 --box-only 只改盒子段）")
            manifest = build_manifest(
                tuple(args.installer), tuple(args.previous) if args.previous else None,
                notes=args.notes, mandatory=args.mandatory,
                min_supported=args.min_supported, released_at=args.released_at,
                expect_app=changelog_version(),
                box=tuple(args.box) if args.box else None,
                box_fixpack=args.box_fixpack, box_notes=args.box_notes)
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
