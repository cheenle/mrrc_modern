# 主界面快捷行「显示当前值」设计

日期：2026-10-09
范围：`static/ft710_ui.js` 的 `renderButtonLabels()`；`static/index.html` / `static/sw.js` 缓存版本；5 个测试钉点。
状态：设计已确认，待实现计划。

## 1. 目标与边界

**目标**：主界面快捷行 5 个按钮（MODE / BAND / FILTER / ATT / PRE）文字显示**当前值**，点击仍轮转到下一个。
语义对齐 Android 端 `FT710Android/.../UI/MainScreen.kt:81-82`：按一下换下一个，但按钮文字始终是当前状态
（`Text("ATT ${state.attenuatorLabel}")`）。

**现状**：`renderButtonLabels()`（`static/ft710_ui.js:401-428`）渲染的是**下一个值**——
MODE 显示下一个模式、BAND 下一个波段、FILTER 下一个滤波宽、ATT 下一个衰减（缩写 `OF`/`6d`/`12`/`18`）、
PRE 下一个预放（缩写 `OF`/`A1`/`A2`）。用户看到的是「按下去会变成什么」，而不是「现在是什么」，ATT/PRE
两格失去按钮身份。

**非目标**（明确不做）：

- 不改轮转逻辑：`getNextMode` / `getNextBand` / `getNextFilter` 与 `(x + 1) % N` 全部不动。
- 不改服务端 / WebSocket 协议 / `fullState` 字段。
- 不改 Android、iOS、`/listen` 页。
- 不加按钮激活态高亮/变色（Android 端也没有）。
- 不做发布动作（CHANGELOG、版本号、SDD 版本历史留到发布时统一处理）。

## 2. 基线事实

- 点击处理器已**乐观更新** `radioState.attenuator` / `preamp` / `filter_width` / `mode_name` / `band_name`
  并立即 `renderButtonLabels()`（`static/ft710_ui.js:1338-1390`）。因此渲染当前值后，点击即时可见，
  服务端回推只做校正。
- capabilities 提供 `att_steps`（FT-710 `(0, 6, 12, 18)`、IC-7300 `(0, 20)`、Yaesu 3 dB 多档）
  与 `preamp_steps`（`("OFF", "AMP1", "AMP2")`）——`backends/ft710/backend.py:120-121`、
  `backends/yaesu/backend.py:66`、`backends/ic7300/backend.py:96-97`。
- 服务端 state 另有 `attenuator_label` / `preamp_label`（`radio_state.py:237-242`，Android 用的是这两个）。
- 无 capabilities 时前端已有一套 FT-710 兜底（`_attStepCount` / `_preStepCount`）。
- 状态栏（`renderStatusBar`）已显示当前 `mode_display` / `band_name`，本次不动。
- `ft710_ui.js` 是受工具守卫保护的 legacy 前端文件（`AGENTS.md`「Tooling Guards」），改动须手工精确、
  不触发 biome 重排；其缓存版本受 5 个测试文件钉死。

## 3. 决策：标签数据源用「方案 1」

**方案 1（采用）**：前端从 capabilities 的 `att_steps` / `preamp_steps` 派生完整标签。
- 优点：点击后立即显示新值（沿用乐观更新，无等待）；caps 驱动、跨后端（FT-710 / IC-7300 / Yaesu
  3 dB 档）自动适配；与现有 `_attStepCount` / `_preStepCount` 同一数据源；无 caps 时回退 FT-710 常量表。
- 代价：衰减文案统一为 `${db}dB`；Yaesu 服务端 label 文案是 `6 dB`（带空格，`backends/yaesu/backend.py:117`），
  显示上略有差异（不影响功能，可接受）。

**方案 2（未采用）**：直接用服务端 `radioState.attenuator_label` / `preamp_label`。
- 优点：与后端文案逐字一致（Android 同款）。
- 代价：点击后要等服务端回推才变，远程连接有可感延迟；且要为空值/未连接补回退，反而多一套分支。

## 4. 行为规格

| 按钮 | 显示（当前值） | 点击（轮转，不变） | 数据源 |
| --- | --- | --- | --- |
| MODE | 当前 `USB` / `CW-U` / … | `getNextMode` | `radioState.mode_name` |
| BAND | 当前 `20m` / … | `getNextBand` | `radioState.band_name` |
| FILTER | 当前滤波宽 `2.4k` / … | `getNextFilter` | `getFilterLabel(radioState.filter_width, radioState.mode_name)` |
| ATT | `OFF` / `6dB` / `12dB` / `18dB` | `(x + 1) % _attStepCount()` | caps `att_steps`；无则 FT-710 常量表 |
| PRE | `OFF` / `AMP1` / `AMP2` | `(x + 1) % _preStepCount()` | caps `preamp_steps`；无则 FT-710 常量表 |

设计评审时已确认的细节：

1. MODE 显示 `mode_name`（如 `CW-U`）而非状态栏的 `mode_display`（`CW`）——与轮转列表同一套命名，
   点击结果可预期。
2. 连接前 HTML 占位文本（`ATT` / `PRE` / `USB` / `20m` / `2.4k`）保持现状，连上即被当前值覆盖。
3. `dataset.current` 保留（无消费者，语义仍正确，避免无谓 diff）。
4. 越界/缺字段防御：`att_steps` 存在时取 `att_steps[idx]`，`0`/`undefined` → `OFF`，否则 `${db}dB`；
   `preamp_steps[idx] || 'OFF'`。

## 5. 改动清单

1. `static/ft710_ui.js`
   - 用 `_attFullLabel(idx)` / `_preFullLabel(idx)` 替换 `_attShortLabel` / `_preShortLabel`（完整标签）。
   - `renderButtonLabels()`：5 个按钮全部渲染当前索引/当前名。
   - 点击处理器、`getNext*`、`renderUpdates` 的触发字段列表均不动。
2. `static/index.html` + `static/sw.js`
   - `ft710_ui.js?v=34 → v=35`（`static/index.html:580`、`static/sw.js:8`）。
   - SW `const CACHE = 'mrrc-v47' → 'mrrc-v48'`（`static/sw.js:2`）。
3. 测试钉点（只动 `ft710_ui.js` 相关，`ft710_main.js?v=39` 不动）：

   | 文件 | 行 | 断言 |
   | --- | --- | --- |
   | `tests/test_server_ws_protocol.py` | 234 / 237 / 239 | v34→v35、mrrc-v47→v48 |
   | `tests/test_spectrum_profile_server.py` | 434 / 436 / 438 | v34→v35、mrrc-v47→v48 |
   | `tests/test_audio.py` | 151 | mrrc-v47→v48 |
   | `tests/test_tx_liveness.py` | 345 | mrrc-v47→v48 |
   | `tests/test_ws_token_transport.py` | 184 | mrrc-v47→v48 |

   `tests/test_spectrum_profile_server.py:444` 的 stale 列表（`v38` / `v33` / `mrrc-v46`）无需改动。
4. 新增前端契约测试（放在 `tests/test_server_ws_protocol.py` 的既有 UI 契约类中）：断言
   `renderButtonLabels` 用当前索引渲染（`_attFullLabel(radioState.attenuator)` /
   `_preFullLabel(radioState.preamp)` / `radioState.band_name`），且不再出现 `nextAtt` / `nextPre`
   作为 ATT/PRE 的按钮文案。

## 6. 验证

- `node --check static/ft710_ui.js`（项目惯例）。
- `python -m unittest discover -s tests -v` 全量绿。
- 手工验收（真机或浏览器）：当前 OFF 时 ATT 显示 `OFF`，点击依次 `OFF → 6dB → 12dB → 18dB → OFF`；
  PRE `OFF → AMP1 → AMP2 → OFF`；MODE/BAND/FILTER 与状态栏一致；IC-7300 下 ATT 显示 `OFF` / `20dB`；
  浏览器控制台无 JS 错误。

## 7. 验收标准

1. 快捷行 5 个按钮任一时刻显示的都是当前值，不是下一个值。
2. ATT/PRE 用完整标签（`6dB` / `12dB` / `18dB`、`AMP1` / `AMP2`），不再出现 `6d` / `12` / `18` / `A1` / `A2`。
3. 点击轮转行为、命令发送、乐观更新与服务端回推校正与改动前一致。
4. 缓存版本与全部钉点测试同步更新，全量测试绿。
