# FlexRadio 天线选择 —— 实现设计（AD-036 的落地）

> 协议依据：`docs/flexradio-antenna-protocol.md`（本仓，2026-11-16）。
> 架构决策：`SDD/08-architecture-decisions.md` **AD-036**。
> 现场：FLEX-6600 `1618-1546-6600-4131` / 固件 4.3.16.42034 / 盒子 `ba1rb`（W103D）。

## 1. 目标与非目标

**目标**

1. 操作员能在网页上选择**接收天线**，选项就是**电台自己报的**那一组，不需要知道型号。
2. 改完立刻生效，并且**界面上能看出来**（S 表 / 频谱随之变化）。
3. 无人值守也要对：应用只能操作**自己的** slice 与 panadapter，绝不碰别的客户端。
4. 没有这个能力的机型（串口后端）不多出一个假控件。

**非目标**（本轮不做，理由在 AD-036 的 Alternatives）

- 发射天线（`txant`）：AD-019 的 TX 门禁还没放开，一次只动一件事。
- diversity / `loopa` / `loopb`：手册说只能从 Panadapter 的 ANT 菜单操作，API 写入口未找到。
- 变频器（XVTR）配置：属于 `Settings → Radio Setup`，本仓没有对应页面。

## 2. 数据流

```
电台状态推送 S…|slice 2 rxant=RX_B ant_list=ANT1,ANT2,RX_A,RX_B,XVTA,XVTB
        │  （增量：只带变化的键）
        ▼
engine._update_slice()          → engine.slices[2] = {rxant, ant_list, pan_id, active, client_handle, …}
        │  仅当 index == own_slice 时
        ▼
_on_broadcast("antenna_name" | "antenna_list", …)
        ▼
server._on_cat_broadcast()      → radio.update(antenna_name=…, antenna_list=[…])
        ▼
_broadcast_state()（脏字段）    → WS stateUpdate {fields:{antenna_name, antenna_list}}
        ▼
static/ft710_ui.js renderUpdates → renderAntennaButton()

操作员点 ANT 键
        ▼
sendCommand('antenna', 'ANT2')  → WS {"type":"set","field":"antenna","value":"ANT2"}
        ▼
server._execute_set_command()   → cat.set_antenna_name('ANT2')
        ▼
backends/flex/backend.py        → 校验 ∈ antenna_list，再 slice s <own> rxant=ANT2
        ▼
电台推送新状态 → 回到最上面（界面以电台回报为准，不做假乐观更新之外的假设）
```

**初始值**：`initial_state_sync()` 带 `antenna`（1-based 序号，兼容旧客户端）、
`antenna_name`（电台叫法）、`antenna_list`（候选）。

## 3. 各层契约（已实现 vs 待做）

| 层 | 契约 | 状态 |
| --- | --- | --- |
| `backends/base.py` | `set_antenna_name(name) -> bool`（默认 False）、`get_antenna_name()`（默认 None）；`set_antenna(int)` 保留 | ✅ 1.28.70 |
| `backends/flex/engine.py` | 解析 `rxant` / `txant` / `ant_list`；`pans{}` 记 pan 归属；`ensure_pan()` 拒收错误回执；`ensure_slice()` 认领/创建自己的 slice | ✅ 1.28.70 |
| 同上 | `meter_by_name(..., num=)`；`s_meter_dbm()` 按 `own_slice` 取 | ⏳ 待发（本篇随 FP58） |
| 同上 | `_connect_once()` 拿不到 pan 就失败（不半连接） | ⏳ 待发（FP58） |
| `backends/flex/backend.py` | `set_antenna_name()` 校验 + 下发 `rxant`；`get_antenna()` 由 `ant_list`+`rxant` 反推序号；`initial_state_sync` 带三个键 | ✅ 1.28.70 |
| `radio_state.py` | `antenna_name: str`、`antenna_list: list[str]`、`mode: int \| str`；`mode_name` 对字符串不抛 | ✅ 1.28.70 |
| `server.py` | `antenna` 命令双形态（名字 / 整数）；拒绝时回一条中文 error | ✅ 1.28.70 |
| `static/*` | DSP 行 `ANT` 键（循环）；`antenna_list` 为空则隐藏；`?v=` 与 `sw.js CACHE` 同步 bump | ✅ 1.28.70 |
| `backends/flex/protocol.py` | 把 `0x50000004`（取值非法）、`0x50000009`（无 pan 槽）、`0x5000002D`（未知键）、`0x50000031`（越界）写进 `error_name()` | ⏳ 待做（FP58，日志/UI 说人话） |

## 4. 判据：怎么算"真的切换了"

天线切换**没有独立的读回接口**，只能看两样东西，缺一不可：

1. **回执 + 状态回推**：`slice s <n> rxant=X` 回 `0`，且随后状态推送里 `rxant=X`。
   （`ant=` 那种"回 0 但状态不变"的假成功必须排除。）
2. **读数变化**：该 slice 自己的 `SLC/LEVEL` 变化。判据不是"绝对值"，而是**相对**：
   接过负载的口（本机 ANT1/ANT2/RX_A）与空口（RX_B/XVTA/XVTB）差 **9–10 dB**。
   同一个口在不同模式（USB vs AM）会差 ~8 dB，所以**比较必须在同一模式、同一频点**下做。

> 现场教训：只做了 1、没做 2 的判读，就会以为"切了没反应"；而 2 又被"读错 slice 的表"
> 污染。两条都钉住之后，才能真正判断是软件没生效，还是**电台本来就收不到东西**。

## 5. 测试清单（已有 / 待补）

| 用例 | 位置 | 钉住什么 |
| --- | --- | --- |
| `AntennaSelectionTests`（5 条） | `tests/test_flex_backend.py` | 名字 → `rxant=`；大小写；不在 `ant_list` 里拒绝且**不发命令**；整数按列表取；同步带三个键 |
| `EnsurePanTests`（4 条） | 同上 | 错误回执不得当 pan id；认领带 handle 的 pan；成功时用回执 id；拿不到 pan 时 `_connect_once` 失败 |
| `EnsureSliceTests`（4 条） | 同上 | `active` 优先；非 active 要激活；自己的 handle 优先于 pan 启发；都没有则 `slice create pan=` |
| `test_slice_status_carries_the_antenna_selection_and_the_radios_list` 等 2 条 | `tests/test_flex_engine.py` | 增量状态解析 + 只对 `own_slice` 广播 |
| `test_the_s_meter_belongs_to_our_own_slice` | 同上 | 四组 `SLC/LEVEL` 里取自己那条（FP58） |
| `test_the_antenna_name_and_list_round_trip` | `tests/test_radio_state.py` | 状态序列化与 `from_sync_result` |
| 缓存守卫（6 个文件） | `test_server_ws_protocol` / `test_audio` / `test_menu_version` / `test_spectrum_profile_server` / `test_tx_liveness` / `test_ws_token_transport` | `?v=` 与内容哈希 pin 必须跟着改 |

**待补**：`0x50000004` 等错误码进 `error_name()` 的用例；`listen` 角色下 `antenna` 命令被拒的用例。

## 6. 真机验收（每次改动都要重跑）

| # | 步骤 | 判据 |
| --- | --- | --- |
| 1 | 连上盒子，看 `fullState.antenna_list` | 等于电台 `ant_list`（本机 6 个） |
| 2 | 逐口下发，读**自己 slice** 的 `SLC/LEVEL` | 接负载的口比空口高 ~9–10 dB（同模式同频点） |
| 3 | 非法名字（`ANT9`） | 界面弹中文错误，**电台侧无命令** |
| 4 | 别的客户端占满 pan 槽后重启应用 | 明确降级而不是接管别人的 pan（日志点名 `0x50000009`） |
| 5 | 两台客户端各有 pan | 我们的调频/天线只影响自己那块 pan（看对方瀑布不动） |
| 6 | FT-710 | `ANT` 键不出现 |

## 7. 风险

| 风险 | 处置 |
| --- | --- |
| 一个 pan 上有多条 slice 时，改一条会带走整块 pan（手册明确） | 我们的引擎保证只在自己新建的 pan 上放一条 slice；文档把这个语义写给操作员（"这是这块瀑布的天线"） |
| 别人也用同一个 SCU 的同一路天线 | 电台按 pan 生效，同一 SCU 上的两块 pan 会互相覆盖 —— 检测到 `active` 与我们不一致时记日志（待办） |
| 浏览器缓存旧 JS（没有 ANT 键） | `?v=` + `sw.js CACHE` 一起 bump，并写进 CHANGELOG 的交付说明 |
| S 表是**幅度**读数，受 `rfgain` 与模式影响 | 验收强调"同模式同频点的相对差"，不拿绝对值下结论 |
