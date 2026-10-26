#!/usr/bin/env python3
"""产出一份盒子用的**热修包**（`latest.json` 的 `boxLine.patch` 指向它）。

    ./.venv/bin/python dev_tools/make_box_patch.py --out dist/box \\
        --version 1.28.13 --fixpack FP9 \\
        upgrade_core.py box_update.py server.py

为什么需要这个脚本：热修包是「只含改动文件」的小包（3 KB 对 18.7 MB），但它**不能
只是那几个文件**。版本号是 `apply()` 从包里读的（`version_in(tree)` → `version.txt`），
而热修包天然没有这个文件——2026-10-27 现场（217 盒子）就是这么丢的：

    下载 3003 B 成功 → 覆盖成功 → 服务重启成功 → 版本号仍是 1.28.11
    → 更新卡永远提示同一个热修，每点一次"成功"一次

所以本脚本**总是**把 `version.txt` 与 `fixpack.txt` 打进包根，不接受用参数覆盖它
们；打完再用 `box_update.extract()` 自己解一遍，确认包里真的带着这两个戳。任何
一处不符就失败退出——宁可打不出包，也不要打出一个"装完还报旧版本"的包。

输出：
  <out>/mrrc-modern-patch-<ver>.tar.gz
  以及一段可直接贴进 latest.json 的 `patch` 片段（stdout）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import box_update  # noqa: E402  （同仓库模块；dev_tools 里的脚本都以仓库根为锚）

#: 这两个文件由本脚本生成，不接受从命令行传入 —— 让「忘记带版本号」无法表达。
STAMPS = ("version.txt", "fixpack.txt")


def _stage(files, staging: Path, version: str, fixpack: str) -> list:
    """把请求的文件按树内相对路径拷进 staging，再写两个戳。返回清单。"""
    written = []
    for rel in files:
        rel = rel.strip().lstrip("./")
        if rel in STAMPS:
            raise SystemExit(f"{rel} 由本脚本生成，不要作为 --files 传入")
        source = REPO / rel
        if not source.is_file():
            raise SystemExit(f"没有这个文件：{rel}")
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        written.append(rel)
    (staging / "version.txt").write_text(version + "\n", encoding="utf-8")
    (staging / "fixpack.txt").write_text(fixpack + "\n", encoding="utf-8")
    return written + list(STAMPS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "dist" / "box"))
    ap.add_argument("--version", required=True)
    ap.add_argument("--fixpack", default="", help="FPn；空则写一个空的 fixpack.txt 会被拒绝")
    ap.add_argument("files", nargs="+", help="改动文件的仓库内相对路径")
    args = ap.parse_args()

    version = args.version.strip().lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"--version 必须是三段版本号（{version!r}）")
    if not re.fullmatch(r"FP\d+", args.fixpack or ""):
        # 空编号会被 apply 解释成"清掉上一个编号"，那比没有更误导（设计 §5）。
        raise SystemExit(f"--fixpack 必须是 FPn（{args.fixpack!r}）")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "tree"
        staging.mkdir()
        contents = _stage(args.files, staging, version, args.fixpack)

        package = out / f"mrrc-modern-patch-{version}.tar.gz"
        # 包内路径不带前缀目录：`box_update.apply()` 认「根上就是树」这种形状，
        # 于是 stage_over() 按相对路径覆盖（带一层目录也只是多一次遍历，但小包
        # 没有必要）。
        with tarfile.open(package, "w:gz") as archive:
            for rel in sorted(contents):
                archive.add(staging / rel, arcname=f"./{rel}")

        # 自检：用设备端同一份解包器解一遍，确认两个戳真的在包里、值也对。
        probe = Path(tmp) / "probe"
        import os
        os.environ["MRRC_UPDATE_DIR"] = str(Path(tmp) / "state")
        count = box_update.extract(package, probe)
        got_version = box_update.version_in(probe)
        got_fixpack = box_update.read_fixpack(probe)
        if (got_version, got_fixpack) != (version, args.fixpack):
            raise SystemExit(
                f"自检失败：包里的版本/编号是 {got_version!r}/{got_fixpack!r}，"
                f"应为 {version}/{args.fixpack}")

    size = package.stat().st_size
    sha = hashlib.sha256(package.read_bytes()).hexdigest()
    fragment = {
        "version": version,
        "fixpack": args.fixpack,
        "url": f"https://www.vlsc.net/mrrc_modern/downloads/{package.name}",
        "sha256": sha,
        "size": size,
        "files": [f"./{rel}" for rel in sorted(contents)],
    }
    print(json.dumps(fragment, ensure_ascii=False, indent=2))
    print(f"\n# {package}  {size} bytes, {count} members", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
