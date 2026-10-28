# MRRC Web Control — 完整文档索引

**最后更新**: 2026-10-13（登记 FT8 四平台打包 spec；此前同日登记快路径 + 自适应级联的 spec 与 plan 及其 A53 spike 结论）  
**版本权威**: `CHANGELOG.md` 首条 —— 本索引**不再复述版本号**：
复述过的那一行曾停在 v1.16.0，跨了十来个发布而无人察觉。  
**入口**: 想知道「改了这个要同步哪些文档」→ 直接看 [PROJECT_MAP.md](PROJECT_MAP.md)

---

## 📚 文档导航

> 机器可校验的版本/尺寸/SHA 一致性由 `.agents/skills/dual-platform-release/harness/release_check.py`
> 检查（`tests/test_release_artifacts.py` 在套件内强制执行）；本索引只负责「哪份文档讲什么」。

### 🧭 从这里开始

| 文档 | 说明 | 读者 |
| ------ | ------ | ------ |
| [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md) | **改动 → 必更文档** 的映射、图层与发布日命令链 | 所有人 |
| [AGENTS.md](AGENTS.md) | 仓库结构、模块表、构建/测试、约定（面向编码代理） | 开发者/代理 |
| [.agents/skills/](.agents/skills/) | 工程护栏与流程技能：`sdd-guardian`（SDD 约束/文档同步）、`dual-platform-release`（发布日全流程 + 产物登记表）、`macos-installer`、`windows-installer` | 开发者/发布 |

### 🚀 快速开始

| 文档 | 说明 | 读者 |
| ------ | ------ | ------ |
| [README.md](README.md) | 项目概述、架构、快速启动 | 所有人 |
| [QUICKSTART.md](QUICKSTART.md) | 5 分钟上手指南 | 新用户 |
| [DEPENDENCIES.md](DEPENDENCIES.md) | 跨平台依赖安装 | 开发者 |

### 🔒 安全配置

| 文档 | 说明 | 读者 |
|------|------|------|
| [SECURITY_GUIDE.md](SECURITY_GUIDE.md) | 安全配置、密码策略、速率限制 | 运维/安全 |

### 📊 项目状态

| 文档 | 说明 | 读者 |
| ------ | ------ | ------ |
| [docs/NEXT-VERSION.md](docs/NEXT-VERSION.md) | **下一版 backlog**：已想清楚、只差做；每条都带验收标准 | 所有人 |
| [EXECUTIVE_SUMMARY.md](EXECUTIVE_SUMMARY.md) | 执行摘要（中文） | 管理层 |
| [COMPLETION_REPORT.md](COMPLETION_REPORT.md) | 完成报告（中文） | 项目经理 |
| [FINAL_VERIFICATION.md](FINAL_VERIFICATION.md) | 验证报告 | QA/测试 |
| [docs/FDE.md](docs/FDE.md) | **Forward Deployed Engineering**：Echo → Delta → Product 的方法论与案例 | 所有人 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更历史 | 所有人 |

### 👤 使用手册（按平台）

| 文档 | 说明 | 读者 |
| ------ | ------ | ------ |
| [docs/remote_help.md](docs/remote_help.md) | **远程协助（Call help）操作手册**：客户主动开启的应用内 SSH | 运维 |
| [docs/OPERATION_GUIDE.md](docs/OPERATION_GUIDE.md) | 桌面版完整操作指南（每个按钮/滑杆/菜单 + 服务端录音 + 故障排查） | 用户 |
| [docs/RASPBERRY_PI_GUIDE.md](docs/RASPBERRY_PI_GUIDE.md) | 树莓派镜像：烧卡、首启、电台接入、真 FFT 库 | 用户 |
| [docs/WINDOWS_INSTALLER_GUIDE.md](docs/WINDOWS_INSTALLER_GUIDE.md) | Windows 安装包说明（含下载表与校验） | 用户 |
| [docs/MACOS_INSTALLER_GUIDE.md](docs/MACOS_INSTALLER_GUIDE.md) | macOS 安装与使用指南：两种 Gatekeeper 弹窗、麦克风权限（RX 无声）、数据位置、升级/卸载、故障排查表 | 用户 |
| [docs/W103D_USER_GUIDE.md](docs/W103D_USER_GUIDE.md) | 发货版 W103D 用户指南：出厂口令、Wi-Fi、接电台、五分钟验收与排障 | 用户 |
| [docs/W103D_REMOTE_GUIDE.md](docs/W103D_REMOTE_GUIDE.md) | 蓝牙遥控器指南：配对（pty + agent）、按键表、kHz 输入、排障 | 用户 |
| [docs/FTX1_USER_GUIDE.md](docs/FTX1_USER_GUIDE.md) | **Yaesu FTX-1F 接入说明（用户向）**：哪一条是电台本身的限制（CAT 没有扫频命令 → 无真瀑布）、哪一条是我们的发射门禁（怎么开）、接机五分钟、已知「点了没反应」的开关清单（待真机确认）、频率被拉回的两种原因与本版修复后的日志形状、表头/音频注意事项、报问题要交什么；站点页 `website/ftx1.html`，技术事实见 [docs/ftx1-technical-analysis.md](docs/ftx1-technical-analysis.md) | 用户 |
| [docs/W103D_FAQ.md](docs/W103D_FAQ.md) | **W103D 常见问题：一句话「连不上门户」**——「看到什么 → 先看哪一条」判据表 + 盒子本机五条定位命令 + 九个真因条目（无 RTC 电池导致 `certificate is not yet valid`、env 属主导致“你还没申请”、一个呼号一条活隧道的换机顺序）；站点页 `website/w103d-faq.html`（`website/w103d-index.html` 专题内第七份） | 用户/运维 |

### 🔧 技术细节

| 文档 | 说明 | 读者 |
| ------ | ------ | ------ |
| [DEPENDENCIES.md](DEPENDENCIES.md) | 跨平台依赖与各机型 USB/驱动要求（含 Yaesu SDR 四款） | 开发者 |
| [FIXES_SUMMARY.md](FIXES_SUMMARY.md) | 历史修复说明（含 TX 分析） | 开发者 |
| [FT-710_CAT_Knowledge_Base.md](FT-710_CAT_Knowledge_Base.md) / [FT-710_CAT.md](FT-710_CAT.md) | FT-710 CAT 命令参考 | 开发者 |
| [IC-7300MK2_CI-V_Knowledge_Base.md](IC-7300MK2_CI-V_Knowledge_Base.md) / [IC-7300_硬件验收清单.md](IC-7300_硬件验收清单.md) | Icom CI-V 参考与真机验收清单 | 开发者 |
| [backends/ft710/](backends/ft710/) | FT-710 后端（CAT、FT4222 频谱、44.1kHz 音频） | 开发者 |
| [backends/ic7300/](backends/ic7300/) | Icom 共享 CI-V 核心 + profile（IC-7300/MK2/705/7610/7760） | 开发者 |
| [backends/yaesu/](backends/yaesu/) | Yaesu 共享 ASCII-CAT 核心 + profile（FTDX10/101D/101MP/FTX-1F/FT-891） | 开发者 |

### 🚢 发布与打包

| 文档 | 说明 | 读者 |
| ------ | ------ | ------ |
| [docs/rpi_pack.md](docs/rpi_pack.md) | **树莓派镜像构建手册**（与 W103D 共用服务层） | 发布 |
| [.agents/skills/dual-platform-release/SKILL.md](.agents/skills/dual-platform-release/SKILL.md) | 发布日总流程（版本 bump → 构建 → 文档/站点 → 部署 → 验证 → tag） | 发布 |
| [.agents/skills/dual-platform-release/release-artifacts.json](.agents/skills/dual-platform-release/release-artifacts.json) | 产物登记表：每个承载版本/尺寸/SHA 的文件一条规则 | 发布 |
| [win_pack.md](win_pack.md) / [mac_pack.md](mac_pack.md) / [pi_pack.md](pi_pack.md) | 三平台打包操作手册（KVM VM / 本机 DMG / Docker pi-gen） | 发布 |
| [docs/W103D_GUIDE.md](docs/W103D_GUIDE.md) / [docs/w103d_pack.md](docs/w103d_pack.md) / [docs/host-selection.md](docs/host-selection.md) / [docs/hardware-matrix.md](docs/hardware-matrix.md) | **W103D 通用电台盒子镜像**（Amlogic 云电脑，11 机型可切换）：**用户手册**（开箱到干活：§4.1 烧盘后扩盘 / §7 口令一览 / §9.8 局域网配网页 `/setup`+`/settings` / §20.5 无线真机记录）、构建手册、主机选型。1.25.5 已上线；1.26.0 的站点卡片已写好（`1,008,925,741 B` / `9913752e…`），**镜像本体待上传**。真机操作另见技能 `w103d-box`（构建与烧盘）/ `w103d-emmc`（固化进 eMMC） | 发布/用户 |

### 🔍 专项分析

| 文档 | 说明 | 读者 |
|------|------|------|
| [docs/TX_LINK_ANALYSIS.md](docs/TX_LINK_ANALYSIS.md) | TX 音频链路深度分析 | 架构师/高级开发 |
| [docs/nano_vna.md](docs/nano_vna.md) | **NanoVNA 天线诊断台 `/Nano` 操作手册**：判定卡与修剪建议、标定（OPEN/SHORT/LOAD 命令行步骤与验证）、野外独立使用（一键配置 + SAVE 槽 4）、基准配置对比、固件升级（三种进 DFU 的方式 / 变体看标签 / 刷后重做标定）、排障表 | 操作员 |
| [docs/squelch_guide.md](docs/squelch_guide.md) | **静噪（SQL）使用与排障**：「静噪门槛」与「静噪接在哪路音频上」的分界、IC-7300 的 `ACC/USB AF SQL`（出厂 OFF，USB 音频不受静噪控制）、FT-710 的 `RF/SQL VR`（出厂 RF）、三步验证与症状速查表 | 操作员 |
| [docs/sdr_receiver.md](docs/sdr_receiver.md) | 独立 SDR 接收机（AD-029）：设备、信道化、容量与页面 | 开发者 |
| [docs/ft8-m1-baseline.md](docs/ft8-m1-baseline.md) | FT8 M1 基线：W103D 上的实测数字 | 开发者 |
| [docs/ft8-m5-acceptance.md](docs/ft8-m5-acceptance.md) | FT8 M5 验收记录（真机 W103D） | QA/开发者 |
| [docs/ft8-w103d-decode-perf-analysis.md](docs/ft8-w103d-decode-perf-analysis.md) | FT8 解码性能分析（A53 实测与快/深路径取舍） | 架构师 |
| [docs/ft8-w103d-phase-a-baseline.md](docs/ft8-w103d-phase-a-baseline.md) | FT8 Phase A 基线测量 | 开发者 |
| [docs/ft8-deep-learning-research-brief.md](docs/ft8-deep-learning-research-brief.md) | FT8 深度学习研究简报 | 研究/架构师 |
| [docs/ft8-dl-speculative-notes.md](docs/ft8-dl-speculative-notes.md) | FT8 深度学习推测性笔记 | 研究 |
| [docs/ft8-dl-technical-review-packet.md](docs/ft8-dl-technical-review-packet.md) | FT8 深度学习技术评审包 | 架构师 |
| [docs/2026-10-03-two-model-retrospective.md](docs/2026-10-03-two-model-retrospective.md) | 两款机型接入的复盘 | 所有人 |
| [docs/2026-10-11-cloud-hub-instance-lifecycle-findings.md](docs/2026-10-11-cloud-hub-instance-lifecycle-findings.md) | Cloud Hub 实例生命周期实况发现 | 运维/架构师 |
| [docs/w152d-feasibility.md](docs/w152d-feasibility.md) | **中兴 W152D 能不能刷成我们的盒子** —— 结论：不能直接刷（S905L4 是 GXL 家族，`glxl4` 无可用 dtb；我们的镜像是 S905L3A/G12A + W103D dtb，`MODEL_ID=307`），且部分批次有**高安锁**；附「安卓当后端」为什么不划算（`/dev/ttyUSB*`、ALSA、systemd 全拿不到，等于第二套产品）、新板型适配的四步与代价、下单前必须问清的六件事 | 架构师/采购 |
| [docs/ftx1-connectivity-analysis.md](docs/ftx1-connectivity-analysis.md) | **FTX-1 的连接面：除了 USB/CAT 还能接什么** —— 先纠正前提（这台机器没有网口，SCU-LAN10 支持列表不含它、选装件里只有 BU-6 蓝牙）；然后逐条拆 **CAT-2 硬件线**（RPTT/PC KEYING → 硬件 PTT 与真 CW）、**后背 10-pin 的 CAT-3**（唯一电气独立的第二条控制通道，与 EXT-TUNER/LINEAR/GPO 互斥）、**microSD 电台侧录音**、**BU-6 蓝牙**；一张「功能 × 路径」矩阵（含哪些任何线都拿不到：真频谱、双收分路音频、SD 读写）；四项按性价比排序的工程建议（`RI`+`TX=2`、硬件 PTT、`LM`/`PB` 语音键控、CAT-3 后备）与真机确认清单 | 架构师/高级开发 |
| [docs/ftx1-technical-analysis.md](docs/ftx1-technical-analysis.md) | **FTX-1 深度技术分析 + CAT 一致性对照**（依据 Yaesu 官方 CAT 手册 + 第三方真机实现）：六个 CPU 的拓扑、三条串口、AI 只推一半、双接收的通道位陷阱、`SS` 能设不能读、`EX` 是 5 段寻址的菜单寄存器表；**附录 C 把本仓发出的 38 条命令逐条对照手册（20 项不符，含 6 条命令 FTX-1 没有）**，第十二节按五类给出修法与“哪些只能上机确认” | 架构师/高级开发 |
| [docs/DOCUMENT_UPDATE_SUMMARY.md](docs/DOCUMENT_UPDATE_SUMMARY.md) / [docs/DOCUMENT_UPDATE_COMPLETION.md](docs/DOCUMENT_UPDATE_COMPLETION.md) | **历史报告（2026-08-17，多电台后端那轮）** —— 不是当前状态，现状看 [PROJECT_MAP.md](PROJECT_MAP.md) | 文档维护者 |

### 📱 iOS App (FT710Mobile/)

| 文档 | 说明 | 读者 |
| ------ | ------ | ------ |
| [FT710Mobile/README.md](FT710Mobile/README.md) | iOS app 简介与构建运行 | 所有人 |
| [FT710Mobile/CLAUDE.md](FT710Mobile/CLAUDE.md) | iOS 工程指南(面向编码代理) | 开发者/代理 |
| [FT710Mobile/docs/ARCHITECTURE.md](FT710Mobile/docs/ARCHITECTURE.md) | iOS 架构设计 | 架构师/高级开发 |
| [docs/IOS_APP_SUMMARY.md](docs/IOS_APP_SUMMARY.md) | iOS 现状总结(2026-07-20 基准) | 所有人 |
| [docs/IOS_APP_ANALYSIS.md](docs/IOS_APP_ANALYSIS.md) | iOS 深度审计(P0-P2 问题清单) | 开发者 |
| [docs/IOS_APP_FIX_GUIDE.md](docs/IOS_APP_FIX_GUIDE.md) | iOS 修复路线图 | 开发者 |
| [docs/IOS_BUILD_GUIDE.md](docs/IOS_BUILD_GUIDE.md) | iOS 构建指南(真机) | 开发者 |
| [docs/IOS_FIXES_PROGRESS.md](docs/IOS_FIXES_PROGRESS.md) | iOS 修复进度核实 | 开发者 |
| [docs/IOS_OPUS_INTEGRATION.md](docs/IOS_OPUS_INTEGRATION.md) | iOS Opus 现状与 TX 启用指南 | 开发者 |
| [docs/IOS_TESTING_GUIDE.md](docs/IOS_TESTING_GUIDE.md) | iOS 测试指南 | 开发者/QA |

### 🤖 Android App (FT710Android/)

| 文档 | 说明 | 读者 |
| ------ | ------ | ------ |
| [FT710Android/CLAUDE.md](FT710Android/CLAUDE.md) | 协议事实、PTT 安全铁律、构建与测试 | 开发者/代理 |
| [FT710Android/BUILD_GUIDE.md](FT710Android/BUILD_GUIDE.md) | 工具链与构建步骤 | 开发者 |
| [docs/superpowers/specs/2026-08-16-ft710-android-app-design.md](docs/superpowers/specs/2026-08-16-ft710-android-app-design.md) | Android 设计 spec | 架构师 |

### 📐 设计规范

| 文档 | 说明 | 读者 |
| ------ | ------ | ------ |
| [SDD/](SDD/) | 软件设计说明（15 章） | 架构师/开发 |
| [docs/superpowers/specs/2026-10-13-ft8-fast-path-cascade-design.md](docs/superpowers/specs/2026-10-13-ft8-fast-path-cascade-design.md) | **FT8 快路径 + 自适应级联**设计 spec（默认走 `ft8_lib` 快路径、按需补跑深路径并取并集；含 2026-10-13 A53 spike 回填） | 架构师 |
| [docs/superpowers/plans/2026-10-13-ft8-fast-path-cascade.md](docs/superpowers/plans/2026-10-13-ft8-fast-path-cascade.md) | 上述 spec 的实现计划（15 个 Task） | 开发者 |
| [docs/superpowers/specs/2026-10-13-ft8-packaging-design.md](docs/superpowers/specs/2026-10-13-ft8-packaging-design.md) | **FT8 四平台打包**设计 spec（镜像内编译 DSP 并随 `mrrc-update` 重编、Boost 头文件 vendor 化、Windows MinGW 与 macOS dylib 依赖闭包进包） | 架构师/打包 |
| [docs/superpowers/specs/2026-10-18-nano-vna-design.md](docs/superpowers/specs/2026-10-18-nano-vna-design.md) | **NanoVNA-H 天线诊断台**设计 spec（`/Nano`：`scan_bin` 二进制帧与帧长自校验、四道守卫（发射中拒绝/一次一件/不抢 CAT 口/角色隔离）、扫描后还原设备屏幕、设备管理与 U3 固件升级（ROM DFU + 版本回读 + 变体人选）） | 架构师/固件 |
| [docs/superpowers/plans/2026-10-18-nano-vna.md](docs/superpowers/plans/2026-10-18-nano-vna.md) | **NanoVNA 第一期**实现计划（纯数学层 → 协议层 → 服务端接线 → 页面 → 文档同步，逐任务带测试） | 开发者 |
| [docs/superpowers/specs/2026-10-17-w103d-preflash-design.md](docs/superpowers/specs/2026-10-17-w103d-preflash-design.md) | **W103D 发货版（预置）镜像**设计 spec（`--preflash` 开关：固定三条口令 + 时区/locale 预置 + 设置页手输 SSID 与明文密码 + 设置页填呼号向 portal 申请公网入口 + 首启探测改成“跑到定型为止”） | 架构师/打包 |
| [docs/superpowers/plans/2026-10-17-w103d-preflash.md](docs/superpowers/plans/2026-10-17-w103d-preflash.md) | 上述 spec 的实现计划（9 个任务：预置脚本与开关 · 设置页两面板 · 探测自愈 · 文档与产物验证） | 开发者 |

---

## 📖 文档详细说明

### README.md (16KB)

**位置**: 根目录  
**内容**:

- 项目简介与功能特性（FT-710 / IC-7300 / IC-7300MK2 多电台）
- 系统要求（Python 3.10+）
- 快速启动步骤（含后端选择）
- 架构概览（前端/后端/电台后端插件）
- 配置选项与环境变量（含 `MRRC_RADIO_MODEL`、`IC7300_CIV_ADDR`）
- 安全最佳实践
- 故障排除
- 完整文档链接

**适用场景**: 新项目入门、架构理解

---

### QUICKSTART.md (2.9KB)

**位置**: 根目录  
**内容**:

- 5 分钟快速启动指南
- 环境准备清单
- 一键启动命令
- 基本使用步骤
- 常见问题解答

**适用场景**: 快速体验、演示准备

---

### DEPENDENCIES.md (28KB)

**位置**: 根目录  
**内容**:

- Python 版本要求（3.10+）
- 操作系统支持（Windows/macOS/Linux）
- 串口驱动安装（FTDI/CP210x / Icom USB CI-V）
- PyAudio 配置
- libopus 安装
- FT4222 驱动（FT-710 可选）
- 虚拟环境创建
- 依赖安装命令

**适用场景**: 环境搭建、依赖排查

---

### SECURITY_GUIDE.md (3.3KB)

**位置**: 根目录  
**内容**:

- 认证机制（Token 验证）
- 密码策略（强度要求、默认密码）
- 速率限制（5 次/5 分钟/IP）
- HTTPS/SSL 配置
- 网络隔离建议
- 安全审计清单
- 入侵检测

**适用场景**: 安全配置、合规检查

---

### EXECUTIVE_SUMMARY.md (3.6KB)

**位置**: 根目录  
**内容**（中文）:

- 项目目标达成状态
- 核心成果（技术修复、安全增强、稳定性提升）
- 测试验证结果（206/206 通过）
- TX 链路分析摘要
- 交付文档清单
- 下一步行动

**适用场景**: 项目汇报、决策支持

---

### COMPLETION_REPORT.md (3.8KB)

**位置**: 根目录  
**内容**（中文）:

- 项目目标达成状态
- 完成指标（代码质量、安全加固、性能优化、稳定性修复）
- 交付物清单（源代码修复 8 个文件，文档 10 个文件）
- 验证结果（测试通过率、代码审查）
- TX 链路分析状态
- 部署建议

**适用场景**: 项目验收、结题报告

---

### FINAL_VERIFICATION.md (3.9KB)

**位置**: 根目录  
**内容**:

- 测试套件验证（206/206 通过）
- 代码质量检查
- 安全功能验证
- 性能优化效果
- 文档完整性检查
- 部署清单
- TX 链路分析状态

**适用场景**: 上线前验证、QA 检查

---

### CHANGELOG.md (4.1KB)

**位置**: 根目录  
**内容**:

- v2.1.0 — TX 链路分析完成
- v2.0.0 — 稳定性与安全加固
  - 安全增强（速率限制、强密码、健康检查）
  - 关键修复（竞态条件、Python 兼容性）
  - 性能优化（同步速度、日志降噪）
  - 代码质量（调试清理、文档修正）
  - 测试验证（206/206 通过）
  - 文档更新（10 份文档）
- v1.2.0 — 主要功能
- v1.0.0 — 初始版本

**适用场景**: 版本追踪、变更影响评估

---

### FIXES_SUMMARY.md (9.1KB)

**位置**: 根目录  
**内容**:

- v2.0.0 关键修复（3 个）
  - Python 3.10+ 兼容性
  - `_cancel_polls` 竞态条件
  - 重复 `rf_gain` 处理器
- 中等优先级修复（5 个）
  - 默认密码安全
  - 登录速率限制
  - 调试制品清理
  - Opus 文档修正
- 性能优化（4 个）
  - 初始同步速度
  - 日志噪音减少
  - 类级状态清理
  - 缺失导入修复
- 代码质量改进（3 个）
  - 健康检查端点
  - 启动时间跟踪
  - 测试兼容性
- **TX 链路分析**（新增）
  - 高风险问题（2 个）
  - 中风险问题（3 个）
  - 低风险问题（3 个）
  - 修复优先级与建议

**适用场景**: 修复追溯、技术问题定位

---

### FT-710_CAT_Knowledge_Base.md (15KB)

**位置**: 根目录  
**内容**:

- CAT 协议基础
- 命令格式说明
- 常用命令参考
- 频率/模式控制
- PTT 控制
- 仪表读取
- 内存频道
- 错误处理

**适用场景**: CAT 开发、协议调试

---

### AGENTS.md (3.7KB)

**位置**: 根目录  
**内容**:

- Agnes 代理配置
- 技能定义
- 工具集成
- 工作流自动化

**适用场景**: 自动化配置、代理开发

---

### docs/TX_LINK_ANALYSIS.md (7.6KB)

**位置**: `docs/` 目录  
**内容**:

- TX 链路架构概览
- 问题识别（8 个）
  - 高风险：PTT 控制路径不一致、TX 仪表轮询条件错误
  - 中风险：AudioWorklet SAB 路径、TX Opus 可用性、未使用 TxJitterBuffer
  - 低风险：前端 Opus 版本、renderUpdates 重复、RadioState 日志
- 测试验证状态
- 修复优先级
- 代码质量指标
- 下一步行动

**适用场景**: TX 链路开发、问题修复指导

---

### docs/DOCUMENT_UPDATE_SUMMARY.md (4.6KB)

**位置**: `docs/` 目录  
**内容**:

- 更新目的与范围
- 新增文档列表
- 更新文档详情
- 文档体系总览
- 更新统计
- 关键信息传递
- 文档一致性检查
- 下一步计划

**适用场景**: 文档维护、版本管理

---

### SDD/ (15 章)

**位置**: `SDD/` 目录  
**内容**:

1. 执行摘要
2. 业务方向
3. 项目定义
4. 系统上下文
5. 非功能性需求
6. 用例模型
7. 主题区域模型
8. 架构决策（10 个）
9. 架构概览
10. 服务模型
11. 组件模型
12. 运营模型
13. 可行性评估
14. 版本历史
15. PTT 安全架构

**适用场景**: 架构设计、技术规范

---

## 🎯 按角色推荐文档

### 新用户/体验用户

1. README.md
2. QUICKSTART.md
3. SECURITY_GUIDE.md

### 开发者

1. README.md
2. DEPENDENCIES.md
3. FIXES_SUMMARY.md
4. FT-710_CAT_Knowledge_Base.md
5. docs/TX_LINK_ANALYSIS.md

### 运维/安全

1. SECURITY_GUIDE.md
2. FINAL_VERIFICATION.md
3. CHANGELOG.md

### 项目经理

1. EXECUTIVE_SUMMARY.md
2. COMPLETION_REPORT.md
3. FINAL_VERIFICATION.md
4. CHANGELOG.md

### 架构师

1. README.md（架构部分）
2. SDD/（全部）
3. docs/TX_LINK_ANALYSIS.md
4. FIXES_SUMMARY.md

### QA/测试

1. FINAL_VERIFICATION.md
2. FIXES_SUMMARY.md
3. docs/TX_LINK_ANALYSIS.md（测试部分）

---

## 📈 文档统计

| 类别 | 数量 | 总大小 |
| ------ | ------ | -------- |
| 核心文档 | 7 | ~75KB |
| 技术文档 | 6 | ~28KB |
| 分析文档 | 4 | ~25KB |
| SDD 文档 | 15+ | ~100KB+ |
| **总计** | **32+** | **~230KB+** |

**中文文档**: 3 份（EXECUTIVE_SUMMARY.md, COMPLETION_REPORT.md, FT710_Web_Control_介绍文章.md）  
**英文文档**: 27+ 份

---

## 🔗 内部链接

所有文档都包含指向相关文档的链接，形成完整的知识网络：

```
README.md
├──→ QUICKSTART.md
├──→ SECURITY_GUIDE.md
├──→ DEPENDENCIES.md
├──→ docs/OPERATION_GUIDE.md
├──→ docs/MACOS_INSTALLER_GUIDE.md
├──→ docs/WINDOWS_INSTALLER_GUIDE.md
├──→ FIXES_SUMMARY.md
│   └──→ docs/TX_LINK_ANALYSIS.md
├──→ FINAL_VERIFICATION.md
│   └──→ docs/TX_LINK_ANALYSIS.md
├──→ EXECUTIVE_SUMMARY.md
│   └──→ docs/TX_LINK_ANALYSIS.md
├──→ COMPLETION_REPORT.md
│   └──→ docs/TX_LINK_ANALYSIS.md
├──→ CHANGELOG.md
├──→ SDD/
└──→ backends/ (ft710 / ic7300 / ic7300mk2)
```

---

## 📝 文档维护指南

### 更新频率

- **CHANGELOG.md**: 每个版本发布时
- **FIXES_SUMMARY.md**: 每次重要修复后
- **FINAL_VERIFICATION.md**: 每次验证后
- **EXECUTIVE_SUMMARY.md**: 每季度或重大变更后
- **COMPLETION_REPORT.md**: 项目里程碑时
- **docs/TX_LINK_ANALYSIS.md**: TX 链路重大变更后

### 更新流程

1. 修改代码/功能
2. 更新相关技术文档
3. 运行测试验证
4. 更新 CHANGELOG.md
5. 更新 FIXES_SUMMARY.md
6. 更新 FINAL_VERIFICATION.md
7. （可选）更新 EXECUTIVE_SUMMARY.md / COMPLETION_REPORT.md
8. 提交 PR

### 文档质量标准

- ✅ 准确反映当前代码状态
- ✅ 包含必要的代码示例
- ✅ 提供清晰的步骤说明
- ✅ 包含故障排除指南
- ✅ 使用统一的格式和术语
- ✅ 包含内部交叉引用
- ✅ 定期审查和更新

---

**索引生成时间**: 2026-08-17  
**维护者**: Agnes Code Review  
**下次审查**: 2026-09-17

## Cloud Hub 相关（2026-09-30 新增）

| 文档 | 内容 |
| ------ | ------ |
| `../SDD/15-ptt-safety-architecture.md` | PTT 半开与 Hub 模式安全（活性闸门） |
| `../SDD/08-architecture-decisions.md` | AD-023（会话遥测）、AD-024（令牌传输） |
| `../SDD/12-operational-model.md` §12.9 | Hub 模式运维要点（前缀/心跳/遥测/限流退化） |
| `../SECURITY_GUIDE.md` §Cloud Hub 模式 | 令牌、上游校验、PTT、已知限制 |
| `../tests/README.md` §本轮新增 | 四项前置能力的守卫测试 |
| `../../mrrc_hub/SDD/` | Hub 端设计记录（15 章）；实况见其 §12.8 |
| `../../mrrc_hub/docs/2026-09-30-fleet-hub-design-review.md` | 现状取证评审（四项 P0） |
