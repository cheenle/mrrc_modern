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


def new_files_since(base_tag: str) -> set:
    """自某个基线标签以来**新增**的文件（设备上不可能存在的那一批）。

    存在的理由是一个真事故（2026-10-30，FP20）：热修只写了 `server.py` +
    `assistant_client.py`，漏掉同一次功能里的 `static/assistant.html` / `.js` /
    `index.html`。结果是盒子升级后接口通了（`/api/assistant/status` → 200），
    但菜单里没有入口、页面 404 —— 客户看到的是「功能没上线」，日志里一片正常。

    补丁是**差分**，所以凡是基线里没有的文件都必须显式带上；漏了就一定坏。
    """
    return _git_name_only(base_tag, "--diff-filter=A")


def _git_name_only(ref: str, *extra: str) -> set:
    """`git diff --name-only <ref>..HEAD` 的一行一个路径。出错就当空集。"""
    import subprocess
    try:
        out = subprocess.run(["git", "diff", "--name-only", *extra, f"{ref}..HEAD"],
                             cwd=str(REPO), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return set()
    if out.returncode != 0:
        return set()
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def ships_to_device(rel: str) -> bool:
    """这个路径会不会落到盒子上（设备跑的是 staged 树，不是仓库）。

    前缀表必须与 `packaging/common/stage-tree.sh` 的排除表对齐 —— 前者说
    「装到设备上的」，后者说「stage 进树的」。`tools/` 曾在这张表里而 stage-tree
    排除它（`--exclude "/tools/"`），于是 2026-11-01 FP23 报了一个设备上根本不会
    有的文件，只能加 `--force` 绕过：一个会说假的守卫比没有守卫更糟。
    """
    # `packaging/` 不在这里：它是构建期的东西，`stage-tree.sh` 明确排除它
    # （`--exclude "packaging/"`）。把它算成出厂文件会让守卫对一堆永远不会
    # 上盒子的路径报警 —— 2026-11-01 FP23 已经吃过一次「守卫说谎」的亏。
    # `profiles/` 反过来必须算：`make_box_source.stage_profiles()` 会把它显式
    # 拷进 staged 树，`w103d.env` / `sunsdr2dx.env` 就是这么上盒子的。
    shipped = ("static/", "backends/", "linux/", "vendor/", "profiles/")
    return rel.startswith(shipped) or ("/" not in rel and rel.endswith(".py"))


def missing_changed_files(base_ref: str, included) -> list:
    """自 `base_ref` 以来**改过**、却没带上的出厂文件（新增 + 修改）。

    2026-11-17 的 FP73 撞出来的：热修只带了本次修复碰过的 `server.py`，而更早
    一个提交（`2f95292b`，登录限流可配置）改过 `config.py` —— 新的 `server.py`
    在模块层读 `config.LOGIN_MAX_ATTEMPTS`，于是补丁解到 1.28.85 的树上 import
    就抛 AttributeError。设备上的表现是「更新成功、服务起不来」，比装不上严重
    得多（没有网页、没有刷新入口，只能 SSH 回去）。

    `--base-tag` 只看得见**新增**文件；改过的旧文件它能说出来，但只有当基线
    是「上一版」时才有意义，所以它单独做一个开关（`--base-ref`）。
    """
    included = {str(p).strip().lstrip("./") for p in included}
    return sorted(m for m in _git_name_only(base_ref) - included
                  if (REPO / m).is_file() and ships_to_device(m))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "dist" / "box"))
    ap.add_argument("--version", required=True)
    ap.add_argument("--fixpack", default="", help="FPn；空则写一个空的 fixpack.txt 会被拒绝")
    ap.add_argument("files", nargs="+", help="改动文件的仓库内相对路径")
    ap.add_argument("--base-tag", default="",
                    help="本次热修的基线标签（如 v1.28.15）：用于发现漏带的新增文件")
    ap.add_argument("--base-ref", default="",
                    help="上一版发布提交（如 35e04dd9）：改过的出厂文件漏带了就拒绝构建")
    ap.add_argument("--force", action="store_true", help="明知漏了新增/改过的文件仍然构建")
    args = ap.parse_args()

    version = args.version.strip().lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"--version 必须是三段版本号（{version!r}）")
    if not re.fullmatch(r"FP\d+", args.fixpack or ""):
        # 空编号会被 apply 解释成"清掉上一个编号"，那比没有更误导（设计 §5）。
        raise SystemExit(f"--fixpack 必须是 FPn（{args.fixpack!r}）")

    if args.base_tag:
        # 新增文件在设备上不可能存在 —— 漏带它们，补丁就会"装成功但功能没上线"
        # （2026-10-30 FP20：接口 200、页面 404、菜单里没有入口）。
        # 只关心**盒子上真的要跑的文件**：docs/、website/、tests/ 这些新增多少都不会
        # 影响设备，全算进来会让提示变成噪音，反而没人看。
        # 这份前缀表必须与 `packaging/common/stage-tree.sh` 的 ".gitignore" 式排除
        # 一致 —— 前者写的是「装到设备上的」，后者决定「stage 进树的」。`tools/` 曾在
        # 这里而 stage-tree 排除它（`--exclude "/tools/"`），于是 2026-11-01 FP23 对
        # `tools/support_receiver/board_admin.py` 报了一个设备上根本不会有的文件，
        # 只能加 `--force` 绕过：一个会说假的守卫比没有守卫更糟。
        missing = sorted(m for m in new_files_since(args.base_tag) - set(args.files)
                         if (REPO / m).is_file() and ships_to_device(m))
        if missing and not args.force:
            raise SystemExit("这些文件自 %s 起是新增的，设备上不会有，补丁必须带上：\n  %s\n"
                             "（确认与本次修复无关再加 --force）"
                             % (args.base_tag, "\n  ".join(missing)))

    if args.base_ref:
        # 改过的文件与新增的同等致命：补丁是差分，设备上那些文件还是旧版。
        touched = missing_changed_files(args.base_ref, args.files)
        if touched and not args.force:
            raise SystemExit(
                "这些出厂文件自 %s 起改过，但不在本次补丁里：\n  %s\n\n"
                "热修是差分：设备上它们会停在上一版的代码，而新带的文件可能依赖新版"
                "（2026-11-17 FP73：server.py 读 config.LOGIN_MAX_ATTEMPTS，漏带\n"
                "config.py 的补丁装完就起不来）。把它们一并带上；确认无关再加 --force。"
                % (args.base_ref, "\n  ".join(touched)))

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
