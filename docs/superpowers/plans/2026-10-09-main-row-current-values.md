# 主界面快捷行「显示当前值」实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 主界面快捷行 5 个按钮（MODE / BAND / FILTER / ATT / PRE）从「显示下一个值」改为「显示当前值」，ATT/PRE 使用完整标签（`OFF`/`6dB`/`12dB`/`18dB`、`OFF`/`AMP1`/`AMP2`），点击轮转行为不变——与 Android 客户端 `MainScreen.kt:81-82` 语义一致。

**架构：** 纯前端改动。`static/ft710_ui.js` 的 `renderButtonLabels()` 改渲染当前索引/当前名；标签由 capabilities 的 `att_steps`/`preamp_steps` 派生（无 caps 时回退 FT-710 常量表），因此点击后的乐观渲染立即显示新值。缓存版本 `ft710_ui.js?v=34→35`、SW `mrrc-v47→v48`，并同步 5 个测试文件的钉点。

**技术栈：** 经典 `<script>` 前端（JS，无构建步骤）、Python `unittest`、Node `--check` 语法校验。

**设计文档：** `docs/superpowers/specs/2026-10-09-main-row-current-values-design.md`（下称"规格"；§N 指该文件章节）

---

## 全局约束（每个任务都适用，违反即返工）

1. **必须用仓库虚拟环境**：主检出 `/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/python -m unittest ...`。系统 `python3` 缺依赖；worktree 里没有 `.venv`，用主检出的解释器 + worktree 的 cwd。
2. **基线（2026-10-09 主检出 main 实测）**：`Ran 1751 tests in 35.116s` → `OK (skipped=1)`。完成后应为 `Ran 1752 tests` → `OK (skipped=1)`（本计划新增 1 个契约测试）。任何新增 FAIL/ERROR 都算返工。
3. **提交纪律**：主检出当前有 8 个与本任务无关的脏文件（`atr1000_tuner.json`、`docs/w103d_pack.md`、`mem_channels.json`、`packaging/box/box-overlay.sh`、`packaging/box/verify.sh`、`start.sh`、`website/images/promo_poster.jpg` 等）和若干未跟踪文件。**永远不要 `git add -A` / `git commit -a`**；每次只 `git add` 本任务明确列出的文件。在专用 worktree 中执行则天然隔离。
4. **受工具守卫保护的 legacy 文件**：`static/ft710_ui.js` 是 4 空格/单引号手写风格，**不要跑 biome 格式化或任何自动修复**；用精确文本替换编辑；改完跑 `node --check`。
5. **每次 commit 前**：`python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged`，必须输出 `SDD-GUARDIAN: clean — no constraint violations.`（exit 0）。
6. **不做计划外改动**：服务端、WebSocket 协议、`fullState`、Android/iOS、`/listen` 页一律不碰；`ft710_main.js` 不动（其缓存版本 `v=39` 保持）。

---

## 执行环境（任务 0，可选但推荐）

在专用 worktree 中执行，避免主检出脏文件混入提交：

- [ ] **步骤 0.1：创建工作区**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern
git worktree add .worktrees/main-row-current-values -b feat/main-row-current-values main
cd .worktrees/main-row-current-values
```

- [ ] **步骤 0.2：验证工作区干净且包含规格提交**

```bash
git status --short
git log --oneline -1
```

预期：`git status --short` 无输出；`git log` 显示 `f854c44 docs: 主界面快捷行显示当前值设计——...`。

> 后续所有命令的 cwd 均为 `/Users/cheenle/HAM/hub/mrrc_modern/.worktrees/main-row-current-values`，Python 解释器固定用 `/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/python`。若操作者选择直接在主检出执行，跳过任务 0，并严格遵守全局约束 3。

---

## 文件结构

| 文件 | 职责 / 改动 |
| --- | --- |
| `static/ft710_ui.js`（修改） | 新增 `_attFullLabel`/`_preFullLabel` 替换 `_attShortLabel`/`_preShortLabel`；`renderButtonLabels()` 改为渲染当前值 |
| `static/index.html`（修改） | 第 580 行缓存版本 `ft710_ui.js?v=34 → v=35` |
| `static/sw.js`（修改） | 第 2 行 `CACHE 'mrrc-v47' → 'mrrc-v48'`；第 8 行 `'/ft710_ui.js?v=34' → v=35'` |
| `tests/test_server_ws_protocol.py`（修改） | 新增当前值契约测试；更新 3 处缓存钉点（234/237/239 行） |
| `tests/test_spectrum_profile_server.py`（修改） | 更新 3 处缓存钉点（434/436/438 行） |
| `tests/test_audio.py`（修改） | 更新 SW CACHE 钉点（151 行） |
| `tests/test_tx_liveness.py`（修改） | 更新 SW CACHE 钉点（345 行） |
| `tests/test_ws_token_transport.py`（修改） | 更新 SW CACHE 钉点（184 行） |

---

## 任务 1：先写失败的契约测试（红）

**文件：**

- 修改：`tests/test_server_ws_protocol.py`（在 `StateBroadcastLogicTests` 类内，第 213 行 `test_state_update_renders_from_actual_fields_not_dirty_only` 结束后、第 215 行 `test_civ_scope_speed_selector_uses_capabilities` 之前插入）

- [ ] **步骤 1.1：插入契约测试**

在 `tests/test_server_ws_protocol.py` 第 213 行（`self.assertIn("renderUpdates(changedFields);", main_source)`）与第 215 行（`def test_civ_scope_speed_selector_uses_capabilities(self):`）之间插入：

```python
    def test_quick_row_shows_current_values_not_next(self):
        """2026-10-09 spec §4: the quick row shows the radio's current
        state (Android parity); ATT/PRE use the full capability labels."""
        ui_source = Path("static/ft710_ui.js").read_text(encoding="utf-8")
        block = ui_source.split("function renderButtonLabels()", 1)[1]
        block = block.split("function renderToggles()", 1)[0]
        self.assertIn("setText('btn-mode', modeName);", block)
        self.assertIn("setText('btn-band', bandName);", block)
        self.assertIn("getFilterLabel(radioState.filter_width, modeName)", block)
        self.assertIn("setText('btn-att', _attFullLabel(radioState.attenuator));", block)
        self.assertIn("setText('btn-pre', _preFullLabel(radioState.preamp));", block)
        self.assertNotIn("nextAtt", block)
        self.assertNotIn("nextPre", block)
        self.assertIn("function _attFullLabel(idx)", ui_source)
        self.assertIn("function _preFullLabel(idx)", ui_source)
        self.assertNotIn("_attShortLabel", ui_source)
        self.assertNotIn("_preShortLabel", ui_source)
```

- [ ] **步骤 1.2：运行测试，确认失败**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern/.worktrees/main-row-current-values
/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/python -m unittest discover -s tests \
  -p "test_server_ws_protocol.py" -k "test_quick_row_shows_current_values_not_next" -v
```

预期：`FAIL: test_quick_row_shows_current_values_not_next`，`AssertionError: 'setText(\'btn-mode\', modeName);' not found`（当前实现是 `setText('btn-mode', nextMode);`），末尾 `FAILED (failures=1)`。

---

## 任务 2：实现当前值渲染并让测试变绿

**文件：**

- 修改：`static/ft710_ui.js:367-399`（两个标签函数）
- 修改：`static/ft710_ui.js:401-428`（`renderButtonLabels`）
- 测试：`tests/test_server_ws_protocol.py`（任务 1 已加）

- [ ] **步骤 2.1：替换 `_attShortLabel` / `_preShortLabel` 为完整标签函数**

把 `static/ft710_ui.js` 中第 367-399 行整段：

```js
// ATT/PRE cycle lengths come from capabilities (att_steps/preamp_steps)
// when present; the FT-710 4-step / 3-step cycles are the fallback.
// Derived short labels reproduce the legacy FT-710 strings exactly.
function _attStepCount() {
    const c = _caps();
    return (c && Array.isArray(c.att_steps) && c.att_steps.length) || 4;
}

function _attShortLabel(idx) {
    const c = _caps();
    if (c && Array.isArray(c.att_steps) && c.att_steps.length) {
        const db = c.att_steps[idx];
        if (!db) return 'OF';
        return db < 10 ? db + 'd' : String(db);   // ≤3 chars, legacy style
    }
    return {0:'OF', 1:'6d', 2:'12', 3:'18'}[idx];
}

function _preStepCount() {
    const c = _caps();
    return (c && Array.isArray(c.preamp_steps) && c.preamp_steps.length) || 3;
}

function _preShortLabel(idx) {
    const c = _caps();
    if (c && Array.isArray(c.preamp_steps) && c.preamp_steps.length) {
        const name = String(c.preamp_steps[idx] || 'OFF');
        if (name === 'OFF') return 'OF';
        const m = name.match(/(\d+)/);   // AMP1 -> A1, AMP2 -> A2
        return m ? 'A' + m[1] : name.slice(0, 3);
    }
    return {0:'OF', 1:'A1', 2:'A2'}[idx];
}
```

替换为：

```js
// ATT/PRE cycle lengths come from capabilities (att_steps/preamp_steps)
// when present; the FT-710 4-step / 3-step cycles are the fallback.
function _attStepCount() {
    const c = _caps();
    return (c && Array.isArray(c.att_steps) && c.att_steps.length) || 4;
}

// Full current-step labels for the quick row, derived from capabilities
// so a tap renders the new value immediately (optimistic render); the
// Android client shows the same strings from the server's *_label.
function _attFullLabel(idx) {
    const c = _caps();
    if (c && Array.isArray(c.att_steps) && c.att_steps.length) {
        const db = c.att_steps[idx];
        return db ? db + 'dB' : 'OFF';
    }
    return {0:'OFF', 1:'6dB', 2:'12dB', 3:'18dB'}[idx] || 'OFF';
}

function _preStepCount() {
    const c = _caps();
    return (c && Array.isArray(c.preamp_steps) && c.preamp_steps.length) || 3;
}

function _preFullLabel(idx) {
    const c = _caps();
    if (c && Array.isArray(c.preamp_steps) && c.preamp_steps.length) {
        return String(c.preamp_steps[idx] || 'OFF');
    }
    return {0:'OFF', 1:'AMP1', 2:'AMP2'}[idx] || 'OFF';
}
```

- [ ] **步骤 2.2：把 `renderButtonLabels()` 改为渲染当前值**

把第 401-428 行整段：

```js
function renderButtonLabels() {
    const modeName = radioState.mode_name;
    const nextMode = getNextMode(modeName);
    setText('btn-mode', nextMode);
    document.getElementById('btn-mode').dataset.current = modeName;

    const bandName = radioState.band_name;
    const nextBand = getNextBand(bandName);
    if (nextBand) {
        setText('btn-band', nextBand.name);
        document.getElementById('btn-band').dataset.current = bandName;
    }

    const filterIdx = radioState.filter_width;
    const nextIdx = getNextFilter(filterIdx, modeName);
    setText('btn-filter', getFilterLabel(nextIdx, modeName));
    document.getElementById('btn-filter').dataset.current = filterIdx;

    // ATT cycle: length/labels from capabilities.att_steps when present
    // (FT-710: OFF -> 6dB -> 12dB -> 18dB; IC-7300: OFF -> 20dB).
    const nextAtt = (radioState.attenuator + 1) % _attStepCount();
    setText('btn-att', _attShortLabel(nextAtt));

    // PRE cycle: length/labels from capabilities.preamp_steps when
    // present (OFF -> AMP1 -> AMP2 on both supported radios).
    const nextPre = (radioState.preamp + 1) % _preStepCount();
    setText('btn-pre', _preShortLabel(nextPre));
}
```

替换为：

```js
function renderButtonLabels() {
    // The quick row shows the radio's CURRENT state (a tap cycles to the
    // next step) — Android parity: the row answers "where am I now".
    const modeName = radioState.mode_name;
    setText('btn-mode', modeName);
    document.getElementById('btn-mode').dataset.current = modeName;

    const bandName = radioState.band_name;
    setText('btn-band', bandName);
    document.getElementById('btn-band').dataset.current = bandName;

    setText('btn-filter', getFilterLabel(radioState.filter_width, modeName));
    document.getElementById('btn-filter').dataset.current = radioState.filter_width;

    setText('btn-att', _attFullLabel(radioState.attenuator));
    setText('btn-pre', _preFullLabel(radioState.preamp));
}
```

> 注意：`getNextMode` / `getNextBand` / `getNextFilter` 仍被点击处理器（`initUI`，约 1339-1390 行）使用，**不要删**；`_attStepCount` / `_preStepCount` 仍被点击处理器使用，**不要删**。

- [ ] **步骤 2.3：JS 语法校验**

```bash
node --check static/ft710_ui.js
```

预期：无输出（exit 0）。

- [ ] **步骤 2.4：运行契约测试，确认通过**

```bash
/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/python -m unittest discover -s tests \
  -p "test_server_ws_protocol.py" -k "test_quick_row_shows_current_values_not_next" -v
```

预期：`Ran 1 test` → `OK`。

- [ ] **步骤 2.5：运行整个测试文件，确认无回归**

```bash
/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/python -m unittest discover -s tests \
  -p "test_server_ws_protocol.py" -v 2>&1 | tail -4
```

预期：末尾 `OK`（无 FAIL/ERROR；缓存钉点测试此刻仍指向 v34/v47，尚未冲突）。

- [ ] **步骤 2.6：sdd 守卫检查 + 提交**

```bash
git add static/ft710_ui.js tests/test_server_ws_protocol.py
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(ui): 快捷行改显当前值——ATT/PRE 用完整标签，点击仍轮转

- renderButtonLabels 渲染当前模式/波段/滤波/ATT/PRE，不再渲染下一个值
- _attFullLabel/_preFullLabel 由 capabilities 派生（OFF/6dB/12dB/18dB、OFF/AMP1/AMP2）
- 新增契约测试 test_quick_row_shows_current_values_not_next
- 规格：docs/superpowers/specs/2026-10-09-main-row-current-values-design.md"
```

预期：sdd 输出 `SDD-GUARDIAN: clean — no constraint violations.`；commit 成功（2 个文件）。

---

## 任务 3：缓存版本与测试钉点

**为什么单独一个提交**：钉点测试要求 `index.html` / `sw.js` 的版本号与测试断言严格一致，必须原子地一起改；与功能改动分开提交，回滚粒度清楚。

**文件：**

- 修改：`static/index.html:580`、`static/sw.js:2,8`
- 修改：`tests/test_server_ws_protocol.py:234,237,239`、`tests/test_spectrum_profile_server.py:434,436,438`、`tests/test_audio.py:151`、`tests/test_tx_liveness.py:345`、`tests/test_ws_token_transport.py:184`

- [ ] **步骤 3.1：更新静态资源版本**

`static/index.html` 第 580 行：

```html
    <script src="ft710_ui.js?v=34"></script>
```

改为：

```html
    <script src="ft710_ui.js?v=35"></script>
```

`static/sw.js` 第 2 行：

```js
const CACHE = 'mrrc-v47';
```

改为：

```js
const CACHE = 'mrrc-v48';
```

`static/sw.js` 第 8 行：

```js
    '/ft710_ui.js?v=34',
```

改为：

```js
    '/ft710_ui.js?v=35',
```

- [ ] **步骤 3.2：同步 5 个测试文件的钉点**

`tests/test_server_ws_protocol.py`（234/237/239 行，只动这三个字符串）：

```python
        self.assertIn('ft710_ui.js?v=35', index_source)
        self.assertIn("const CACHE = 'mrrc-v48'", sw_source)
        self.assertIn("'/ft710_ui.js?v=35'", sw_source)
```

`tests/test_spectrum_profile_server.py`（434/436/438 行）：

```python
        self.assertIn("ft710_ui.js?v=35", html)
        self.assertIn("const CACHE = 'mrrc-v48';", sw)
        self.assertIn("'/ft710_ui.js?v=35'", sw)
```

> 第 444 行 stale 列表 `("ft710_main.js?v=38", "ft710_ui.js?v=33", "mrrc-v46")` **不改**（它们检查的是旧值不出现）。

`tests/test_audio.py` 第 151 行：

```python
        self.assertIn("mrrc-v48", sw_source)
```

`tests/test_tx_liveness.py` 第 345 行：

```python
        self.assertIn("const CACHE = 'mrrc-v48'", sw)
```

`tests/test_ws_token_transport.py` 第 184 行（保留行尾注释）：

```python
        self.assertIn("const CACHE = 'mrrc-v48'", sw)   # SW cache moves whenever an asset does
```

- [ ] **步骤 3.3：运行全部钉点测试**

```bash
V=/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/python
$V -m unittest discover -s tests -p "test_server_ws_protocol.py" \
  -k "test_static_assets_are_cache_busted_after_ui_changes" -v
$V -m unittest discover -s tests -p "test_spectrum_profile_server.py" \
  -k "test_asset_versions_were_bumped" -v
$V -m unittest discover -s tests -p "test_spectrum_profile_server.py" \
  -k "test_no_stale_cache_pins_anywhere" -v
$V -m unittest discover -s tests -p "test_audio.py" \
  -k "test_tx_static_assets_are_cache_busted" -v
$V -m unittest discover -s tests -p "test_tx_liveness.py" \
  -k "test_cache_bust_covers_the_ptt_manager" -v
$V -m unittest discover -s tests -p "test_ws_token_transport.py" \
  -k "test_cache_bust_covers_the_assets_that_changed" -v
```

预期：6 条命令全部 `OK`。

- [ ] **步骤 3.4：确认没有遗留旧版本号**

```bash
grep -rn "ft710_ui.js?v=34\|mrrc-v47" static tests
```

预期：无输出（exit 1）。

- [ ] **步骤 3.5：sdd 守卫检查 + 提交**

```bash
git add static/index.html static/sw.js tests/test_server_ws_protocol.py \
  tests/test_spectrum_profile_server.py tests/test_audio.py \
  tests/test_tx_liveness.py tests/test_ws_token_transport.py
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "chore(ui): 快捷行改动的缓存版本与测试钉点——ft710_ui.js v34→v35、SW mrrc-v47→v48

只动 ft710_ui.js 相关钉点，ft710_main.js?v=39 保持不变。"
```

预期：sdd `clean`；commit 成功（7 个文件）。

---

## 任务 4：全量验证与收尾

- [ ] **步骤 4.1：全量测试**

```bash
cd /Users/cheenle/HAM/hub/mrrc_modern/.worktrees/main-row-current-values
/Users/cheenle/HAM/hub/mrrc_modern/.venv/bin/python -m unittest discover -s tests 2>&1 | tail -6
```

预期：`Ran 1752 tests ...` → `OK (skipped=1)`（1751 基线 + 1 新增契约测试；零新增 FAIL/ERROR）。

- [ ] **步骤 4.2：最终语法与残留检查**

```bash
node --check static/ft710_ui.js && echo "syntax ok"
grep -rn "_attShortLabel\|_preShortLabel" static/ft710_ui.js || echo "old helpers removed"
```

预期：`syntax ok`；`old helpers removed`。

- [ ] **步骤 4.3：人工验收（浏览器，需运行中的服务端/电台）[操作者检查]**

1. 打开主界面（连上电台后）：MODE/BAND/FILTER 显示的是当前状态（与状态栏一致，MODE 用 `mode_name` 原词如 `CW-U`）。
2. ATT 当前 OFF 时按钮显示 `OFF`；点击依次 `OFF → 6dB → 12dB → 18dB → OFF`，且**每次点击立即显示新值**。
3. PRE 显示 `OFF → AMP1 → AMP2 → OFF`。
4. IC-7300（或其它后端）下 ATT 显示 `OFF` / `20dB`（caps 驱动）。
5. 浏览器控制台无 JS 错误；服务端回推后显示不回跳。

> Agent 无浏览器/电台时**不得**声称此步通过，如实标注为操作者检查。

- [ ] **步骤 4.4：收尾（若在 worktree 中执行）**

使用 superpowers:finishing-a-development-branch 技能的流程走合并/PR/清理；**不要**在主检出有脏文件时直接 `git merge` 之外的任何批量操作。

---

## 规格覆盖度自检

| 规格 §4 行为 | 覆盖任务 |
| --- | --- |
| MODE 显示当前 `mode_name` | 任务 1（断言）/ 任务 2（代码） |
| BAND 显示当前 `band_name` | 同上 |
| FILTER 显示当前 `filter_width` 的标签 | 同上 |
| ATT 显示 `OFF/6dB/12dB/18dB`（caps 派生 + 回退） | 任务 2 步骤 2.1 |
| PRE 显示 `OFF/AMP1/AMP2`（caps 派生 + 回退） | 任务 2 步骤 2.1 |
| 点击轮转与乐观更新不变 | 任务 2 步骤 2.2（只改渲染；点击处理器不动） |
| `dataset.current` 保留 | 任务 2 步骤 2.2 |
| 缓存版本与 5 个测试文件钉点 | 任务 3 |
| `node --check` + 全量测试 | 任务 2 步骤 2.3 / 任务 4 步骤 4.1 |
| 非目标（服务端/协议/Android/listen 不碰） | 全局约束 6；文件结构只有 8 个文件 |
