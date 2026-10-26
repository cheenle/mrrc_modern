# 树莓派镜像线对齐（一键升级 + 盒子设置控制）实现计划

> **状态（2026-10-27）**：任务 1–10 已执行并推送（`v1.28.12`，tag 已在 origin）。
> 任务 11（真机验收）待真机到位后执行。
>
> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 重出一版树莓派镜像，使 `/settings` 的一键升级与全套盒子设置控制在 Pi 上可用；修掉四条只在 Pi 平台暴露的缺陷，用离线门禁钉住，发布后再做真机验收。

**架构：** 运行时代码（`box_ops`/`net_wifi`/`box_update`/设置页）**一行不改** —— 它本来就是两条镜像线共用。改动只落在：① 一份被三处调用的版本号读法；② Pi 平台壳的两个构建参数（管制域、版本号来源）；③ 解析链修复脚本的状态判据；④ 产物验证的七条门禁；⑤ 文档与发布口径。

**技术栈：** Bash（构建壳、门禁、设备脚本）、Python 3.11（`unittest`、`box_update` 导入门禁）、pi-gen（Docker，原生 arm64）、xz。

**权威规格：** `docs/superpowers/specs/2026-10-27-rpi-box-parity-design.md`（下称 spec；§号指它）

---

## 文件结构

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `packaging/common/version_of.sh` | **新建** | 版本号的**唯一**读法：CLI，`CHANGELOG.md → X.Y.Z`，镜像线标题优先 |
| `packaging/rpi/build-image.sh` | 改 | 用上面的 helper 取版本；生成 pi-gen `config` 时写入 `WPA_COUNTRY` |
| `packaging/common/mrrc-service-layer.sh` | 改 | 兑底分支改用同一个 helper（其余不动） |
| `linux/mrrc_update.sh` | 改 | CHANGELOG 分支改用 helper，缺失时退回 `version.txt` |
| `linux/fix_resolv_conf.sh` | 改 | 新增 resolved 状态判据：不用 resolved 的机器（树莓派）退出 0 |
| `packaging/rpi/verify-image.sh` | 改 | step 2 加五条、step 4 加两条产物检查 |
| `tests/test_rpi_packaging.py` | 改 | 两条源码级断言：`WPA_COUNTRY` 非空默认、版本号走 helper |
| `tests/test_fix_resolv_conf.py` | 改 | 状态注入 + 两条新用例 |
| `tests/test_release_artifacts.py` | 改 | `version_of.sh` 与 `make_box_source.version_of()` 输出一致 |
| `docs/RASPBERRY_PI_GUIDE.md` | 改 | 用户向：下载卡数字、`MRRC-Modern.local`、`/setup`、`/settings`、**更新章节改为"立即升级"** |
| `docs/rpi_pack.md` | 改 | `MRRC_PI_WPA_COUNTRY`、版本读法、七条验证 |
| `AGENTS.md` | 改 | `packaging/` 行补一句管制域陷阱 |
| `CHANGELOG.md` | 改 | 新镜像线条目（版本号与 FP 的唯一来源） |
| `website/{index,zh/index}.html` | 改 | 下载卡：新字节数、SHA-256、`MRRC-Modern.local` |
| `.agents/skills/dual-platform-release/release-artifacts.json` | 改 | Pi 指南规则从 `optional` 转必检 |

---

### 任务 1：版本号的唯一读法

**文件：**
- 创建：`packaging/common/version_of.sh`
- 测试：`tests/test_release_artifacts.py`（文件末尾追加 `VersionOfScriptTests`）

- [x] **步骤 1：写失败的测试**

在 `tests/test_release_artifacts.py` 末尾追加：

```python
class VersionOfScriptTests(unittest.TestCase):
    """`packaging/common/version_of.sh` 是版本号的唯一读法（spec 2026-10-27 §4.0）。

    它存在的理由是一次实测错误：镜像线标题写 `## [镜像线 v1.28.11 · FP7]`，而
    `build-image.sh` 的正则只认 `## [vX.Y.Z]`，于是当前 CHANGELOG 下它返回
    **1.28.6** —— 桌面线的版本。一个今天构建的镜像会把自已报成落后五个修复包。
    """

    def _run(self, changelog_text):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "CHANGELOG.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(changelog_text)
            return subprocess.run(
                ["sh", str(SCRIPT), path],
                capture_output=True, text=True)

    def test_it_reads_the_image_line_heading(self):
        proc = self._run("## [镜像线 v9.8.7 · FP4] — x\n\n## [v9.8.3] — y\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "9.8.7")

    def test_the_desktop_heading_is_only_a_fallback(self):
        proc = self._run("## [v1.30.1] — a desktop-only release\n")
        self.assertEqual(proc.stdout.strip(), "1.30.1")

    def test_no_heading_is_an_error_not_an_empty_version(self):
        proc = self._run("# Changelog\n\nnothing here\n")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout.strip(), "")

    def test_it_agrees_with_make_box_source(self):
        """Two implementations, one number — otherwise the box tarball and the
        image can label the same code differently."""
        spec = importlib.util.spec_from_file_location(
            "make_box_source", REPO / "dev_tools" / "make_box_source.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        expected, _ = module.version_of(REPO)
        proc = subprocess.run(["sh", str(SCRIPT), str(REPO / "CHANGELOG.md")],
                              capture_output=True, text=True)
        self.assertEqual(proc.stdout.strip(), expected)
```

文件顶部需要两个常量 —— 这个文件已经有 `REPO`（第 24 行），**直接沿用**，只在它旁边
加一行；并在顶部 import 区补 `import os` 与 `import subprocess`（当前没有，
`tempfile` 已有）：

```python
import os
import subprocess

SCRIPT = REPO / "packaging" / "common" / "version_of.sh"
```

- [x] **步骤 2：运行测试确认失败**

运行：`python3 -m unittest tests.test_release_artifacts.VersionOfScriptTests -v`
预期：4 个 ERROR/FAIL，首个报 `FileNotFoundError: ... packaging/common/version_of.sh`

- [x] **步骤 3：写实现**

创建 `packaging/common/version_of.sh`：

```sh
#!/bin/sh
# 版本号的唯一读法。usage: version_of.sh <CHANGELOG.md>  →  X.Y.Z（无标题则退出 1）
#
# 镜像线标题优先（`## [镜像线 vX.Y.Z · FPn]`），否则桌面线（`## [vX.Y.Z]`）。
# 这不是偏好，是 2026-10-19 之后的事实：两条线各自编号，镜像线标题永远带
# `镜像线 ` 前缀，所以只认 `## [v…]` 的正则会一路滑到桌面线的最新一条 ——
# 2026-10-27 实测：当前 CHANGELOG 下它返回 1.28.6，而镜像线是 1.28.11。
#
# 规则与 dev_tools/make_box_source.py::version_of() 必须一字不差；
# tests/test_release_artifacts.py::VersionOfScriptTests 把两个实现的输出钉在一起。
#
# 三处调用：packaging/rpi/build-image.sh（宿主）、
# packaging/common/mrrc-service-layer.sh（pi-gen chroot / 盒子 chroot）、
# linux/mrrc_update.sh（设备）。所以是 CLI 而不是被 source 的函数。
set -u
file="${1:-}"
[ -n "$file" ] && [ -f "$file" ] || {
	echo "version_of: no such CHANGELOG: ${file:-<none>}" >&2
	exit 1
}
# 两条独立的正则，而不是一条合并的 sed："文件里第一条匹配"不能决定答案 ——
# 镜像线的标题在文件里可能排在若干桌面线条目之后。
v=$(grep -m1 -oE '## \[镜像线 v[0-9]+\.[0-9]+\.[0-9]+' "$file" |
	grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
[ -n "$v" ] || v=$(grep -m1 -oE '## \[v[0-9]+\.[0-9]+\.[0-9]+\]' "$file" |
	grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
[ -n "$v" ] || {
	echo "version_of: no '## [镜像线 vX.Y.Z' or '## [vX.Y.Z]' heading in $file" >&2
	exit 1
}
printf '%s\n' "$v"
```

```bash
chmod +x packaging/common/version_of.sh
```

- [x] **步骤 4：运行测试确认通过**

运行：`python3 -m unittest tests.test_release_artifacts.VersionOfScriptTests -v`
预期：4 个 OK；`test_it_agrees_with_make_box_source` 断言当前值 `1.28.11`

- [x] **步骤 5：Commit**

```bash
git add packaging/common/version_of.sh tests/test_release_artifacts.py
git commit -m "feat(packaging): 版本号的唯一读法 —— 镜像线标题优先

只认 ## [vX.Y.Z] 的正则在镜像线编号（2026-10-19）之后一路滑到桌面线：
当前 CHANGELOG 下返回 1.28.6，而镜像线是 1.28.11。三处副本（Pi 构建器、
服务层兑底、就地升级器）各错各的，现在一份实现、一个测试钉住。"
```

---

### 任务 2：三个调用点改用 helper

**文件：**
- 修改：`packaging/rpi/build-image.sh:29`（`VERSION=` 那一行）
- 修改：`packaging/common/mrrc-service-layer.sh`（`log "version stamp"` 段落）
- 修改：`linux/mrrc_update.sh`（`echo "==> version stamp"` 段落）
- 测试：`tests/test_rpi_packaging.py`（新增一条）

- [x] **步骤 1：写失败的测试**

在 `tests/test_rpi_packaging.py` 的 `RpiPackagingFilesTests` 里追加：

```python
    def test_the_build_reads_the_version_through_the_shared_helper(self):
        """构建器不得自带一份版本正则（spec 2026-10-27 §2.4）。

        两份实现的后果是实测过的：`## [vX.Y.Z]` 只匹配桌面线，镜像线标题
        `## [镜像线 vX.Y.Z · FPn]` 永远不匹配，于是镜像会把自己报成桌面线的版本号。
        """
        text = (RPI / "build-image.sh").read_text(encoding="utf-8")
        self.assertIn("packaging/common/version_of.sh", text)
        self.assertIn('VERSION="$(sh ', text)
        self.assertNotIn(
            "grep -m1 -oE '## \\[v", text,
            "build-image.sh still carries its own CHANGELOG regex")
        self.assertIn("version_of.sh", SERVICE_LAYER.read_text(encoding="utf-8"))
        self.assertIn("version_of.sh", UPDATER.read_text(encoding="utf-8"))
```

文件头部补 `UPDATER = REPO_ROOT / "linux" / "mrrc_update.sh"`。

- [x] **步骤 2：运行测试确认失败**

运行：`python3 -m unittest tests.test_rpi_packaging.RpiPackagingFilesTests.test_the_build_reads_the_version_through_the_shared_helper -v`
预期：FAIL，`'packaging/common/version_of.sh' not found in ...`

- [x] **步骤 3：改三个调用点**

`packaging/rpi/build-image.sh`，把第 29 行整行替换为：

```bash
# 版本号的唯一读法（spec 2026-10-27 §4.0）。不要再在这里写正则：镜像线标题是
# `## [镜像线 vX.Y.Z · FPn]`，只认 `## [v…]` 的写法会拿回桌面线的版本号。
VERSION="$(sh "$REPO_ROOT/packaging/common/version_of.sh" "$REPO_ROOT/CHANGELOG.md")"
: "${VERSION:?Could not read the version from CHANGELOG.md}"
```

`packaging/common/mrrc-service-layer.sh`，把 version stamp 段里的

```bash
if [ -z "$version" ] && [ -f "$REPO_SRC/CHANGELOG.md" ]; then
	version="$(grep -m1 -oE '## \[v[0-9]+\.[0-9]+\.[0-9]+\]' "$REPO_SRC/CHANGELOG.md" |
		grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+')"
fi
```

替换为：

```bash
if [ -z "$version" ] && [ -f "$REPO_SRC/CHANGELOG.md" ]; then
	# 与 Pi 构建器、就地升级器同一份读法（spec 2026-10-27 §4.0）。helper 就在本层
	# 自己的素材目录里，缺了它上面的前置检查早就报错了。
	version="$(sh "$MRRC_ASSETS/version_of.sh" "$REPO_SRC/CHANGELOG.md" || true)"
fi
```

`linux/mrrc_update.sh`，把

```bash
if [ -n "${MRRC_UPDATE_VERSION:-}" ]; then
	version="$MRRC_UPDATE_VERSION"
elif [ -f "$SRC_TREE/CHANGELOG.md" ]; then
	version="$(grep -m1 -oE '## \[v[0-9]+\.[0-9]+\.[0-9]+\]' "$SRC_TREE/CHANGELOG.md" \
		2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)"
elif [ -s "$SRC_TREE/version.txt" ]; then
	version="$(head -1 "$SRC_TREE/version.txt" | tr -d '[:space:]')"
else
	version=""
fi
```

替换为：

```bash
if [ -n "${MRRC_UPDATE_VERSION:-}" ]; then
	version="$MRRC_UPDATE_VERSION"
elif [ -f "$SRC_TREE/packaging/common/version_of.sh" ]; then
	# 与 Pi 构建器、服务层同一份读法（spec 2026-10-27 §4.0）。用**取回来的**树里的
	# helper，和 stage-tree.sh 的理由一样：正在安装的这一版才知道该怎样读自己。
	version="$(sh "$SRC_TREE/packaging/common/version_of.sh" \
		"$SRC_TREE/CHANGELOG.md" 2>/dev/null || true)"
else
	version=""
fi
# staged 过的树没有 CHANGELOG.md（排除清单丢掉 /*.md），版本号只写在 version.txt 里 ——
# 这也是把本地目录当 MRRC_UPDATE_SRC 时最常见的形态。
if [ -z "$version" ] && [ -s "$SRC_TREE/version.txt" ]; then
	version="$(head -1 "$SRC_TREE/version.txt" | tr -d '[:space:]')"
fi
```

- [x] **步骤 4：运行测试确认通过**

运行：`python3 -m unittest tests.test_rpi_packaging tests.test_release_artifacts -v`
预期：全部 OK

- [x] **步骤 5：Commit**

```bash
git add packaging/rpi/build-image.sh packaging/common/mrrc-service-layer.sh linux/mrrc_update.sh tests/test_rpi_packaging.py
git commit -m "fix(packaging): 三个调用点改用版本号 helper，去掉各自的内联正则"
```

---

### 任务 3：管制域钉进构建（spec §2.1 / §4.1）

**文件：**
- 修改：`packaging/rpi/build-image.sh`（开关段 + `config` heredoc）
- 测试：`tests/test_rpi_packaging.py`

- [x] **步骤 1：写失败的测试**

在 `tests/test_rpi_packaging.py` 追加（`_config_heredoc` 放在模块级，两条测试共用）：

```python
def _pi_gen_config_text() -> str:
    """`build-image.sh` 写进 pi-gen `config` 的那段 heredoc 的正文。"""
    text = (RPI / "build-image.sh").read_text(encoding="utf-8")
    start = text.index('cat >"$WORK/config" <<EOF')
    body = text[start:].split("\n", 1)[1]
    return body.split("\nEOF", 1)[0]


class PiGenConfigTests(unittest.TestCase):
    def test_the_generated_config_sets_the_wlan_regulatory_domain(self):
        """不设 WPA_COUNTRY，pi-gen 就让镜像出厂 Wi-Fi 是关的（spec §2.1）。

        `stage2/02-net-tweaks/01-run.sh` 是
        `if [ -v WPA_COUNTRY ]; then … elif [ -d /var/lib/NetworkManager ]; then
        echo WirelessEnabled=false …`，而 `export WPA_COUNTRY`（build.sh 对未设
        变量的处理）并不让 `-v` 为真 —— 实测。后果：/settings 的 Wi-Fi 面板扫到 0
        个网络，烧卡预置的 mrrc-wifi.txt 也连不上。
        """
        config = _pi_gen_config_text()
        self.assertIn("WPA_COUNTRY='${WPA_COUNTRY_CFG}'", config)
        # 默认值必须非空：`${MRRC_PI_WPA_COUNTRY-}` 是同一个缺陷换个写法。
        text = (RPI / "build-image.sh").read_text(encoding="utf-8")
        match = re.search(r'^WPA_COUNTRY_CFG="\$\{MRRC_PI_WPA_COUNTRY-([A-Z]{2})\}"',
                          text, re.MULTILINE)
        self.assertIsNotNone(match, "MRRC_PI_WPA_COUNTRY has no non-empty default")
```

该文件顶部目前只有 `import unittest` 与 `from pathlib import Path`，需要补一行
`import re`。

- [x] **步骤 2：运行测试确认失败**

运行：`python3 -m unittest tests.test_rpi_packaging.PiGenConfigTests -v`
预期：FAIL，`AssertionError: 0 != 1`

- [x] **步骤 3：改 `build-image.sh`**

在 `FRP_PROXY=…` 那行之后插入：

```bash
# 无线电管制域。**不是偏好设置**：pi-gen 的 stage2/02-net-tweaks/01-run.sh 是
#   if [ -v WPA_COUNTRY ]; then raspi-config nonint do_wifi_country "$WPA_COUNTRY"
#   elif [ -d /var/lib/NetworkManager ]; then echo WirelessEnabled=false > …state
# 而 build.sh 对未设变量只做 `export WPA_COUNTRY`（不赋值），`[ -v ]` 判的是
# "有没有值" —— 实测为假。于是**不设这个键的镜像出厂 Wi-Fi 是关的**：/settings
# 的 Wi-Fi 面板扫不到任何网络，/boot/firmware/mrrc-wifi.txt 预置也连不上。
# 默认 CN：2.4G 1–13 与 5G 149–165，最不容易把客户的 AP 排除在外。
WPA_COUNTRY_CFG="${MRRC_PI_WPA_COUNTRY-CN}"
```

并在 `config` heredoc 里 `TARGET_HOSTNAME=MRRC-Modern` 之后加一行：

```
WPA_COUNTRY='${WPA_COUNTRY_CFG}'
```

以及构建开头的一行回显（放在 `echo "==> Building …"` 之后）：

```bash
echo "==> WLAN country: ${WPA_COUNTRY_CFG}"
```

- [x] **步骤 4：运行测试确认通过**

运行：`python3 -m unittest tests.test_rpi_packaging -v`
预期：全部 OK

- [x] **步骤 5：Commit**

```bash
git add packaging/rpi/build-image.sh tests/test_rpi_packaging.py
git commit -m "fix(rpi): 构建时写入 WPA_COUNTRY —— 不设它 pi-gen 就让出厂 Wi-Fi 关闭"
```

---

### 任务 4：解析链修复不再把健康的树莓派判成坏机器（spec §2.2 / §4.2）

**文件：**
- 修改：`linux/fix_resolv_conf.sh`（stub 判据）
- 测试：`tests/test_fix_resolv_conf.py`

- [x] **步骤 1：写失败的测试**

`tests/test_fix_resolv_conf.py`：先给 `run_script` 加 `state` 参数（保持既有调用不变）：

```python
    def run_script(self, *, args=(), unit=RESOLVED_STAND_IN, stub=None, state=None):
        env = {**os.environ,
               "MRRC_RESOLV_CONF": str(self.conf),
               "MRRC_RESOLV_STUB": str(stub or self.stub),
               "MRRC_RESOLVED_UNIT": unit,
               # 测试绝不能重启运行它们的机器上的 resolved（Linux CI 上它通常是活的）。
               "MRRC_RESOLV_RESTART": "0"}
        if state is not None:
            env["MRRC_RESOLVED_STATE"] = state
        return subprocess.run(["bash", str(SCRIPT), *args],
                              capture_output=True, text=True, env=env)
```

把既有 `test_a_missing_stub_is_a_warning_and_no_change` 改成显式注入 `state="active"`
（即：resolved 真在跑、但 stub 还没出现），并追加两条：

```python
    def test_a_missing_stub_is_a_warning_and_no_change(self):
        """Resolved installed and running, stub missing: linking to a missing file
        would break DNS outright, so the repair backs off and says why."""
        proc = self.run_script(stub=self.dir / "absent-stub", state="active")
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(self.conf.is_symlink())
        self.assertIn("does not exist", proc.stderr)

    def test_a_machine_that_does_not_use_resolved_is_not_an_error(self):
        """树莓派的解析链不是 systemd-resolved（spec 2026-10-27 §2.2）。

        pi-gen 在导出阶段把 /etc/resolv.conf 写成 `nameserver 8.8.8.8`，之后由
        NetworkManager 接管重写。resolved 不跑 ⇒ /run 里没有 stub。把这种机器判成
        坏的，就是让一台 DNS 完全正常的机器去点"修复 DNS"，并让每次升级都打一行
        假警告。文件仍然不许动。
        """
        proc = self.run_script(stub=self.dir / "absent-stub", state="absent")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.conf.is_symlink())
        self.assertIn("NetworkManager", proc.stdout)
        self.assertEqual(self.conf.read_text(encoding="utf-8"), BAKED)

    def test_an_undecidable_resolved_state_stays_conservative(self):
        """问不到 systemd 就不装懂：仍然退出 1，仍然不动文件。"""
        proc = self.run_script(stub=self.dir / "absent-stub", state="unknown")
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(self.conf.is_symlink())
```

- [x] **步骤 2：运行测试确认失败**

运行：`python3 -m unittest tests.test_fix_resolv_conf -v`
预期：`test_a_machine_that_does_not_use_resolved_is_not_an_error` FAIL（`1 != 0`），
`test_an_undecidable_resolved_state_stays_conservative` 可能已通过（现状就是 1）

- [x] **步骤 3：改 `linux/fix_resolv_conf.sh`**

在 `if [ -L "$CONF" ]` **之前**插入状态块：

```sh
# ── 这台机器用不用 systemd-resolved ──────────────────────────────────────
# /run 是 tmpfs，stub 只在 resolved 真跑着时才存在，所以"stub 不在"同时包含两种
# 完全不同的机器：resolved 装了但没起来（真问题），与这台机器根本不走 resolved
# （树莓派：NetworkManager 直接写 /etc/resolv.conf，pi-gen 的导出阶段还写死过
# nameserver 8.8.8.8）。把后者判成坏的，就是让一台 DNS 正常的机器去修 DNS。
# MRRC_RESOLVED_STATE=active|enabled|absent|unknown 供测试注入（spec §4.2）。
STATE="${MRRC_RESOLVED_STATE:-}"
if [ -z "$STATE" ]; then
	if ! command -v systemctl >/dev/null 2>&1; then
		STATE=unknown
	elif systemctl is-active --quiet systemd-resolved 2>/dev/null; then
		STATE=active
	elif systemctl is-enabled --quiet systemd-resolved 2>/dev/null; then
		STATE=enabled
	else
		STATE=absent
	fi
fi
```

把 stub 判据改成：

```sh
if [ ! -e "$STUB" ]; then
	if [ "$STATE" = "absent" ]; then
		echo "fix_resolv_conf: 这台机器不用 systemd-resolved（$STUB 不存在，且未启用）"
		echo "fix_resolv_conf: $CONF 由 NetworkManager 管，无需修复"
		exit 0
	fi
	echo "fix_resolv_conf: $STUB does not exist (resolved: $STATE) — $CONF left alone" >&2
	exit 1
fi
```

- [x] **步骤 4：运行测试确认通过**

运行：`python3 -m unittest tests.test_fix_resolv_conf -v`
预期：全部 OK（含既有的备份/幂等/dry-run/升级器调用四条）

- [x] **步骤 5：Commit**

```bash
git add linux/fix_resolv_conf.sh tests/test_fix_resolv_conf.py
git commit -m "fix(box): 解析链修复不再把 NetworkManager 托管的机器判成坏机器

树莓派的 stub 永远不在（resolved 不跑），旧判据于是对一台 DNS 正常的机器
退出 1：设置页的修复按钮报失败，每次 mrrc-update 打一行假警告。"
```

---

### 任务 5：产物门禁加七条（spec §4.3）

**文件：**
- 修改：`packaging/rpi/verify-image.sh`（step 2 五条、step 4 两条）
- 测试：`tests/test_rpi_packaging.py`（结构断言：脚本里真的有这七条）

- [x] **步骤 1：写失败的测试**

```python
class PiArtifactGateTests(unittest.TestCase):
    """produced artifact 的门禁必须真的写进脚本（spec §4.3）。

    七条都对应一次真事故发生过的半失败：升级链缺一半、提权通道没 enable、
    镜像带着构建机的解析地址、出厂 Wi-Fi 是关的、版本号来自另一条线、
    一键升级在镜像里导入不了、回滚探活永远成功。
    """

    def test_the_verify_script_gates_the_box_surface(self):
        text = (RPI / "verify-image.sh").read_text(encoding="utf-8")
        for needle in (
            "mrrc-box-helper.service",          # 提权通道装进来了
            "multi-user.target.wants",          # 且 enabled
            "WirelessEnabled=false",            # 出厂 Wi-Fi 不是关的
            "version_of.sh",                    # 版本号来自镜像线
            "192\\.168\\.65\\.",                # 解析链不是构建机留下的
            "box_update",                        # 一键升级能 import
            "curl",                             # 回滚探活有实现
        ):
            self.assertIn(needle, text, f"verify-image.sh 缺门禁: {needle}")
```

- [x] **步骤 2：运行测试确认失败**

运行：`python3 -m unittest tests.test_rpi_packaging.PiArtifactGateTests -v`
预期：FAIL，首个缺失项 `mrrc-box-helper.service`

- [x] **步骤 3：改 `packaging/rpi/verify-image.sh`**

step 2 里，把现有的 `v=$(cat …version.txt)` 那两行替换为下面整块（保留原回显风格）：

```sh
v=$(cat /mnt/opt/mrrc_modern/version.txt 2>/dev/null)
[ -n "$v" ] && ok "version.txt = $v" || fail "version.txt missing"
# 版本号必须来自**镜像线**（spec 2026-10-27 §2.4）：只认 `## [vX.Y.Z]` 的正则
# 会一路滑到桌面线的最新一条，产出自我报错版本号的镜像。
if want=$(sh /src/packaging/common/version_of.sh /src/CHANGELOG.md 2>/dev/null); then
	[ "$want" = "$v" ] && ok "version.txt matches the image line ($want)" \
		|| fail "version.txt=$v but CHANGELOG 镜像线=$want"
else
	fail "version_of.sh could not read /src/CHANGELOG.md"
fi
# 一键升级与提权通道：盒子上早就有，树莓派从来没验过（spec §4.3）。
grep -q "fix_resolv_conf.sh" /mnt/usr/local/bin/mrrc-update \
	&& ok "mrrc-update carries the resolver repair" \
	|| fail "mrrc-update does not call fix_resolv_conf.sh"
[ -f /mnt/etc/systemd/system/mrrc-box-helper.service ] \
	&& ok "mrrc-box-helper.service" || fail "mrrc-box-helper.service missing"
[ -L /mnt/etc/systemd/system/multi-user.target.wants/mrrc-box-helper.service ] \
	&& ok "box helper enabled" || fail "box helper is not enabled"
# 出厂 Wi-Fi 必须是开的：pi-gen 在 WPA_COUNTRY 缺失时写这一行（spec §2.1）。
nmstate=/mnt/var/lib/NetworkManager/NetworkManager.state
if [ -f "$nmstate" ] && grep -q "^WirelessEnabled=false" "$nmstate"; then
	fail "Wi-Fi ships soft-disabled (WPA_COUNTRY was not set at build time)"
else
	ok "Wi-Fi not soft-disabled"
fi
# 解析链：镜像不得带着构建机的地址出门（Armbian 2026-10-25 那个形态）。
if grep -qE "192\.168\.65\.|DNS requests are forwarded to the host" /mnt/etc/resolv.conf 2>/dev/null; then
	fail "/etc/resolv.conf carries the build host's resolver"
else
	ok "resolv.conf is not a build-host leftover"
fi
```

step 4（chroot 区段）追加两条，放在既有 entry-point import 之后：

```sh
chroot /mnt /opt/mrrc_modern/venv/bin/python -c "
import sys
sys.path.insert(0, '/opt/mrrc_modern')
sys.path.insert(0, '/opt/mrrc_modern/linux')
import box_update, box_helper
cmd = box_update.watchdog_command('/opt/mrrc_modern', '/tmp/rollback.tar.gz')
assert 'curl' in cmd, cmd
print('     box_update + box_helper import OK (watchdog probes with curl)')" \
	|| fail "box_update/box_helper import"
[ -x /mnt/usr/bin/curl ] && ok "curl present (the rollback probe)" \
	|| fail "curl missing — the rollback probe can never report a dead tree"
```

- [x] **步骤 4：运行测试确认通过**

运行：`python3 -m unittest tests.test_rpi_packaging -v` 且 `bash -n packaging/rpi/verify-image.sh`
预期：测试全 OK；`bash -n` 无输出（语法通过）

- [x] **步骤 5：Commit**

```bash
git add packaging/rpi/verify-image.sh tests/test_rpi_packaging.py
git commit -m "test(rpi): 产物门禁补七条 —— 一键升级、提权通道、管制域、版本号、解析链、curl"
```

---

### 任务 6：文档改成真话

**文件：**
- 修改：`docs/RASPBERRY_PI_GUIDE.md`（§二 烧卡、§三 首启、**§八 更新**、常见问题、新增验收记录小节）
- 修改：`docs/rpi_pack.md`（开关表 + 验证清单 + 现状校准）
- 修改：`AGENTS.md`（`packaging/` 行）

- [x] **步骤 1：改 `docs/RASPBERRY_PI_GUIDE.md`**

「八、更新」整节替换为：

```markdown
## 八、更新

**推荐做法：在浏览器里点一次。** 用管理员登录 → 右上角 ☰ → 「盒子设置 / 设置」→
「软件更新」卡 → 看到「可升级到 x.y.z」就点 **立即升级**。

- 升级会**短暂断线十几秒**（服务自重启），页面会自动重连，回来后 ☰ 页脚显示的
  `版本 · FPn` 就是新版本；
- 正在发射（PTT / 调谐 / CQ / FT8 发波）时**拒绝升级**，并告诉你为什么；
- 新代码 90 秒内没起来，会**自动退回**上一版并如实报告"已回滚"——不会变成砖。

### 老镜像（1.28.1 及更早）请重烧

1.28.1 及更早的镜像里还没有这条一键升级通道（它 2026-10-18 才落地）。这些机器**推荐
直接烧写新镜像**：镜像永远是"出厂状态"，最干净。

烧写前把 `/opt/mrrc_modern/env/mrrc.env` 的内容抄下来（口令、串口、声卡、录音目录），
烧完按「二、烧卡」的预置步骤放回 boot 分区。

> 拿不到 SD 卡（装在机柜里、线都扎好了）时才考虑另一条路：SSH 进去执行一次
> `sudo MRRC_UPDATE_SRC=https://github.com/cheenle/mrrc_modern mrrc-update`。
> **这一步做完以后就再也不用 SSH 了** —— 之后的每一次升级都在浏览器里点。
```

「二、烧卡」里替换下载卡数字为构建后的真实值，并改地址：

- `https://raspberrypi.local:8888` → `https://MRRC-Modern.local:8888`
  （文件内所有出现处，含常见问题表与 `ssh mrrc@raspberrypi.local`）

「三、上电与首次启动」补充引导页一行：

```markdown
浏览器打开后先看到 **引导页 `/setup`**：设一个网页登录口令（≥8 位），选一个 Wi-Fi
（也可以先跳过、走网线）。设完直接进主界面，不用再登一次。
```

新增「六、进来之后能做什么」小节（插在现有「五、接电台」与「六、常用命令」之间，
并把后续章节序号顺延）：

```markdown
## 六、进来之后能做什么

浏览器里那一套与中兴盒子镜像**完全一样**（两条镜像线共用同一份服务层）：

| 位置 | 能做什么 |
| --- | --- |
| ☰ 菜单 → 盒子设置 | Wi-Fi 扫描/切换/保存/忘记、有线 DHCP/静态、盒子名字、时区、系统口令、重启 |
| ☰ 菜单 → 软件更新 | 版本号（`版本 · FPn`）、检查更新、一键升级 |
| ☰ 菜单 → 性能负载 | CPU / 内存 / 磁盘 / 网络 / 温度 / 事件循环延迟 |
| ☰ 菜单 → 天线分析 | 接 NanoVNA-H 时扫 SWR、谐振点、调谐建议 |
| ☰ 菜单 → 蓝牙遥控 | 扫描、配对、重配（HID 遥控器按键直接换频） |
```

本任务**不**写「验收记录」小节：真机还没做，现在写就是先写结论。它由任务 11 在建有
实测数据时新增（含表头与那一行真数据）。

- [x] **步骤 2：改 `docs/rpi_pack.md`**

开关表加一行：

```markdown
| `MRRC_PI_WPA_COUNTRY` | 默认 `CN` | **不是偏好**：pi-gen 的 `stage2/02-net-tweaks/01-run.sh` 在 `WPA_COUNTRY` 未设时写 `WirelessEnabled=false`，镜像出厂的 Wi-Fi 就是关的（`/settings` 扫不到网络、烧卡预置连不上）。见 spec 2026-10-27 §2.1 |
```

「构建时数据流」之后加一小节：

```markdown
## 版本号从哪来

`packaging/common/version_of.sh` 是唯一读法：**镜像线标题优先**
（`## [镜像线 vX.Y.Z · FPn]`），否则桌面线（`## [vX.Y.Z]`）。Pi 构建器、服务层兑底、
设备上的 `mrrc-update` 三处都调它 —— 之前三处各带一份只认 `## [v…]` 的正则，
镜像线编号之后它们都会返回桌面线的版本号（2026-10-27 实测：1.28.6）。
```

- [x] **步骤 3：改 `AGENTS.md`**

`packaging/` 那一行的末尾（`... 断言。Manual` 之前）补：

```
Pi 平台壳还负责写 pi-gen 的 `WPA_COUNTRY`（默认 CN）：pi-gen 在该键未设时把
`WirelessEnabled=false` 写进 `NetworkManager.state`，镜像出厂的 Wi-Fi 就是关的，
`/settings` 的 Wi-Fi 面板与烧卡预置一起失效。
```

- [x] **步骤 4：检查**

运行：`grep -n "raspberrypi.local" docs/RASPBERRY_PI_GUIDE.md website/*.html website/zh/*.html`
预期：无输出（全部改成 `MRRC-Modern.local`）

- [x] **步骤 5：Commit**

```bash
git add docs/RASPBERRY_PI_GUIDE.md docs/rpi_pack.md AGENTS.md website/index.html website/zh/index.html
git commit -m "docs(rpi): 更新章节改为浏览器一键升级；老镜像推荐重烧；地址改 MRRC-Modern.local"
```

---

### 任务 7：写 CHANGELOG 条目（版本号与 FP 的唯一来源）

**文件：**
- 修改：`CHANGELOG.md`（插入到 `# Changelog` 头之后、现有第一条之前）

- [x] **步骤 1：确认下一个号**

```bash
grep -m1 -oE '## \[镜像线 v([0-9]+\.[0-9]+\.[0-9]+) · (FP[0-9]+)\]' CHANGELOG.md
```

预期输出形如 `## [镜像线 v1.28.11 · FP7]` ⇒ 本次 **`v1.28.12 · FP8`**（版本 +1、FP +1）。
若结果不是 1.28.11/FP7，以命令输出为准顺延，**不要**写死。

- [x] **步骤 2：插入条目**

```markdown
## [镜像线 v1.28.12 · FP8] — 2026-10-27 — 树莓派镜像重出：一键升级与盒子设置控制全面可用

**这是盒子线的第 8 个 Fix Pack**（版本 `1.28.12`，编号 `FP8`）。

**触发**：站上那张树莓派镜像停在 `1.28.1`（2026-10-17 构建），而盒子线已经到
`1.28.11 · FP7`。差的不是"几个功能"，而是**整条升级链**：`box_update` 于 2026-10-18
才落地，那之前的镜像里没有一键升级，也没有 FP 编号。

### 修掉四条只在 Pi 平台暴露的缺陷

- **出厂 Wi-Fi 是关的**（根因：构建参数缺失）。pi-gen 的
  `stage2/02-net-tweaks/01-run.sh` 在 `WPA_COUNTRY` 未设时把
  `WirelessEnabled=false` 写进 `NetworkManager.state`，而构建器的配置里从来没有这个键
  ——于是 `/settings` 的 Wi-Fi 面板扫到 0 个网络，`/boot/firmware/mrrc-wifi.txt` 预置
  也连不上，而这正是"烧好卡直接寄给客户"唯一的免屏配网路径。默认 `CN`
  （`MRRC_PI_WPA_COUNTRY` 可覆盖）。
- **构建脚本读错版本号**：只认 `## [vX.Y.Z]` 的正则不匹配镜像线标题
  `## [镜像线 vX.Y.Z · FPn]`，今天构建会把自己报成 `1.28.6` —— 开机就提示"可升级到
  1.28.11"，再拉 18 MB 覆盖同样的代码。现在 `packaging/common/version_of.sh` 是唯一读法，
  三处调用、一个测试钉住。
- **「修复 DNS」在健康的 Pi 上给出错误结论**：树莓派的解析链是 NetworkManager 直写
  `/etc/resolv.conf`，`systemd-resolved` 不跑，于是 stub 不存在，旧判据退出 1 ——
  文件没动（对的），结论是错的。现在先问"这台机器用不用 resolved"，不用的机器退出 0
  并说明原因。
- **产物门禁补七条**（`packaging/rpi/verify-image.sh`）：一键升级与提权通道能导入且
  已 enable、出厂 Wi-Fi 未被软关闭、版本号来自镜像线、`/etc/resolv.conf` 不是构建机残留、
  回滚探活的 `curl` 在镜像里。

### 交付

- **树莓派镜像重出**：`MRRC-Modern-rpi64.img.xz`（固定名），内含 1.28.12 的全部代码。
- **盒子线**：`linux/fix_resolv_conf.sh` 的行为修正随整包/热修下发给已装盒子；
  对其余功能无影响。

> **离线门禁**：产物七项检查通过（`packaging/rpi/verify-image.sh`）。
> **真机验收**：矩阵与结论见 `docs/superpowers/specs/2026-10-27-rpi-box-parity-design.md`
> §5，结论回填本条目。
```

- [x] **步骤 3：验证解析**

```bash
sh packaging/common/version_of.sh CHANGELOG.md
```

预期：`1.28.12`

- [x] **步骤 4：Commit**

```bash
git add CHANGELOG.md
git commit -m "release: v1.28.12 · FP8 —— 树莓派镜像重出，一键升级与盒子设置控制对齐"
```

---

### 任务 8：构建镜像（约 1 小时，可续跑）

**文件：** 无源码改动（产物：`dist/rpi/MRRC-Modern-v1.28.12-rpi64.img.xz`）

- [x] **步骤 1：前置检查**

```bash
docker info >/dev/null && echo "docker OK"
df -Pk "$(dirname build/pi-gen)" | awk 'NR==2 {print int($4/1048576)" GB free"}'
```

预期：`docker OK` 且 free ≥ 20 GB。不足时把 `MRRC_PI_WORK` 指到别的卷再跑。

- [x] **步骤 2：构建**

```bash
packaging/rpi/build-image.sh
```

预期：开头出现 `==> Building MRRC Modern v1.28.12 rpi64 image` 与
`==> WLAN country: CN`；中途 `ft8/dsp: building with gfortran (N jobs)`、
`version: 1.28.12`、`entry points import OK`、`=== overlay done ===`；
结尾打印产物路径与 SHA-256。

失败时：`cd build/pi-gen && CONTINUE=1 IGNORE_FILE_CHANGES=1 ./build-docker.sh` 续跑
（容器已被 `PRESERVE_CONTAINER=1` 保留）。

- [x] **步骤 3：Commit（无代码改动则跳过）**

产物不进仓库（`dist/` 在 `.gitignore` 内）。本步骤只在步骤 2 暴露了需要修的代码改动时执行。

---

### 任务 9：产物门禁（七条）

**文件：** 无

- [x] **步骤 1：跑验证**

```bash
packaging/rpi/verify-image.sh dist/rpi/MRRC-Modern-v1.28.12-rpi64.img.xz
```

预期：结尾 `== VERIFY_OK`，且输出里能看到
`version.txt matches the image line (1.28.12)`、`Wi-Fi not soft-disabled`、
`box helper enabled`、`box_update + box_helper import OK`、`curl present`。

- [x] **步骤 2：失败则回到对应任务**

| 失败项 | 回到 |
| --- | --- |
| `version.txt matches the image line` | 任务 1/2（helper 没接上） |
| `Wi-Fi not soft-disabled` | 任务 3（`WPA_COUNTRY` 没进 config） |
| `box_update/box_helper import` | 服务层 staging：`stage-tree.sh` 排除清单 |
| `curl present` | `packaging/common/packages.txt` |
| `resolv.conf is not a build-host leftover` | `packaging/rpi/build-image.sh` 的清场步骤 |

- [x] **步骤 3：记录产物**

```bash
shasum -a 256 dist/rpi/MRRC-Modern-v1.28.12-rpi64.img.xz
stat -f%z dist/rpi/MRRC-Modern-v1.28.12-rpi64.img.xz   # macOS；Linux 用 stat -c%s
```

把两个数字记下来，任务 10 要用。

---

### 任务 10：发布

**文件：**
- 修改：`website/downloads/latest.json`（由工具生成）
- 修改：`website/index.html`、`website/zh/index.html`（下载卡）
- 修改：`.agents/skills/dual-platform-release/release-artifacts.json`（rpi 规则转必检）

- [x] **步骤 1：源码包（盒子线）与热修包**

```bash
mkdir -p dist/box
python3 dev_tools/make_box_source.py --out dist/box/mrrc-modern-src-1.28.12.tar.gz --version 1.28.12 --fixpack FP8
```

热修包（可选但建议：相对 FP7 只改了一个 shell 脚本，整包要 18 MB —— 公网实测 5 分钟
只下 3.8 MB）。`make_box_source.py` 的 staging 目录是临时目录（跑完即删），所以热修包
**从刚生成的整包里解**：

```bash
rm -rf dist/box/patchtree && mkdir -p dist/box/patchtree
tar xzf dist/box/mrrc-modern-src-1.28.12.tar.gz -C dist/box/patchtree
tar czf dist/box/mrrc-modern-patch-1.28.12.tar.gz \
    -C dist/box/patchtree/mrrc-modern-1.28.12 ./linux/fix_resolv_conf.sh
```

包内路径必须是相对树根的 `./linux/…`：`box_update.extract()` 会拒绕对路径，而
`stage_over()` 按相对路径覆盖（包里只有 `linux/` 一个子目录、又没有 `server.py`，
所以 `apply()` 不会把 `tree` 误认成“带一层目录的整棵树”）。

- [x] **步骤 2：生成清单**

```bash
python3 dev_tools/make_latest_json.py \
  --box 1.28.12 https://www.vlsc.net/mrrc_modern/downloads/mrrc-modern-src-1.28.12.tar.gz \
        dist/box/mrrc-modern-src-1.28.12.tar.gz \
  --box-fixpack FP8 \
  --box-notes "树莓派镜像重出（一键升级 + 设置控制全面可用）；盒子侧只修了解析链修复脚本的判据" \
  --box-only --out website/downloads/latest.json
```

若做了热修包，再手工把 `patch` 段插进 `boxLine`（字段与 `upgrade_core.parse_manifest`
的严格校验一致）：

```json
"patch": {"version": "1.28.12", "requires": "1.28.11",
          "url": "https://www.vlsc.net/mrrc_modern/downloads/mrrc-modern-patch-1.28.12.tar.gz",
          "sha256": "<热修包 sha256>", "size": <字节数>, "notes": "热修：只含 linux/fix_resolv_conf.sh"}
```

然后用

```bash
python3 -c "import upgrade_core,json;print(upgrade_core.parse_manifest(open('website/downloads/latest.json').read())['box'])"
```

确认能解析（`parse_manifest` 接受 str/bytes/dict，热修段的 `requires` 缺失会被直接拒绝）。

- [x] **步骤 3：上传（`/var/tmp`，服务端先验 SHA，再 mv 到位）**

```bash
scp dist/box/mrrc-modern-src-1.28.12.tar.gz www.vlsc.net:/var/tmp/
ssh www.vlsc.net 'sha256sum /var/tmp/mrrc-modern-src-1.28.12.tar.gz'
# ↑ 与本地 shasum -a 256 逐字比对，再执行下面的 mv
ssh www.vlsc.net 'D=/var/www/vlsc.net/mrrc_modern/downloads; set -e;
  sudo -n mv /var/tmp/mrrc-modern-src-1.28.12.tar.gz $D/;
  sudo -n chown www-data:www-data $D/mrrc-modern-src-1.28.12.tar.gz;
  sudo -n chmod 644 $D/mrrc-modern-src-1.28.12.tar.gz'
```

镜像（约 546 MB，**必须走 `/var/tmp`**，`/tmp` 是 958 MB 的 tmpfs）：

```bash
ssh www.vlsc.net 'cat > /var/tmp/MRRC-Modern-v1.28.12-rpi64.img.xz.new' \
  < dist/rpi/MRRC-Modern-v1.28.12-rpi64.img.xz
ssh www.vlsc.net 'sha256sum /var/tmp/MRRC-Modern-v1.28.12-rpi64.img.xz.new'   # 比对后再往下
ssh www.vlsc.net 'D=/var/www/vlsc.net/mrrc_modern/downloads; set -e;
  sudo -n mv /var/tmp/MRRC-Modern-v1.28.12-rpi64.img.xz.new $D/MRRC-Modern-v1.28.12-rpi64.img.xz;
  sudo -n chown www-data:www-data $D/MRRC-Modern-v1.28.12-rpi64.img.xz;
  sudo -n chmod 644 $D/MRRC-Modern-v1.28.12-rpi64.img.xz;
  sudo -n ln -sfn MRRC-Modern-v1.28.12-rpi64.img.xz $D/MRRC-Modern-rpi64.img.xz'
```

最后上传清单（**必须是最后一个**，清单不能指向还在路上的文件）：

```bash
scp website/downloads/latest.json www.vlsc.net:/var/tmp/
ssh www.vlsc.net 'D=/var/www/vlsc.net/mrrc_modern/downloads; set -e;
  sudo -n mv /var/tmp/latest.json $D/; sudo -n chown www-data:www-data $D/latest.json;
  sudo -n chmod 644 $D/latest.json'
```

- [x] **步骤 4：更新两张下载卡**

`website/zh/index.html` 与 `website/index.html` 的 Pi 卡片：把
`531 MB (530,820,848 bytes)` 与 `SHA-256: a00ab228…` 换成任务 9 记录的新值，
并把 `https://raspberrypi.local:8888` 改成 `https://MRRC-Modern.local:8888`；
文案补一句"本版含浏览器内一键升级（1.28.1 及更早请重烧）"。

- [x] **步骤 5：`release-artifacts.json` 转必检**

把 `pi-guide` 那条的 `"optional": true` 删除（发布过就不再允许缺失），`why` 改为
`"Published Pi images must stay referenced by the guide."`。

- [x] **步骤 6：部署站点（**只跑一次**）**

```bash
cd website && echo y | ./deploy.sh && cd ..
```

- [x] **步骤 7：线上复核**

```bash
python3 .agents/skills/dual-platform-release/harness/release_check.py --online
curl -sI https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-rpi64.img.xz | head -3
curl -s https://www.vlsc.net/mrrc_modern/downloads/latest.json | python3 -m json.tool | head -20
```

预期：checker 全绿；卡片 URL 200 且 `content-length` 等于任务 9 的字节数；
清单里 `boxLine.version == "1.28.12"`、`fixpack == "FP8"`。

- [x] **步骤 8：Commit + tag + push**

```bash
git add website/index.html website/zh/index.html website/downloads/latest.json \
        .agents/skills/dual-platform-release/release-artifacts.json
git commit -m "release: 树莓派镜像 1.28.12 上线（一键升级 + 设置控制），下载卡与清单同步"
git tag v1.28.12
git push origin main --follow-tags
```

---

### 任务 11：真机验收（发布之后，spec §5）

**文件：**
- 修改：`docs/RASPBERRY_PI_GUIDE.md`（「验收记录」表）
- 修改：`CHANGELOG.md`（回填同一条 `[镜像线 v1.28.12 · FP8]` 条目）

- [ ] **步骤 1：按 spec §5 的矩阵逐条做，记录机型/镜像/结论**

必做四条：**首启**、**Wi-Fi 扫描**（§2.1 的回归判据）、**一键升级**、**回滚**。
其余按 spec §5 的"可以只做一次"处理。

- [ ] **步骤 2：把结论写进两处**

`docs/RASPBERRY_PI_GUIDE.md` 末尾**新增**「验收记录」小节（本任务之前它不存在）：

```markdown
## 验收记录

| 日期 | 机型 | 镜像 | 结论 |
| --- | --- | --- | --- |
| 2026-10-28 | Pi 4B 4GB | v1.28.12 | 12 条矩阵逐条写入：通过 / 不通过 / 未验证 |
```

（上表是**格式示例**，日期、机型、逐条结论必须是你真机跑出来的那几个值。）

`CHANGELOG.md` 同一条目末尾把

```
> **真机验收**：矩阵与结论见 …，结论回填本条目。
```

替换为实测结论（逐条：通过 / 不通过 / 未验证，附日期与机型）。

- [ ] **步骤 3：不通过时**

不要撤发布、不要改历史条目：修 → 重出镜像（同版本号 + FP+1 的新条目）→ 覆盖固定文件名。
这是 spec §4.6 定下的规则。

- [ ] **步骤 4：Commit**

```bash
git add docs/RASPBERRY_PI_GUIDE.md CHANGELOG.md
git commit -m "docs: 树莓派 1.28.12 真机验收结论回填"
```

---

## 自检记录

- **规格覆盖**：§2.1→任务 3；§2.2→任务 4；§2.3→任务 6/7；§2.4→任务 1/2；
  §4.0→任务 1/2；§4.1→任务 3；§4.2→任务 4；§4.3→任务 5/9；§4.4→任务 6；
  §4.5→任务 7/10；§4.6→任务 7/11；§5→任务 11。无遗漏。
- **占位符**：无。任务 6 **不**写「验收记录」（那时没有数据），它由任务 11 带实测值新增；
  任务 11 里那张表的日期/机型是**格式示例**，并已标注必须换成实测值。
- **类型/命名一致**：`version_of.sh`、`MRRC_PI_WPA_COUNTRY`、`WPA_COUNTRY_CFG`、
  `MRRC_RESOLVED_STATE`、`_pi_gen_config_text()` 在全文只有一种写法。
