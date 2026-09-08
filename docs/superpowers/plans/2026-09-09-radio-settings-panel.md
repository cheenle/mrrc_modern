# 电台设置面板（RF 功率滑块）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** ☰ 菜单「设置」打开「电台设置」模态面板，内含 RF PWR 滑块（5–100，松手才发 `rf_power` 命令，拖动中轮询回显被抑制）。

**架构：** 纯前端改动。面板 DOM 动态生成（仿 `showMemoryManager` 的自定义模态模式），复用已有 `sendCommand('rf_power')` 服务端链路（FT-710 `PC` 5–100W / IC-7300 CI-V level 0x0A pct↔raw），服务端零改动。规格：`docs/superpowers/specs/2026-09-09-radio-settings-panel-design.md`。

**技术栈：** 原生 JS（无框架/无构建）、unittest（服务端测试通过源码字符串断言覆盖前端 JS）、node --check 语法闸门。

**重要环境约束（macos-installer gotcha 10）：** 本机跑测试一律用 `.venv/bin/python`（绝对路径或先 `cd` 到仓库根），**禁止** `source .venv/bin/activate`（activate 硬编码指向 mrrc_ft710 项目）和裸 `python3`（无依赖，50 个 import error）。

---

## 文件结构

| 文件 | 职责 | 动作 |
| --- | --- | --- |
| `static/ft710_ui.js` | `showSettingsPanel()` 面板 + 菜单动作改向 + `renderSliders` 面板回显（拖动保护） | 修改 |
| `static/index.html` | 仅 `ft710_ui.js?v=28` → `?v=29` cache-bust | 修改 |
| `static/sw.js` | `CACHE 'mrrc-v29'→'mrrc-v30'` + ASSETS 里 `/ft710_ui.js?v=28`→`?v=29` | 修改 |
| `tests/test_server_ws_protocol.py` | 新增面板结构断言测试 + cache-bust 断言重钉（:219-227） | 修改 |
| `SDD/14-version-history.md` | V2.34 条目 | 修改 |
| `SDD/README.md` | SDD Version/Baseline Date/Status | 修改 |

不做的事（YAGNI，来自规格 §7）：不加 MIC GAIN/VOX、不唤醒旧 `#dsp-sliders`、不改服务端、不做瓦特/百分比单位换算显示。

---

### 任务 1：设置面板的失败测试（红）

**文件：**

- 修改：`tests/test_server_ws_protocol.py`（在 `test_static_assets_are_cache_busted_after_ui_changes` 之前插入新测试）

- [ ] **步骤 1：编写失败测试**

在 `test_civ_scope_speed_selector_uses_capabilities` 方法之后、`test_static_assets_are_cache_busted_after_ui_changes` 之前插入：

```python
    def test_settings_panel_exposes_rf_power_slider(self):
        """SDD V2.34: the ☰ settings action opens a radio-settings panel
        with the RF PWR slider; poll echo must not fight an in-flight drag."""
        ui_source = Path("static/ft710_ui.js").read_text(encoding="utf-8")
        self.assertIn("function showSettingsPanel(", ui_source)
        self.assertIn('id="slider-rfpower-p"', ui_source)
        self.assertIn("sendCommand('rf_power', v);", ui_source)
        self.assertIn("showSettingsPanel();", ui_source)
        self.assertIn("pSlider.dataset.dragging !== '1'", ui_source)
```

- [ ] **步骤 2：运行验证失败**

```bash
.venv/bin/python -m unittest tests.test_server_ws_protocol.TestWebSocketProtocol.test_settings_panel_exposes_rf_power_slider -v
```

预期：FAIL（`AssertionError: 'function showSettingsPanel(' not found`）。若测试类名不同，用 `-v` 列出后按实际类名运行。

### 任务 2：实现 showSettingsPanel + 菜单改向 + 拖动保护（绿）

**文件：**

- 修改：`static/ft710_ui.js`（三处：renderSliders ≈:416-425、handleMenuAction case 'settings' ≈:1707-1714、showMemoryManager 之后新增函数 ≈:1835）

- [ ] **步骤 1：renderSliders 增加面板回显（拖动保护）**

将现有 `renderSliders`（`function renderSliders() {` 到其闭合 `}`）整体替换为：

```javascript
function renderSliders() {
    // NOTE: the AF slider is browser-side playback volume (cookie),
    // NOT the radio's AF gain — it is intentionally not rendered from
    // radio state, or the CAT poll would fight the user's setting.
    setSlider('slider-rfpower', 'val-rfpower', radioState.rf_power);
    // Settings-panel slider: skip the echo while the user is dragging,
    // or the poll would snap the thumb back mid-drag.
    const pSlider = document.getElementById('slider-rfpower-p');
    if (pSlider && pSlider.dataset.dragging !== '1') {
        pSlider.value = radioState.rf_power;
        setText('val-rfpower-p', radioState.rf_power);
    }
    // RF Gain (RG 0-255) shown as 0-100% on the slider.
    setSlider('slider-rfgain', 'val-rfgain',
        Math.round((radioState.rf_gain ?? 255) / 255 * 100));
}
```

（原有注释与 rfgain 两行保持原样，新增的是中间 `pSlider` 一段。`getElementById` 在面板关闭时返回 null，天然 no-op。）

- [ ] **步骤 2：新增 showSettingsPanel 函数**

在 `showMemoryManager` 函数的闭合 `}` 之后（`// ── Haptic Feedback` 注释之前）插入：

```javascript
// ── Radio Settings Panel ────────────────────────────────────────────
function showSettingsPanel() {
    const existing = document.querySelector('.modal-overlay');
    if (existing) existing.remove();

    const initVal = radioState.rf_power || 100;
    const html =
        '<div class="modal-title">Radio Settings</div>' +
        '<div class="slider-row">' +
        '<span class="slider-label">RF PWR</span>' +
        '<input type="range" id="slider-rfpower-p" class="ft-slider" ' +
        'min="5" max="100" value="' + initVal + '" />' +
        '<span class="slider-val" id="val-rfpower-p">' + initVal + '</span>' +
        '</div>' +
        '<div style="font-size:11px;color:#999;margin:6px 2px 10px;">' +
        'FT-710: watts (5-100 W) &middot; IC-7300: percent</div>' +
        '<button class="modal-close" id="rsp-close">Close</button>';

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    const content = document.createElement('div');
    content.className = 'modal-content';
    content.innerHTML = html;
    overlay.appendChild(content);
    document.body.appendChild(overlay);

    const slider = document.getElementById('slider-rfpower-p');
    slider.dataset.dragging = '0';
    slider.addEventListener('input', function() {
        slider.dataset.dragging = '1';
        setText('val-rfpower-p', this.value);
    });
    slider.addEventListener('change', function() {
        slider.dataset.dragging = '0';
        setText('val-rfpower-p', this.value);
        const v = parseInt(this.value, 10);
        sendCommand('rf_power', v);
        radioState.rf_power = v;   // optimistic; next poll (3 s skip) corrects if needed
    });
    document.getElementById('rsp-close').addEventListener('click', function() { overlay.remove(); });
    overlay.addEventListener('click', function(e) { if (e.target === overlay) overlay.remove(); });
}
```

样式说明：`.modal-overlay/.modal-content/.modal-title/.modal-close`（ft710.css:413-427）与 `.slider-row/.slider-label/.ft-slider/.slider-val`（ft710.css:231-248）均已存在，无需新 CSS。语义：FT-710 = 瓦特，IC-7300 = 百分比（`set_rf_power` 两后端都是 5–100 尺度），标签中性 "RF PWR"。

- [ ] **步骤 3：菜单「设置」改向**

将 `handleMenuAction` 中现有 case（`case 'settings':` 到 `break;`）：

```javascript
        case 'settings':
            // Scroll to DSP panel
            const dspPanel = document.querySelector('.dsp-panel');
            if (dspPanel) dspPanel.scrollIntoView({behavior:'smooth'});
            break;
```

替换为：

```javascript
        case 'settings':
            showSettingsPanel();
            break;
```

- [ ] **步骤 4：语法检查**

```bash
node --check static/ft710_ui.js
```

预期：无输出（语法 OK）。

- [ ] **步骤 5：运行任务 1 的测试验证通过**

```bash
.venv/bin/python -m unittest tests.test_server_ws_protocol.TestWebSocketProtocol.test_settings_panel_exposes_rf_power_slider -v
```

预期：PASS。

- [ ] **步骤 6：Commit**

```bash
git add static/ft710_ui.js tests/test_server_ws_protocol.py
git commit -m "feat(ui): radio settings panel with RF power slider (SDD V2.34)"
```

### 任务 3：cache-bust 重钉（红→绿）

**文件：**

- 修改：`tests/test_server_ws_protocol.py:219-227`（`test_static_assets_are_cache_busted_after_ui_changes`）
- 修改：`static/index.html`（`ft710_ui.js?v=28` 引用，脚本标签在文件尾部）
- 修改：`static/sw.js:2`（CACHE 行）与 `static/sw.js:6`（ASSETS 数组 ui 行）

- [ ] **步骤 1：重钉测试断言（红）**

将 `test_static_assets_are_cache_busted_after_ui_changes` 方法体整体替换为：

```python
    def test_static_assets_are_cache_busted_after_ui_changes(self):
        index_source = Path("static/index.html").read_text(encoding="utf-8")
        self.assertIn('/ft710.css?v=24', index_source)
        self.assertIn('/ft710_main.js?v=27', index_source)
        self.assertIn('/ft710_ui.js?v=29', index_source)

        sw_source = Path("static/sw.js").read_text(encoding="utf-8")
        self.assertIn("const CACHE = 'mrrc-v30'", sw_source)
        self.assertIn("'/ft710_main.js?v=27'", sw_source)
        self.assertIn("'/ft710_ui.js?v=29'", sw_source)
```

运行：

```bash
.venv/bin/python -m unittest tests.test_server_ws_protocol.TestWebSocketProtocol.test_static_assets_are_cache_busted_after_ui_changes -v
```

预期：FAIL（index.html 还是 v=28）。

- [ ] **步骤 2：更新 index.html 与 sw.js（绿）**

`static/index.html`：`/ft710_ui.js?v=28` → `/ft710_ui.js?v=29`（仅此一处替换；`ft710_main.js?v=27` 与 `ft710.css?v=24` 不动）。

`static/sw.js` 第 2 行：`const CACHE = 'mrrc-v29';` → `const CACHE = 'mrrc-v30';`
`static/sw.js` ASSETS 数组：`'/ft710_ui.js?v=28',` → `'/ft710_ui.js?v=29',`（其余资产行不动）。

- [ ] **步骤 3：运行验证通过 + 全量回归**

```bash
.venv/bin/python -m unittest tests.test_server_ws_protocol.TestWebSocketProtocol.test_static_assets_are_cache_busted_after_ui_changes -v
.venv/bin/python -m unittest discover -s tests 2>&1 | tail -3
```

预期：单测 PASS；全量 `Ran 694 tests ... OK`（693 + 新增 1）。

- [ ] **步骤 4：Commit**

```bash
git add static/index.html static/sw.js tests/test_server_ws_protocol.py
git commit -m "chore(ui): cache-bust ft710_ui.js v=29 / sw mrrc-v30 after settings panel"
```

### 任务 4：SDD 文档同步

**文件：**

- 修改：`SDD/14-version-history.md`（表头下第一行）
- 修改：`SDD/README.md`（Quick Facts 表：SDD Version / Baseline Date / Status 三行）

- [ ] **步骤 1：SDD/14-version-history.md 新增条目**

在 `| Version | Date | Author | Changes |` 表头分隔行之后插入：

```markdown
| SDD V2.34 | 2026-09-09 | Kimi | **Radio settings panel with RF power slider (frontend-only).** The ☰ menu "Settings" action now opens a "Radio Settings" modal (`showSettingsPanel`, ft710_ui.js, modeled on the memory manager's custom-content modal) instead of scrolling to the hidden DSP sliders block. It exposes the RF PWR slider (5–100; FT-710 = watts via `PC`, IC-7300 = percent via CI-V level 0x0A pct↔raw) reusing the existing `rf_power` server route — no server changes. `input` updates the readout only; `change` (release) sends `sendCommand('rf_power', v)` so a drag cannot flood the serial port, and `renderSliders` skips the poll echo while `slider-rfpower-p` is mid-drag (`dataset.dragging` guard — `setSlider` itself stays generic). The dormant hidden `#dsp-sliders` block is left untouched. Cache-bust `ft710_ui.js?v=28→29`, service worker `mrrc-v29→mrrc-v30` (`ft710_main.js?v=27` unchanged); cache-bust assertions re-pinned in `test_server_ws_protocol.py` (+1 new panel test → 694 tests green); JS syntax verified via `node --check`. Real-radio acceptance (slider → radio power readout, both models) remains an operator check. |
```

- [ ] **步骤 2：SDD/README.md Quick Facts 更新**

三行替换：

- `| SDD Version | V2.33 |` → `| SDD Version | V2.34 |`
- `| Baseline Date | 2026-09-09 |` 保持（同日）
- `| Status | ...` 行首追加一句（其余不动）：`Radio-settings panel with RF power slider shipped (frontend-only, SDD V2.34);`（接在 `| Status |` 之后、原有 `v1.14.0 is the latest published release...` 之前）

- [ ] **步骤 3：SDD 守护检查 + Commit**

```bash
git add SDD/14-version-history.md SDD/README.md
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged   # 必须 exit 0
git commit -m "docs(sdd): V2.34 — radio settings panel with RF power slider"
```

---

## 自检记录

1. **规格覆盖度**：规格 §2 交互（面板/滑块/拖动不连发/回显抑制/未连接静默/旧区块不动）→ 任务 2；§4 cache-bust+测试 → 任务 1、3；§5 SDD 同步 → 任务 4；§6 手动验收 → 留操作员（规格已注明）。无遗漏。
2. **占位符扫描**：所有代码步骤含完整代码与精确替换锚点；命令含预期输出。无 TODO/待定。
3. **类型/命名一致性**：`slider-rfpower-p`/`val-rfpower-p`/`rsp-close`/`pSlider.dataset.dragging`/`showSettingsPanel()` 在任务 1 测试断言、任务 2 实现、任务 3 cache-bust 间一致；`sendCommand`/`setText`/`radioState` 为 ft710_main.js 全局（ft710_ui.js 已有调用先例）。
