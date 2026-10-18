# 电台电源（菜单里的重启/关机）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:executing-plans 逐任务实现此计划。
> 步骤使用复选框（`- [ ]`）语法跟踪进度。规格：
> `docs/superpowers/specs/2026-10-16-radio-power-switch-design.md`（本计划的唯一依据）。

**目标：** 在 ☰ 菜单里给出「重启电台…」（关机 + 有界可取消的持续唤醒）与「关机…」
（保持关闭，永不自动唤醒），守卫沿用 V2.13 的实测结论，并补掉 FT8 发射中的真空档。

**架构：** 唯一执行点在 `server.py`：把 WS `power` 分支的守卫提成 `_power_off()`，
新增 `_power_rescue` 任务——它**复用既有 `_power_on_radio()`**（PS1 ≤3 次 + `boot_verify()`
回读 + 每轮刷新 `POWER_BOOT_WINDOW_S`），所以新代码只有"循环 / 广播 / 取消"三件事。
前端只做加法：菜单两项、页头 `status-power`（救援期间可点取消）、`powerRescue` 消息分支。

**技术栈：** Python 3.11+ asyncio（stdlib unittest，无硬件）、原生 JS（两个受 tooling-guard
保护的 monolith 只加不重排）、`static/sw.js` 缓存版本。

---

## 文件结构

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `server.py` | 修改 | `_power_off()`、`_power_rescue*()`、`POWER_RESCUE_*` 常量、WS `power` 分支改调二者 |
| `tests/test_power_switch.py` | 修改 | 守卫 + 救援三条路径 + 协议 + 角色门（本计划的主要测试面） |
| `static/index.html` | 修改 | 菜单两项、`status-power`、缓存版本 `?v=` |
| `static/ft710_ui.js` | 修改 | `handleMenuAction` 两分支、`renderPowerRescue()`、`status-power` 点击 |
| `static/ft710_main.js` | 修改 | `powerRescue` 消息分支（默认 state 不动） |
| `static/sw.js` | 修改 | `CACHE` 版本 + 预缓存清单两处 `?v=` |
| `tests/test_server_ws_protocol.py`、`tests/test_spectrum_profile_server.py`、`tests/test_audio.py` | 修改 | 缓存版本 pin 同步（三处 old→new，stale 断言补旧值） |
| `SDD/10-service-model.md`、`SDD/14-version-history.md`、`SDD/README.md`、`docs/OPERATION_GUIDE.md`、`AGENTS.md`、`CHANGELOG.md`、`tests/README.md` | 修改 | §9 文档同步清单 |

---

## 任务 1：服务端守卫提成 `_power_off()` + FT8 真空档

**文件：** 修改 `server.py:2701-2741`、`tests/test_power_switch.py`

- [ ] **步骤 1：写失败的测试**（追加到 `tests/test_power_switch.py`）

```python
class PowerOffGuardTests(unittest.TestCase):
    """关机守卫：既有三条 + FT8 发射中（本次补的真空档）。"""

    def test_refuses_while_ft8_is_transmitting(self):
        cat = _FakeCat()
        radio = server.RadioState()
        radio.update(power_on=True)
        server._power_boot_until = 0.0
        with patch.object(server, "_ft8_transmitting", return_value=True):
            msg = asyncio.run(server._power_off(cat, radio))
        self.assertTrue(msg)                      # 非空 = 拒绝理由
        self.assertEqual(cat.power_calls, [])     # 一条 PS0 都不许发
        self.assertTrue(radio.power_on)

    def test_power_off_sends_ps0_twice(self):
        cat, radio = _FakeCat(), server.RadioState()
        server._power_boot_until = 0.0
        msg = asyncio.run(server._power_off(cat, radio))
        self.assertEqual(msg, "")
        self.assertEqual(cat.power_calls, [False, False])
        self.assertFalse(radio.power_on)

    def test_refuses_inside_boot_window_with_remaining_seconds(self):
        cat, radio = _FakeCat(), server.RadioState()
        server._power_boot_until = time.monotonic() + 9.0
        msg = asyncio.run(server._power_off(cat, radio))
        self.assertIn("秒后再关机", msg)
        self.assertEqual(cat.power_calls, [])
        server._power_boot_until = 0.0
```

（`import time` 加到文件头；`_FakeCat` 已有 `set_power` 记录 `power_calls`。）

- [ ] **步骤 2：运行，确认失败**

运行：`.venv/bin/python -m unittest tests.test_power_switch -v`
预期：`AttributeError: module 'server' has no attribute '_power_off'`

- [ ] **步骤 3：最小实现**（`server.py`，替换 `elif field == "power":` 整段）

```python
async def _power_off(cat, radio, scheduler=None) -> str:
    """Turn the radio off; return "" on success or the user-facing refusal text.

    Guards, each one bought with an incident:
      * never while transmitting — including FT8, whose PTT is its own
        ``_ft8_transmitting()`` (the original branch predates FT8 and only
        checked ``radio.is_transmitting``);
      * never inside POWER_BOOT_WINDOW_S of a PS1 — a PS0 landing mid-boot
        wedged the FT-210 CAT MCU (2026-07-27) and needed a power cycle;
      * PS0 twice — the first write is occasionally lost.
    """
    if radio.is_transmitting or _ft8_transmitting():
        return "发射中不能关机"
    if time.monotonic() < _power_boot_until:
        remain = int(_power_boot_until - time.monotonic()) + 1
        return f"电台正在启动，请 {remain} 秒后再关机"
    if scheduler is not None:
        scheduler.skip_next_poll("power_on", 10.0)
    await cat.set_power(False)
    await asyncio.sleep(0.5)
    await cat.set_power(False)
    radio.update(power_on=False)
    return ""
```

（`time` 已 import；`_power_boot_until` 已是模块级。）

- [ ] **步骤 4：运行，确认通过** → 预期 `OK`
- [ ] **步骤 5：commit**（`feat(power): 关机守卫提成 _power_off 并补 FT8 发射中的真空档`）

## 任务 2：救援循环 + WS 协议扩展

**文件：** 修改 `server.py`（常量区 `2264-2270`、`_power_on_radio` 之后、WS `power` 分支）、
`tests/test_power_switch.py`

- [ ] **步骤 1：写失败的测试**

```python
class PowerRescueTests(unittest.TestCase):
    """「重启电台」= 关机 + 有界、可取消、只回读确认的持续唤醒。"""

    def setUp(self):
        self._sleep = patch.object(server.asyncio, "sleep", _no_sleep)
        self._sleep.start()
        server.POWER_ON_VERIFY_S = 0.01
        server._power_boot_until = 0.0
        server.POWER_RESCUE_INTERVAL_S = 0.01
        server.POWER_RESCUE_MAX_S = 0.05
        server._power_rescue_state = ""
        server._power_rescue_task = None

    def tearDown(self):
        self._sleep.stop()
        server.POWER_RESCUE_INTERVAL_S = 30.0
        server.POWER_RESCUE_MAX_S = 900.0

    def test_woke_on_third_round(self):
        cat = _FakeCat(fa_answer=True, fa_after_attempts=3)   # 3 次 PS1 后才回 FA
        asyncio.run(server._power_rescue(cat, None))
        self.assertEqual(server._power_rescue_state, "woke")
        self.assertEqual(cat.power_calls.count(True), 3)

    def test_timeout_when_radio_stays_silent(self):
        cat = _FakeCat(fa_answer=False)
        asyncio.run(server._power_rescue(cat, None))
        self.assertEqual(server._power_rescue_state, "timeout")
        self.assertFalse(server._power_rescue_running())

    def test_not_connected_round_writes_nothing(self):
        cat = _FakeCat(fa_answer=False)
        cat.connected = False
        asyncio.run(server._power_rescue(cat, None))
        self.assertEqual(cat.power_calls, [])

    def test_cancel_stops_further_ps1(self):
        cat = _FakeCat(fa_answer=False)
        server.POWER_RESCUE_MAX_S = 5.0

        async def cancel_soon():
            await asyncio.sleep(0)          # 让救援跑到第一轮
            server._power_rescue_cancel()
        async def both():
            task = asyncio.ensure_future(server._power_rescue(cat, None))
            await cancel_soon()
            await task
        asyncio.run(both())
        self.assertEqual(server._power_rescue_state, "cancelled")
        self.assertLessEqual(cat.power_calls.count(True), 1)
```

- [ ] **步骤 2：运行，确认失败**（`no attribute '_power_rescue'`）
- [ ] **步骤 3：实现**

```python
POWER_RESCUE_INTERVAL_S = 30.0    # 两轮之间至少间隔
POWER_RESCUE_MAX_S = 900.0        # 上限 15 分钟，到点报「需要现场按面板」
_power_rescue_task: Optional[asyncio.Task] = None
_power_rescue_state: str = ""     # "" | running | woke | timeout | cancelled
_power_rescue_attempts: int = 0


def _power_rescue_running() -> bool:
    return _power_rescue_state == "running"


async def _broadcast_power_rescue(state: str, **extra) -> None:
    payload = {"type": "powerRescue", "state": state,
               "attempt": _power_rescue_attempts, "maxSeconds": int(POWER_RESCUE_MAX_S)}
    payload.update(extra)
    await _broadcast_to_control(json.dumps(payload))   # 既有广播助手，名字按实际代码


async def _power_rescue(cat, backend=None) -> str:
    """Repeatedly try to wake the radio until it answers, the deadline passes, or it is cancelled.

    One round is the *already tested* `_power_on_radio` (PS1 x<=3, 1 Hz FA/CI-V
    read-back, boot window refreshed on every write) — this loop adds only the
    budget, the cancellation and the reporting.
    """
    global _power_rescue_state, _power_rescue_attempts
    _power_rescue_state, _power_rescue_attempts = "running", 0
    started = time.monotonic()
    await _broadcast_power_rescue("running")
    while True:
        if time.monotonic() - started >= POWER_RESCUE_MAX_S:
            _power_rescue_state = "timeout"
            logger.warning("Power rescue gave up after %d round(s) / %.0f s",
                           _power_rescue_attempts, time.monotonic() - started)
            await _broadcast_power_rescue("timeout")
            return "timeout"
        if not getattr(cat, "connected", False):
            # The radio's USB is gone (powered off).  Wait for the existing
            # watchdog to reconnect — never open the port from here.
            await asyncio.sleep(2.0)
            continue
        _power_rescue_attempts += 1
        await _broadcast_power_rescue("running")
        if await _power_on_radio(cat, backend):
            _power_rescue_state = "woke"
            logger.info("Power rescue woke the radio on round %d after %.0f s",
                        _power_rescue_attempts, time.monotonic() - started)
            await _broadcast_power_rescue("woke")
            return "woke"
        await asyncio.sleep(POWER_RESCUE_INTERVAL_S)


def _power_rescue_cancel() -> None:
    """Stop a rescue attempt.  Sends nothing to the radio."""
    global _power_rescue_state
    if _power_rescue_task is not None and not _power_rescue_task.done():
        _power_rescue_task.cancel()
    if _power_rescue_state == "running":
        _power_rescue_state = "cancelled"
```

`_power_rescue` 需把 `CancelledError` 当作 `cancelled` 收尾（在 `server.py` 的调用点
`except asyncio.CancelledError:` 里广播）。启动处（WS 分支）：

```python
elif field == "power":
    value_l = str(value).lower()
    if value_l == "restart":
        refusal = await _power_off(cat, radio, scheduler)
        if refusal:
            await ws.send_text(json.dumps({"type": "error", "message": refusal}))
        else:
            _power_rescue_cancel()
            _power_rescue_task = asyncio.create_task(_power_rescue(cat, backend), name="power_rescue")
    elif value_l == "cancelrescue":
        _power_rescue_cancel()
        await _broadcast_power_rescue("cancelled")
    elif on:
        ...（既有 true 分支不动）
    else:
        _power_rescue_cancel()                      # 「关机」永远保持关闭
        refusal = await _power_off(cat, radio, scheduler)
        if refusal:
            await ws.send_text(json.dumps({"type": "error", "message": refusal}))
```

- [ ] **步骤 4：运行** → `OK`；再跑一次全量离线套件确认无回归
- [ ] **步骤 5：commit**（`feat(power): 重启电台 = 有界可取消的持续唤醒，协议新增 restart/cancelRescue`）

## 任务 3：前端菜单、状态与缓存版本

**文件：** `static/index.html`、`static/ft710_ui.js`、`static/ft710_main.js`、`static/sw.js`

- [ ] **步骤 1：`index.html`** 在「接入云端」之后插两项（沿用现有缩进），状态行加
      `<span class="status-item" id="status-power">PWR --</span>`
- [ ] **步骤 2：`ft710_ui.js`** 加 `handleMenuAction` 两分支 + `renderPowerRescue(msg)` +
      `status-power` 点击（确认后 `sendCommand({type:'set',field:'power',value:'cancelRescue'})`）；
      listen 角色（`radioRole === 'listen'`）不渲染两项
- [ ] **步骤 3：`ft710_main.js`** `onmessage` 加 `case "powerRescue":` → `renderPowerRescue(msg)`；
      `power_on` 已在 state 里，`renderPower()` 更新 `PWR 开/关`
- [ ] **步骤 4：缓存版本** `ft710_main.js?v=43→44`、`ft710_ui.js?v=36→37`、`sw.js` `mrrc-v52→v53`
      与预缓存清单
- [ ] **步骤 5：运行** 相关 pin 测试并同步（`tests/test_server_ws_protocol.py`、
      `tests/test_spectrum_profile_server.py`、`tests/test_audio.py` 的 stale 列表）
- [ ] **步骤 6：commit**（`feat(ui): ☰ 菜单里的重启电台/关机 + 顶栏 PWR 状态与取消入口`）

## 任务 4：角色门与前端守卫测试

- [ ] **步骤 1：写测试** —— listen token 发 `field:"power"` 四个取值全部被拒
      （沿用 `tests/test_listen_only.py` 的 `_FakeWS`/token 夹具）；`index.html` 含两项与
      `status-power`；`handleMenuAction` 含两分支；缓存版本三处一致且旧值不在
- [ ] **步骤 2：运行** → 通过；`tests/README.md` 计数更新（新增测试数见实际读数）
- [ ] **步骤 3：commit**

## 任务 5：文档同步（规格 §9 清单）

- [ ] `SDD/10-service-model.md` 的 `power` 行：值域四种 + 救援语义 + 守卫
- [ ] `SDD/14-version-history.md`：新版本条目（含 V1–V7 结果；真机未跑前写"待真机"）
- [ ] `docs/OPERATION_GUIDE.md`：第 16 行免责句改写 + ⏻ 行补注 + §6.x 新增「电台电源」小节；
      `python3 website/build_guide.py` 重生成两个 guide 页
- [ ] `CHANGELOG.md` / `AGENTS.md` / `tests/README.md` / `SDD/README.md` Quick Facts
- [ ] `release_check.py` 离线 + 全量套件绿 → commit

## 任务 6：真机验收 V1–V7（人工，操作员执行）

- [ ] V1 关机 / V2 重启（记录第几轮醒、耗时）/ V3 取消 / V4 救援中关机（窗口内外）
      / V5 TX 与 FT8 发射中 / V6 物理断电后的失败路径 / V7 listen 角色
- [ ] 把读数回填 `CHANGELOG.md` 与 `SDD/14`；**若 V2/V6 证明 PS1 根本唤不醒 → 按规格 §8
      逃生舱只发布「关机…」一项**
