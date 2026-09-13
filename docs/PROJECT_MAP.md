# MRRC Modern 项目地图（文档 / 设计 / 工程 / Harness / 发布）

> 这份文件回答一个问题：**"我要改 X，必须同步哪些东西？发布时必须动哪些东西？"**
> 它是人读的入口；机器可读的对应物是 `.agents/skills/sdd-guardian/harness/index.json`（知识路由）
> 与 `.agents/skills/dual-platform-release/release-artifacts.json`（发布承载物注册表）。

---

## 1. 真相来源（谁说了算）

| 主题 | 唯一真相来源 | 由什么强制 |
| --- | --- | --- |
| 应用版本号 | `CHANGELOG.md` 顶部 `## [vX.Y.Z]` | 构建脚本解析它；`release_check` 以它为准 |
| SDD 版本号 | `SDD/14-version-history.md` **首行** | `tests/test_sdd_docs_consistency.py`（README/生成页/落地页/首页必须一致） |
| 设计决策 | `SDD/08-architecture-decisions.md`（AD-001 … AD-019） | `release_check` 的 AD 索引规则 + 守护 `constraints.json` |
| 工程约束（踩坑换来的） | `.agents/skills/sdd-guardian/harness/constraints.json` | `sdd_context.py check <paths>`（block 级违规退出码 2） |
| 发布承载物（版本/SHA/字节数） | `.agents/skills/dual-platform-release/release-artifacts.json` | `release_check.py` + `tests/test_release_artifacts.py` |
| 后端能力与机型差异 | `backends/*/{*_profiles.py,config_*.py}` 的 profile 表 | profile 自洽性测试 + 未验证机型纪律（AD-019） |
| 音频/录音参数 | `config.py` + `recorder.py` 常量 | 测试 + SDD/12 运行模型 |

---

## 2. 分层地图

### 2.1 代码 → 设计 → 测试 → 文档

| 代码 | 设计引用 | 主要测试 | 必须同步的文档 |
| --- | --- | --- | --- |
| `server.py`（FastAPI、5 个 WS、REST） | AD-001、SDD 9.2 / 10 / 12 | `test_server_ws_protocol.py`、`test_server_security.py`、`test_recorder_api.py` | README（WS/env 表）、AGENTS、SDD 10/12 |
| `backends/base.py` | AD-016 | `test_backend_factory.py` | AGENTS 后端表、SDD 11 |
| `backends/ft710/**`（**已验证路径**） | AD-002、AD-005、AD-014 | `test_cat_controller.py`、`test_ft710_power.py`、`test_server_scope_init.py` | AGENTS、DEPENDENCIES、SDD 9/10/15 |
| `backends/yaesu/**`（FTDX10/FTDX101D/MP/FTX-1F，未验证） | **AD-018**、**AD-019**、NFR-067 | `test_yaesu_profiles.py`、`test_yaesu_cat_core.py`、`test_yaesu_backend.py`、`test_yaesu_wiring.py`、`test_yaesu_fake_radio.py` | AGENTS、README、DEPENDENCIES、SDD 05/09/10/11/12/13/15、`tests/README.md` |
| `backends/ic7300/**`（CI-V 核心 + profile） | AD-016、AD-019 | `test_civ_*.py`、`test_unverified_tx_gate.py`、`test_model_mismatch.py` | 同上 + `_diag_civ.py` 用法 |
| `audio_handler.py` / `audio_resample.py` / `opus_rx.py` | AD-004、AD-008、AD-011 | `test_audio.py` | AGENTS、DEPENDENCIES、SDD 9.3/9.4 |
| `recorder.py`（16 kHz 存储域） | **AD-017** | `test_recorder.py`、`test_recorder_api.py` | AGENTS、README、SDD 12/13（R10）、CHANGELOG |
| `poll_scheduler.py` / `radio_state.py` | AD-003、AD-009、AD-012、AD-015 | `test_poll_scheduler.py`、`test_radio_state.py` | SDD 9.6 |
| `scope_*` / `backends/*/scope_*` | AD-005、AD-006 | `test_scope_*.py` | SDD 9.5、AGENTS |
| `atr1000_*.py` | SDD 9.8 | `test_atr1000_*.py` | SDD 9.8、README env |
| `static/**`（前端） | SDD 9.7 | `test_ws_protocol.py` 中的前端契约测试 | AGENTS（两个勿格式化文件）、缓存版本号 |
| `packaging/**` | AD-001、SDD 12 | `test_windows_packaging_files.py`、`test_rpi_packaging.py` | `mac_pack.md` / `win_pack.md` / `pi_pack.md`、安装指南 |

### 2.2 设计层

| 目录 | 内容 | 何时更新 |
| --- | --- | --- |
| `SDD/01…15` | 15 章 IBM TeamSD 设计记录 | 行为/架构变化时（见 §3 对照表）；每章对应的"何时更新"见下 |
| `SDD/08` | AD-001 … AD-019 与索引表 | 做架构决策时（新增 AD + 索引行 + 版本历史行） |
| `SDD/13` | 风险 R1…R12 与假设 A1…A9 | 出现新风险/假设或被证伪时 |
| `SDD/14` | 版本历史（SDD 版本唯一真相） | **每次**发布/设计变更 |
| `docs/superpowers/specs/**` | 规格（日期命名，设计权威） | 设计阶段；实现后追加"Implementation outcome" |
| `docs/superpowers/plans/**` | 实现计划（步骤可勾选） | 执行期同步偏差 |
| `website/sdd/**` | **生成物**（`python3 website/build_sdd.py`；含 `diagrams/` 副本） | 改了 `SDD/*.md` 或 `SDD/diagrams/*` 之后 |
| `SDD/diagrams/*.svg`（10 张） | 设计图（**图的唯一来源**） | 能力/架构变化时重画；每张带 `diagram-version` 标记 + 关键能力关键词 |
| `docs/images/ui-guide-*.svg`（2 张） | UI 图（**唯一来源**，`website/images/` 为生成副本） | UI 布局/菜单变化时更新 |

### 2.3 工程层

| 区域 | 入口 | 备注 |
| --- | --- | --- |
| 依赖 | `requirements.txt` / `DEPENDENCIES.md` | 新依赖要同时进 PyInstaller spec 的 `hiddenimports` |
| 环境变量 | `config.py`（`MRRC_*`，兼容 `FT710_*`） | README 环境表 + AGENTS + SDD 12 三处同改 |
| 测试 | `python -m unittest discover -s tests`（当前 1048 例） | 新增模块要进 `tests/README.md` |
| 打包 | `packaging/{macos,windows,rpi}/` | 见 §4 发布链 |
| 静态检查 | `~/.pi-lens/tools/node_modules/.bin/pyright`（配 `pyrightconfig.json`）+ `biome.json` | 两个前端文件禁止被格式化（见 AGENTS） |

### 2.4 Harness / 守护层

| 组件 | 位置 | 作用 |
| --- | --- | --- |
| 约束注册表 | `.agents/skills/sdd-guardian/harness/constraints.json` | AD/事件史蒸馏出的 block/watch 规则（`cat-sh-format`、`cat-pr-errata`、`cat-no-dn`、`audio-16k-rate`…） |
| 知识路由 | `.agents/skills/sdd-guardian/harness/index.json` | 文件 glob/关键词 → SDD 引用（AD/NFR/UC/R/章节），19 个主题 |
| 上下文工具 | `…/sdd_context.py` | `prime` / `brief <paths>` / `sdd <id>` / `check <paths>` / `hook` |
| 发布检查器 | `.agents/skills/dual-platform-release/harness/release_check.py` | 版本一致性 + 制品事实 + 陈旧链接 +（`--online`）发布态与标签 |
| 发布注册表 | `…/dual-platform-release/release-artifacts.json` | 承载物的机器可读清单（改代码时若新增版本承载文件，**必须加规则**） |
| 技能 | `.agents/skills/{dual-platform-release,macos-installer,windows-installer,sdd-guardian}/` | 发布/打包/守护的操作手册（含真实踩坑） |

---

## 3. "改了什么 → 必须更新什么"对照表

| 改动类型 | SDD | 其他文档 | 测试 | 发布注册表 |
| --- | --- | --- | --- | --- |
| 新增/修改电台后端 | 05（NFR）、08（AD）、09/10/11（架构/服务/组件）、12（运行）、13（风险/假设）、14 | `AGENTS.md` 后端表、README 机型表、`DEPENDENCIES.md`、`tests/README.md` | 新模块测试 + 门禁/身份校验 | 若新机型出现在 UI/文档的机型列表，加规则 |
| 新环境变量 | 12 | README 环境表、AGENTS、`DEPENDENCIES.md`（若是依赖相关） | `test_config.py` | — |
| 音频/录音参数变化 | 12、13 | README、`AGENTS.md` | `test_audio.py`、`test_recorder*.py` | — |
| 前端行为/缓存版本 | 09.7 | AGENTS（勿格式化清单） | 前端契约测试 | — |
| 新增 WebSocket 端点 | 05（鉴权 NFR）、10 | README、AGENTS | `test_server_security.py`、`test_server_ws_protocol.py` | — |
| 打包/安装流程 | 12 | `mac_pack.md`/`win_pack.md`/`pi_pack.md`、安装指南 | `test_*_packaging*.py` | 规则通常已覆盖 |
| 架构/能力变化（含图） | 对应章 + 14 | `PROJECT_MAP`、相关指南 | 相关测试 + `release_check` 图规则 | `release-artifacts.json` 的 `diagrams` 段（新图或新关键词） |
| **每次发布** | **14（新行）** | CHANGELOG、README/SDD 状态、落地页、指南 | 全套 + `release_check` | 版本/制品规则随制品变化 |

---

## 4. 发布日唯一入口（细节见 `dual-platform-release` 技能）

```bash
# 0. 门：测试 + 守护
.venv/bin/python -m unittest discover -s tests
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged

# 1. 版本：CHANGELOG 顶部 + .iss
# 2. 构建：packaging/macos/build.sh；Windows 走 win_pack.md 的 KVM VM 流程
# 3. 文档/网站同步 + 生成页
python3 website/build_sdd.py
python3 .agents/skills/dual-platform-release/harness/release_check.py      # 必须 0 failing

# 4. 部署：installers → downloads/；HTML → deploy.sh
# 5. 发布态验证（含 SHA 深检与标签）
python3 .agents/skills/dual-platform-release/harness/release_check.py --online --deep
```

---

## 5. 可执行不变量清单（当前被测试强制）

| 不变量 | 强制者 |
| --- | --- |
| README Quick Facts == SDD/14 首行 == 生成器 == 落地页 == 首页徽章 | `tests/test_sdd_docs_consistency.py` |
| 落地页 AD 索引包含 `SDD/08` 的每一条 AD | 同上 |
| 应用版本在 `.iss`/网站卡片/指南/文档中一致，且旧下载链接不残留 | `tests/test_release_artifacts.py` + `release_check.py` |
| 每张设计图带版本标记（≤2 版滞后）且含其描绘能力的关键词；`website/` 下的图副本与来源逐字节一致 | `release_check.py` 的 `diagrams` 规则 + `tests/test_release_artifacts.py` |
| 卡片字节数/SHA == 实际构建产物 | `release_check.py`（有构建时） |
| `SH00NN` 滤波格式 + 回读、`PR00/PR01` 压缩机、不查询 `DN;` | 守护 `constraints.json`（block 级） |
| 未验证机型：`verified=false` + TX 门禁 + 逐表溯源 + 只读身份校验 | profile/后端测试 + NFR-067 + AD-019 |
| 前端两个文件不被格式化工具改写 | `biome.json` overrides + `.pi-lens.json` ignore |
| 每个注册后端都具备服务器读取的全部属性（如 `cat`） | `tests/test_yaesu_wiring.py::ServerVisibleSurfaceTests` |
