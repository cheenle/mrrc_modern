#!/usr/bin/env python3
"""产出一份盒子用的源码包（`latest.json` 的 `box` 段就是指向它）。

    ./.venv/bin/python dev_tools/make_box_source.py --out dist/w103d

为什么不是「安装包」：W103D 上跑的就是源码树（`/opt/mrrc_modern`），升级 =
下载一份**已按 `packaging/common/stage-tree.sh` 过完排除清单**的 tar.gz、校验、
覆盖、重启。这个脚本只做前半段（stage + 打包 + 报数），设备端由 `box_update.py`
消费同样的结构。

输出：
  dist/w103d/mrrc-modern-src-<ver>.tar.gz
  以及一段可直接贴进 latest.json 的 `box` 片段（stdout）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def version_of(tree: Path) -> str:
    """The **box** line's version: `## [镜像线 vX.Y.Z]`, else the desktop heading.

    两条线各自编号（2026-10-19）：Windows 构建机缺席时桌面包线会停在旧版本，而盒子
    源码包必须跟着盒子线走——否则盒子下载了一个自称旧版本的包，升级后又"发现新版本"。
    """
    text = (tree / "CHANGELOG.md").read_text(encoding="utf-8")
    for pattern in (r"##\s*\[镜像线 v(\d+\.\d+\.\d+)\]",
                    r"##\s*\[v(\d+\.\d+\.\d+)\]"):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    raise SystemExit("CHANGELOG.md 里找不到 ## [镜像线 vX.Y.Z] 或 ## [vX.Y.Z]")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "dist" / "w103d"))
    ap.add_argument("--version", default="")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    version = args.version or version_of(REPO)

    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "tree"
        subprocess.run(["bash", str(REPO / "packaging" / "common" / "stage-tree.sh"),
                        str(REPO), str(staged)], check=True)
        (staged / "version.txt").write_text(version + "\n", encoding="utf-8")
        (staged / "VERSION").write_text(version + "\n", encoding="utf-8")

        package = out / f"mrrc-modern-src-{version}.tar.gz"
        # 一层目录：设备端直接解到 staging 里，再找 server.py（box_update 两种形状都认）
        with tarfile.open(package, "w:gz") as archive:
            archive.add(staged, arcname=f"mrrc-modern-{version}")

    size = package.stat().st_size
    sha = hashlib.sha256(package.read_bytes()).hexdigest()
    print(json.dumps({"box": {
        "version": version,
        "url": f"https://www.vlsc.net/mrrc_modern/downloads/{package.name}",
        "sha256": sha,
        "size": size,
    }}, ensure_ascii=False, indent=2))
    print(f"\n# {package}  {size} bytes", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
