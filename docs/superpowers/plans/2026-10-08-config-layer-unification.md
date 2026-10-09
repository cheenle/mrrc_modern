# 配置写入层统一（D-8）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 把今天在**各写各的** env 文件写入路径收敛成**一个**写入层 `env_store.py`，并把它钉死——之后任何新界面（`/manage`、`/setup`）都只能调它，不可能再长出第二份实现。

**架构：** 新增纯标准库、零应用依赖的根模块 `env_store.py`：容错读（BOM/UTF-8/cp936/latin-1，从 `first_run` 搬来）+ 纯函数合并（保注释、保序、保不属于自己的键）+ **跨平台文件锁下的原子替换**（保留目标文件的 mode 与 owner）+ 两道围栏（禁写键、安全门禁）+ `python -m env_store set` CLI。七个现存写入点全部改为调它。**重启不由本层做**——三个调用者的重启语义各不相同（`server` 退出码 42 交回启动器、`mrrc-radio` 调 systemctl、firstboot 时服务还没起），本层用 `EnvWriteResult` 把结果回报给调用者。

**技术栈：** Python 3.13.14（用主仓的 `.venv`，见全局约束 1）、纯标准库（`fcntl`/`msvcrt`、`os.replace`、`stat`、`argparse`）、`unittest`、bash（`install.sh`、`box-overlay.sh` 都在 `set -euo pipefail` 下）。

**设计文档：** `docs/superpowers/specs/2026-10-08-w103d-setup-ap-design.md`（下称"规格"；§N / D-N / R-N 指其章节、决策号与需求号）。本计划只做规格 §8.5 的**一期前半**（D-8 配置层统一）；`/manage` 页面、`/setup` 引导、`net_wifi` 适配层、热点**都不在本计划内**。

---

## 全局约束（每个任务都适用，违反即返工）

1. **测试环境与基线（2026-10-08 实测）**：本 worktree **没有自己的 venv**，用主仓那个：
   ```bash
   cd /Users/cheenle/HAM/hub/mrrc_modern/.worktrees/w103d-box
   PY=/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/python
   $PY -m unittest discover -s tests
   ```
   基线 ⇒ `Ran 1766 tests` / `FAILED (failures=1, skipped=1)`。那**唯一一条红是 worktree 路径造成的，不是产品缺陷**：`test_tls_trust_store.CallSiteGuardTests` 按**绝对路径**的 parts 过滤隐藏目录，而本 worktree 在 `.worktrees/` 下 ⇒ 扫描集为空 ⇒ 它自己的"扫描不许空转"断言失败。**任务 1 就修它**；在任务 1 完成前，任何一步的"预期全绿"都指"除这一条外全绿"。用 `unittest discover`，不用 `pytest`（`tests/README.md` 是权威：1766 tests / 93 modules）。
2. **永远不要 `git add -A` / `git commit -a`。** 仓里有与本任务无关的脏文件（`atr1000_tuner.json`、`mem_channels.json`——AGENTS.md 明写"用户运行期文件，勿动"——以及 `website/downloads/latest.json.bak-*`）。每个 commit 步骤都逐字列出要 add 的路径。
3. **`env_store.py` 必须是纯标准库、零应用 import。** 三个硬理由：① `install.sh` 在 STEP 7 用 `$PYTHON` 调它，那时只保证有个能跑的 venv，不保证应用依赖齐；② `box-overlay.sh` 在 chroot 里调它；③ 四个 PyInstaller spec 的 `pathex=[str(ROOT), ...]` 让根模块被 import 分析自动收进冻结包（`launcher_net.py` 的先例）。**不要**在里面 `import config` / `cloud_hub` / `server`。
4. **re-export 的名字一个字都不许改。** `read_env_text` 与 `update_env_file` 被以下地方直接 import / patch，改名即静默炸在装机环境里：
   - `packaging/box/firstboot_wrapper.py:31` 与 `packaging/rpi/pi-gen-stage4/01-deploy-mrrc/files/opt/mrrc_modern/linux/firstboot_wrapper.py:29`：`from first_run import read_env_text`（装机后 `first_run.py` 是它的**同级**文件）
   - `linux/mrrc_radio.py:29`：`from first_run import read_env_text, update_env_file`
   - `tests/test_server_setup.py:108`：`mock.patch.object(server.first_run, "update_env_file")`
   所以 `linux/first_run.py` 与 `macos/first_run.py` 必须**继续暴露这两个名字**（改成 re-export），而不是删掉。
5. **字节兼容优先。** 合并语义、尾部换行（`"\n".join(out) + "\n"`）、"总是写 UTF-8"这三条是既有测试钉住的（`tests/test_first_run.py:310`、`tests/test_linux_first_run.py:123`、`tests/test_mrrc_radio.py:91`、`tests/test_rpi_packaging.py` 的 GBK preseed 两用例）。新实现必须在这些测试**不改一行**的前提下通过——它们是这次收敛的回归网。
6. **本层绝不重启任何服务，也绝不碰 NetworkManager**（规格 D-4：两个域各有唯一写入者）。重启留给调用者，理由见"架构"段。
7. **`MRRC_CONFIG_FILE` 是禁写键**（规格 D-7 / §4.3）。它是 D-11 的载荷：systemd 单元用 `Environment=` 钉着它，写入层能改它 = 把"UI 显示已连接、上游 TLS 校验失败"那个静默故障挖回来。今天**没有任何代码把它写进 env 文件**（只有 `box-overlay.sh:118` 的 `Environment=` 与 `macos/launcher.py:400` 的 `os.environ`），所以这道闸是零成本的。
8. **`MRRC_ALLOW_UNVERIFIED_TX` 是安全门禁键**（AD-019 / NFR-067 / 规格 D-5）。写入 `1` 必须由调用点显式传 `allow_unverified_tx=True`，把"两步确认"变成**写入层的性质**而不是某个页面的标记。方向必须与既有 `tests/test_box_profiles.py::test_no_profile_opens_the_transmit_gate`（"=1 就红"）一致。今天代码里**没有任何路径写 `1`**（11 个 profile 全写 `0`，`server.py` / `config.py` / `backends/**` 只读它），所以这道闸不会卡住现有调用者。
9. **权限保持是本次收敛的净收益之一，不是装饰。** 盒子的 env 文件是 `0640 mrrc:mrrc`（`box-overlay.sh:177-178` 与两份 firstboot 都显式设过）且**里面是 Web 口令**；`Path.write_text` 新建的临时文件在常见 umask 下是 `0644`，于是 `cloud_hub._write_config` 的 `tmp.replace(path)` 今天会在**第一次 Cloud Hub connect 时把它悄悄放宽到 0644**。任务 4 修掉它并加测试。
10. **三份 `--exclude` 清单必须同步**：`tests/test_box_profiles.py::ExclusionParityTests` 断言 `packaging/rpi/build-image.sh`、`packaging/box/box-overlay.sh`、`linux/mrrc_update.sh` 三者的排除表**逐字相同**。本计划新增的 `env_store.py` 是根模块，三个 copier 都会自动带上，**不需要**改排除表——但如果你动了它们，三处一起动。
11. **SDD 守门**：编辑前
    ```bash
    python3 ~/.pi/agent/skills/sdd-guardian/harness/sdd_context.py brief <要改的文件>
    ```
    每次 commit 前必须干净：
    ```bash
    $PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
    ```
    本计划相关的 block 级约束是 `secrets-hardcoded`（口令只从环境变量来，绝不写字面量）与 `support-bundle-privacy`（口令/私钥绝不进诊断包）——所以**CLI 与日志一律只打键名，绝不打值**。
12. **命名口径统一（跨任务不许漂移）**：模块 `env_store`；函数 `read_env_text` / `parse_env_text` / `load` / `render` / `update_env_file` / `main`；异常基类 `EnvStoreError`，子类 `ProtectedKeyError` / `SafetyGateError` / `EnvLockTimeout`；结果 `EnvWriteResult`（字段 `path` / `changed` / `added` / `encoding` / `written`）；常量 `PROTECTED_KEYS` / `SAFETY_GATE_KEY` / `DEFAULT_LOCK_TIMEOUT`。
13. **不动这些**：`config.py`（它只**读** env，是常量层，不是写入层）、`backends/**`、`server.py` 里除 `/api/setup` 那一次调用之外的任何逻辑、`static/**`（本计划不含前端）、11 个 `packaging/box/profiles/*.env`（数据，`test_box_profiles` 钉着）、`linux/mrrc_update.sh`（它明写"env file left untouched"，是唯一正确的旁观者）、`tests/test_tls_trust_store.py` 的 `_SKIP_DIRS` 内容（任务 1 只改**判断方式**，不改名单）。

---

## 摸底结论：写入点不是 5 个，是 7 个

规格 D-8 的 2026-10-08 补充记了 5 处。实跑 `grep` 后是 **7 处**，而且语义**两两不同**——这正是"再加一个同意前几个的写入者"不可能成立的原因：

| # | 位置 | 今天的语义 | 坏在哪 |
| --- | --- | --- | --- |
| 1 | `linux/first_run.py:170` `update_env_file` | 合并、保注释、`path.write_text` | **非原子**（写一半被杀 = 截断的配置） |
| 2 | `macos/first_run.py:221` `update_env_file` | 与 #1 **同一函数的第二份** | 两份会漂移（`apply_first_run` 已经漂了，见"发现但未做"） |
| 3 | `cloud_hub.py:142` `_write_config` | 合并、`tmp.replace` 原子 | **丢掉所有注释**、重排键、**放宽文件权限**（约束 9） |
| 4 | `packaging/box/firstboot_wrapper.py:82-90` | 内联 append-if-absent + 整文件 `write_text` | 第三份合并实现，非原子 |
| 5 | `packaging/rpi/.../linux/firstboot_wrapper.py:80-88` | 与 #4 近乎逐字相同（只差 `/boot` vs `/boot/firmware`、`0.0.0.0` vs `::`） | 第四份 |
| 6 | `install.sh:769-798` | heredoc **整文件覆盖** `$SCRIPT_DIR/.env` | shell 插值无转义；覆盖已有文件时**不留备份** |
| 7 | `packaging/box/box-overlay.sh:170-176` | 5 行 `grep -q '^K=' \|\| echo 'K=V' >>` | 第五份 append-if-absent，与 #4 是同一意图的两份实现 |

调用者（不是写入者，但要跟着改）：`server.py:3219`（`/api/setup`）、`server.py:909/4546/4583/4736`（经 `cloud_hub._write_config`）、`linux/mrrc_radio.py:111`（`apply()`）、`macos/first_run.py:265`（探测前先落盘口令）、`linux/first_run.py:242` 与 `macos/first_run.py:313`（`apply_first_run` 收尾）。

---

## 文件结构

**新建**

| 路径 | 职责 |
|---|---|
| `env_store.py` | **唯一的 env 文件写入层**。容错读 + 纯函数合并 + 锁下原子替换（保 mode/owner）+ 禁写键与安全门禁 + `python -m env_store set` CLI。纯标准库、零应用依赖，可单测、可热修 |
| `tests/test_env_store.py` | 该纯模块的单测（不 import `server`/`cloud_hub`）：合并语义、容错读、锁、原子性与权限保持、两道围栏、CLI |
| `tests/test_env_store_convergence.py` | **收敛守卫**：① 规格 R4 的验收（两个入口各改一个字段，两个改动都在、第三个字段没丢）；② 出货树里不许再出现第二份 env 写入实现（源码级，沿用 `test_tls_trust_store.CallSiteGuardTests` 的形状） |

**修改**

| 路径 | 改什么 |
|---|---|
| `tests/test_tls_trust_store.py:143-146` | **前置修复**：隐藏目录过滤改按**相对 `REPO_ROOT`** 的 parts 判断（今天按绝对路径 ⇒ 任何 worktree 下扫描空转） |
| `linux/first_run.py:122-186` | 删掉本地 `_bom_encodings` / `read_env_text` / `update_env_file` 实现，改为 bootstrap `sys.path` + 从 `env_store` re-export 同名函数；清掉随之失效的 `codecs` / `locale` import |
| `macos/first_run.py:174-240` | 同上（**第二份实现消失**） |
| `cloud_hub.py:142-157` | `_write_config` 变 `env_store.update_env_file` 的薄壳（签名与 5 个调用点不变）；注释不再被丢、权限不再被放宽 |
| `server.py:34` 附近 + `:3219` | 加 `import env_store`；`/api/setup` 改调 `env_store.update_env_file`，并按 `EnvWriteResult` 记一行日志（只打键名） |
| `packaging/box/firstboot_wrapper.py:31,82-90` | 内联 append 改 `update_env_file(..., only_if_absent=True)`（仍从同级 `first_run` 取名字，装机路径不变） |
| `packaging/rpi/pi-gen-stage4/01-deploy-mrrc/files/opt/mrrc_modern/linux/firstboot_wrapper.py:29,80-88` | 同上 |
| `packaging/box/box-overlay.sh:168-176` | 5 行 `grep -q \|\| echo >>` 改一次 `env_store set --if-absent` |
| `install.sh:768-799` | heredoc 只负责**创建带注释的模板**（文件不存在时）；三个算出来的值改由 `env_store set` 写入；覆盖已有文件前先留备份 |
| `linux/mrrc_radio.py:47-56,64,72` | 删本地 `_parse_env_text`，改从 `env_store` 取 `parse_env_text`；`apply()` 的备份行为不变 |
| `tests/test_server_setup.py:108` | patch 目标从 `server.first_run.update_env_file` 改成 `server.env_store.update_env_file` |
| `tests/README.md:8,22` | 套件计数 1766 → 新值；新增两个模块小节 |
| `SDD/08-architecture-decisions.md` | 新 AD：env 文件的唯一写入层 |
| `SDD/14-version-history.md`、`SDD/README.md:50` | 新版本条目 + Quick Facts 版本号 |
| `AGENTS.md` | 模块表新增 `env_store.py` 一行 |
| `CHANGELOG.md` | 顶部 unreleased 段记这次收敛（含权限放宽的修复） |

**发现但未做（诚实记录，不在本计划内）**

- `linux/first_run.py` 与 `macos/first_run.py` 的 `apply_first_run` **已经漂移**：macOS 那份在串口探测**之前**先把口令落盘（2026-10-03 现场：探测再也没返回，留给运维的是原封不动的模板），Linux 那份没有。同一个失败模式在盒子上同样成立（`probe_radio_model` 卡住 ⇒ HDMI 横幅不打印、口令只在内存里）。**这是行为改动、且要真机首启验证**，不属于"写入层统一"，留给后续单独一条。收敛完成后这类漂移**不可能再发生在写入层**，但仍可能发生在 `apply_first_run`——记下来，别顺手改。
- `needs_first_run` / `detect_serial_ports` / `probe_*` 在 linux 与 macos 两份里也各有实现（平台差异是真实的：`DEFAULT_SERIAL_PORTS` 不同）。合并它们是另一件事。
- `config.load_user_config_into_environ()` 与 `server.py:4441/4609`、`support_bundle.py` 各自解析 env 文本（**只读**）。本计划统一的是**写入**；把读取也收敛到 `env_store.load()` 是合理后续，但会牵动 `config.py` 的导入时序（它的常量在 import 期就算好），不在本期。

---
## 任务 1：前置修复——worktree 下空转的 TLS 守卫

**为什么先做它：** 本计划每一步都以"跑套件、预期全绿"收尾。这条守卫在 `.worktrees/` 下**必定红**（扫描集为空），留着它，后面每一步的绿/红都失去意义。它的注释自己写着"扫描一旦空转（REPO_ROOT 指错、过滤写反）它必定失败"——它没坏，它**正确地报告了自己看不见任何东西**；坏的是过滤条件按绝对路径判断。

**文件：**
- 修改：`tests/test_tls_trust_store.py:143-146`（`CallSiteGuardTests._offenders`）
- 测试：`tests/test_tls_trust_store.py`（同一文件内新增一个用例）

- [ ] **步骤 1：先复现，确认根因就是路径**

运行：
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern/.worktrees/w103d-box
PY=/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/python
$PY -m unittest tests.test_tls_trust_store.CallSiteGuardTests -v
```
预期：`FAILED`，`AssertionError: 'server.py' not found in set() : the scan must actually look at the shipped tree`

再在主仓（**非** worktree）跑同一条，确认它在那里是绿的：
```bash
cd /Users/cheenle/HAM/hub/mrrc_modern && .venv/bin/python -m unittest tests.test_tls_trust_store.CallSiteGuardTests -v
```
预期：`OK`。两边代码相同、只有路径不同 ⇒ 根因是绝对路径里的 `.worktrees`。

- [ ] **步骤 2：写失败测试**

在 `tests/test_tls_trust_store.py` 的 `CallSiteGuardTests` 类里，`test_every_call_site_sets_a_context` **之前**插入：

```python
    def test_the_hidden_directory_filter_is_relative_to_the_repo(self):
        """A git worktree lives under ``.worktrees/`` — an absolute-path filter
        skips every file in it and the guard reports an empty scan.

        实测 2026-10-08：本仓的 worktree 路径是 ``…/mrrc_modern/.worktrees/w103d-box``，
        而过滤条件按 ``path.parts``（绝对路径的每一段）判断隐藏目录 ⇒ 整个出货树被
        跳过、``scanned`` 为空。superpowers 的工作流**总是**在 worktree 里跑，所以这条
        守卫在每个这样的会话里都是红的——而红的原因是它看不见代码，不是代码有问题。
        """
        offenders, scanned = self._offenders()
        self.assertIn("server.py", scanned)
        self.assertIn("net_tls.py", scanned)
        self.assertEqual([], offenders)
```

- [ ] **步骤 3：运行测试验证失败**

运行：`$PY -m unittest tests.test_tls_trust_store.CallSiteGuardTests -v`
预期：**两条都 FAIL**（新用例复现的就是旧用例的病因），报 `'server.py' not found in set()`。

- [ ] **步骤 4：改过滤条件为相对路径**

把 `_offenders()` 里的这段（今天在第 143-146 行）：

```python
        for path in sorted(REPO_ROOT.rglob("*.py")):
            parts = set(path.parts)
            if parts & _SKIP_DIRS or any(part.startswith(".") for part in path.parts):
                continue
```

改成：

```python
        for path in sorted(REPO_ROOT.rglob("*.py")):
            # 相对 REPO_ROOT 判断：绝对路径里可能带 .worktrees（git worktree）或任何
            # 以点开头的父目录，那与"出货树里有没有隐藏目录"无关。
            relative = path.relative_to(REPO_ROOT).parts
            if set(relative) & _SKIP_DIRS or any(p.startswith(".") for p in relative):
                continue
```

- [ ] **步骤 5：运行测试验证通过**

运行：`$PY -m unittest tests.test_tls_trust_store -v`
预期：`Ran 11 tests` / `OK`（今天 10 个用例 + 新的 1 个）。

- [ ] **步骤 6：变异验证——确认新测试真的盯着这行**

把第 4 步的 `path.relative_to(REPO_ROOT).parts` 临时改回 `path.parts`，运行：
```bash
$PY -m unittest tests.test_tls_trust_store.CallSiteGuardTests -v
```
预期：**两条都红**。改回来后预期全绿。（在 worktree 里这条变异必然复现；若它没红，说明你在主仓跑，换回 worktree 目录再验一次。）

- [ ] **步骤 7：跑全量，确认基线变成全绿**

运行：`$PY -m unittest discover -s tests 2>&1 | tail -4`
预期：`Ran 1767 tests` / `OK (skipped=1)`。**这是后续每个任务的基线。**

- [ ] **步骤 8：Commit**

```bash
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add tests/test_tls_trust_store.py
git commit -m "test: the TLS call-site guard skipped everything under a git worktree"
```

---

## 任务 2：`env_store.py` 的纯函数层——解析与合并

**为什么先做纯函数：** 合并语义是这次收敛的全部内容，而它**不需要磁盘**就能测完。`render()` 纯 ⇒ 本任务的测试全跑在内存里，后面几层（锁、原子、权限）各自只加自己那一条性质。

**文件：**
- 创建：`env_store.py`
- 创建：`tests/test_env_store.py`

- [ ] **步骤 1：写失败测试**

创建 `tests/test_env_store.py`（4 空格缩进、双引号，与仓内测试风格一致）：

```python
"""env_store: the one writer of MRRC's env file (design D-8).

Seven code paths used to write it on their own and disagreed in ways that only
a field report reveals — one dropped every comment and re-sorted the keys, one
widened the box's 0640 to 0644, and none could notice a key written between its
own read and its own write. These tests hold the single implementation to all
seven's promises at once.

Design: docs/superpowers/specs/2026-10-08-w103d-setup-ap-design.md §D-8
"""
from __future__ import annotations

import unittest

import env_store


TEMPLATE = """\
# MRRC Modern Configuration
# 注释必须活下来：这是运维唯一能读到的说明

# ── Serial ──────────────────────────────────────────────
MRRC_SERIAL_PORT=/dev/ttyUSB0
MRRC_BAUD_RATE=38400

# ── Web Server ──────────────────────────────────────────
MRRC_WEB_PORT=8888
MRRC_WEB_HOST=0.0.0.0
#MRRC_WEB_PASSWORD=
"""


class RenderTests(unittest.TestCase):
    """合并语义（纯函数，不碰磁盘）。"""

    def test_an_existing_key_keeps_its_line_and_its_comment(self):
        out = env_store.render(TEMPLATE, {"MRRC_WEB_PORT": "9000"})
        self.assertIn("MRRC_WEB_PORT=9000\n", out)
        self.assertIn("# ── Web Server ─", out)
        self.assertNotIn("MRRC_WEB_PORT=8888", out)

    def test_a_new_key_is_appended(self):
        out = env_store.render(TEMPLATE, {"MRRC_ATR1000_HOST": "10.0.0.7"})
        self.assertTrue(out.endswith("MRRC_ATR1000_HOST=10.0.0.7\n"))

    def test_keys_this_call_does_not_own_survive(self):
        """规格 D-8 第 2 条：保留不属于自己的键（口令、证书路径、Cloud Hub 写的键）。"""
        out = env_store.render(TEMPLATE, {"MRRC_WEB_PORT": "9000"})
        self.assertIn("MRRC_SERIAL_PORT=/dev/ttyUSB0", out)
        self.assertIn("MRRC_BAUD_RATE=38400", out)
        self.assertIn("MRRC_WEB_HOST=0.0.0.0", out)

    def test_a_commented_out_key_is_not_treated_as_that_key(self):
        """`#MRRC_WEB_PASSWORD=` 是文档，不是一个待改的键。

        `install.sh` 的模板与 `macos/default.env` 都靠注释掉的键做说明；把它当成键来
        改，会把运维的说明变成一条真的配置。
        """
        out = env_store.render(TEMPLATE, {"MRRC_WEB_PASSWORD": "s3cret"})
        self.assertIn("#MRRC_WEB_PASSWORD=", out, "the commented line is documentation")
        self.assertTrue(out.endswith("MRRC_WEB_PASSWORD=s3cret\n"))

    def test_the_value_is_taken_verbatim_after_the_first_equals(self):
        """`MRRC_SSL_CERT=/path/a=b.pem` 里只有第一个 `=` 是分隔符。"""
        out = env_store.render("", {"MRRC_SSL_CERT": "/etc/ssl/a=b.pem"})
        self.assertEqual(out, "MRRC_SSL_CERT=/etc/ssl/a=b.pem\n")

    def test_only_if_absent_leaves_a_present_key_alone(self):
        """两份 firstboot 与 box-overlay 的 headless defaults 就是这个语义。"""
        out = env_store.render(
            TEMPLATE,
            {"MRRC_WEB_PORT": "9000",
             "MRRC_SSL_CERT": "/var/lib/mrrc/certs/server.crt"},
            only_if_absent=True,
        )
        self.assertIn("MRRC_WEB_PORT=8888", out)
        self.assertNotIn("9000", out)
        self.assertIn("MRRC_SSL_CERT=/var/lib/mrrc/certs/server.crt", out)

    def test_an_empty_body_renders_as_a_single_newline(self):
        """字节兼容：今天的实现是 `"\\n".join(out) + "\\n"`。"""
        self.assertEqual(env_store.render("", {}), "\n")

    def test_a_file_without_a_trailing_newline_gains_one(self):
        self.assertEqual(env_store.render("MRRC_WEB_PORT=1", {}), "MRRC_WEB_PORT=1\n")


class ParseTests(unittest.TestCase):
    def test_comments_blanks_and_valueless_lines_are_skipped(self):
        parsed = env_store.parse_env_text(TEMPLATE)
        self.assertEqual(parsed["MRRC_SERIAL_PORT"], "/dev/ttyUSB0")
        self.assertEqual(parsed["MRRC_BAUD_RATE"], "38400")
        self.assertNotIn("MRRC_WEB_PASSWORD", parsed, "commented out is not set")

    def test_values_are_stripped(self):
        self.assertEqual(env_store.parse_env_text("A=  x  \n")["A"], "x")

    def test_the_last_occurrence_of_a_duplicated_key_wins(self):
        """systemd 的 EnvironmentFile 也是后者胜；两份不一致时别发明第三种。"""
        self.assertEqual(env_store.parse_env_text("A=1\nA=2\n")["A"], "2")

    def test_render_then_parse_round_trips_the_update(self):
        body = env_store.render(TEMPLATE, {"MRRC_WEB_PORT": "9000"})
        self.assertEqual(env_store.parse_env_text(body)["MRRC_WEB_PORT"], "9000")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_env_store -v`
预期：`ModuleNotFoundError: No module named 'env_store'`

- [ ] **步骤 3：写最少实现**

创建 `env_store.py`：

```python
"""The one writer of MRRC's env file (design D-8).

Seven code paths used to write ``mrrc.env`` / ``.env`` on their own:
``linux/first_run.py`` and ``macos/first_run.py`` (the same function twice),
``cloud_hub._write_config``, the two ``firstboot_wrapper.py`` copies,
``install.sh`` and ``box-overlay.sh``. They disagreed in ways that only a field
report reveals — one dropped every comment and re-sorted the keys, one widened
the box's ``0640`` to ``0644`` on the first Cloud Hub connect, and none of them
could notice a key written between its own read and its own write. This module
is the single implementation; all seven now call it.

Stdlib only and no app imports, on purpose: ``install.sh`` runs it with the
virtualenv's interpreter before the server exists, ``box-overlay.sh`` runs it
inside a chroot, and the PyInstaller specs already put the repo root on
``pathex`` (the ``launcher_net.py`` precedent). Restarting the service is
deliberately **not** here — the callers restart differently (``server`` exits 42
and lets the launcher relaunch it, ``mrrc-radio`` calls systemctl, firstboot has
not started the service yet), so the layer reports what it did through
``EnvWriteResult`` and the caller decides.
"""
from __future__ import annotations

__all__ = [
    "PROTECTED_KEYS", "SAFETY_GATE_KEY", "DEFAULT_LOCK_TIMEOUT",
    "EnvStoreError", "ProtectedKeyError", "SafetyGateError", "EnvLockTimeout",
    "read_env_text", "parse_env_text", "load", "render", "update_env_file",
    "EnvWriteResult", "main",
]


def _key_of(line: str) -> str | None:
    """The key of a ``KEY=VALUE`` line; None for a comment, blank or no ``=``.

    A commented-out key (``#MRRC_WEB_PASSWORD=``) is documentation, not a key:
    both ``install.sh``'s template and ``macos/default.env`` explain options
    that way, and treating such a line as set would turn an operator's note
    into a real configuration value.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        return None
    return line.split("=", 1)[0].strip()


def parse_env_text(text: str) -> dict[str, str]:
    """``KEY=VALUE`` of an env file body, comments and blanks skipped.

    Values are stripped. On a duplicated key the last one wins, which is what
    systemd's ``EnvironmentFile`` does too — two readers disagreeing about the
    same file is worse than either choice.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        key = _key_of(line)
        if key is None:
            continue
        out[key] = line.split("=", 1)[1].strip()
    return out


def render(text: str, updates: dict[str, str], *, only_if_absent: bool = False) -> str:
    """Merge ``updates`` into an env file body. Pure; ``update_env_file`` writes it.

    An existing key keeps its own line, and therefore the comment above it and
    the section it sits in; a new key is appended. Keys this call does not own
    are copied through untouched — design D-8's second requirement, and the
    reason a whole-file rewrite is never acceptable here.

    ``only_if_absent`` leaves a key that is already present alone. That is what
    the two firstboot wrappers and ``box-overlay.sh`` mean by "headless
    defaults, only keys that are not already set".
    """
    pending = dict(updates)
    out: list[str] = []
    for line in text.splitlines():
        key = _key_of(line)
        if key is not None and key in pending:
            value = pending.pop(key)
            out.append(line if only_if_absent else f"{key}={value}")
            continue
        out.append(line)
    for key, value in pending.items():
        out.append(f"{key}={value}")
    # Byte-compatible with the implementation this replaces ("\\n".join + "\\n"),
    # so a file that lacked a trailing newline gains one, and an empty body is a
    # single newline rather than an empty file.
    return "\n".join(out) + "\n"
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_env_store -v`
预期：`Ran 12 tests` / `OK`。

- [ ] **步骤 5：变异验证**

把 `render()` 里 `out.append(line if only_if_absent else f"{key}={value}")` 改成 `out.append(f"{key}={value}")`（即忽略 `only_if_absent`），运行：
```bash
$PY -m unittest tests.test_env_store.RenderTests.test_only_if_absent_leaves_a_present_key_alone -v
```
预期：FAIL。改回来。

再把 `_key_of` 里 `stripped.startswith("#")` 这个条件删掉，运行：
```bash
$PY -m unittest tests.test_env_store.RenderTests.test_a_commented_out_key_is_not_treated_as_that_key -v
```
预期：FAIL。改回来。

- [ ] **步骤 6：Commit**

```bash
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add env_store.py tests/test_env_store.py
git commit -m "feat: env_store, the merge semantics of the one env-file writer"
```

---
## 任务 3：容错读——把 `read_env_text` 从两份 `first_run` 搬过来

**为什么单独一个任务：** 这段代码是一次现场事故的产物（2026-09-12：preseed 被 ANSI/GBK 编辑器存过 ⇒ `mrrc-firstboot.service` **首启即失败**），而它今天在 `linux/first_run.py:122-168` 与 `macos/first_run.py:174-219` **各有一份**，两份的注释还互相写着 "Keep in sync with …"——那就是漂移的自白。搬动时必须**逐字保留回退顺序**（UTF-32 BOM 先于 UTF-16，因为前者的 BOM 以前者开头；然后 utf-8 → cp936 → 本地代码页 → latin-1），因为 `tests/test_first_run.py:310`、`tests/test_linux_first_run.py:123`、`tests/test_rpi_packaging.py` 的 GBK preseed 两用例都钉着它。

**文件：**
- 修改：`env_store.py`（追加）
- 测试：`tests/test_env_store.py`（追加）

- [ ] **步骤 1：写失败测试**

在 `tests/test_env_store.py` 末尾（`if __name__` 之前）追加；并把文件头的 import 改成：

```python
import tempfile
import unittest
from pathlib import Path

import env_store
```

追加的用例（`DAMAGED` 就是 `tests/test_linux_first_run.py` 里那份现场文件的字节）：

```python
#: 现场那份损坏文件：em dash `e2 80 94` 被 ANSI 编辑器写成了 `e2 80 3f`。
DAMAGED = (b"# mrrc_modern.env \xe2\x80?MRRC Modern launcher "
           b"configuration template.\nMRRC_WEB_PASSWORD=secret\n"
           b"MRRC_RADIO_MODEL=ft710\n")


class ReadEnvTextTests(unittest.TestCase):
    """容错读：一个在运维自己的 PC 上、用他的编辑器存过的文件。"""

    def _write(self, tmp: str, raw: bytes) -> Path:
        path = Path(tmp) / "mrrc.env"
        path.write_bytes(raw)
        return path

    def test_the_field_regression_file_is_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            text, enc = env_store.read_env_text(self._write(tmp, DAMAGED))
        self.assertIn("MRRC_WEB_PASSWORD=secret", text)
        self.assertIn("MRRC_RADIO_MODEL=ft710", text)
        self.assertNotEqual(enc, "utf-8")

    def test_gbk_values_survive(self):
        name = "麦克风 (USB Audio CODEC)"
        raw = ("# 配置\nMRRC_AUDIO_RX_DEVICE=" + name + "\n").encode("cp936")
        with tempfile.TemporaryDirectory() as tmp:
            text, enc = env_store.read_env_text(self._write(tmp, raw))
        self.assertNotEqual(enc, "utf-8")
        self.assertIn(f"MRRC_AUDIO_RX_DEVICE={name}", text)

    def test_utf16_saved_file_is_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = "MRRC_WEB_PORT=8888\r\n".encode("utf-16")
            text, enc = env_store.read_env_text(self._write(tmp, raw))
        self.assertIn("MRRC_WEB_PORT=8888", text)

    def test_utf8_files_stay_untouched(self):
        raw = "# 中文注释 — em dash\nMRRC_RADIO_MODEL=ic7300\n".encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            text, enc = env_store.read_env_text(self._write(tmp, raw))
        self.assertEqual(enc, "utf-8")
        self.assertIn("中文注释 — em dash", text)

    def test_a_missing_file_raises_rather_than_inventing_config(self):
        """读不存在的文件是调用者的错（它应该先 `load()`），不是这里该吞的。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                env_store.read_env_text(Path(tmp) / "nope.env")


class LoadTests(unittest.TestCase):
    def test_a_missing_file_loads_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(env_store.load(Path(tmp) / "nope.env"), {})

    def test_load_is_read_then_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mrrc.env"
            path.write_bytes(DAMAGED)
            self.assertEqual(env_store.load(path)["MRRC_RADIO_MODEL"], "ft710")
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_env_store -v`
预期：`AttributeError: module 'env_store' has no attribute 'read_env_text'`（新增 7 个用例全红，任务 2 的 12 个仍绿）。

- [ ] **步骤 3：写实现——从 `linux/first_run.py:122-168` 搬过来**

在 `env_store.py` 顶部补 import（`from __future__` 那行之后）：

```python
import codecs
import locale
import sys
from pathlib import Path
```

然后在 `parse_env_text` **之前**插入这两个函数（主体与 `linux/first_run.py:122-168` 逐字相同，只改了 docstring 里那句已经不成立的 "Keep in sync with macos/first_run.py"）：

```python
def _bom_encodings() -> tuple[tuple[bytes, str], ...]:
    """BOMs a text editor may have written (UTF-32 first: its BOM prefixes UTF-16's)."""
    return (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    )


def read_env_text(path: Path) -> tuple[str, str]:
    """Read a user-editable text file without ever raising on its encoding.

    Returns ``(text, encoding)`` where ``encoding`` is ``"utf-8"`` for a clean
    file and the fallback that was used otherwise, so callers can say so.

    On the Pi and the box this file arrives as the **preseed**: written on the
    operator's own PC and copied onto the boot partition, so a non-UTF-8 save
    (an ANSI/GBK editor turns the template's em-dash ``e2 80 94`` into
    ``e2 80 3f``) is the likeliest encoding to show up. A strict UTF-8 read made
    ``mrrc-firstboot.service`` fail on the very first boot — the same field bug
    as the Windows/macOS launchers (2026-09-12). This is now the **only** copy:
    ``linux/first_run.py`` and ``macos/first_run.py`` re-export it.
    """
    raw = path.read_bytes()
    for bom, encoding in _bom_encodings():
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace"), encoding
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    # Not UTF-8: prefer the local code page (so Chinese device names survive),
    # then GBK explicitly, then a codec that cannot fail — KEY=VALUE lines are
    # ASCII and still parse.
    for encoding in ("cp936", locale.getpreferredencoding(False), "latin-1"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        print(f"Warning: {path} is not valid UTF-8; reading it as {encoding}. "
              f"Re-save it as UTF-8 (any edit from MRRC does that).",
              file=sys.stderr)
        return text, encoding
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def load(path: Path) -> dict[str, str]:
    """The env as it is on disk right now; empty when the file is absent.

    Replaces ``mrrc_radio.current_env()`` and the ad-hoc ``read_env_text(...) →
    parse`` pairs. Never raises on a missing file: "not configured yet" is the
    normal state on a first boot, not an error.
    """
    path = Path(path)
    if not path.is_file():
        return {}
    return parse_env_text(read_env_text(path)[0])
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_env_store -v`
预期：`Ran 19 tests` / `OK`。

- [ ] **步骤 5：变异验证**

把 `read_env_text` 里的 `for encoding in ("cp936", locale.getpreferredencoding(False), "latin-1"):` 改成 `for encoding in ("utf-8",):`，运行：
```bash
$PY -m unittest tests.test_env_store.ReadEnvTextTests.test_gbk_values_survive -v
```
预期：FAIL（utf-8 解不了 cp936 字节 ⇒ 落到 `errors="replace"` 分支，中文声卡名变成乱码）。改回来。

- [ ] **步骤 6：跑全量**

运行：`$PY -m unittest discover -s tests 2>&1 | tail -3`
预期：`Ran 1786 tests` / `OK (skipped=1)`（1767 + 19）。

- [ ] **步骤 7：Commit**

```bash
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add env_store.py tests/test_env_store.py
git commit -m "feat: env_store owns the tolerant env reader, one copy instead of two"
```

---

## 任务 4：锁下的原子写——`update_env_file` 与 `EnvWriteResult`

**这一任务同时修两个真缺陷：**
1. **非原子**（写入点 #1/#2/#4/#5）：`path.write_text(...)` 写到一半被杀 = 一份截断的配置，而它里面有 Web 口令。
2. **权限放宽**（写入点 #3，全局约束 9）：`cloud_hub._write_config` 的 `tmp.replace(path)` 不继承 mode，新建的 tmp 在 umask 022 下是 `0644` ⇒ 盒子上那份 `0640` 的口令文件在**第一次 Cloud Hub connect 后被静默放宽**。

**文件：**
- 修改：`env_store.py`（追加）
- 测试：`tests/test_env_store.py`（追加）

- [ ] **步骤 1：写失败测试**

在 `tests/test_env_store.py` 末尾追加；文件头 import 补上：

```python
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
```

```python
class UpdateEnvFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "mrrc.env"
        self.path.write_text(TEMPLATE, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_merge_keeps_the_comment_the_key_sat_under(self):
        """写入点 #3 今天会把整个文件的注释删光。"""
        env_store.update_env_file(self.path, {"MRRC_WEB_PORT": "9000"})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("# ── Web Server ─", text)
        self.assertIn("# 注释必须活下来", text)
        self.assertIn("MRRC_WEB_PORT=9000", text)

    def test_a_key_it_does_not_own_survives_on_disk(self):
        env_store.update_env_file(self.path, {"MRRC_WEB_PORT": "9000"})
        loaded = env_store.load(self.path)
        self.assertEqual(loaded["MRRC_SERIAL_PORT"], "/dev/ttyUSB0")
        self.assertEqual(loaded["MRRC_WEB_HOST"], "0.0.0.0")

    def test_it_creates_the_file_and_its_parents(self):
        target = Path(self._tmp.name) / "env" / "deep" / "mrrc.env"
        env_store.update_env_file(target, {"MRRC_WEB_PORT": "8888"})
        self.assertEqual(env_store.load(target)["MRRC_WEB_PORT"], "8888")

    def test_a_non_utf8_file_is_healed_to_utf8_by_the_write(self):
        self.path.write_bytes(DAMAGED)
        env_store.update_env_file(self.path, {"MRRC_WEB_PORT": "8443"})
        text = self.path.read_bytes().decode("utf-8")   # must not raise
        self.assertIn("MRRC_WEB_PORT=8443", text)
        self.assertIn("MRRC_WEB_PASSWORD=secret", text)

    def test_only_if_absent_leaves_the_present_value_alone(self):
        env_store.update_env_file(self.path, {"MRRC_WEB_PORT": "9000"},
                                  only_if_absent=True)
        self.assertEqual(env_store.load(self.path)["MRRC_WEB_PORT"], "8888")

    def test_no_temporary_file_is_left_behind(self):
        env_store.update_env_file(self.path, {"MRRC_WEB_PORT": "9000"})
        leftovers = [p.name for p in Path(self._tmp.name).iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual([], leftovers)

    def test_the_result_reports_what_changed_and_what_was_added(self):
        result = env_store.update_env_file(
            self.path,
            {"MRRC_WEB_PORT": "9000", "MRRC_ATR1000_HOST": "10.0.0.7"},
        )
        self.assertEqual(result.changed, {"MRRC_WEB_PORT": "9000"})
        self.assertEqual(result.added, ("MRRC_ATR1000_HOST",))
        self.assertEqual(result.encoding, "utf-8")
        self.assertTrue(result.written)
        self.assertEqual(result.path, self.path)

    def test_a_write_that_changes_nothing_says_so(self):
        """`written=False` 是调用者"要不要重启"的判据之一，不能总是 True。"""
        result = env_store.update_env_file(self.path, {"MRRC_WEB_PORT": "8888"})
        self.assertFalse(result.written)
        self.assertEqual(result.changed, {})
        self.assertEqual(result.added, ())


class FileModeTests(unittest.TestCase):
    """盒子的 env 文件是 `0640 mrrc:mrrc`，里面是 Web 口令。"""

    def test_an_atomic_write_keeps_the_mode_of_the_file_it_replaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mrrc.env"
            path.write_text("MRRC_WEB_PASSWORD=secret\n", encoding="utf-8")
            os.chmod(path, 0o640)
            env_store.update_env_file(path, {"MRRC_WEB_PORT": "8888"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_the_mutation_that_caused_the_widening_is_caught(self):
        """把保持 mode 的那行删掉，本用例必须红——它就是云接入今天的写法。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mrrc.env"
            path.write_text("A=1\n", encoding="utf-8")
            os.chmod(path, 0o600)
            env_store.update_env_file(path, {"B": "2"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class LockTests(unittest.TestCase):
    """规格 R4：并发改不丢字段（整文件不可 last-write-wins）。"""

    def test_a_second_writer_waits_rather_than_interleaving(self):
        """确定性版本：锁真的在拦人（不依赖时序，所以不会闪红）。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mrrc.env"
            path.write_text("A=1\n", encoding="utf-8")
            with env_store._locked(path, 1.0):
                with self.assertRaises(env_store.EnvLockTimeout):
                    env_store.update_env_file(path, {"B": "2"}, lock_timeout=0.2)

    def test_two_writers_in_parallel_lose_neither_field(self):
        """真并发版本：两个入口各改一个字段，第三个字段没丢。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mrrc.env"
            path.write_text("MRRC_WEB_PASSWORD=keepme\n", encoding="utf-8")
            errors: list[BaseException] = []
            barrier = threading.Barrier(2)

            def writer(key: str, value: str) -> None:
                try:
                    barrier.wait(timeout=5)
                    for i in range(25):
                        env_store.update_env_file(path, {key: f"{value}{i}"})
                except BaseException as exc:      # noqa: BLE001 - reported below
                    errors.append(exc)

            threads = [
                threading.Thread(target=writer, args=("MRRC_WEB_PORT", "9")),
                threading.Thread(target=writer, args=("MRRC_SCOPE_BAUD", "1")),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            self.assertEqual([], errors)
            loaded = env_store.load(path)
            self.assertEqual(loaded["MRRC_WEB_PASSWORD"], "keepme")
            self.assertTrue(loaded["MRRC_WEB_PORT"].startswith("9"))
            self.assertTrue(loaded["MRRC_SCOPE_BAUD"].startswith("1"))
```

- [ ] **步骤 2：运行测试验证失败**

运行：`$PY -m unittest tests.test_env_store -v`
预期：新增用例全红（`AttributeError: module 'env_store' has no attribute 'update_env_file'`），任务 2、3 的 19 个仍绿。

- [ ] **步骤 3：写实现**

在 `env_store.py` 顶部补 import：`import os`、`import stat`、`import time`、`from contextlib import contextmanager`、`from dataclasses import dataclass`、`from typing import Iterator`。然后在 `load()` 之后追加：

```python
DEFAULT_LOCK_TIMEOUT = 5.0


class EnvStoreError(Exception):
    """Base class: every failure here is reportable, not fatal-on-boot."""


class EnvLockTimeout(EnvStoreError):
    """Another MRRC process held this env file's write lock for too long."""


@dataclass(frozen=True)
class EnvWriteResult:
    """What one write did, so the caller can decide and report (design D-8 §3).

    The layer never restarts anything: the three callers restart differently
    (``server`` exits 42 and lets the launcher relaunch it, ``mrrc-radio`` calls
    systemctl, firstboot has not started the service yet), so "写后按需重启" is
    the caller's decision and this is what it decides from.
    """

    path: Path
    changed: dict[str, str]      #: key -> new value, for keys that were already there
    added: tuple[str, ...]       #: keys the file did not have before
    encoding: str                #: what the file was read as ("utf-8" normally)
    written: bool                #: False when nothing on disk needed to change


def _acquire(fd: int) -> None:
    """One non-blocking attempt at an exclusive lock; raises OSError when busy.

    ``msvcrt`` on Windows and ``fcntl`` on POSIX — both stdlib, because the
    Windows launcher writes this file too and the layer must not grow a
    dependency (全局约束 3).
    """
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass                      # closing the fd releases it anyway
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _locked(path: Path, timeout: float) -> Iterator[None]:
    """Hold ``<path>.lock`` exclusively across the read-merge-write.

    A merge is only merge-safe if nobody else's write lands between this
    process's read and its replace — that is exactly what R4 means by "并发改
    不丢字段". A *sibling* lock file rather than the env file itself: the write
    replaces the target by rename, so a lock on its inode would be left holding
    a file nobody ever reads again.

    A stale lock is not a failure mode worth designing around: the OS releases
    an flock/msvcrt lock when the holding process dies, so a crashed writer
    cannot lock the box out. The lock file itself is left on disk (deleting it
    would race the next acquirer) and carries no configuration.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.with_name(path.name + ".lock")),
                 os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                _acquire(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise EnvLockTimeout(
                        f"{path} stayed locked for {timeout:.1f}s; another MRRC "
                        "process is writing it"
                    ) from None
                time.sleep(0.01)
        yield
    finally:
        try:
            _release(fd)
        finally:
            os.close(fd)


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` by rename, keeping the target's mode and owner.

    Preserving them is the point, not a nicety: the box's env file is
    ``0640 mrrc:mrrc`` (``box-overlay.sh`` and both firstboot wrappers set it)
    and holds the web password, while a fresh temp file is created ``0644``
    under the usual umask — so the plain ``tmp.replace(path)`` that
    ``cloud_hub._write_config`` used silently widened it on the first Cloud Hub
    connect.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        original = path.stat()
    except OSError:
        original = None
    if original is not None:
        os.chmod(tmp, stat.S_IMODE(original.st_mode))
        if hasattr(os, "chown"):          # not on Windows
            try:
                os.chown(tmp, original.st_uid, original.st_gid)
            except OSError:
                pass    # not the owner (or not root): mode is the part that matters
    os.replace(tmp, path)


def update_env_file(
    path: Path,
    updates: dict[str, str],
    *,
    only_if_absent: bool = False,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> EnvWriteResult:
    """Merge ``updates`` into the env file at ``path`` — the only writer.

    Read → merge → atomic replace, under a lock, preserving comments, the keys
    this call does not own (the password, certificate paths, whatever Cloud Hub
    wrote), the file's mode and its owner. Creates the file and its directory
    when absent. Always writes UTF-8: a file that arrived from an ANSI/GBK
    editor is normalised by the first write.

    The positional signature is the one ``linux/first_run.py`` and
    ``macos/first_run.py`` already had, so their callers (``server.py``,
    ``linux/mrrc_radio.py``, both firstboot wrappers) and the tests that patch
    the name keep working unchanged.
    """
    path = Path(path)
    with _locked(path, lock_timeout):
        existed = path.exists()
        before_bytes = path.read_bytes() if existed else b""
        text, encoding = read_env_text(path) if existed else ("", "utf-8")
        before = parse_env_text(text)
        body = render(text, updates, only_if_absent=only_if_absent)
        after = parse_env_text(body)
        after_bytes = body.encode("utf-8")
        # Compare bytes, not text: a cp936 file whose parsed content did not
        # change still has to be rewritten, because the promise is "any edit
        # from MRRC normalises it to UTF-8".
        written = after_bytes != before_bytes
        if written:
            _atomic_write(path, body)
    return EnvWriteResult(
        path=path,
        changed={k: after[k] for k in after if k in before and after[k] != before[k]},
        added=tuple(k for k in after if k not in before),
        encoding=encoding,
        written=written,
    )
```

- [ ] **步骤 4：运行测试验证通过**

运行：`$PY -m unittest tests.test_env_store -v`
预期：`Ran 33 tests` / `OK`。

- [ ] **步骤 5：变异验证（两条，逐条改回）**

1. 把 `_atomic_write` 里的 `os.chmod(tmp, stat.S_IMODE(original.st_mode))` 删掉，运行：
   ```bash
   $PY -m unittest tests.test_env_store.FileModeTests -v
   ```
   预期：两条都 FAIL（得到 `0o644`）——这就是 `cloud_hub._write_config` 今天在盒子上干的事。改回来。
2. 把 `update_env_file` 里的 `with _locked(path, lock_timeout):` 整层去掉（保留体内代码、改缩进），运行：
   ```bash
   $PY -m unittest tests.test_env_store.LockTests -v
   ```
   预期：`test_a_second_writer_waits_rather_than_interleaving` FAIL（`EnvLockTimeout` 不再抛）。改回来。

- [ ] **步骤 6：跑全量**

运行：`$PY -m unittest discover -s tests 2>&1 | tail -3`
预期：`Ran 1800 tests` / `OK (skipped=1)`（1786 + 14）。

- [ ] **步骤 7：Commit**

```bash
$PY .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git add env_store.py tests/test_env_store.py
git commit -m "feat: env_store writes atomically under a lock and keeps the file mode"
```

---
%%NEXT%%
