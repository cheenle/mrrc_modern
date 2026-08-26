# SDD-Guardian 使用指南

把 `SDD/`(15 章设计文档)变成工程全流程的活护栏:需求/上下文/架构决策/可行性随时可查,约束违规实时拦截。

## 加载方式(下次开工什么都不用做)

| 机制 | 状态 | 生效时机 |
|------|------|----------|
| 项目级 skill(`.agents/skills/sdd-guardian/`) | 已随仓库提交 | 新 session 自动扫描发现;涉及编码任务时 agent 自动调用 |
| Superpowers 工作流技能(17 个,`~/.codex/skills/`) | 已通过 `extra_skill_dirs` 挂入 `~/.kimi-code/config.toml` | 全部 session 默认可调用(brainstorming、TDD、systematic-debugging、writing-plans 等驱动工作流;sdd-guardian 管设计约束,两者互补) |
| SessionStart hook | 已装入 `~/.kimi-code/config.toml` | 每次开会话自动注入黄金规则摘要 |
| PreToolUse hook | 已装入 `~/.kimi-code/config.toml` | 每次 Edit/Write 前自动检查,阻断级违规直接拒绝 |

手动调用(通常不需要):`/skill:sdd-guardian [任务描述]`,superpowers 技能同理(如 `/skill:brainstorming`)。

## 日常命令(harness CLI)

```bash
H=.agents/skills/sdd-guardian/harness/sdd_context.py

python3 $H brief <文件>          # 动手前:该文件的完整工程简报
                                 # (约束 + AD 全文 + NFR + 用例 + 风险/开放问题,实时切片自 SDD)
python3 $H brief --task "任务"    # 按主题拉简报(支持中文关键词)
python3 $H sdd AD-011            # 单条查询:AD-xxx / NFR-xxx / UC-xxx / Rn / In / SCn / An / 9.6 / 关键词
python3 $H trace <spec/plan.md>  # superpowers spec/plan 的 SDD 引用审计(提示缺失引用,不阻断)
python3 $H context <文件>        # 快速视图:只看约束,不看 SDD 正文
python3 $H check --staged        # 提交前:扫暂存区,exit 2 = 有阻断违规
python3 $H check <文件>          # 扫指定文件
python3 $H prime                 # 黄金规则摘要(hook 每次会话自动跑)
```

典型节奏:`brief` → 写代码(hook 自动拦截)→ `unittest` → `check --staged` → 文档同步 → 提交。

## 规则分层

- **阻断(8 条)**:DN; 禁发、PR00/01 映射、AC000/001/003、SH00NN 格式、串口 I/O 只在 CatController、禁 16kHz 音频、index.html 禁内联 JS、禁硬编码密钥
- **警告(6 条)**:状态必须 `radio.update()`、新 WS 端点要 token 鉴权、PyAudio 设备采样率来自后端能力表(AD-011 修订)、部署值走环境变量、静态文件路径必须包含在 STATIC_DIR 内(I8)、密码比较用 hmac.compare_digest(I9)
- **指导(7 条)**:TX0 不加验证循环、轮询查后复查 skip、PTT 走优先级通道、音频/频谱子通道需独立重连(I10)、iOS PTT 必须无条件释放+看门狗(I11)、文档同步、测试约定

## 怎么扩展(规则是活文档)

- 新教训/新事故 → 在 `harness/constraints.json` 加一条规则(id、severity、scope、patterns、sdd_ref),hook/check/brief 三处自动生效
- 新工程领域 → 在 `harness/index.json` 加一个 topic(globs + 关键词 + SDD refs)
- SDD 正文更新 → 什么都不用做,`brief`/`sdd` 实时切片自动呈现新内容
- 改完跑 `venv/bin/python -m unittest tests.test_sdd_harness`(31 个测试守护 harness 本身)

## 与 superpowers 的关系(两层融合)

superpowers 管**过程**(头脑风暴→计划→执行→评审→收尾),sdd-guardian 管**内容**(SDD 约束/需求/文档同步)。冲突时 SDD/AGENTS.md/constraints.json 是项目法律,永远优先。每个 superpowers 阶段挂的 guardian 动作、spec↔SDD 引用规则见 `references/workflow-integration.md`;常用一条:`brief` 拉简报,`trace` 审 spec 引用。

## 参考文档

- `SKILL.md` — 六阶段生命周期(简报→设计→实现→测试→验证→文档同步/提交)+ superpowers 阶段映射表
- `references/workflow-integration.md` — 两套技能的融合契约(分析、仲裁、映射、边界)
- `references/constraint-catalog.md` — 全部规则 + 事故渊源
- `references/lifecycle.md` — 每阶段检查清单
- 冲突时以 `SDD/` 原文为准

## 卸载 hooks

编辑 `~/.kimi-code/config.toml`,删掉 `# BEGIN sdd-guardian` 到 `# END sdd-guardian` 之间的段落即可(skill 本体仍在仓库里,可随时重装:`python3 .agents/skills/sdd-guardian/harness/install_hooks.py`)。
