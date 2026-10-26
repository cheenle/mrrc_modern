# 电台参数弹窗 SQL（静噪）滑杆 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [x]`）语法来跟踪进度。

**目标：** 在 ☰ →「电台参数」的「电台高级设置」弹窗里补一条 SQL（静噪）滑杆，量程 0–100，值记 cookie，
Icom 既有的 5 s 回读经 `renderSliders()` 回显。

**架构：** 纯前端。服务端 `squelch` 分支、三个后端的 `set_squelch`、`radio_state.squelch` 广播都已存在，
一行不改。滑杆用弹窗里既有的 `addSlider()` 生成；`sendCommand('squelch', v)` 只在 `change`（松手）时发。
同时把 `addSlider()` 里一直没人置位的 `dataset.dragging` 判据补成真的。

**技术栈：** 原生 JS（`static/ft710_ui.js`，无构建步骤）、Python `unittest`（源码级契约测试）、
`node --check`（语法门）。

**规格：** `docs/superpowers/specs/2026-10-26-radio-settings-squelch-design.md`

**准备（每个任务开始前都先跑）：**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
git status --short          # 期望：只剩 website/board/index.html（既有改动，不要提交）
```

---

## 文件结构

| 文件 | 责任 | 改动 |
| --- | --- | --- |
| `static/ft710_ui.js` | 弹窗生成（`showSettingsPanel`）、滑杆工厂（`addSlider`）、状态回显（`renderUpdates` / `renderSliders`） | 加 SQL 滑杆 + 拖动置位 + 回读同步 |
| `static/index.html` | 资源版本 pin | `ft710_ui.js?v=39` → `?v=40` |
| `static/sw.js` | SW 缓存号 + 预缓存清单 | `mrrc-v56` → `mrrc-v57`，`'/ft710_ui.js?v=40'` |
| `tests/test_server_ws_protocol.py` | 前端契约 + 缓存 pin | 新增 3 个测试，改 3 处 pin |
| `tests/test_spectrum_profile_server.py` | 缓存 pin | 改 3 处 pin |
| `tests/test_ws_token_transport.py` / `test_tx_liveness.py` / `test_menu_version.py` / `test_audio.py` | 缓存 pin | 各改 1 处 |
| `tests/README.md` | 套件总数 | 更新用例数 |

`server.py`、`backends/**`、`radio_state.py`、`listen`/`sdr` 页面、安卓/iOS：**不动**。

---

## 任务 1：SQL 滑杆进弹窗 + 把拖动守卫变成真的

**文件：**
- 修改：`static/ft710_ui.js:2253-2280`（`addSlider` 的两个监听器）、`:2245-2248`（说明文字）、
  `:2319-2325`（RF Gain 之后插入 SQL）
- 测试：`tests/test_server_ws_protocol.py`（在 `test_settings_panel_exposes_rf_power_slider` 之后）

- [x] **步骤 1：写失败测试**

在 `tests/test_server_ws_protocol.py` 里 `test_settings_panel_exposes_rf_power_slider` 方法之后
（`def test_static_assets_are_cache_busted_after_ui_changes` 之前）插入：

```python
    def test_settings_panel_exposes_squelch_slider(self):
        """电台参数弹窗里的 SQL（静噪）：0-100，松手才下发，值记 cookie。"""
        ui_source = Path("static/ft710_ui.js").read_text(encoding="utf-8")
        panel = ui_source.split("function showSettingsPanel(", 1)[1]
        panel = panel.split("// ── Haptic Feedback", 1)[0]
        self.assertIn("'slider-sql'", panel)
        self.assertIn("'val-sql'", panel)
        self.assertIn("sendCommand('squelch', v);", panel)
        self.assertIn("FT710Settings.setCookie('ft710_sql', String(v));", panel)
        # 不回读的机型靠 cookie 记住最后下发的值，初值必须夹在 0..100。
        self.assertIn("FT710Settings.getCookie('ft710_sql')", panel)
        self.assertIn("Math.max(0, Math.min(100, sqlValue))", panel)

    def test_settings_slider_drag_guard_is_actually_set(self):
        """`dataset.dragging` 判据必须有人置位，否则轮询会把拖动的拇指弹回去。"""
        ui_source = Path("static/ft710_ui.js").read_text(encoding="utf-8")
        block = ui_source.split("function addSlider(", 1)[1]
        block = block.split("return slider;", 1)[0]
        self.assertIn("slider.dataset.dragging = '1';", block)
        self.assertIn("delete slider.dataset.dragging;", block)
```

- [x] **步骤 2：运行测试验证失败**

```bash
.venv/bin/python -m unittest tests.test_server_ws_protocol.ServerWSProtocolTests.test_settings_panel_exposes_squelch_slider tests.test_server_ws_protocol.ServerWSProtocolTests.test_settings_slider_drag_guard_is_actually_set -v
```

（类名以文件里实际的类名为准；不确定就跑整模块：
`.venv/bin/python -m unittest tests.test_server_ws_protocol -v 2>&1 | tail -20`）

预期：2 个 FAIL，报 `AssertionError: "'slider-sql'" not found` / `"slider.dataset.dragging = '1';" not found`。

- [x] **步骤 3：实现——`addSlider` 的拖动守卫**

把 `static/ft710_ui.js` 里这两行（`addSlider` 尾部）：

```js
        slider.addEventListener('input', function() {
            setText(valId, fmt ? fmt(parseInt(this.value, 10)) : this.value);
        });
        slider.addEventListener('change', function() { onCommit(parseInt(this.value, 10)); });
```

替换为：

```js
        slider.addEventListener('input', function() {
            // 拖动中：让 renderSliders() 的回显让路。这个判据以前只被读、
            // 从来没人置位，于是轮询随时能把正在拖的拇指弹回去。
            slider.dataset.dragging = '1';
            setText(valId, fmt ? fmt(parseInt(this.value, 10)) : this.value);
        });
        slider.addEventListener('change', function() {
            delete slider.dataset.dragging;
            onCommit(parseInt(this.value, 10));
        });
```

- [x] **步骤 4：实现——SQL 滑杆**

4a. 弹窗说明文字，把

```js
    blurb.textContent = '日常操作之外的东西都在这里：降噪/消噪电平、发射功率、增益。';
```

改成

```js
    blurb.textContent = '日常操作之外的东西都在这里：降噪/消噪电平、发射功率、增益、静噪。';
```

4b. 在 RF Gain 那条 `addSlider(...)` 调用**之后**（`addSlider('slider-micgain', ...)` 之前）插入：

```js
    // SQL（静噪）：0 = 常开，100 = 最强门限。服务端统一按 0-100 收
    // （FT-710/Yaesu 发 `SQ0xxx`，Icom 在此之上映射到 CI-V 0-255）。
    // Yaesu 机型不回读，所以初值优先取本机上次下发的值（cookie），
    // 没有再退到 radioState —— 与 Mic Gain 同一种处理。
    let sqlValue = 0;
    try {
        const storedSql = FT710Settings.getCookie('ft710_sql');
        const parsedSql = storedSql === null ? NaN : parseInt(storedSql, 10);
        if (!isNaN(parsedSql)) sqlValue = parsedSql;
        else if (typeof radioState.squelch === 'number') sqlValue = radioState.squelch;
    } catch (e) {
        sqlValue = typeof radioState.squelch === 'number' ? radioState.squelch : 0;
    }
    sqlValue = Math.max(0, Math.min(100, sqlValue));
    radioState.squelch = sqlValue;   // UI 与状态从此一致，回显不会互相打架

    addSlider('slider-sql', 'val-sql', 'SQL', 0, 100, sqlValue,
              '静噪门限：0 = 常开，越大越静。Yaesu 机型不回读，显示的是最后下发的值',
              function(v) {
                  sendCommand('squelch', v);
                  radioState.squelch = v;   // 乐观更新；Icom 的 5 s 回读会纠正
                  FT710Settings.setCookie('ft710_sql', String(v));
              });
```

- [x] **步骤 5：语法门**

```bash
node --check static/ft710_ui.js && echo "JS OK"
```

预期：`JS OK`（无输出即无语法错误）。

- [x] **步骤 6：运行测试验证通过**

```bash
.venv/bin/python -m unittest tests.test_server_ws_protocol -v 2>&1 | tail -20
```

预期：3 个新/旧 settings/panel 相关测试全 PASS；该模块 0 失败。

- [x] **步骤 7：Commit**

```bash
git add static/ft710_ui.js tests/test_server_ws_protocol.py
git commit -m "feat(ui): SQL squelch slider in the radio settings panel

The server has accepted \`squelch\` (0-100, clamped, broadcast in fullState and
stateUpdate) since the settings panel existed, and all three backends implement
set_squelch -- the interface simply never had a knob for it.

The slider lives in the radio-settings dialog next to RF Gain, seeds from the
ft710_sql cookie (then radioState), commits on release, and keeps the cookie in
step.  Seeding also writes radioState so the models without a read-back never
disagree with what the thumb shows.

addSlider's dataset.dragging guard was read but never set, so a poll could
snap the thumb back mid-drag; input now sets it and change clears it."
```

---

## 任务 2：Icom 的 5 s 回读要能走到滑杆上

**文件：**
- 修改：`static/ft710_ui.js`（`renderUpdates` 的 `settingsFields` 与 `needSliders` 分支、`renderSliders` 末尾）
- 测试：`tests/test_server_ws_protocol.py`

为什么需要：`squelch` 不在 `settingsFields` 里，`stateUpdate {squelch: n}` 到前端后
`renderUpdates` 不会调 `renderSliders()`，滑杆就永远停在打开时的值。

- [x] **步骤 1：写失败测试**

在 `test_settings_slider_drag_guard_is_actually_set` 之后插入：

```python
    def test_squelch_readback_reaches_the_slider(self):
        """Icom 的 5s 回读走 stateUpdate -> renderUpdates -> renderSliders。"""
        ui_source = Path("static/ft710_ui.js").read_text(encoding="utf-8")
        self.assertIn("'auto_notch', 'compressor', 'tuner_status', 'rf_power', 'squelch',",
                      ui_source)
        self.assertIn("if (['rf_power', 'rf_gain', 'squelch'].includes(f)) needSliders = true;",
                      ui_source)
        render = ui_source.split("function renderSliders()", 1)[1]
        render = render.split("function setSlider(", 1)[0]
        self.assertIn("document.getElementById('slider-sql')", render)
        self.assertIn("sqlSlider.dataset.dragging !== '1'", render)
```

- [x] **步骤 2：运行测试验证失败**

```bash
.venv/bin/python -m unittest tests.test_server_ws_protocol -v 2>&1 | grep -E "squelch_readback|FAILED"
```

预期：`test_squelch_readback_reaches_the_slider` FAIL（`"'auto_notch', 'compressor',
'tuner_status', 'rf_power', 'squelch'," not found`）。

- [x] **步骤 3：实现——`renderUpdates`**

`static/ft710_ui.js` 的 `settingsFields` 数组，把

```js
        'auto_notch', 'compressor', 'tuner_status', 'rf_power',
```

改成

```js
        'auto_notch', 'compressor', 'tuner_status', 'rf_power', 'squelch',
```

再把

```js
            if (['rf_power', 'rf_gain'].includes(f)) needSliders = true;
```

改成

```js
            if (['rf_power', 'rf_gain', 'squelch'].includes(f)) needSliders = true;
```

- [x] **步骤 4：实现——`renderSliders`**

在 `renderSliders()` 的 RF Gain 那两行**之后**、函数结尾 `}` 之前插入：

```js
    // SQL：0 = 常开。Yaesu 不回读，state 只会在我们下发或别的设备下发时变；
    // Icom 每 5 s 回读一次，面板旋钮动过这里会跟上（拖动中让路）。
    const sqlSlider = document.getElementById('slider-sql');
    const sqlState = typeof radioState.squelch === 'number' ? radioState.squelch : null;
    if (sqlSlider && sqlState !== null && sqlSlider.dataset.dragging !== '1' &&
        Number(sqlSlider.value) !== sqlState) {
        setSlider('slider-sql', 'val-sql', sqlState);
        try {
            FT710Settings.setCookie('ft710_sql', String(sqlState));
        } catch (e) { /* cookie 写不进去不影响控制 */ }
    }
```

- [x] **步骤 5：语法门 + 跑测试**

```bash
node --check static/ft710_ui.js && .venv/bin/python -m unittest tests.test_server_ws_protocol -v 2>&1 | tail -10
```

预期：`node --check` 静默通过；模块全 PASS。

- [x] **步骤 6：Commit**

```bash
git add static/ft710_ui.js tests/test_server_ws_protocol.py
git commit -m "feat(ui): let the squelch read-back reach the slider

squelch was missing from renderUpdates' settingsFields, so stateUpdate
{squelch} (the IC-7300 family polls it every 5 s) never triggered
renderSliders and an open dialog kept showing whatever it seeded with.

renderSliders now syncs the slider when the state and the thumb disagree,
skipping the sync while the operator is dragging, and follows the value into
the cookie so the next open starts from the radio's truth on Icom and from the
last commanded value on Yaesu."
```

---

## 任务 3：缓存号（漏了等于没发）

**文件：**
- 修改：`static/index.html:575`、`static/sw.js:2,8`
- 测试：`tests/test_server_ws_protocol.py:250-257`、`tests/test_spectrum_profile_server.py:448-463`、
  `tests/test_ws_token_transport.py:184`、`tests/test_tx_liveness.py:345`、
  `tests/test_menu_version.py:111`、`tests/test_audio.py:151`

- [x] **步骤 1：先把断言改成新 pin（失败测试）**

`tests/test_server_ws_protocol.py`：

```python
        self.assertIn('ft710_ui.js?v=39', index_source)          →  'ft710_ui.js?v=40'
        self.assertIn("const CACHE = 'mrrc-v56'", sw_source)     →  "'mrrc-v57'"
        self.assertIn("'/ft710_ui.js?v=39'", sw_source)          →  "'/ft710_ui.js?v=40'"
```

`tests/test_spectrum_profile_server.py`（`test_asset_versions_were_bumped`）：

```python
        self.assertIn("ft710_ui.js?v=39", html)                  →  "ft710_ui.js?v=40"
        self.assertIn("const CACHE = 'mrrc-v56';", sw)           →  "'mrrc-v57';"
        self.assertIn("'/ft710_ui.js?v=39'", sw)                 →  "'/ft710_ui.js?v=40'"
```

同文件 `test_no_stale_cache_pins_anywhere` 的 stale 元组里补两个旧值：

```python
                          "ft710_main.js?v=43", "ft710_ui.js?v=36", "mrrc-v52"):
```

改成

```python
                          "ft710_main.js?v=43", "ft710_ui.js?v=36", "mrrc-v52",
                          "ft710_ui.js?v=39", "mrrc-v56"):
```

`tests/test_ws_token_transport.py:184`、`tests/test_tx_liveness.py:345`、
`tests/test_menu_version.py:111`、`tests/test_audio.py:151`：把 `mrrc-v56` 改成 `mrrc-v57`。

- [x] **步骤 2：运行测试验证失败**

```bash
.venv/bin/python -m unittest tests.test_server_ws_protocol tests.test_spectrum_profile_server tests.test_ws_token_transport tests.test_tx_liveness tests.test_menu_version tests.test_audio 2>&1 | tail -6
```

预期：FAILED（`'ft710_ui.js?v=40' not found`、`"const CACHE = 'mrrc-v57'" not found` 等）。

- [x] **步骤 3：改静态资源**

3a. `static/index.html`：

```html
    <script src="ft710_ui.js?v=39"></script>
```

→

```html
    <script src="ft710_ui.js?v=40"></script>
```

3b. `static/sw.js`：

```js
const CACHE = 'mrrc-v56';
```

→

```js
const CACHE = 'mrrc-v57';
```

```js
    '/ft710_ui.js?v=39',
```

→

```js
    '/ft710_ui.js?v=40',
```

（`ft710.css?v=27` 与 `ft710_main.js?v=46` **不动**。）

- [x] **步骤 4：运行测试验证通过**

```bash
.venv/bin/python -m unittest tests.test_server_ws_protocol tests.test_spectrum_profile_server tests.test_ws_token_transport tests.test_tx_liveness tests.test_menu_version tests.test_audio 2>&1 | tail -6
```

预期：`OK`。

- [x] **步骤 5：Commit**

```bash
git add static/index.html static/sw.js tests/test_server_ws_protocol.py tests/test_spectrum_profile_server.py tests/test_ws_token_transport.py tests/test_tx_liveness.py tests/test_menu_version.py tests/test_audio.py
git commit -m "chore(ui): cache-bust after the settings-panel change

index.html is cache-first and sw.js is served from the SW cache, so a missed
pin is how a box keeps running yesterday's bundle: ft710_ui.js?v=40 and
mrrc-v57, with the old values moved into the stale-pin test's list."
```

---

## 任务 4：全量套件 + 计数 + 收尾

**文件：**
- 修改：`tests/README.md`（第 8 行与"Test Results Summary"表）

- [x] **步骤 1：跑全量**

```bash
.venv/bin/python -m unittest discover -s tests 2>&1 | tail -6
```

预期：`Ran NNNN tests ... OK (skipped=1)`。当前基线是 `2991`，本计划新增 3 个测试 → 期望 `2994`
（若与实测不符，**以实测为准**，不要照抄计划里的数字）。

- [x] **步骤 2：更新 `tests/README.md`**

- 第 8 行 `2991 tests across 152 test modules ...` → 实测用例数，模块数不变（`152`）。
- 表格 `| Total tests | 2991 |` → 实测值。
- 表格 `| Passed | 2990 (plus 1 skipped) |` → `实测值-1 (plus 1 skipped)`。

- [x] **步骤 3：规矩检查**

```bash
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git status --short
```

预期：`check` 无 block 输出；`git status` 只剩 `website/board/index.html`（既有未提交改动，不碰）。

- [x] **步骤 4：Commit**

```bash
git add tests/README.md
git commit -m "test: re-read the suite totals after the SQL slider"
```

- [x] **步骤 5：人工烟测（没有电台也能做）**

```bash
MRRC_WEB_PASSWORD=smoke MRRC_SERIAL_PORT=/dev/null .venv/bin/python server.py &
sleep 6
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8888/index.html
curl -s http://127.0.0.1:8888/ft710_ui.js?v=40 | grep -c "slider-sql"
kill %1
```

预期：`200`；`grep -c` ≥ `1`（说明新 bundle 真的被服务端发出去了）。

浏览器里打开 `http://127.0.0.1:8888/` → ☰ →「电台参数」→ 弹窗第 5 条是 `SQL`，拖到 40 松手，
关掉再打开仍是 40（cookie 生效）。电台不在线时 `sendCommand` 会收到服务端 `error` toast——
这是既有行为，不是本改动的缺陷。

---

## 交接

发版（**不在本计划内**）：`CHANGELOG.md` 写 `## [镜像线 v1.28.11 · FP7]`，
`dev_tools/make_box_source.py --fixpack FP7` 造包，更新 `website/downloads/latest.json` 的 `boxLine`
（version/fixpack/url/sha256/size），SDD `14-version-history.md` 追加一行——四件事一次提交，
否则 `tests/test_release_artifacts.py::BoxLineFixPackTests` 会红。
