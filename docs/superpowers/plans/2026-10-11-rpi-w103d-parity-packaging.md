# 树莓派镜像与 W103D 对齐（打包结构统一）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把"往 rootfs 里装 MRRC 服务"这件事收敛成唯一实现（`packaging/common/`），Pi 与 W103D 两条构建线都变成薄壳 + 少量显式参数，并各自真跑一次、用四步验证产物。

**架构：** 服务层脚本 `mrrc-service-layer.sh` 在两侧 chroot 内运行、无平台分支；平台差异只有 `MRRC_WEB_HOST` 与 `MRRC_BOOT_DIR` 两个参数；排除清单只剩 `stage-tree.sh` 一处，由 Pi 构建器（宿主侧）、box overlay（chroot 内）、就地升级器（拉下来的新树）三处调用；Pi 构建新增四个镜像源改写点。

**技术栈：** bash 3.2（宿主侧）/ bash 5（chroot 内）、pi-gen（pinned `2026-06-18-raspios-bookworm-arm64`）、Docker（`--privileged`、arm64 原生）、rsync、systemd、unittest。

**设计依据：** `docs/superpowers/specs/2026-10-11-rpi-w103d-parity-packaging-design.md`（已提交 `8c429fb`）。

---

## 全局约束

- **不发布任何东西**：不动网站、不动 `release-artifacts.json`、不改变 `dist/` 里已有产物的名字。唯一的例外是任务 12（修基线那 1 红，需先核对线上 URL）。
- **在 main 上直接提交**，小步频繁。理由：这是"下一个人要继续往上搭"的结构改动（W103D 设置页 Task 13 会往服务层加 polkit/helper 单元），放分支上会诱使后续工作继续搭在旧结构上。工作期间 **`packaging/box/box-overlay.sh` 归本计划独占**，任务 6 一落地就提交，缩短窗口。
- **不动服务端 Python**（`server.py`、`box_ops.py`、`net_wifi.py`）——那是另一份在飞的设计。
- **测试用仓库的 venv 跑**：`/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/python -m unittest …`。系统 `python3` 会给出 156 个 `ModuleNotFoundError` 假故障。
- **构建跑着的时候不要改脚本**：bash 增量读取脚本文件，`build-image.sh` / `box-overlay.sh` / `stage-tree.sh` 运行中被编辑会读坏。
- **`packaging/box/box-overlay.sh` 的现役语义必须逐条保留**（口令/串口/证书留给首启、D-11 两行、日志落盘、导入门禁、回收），本计划只搬位置、不删内容。
- 提交信息用中文、沿用仓库风格（`feat:` / `fix:` / `refactor:` / `test:` / `docs:`）。

## 已知的当场缺陷（本计划必须修）

`packaging/box/box-overlay.sh`、`packaging/rpi/build-image.sh`、`linux/mrrc_update.sh` 各有一处**运行期致命**的写法：注释行夹在 `rsync` 的续行参数表中间。bash 里 `\` 续行后 `#` 在词首即开始注释并结束整条命令，于是紧随其后的 `--exclude …` 变成一条新命令：

```
$ printf 'echo one \\\n\t--two \\\n\t# c\n\t--three\n' > /tmp/t.sh && bash /tmp/t.sh
one --two
/tmp/t.sh: 行 5: --three: 未找到命令        # exit 127
```

`bash -n` 看不出来（运行期语义），`ExclusionParityTests` 也看不出来（正则扫原文照样数到全部 exclude）。这三次提交都发生在 2026-10-11 的 W103D 构建**之后**，所以已发布的 1.25.5 是好的，下一次构建会挂。任务 1 先立一条广义守卫，任务 2/6/7/9 让这三个文件不再有第二份清单，顺带修掉。

---

## 文件结构

**创建**

| 文件 | 职责 |
| --- | --- |
| `packaging/common/stage-tree.sh` | 排除清单唯一出处 + `rsync` 落树；bash 3.2 兼容；三处调用 |
| `packaging/common/mrrc-service-layer.sh` | 服务层全部步骤（chroot 内，无平台分支） |
| `packaging/common/packages.txt` | 系统包清单唯一出处 |
| `packaging/common/mrrc-show-password` | 从 `packaging/rpi/.../files/usr/local/bin/` 搬来 |
| `packaging/common/motd-10-mrrc` | 登录横幅（从 rpi stage4 `files/etc/update-motd.d/10-mrrc` 搬来并动态化主机名） |
| `packaging/common/part-nodes.sh` | 从 `packaging/box/` 搬来（Pi 的产物验证也要用它） |
| `tests/test_packaging_parity.py` | 守卫测试（8 条） |
| `docs/rpi_pack.md` | Pi 构建手册（与 `docs/w103d_pack.md` 同构） |

**移动（`git mv`）**

| 从 | 到 |
| --- | --- |
| `packaging/box/firstboot_wrapper.py` | `packaging/common/firstboot_wrapper.py`（并参数化） |
| `packaging/box/fetch-frpc.sh` | `packaging/common/fetch-frpc.sh` |
| `packaging/box/profiles/*.env`（11 份） | `packaging/common/profiles/*.env` |
| `packaging/box/part-nodes.sh` | `packaging/common/part-nodes.sh` |

**修改**

| 文件 | 改动 |
| --- | --- |
| `packaging/box/box-overlay.sh` | 瘦身为平台壳 |
| `packaging/box/build-image.sh` | 只改对 `part-nodes.sh` 的引用路径 |
| `packaging/rpi/build-image.sh` | 镜像源改写、调 `stage-tree.sh`、备素材与 `build.env`、`TARGET_HOSTNAME` |
| `packaging/rpi/pi-gen-stage4/prerun.sh` | FATAL 校验四项 |
| `packaging/rpi/pi-gen-stage4/01-deploy-mrrc/00-run-chroot.sh` | 极薄壳，转调服务层 |
| `packaging/rpi/pi-gen-stage4/00-install-packages/00-packages` | 只留 `e2fsprogs` |
| `packaging/rpi/verify-image.sh` | 升级为四步验证 |
| `linux/mrrc_update.sh` | 改成驱动器 |
| `tests/test_box_profiles.py` | `PROFILE_DIR` 改路径；删掉已被取代的 `ExclusionParityTests` |
| `tests/test_image_imports.py` | `BUILDER` 指向 `stage-tree.sh`，文档串同步 |
| `tests/test_mrrc_radio.py` | profiles 路径 |
| `AGENTS.md`、`.agents/skills/dual-platform-release/SKILL.md` | 路径与已退役描述订正 |
| `website/index.html` | 任务 12：W103D 卡片的字节数与 SHA |

**删除**

| 路径 | 原因 |
| --- | --- |
| `packaging/rpi/pi-gen-stage4/01-deploy-mrrc/files/` | 整个树删掉（含两个提交进仓库的 `.pyc`）；服务层产出不再有第二份 |

---

## 任务 0：隔离与基线

**文件：** 无

- [ ] **步骤 1：确认工作区干净（对你要动的文件而言）**

运行：

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
git status --short packaging/ linux/mrrc_update.sh tests/test_packaging_parity.py
```

预期：`packaging/` 与 `linux/mrrc_update.sh`、`tests/` 下**没有**未提交改动（仓库里其它地方有别人在飞的改动，例如 `box_ops.py`、`website/`，不要去碰、不要 `git add -A`）。

- [ ] **步骤 2：记下基线**

运行：

```bash
.venv/bin/python -m unittest discover -s tests 2>&1 | tail -4
```

预期：`Ran 1898 tests`、`FAILED (failures=1, skipped=1)`，失败项是 `test_release_artifacts.RepositoryStateTests.test_offline_release_check_is_clean`（`[website-w103d-card]`）。任务 12 修它，其余任务是不要让这个数字变坏。

- [ ] **步骤 3：确认 Docker 与缓存**

运行：

```bash
docker info >/dev/null 2>&1 && echo "docker OK"
ls -d build/w103d/w103d.img build/pi-gen/.git 2>/dev/null
docker images --format '{{.Repository}}:{{.Tag}}' | grep -E 'pi-gen|debian'
```

预期：`docker OK`、两个缓存路径都存在、`pi-gen:latest` 与 `debian:bookworm` 都在。

---

## 任务 1：守卫测试（先写红的）

**文件：**

- 创建：`tests/test_packaging_parity.py`

- [ ] **步骤 1：写测试文件**

创建 `tests/test_packaging_parity.py`：

```python
"""Two image lines, one service layer — and this is what keeps it that way.

The Pi image and the W103D box image install the same server into the same
``/opt/mrrc_modern``. Everything they share lives in ``packaging/common/`` now;
before 2026-10-11 it lived in both ``packaging/box/`` and ``packaging/rpi/``, and
the drift was concrete rather than theoretical: ``firstboot_wrapper.py`` existed
twice (differing in the boot path, the bind address and a docstring), the
``mrrc-show-password`` helper lived under ``packaging/rpi/`` and the box overlay
reached across to fetch it, the exclusion list had three byte-identical copies
kept in sync by ``ExclusionParityTests``, and the Pi's systemd unit was missing
the ``Environment=MRRC_CONFIG_FILE`` line that makes Cloud Hub work (D-11).

Every test here fails against that layout and passes against the unified one, so
the unification cannot be quietly undone by adding "just one" platform copy.
"""
from __future__ import annotations

import re
import stat
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMMON = REPO / "packaging" / "common"

#: Where the exclusion list is allowed to live. One entry, on purpose.
STAGE_TREE = COMMON / "stage-tree.sh"

#: Files the service layer generates. None of them may exist in a platform shell.
SERVICE_LAYER_OUTPUT = (
    "mrrc-modern.service",
    "mrrc-firstboot.service",
    "firstboot_wrapper.py",
    "mrrc-show-password",
    "10-mrrc",
)

#: What each platform shell must call. (path, [required substrings])
CALL_SITES = (
    ("packaging/rpi/build-image.sh", ("stage-tree.sh",)),
    ("packaging/rpi/pi-gen-stage4/01-deploy-mrrc/00-run-chroot.sh",
     ("mrrc-service-layer.sh",)),
    ("packaging/box/box-overlay.sh", ("stage-tree.sh", "mrrc-service-layer.sh")),
)

#: The only values allowed to differ between the two lines, and what they are.
WEB_HOST = {"packaging/rpi": "::", "packaging/box": "0.0.0.0"}
BOOT_DIR = {"packaging/rpi": "/boot/firmware", "packaging/box": "/boot"}

#: Packages the Pi stage may install on its own. Anything else has to come from
#: packaging/common/packages.txt — one answer to "what does the server need".
PI_PLATFORM_PACKAGES = frozenset({"e2fsprogs"})


class ExclusionListTests(unittest.TestCase):
    def _carriers(self) -> list[Path]:
        found = []
        for path in sorted(REPO.glob("packaging/**/*.sh")) + [
            REPO / "linux" / "mrrc_update.sh"
        ]:
            if "--exclude" in path.read_text(encoding="utf-8"):
                found.append(path.relative_to(REPO))
        return found

    def test_the_exclusion_list_lives_in_exactly_one_file(self):
        self.assertEqual([str(STAGE_TREE.relative_to(REPO))],
                         [str(p) for p in self._carriers()],
                         "the per-image exclusion list has a second home — that is "
                         "how a file ends up in one image and missing from the other")

    def test_the_one_list_still_carries_every_rule(self):
        """A list that got emptied to make the test above pass is worse than no test."""
        text = STAGE_TREE.read_text(encoding="utf-8")
        entries = re.findall(r'--exclude\s+"([^"]+)"', text)
        self.assertGreaterEqual(len(entries), 40, f"only {len(entries)} exclude rules")
        for canary in ("/.env", "/recordings/", "website/", "packaging/", "tests/"):
            with self.subTest(canary=canary):
                self.assertIn(canary, entries)

    def test_the_exclusion_list_is_byte_identical_to_what_shipped(self):
        """The 2026-10-11 list, frozen: the refactor moved it, it did not edit it."""
        expected = [
            ".git/", "venv/", ".venv/", "dist/", "build/", "logs/", "certs/",
            "promo/", "FT710Mobile/", "FT710Android/", "website/", "tests/",
            ".agents/", "docs/", "SDD/", "packaging/", "macos/", "windows/",
            "__pycache__/", "*.pyc", ".DS_Store", "atr1000_tuner.json",
            "/.local/", "/.env", "/recordings/", "/mem_channels.json",
            "/.worktrees/", "/.superpowers/", "/.agnes/", "/.claude/",
            "/.pytest_cache/", "/.ruff_cache/", "/dev_tools/", "/tools/",
            "/support-out/", "/lib/", "/biome.json", "/build_mrrc_v1101.ps1",
            "/*.md", "/*.pdf", "/*.jpg", "/*.JPG", "/*.PNG", "/*.m4a",
        ]
        got = re.findall(r'--exclude\s+"([^"]+)"', STAGE_TREE.read_text(encoding="utf-8"))
        self.assertEqual(got, expected)


class CallSiteTests(unittest.TestCase):
    def test_every_builder_calls_the_shared_layers(self):
        for relative, required in CALL_SITES:
            text = (REPO / relative).read_text(encoding="utf-8")
            for needle in required:
                with self.subTest(path=relative, needle=needle):
                    self.assertIn(needle, text, f"{relative} does not use {needle}")


class PiStageTests(unittest.TestCase):
    def test_the_pi_stage_carries_no_service_layer_output(self):
        """The regression gate for every duplicate this refactor removed."""
        stage = REPO / "packaging" / "rpi" / "pi-gen-stage4"
        names = {p.name for p in stage.rglob("*") if p.is_file()}
        for name in SERVICE_LAYER_OUTPUT:
            with self.subTest(name=name):
                self.assertNotIn(name, names,
                                 f"{name!r} is generated by packaging/common — a copy "
                                 f"here is a second implementation of the same unit")
        pyc = [p for p in stage.rglob("*.pyc")]
        self.assertEqual(pyc, [], "a committed .pyc ships inside the image")

    def test_the_pi_package_list_holds_only_platform_extras(self):
        text = (REPO / "packaging" / "rpi" / "pi-gen-stage4" /
                "00-install-packages" / "00-packages").read_text(encoding="utf-8")
        entries = {line.strip() for line in text.splitlines()
                   if line.strip() and not line.strip().startswith("#")}
        self.assertTrue(entries <= PI_PLATFORM_PACKAGES,
                        f"not platform extras: {sorted(entries - PI_PLATFORM_PACKAGES)}")
        shared = {line.strip() for line in (COMMON / "packages.txt")
                  .read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.strip().startswith("#")}
        self.assertEqual(entries & shared, set(),
                         "the Pi stage re-lists packages the service layer installs")


class IntentionalDivergenceTests(unittest.TestCase):
    """Differences must be asserted, not forgotten. There are exactly two."""

    def _assignments(self, root: str, key: str) -> list[tuple[str, str]]:
        out = []
        for path in sorted((REPO / root).rglob("*")):
            if not path.is_file() or path.suffix not in (".sh", ".env", ".py", ""):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for match in re.finditer(rf"{key}=(?:'|\")?([^'\"\s]+)", text):
                out.append((str(path.relative_to(REPO)), match.group(1)))
        return out

    def test_the_web_bind_is_the_only_bind_divergence(self):
        pi = dict(self._assignments("packaging/rpi", "MRRC_WEB_HOST"))
        box = dict(self._assignments("packaging/box", "MRRC_WEB_HOST"))
        self.assertEqual(set(pi.values()), {WEB_HOST["packaging/rpi"]})
        self.assertEqual(set(box.values()), {WEB_HOST["packaging/box"]})

    def test_the_boot_partition_is_the_only_path_divergence(self):
        pi = dict(self._assignments("packaging/rpi", "MRRC_BOOT_DIR"))
        box = dict(self._assignments("packaging/box", "MRRC_BOOT_DIR"))
        self.assertEqual(set(pi.values()), {BOOT_DIR["packaging/rpi"]})
        self.assertEqual(set(box.values()), {BOOT_DIR["packaging/box"]})


class ShellHygieneTests(unittest.TestCase):
    """Failure modes that no test-driven shell script ever catches on its own."""

    SHELLS = ("packaging/common/*.sh", "packaging/box/*.sh", "packaging/rpi/*.sh")

    def _shells(self) -> list[Path]:
        paths: list[Path] = []
        for pattern in self.SHELLS:
            paths.extend(sorted(REPO.glob(pattern)))
        paths.append(REPO / "linux" / "mrrc_update.sh")
        return paths

    def test_shell_scripts_are_executable(self):
        """box-overlay.sh shipped as 0644 once: "bad interpreter: Permission denied"."""
        for path in self._shells():
            with self.subTest(path=str(path.relative_to(REPO))):
                mode = path.stat().st_mode
                self.assertTrue(mode & stat.S_IXUSR, f"{path} is not executable")

    def test_shared_scripts_are_bash_32_compatible(self):
        """The Pi builder runs stage-tree.sh with macOS's /bin/bash (3.2)."""
        banned = ("declare -A", "readarray", "mapfile")
        for path in sorted(COMMON.glob("*.sh")):
            text = path.read_text(encoding="utf-8")
            for needle in banned:
                with self.subTest(path=path.name, needle=needle):
                    self.assertNotIn(needle, text)
            self.assertIsNone(re.search(r"\$\{[A-Za-z_][A-Za-z0-9_]*,,\}", text),
                              f"{path.name} uses ${var,,} (bash 4)")

    def test_the_service_layer_still_reclaims_its_scratch(self):
        """Both directories hold build scratch; either one left behind ships."""
        text = (COMMON / "mrrc-service-layer.sh").read_text(encoding="utf-8")
        self.assertIn("rm -rf /tmp/*", text)
        self.assertIn('rm -rf "$MRRC_ASSETS"', text)

    def test_no_comment_inside_a_continued_argument_list(self):
        r"""A `#` after a line continuation ends the COMMAND, not the line.

        Committed to all three builders on 2026-10-11 and shipped nowhere: the
        comments were inserted between the --exclude groups *after* that day's
        image was built, so the next build of either image would have died with
        `--exclude: command not found` (exit 127) at the rsync.

        Neither `bash -n` nor a regex over the text can see it — the first is a
        parse-time check and this is runtime semantics, the second finds every
        --exclude regardless of whether bash ever passes it to rsync. So it gets
        its own rule: a continued line whose following comment line is in turn
        followed by an indented line (i.e. more arguments) is a defect.
        """
        offenders = []
        for path in self._shells():
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if not line.rstrip().endswith("\\"):
                    continue
                j = i + 1
                saw_comment = False
                while j < len(lines) and (not lines[j].strip()
                                          or lines[j].lstrip().startswith("#")):
                    saw_comment = saw_comment or lines[j].lstrip().startswith("#")
                    j += 1
                if saw_comment and j < len(lines) and lines[j][:1].isspace():
                    offenders.append(f"{path.relative_to(REPO)}:{i + 1}")
        self.assertEqual(offenders, [],
                         "a comment inside a continued argument list turns the rest "
                         "of the options into their own command")


class PackageListTests(unittest.TestCase):
    def test_libopus_is_named(self):
        """Without it the server silently falls back to PCM instead of Opus."""
        text = (COMMON / "packages.txt").read_text(encoding="utf-8")
        self.assertIn("libopus0", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：跑它，确认红**

运行：

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m unittest tests.test_packaging_parity -v 2>&1 | tail -25
```

预期：大量 FAIL/ERROR —— `packaging/common/` 不存在（`FileNotFoundError`）、排除清单有三个载体、`--exclude: command not found` 那条在三个文件上各报一处。

- [ ] **步骤 3：Commit（红测试先落盘）**

```bash
git add tests/test_packaging_parity.py
git commit -m "test: 打包平权守卫 —— 排除清单只有一处、服务层产出不得有第二份副本" \
  -m "含一条新守卫：续行后紧跟注释、再下一行是缩进参数 = 运行期致命（bash 会把后续 --exclude 当命令）。三处构建脚本今天都中了这条。"
```

---

## 任务 2：`packaging/common/stage-tree.sh` + `part-nodes.sh` 搬家

**文件：**

- 创建：`packaging/common/stage-tree.sh`
- 移动：`packaging/box/part-nodes.sh` → `packaging/common/part-nodes.sh`
- 修改：`packaging/box/build-image.sh`（引用路径）

- [ ] **步骤 1：写 `stage-tree.sh`**

创建 `packaging/common/stage-tree.sh`：

```bash
#!/bin/bash
# Stage the repository tree into an image's /opt/mrrc_modern — the ONE place the
# exclusion list lives.
#
# Three callers, one list:
#   * packaging/rpi/build-image.sh    on the build host (macOS), staging into pi-gen
#   * packaging/box/box-overlay.sh    inside the Armbian chroot, /src → /opt/mrrc_modern
#   * linux/mrrc_update.sh            on the box, using the copy in the tree it fetched
#
# Two of them disagreeing is how a file ends up in one image and missing from the
# other, and the symptom is a runtime ImportError rather than a build error, so
# tests/test_packaging_parity.py fails the build when this list grows a second home.
#
# bash 3.2 on purpose: the Pi builder runs this with macOS's /bin/bash.
#
# usage: stage-tree.sh <src> <dst>
set -euo pipefail

SRC="${1:?usage: stage-tree.sh <src> <dst>}"
DST="${2:?usage: stage-tree.sh <src> <dst>}"
[ -d "$SRC" ] || { echo "stage-tree: no such source tree: $SRC" >&2; exit 1; }
mkdir -p "$DST"

# Deliberately no --delete: image builds stage into an empty directory, and the
# in-place updater's --delete is a separate decision (see the note in
# linux/mrrc_update.sh) rather than something to smuggle in with this extraction.
#
# Why the prose lives here and not inside the argument list: a `#` on a line that
# continues an argument list ENDS THE COMMAND, so every option after it becomes a
# new command called --exclude. Three builders shipped that on 2026-10-11 and no
# test could see it (bash -n is a parse-time check and this is runtime semantics;
# a regex over the text finds every --exclude whether or not bash ever passes it
# on). ShellHygieneTests.test_no_comment_inside_a_continued_argument_list is the
# rule that keeps it from coming back.
#
# `packaging/` is excluded — the service layer's own assets (profiles,
# fetch-frpc.sh, firstboot_wrapper.py, the units' source) are staged separately
# by each builder into /opt/mrrc-build.
#
# The root-anchored (/x) rules match only the repo root: per-box state (instance
# identity, recordings, operator files) and dev-only material must never be
# shipped by an image, nor overwritten by an update.
rsync -a "$SRC/" "$DST/" \
 --exclude ".git/" --exclude "venv/" --exclude ".venv/" --exclude "dist/" \
 --exclude "build/" --exclude "logs/" --exclude "certs/" --exclude "promo/" \
 --exclude "FT710Mobile/" --exclude "FT710Android/" --exclude "website/" \
 --exclude "tests/" --exclude ".agents/" --exclude "docs/" --exclude "SDD/" \
 --exclude "packaging/" --exclude "macos/" --exclude "windows/" \
 --exclude "__pycache__/" --exclude "*.pyc" --exclude ".DS_Store" \
 --exclude "atr1000_tuner.json" \
 --exclude "/.local/" --exclude "/.env" --exclude "/recordings/" \
 --exclude "/mem_channels.json" --exclude "/.worktrees/" --exclude "/.superpowers/" \
 --exclude "/.agnes/" --exclude "/.claude/" --exclude "/.pytest_cache/" \
 --exclude "/.ruff_cache/" --exclude "/dev_tools/" --exclude "/tools/" \
 --exclude "/support-out/" --exclude "/lib/" --exclude "/biome.json" \
 --exclude "/build_mrrc_v1101.ps1" --exclude "/*.md" --exclude "/*.pdf" \
 --exclude "/*.jpg" --exclude "/*.JPG" --exclude "/*.PNG" --exclude "/*.m4a"

echo "stage-tree: $SRC → $DST ($(find "$DST" -type f | wc -l | tr -d ' ') files)"
```

- [ ] **步骤 2：设可执行位并搬 `part-nodes.sh`**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
chmod 0755 packaging/common/stage-tree.sh
git mv packaging/box/part-nodes.sh packaging/common/part-nodes.sh
grep -n "part-nodes" packaging/box/build-image.sh
```

预期：`grep` 打出引用行（形如 `-v "$HERE":/box:ro` 后的 `. /box/part-nodes.sh`）。

- [ ] **步骤 3：改 `packaging/box/build-image.sh` 的引用**

把 `build-image.sh` 里指向 `packaging/box/part-nodes.sh` 的那一处改成 `packaging/common/part-nodes.sh`（容器内的挂载路径随之变）。若该脚本把 `packaging/box` 挂到 `/box`，追加一个 `-v "$REPO/packaging/common":/common:ro`，并把容器内引用写成 `/common/part-nodes.sh`。

- [ ] **步骤 4：跑测试**

```bash
.venv/bin/python -m unittest tests.test_packaging_parity 2>&1 | tail -12
```

预期：`ExclusionListTests` 三条全绿（唯一载体、条目仍在、逐字冻结列表）；`test_no_comment_inside_a_continued_argument_list` 仍然红（另外两个文件还没改）；`CallSiteTests` 仍红。

- [ ] **步骤 5：Commit**

```bash
git add packaging/common/stage-tree.sh packaging/common/part-nodes.sh packaging/box/build-image.sh
git commit -m "refactor(packaging): 排除清单收敛到 common/stage-tree.sh 一处" \
  -m "同时把 part-nodes.sh 搬进 common —— Pi 的产物验证要用同一段 mknod 逻辑。"
```

---

## 任务 3：`packaging/common/` 素材搬家与参数化

**文件：**

- 移动并修改：`packaging/common/firstboot_wrapper.py`
- 移动：`packaging/common/fetch-frpc.sh`、`packaging/common/profiles/*.env`
- 创建：`packaging/common/mrrc-show-password`、`packaging/common/motd-10-mrrc`

- [ ] **步骤 1：搬家**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
mkdir -p packaging/common
git mv packaging/box/firstboot_wrapper.py packaging/common/firstboot_wrapper.py
git mv packaging/box/fetch-frpc.sh packaging/common/fetch-frpc.sh
git mv packaging/box/profiles packaging/common/profiles
git mv packaging/rpi/pi-gen-stage4/01-deploy-mrrc/files/usr/local/bin/mrrc-show-password \
       packaging/common/mrrc-show-password
git mv packaging/rpi/pi-gen-stage4/01-deploy-mrrc/files/etc/update-motd.d/10-mrrc \
       packaging/common/motd-10-mrrc
chmod 0755 packaging/common/mrrc-show-password packaging/common/motd-10-mrrc
chmod 0755 packaging/common/fetch-frpc.sh
```

- [ ] **步骤 2：给 `firstboot_wrapper.py` 加平台参数**

在 `packaging/common/firstboot_wrapper.py` 里做四处改动（其余逐字保留，包括证书属主那段）：

1) 模块文档串头两句改成同时覆盖两条线：

```python
"""mrrc-firstboot.service entry point (root): preseed → auto-config → cert.

Runs once on the first boot of a freshly flashed MRRC Modern image — the Armbian
box (ZTE W103D, boot partition at ``/boot``) and the Raspberry Pi (Raspberry Pi
OS, boot partition at ``/boot/firmware``). Both facts come from the unit as
environment variables rather than from two copies of this file: the overlay that
writes the unit passes the platform's value, so there is one implementation and
two configuration lines.
```

1) 常量区改成读环境变量：

```python
ENV_DIR = Path("/opt/mrrc_modern/env")
ENV_FILE = ENV_DIR / "mrrc.env"
#: Set by mrrc-firstboot.service: /boot on Armbian, /boot/firmware on Raspberry
#: Pi OS. These are the two platforms this file serves, and a wrong value is
#: silent — `adopt_preseed()` simply finds no file and auto-generates a password
#: the operator did not ask for.
BOOT_DIR = Path(os.environ.get("MRRC_BOOT_DIR", "/boot"))
PRESEED = BOOT_DIR / "mrrc.env"
#: Set by mrrc-firstboot.service from packaging/common/mrrc-service-layer.sh's
#: MRRC_WEB_HOST (0.0.0.0 on the box, :: on the Pi). Only used when the env file
#: being written has no MRRC_WEB_HOST of its own — i.e. on the preseed path.
WEB_HOST = os.environ.get("MRRC_WEB_HOST", "0.0.0.0")
DONE = Path("/var/lib/mrrc/firstboot-done")
CERT_DIR = Path("/var/lib/mrrc/certs")
```

1) `main()` 里那段默认值用 `WEB_HOST`：

```python
    for k, v in (("MRRC_WEB_HOST", WEB_HOST), ("MRRC_WEB_PORT", "8888"),
                 ("MRRC_SSL_CERT", str(CERT_DIR / "server.crt")),
                 ("MRRC_SSL_KEY", str(CERT_DIR / "server.key"))):
```

1) 文档串第 4 条里的 "dual-stack bind" 改成 "the platform's bind address"。

- [ ] **步骤 3：写 `motd-10-mrrc`（主机名动态）**

覆盖 `packaging/common/motd-10-mrrc`：

```sh
#!/bin/sh
# Shown at login (§ "MRRC Modern: …"). The hostname is asked for, not baked in:
# the image ships with MRRC-Modern, and the settings page (or the operator) can
# change it, so a hard-coded "raspberrypi.local" would go stale the first time
# somebody renamed the box.
echo "  MRRC Modern: https://$(hostname).local:8888   (web password: sudo mrrc-show-password)"
```

- [ ] **步骤 4：跑测试**

```bash
.venv/bin/python -m unittest tests.test_box_profiles tests.test_mrrc_radio tests.test_image_imports 2>&1 | tail -20
```

预期：**红** —— `test_box_profiles` 的 `PROFILE_DIR` 还指向旧路径，`test_mrrc_radio` 同。这是任务 10 的活，先记下；本步骤只是确认失败原因是"路径搬家"而不是别的。

- [ ] **步骤 5：Commit**

```bash
git add -A packaging/common packaging/box packaging/rpi
git commit -m "refactor(packaging): 共用素材搬进 common/ —— profiles、frpc、首启包装、口令助手、motd" \
  -m "firstboot_wrapper 的平台差异改成由 unit 传环境变量（MRRC_BOOT_DIR / MRRC_WEB_HOST），从此只有一份实现。"
```

---

## 任务 4：`packaging/common/packages.txt`

**文件：**

- 创建：`packaging/common/packages.txt`

- [ ] **步骤 1：写文件**

```text
# System packages every MRRC Modern image needs. One answer to "what does the
# server need", installed by packaging/common/mrrc-service-layer.sh on both lines.
#
# Measured, not assumed: on the pinned Armbian base (packaging/box/, 2026-10-11)
# install.sh could not create its virtualenv because python3.11-venv, pip, the
# PortAudio/ALSA headers and libopus0 were all absent — only libasound2, gcc,
# make, rsync, git, curl and sudo were there. install.sh cannot repair that
# itself: it creates the virtualenv in STEP 2 and installs system packages in
# STEP 3, so a missing ensurepip is fatal before apt ever runs, and a venv made
# without it has no pip at all.
#
# libopus0 is the one that is easy to miss and expensive to miss: without it the
# server silently falls back to PCM instead of Opus.
#
# python3.11-venv is deliberately pinned to the base's Python (both bases are
# bookworm / 3.11 today). A base bump must re-measure this file; apt fails loudly
# on a package name that no longer exists, which is the outcome we want.
python3.11-venv
python3-dev
portaudio19-dev
libportaudio2
libasound2-dev
libopus0
libopus-dev
avahi-daemon
```

- [ ] **步骤 2：跑测试**

```bash
.venv/bin/python -m unittest tests.test_packaging_parity.PackageListTests -v
```

预期：PASS（`libopus0` 在位）。

- [ ] **步骤 3：Commit**

```bash
git add packaging/common/packages.txt
git commit -m "refactor(packaging): 系统包清单收敛到 common/packages.txt"
```

---

## 任务 5：`packaging/common/mrrc-service-layer.sh`

**文件：**

- 创建：`packaging/common/mrrc-service-layer.sh`

- [ ] **步骤 1：写服务层**

创建 `packaging/common/mrrc-service-layer.sh`（内容搬运自 `packaging/box/box-overlay.sh`，逐条保留语义，只做参数化与去平台化）：

```bash
#!/bin/bash -e
# The MRRC Modern service layer: everything that turns a bare rootfs into an
# appliance, for both image lines. Runs INSIDE the target rootfs as root.
#
# Every step is idempotent: a build that failed halfway can be re-run.
#
# Inputs (environment; each has a default so this script is runnable by hand):
#   MRRC_ASSETS       this layer's own assets          (default /opt/mrrc-build)
#   MRRC_HOME         install location                 (default /opt/mrrc_modern)
#   MRRC_USER         service account, must already exist (default mrrc)
#   MRRC_VERSION      version.txt / VERSION, or a CHANGELOG.md to read it from
#   MRRC_WEB_HOST     bind address written to the env file (default 0.0.0.0; the
#                     Pi passes :: — the one bind divergence, asserted in tests)
#   MRRC_BOOT_DIR     boot partition a preseed mrrc.env may sit on (default /boot;
#                     the Pi passes /boot/firmware). Written into the unit; only
#                     firstboot_wrapper.py reads it.
#   MRRC_EXTRA_PACKAGES  space-separated platform extras
#   REPO_SRC          the tree the caller staged from — used ONLY to find
#                     CHANGELOG.md when MRRC_VERSION is unset (default /src)
#   PIP_INDEX_URL     honoured by pip itself; each builder exports it
#
# Deliberately NOT here: anything needing hardware. The web password, the serial
# port, the USB sound card and the self-signed certificate are all resolved on
# the box's first boot (design D-3). Baking a guess in produces a configuration
# that looks usable and cannot work.
#
# Deliberately NOT here either: the board- and distro-specific bits (the W103D's
# MT7663S firmware, its dnsmasq/FIQ masks, whether the account comes from
# adduser or from pi-gen's FIRST_USER_NAME). Those live in the platform shells,
# which call this script.
set -euo pipefail

MRRC_ASSETS="${MRRC_ASSETS:-/opt/mrrc-build}"
MRRC_HOME="${MRRC_HOME:-/opt/mrrc_modern}"
MRRC_USER="${MRRC_USER:-mrrc}"
MRRC_WEB_HOST="${MRRC_WEB_HOST:-0.0.0.0}"
MRRC_BOOT_DIR="${MRRC_BOOT_DIR:-/boot}"
MRRC_EXTRA_PACKAGES="${MRRC_EXTRA_PACKAGES:-}"
REPO_SRC="${REPO_SRC:-/src}"
VENV="$MRRC_HOME/venv"
ENV_DIR="$MRRC_HOME/env"
ENV_FILE="$ENV_DIR/mrrc.env"

log() { printf '\n=== %s ===\n' "$*"; }

log "preconditions"
id -u "$MRRC_USER" >/dev/null 2>&1 || {
 echo "mrrc-service-layer: no '$MRRC_USER' account. The platform shell creates" \
      "it (adduser --system on the box, pi-gen's FIRST_USER_NAME on the Pi)." >&2
 exit 1
}
# The tree is staged by the caller (stage-tree.sh); this is the check that turns
# "the tree was not staged" into a build-time error instead of a boot-time
# ModuleNotFoundError.
[ -f "$MRRC_HOME/requirements.txt" ] || {
 echo "mrrc-service-layer: no code tree at $MRRC_HOME — the caller must stage it" \
      "with packaging/common/stage-tree.sh first" >&2
 exit 1
}
[ -f "$MRRC_ASSETS/packages.txt" ] || {
 echo "mrrc-service-layer: assets missing at $MRRC_ASSETS (packages.txt)" >&2
 exit 1
}

log "system packages"
apt-get update -qq
# shellcheck disable=SC2086  # word splitting is the point: a space-separated list
APT_PACKAGES="$(grep -vE '^\s*(#|$)' "$MRRC_ASSETS/packages.txt" | tr '\n' ' ') $MRRC_EXTRA_PACKAGES"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends $APT_PACKAGES

log "python environment (install.sh owns the dependency list)"
cd "$MRRC_HOME"
./install.sh --yes --install-service --no-scope || {
 echo "mrrc-service-layer: install.sh failed" >&2
 exit 1
}
# install.sh writes an env file next to the source tree; the unit reads
# $ENV_FILE instead, so move it there rather than maintaining two env files.
if [ -f "$MRRC_HOME/.env" ] && [ ! -f "$ENV_FILE" ]; then
 mkdir -p "$ENV_DIR"
 mv "$MRRC_HOME/.env" "$ENV_FILE"
fi
# install.sh's template is written on the *build* host: it carries that machine's
# detected serial port and a password it generated there. On a device both are
# first-boot facts (design D-3; the web password is generated on the device and
# read back with mrrc-show-password) — strip them so first_run does its job
# instead of trusting the build machine.
sed -i -e '/^#\?MRRC_SERIAL_PORT=/d' -e '/^#\?MRRC_WEB_PASSWORD=/d' "$ENV_FILE"
chown -R "$MRRC_USER:$MRRC_USER" "$MRRC_HOME"
chmod 640 "$ENV_FILE" 2>/dev/null || true

log "FTDI scope libraries (aarch64, vendored in the repo)"
install -d "$MRRC_HOME/vendor/ftdi"
install -m 0755 "$MRRC_HOME/vendor/ftdi/libft4222.so" "$MRRC_HOME/vendor/ftdi/libft4222.so"
# The second name is the same ELF: FTDI's Linux libft4222 links D2XX in
# statically and re-exports its symbols, so both of find_ftdi_libraries()'s slots
# can be this one file. See packaging/box/vendor-ftdi.md.
ln -sf libft4222.so "$MRRC_HOME/vendor/ftdi/libftd2xx.so"

log "udev rule: the FT4222 scope interface belongs to the service user"
# libft4222 opens the USB device directly — the FT4222 has no /dev node of its
# own — and it does so as `mrrc`. Without this rule the node is root:root 0664,
# every scope open fails with FTDI error 2 ("device not opened") and the real FFT
# never starts, while CAT and audio keep working: the most confusing half-failure
# this feature can have (found on the first real box, 2026-10-11 — root ran the
# scope pipe fine, mrrc could not). dialout is the group both images already put
# mrrc in for ttyUSB*.
cat > /etc/udev/rules.d/99-mrrc-ftdi.rules <<'RULE'
SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="601c", MODE="0660", GROUP="dialout"
RULE

log "frpc (Cloud Hub tunnel client)"
install -d "$MRRC_HOME/fleet"
bash "$MRRC_ASSETS/fetch-frpc.sh" "$MRRC_HOME/fleet"

log "mrrc-radio, the updater, the password helper and the login banner"
install -m 0755 "$MRRC_HOME/linux/mrrc_update.sh" /usr/local/bin/mrrc-update
# mrrc-radio is a wrapper, not a copy of the module. linux/mrrc_radio.py has no
# shebang (it is a module the tests import, and the interpreter that has pyserial
# and the backend package is the venv, not the system python3), so installing the
# .py itself as /usr/local/bin/mrrc-radio handed a Python file to /bin/sh —
# "Syntax error: ( unexpected" on every invocation. A copy would also go stale
# the moment mrrc-update replaced the tree, since it does not re-install
# /usr/local/bin. The tree stays the single source.
cat >/usr/local/bin/mrrc-radio <<'WRAPPER'
#!/bin/sh
exec /opt/mrrc_modern/venv/bin/python /opt/mrrc_modern/linux/mrrc_radio.py "$@"
WRAPPER
chmod 0755 /usr/local/bin/mrrc-radio
install -m 0755 "$MRRC_ASSETS/mrrc-show-password" /usr/local/bin/mrrc-show-password
install -d /etc/update-motd.d
install -m 0755 "$MRRC_ASSETS/motd-10-mrrc" /etc/update-motd.d/10-mrrc

log "systemd units"
cat >/etc/systemd/system/mrrc-modern.service <<UNIT
[Unit]
Description=MRRC Modern web control server
After=network-online.target
Wants=network-online.target

[Service]
User=$MRRC_USER
Group=$MRRC_USER
WorkingDirectory=$MRRC_HOME
EnvironmentFile=$ENV_FILE
# Same file, so Cloud Hub's connect() writes where the service will read
# (design D-11): it persists the certificate paths and the TX heartbeat, and a
# split between these two lines is silent — the UI reports 已连接 while upstream
# TLS verification fails.
Environment=MRRC_CONFIG_FILE=$ENV_FILE
ExecStart=$VENV/bin/python server.py
Restart=on-failure
RestartSec=3
# The default KillMode=control-group also stops the frpc child. That matters on
# Linux: frpc's stale-process sweep in cloud_hub.py is Windows-only, and a
# leftover frpc holds the proxy name so the next start cannot register it.
StandardOutput=append:$MRRC_HOME/logs/server-stdout.log
StandardError=append:$MRRC_HOME/logs/server-stdout.log

[Install]
WantedBy=multi-user.target
UNIT

cat >/etc/systemd/system/mrrc-firstboot.service <<UNIT
[Unit]
Description=MRRC Modern first-boot auto-configuration
After=network-online.target
Wants=network-online.target
ConditionPathExists=!/var/lib/mrrc/firstboot-done

[Service]
Type=oneshot
# The two platform facts firstboot_wrapper.py cannot guess: where a preseed
# mrrc.env would be, and which address the server binds.
Environment=MRRC_BOOT_DIR=$MRRC_BOOT_DIR
Environment=MRRC_WEB_HOST=$MRRC_WEB_HOST
ExecStart=$VENV/bin/python $MRRC_HOME/linux/firstboot_wrapper.py
StandardOutput=journal+console
StandardError=journal
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

mkdir -p "$MRRC_HOME/linux" "$MRRC_HOME/logs" /var/lib/mrrc/certs
install -m 0755 "$MRRC_ASSETS/firstboot_wrapper.py" \
 "$MRRC_HOME/linux/firstboot_wrapper.py"
chown -R "$MRRC_USER:$MRRC_USER" /var/lib/mrrc "$MRRC_HOME/logs"

log "version stamp"
# version.txt is what the support bundle manifest and the upgrade channel read out
# of an installed tree, so an image without it fails those two things while still
# looking complete.
version="${MRRC_VERSION:-}"
if [ -z "$version" ] && [ -f "$REPO_SRC/CHANGELOG.md" ]; then
 version="$(grep -m1 -oE '## \[v[0-9]+\.[0-9]+\.[0-9]+\]' "$REPO_SRC/CHANGELOG.md" |
  grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+')"
fi
: "${version:?Could not determine a version (set MRRC_VERSION or ship CHANGELOG.md)}"
printf '%s\n' "${version#v}" >"$MRRC_HOME/version.txt"
cp "$MRRC_HOME/version.txt" "$MRRC_HOME/VERSION"
chown "$MRRC_USER:$MRRC_USER" "$MRRC_HOME/version.txt" "$MRRC_HOME/VERSION"
echo "version: ${version#v}"

log "headless defaults (only keys that are not already set)"
touch "$ENV_FILE"
grep -q '^MRRC_WEB_HOST=' "$ENV_FILE" || echo "MRRC_WEB_HOST=$MRRC_WEB_HOST" >>"$ENV_FILE"
grep -q '^MRRC_WEB_PORT=' "$ENV_FILE" || echo 'MRRC_WEB_PORT=8888' >>"$ENV_FILE"
# Second PTT defence (design D-4): connect() writes the 5 s heartbeat gate; the
# duration ceiling is off by default and has to be asked for.
grep -q '^MRRC_PTT_MAX_TX_SECONDS=' "$ENV_FILE" || echo 'MRRC_PTT_MAX_TX_SECONDS=120' >>"$ENV_FILE"
# The radio's USB-audio capture needs a deeper host buffer than the desktops:
# ALSA hands a blocking stream ~2 periods of 20 ms, and an overflow stops the
# capture interface silently (measured on the first real box, 2026-10-11). The
# code default is the same value on Linux; writing it here makes the knob visible
# in the env file an operator actually edits.
grep -q '^MRRC_RX_BUFFER_MS=' "$ENV_FILE" || echo 'MRRC_RX_BUFFER_MS=100' >>"$ENV_FILE"
grep -q '^MRRC_SSL_CERT=' "$ENV_FILE" || echo 'MRRC_SSL_CERT=/var/lib/mrrc/certs/server.crt' >>"$ENV_FILE"
grep -q '^MRRC_SSL_KEY=' "$ENV_FILE" || echo 'MRRC_SSL_KEY=/var/lib/mrrc/certs/server.key' >>"$ENV_FILE"
chown "$MRRC_USER:$MRRC_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

log "enable services"
systemctl enable mrrc-firstboot.service mrrc-modern.service

log "import gate: the entry points the units run must import from THIS tree"
# The failure modes this catches both shipped once (2026-10-10): server.py
# imported macos.first_run, which the exclusion list drops on purpose, and the
# installed mrrc-radio resolved its tree from __file__. Both die before any log
# line and neither is visible to a syntax check.
(
 cd "$MRRC_HOME"
 PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" -c "import server; import sys; sys.path.insert(0, '$MRRC_HOME'); sys.path.insert(0, '$MRRC_HOME/linux'); import net_wifi, box_ops; import first_run, mrrc_radio"
) || {
 echo "mrrc-service-layer: an image entry point cannot import from this tree" >&2
 exit 1
}
echo "entry points import OK"

log "reclaim space"
apt-get clean
rm -rf /var/lib/apt/lists/* "$VENV/.cache" /root/.cache
# install.sh leaves validation scratch in /tmp and the assets are a build-only
# copy: neither may ship. (On the Pi /tmp is a tmpfs during the build anyway, but
# /tmp is a real directory on the box, and MRRC_ASSETS is real on both.)
rm -rf /tmp/* /tmp/.[!.]* 2>/dev/null || true
rm -rf "$MRRC_ASSETS"
find "$MRRC_HOME" -name '__pycache__' -type d -prune -exec rm -rf {} +

log "overlay done"
du -sh "$MRRC_HOME"
```

- [ ] **步骤 2：设可执行位并跑卫生测试**

```bash
chmod 0755 packaging/common/mrrc-service-layer.sh
.venv/bin/python -m unittest tests.test_packaging_parity.ShellHygieneTests -v
```

预期：`test_shell_scripts_are_executable`、`test_shared_scripts_are_bash_32_compatible`、`test_the_service_layer_still_reclaims_its_scratch`、`test_no_comment_inside_a_continued_argument_list` 全绿（清单已搬家，三个旧载体将在任务 6/7/9 里失去字面量）。

- [ ] **步骤 3：Commit**

```bash
git add packaging/common/mrrc-service-layer.sh
git commit -m "feat(packaging): 服务层单一权威 mrrc-service-layer.sh（两条镜像线共用）" \
  -m "逐条保留 box-overlay 的现役语义：D-11 两行、日志落盘、PTT 上限、RX 缓冲、FTDI+udev 规则、导入门禁、回收。平台差异只剩 MRRC_WEB_HOST 与 MRRC_BOOT_DIR 两个参数。"
```

---

## 任务 6：`packaging/box/box-overlay.sh` 瘦身为平台壳

**文件：**

- 修改：`packaging/box/box-overlay.sh`（整体重写）

- [ ] **步骤 1：重写为薄壳**

用下面内容整体替换 `packaging/box/box-overlay.sh`：

```bash
#!/bin/bash -e
# W103D (ZTE, Amlogic S905L3A, Armbian) platform shell.
#
# It creates the conditions the shared service layer assumes and then hands over:
# the account, the board's WiFi firmware, the two distro masks, and the assets.
# Everything else — the code tree, the units, the CLIs, the env defaults, the
# import gate, the reclaim — is packaging/common/mrrc-service-layer.sh, because
# the Raspberry Pi image installs the exact same server and two implementations
# is how they drift (see the design doc, 2026-10-11).
set -euo pipefail

REPO_SRC="${REPO_SRC:-/src}"
MRRC_HOME=/opt/mrrc_modern
MRRC_USER=mrrc
MRRC_ASSETS=/opt/mrrc-build

log() { printf '\n=== %s ===\n' "$*"; }

log "user and groups"
if ! id -u "$MRRC_USER" >/dev/null 2>&1; then
 adduser --system --group --home "$MRRC_HOME" --shell /bin/bash "$MRRC_USER"
fi
# dialout: /dev/ttyUSB* and /dev/ttyACM*; audio: the radio's USB sound card.
for g in dialout audio; do
 getent group "$g" >/dev/null 2>&1 || groupadd --system "$g"
 adduser "$MRRC_USER" "$g" >/dev/null
done

log "stage the code tree (the one exclusion list)"
mkdir -p "$MRRC_HOME" "$MRRC_ASSETS"
bash "$REPO_SRC/packaging/common/stage-tree.sh" "$REPO_SRC" "$MRRC_HOME"
# The service layer's own assets travel separately, because the exclusion list
# drops packaging/ from the installed tree — and they go to the same path the Pi
# builder uses, so the layer has one asset path to know about.
cp -a "$REPO_SRC/packaging/common/." "$MRRC_ASSETS/"

log "MT7663S WiFi N9 firmware (vendored factory build)"
# The base image's linux-firmware N9 is the 2020 build, which associates and then
# progressively dies on this board (scan wedges, auth timeouts, MCU mailbox -110
# after ~15 min). The factory 2021 build (7663mp1827) is the one the kernel port
# was validated against. See packaging/box/vendor-mt7663.md.
install -m 0644 "$REPO_SRC/vendor/mediatek/mt7663_n9_v3.bin" \
 /lib/firmware/mediatek/mt7663_n9_v3.bin
echo "223f73f17f0f986dc4e7167daa6eef14ffb41c713f22d70f9645eb049bdec80a  /lib/firmware/mediatek/mt7663_n9_v3.bin" | sha256sum -c
# A linux-firmware package upgrade would put the broken build back; the diversion
# sends package writes to .distrib instead of over our file.
dpkg-divert --add --no-rename /lib/firmware/mediatek/mt7663_n9_v3.bin

log "distro masks"
# Debian's own dnsmasq.service is enabled by the package and collides with
# systemd-resolved for port 53 — a failed unit on every fresh box, and harmless
# only by luck. Nothing here runs a standalone dnsmasq any more (the hotspot's
# shared mode is gone), but the daemon package can still be pulled in by another
# dependency, so the mask stays as the second line of defence.
systemctl mask dnsmasq.service || true
# Amlogic vendor images ship a getty on a vendor-only FIQ console; the mainline
# kernel does not have it and the unit would wait 90 s per boot.
systemctl mask serial-getty@ttyFIQ0.service || true

log "hand over to the shared service layer"
exec bash "$MRRC_ASSETS/mrrc-service-layer.sh"
```

- [ ] **步骤 2：确认没漏掉现役语义**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
for k in MRRC_PTT_MAX_TX_SECONDS MRRC_RX_BUFFER_MS MRRC_CONFIG_FILE server-stdout.log \
         mrrc-radio mrrc-update mrrc-show-password 99-mrrc-ftdi.rules \
         firstboot_wrapper.py version.txt; do
  printf '%-28s box=%s common=%s\n' "$k" \
    "$(grep -c "$k" packaging/box/box-overlay.sh)" \
    "$(grep -c "$k" packaging/common/mrrc-service-layer.sh)"
done
```

预期：每行的 `common` 列 ≥1；`box` 列除了 `MRRC_ASSETS`/`mrrc-service-layer.sh` 相关项外为 0（薄壳里不该再有这些细节）。

- [ ] **步骤 3：跑测试**

```bash
.venv/bin/python -m unittest tests.test_packaging_parity tests.test_setup_retired -v 2>&1 | tail -20
```

预期：`CallSiteTests` 全绿；`ExclusionListTests` 仍红（Pi 与 updater 还没改）；`test_setup_retired` 绿（它要求 `packaging/box/box-overlay.sh` 里不出现 `mrrc-setup-ap`，薄壳里没有）。

- [ ] **步骤 4：Commit**

```bash
git add packaging/box/box-overlay.sh
git commit -m "refactor(box): box-overlay 退化为平台壳，服务层交给 common" \
  -m "顺带修掉 21:26 那次提交引入的运行期致命写法：注释行夹在 rsync 续行里，bash 会把后续 --exclude 当命令（exit 127）。"
```

---

## 任务 7：`packaging/rpi/build-image.sh`（镜像源 + 落树 + 素材 + build.env）

**文件：**

- 修改：`packaging/rpi/build-image.sh`（整体重写）

- [ ] **步骤 1：重写**

用下面内容整体替换 `packaging/rpi/build-image.sh`：

```bash
#!/usr/bin/env bash
# Build the MRRC Modern Raspberry Pi image (rpi64) via pi-gen in Docker.
# Prereqs: Docker Desktop running (Apple Silicon = native aarch64), >=20GB free.
# Usage: packaging/rpi/build-image.sh          (version from CHANGELOG top entry)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
PIGEN_REF="2026-06-18-raspios-bookworm-arm64"
WORK="${MRRC_PI_WORK:-$REPO_ROOT/build/pi-gen}"
OUT="${MRRC_PI_OUT:-$REPO_ROOT/dist/rpi}"

# Where the build's apt fetches from. pi-gen has no mirror switch at all: stage0
# debootstraps from deb.debian.org, which measured 65 KB/s from a container on
# this network while domestic mirrors managed 700-830 KB/s for the same files.
# Four upstream points are rewritten in the clone, all of them ordinary files:
# the debootstrap URL, the Debian sources, the Raspberry Pi archive, and the
# container image's own apt. Set MRRC_PI_APT_MIRROR= (empty) to use upstream.
APT_MIRROR="${MRRC_PI_APT_MIRROR-https://mirrors.tuna.tsinghua.edu.cn}"
# PyPI measured 37 KB/s here against 2.9 MB/s domestically; the dependency
# install ships inside the image, so the device never runs pip.
PIP_INDEX="${MRRC_PI_PIP_INDEX-https://pypi.tuna.tsinghua.edu.cn/simple}"
# github.com served 33 KB/s and truncated mid-transfer; a list, because these
# proxies go down — the empty entry at the end means GitHub itself is the last
# resort. fetch-frpc.sh verifies frp's own checksum per source, so a mirror can
# accelerate the download but cannot change what gets installed.
FRP_PROXY="${MRRC_PI_FRP_PROXY-https://gh-proxy.com/,https://ghfast.top/,}"

VERSION="$(grep -m1 -oE '## \[v[0-9]+\.[0-9]+\.[0-9]+\]' "$REPO_ROOT/CHANGELOG.md" | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+')"
: "${VERSION:?Could not read version from CHANGELOG.md}"
echo "==> Building MRRC Modern ${VERSION} rpi64 image"

# ── preflight ──
docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon not running (open -a Docker)"; exit 1; }
mkdir -p "$(dirname "$WORK")"
FREE_GB=$(df -Pk "$(dirname "$WORK")" | awk 'NR==2 {print int($4/1048576)}')
(( FREE_GB >= 20 )) || { echo "ERROR: need >=20GB free on $(dirname "$WORK"), have ${FREE_GB}GB"; exit 1; }

# ── pi-gen clone (pinned; skip when the right ref is already in place —
#    allows pre-seeding the clone on hosts where GitHub is unreachable) ──
if [ -d "$WORK/.git" ] && \
   [ "$(git -c safe.directory="$WORK" -C "$WORK" describe --tags 2>/dev/null)" = "$PIGEN_REF" ]; then
    echo "==> pi-gen $PIGEN_REF already present, skipping clone"
else
    rm -rf "$WORK"
    git clone --depth 1 --branch "$PIGEN_REF" https://github.com/RPi-Distro/pi-gen.git "$WORK"
fi

# ── mirror rewrites (idempotent) ──
# A substring swap per file rather than a sources.list generator: the pinned ref
# is what makes these paths and strings stable, and the patch says so when it
# finds the file already pointing somewhere else.
patch_text() {  # patch_text <file> <from> <to>
    python3 - "$1" "$2" "$3" <<'PY'
import pathlib, sys
path, src, dst = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text(encoding="utf-8")
if src not in text:
    print(f"    {path}: already on {dst}" if dst in text else f"    {path}: {src!r} not found")
else:
    path.write_text(text.replace(src, dst), encoding="utf-8")
    print(f"    {path}: {src} -> {dst}")
PY
}

if [ -n "$APT_MIRROR" ]; then
    MIRROR_HOST="${APT_MIRROR#https://}"; MIRROR_HOST="${MIRROR_HOST#http://}"; MIRROR_HOST="${MIRROR_HOST%/}"
    echo "==> apt mirror: $MIRROR_HOST"
    patch_text "$WORK/stage0/prerun.sh" "http://deb.debian.org/debian/" "http://$MIRROR_HOST/debian/"
    patch_text "$WORK/stage0/00-configure-apt/files/sources.list" "deb.debian.org" "$MIRROR_HOST"
    patch_text "$WORK/stage0/00-configure-apt/files/raspi.list" "archive.raspberrypi.com/debian" "$MIRROR_HOST/raspberrypi"
    # The container image's own apt (Dockerfile) is the fourth upstream point and
    # the first one to run: without it the very first build sits in
    # `apt-get install git parted …` at 65 KB/s. Cached as a layer afterwards.
    patch_text "$WORK/Dockerfile" "RUN apt-get -y update" \
        "RUN sed -i \"s|deb.debian.org|$MIRROR_HOST|g\" /etc/apt/sources.list && apt-get -y update"
fi

# ── stage: copy ours in, stage the code tree the ONE way ──
rsync -a --delete "$REPO_ROOT/packaging/rpi/pi-gen-stage4/" "$WORK/rpi-stage4/"
STAGE_FILES="$WORK/rpi-stage4/01-deploy-mrrc/files"
mkdir -p "$STAGE_FILES/opt/mrrc_modern" "$STAGE_FILES/opt/mrrc-build"
bash "$REPO_ROOT/packaging/common/stage-tree.sh" "$REPO_ROOT" "$STAGE_FILES/opt/mrrc_modern"
# The service layer's own assets, at the path it knows (packaging/ is dropped
# from the installed tree by the exclusion list, so this is a second copy on
# purpose — reclaimed at the end of the overlay).
rsync -a "$REPO_ROOT/packaging/common/" "$STAGE_FILES/opt/mrrc-build/"

# pi-gen's chroot does not inherit this shell's environment, so every platform
# parameter travels in a file the stage script sources. __VERSION__ style
# markers are not used: this is generated, not templated.
cat > "$STAGE_FILES/opt/mrrc-build/build.env" <<EOF
# Generated by packaging/rpi/build-image.sh — build-time only, reclaimed by the
# service layer, never part of the image.
MRRC_VERSION='${VERSION}'
MRRC_WEB_HOST='::'
MRRC_BOOT_DIR='/boot/firmware'
PIP_INDEX_URL='${PIP_INDEX}'
MRRC_FRP_PROXY='${FRP_PROXY}'
EOF

# version.txt is the cross-platform name (support bundle manifest + upgrade
# channel read it); VERSION is for operators upgrading in place.
printf '%s\n' "${VERSION#v}" > "$STAGE_FILES/opt/mrrc_modern/version.txt"
cp "$STAGE_FILES/opt/mrrc_modern/version.txt" "$STAGE_FILES/opt/mrrc_modern/VERSION"

# ── export markers ──
touch "$WORK/stage2/SKIP_IMAGES"
touch "$WORK/rpi-stage4/EXPORT_IMAGE"

# ── pi-gen config ──
cat > "$WORK/config" <<EOF
IMG_NAME="MRRC-Modern-${VERSION}-rpi64"
RELEASE=bookworm
TARGET_ARCH=64
TARGET_HOSTNAME=MRRC-Modern
FIRST_USER_NAME=mrrc
FIRST_USER_PASS=mrrc
ENABLE_SSH=1
DEPLOY_COMPRESSION=none
STAGE_LIST="stage0 stage1 stage2 rpi-stage4"
EOF

cd "$WORK"
export PIGEN_DOCKER_OPTS="-v $WORK:/pi-gen"
# PRESERVE_CONTAINER=1 keeps the pigen_work container on failure; resume with
# CONTINUE=1 IGNORE_FILE_CHANGES=1 ./build-docker.sh
IGNORE_FILE_CHANGES=1 PRESERVE_CONTAINER=1 ./build-docker.sh

# ── artifact ──
mkdir -p "$OUT"
IMG=$(ls "$WORK/deploy/"*.img | head -1)
XZ_OUT="$OUT/MRRC-Modern-${VERSION}-rpi64.img.xz"
xz -T0 -9 -c "$IMG" > "$XZ_OUT"
rm -f "$IMG"
echo "==> Done: $XZ_OUT"
if command -v sha256sum >/dev/null 2>&1; then sha256sum "$XZ_OUT"; else shasum -a 256 "$XZ_OUT"; fi
```

- [ ] **步骤 2：`TARGET_HOSTNAME` 生效点确认**

```bash
grep -n "TARGET_HOSTNAME" build/pi-gen/build.sh | head -3
```

预期：`build.sh:203:export TARGET_HOSTNAME=${TARGET_HOSTNAME:-raspberrypi}` —— 说明 config 里的 `TARGET_HOSTNAME=MRRC-Modern` 会被 `config` 覆盖（pi-gen 先 source `config`）。

- [ ] **步骤 3：干跑镜像源改写（不跑构建）**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
MRRC_PI_APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn \
  bash -c 'source <(sed -n "/^patch_text()/,/^}/p" packaging/rpi/build-image.sh); 
           patch_text build/pi-gen/stage0/prerun.sh "http://deb.debian.org/debian/" "http://mirrors.tuna.tsinghua.edu.cn/debian/"'
grep -n "bootstrap" build/pi-gen/stage0/prerun.sh
```

预期：`bootstrap ${RELEASE} "${ROOTFS_DIR}" http://mirrors.tuna.tsinghua.edu.cn/debian/`；再跑一次同样的命令应打印 `already on …`（幂等）。

- [ ] **步骤 4：跑测试**

```bash
.venv/bin/python -m unittest tests.test_packaging_parity -v 2>&1 | tail -14
```

预期：`ExclusionListTests`、`CallSiteTests`、`PiStageTests.test_the_pi_package_list_holds_only_platform_extras`（未改，仍红）之外全部绿；`test_no_comment_inside_a_continued_argument_list` 只余 `linux/mrrc_update.sh` 一处。

- [ ] **步骤 5：Commit**

```bash
git add packaging/rpi/build-image.sh
git commit -m "feat(rpi): 构建期镜像源开关（apt×4/pip/frp）+ 落树走 stage-tree + 主机名对齐" \
  -m "顺带修掉同一个运行期致命写法；主机名 MRRC-Modern，与 box_ops.DEFAULT_HOSTNAME 一致。"
```

---

## 任务 8：Pi stage4 瘦身

**文件：**

- 修改：`packaging/rpi/pi-gen-stage4/00-install-packages/00-packages`
- 修改：`packaging/rpi/pi-gen-stage4/prerun.sh`
- 修改：`packaging/rpi/pi-gen-stage4/01-deploy-mrrc/00-run-chroot.sh`
- 删除：`packaging/rpi/pi-gen-stage4/01-deploy-mrrc/files/`（整树）

- [ ] **步骤 1：`00-packages` 只留平台额外项**

覆盖 `packaging/rpi/pi-gen-stage4/00-install-packages/00-packages`：

```text
# Platform extras only. Everything the server itself needs is in
# packaging/common/packages.txt and is installed by the service layer, because
# both image lines must agree on that list — tests/test_packaging_parity.py fails
# if a package appears in both places.
#
# e2fsprogs: pi-gen's export-image runs resize2fs/zerofree against this rootfs.
e2fsprogs
```

- [ ] **步骤 2：`prerun.sh` 加 FATAL 校验**

覆盖 `packaging/rpi/pi-gen-stage4/prerun.sh`：

```bash
#!/bin/bash -e
# pi-gen stage prerun: carry the previous stage's rootfs into this stage, then
# overlay this stage's files/ tree onto it. This pi-gen version has no automatic
# files/ copy, and cwd is the stage dir (build.sh pushd's before calling prerun).
if [ ! -d "$ROOTFS_DIR" ]; then
 copy_previous
fi

cp -a 01-deploy-mrrc/files/. "$ROOTFS_DIR/"

# Four things build-image.sh must have staged. Checked here rather than assumed:
# a missing code tree fails at boot with an ImportError, and a missing asset dir
# fails two steps later inside the service layer with a message about the wrong
# thing.
for f in opt/mrrc_modern/requirements.txt opt/mrrc_modern/version.txt \
         opt/mrrc-build/mrrc-service-layer.sh opt/mrrc-build/build.env \
         opt/mrrc-build/packages.txt; do
 [ -e "$ROOTFS_DIR/$f" ] || {
  echo "FATAL: $f missing — packaging/rpi/build-image.sh stages it" >&2
  exit 1
 }
done
echo "prerun: staged $(find "$ROOTFS_DIR/opt/mrrc_modern" -type f | wc -l) code files"
```

- [ ] **步骤 3：`00-run-chroot.sh` 变成极薄壳**

覆盖 `packaging/rpi/pi-gen-stage4/01-deploy-mrrc/00-run-chroot.sh`：

```bash
#!/bin/bash -e
# Runs INSIDE the arm64 rootfs: pi-gen feeds this file to
# `capsh --chroot=… -- -e`, which execs /bin/bash -e with the script on stdin —
# so the shebang above is a comment, $BASH_SOURCE is not a path, and `set -u` is
# unsafe (the chroot has no login environment).
#
# Everything that is not Raspberry-Pi-specific lives in the shared service layer;
# this file only creates the conditions that layer assumes.
set -eo pipefail

MRRC_HOME=/opt/mrrc_modern
ASSETS=/opt/mrrc-build

[ -x "$ASSETS/mrrc-service-layer.sh" ] || {
 echo "FATAL: the shared service layer is not staged at $ASSETS" >&2
 exit 1
}
[ -f "$ASSETS/build.env" ] || { echo "FATAL: $ASSETS/build.env is missing" >&2; exit 1; }

# Version, bind address, boot partition and mirrors: all generated on the host by
# build-image.sh, because pi-gen's chroot does not inherit the host environment.
set -a
. "$ASSETS/build.env"
set +a

# pi-gen's FIRST_USER_NAME=mrrc created the account; the service layer assumes it
# exists and never creates it. dialout: /dev/ttyUSB*/ttyACM*. audio: the radio's
# USB sound card.
usermod -aG dialout,audio mrrc
chown -R mrrc:mrrc "$MRRC_HOME"

exec bash "$ASSETS/mrrc-service-layer.sh"
```

- [ ] **步骤 4：删掉整个 `files/` 树**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
git rm -r packaging/rpi/pi-gen-stage4/01-deploy-mrrc/files
git status --short packaging/rpi
```

预期：`files/` 下的 8 个文件（两套 systemd 单元、motd、show-password、firstboot_wrapper 副本、两个 `.pyc`）全部标记删除。

- [ ] **步骤 5：跑测试**

```bash
.venv/bin/python -m unittest tests.test_packaging_parity -v 2>&1 | tail -14
```

预期：**全绿**（排除清单唯一载体、三处调用点、Pi stage 无服务层产出、包清单无交集、两处有意差异、四条卫生规则、包清单含 libopus）。

- [ ] **步骤 6：Commit**

```bash
git add -A packaging/rpi/pi-gen-stage4
git commit -m "refactor(rpi): stage4 只剩三件平台事，files/ 整树删除（含两个 .pyc）" \
  -m "单元/motd/口令助手/首启包装从此只有 common/ 一份；包清单也只剩 common/packages.txt 一处。"
```

---

## 任务 9：`linux/mrrc_update.sh` 改成驱动器

**文件：**

- 修改：`linux/mrrc_update.sh`（整体重写）

- [ ] **步骤 1：重写**

用下面内容整体替换 `linux/mrrc_update.sh`：

```bash
#!/bin/bash -e
# Update MRRC Modern in place, without reflashing the image.
#
# Deliberately the smallest thing that works: fetch the tree, install whatever
# requirements.txt now needs, restart. There is no rollback, because there is no
# verified way to decide a rollback is warranted — upgrade_core.py's automatic
# path (slice 2) is explicitly not started, and inventing one here would be a
# second, untested state machine over the same files.
#
# The file list is not here either: the fetched tree carries
# packaging/common/stage-tree.sh, the one place the exclusion list lives, and this
# script calls it. Using the *fetched* copy is also the only version that is
# right — the new release is the one that knows what it ships.
#
# One deliberate behaviour change against the pre-2026-10-11 version: nothing is
# deleted any more. That version passed --delete to rsync, and dropping a file
# from the tree also dropped it on every box; the de-duplication of the list does
# not get to smuggle that decision in with it. Removing stale files needs its own
# evaluation (design doc §4.4), so this updater only adds and overwrites for now.
set -euo pipefail

MRRC_HOME="${MRRC_HOME:-/opt/mrrc_modern}"
MRRC_USER="${MRRC_USER:-mrrc}"
SRC="${MRRC_UPDATE_SRC:-}"          # git URL, or a local directory to rsync from
BRANCH="${MRRC_UPDATE_BRANCH:-main}"
CLONE=""

cleanup() { [ -n "$CLONE" ] && rm -rf "$CLONE"; return 0; }
trap cleanup EXIT

[ -n "$SRC" ] || { echo "set MRRC_UPDATE_SRC to a git URL or a local directory" >&2; exit 1; }

if [ -d "$SRC" ]; then
 # `cd … && pwd` rather than readlink -f: this runs on the box, and the path
 # must be absolute for rsync's root-anchored excludes to mean anything.
 SRC_TREE="$(cd "$SRC" && pwd)"
 echo "==> updating from the local tree $SRC_TREE"
else
 echo "==> cloning $SRC ($BRANCH)"
 CLONE="$(mktemp -d)"
 SRC_TREE="$CLONE/tree"
 git clone --depth 1 --branch "$BRANCH" "$SRC" "$SRC_TREE"
fi

STAGER="$SRC_TREE/packaging/common/stage-tree.sh"
[ -f "$STAGER" ] || {
 echo "mrrc-update: $SRC_TREE has no packaging/common/stage-tree.sh." >&2
 echo "That tree predates the 2026-10-11 packaging change, so this updater" >&2
 echo "cannot know which files it is supposed to install. Use $BRANCH or later." >&2
 exit 1
}

bash "$STAGER" "$SRC_TREE" "$MRRC_HOME"

echo "==> python dependencies"
"$MRRC_HOME/venv/bin/pip" install --upgrade -q -r "$MRRC_HOME/requirements.txt"

chown -R "$MRRC_USER:$MRRC_USER" "$MRRC_HOME"
echo "==> restarting"
systemctl restart mrrc-modern
systemctl --no-pager --lines=5 status mrrc-modern || true
echo "==> done; env file left untouched at $MRRC_HOME/env/mrrc.env"
```

- [ ] **步骤 2：跑卫生测试与镜像导入测试**

```bash
.venv/bin/python -m unittest tests.test_packaging_parity tests.test_image_imports -v 2>&1 | tail -14
```

预期：`test_no_comment_inside_a_continued_argument_list` 绿（最后一处载体已消失），`ExclusionListTests` 绿。`test_image_imports` 需要任务 10 改 `BUILDER` 路径，此步可能红——记下原因。

- [ ] **步骤 3：Commit**

```bash
git add linux/mrrc_update.sh
git commit -m "refactor(upgrade): 就地升级器改用拉下来那份 stage-tree.sh（清单第三份拷贝退场）" \
  -m "同时显式记录一处行为变化：不再 --delete（删过时文件是独立决定，不随这次去重夹带）。另修 git URL 路径本来就是坏的（对 /opt/mrrc_modern 做 git fetch，而它不是仓库）。"
```

---

## 任务 10：引用与文档同步

**文件：**

- 修改：`tests/test_box_profiles.py`、`tests/test_image_imports.py`、`tests/test_mrrc_radio.py`
- 修改：`AGENTS.md`、`.agents/skills/dual-platform-release/SKILL.md`
- 修改：`docs/superpowers/specs/2026-09-09-raspberry-pi-image-design.md`

- [ ] **步骤 1：跑测试看红在哪**

```bash
.venv/bin/python -m unittest discover -s tests 2>&1 | grep -E "^(FAIL|ERROR):" | head -20
```

预期：至少 `test_box_profiles` 的 `ExclusionParityTests`（清单已不在那三个文件里）、`ProfileSetTests`（`PROFILE_DIR`）、`test_mrrc_radio`、`test_image_imports`（`BUILDER`）。

- [ ] **步骤 2：`tests/test_box_profiles.py`**

1) `PROFILE_DIR` 改路径：

```python
PROFILE_DIR = REPO / "packaging" / "common" / "profiles"
```

1) **删掉** `ExclusionParityTests` 整个类（它的职责被 `tests/test_packaging_parity.py` 以更强的形式接手：过去是"三份逐字节相同"，现在是"只有一份"）。删掉类顶部那句提到三个实现者的注释。

2) 文件顶部文档串里凡提到配置文件在 `packaging/box/profiles` 的地方改成 `packaging/common/profiles`。

- [ ] **步骤 3：`tests/test_image_imports.py`**

1) `BUILDER` 指向唯一载体：

```python
#: The authoritative exclusion list. It used to be the Pi builder's own rsync
#: invocation; since 2026-10-11 it is one shared script that all three copiers
#: call (packaging/common/stage-tree.sh), so this points at the list itself.
BUILDER = REPO / "packaging" / "common" / "stage-tree.sh"
```

1) 模块文档串第 4-6 行的说明同步（"mirrored by …" 改成"three copiers call one list"）。

2) `box_firstboot()` 的注释里 `packaging/box/firstboot_wrapper.py` 改成 `packaging/common/firstboot_wrapper.py`。

- [ ] **步骤 4：`tests/test_mrrc_radio.py`**

把第 123 行附近提到的 `packaging/box/profiles` 改成 `packaging/common/profiles`。

- [ ] **步骤 5：跑测试**

```bash
.venv/bin/python -m unittest discover -s tests 2>&1 | tail -5
```

预期：回到 `Ran 1900+ tests`（新增的 parity 测试计入）、`FAILED (failures=1, skipped=1)`，唯一失败仍是 `test_release_artifacts`（任务 12）。

- [ ] **步骤 6：文档订正**

`AGENTS.md`：

- 把 `net_wifi.py / linux/setup_ap.py` 那一行改写成现状：热点已于 2026-10-11 真机验证失败并退役（`linux/setup_ap.py` 已删除），引导改走 LAN 设置页；`net_wifi` 只剩 STA 半边。
- 在 `server.py` 行或文件结构段落里补一句打包结构：`packaging/common/` 是两条镜像线共用的服务层与排除清单唯一出处，`packaging/box/`、`packaging/rpi/` 只是平台壳。

`.agents/skills/dual-platform-release/SKILL.md`：把其中 `packaging/box/box-overlay.sh` / `packaging/rpi/...` 的路径引用与"服务层在哪"的描述同步到新结构；不新增流程。

`docs/superpowers/specs/2026-09-09-raspberry-pi-image-design.md`：在 §4（FTDI 手动放库）与 §8（YAGNI：不做自更新、不做 FTDI 内置）各加一行"**已被 `2026-10-11-rpi-w103d-parity-packaging-design.md` 修订**"，并写明改成什么。

- [ ] **步骤 7：Commit**

```bash
git add tests/test_box_profiles.py tests/test_image_imports.py tests/test_mrrc_radio.py \
        AGENTS.md .agents/skills/dual-platform-release/SKILL.md \
        docs/superpowers/specs/2026-09-09-raspberry-pi-image-design.md
git commit -m "docs+test: 打包结构变动后的引用同步（路径、退役描述、被修订的旧设计）"
```

---

## 任务 11：`packaging/rpi/verify-image.sh` 升级为四步验证

**文件：**

- 修改：`packaging/rpi/verify-image.sh`（整体重写）

- [ ] **步骤 1：重写**

用下面内容整体替换 `packaging/rpi/verify-image.sh`：

```bash
#!/usr/bin/env bash
# Four-step verification of a built rpi64 image, against the REAL artifact.
#
# Same recipe as docs/w103d_pack.md: the .img is an MBR disk with p1 = boot
# FAT32 and p2 = root ext4, macOS cannot loop-mount ext4, and the image is arm64
# like this host, so a privileged arm64 container can mount it and even chroot
# into it natively. util-linux and coreutils are all this needs — no apt, which
# matters because apt is the slowest thing on this network.
#
#   ① code identity  — the image's tree is byte-identical to this checkout
#   ② installed      — profiles, CLIs, frpc, FTDI libs, account, no leftovers
#   ③ D-11 wiring    — EnvironmentFile and MRRC_CONFIG_FILE are the same file
#   ④ it runs        — chroot the artifact, import every dependency + libopus
#
# usage: packaging/rpi/verify-image.sh dist/rpi/MRRC-Modern-v1.25.5-rpi64.img.xz
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
IMG_XZ="${1:?usage: verify-image.sh <img.xz>}"
[ -f "$IMG_XZ" ] || { echo "no such image: $IMG_XZ" >&2; exit 1; }
IMG_XZ="$(cd "$(dirname "$IMG_XZ")" && pwd)/$(basename "$IMG_XZ")"
OUT_DIR="$(dirname "$IMG_XZ")"

echo "==> xz integrity"
xz -t "$IMG_XZ"
BYTES=$(stat -f %z "$IMG_XZ" 2>/dev/null || stat -c %s "$IMG_XZ")
(( BYTES > 200000000 && BYTES < 2000000000 )) || {
 echo "ERROR: image size ${BYTES} outside 0.2-2.0GB" >&2; exit 1; }
echo "    ${BYTES} bytes"

docker run --rm --privileged --platform linux/arm64 \
 -v "$OUT_DIR":/out:ro -v "$REPO":/src:ro \
 -v "$REPO/packaging/common":/common:ro \
 debian:bookworm bash -euo pipefail -c '
  img=$(ls /out/*-rpi64.img.xz | head -1)
  gunzip -c "$img" > /tmp/i.img
  loop=$(losetup --show -fP /tmp/i.img)
  . /common/part-nodes.sh
  part_nodes "$loop"
  mkdir -p /mnt
  mount -o ro "${loop}p2" /mnt

  bad=0
  ok()   { echo "  ✓ $*"; }
  fail() { echo "  ✗ $*"; bad=1; }

  echo "==> ① code identity (image vs this checkout)"
  for f in server.py requirements.txt static/ft710_ui.js scope_handler.py; do
   a=$(sha256sum "/mnt/opt/mrrc_modern/$f" | cut -d" " -f1)
   b=$(sha256sum "/src/$f" | cut -d" " -f1)
   [ "$a" = "$b" ] && ok "$f identical" || fail "$f differs ($a vs $b)"
  done

  echo "==> ② what got installed"
  [ "$(find /mnt/opt/mrrc_modern/profiles -name "*.env" | wc -l)" = 11 ] \
   && ok "11 radio profiles" || fail "profiles: $(ls /mnt/opt/mrrc_modern/profiles 2>&1 | head -1)"
  for f in mrrc-update mrrc-radio mrrc-show-password; do
   [ -e "/mnt/usr/local/bin/$f" ] && ok "/usr/local/bin/$f" || fail "/usr/local/bin/$f missing"
  done
  [ -x /mnt/etc/update-motd.d/10-mrrc ] && ok "login banner" || fail "10-mrrc missing"
  file /mnt/opt/mrrc_modern/fleet/frpc | grep -q aarch64 \
   && ok "frpc is an aarch64 ELF" || fail "frpc: $(file /mnt/opt/mrrc_modern/fleet/frpc 2>&1 | head -1)"
  [ -f /mnt/opt/mrrc_modern/vendor/ftdi/libft4222.so ] \
   && ok "FTDI libft4222.so" || fail "FTDI library missing"
  [ -L /mnt/opt/mrrc_modern/vendor/ftdi/libftd2xx.so ] \
   && ok "libftd2xx.so symlink" || fail "libftd2xx.so is not a symlink"
  [ -f /mnt/etc/udev/rules.d/99-mrrc-ftdi.rules ] \
   && ok "FT4222 udev rule" || fail "99-mrrc-ftdi.rules missing"
  grep -q "^mrrc:" /mnt/etc/passwd && ok "mrrc account" || fail "mrrc account missing"
  [ -f /mnt/opt/mrrc_modern/version.txt ] \
   && ok "version.txt = $(cat /mnt/opt/mrrc_modern/version.txt)" || fail "version.txt missing"
  # Build scratch must not ship: the assets directory is the one this
  # change introduced, and __pycache__ is the one it deletes.
  [ ! -e /mnt/opt/mrrc-build ] && ok "no /opt/mrrc-build" || fail "/opt/mrrc-build shipped"
  [ -z "$(find /mnt/opt/mrrc_modern -name __pycache__ -print -quit)" ] \
   && ok "no __pycache__" || fail "__pycache__ shipped"

  echo "==> ③ D-11 wiring"
  unit=/mnt/etc/systemd/system/mrrc-modern.service
  grep -q "^User=mrrc$" "$unit" && ok "User=mrrc" || fail "User= line"
  a=$(grep "^EnvironmentFile=" "$unit" | cut -d= -f2)
  b=$(grep "^Environment=MRRC_CONFIG_FILE=" "$unit" | cut -d= -f3)
  [ -n "$a" ] && [ "$a" = "$b" ] \
   && ok "EnvironmentFile == MRRC_CONFIG_FILE ($a)" \
   || fail "split config file: EnvironmentFile=$a MRRC_CONFIG_FILE=$b"
  grep -q "^MRRC_WEB_HOST=::$" /mnt/opt/mrrc_modern/env/mrrc.env \
   && ok "env file binds ::" || fail "MRRC_WEB_HOST not :: (Pi diverges here by design)"

  echo "==> ④ it runs (native arm64 chroot)"
  for d in dev proc sys; do mount --bind /$d /mnt/$d; done
  chroot /mnt /opt/mrrc_modern/venv/bin/python -c \
   "import fastapi, numpy, pyaudio, lameenc, cryptography, serial; print(\"    deps import OK\")" \
   || fail "dependency import"
  chroot /mnt /opt/mrrc_modern/venv/bin/python -c \
   "import ctypes; ctypes.CDLL(\"libopus.so.0\"); print(\"    libopus loads OK\")" \
   || fail "libopus"
  chroot /mnt /opt/mrrc_modern/venv/bin/python -c "
import sys
sys.path.insert(0, \"/opt/mrrc_modern\")
sys.path.insert(0, \"/opt/mrrc_modern/linux\")
import server, net_wifi, box_ops, first_run, mrrc_radio
print(\"    entry points import OK\")" || fail "entry point import"

  umount /mnt/dev /mnt/proc /mnt/sys || true
  umount /mnt
  losetup -d "$loop"
  [ "$bad" = 0 ] && echo "==> VERIFY_OK" || { echo "==> VERIFY_FAILED"; exit 1; }
 '
```

- [ ] **步骤 2：语法与帮助路径检查**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
bash -n packaging/rpi/verify-image.sh && echo "syntax OK"
packaging/rpi/verify-image.sh 2>&1 | head -2
```

预期：`syntax OK`；不带参数时打印 usage 并以非零退出。

- [ ] **步骤 3：Commit**

```bash
git add packaging/rpi/verify-image.sh
git commit -m "test(rpi): 产物验证升级为四步（代码身份/装机内容/D-11/arm64 原生 chroot 真跑）" \
  -m "对齐 docs/w103d_pack.md 的配方；镜像是 MBR+ext4，与盒子同构，同一套容器配方可用。"
```

---

## 任务 12：修基线那 1 红（网站 W103D 卡片）

**文件：**

- 可能修改：`website/index.html`（或 `website/zh/index.html` / `release-artifacts.json`）

- [ ] **步骤 1：先核对线上产物，再决定改哪边**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
curl -sI https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-1.25.5-w103d.img.gz | grep -iE "^(HTTP|content-length)"
stat -f %z dist/w103d/MRRC-Modern-1.25.5-w103d.img.gz
shasum -a 256 dist/w103d/MRRC-Modern-1.25.5-w103d.img.gz | cut -c1-12
grep -oE "(1,[0-9]{3},[0-9]{3}|[0-9]{9,}) bytes" website/index.html | head -3
grep -oE "SHA-256: [0-9a-f]{12}" website/index.html | head -3
```

预期与判据：**线上 `content-length` 必须等于本地 `stat` 的 1,268,146,785**。若相等 → 卡片写错了，按本地数字改卡片（步骤 2）。若不等 → **停手并报告**：说明线上那份与本地这份不是同一个文件，改卡片会把卡片指向一个不存在的产物，需要人工决定以哪份为准。

- [ ] **步骤 2：改卡片（仅当步骤 1 相符）**

把 `website/index.html` 里 W103D 卡片那一行的字节数改为 `1.18 GB (1,268,146,785 bytes)`（尺寸用两位有效数字即可，与既有风格一致），SHA-256 改成步骤 1 打印的完整值。中文站 `website/zh/index.html` 若也有该卡片同样处理。

- [ ] **步骤 3：跑 release check**

```bash
.venv/bin/python -m unittest tests.test_release_artifacts -v 2>&1 | tail -6
```

预期：PASS。若仍红，看它报的是哪一项（`[pi-guide]` 是 `·` 警告，不是失败）。

- [ ] **步骤 4：Commit**

```bash
git add website/index.html website/zh/index.html
git commit -m "site: W103D 卡片补上 1.25.5 重发后的字节数与 SHA（release_check 基线红）"
```

---

## 任务 13：`docs/rpi_pack.md`

**文件：**

- 创建：`docs/rpi_pack.md`

- [ ] **步骤 1：写构建手册**

创建 `docs/rpi_pack.md`，与 `docs/w103d_pack.md` 同构，至少覆盖：

1. **前置**：Docker Desktop 必须运行（macOS 不能 loop-mount ext4）、≥20 GB 空闲、`pi-gen` 克隆锁定 ref `2026-06-18-raspios-bookworm-arm64`。
2. **缓存位置**：`build/pi-gen`（scratch，`work/`+`deploy/` 挂在这个卷上）、`pi-gen:latest` 容器镜像。
3. **四个镜像源改写点**（表：文件 / 上游 / 默认镜像），以及三个开关 `MRRC_PI_APT_MIRROR` / `MRRC_PI_PIP_INDEX` / `MRRC_PI_FRP_PROXY`（置空即回上游）。
4. **失败续跑**：`PRESERVE_CONTAINER=1` 保留 `pigen_work` 容器，失败后 `CONTINUE=1 IGNORE_FILE_CHANGES=1 ./build-docker.sh`。
5. **产物验证**：`packaging/rpi/verify-image.sh <img.xz>` 的四步与各自判据。
6. **本次实跑记录**（任务 15/16 填）：耗时、每一步的观测、遇到的坑与结论。

- [ ] **步骤 2：Commit**

```bash
git add docs/rpi_pack.md
git commit -m "docs: 树莓派构建手册（镜像源开关、缓存、续跑、四步验证）"
```

---

## 任务 14：全量测试 + 守卫复查

**文件：** 无

- [ ] **步骤 1：跑全量**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
.venv/bin/python -m unittest discover -s tests 2>&1 | tail -5
```

预期：`Ran 1900+ tests`、`OK (skipped=1)` —— 基线那 1 红已被任务 12 修掉，且**没有新增**失败。

- [ ] **步骤 2：确认没有把别人在飞的改动卷进来**

```bash
git status --short | grep -v "^??" 
git log --oneline -12
```

预期：未提交项只剩仓库里本来就在飞的那些（`box_ops.py`、`website/`、`atr1000_tuner.json`、`mem_channels.json`、`docs/*_GUIDE.md` 等），本计划的改动全部已提交。

- [ ] **步骤 3：把"三份清单"的历史断言从测试里彻底清掉**

```bash
grep -rn "ExclusionParityTests" tests/ docs/ .agents/ 2>/dev/null | grep -v __pycache__
```

预期：只剩 `docs/website/sdd` 之类历史文档里的提及（不改），`tests/` 下为 0。

---

## 任务 15：真跑 W103D（不发布）

**文件：**

- 修改：`docs/rpi_pack.md`（记录）—— 以及必要时回头修脚本

- [ ] **步骤 1：只查前置**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
packaging/box/build-image.sh --check
```

预期：Docker、基底缓存、磁盘都通过。

- [ ] **步骤 2：真构建**

```bash
time packaging/box/build-image.sh 2>&1 | tee /tmp/mrrc-box-build.log | tail -40
```

预期：跑到底，产出 `dist/w103d/MRRC-Modern-1.25.5-w103d.img.gz`。**重点观察**：`=== stage the code tree (the one exclusion list) ===` 这一步不再报 `--exclude: command not found`；服务层的每一步都打印出来；`=== import gate ===` 打印 `entry points import OK`；最后 `rm -rf "$MRRC_ASSETS"` 之后 `/opt/mrrc-build` 不在产物里。

若卡住：按 `docs/w103d_pack.md` 的判据区分"源慢"与"代码坏"（连着但零下载 = 源慢）。

- [ ] **步骤 3：产物四步验证**

```bash
docker run --rm --privileged --platform linux/arm64 \
  -v "$PWD/dist/w103d":/out:ro -v "$PWD/packaging/box":/box:ro -v "$PWD/packaging/common":/common:ro \
  debian:bookworm bash -euo pipefail -c '
  gunzip -c /out/MRRC-Modern-*-w103d.img.gz > /tmp/i.img
  loop=$(losetup --show -fP /tmp/i.img); . /common/part-nodes.sh; part_nodes "$loop"
  mkdir -p /mnt; mount -o ro "${loop}p2" /mnt
  ls /mnt/opt/mrrc_modern/profiles | wc -l
  [ ! -e /mnt/opt/mrrc-build ] && echo "no assets dir: OK"
  grep -E "^(EnvironmentFile|Environment=MRRC_CONFIG_FILE|User)=" /mnt/etc/systemd/system/mrrc-modern.service
  for d in dev proc sys; do mount --bind /$d /mnt/$d; done
  chroot /mnt /opt/mrrc_modern/venv/bin/python -c "import server, net_wifi, box_ops; print(\"imports OK\")"
  chroot /mnt /opt/mrrc_modern/venv/bin/python -c "import ctypes; ctypes.CDLL(\"libopus.so.0\"); print(\"libopus OK\")"
  umount /mnt/dev /mnt/proc /mnt/sys; umount /mnt; losetup -d "$loop"'
```

预期：`11`、`no assets dir: OK`、三行单元配置里两处路径相同且 `User=mrrc`、两次 chroot 打印 OK。

- [ ] **步骤 4：把实跑结论写进 `docs/w103d_pack.md` 的"结构变动"一节**

记录：日期、耗时、观察到的日志锚点、有无偏离预期。若有偏离 → 先修脚本再继续（不要带着"已知但没修"的状态往下走）。

- [ ] **步骤 5：Commit**

```bash
git add docs/w103d_pack.md docs/rpi_pack.md
git commit -m "docs(pack): 结构统一后 W103D 真构建记录（耗时/日志锚点/验证结论）"
```

---

## 任务 16：真跑 Pi（不发布）

**文件：**

- 修改：`docs/rpi_pack.md`

- [ ] **步骤 1：真构建**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
time packaging/rpi/build-image.sh 2>&1 | tee /tmp/mrrc-pi-build.log | tail -60
```

预期：pi-gen 跑完 stage0→2 + rpi-stage4，产出 `dist/rpi/MRRC-Modern-v1.25.5-rpi64.img.xz`（约 0.6–1.2 GB）。

**重点观察（本期最想拿到的数据）**：

- `==> apt mirror: mirrors.tuna.tsinghua.edu.cn` 后四个 `patch_text` 的输出；
- stage0 debootstrap 的**耗时**（这是 65 KB/s 那台主机的替代品，决定整个构建是 20 分钟还是 3 小时）；
- `prerun: staged N code files`；
- `=== system packages ===` → `=== import gate ===` 之间 install.sh 是否跑通（**这是本设计 §10 里唯一的中等风险项**）；
- `entry points import OK`。

若 `install.sh` 在 pi-gen 的 chroot 里失败：**不要**退化成"服务层自己建 venv"（那会重新引入第二份依赖流程）。先读日志定位（常见：`/dev/null` 缺失、`systemctl` 不可用、`$USER` 未设——后两个 install.sh 已有默认值），把修复做进 `install.sh`，并在 `docs/rpi_pack.md` 记录症状与判据。

- [ ] **步骤 2：产物四步验证**

```bash
packaging/rpi/verify-image.sh dist/rpi/MRRC-Modern-v1.25.5-rpi64.img.xz
```

预期：`==> VERIFY_OK`。任何一项 ✗ 都要先修（`/opt/mrrc-build` 残留、profiles 不是 11、D-11 两行不一致、chroot 导入失败）。

- [ ] **步骤 3：把实跑数据写进 `docs/rpi_pack.md`**

至少记录：总耗时与各阶段耗时、debootstrap 实际速度、四个改写点是否生效、install.sh 是否一次通过、四步验证的实际输出、以及**任何"只有真跑才会暴露"的东西**（本期已经有一个先例：注释行夹在续行里）。

- [ ] **步骤 4：Commit**

```bash
git add docs/rpi_pack.md
git commit -m "docs(rpi): 首次全量真构建记录（镜像源实测、install.sh 通过性、四步验证）"
```

---

## 任务 17：收尾

**文件：** 无（本仓库）/ `~/.pi/agent/skills/w103d-box/SKILL.md`（仓库外）

- [ ] **步骤 1：把结论回写给操作者技能**

更新 `~/.pi/agent/skills/w103d-box/SKILL.md`：`overlay-inside-image` 从 `packaging/box/box-overlay.sh` 改成 `packaging/common/mrrc-service-layer.sh`，`build-script` 补上 `packaging/common/stage-tree.sh`；在"九个坑"表里加一条第 10 条——**续行里夹注释 = `--exclude: command not found`（运行期，`bash -n` 与正则都看不见）**；"固定陷阱"表里补上"排除清单现在只有一处，改它要改 `packaging/common/stage-tree.sh`（过去那句'要同步三处'已作废）"。

- [ ] **步骤 2：报告**

给用户一份简报：本期改动清单、两侧构建的实际耗时与结论、四步验证结果、以及**下期（W103D 设置页落完后）要做的事**：服务层吸收 helper unit / polkit 规则 / Wi-Fi preseed unit → 最终构建 Pi → 更新网站卡片（FTDI 内置、`MRRC-Modern.local`、字节数/SHA）、`release-artifacts.json`、`RASPBERRY_PI_GUIDE.md`。

---

## 自检

**规格覆盖度**

| 设计章节 | 落地任务 |
| --- | --- |
| §3 目标形态（common/ 四件 + 素材搬迁 + files/ 删除） | 2、3、4、5、8 |
| §4.1 服务层入参 | 5（脚本头 + 两处参数化） |
| §4.2 落树时机三处 | 2（脚本）、6（box）、7（Pi）、9（updater） |
| §4.2.1 `MRRC_ASSETS` 与 tmpfs 陷阱 | 5（默认值）、7（staged 到 `/opt/mrrc-build`）、8（校验）、11（验证） |
| §4.3 bash 3.2 | 1（守卫）、2（脚本） |
| §4.4 updater 改驱动器 + `--delete` 记录 | 9 |
| §5 镜像源四个改写点 + 三开关 | 7、13 |
| §6.1/6.2 平台壳边界 | 6、8 |
| §6.3 有意差异白名单 + 主机名 + motd | 1（守卫）、3（motd）、5（两参数）、7（TARGET_HOSTNAME） |
| §8.1 守卫测试 7 条 | 1 |
| §8.2 四步验证 | 11、15、16 |
| §8.3 两侧真跑 | 15、16 |
| §11 基线红 + AGENTS.md 订正 | 12、10 |
| 设计外的当场缺陷（三脚本续行注释） | 1（守卫）、6、7、9（三个文件） |

**占位符扫描**：无 TODO/待定；任务 12 的"停手并报告"是一个带判据的分支，不是占位符。

**类型一致性**：`MRRC_ASSETS`（服务层读）与 `files/opt/mrrc-build`（构建器写）在任务 5/7/8/11 里是同一路径；`stage-tree.sh <src> <dst>` 的参数序在三处调用点（任务 2/6/7/9）一致；`MRRC_WEB_HOST` / `MRRC_BOOT_DIR` 的取值在任务 1 的断言、任务 5 的默认值、任务 7 的 `build.env`、任务 8 的单元里一致（`::` / `/boot/firmware`）。
