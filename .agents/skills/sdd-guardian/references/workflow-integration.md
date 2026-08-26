# Workflow Integration — Superpowers × SDD-Guardian

两套技能的深度分析结论与融合契约。原则:**不 fork superpowers**(`~/.codex/skills/`,
外部维护,升级会覆盖修改);融合层全部落在项目自有的 sdd-guardian 内。

## 1. 本质:同一生命周期的两层

| 层 | 技能集 | 管什么 | 可追溯链 |
|----|--------|--------|----------|
| 过程纪律 (HOW) | superpowers ×17 | 先 brainstorm 再 plan 再执行,每步有门禁 | spec → plan → spec 合规审 → code review |
| 内容纪律 (WHAT) | sdd-guardian | 设计是否违反 SDD:约束、需求、决策、可行性 | SDD → brief → AD/NFR 检查 → doc-sync |

重叠区(双方都管):预提交验证、测试纪律、计划门禁。
互补区:superpowers 没有"行为变更必须同步既有文档"的规则(sdd-guardian 独有);
sdd-guardian 没有结构化创意/计划/评审流程(superpowers 独有)。

## 2. 优先级仲裁(冲突时谁赢)

按 using-superpowers 自己的优先级规则(用户指令 > 技能 > 系统默认):
**SDD/、AGENTS.md、constraints.json 是项目法律,高于任何工作流技能的默认行为。**
例:某 superpowers 模式若与 SH00NN 格式、DN 禁令、44.1kHz 设备域冲突,约束永远赢;
PreToolUse hook 的阻断不因任何工作流豁免。

## 3. 阶段融合映射(每个 superpowers 技能挂哪个 guardian 动作)

| 生命周期 | Superpowers 技能 | SDD-Guardian 动作 |
|----------|------------------|-------------------|
| 创意/需求 | `brainstorming` | `brief --task`;产出 spec 必须引用触及区域的 SDD refs(AD/NFR/§),用 `trace` 验证;先查 §3.2/3.3 范围与 I6–I11 开放问题 |
| 计划 | `writing-plans` / `create-plan` | `brief <files>`;计划任务清单必须包含受影响 SDD 章节 + 一条 doc-sync 任务;`trace` 验证引用覆盖 |
| 隔离 | `using-git-worktrees` | 基线验证 = `unittest` + `check`(替代裸跑测试) |
| 执行 | `executing-plans` / `subagent-driven-development` | PreToolUse hook 自动阻断违规;子代理任务简报里粘贴相关约束(`context <files>` 输出) |
| 调试 | `systematic-debugging` | **先读 `references/constraint-catalog.md`** — 它本质是本项目的事故史,每条阻断规则都是一个生产事故的根因;嫌疑文件跑 `check` |
| 验证 | `verification-before-completion` | 完成门禁 = 三件套全绿:`unittest` + `check --staged` clean + Phase 5 doc-sync 完成 |
| 评审 | `requesting-code-review` / `receiving-code-review` | 评审上下文附 `brief` 输出;`constraints.json` 即评审清单(block/warn 逐条过) |
| 收尾 | `finishing-a-development-branch` | 合并/PR 前过 Phase 5 文档同步表 + SDD/14 版本条目 |

## 4. 产物可追溯(spec/plan ↔ SDD)

superpowers 的 spec/plan 存在 `docs/superpowers/`;SDD 是本项目唯一设计记录。桥接规则:

- spec/plan 中提到的每个代码文件,其路由到的 SDD refs(AD/NFR/§)应在文档中被引用;
- 验证命令:`python3 harness/sdd_context.py trace <spec-or-plan.md>` — 列出已引用/缺失的 SDD refs(advisory,不阻断);
- 设计定稿落地为运行时行为后,SDD 在同一提交内更新(spec 是提案,SDD 是现状)。

## 5. 不做的事(融合边界)

- 不修改 `~/.codex/skills/` 下任何文件(外部所有,升级即丢);
- 不把 superpowers 流程复制进 sdd-guardian(引用其名即可,单源);
- hook 只阻断内容违规,不阻断流程跳过(流程纪律由 superpowers 的元技能与 agent 自律承担)。
