# 电台电源：菜单里的「重启电台…」与「关机…」 设计

- **日期**：2026-10-16
- **状态**：已批准，待实现
- **范围**：产品 Web 界面（☰ 菜单）+ 服务端 WS `set power` 语义；多后端（Yaesu `PS0/PS1`、Icom CI-V `0x18 00/01`）
- **相关代码**：`server.py`（`_power_on_radio` / WS `set` 的 `power` 分支 / `_power_boot_until`）、
  `radio_state.py:power_on`、`backends/base.py:boot_verify`、`backends/ft710/cat_controller.py:set_power`、
  `backends/ic7300/civ_controller.py:build_power_on_frame`、`static/index.html`（☰ 菜单、状态行）、
  `static/ft710_ui.js`（`handleMenuAction`、`showToast`）、`static/ft710_main.js`（WS `onmessage`）、
  `static/sw.js`（缓存版本）
- **相关文档**：`CHANGELOG.md`（v1.7.7「Withdrawn」）→ **SDD V2.13**（撤回的电源键）、
  `SDD/10-service-model.md`（`power` 命令行）、`SDD/14-version-history.md`、
  `docs/OPERATION_GUIDE.md`（第 16 行的免责句 + 菜单/⏻ 说明）、`AGENTS.md`、`tests/README.md`

---

## 1. 背景：这个开关被撤回过一次，这次要按撤回的教训来做

`CHANGELOG.md` v1.7.7「Withdrawn (after field reliability testing, SDD V2.13)」记着两条**实测**事实：

1. **`PS1;`（开机）不可靠** —— 健康的电台连试 3 次、跨 36 秒没醒；
2. **`PS0;` 落在 `PS1;` 之后十几秒的启动窗口内，会把 FT-710 的 CAT MCU 卡死** ——
   串口/音频/频谱 USB 全都枚举在、CAT 永久聋，**要物理断电**才能救。

当时的处置是把 UI 上的电源键整个撤掉，只留 `power` 命令给维护脚本（`_power_cycle*.py`），
并留下三条守卫：`PS1` 后 15 s 内拒绝 `PS0`、`PS1` 重试 ≤3 且用 `FA;` 回读校验、`PS0` 双发。
这些守卫今天仍在 `server.py:2264-2741` 里。

2026-10-16 操作员要求把它作为**菜单项**放回（不是页头那个一键开关），并要求两种意图分开
（见 §3）。本设计的前提是**不推翻上述事实**，而是：

- 把"一次尝试"改成**有界、可取消的持续尝试**（这正是"3 次/36 s 全失败"的直接对策）；
- 把"看起来成功了"改成**只有回读确认才算成功**（沿用 `backend.boot_verify()`）；
- 新增一条**实测要复核的怀疑**：`PS0` 之后电台的 USB 串口会消失，`PS1` 让电台起来时串口
  **重新枚举**（设备路径变化 / 打开时尚未就绪），于是"唤醒其实成功、脚本却读不到回执" ——
  维护脚本 `_power_cycle.py` 自己就写着 *"10 attempts in case port is re-enumerating after
  power-off"*。本设计把"等端口回来再写、串行不并发"作为硬前提（§5 第 4 条），
  并在真机验收里把这条旧结论重新量一次（§8 V2/V6）。

## 2. 目标与非目标

**目标**：在 ☰ 菜单里提供两件事，语义**不可含糊**：

- `重启电台…`：关机 → 等它真的断（端口消失）→ **持续尝试唤醒**，直到醒来 / 15 分钟 / 用户取消；
- `关机…`：只关，**保持关闭，永不自动唤醒**。

**非目标（YAGNI，明确不做）**：

- 云端/hub 侧遥控（不新增 REST 端点，电源只在 WS 上）；
- 外部开关硬件（智能插座 / 继电器 / ESPHome）——那是另一条设计（上一轮的方案 B）；
- 定时开关机、断电后自动开机、开机自检、状态历史；
- 救援状态的**持久化**：服务器进程重启 = 救援结束（此时操作员本来就在场，且新进程会照常
  重连串口；"我走开了、它还在试"这一场景不在本设计里）；
- 改动 listen-only 角色能做什么（`power` 本来就不在白名单里，见 §5 第 7 条）。

## 3. 交互与协议

**菜单**（`static/index.html` 的 `menu-list`，插在「接入云端」之后、「录音」之前）：

| 项 | `data-action` | 点击后 |
| --- | --- | --- |
| `重启电台（关 → 自动唤醒）…` | `power-restart` | `confirm` 说明"会短暂断线；15 分钟内持续尝试唤醒，失败需要到现场按面板" → 发 `{"type":"set","field":"power","value":"restart"}` |
| `关机（保持关闭）…` | `power-off` | `confirm` 红字说明"关机后远程不可用，只能到现场开机" → 发 `{"type":"set","field":"power","value":false}` |

**协议**：`field: "power"` 的取值扩到四种，**旧的 `true|false` 语义一字不改**
（维护脚本 `_power_cycle_ws.py` 与任何老客户端不受影响）：

| value | 语义 |
| --- | --- |
| `true` | 只做一次开机尝试（`_power_on_radio`，≤3 次 / ≤36 s），失败报错 —— 现状 |
| `false` | 关机（守卫见 §5），**并且取消任何进行中的救援** |
| `"restart"` | 关机 + 起救援（有界、可取消） |
| `"cancelRescue"` | 取消救援（不碰电台） |

**广播**：新增 `{"type":"powerRescue","state":"running"|"woke"|"timeout"|"cancelled",
"attempt":N,"elapsed":S,"maxSeconds":900}`；`radio_state.power_on` 与 fullState 继续由既有
`radio.update()` 通道推送（客户端已经在收 `power_on`）。

**状态呈现**（页头状态行加 `id="status-power"`）：`PWR 开` / `PWR 关` / `PWR 唤醒中 3`。
救援进行中，这一格**可点** = 取消救援（`confirm` 后发 `cancelRescue`）。
`attempt` / 「第 N 次」是**已开始的轮数**（第 1 轮 = 关机后的第一次 `PS1`）。
每轮尝试发一条 4 秒 toast「正在尝试唤醒电台（第 N 次）…点顶栏 PWR 可取消」；
`woke` → 绿色 toast「电台已开机」；`timeout` → 红色 toast「15 分钟内没唤醒：需要到现场按面板开机」。

## 4. 服务端实现

**提取**：把 WS `set` 的 `power` 分支里的守卫提成 `_power_off(cat, radio, scheduler) -> str`
（返回 `""` 表示成功，否则返回给用户的错误文案），使"关机"只有一处实现。

**救援**：新增模块级状态与任务

```python
POWER_RESCUE_INTERVAL_S = 30.0   # 两轮之间至少间隔这么久
POWER_RESCUE_MAX_S      = 900.0  # 15 分钟上限，到点报"需要现场按面板"
POWER_RESTART_SETTLE_MIN_S = 3.0 # 关机后至少等这么久再发第一条 PS1
POWER_RESTART_SETTLE_MAX_S = 15.0# 或等到串口消失为止（先到者为准）
```

- `_power_rescue_task: asyncio.Task | None`、`_power_rescue_attempts: int`、
  `_power_rescue_started: float`、`_power_rescue_state: str`。
- 一轮 = **复用既有 `_power_on_radio(cat, backend)`**（它已经做了：PS1 ≤3 次、每次刷新
  `_power_boot_until`、每秒 `boot_verify()` 回读、失败给结论）。循环：未取消且未到上限 →
  `if await _power_on_radio(...): woke` → 否则 `await sleep(POWER_RESCUE_INTERVAL_S)` 再来。
  也就是说：**新代码只有循环、广播、取消三件事**，重试与校验语义全部来自已被测试覆盖的函数。
- 起救援前先 `_power_off()`，再 `_power_restart_settle()`：等 `cat.connected` 变假或
  上限 15 s（下限 3 s）—— 这样不必猜"电台关完了没有"。
- 结束时（`woke`/`timeout`/`cancelled`）清状态并广播；`timeout` 同时写一条 WARNING 日志，
  含总轮数与总耗时。
- 取消：`_power_rescue_cancel()`（置标志 + `task.cancel()` + 广播 `cancelled`）。
  **它不发送任何 CAT 命令**。

**日志**：每次进入/退出、每轮结果、最终结论各一行（`logger.info`/`warning`），
含 `attempt`、`elapsed`、电台回的读值 —— 这正是下次现场判读的读数。

## 5. 守卫（不可协商，逐条对应一次真实事故或实测）

1. **发射中拒绝**关机/重启：`radio.is_transmitting` **或** `_ft8_transmitting()`。
   （后半条是本次补的真空档：电源分支早于 FT8 存在，现在只查前者。）
2. **`PS1` 后 15 s（`POWER_BOOT_WINDOW_S`）内拒绝 `PS0`**，并把剩余秒数告诉用户（既有）。
   救援每轮都会刷新该窗口，因此**救援期间"关机"会被拒** —— 这是设计意图；
   「取消救援」是**独立动作，不受此限**。（取消救援后仍可能要在 15 s 窗口过后才能真正关机，
   UI 用既有的倒计时文案解释。）
3. **`PS0` 双发**（既有；第一次写偶发丢失）。
4. **唤醒只在 `cat.connected` 为真时写**，且**串行**：一轮一次，绝不并发两条 `PS1`；
   端口不在（`PS0` 后 USB 消失）就等既有 watchdog 重连，不自己开串口
   （也满足 `cat-direct-serial-io` 约束：所有串口 I/O 只经 `CatController`）。
5. **有界 + 可取消 + 结束即停手**：上限 15 分钟；取消后不再发任何 `PS1`。
6. **「关机…」的语义是"保持关闭"**：它先取消救援，且**不发送 `PS1`**（A1 的关键承诺）。
7. **角色门是执行点**：`power` 不在 `LISTEN_ALLOWED_SET_FIELDS` 里 ⇒ listen 角色发
   `set power` 被 WS 门拒绝（错误文案沿用 `LISTEN_ONLY_MESSAGE`）；UI 对 listen 角色不渲染这两项。
8. **唯一成功判据是回读**（`FA;` / CI-V 频率读，即 `backend.boot_verify()`），不是"我发过命令"。
9. `radio.update()` 走既有 dirty-field 通道（不直接赋 `radio_state` 字段，否则客户端看不到变化）。

## 6. 前端实现（两个受 tooling-guard 保护的文件，只做加法）

- `static/index.html`：菜单两项 + 状态行 `status-power`；**缓存版本三处同步**
  （`ft710_main.js?v=` / `ft710_ui.js?v=`、`sw.js` 的 `CACHE` 与预缓存清单；测试里的 pin 一并更新）。
- `static/ft710_ui.js`：`handleMenuAction` 两个分支（`window.confirm` 沿用"删除录音"那套）、
  `renderPowerRescue(msg)`（更新 `status-power` 文案与可点状态）、`status-power` 的点击处理。
- `static/ft710_main.js`：`onmessage` 增加 `powerRescue` 分支（沿用该文件既有的
  tab 缩进 / 双引号风格，**不重排、不"格式化"**）。
- listen 角色：`role === 'listen'` 时不渲染这两项（执行点仍是服务端门，见 §5 第 7 条）。

## 7. 失败处理与文案

| 情形 | 界面 | 日志 |
| --- | --- | --- |
| 关机被拒（TX / FT8 发射中） | 既有错误 toast「发射中不能关机」 | `info` |
| 关机被拒（启动窗口） | 既有文案「电台正在启动，请 N 秒后再关机」 | `info` |
| 救援进行中 | toast/状态行「正在尝试唤醒（第 N 次）」 | 每轮 `info` |
| 唤醒成功 | 绿 toast「电台已开机」+ `PWR 开` | `info`（第 N 次、耗时） |
| 15 分钟未醒 | 红 toast「15 分钟内没唤醒：需要到现场按面板开机」+ `PWR 关` | `warning`（总轮数、总耗时） |
| 用户取消 | 「已取消唤醒尝试」+ `PWR 关` | `info` |
| 一次 `power:true` 失败（老客户端/脚本） | 既有文案「电台开机无响应——请检查电台并手动开机」 | `warning` |

**不允许**出现的表述：把 `timeout` 说成"开机失败请重试"以外的任何暗示成功的措辞；
也不允许在未回读的情况下把 `power_on` 置真。

## 8. 测试与验收

**离线（进套件，扩展 `tests/test_power_switch.py`）**：

1. 关机：TX 中拒绝；**FT8 发射中拒绝**；启动窗口内拒绝且文案含剩余秒数；正常路径下 `PS0` 写两次；
2. 「重启」= 先关机、再起救援（顺序断言）；`power_on` 变假；
3. 救援：`woke`（第 N 轮成功即停）、`timeout`（上限后停止并广播）、`cancelled`（取消后不再写 `PS1`）；
4. 救援期间**每次 `PS1` 都刷新启动窗口**；`cat.connected` 为假时**一轮都不写**；
5. 「关机…」取消进行中的救援，且**不发送 `PS1`**；
6. listen 角色对 `field:"power"`（四个取值）被 WS 门拒绝；
7. 前端守卫：`index.html` 有那两项与 `status-power`；`handleMenuAction` 有两分支；
   缓存版本三处 pin 一致，且旧版本号已不在（沿用现有 stale 断言写法）。

**真机（必须在真 FT-710 上跑，本节全部为验收门）**：

| # | 场景 | 判据 |
| --- | --- | --- |
| V1 | 关机 | 电台熄灭、`power_on` 回读为假、界面 `PWR 关` |
| V2 | 重启 | 记录**第几轮醒来、总耗时**（这是复核"PS1 不可靠"的核心读数） |
| V3 | 救援中取消 | 救援停止、电台保持关闭、不再有任何 `PS1` |
| V4 | 救援中点「关机…」 | 15 s 窗口内被拒（有倒计时），窗口过后关机生效 |
| V5 | TX 中 / FT8 发射中 | 两个动作都被拒，电台不熄火 |
| V6 | 失败路径（把电台电源物理断开后点「重启」） | 15 分钟后必须给出"需要现场按面板"，**不得**报成功 |
| V7 | listen 角色 | 菜单里看不到；直接发 WS `set power` 被拒 |

**逃生舱（先写进规格，不许事后改口径）**：若 V2/V6 证明 `PS1` 在真机上**根本唤不醒**
（即排除了端口重新枚举这一层之后仍然失败），则**只发布「关机…」一项**、文案写"开机需
现场按面板"，**不把未验证的能力摆在界面上** —— 这是 V2.13 那次撤回的直接延续。

## 9. 文档同步清单（一次做完）

`CHANGELOG.md` 新条目（含 V1–V7 结果）· `SDD/10-service-model.md` 的 `power` 行（值域扩到四种 +
救援语义）· `SDD/14-version-history.md`（新版本条目）· `SDD/README.md` Quick Facts 版本 ·
`docs/OPERATION_GUIDE.md`（第 16 行免责句 + ⏻ 说明 + §6.x 新增"电台电源"小节）→ 重跑
`website/build_guide.py`（`guide.html` 与 `zh/guide.html`）· `AGENTS.md`（界面/菜单说明与
`server.py` 行）· `tests/README.md` 计数 · `docs/PROJECT_MAP.md` 若涉及。
