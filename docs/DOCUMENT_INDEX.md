# FT-710 Web Control — 完整文档索引

**最后更新**: 2026-07-25  
**文档总数**: 18 份核心 + 10 份 iOS  
**版本**: v2.2.0

---

## 📚 文档导航

### 🚀 快速开始
| 文档 | 说明 | 读者 |
|------|------|------|
| [README.md](README.md) | 项目概述、架构、快速启动 | 所有人 |
| [QUICKSTART.md](QUICKSTART.md) | 5 分钟上手指南 | 新用户 |
| [DEPENDENCIES.md](DEPENDENCIES.md) | 跨平台依赖安装 | 开发者 |

### 🔒 安全配置
| 文档 | 说明 | 读者 |
|------|------|------|
| [SECURITY_GUIDE.md](SECURITY_GUIDE.md) | 安全配置、密码策略、速率限制 | 运维/安全 |

### 📊 项目状态
| 文档 | 说明 | 读者 |
|------|------|------|
| [EXECUTIVE_SUMMARY.md](EXECUTIVE_SUMMARY.md) | 执行摘要（中文） | 管理层 |
| [COMPLETION_REPORT.md](COMPLETION_REPORT.md) | 完成报告（中文） | 项目经理 |
| [FINAL_VERIFICATION.md](FINAL_VERIFICATION.md) | 验证报告 | QA/测试 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更历史 | 所有人 |

### 🔧 技术细节
| 文档 | 说明 | 读者 |
|------|------|------|
| [FIXES_SUMMARY.md](FIXES_SUMMARY.md) | 修复详细说明（含 TX 分析） | 开发者 |
| [FT-710_CAT_Knowledge_Base.md](FT-710_CAT_Knowledge_Base.md) | CAT 命令参考 | 开发者 |
| [AGENTS.md](AGENTS.md) | Agnes 代理配置 | 开发者 |
| [win_pack.md](win_pack.md) | Windows 安装包打包手册（ham.vlsc.net KVM 虚拟机全流程） | 开发者/发布 |

### 🔍 专项分析
| 文档 | 说明 | 读者 |
|------|------|------|
| [docs/TX_LINK_ANALYSIS.md](docs/TX_LINK_ANALYSIS.md) | TX 音频链路深度分析 | 架构师/高级开发 |
| [docs/DOCUMENT_UPDATE_SUMMARY.md](docs/DOCUMENT_UPDATE_SUMMARY.md) | 本次文档更新总结 | 文档维护者 |

### 📱 iOS App (FT710Mobile/)
| 文档 | 说明 | 读者 |
|------|------|------|
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

### 📐 设计规范
| 文档 | 说明 | 读者 |
|------|------|------|
| [SDD/](SDD/) | 软件设计说明（15 章） | 架构师/开发 |

---

## 📖 文档详细说明

### README.md (16KB)
**位置**: 根目录  
**内容**:
- 项目简介与功能特性
- 系统要求（Python 3.10+, Node.js 20+）
- 快速启动步骤
- 架构概览（前端/后端/电台）
- 配置选项与环境变量
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
- 串口驱动安装（FTDI/CP210x）
- PyAudio 配置
- libopus 安装
- FT4222 驱动
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
|------|------|--------|
| 核心文档 | 7 | ~73KB |
| 技术文档 | 4 | ~26KB |
| 分析文档 | 4 | ~25KB |
| SDD 文档 | 15+ | ~100KB+ |
| **总计** | **30+** | **~225KB+** |

**中文文档**: 3 份（EXECUTIVE_SUMMARY.md, COMPLETION_REPORT.md, FT710_Web_Control_介绍文章.md）  
**英文文档**: 25+ 份

---

## 🔗 内部链接

所有文档都包含指向相关文档的链接，形成完整的知识网络：

```
README.md
├──→ QUICKSTART.md
├──→ SECURITY_GUIDE.md
├──→ DEPENDENCIES.md
├──→ FIXES_SUMMARY.md
│   └──→ docs/TX_LINK_ANALYSIS.md
├──→ FINAL_VERIFICATION.md
│   └──→ docs/TX_LINK_ANALYSIS.md
├──→ EXECUTIVE_SUMMARY.md
│   └──→ docs/TX_LINK_ANALYSIS.md
├──→ COMPLETION_REPORT.md
│   └──→ docs/TX_LINK_ANALYSIS.md
├──→ CHANGELOG.md
└──→ SDD/
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

**索引生成时间**: 2026-07-14 08:26  
**维护者**: Agnes Code Review  
**下次审查**: 2026-08-14
