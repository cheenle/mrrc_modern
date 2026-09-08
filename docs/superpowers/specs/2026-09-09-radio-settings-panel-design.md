# 电台设置面板（RF 功率滑块）设计

日期：2026-09-09 · 状态：已批准 · 范围：frontend-only（服务端零改动）

## 1. 背景与目标

在 ☰ 主菜单的「设置」中提供电台 TX 功率设置（滑块）。

现状盘点（探索结论）：

- 服务端 `rf_power` 设置链路**已完整**：`server.py:1224` 路由 → FT-710 `PC`（5–100 瓦特）/ IC-7300 CI-V level 0x0A（0–100% ↔ 0–255 raw，`_pct_to_raw` 映射已存在），带 `skip_next_poll("rf_power", 3.0)` 防回读覆盖；`rf_power` 已在轮询列表与状态广播中。
- 前端 `static/index.html` 存在休眠 UI：`#dsp-sliders` 区块（含 `slider-rfpower-row`）整体 `display:none`（注释 "hidden - not enough space"），无任何代码会显示它；`ft710_ui.js` 已有滑块 input/change 处理器与 `renderSliders` 回显。
- ☰ 菜单 `settings` 动作目前仅滚动到 `.dsp-panel`。

决策：不唤醒旧区块（当初因小屏空间不足隐藏），改为**菜单「设置」打开独立的「电台设置」模态面板**（用户选定方案 A）。

## 2. 交互设计

- ☰ 菜单 → 设置 → 打开「电台设置」模态面板（复用 `modal-overlay` 视觉风格，内容自定义，非 `showModal` 的选择网格）。
- 面板内容（v1 只一项，YAGNI）：
  - **RF PWR 滑块**：range **5–100**，数值即时显示。
  - 语义：FT-710 = 瓦特；IC-7300 = 百分比。标签用中性 "RF PWR"，两台机器统一 5–100 尺度，不区分显示单位。
- 滑块行为：
  - `input` 事件 → 只更新数值显示（不发送）。
  - `change` 事件（松手）→ `sendCommand('rf_power', v)` —— 拖动过程不连发 CAT 命令。
  - 服务端回显（`rf_power` 广播 → `renderSliders`）在**用户正在拖动时被抑制**，松手后恢复跟随。实现：面板滑块上的 `isDragging` 布尔标志——`pointerdown`/`touchstart`/`input` 置位，`pointerup`/`touchend`/`change` 清除；`setSlider` 更新前检查（现有 `setSlider` 无拖动保护，需在面板内补）。
- 电台未连接时 `sendCommand` 静默丢弃（现有行为），面板不阻塞、不报错。
- 旧 `#dsp-sliders` 隐藏区块保持原样不动。

## 3. 实现落点

| 文件 | 改动 |
| --- | --- |
| `static/ft710_ui.js` | 新增 `showSettingsPanel()`：动态生成模态 DOM（标题「电台设置」/ "Radio Settings"、RF PWR 滑块行、关闭按钮）；绑定 input/change；`handleMenuAction('settings')` 改为调用 `showSettingsPanel()`；`renderSliders` 增加面板滑块回显（带拖动保护） |
| `static/index.html` | 仅 cache-bust 引用更新（`ft710_ui.js?v=29`），无结构改动 |
| `static/ft710_main.js` | 不改（`sendCommand('rf_power')` 已支持） |
| 服务端 | 零改动 |

## 4. Cache-bust 与测试

- cache-bust：`ft710_ui.js?v=28 → 29`；service worker `mrrc-v29 → mrrc-v30`（`static/sw.js` 与 `index.html` 引用同步）。`ft710_main.js` 未改动，`v=27` 不动。
- `tests/test_server_ws_protocol.py:220-226` cache-bust 断言重钉（ui 29 / sw mrrc-v30）。
- JS 语法：`node --check static/ft710_ui.js`（SDD V2.29 先例；仓库无前端 DOM 测试框架）。
- 回归：现有 `rf_power` 后端测试（cat/civ controller、WS 协议）全部通过；全量套件绿。

## 5. 文档同步（SDD-Guardian Phase 5）

- `SDD/14-version-history.md` 新条目 **V2.34**（frontend-only：设置面板 + 功率滑块 + cache-bust + 测试重钉）。
- `SDD/README.md`：SDD Version → V2.34、Baseline Date、Status 行追加。
- CHANGELOG 条目随下次发版一起写（顶部若加 `[Unreleased]` 段，`build.sh` 的 `grep -m1 '## \[vX.Y.Z\]'` 会跳到下一个命名版本，构建安全）。
- AGENTS.md / tests/README：无模块/计数变化，不需要改。

## 6. 验收

- 自动：全量测试绿；cache-bust 断言过；`node --check` 过。
- 手动（真机，操作员检查）：滑块设置功率 → 电台面板功率值变化；FT-710 显示瓦特、IC-7300 显示百分比；TX 时功率计与设定一致；拖动中滑块不被轮询回显打断。

## 7. 明确不做（YAGNI）

- 不加 MIC GAIN / VOX / 其他电台侧设置（面板结构为将来扩展留位，v1 不实现）。
- 不改服务端任何代码。
- 不唤醒旧 `#dsp-sliders` 区块。
- 不做功率单位换算显示（IC-7300 不显示近似瓦特）。
