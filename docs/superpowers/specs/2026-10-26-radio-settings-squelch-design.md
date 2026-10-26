# 电台参数弹窗里的 SQL（静噪）滑杆 设计

- **日期**：2026-10-26
- **状态**：待实现
- **范围**：`static/ft710_ui.js` 的「电台高级设置」弹窗 + `static/index.html` / `static/sw.js` 的缓存号 + 相关测试
- **相关代码**：`static/ft710_ui.js`（`showSettingsPanel` / `renderSliders` / `renderUpdates`）、
  `static/index.html`、`static/sw.js`
- **相关文档**：`CHANGELOG.md`、SDD `14-version-history.md`（发版时写，见 §8）
- **一句话**：服务端早就收 `squelch`，广播里也早有这个字段，只有界面上一直没有它的旋钮——补上。

---

## 1. 现状（读代码得到的事实）

| 位置 | 事实 |
| --- | --- |
| `server.py:3202` | `field == "squelch"` → `max(0, min(100, int(value)))` → `cat.set_squelch(v)` → `radio.update(squelch=v)`。**已存在，无需改动** |
| `radio_state.py:105` | `squelch: int = 0  # SQ0; 0-100`，且进 `to_dict()`，因此 `fullState` 与 `stateUpdate` 都带它 |
| `backends/ft710/cat_controller.py:619` | `set_squelch` → `SQ0{value:03d}`（FT-710 CAT 手册：P2 = 000–100） |
| `backends/yaesu/cat_core.py:584` | 同上，Yaesu 家族共用（`SQ` 在 FTX-1 一致性审计中是 ✓，`docs/ftx1-technical-analysis.md:321`） |
| `backends/ic7300/civ_controller.py:900` | `_level_set(LVL_SQL, pct_to_raw)` —— 0–100 ↔ CI-V 0–255 |
| `backends/ic7300/backend.py:208` | `("squelch", self._slow_squelch)` —— **Icom 有 5 s 回读**（`_raw_to_pct` 回 0–100） |
| `backends/ft710/backend.py:181` / `backends/yaesu/backend.py:143` | 轮询列表里**没有** `SQ` —— Yaesu 侧不回读 |
| `static/ft710_ui.js` | 没有任何 SQL 控件；`radioState.squelch` 只在 `ft710_main.js:402` 有个默认 0 |
| `static/index.html:381` | ☰ → 「电台参数」→ `showSettingsPanel()`（标题「电台高级设置」） |

结论：这是**纯前端补一个控件**的改动，服务端与三个后端一行都不用改。

## 2. 决策（用户拍板）

| # | 决策 | 理由 |
| --- | --- | --- |
| D-1 | 只放进「电台参数」弹窗，不做主界面滑杆 | 静噪属于「调一次就不常动」的参数；主界面滑杆区已经很挤，手机上再加一条会压窄操作区 |
| D-2 | **不加** `SQ` 回读轮询（FT-710 / Yaesu 侧） | 省掉每 2 s 一条 CAT 查询；代价用文案说清楚（D-4）。Icom **已有的** 5 s 回读照用 |
| D-3 | 值记在 cookie `ft710_sql` | 与 `Mic Gain` 同一套；电台的 SQ 本身持久，重开页面看到上次下发的值比看 0 有用 |
| D-4 | 滑杆文案写明「Yaesu 机型显示最后下发的值」 | 不回读就必须承认它，不能让操作员以为面板旋钮动过这里会跟着变 |
| D-5 | 本次提交**不写 CHANGELOG 标题**，发版时写 `## [镜像线 v1.28.11 · FP7]` | 见 §8：标题必须与 `latest.json` 的 `boxLine` 对齐，而清单只有在造包之后才成立 |

## 3. 数据流

```
打开弹窗            cookie ft710_sql（合法整数 0..100）
                    → 否则 radioState.squelch
                    → 否则 0
                    → 同时写回 radioState.squelch（让状态与 UI 一致）

拖动 input          只更新读数（并且 dataset.dragging = '1'）
松手 change         sendCommand('squelch', v)
                    + radioState.squelch = v
                    + FT710Settings.setCookie('ft710_sql', v)
                    + dataset.dragging 清除

服务端 stateUpdate   {squelch: n}（Icom 的 5 s 回读，或另一台设备下发的值）
                    → renderUpdates 把 'squelch' 当 settings 字段 → renderSliders()
                    → 滑杆未在拖动且值不同 → 更新滑杆 + 更新 cookie
```

**为什么 Yaesu 上不会自相矛盾**：Yaesu 侧没有任何东西会改 `radioState.squelch`，除了我们自己下发
（乐观更新）和另一台浏览器下发的值。前者等于滑杆当前值（不触发同步），后者本来就该跟着变。
打开弹窗时把 cookie 的值写回 `radioState.squelch` 这一步，正是为了让"没回读"的机型不会出现
「状态 0 vs 滑杆 40」这种永久分歧。

## 4. 前端实现（`static/ft710_ui.js`）

### 4.1 `showSettingsPanel()`

- 顶部说明补一句：`…发射功率、增益、静噪。`
- 位置：**RF Gain 之后、Mic Gain 之前**（把 RX 侧的三条放一起）：

```
RF PWR | NR Level | NB Level | RF Gain | SQL | Mic Gain | Mic Vol
```

```js
let sqlValue = 0;
try {
    const stored = FT710Settings.getCookie('ft710_sql');
    const parsed = stored === null ? NaN : parseInt(stored, 10);
    if (!isNaN(parsed)) sqlValue = parsed;
    else if (typeof radioState.squelch === 'number') sqlValue = radioState.squelch;
} catch (e) { sqlValue = radioState.squelch || 0; }
sqlValue = Math.max(0, Math.min(100, sqlValue));
radioState.squelch = sqlValue;          // UI 与状态从此一致

addSlider('slider-sql', 'val-sql', 'SQL', 0, 100, sqlValue,
          '静噪门限：0 = 常开，越大越静。Yaesu 机型不回读，显示的是最后下发的值',
          function(v) {
              sendCommand('squelch', v);
              radioState.squelch = v;
              FT710Settings.setCookie('ft710_sql', String(v));
          });
```

### 4.2 `renderUpdates()`

```js
// settingsFields 里加 'squelch'
if (['rf_power', 'rf_gain', 'squelch'].includes(f)) needSliders = true;
```

### 4.3 `renderSliders()`

```js
    // SQL：0 = 常开。Yaesu 不回读，状态只在我们自己下发时变；Icom 每 5 s 回读，
    // 面板旋钮动过这里跟着动（同 RF PWR 的拖动守卫）。
    const sqlSlider = document.getElementById('slider-sql');
    const sqlState = typeof radioState.squelch === 'number' ? radioState.squelch : null;
    if (sqlSlider && sqlState !== null && sqlSlider.dataset.dragging !== '1' &&
        Number(sqlSlider.value) !== sqlState) {
        setSlider('slider-sql', 'val-sql', sqlState);
        try { FT710Settings.setCookie('ft710_sql', String(sqlState)); } catch (e) { /* cookie 坏了不影响控制 */ }
    }
```

### 4.4 `addSlider()` 的拖动守卫变成真的

现有代码里 `pSlider.dataset.dragging !== '1'` 这个判据**没有任何地方置位**（`rg dragging` 只有
`ft710_ui.js:437-440` 两处），所以回显其实随时可能把正在拖的拇指弹回去。在 `addSlider` 里补上：

```js
slider.addEventListener('input', function() {
    slider.dataset.dragging = '1';
    setText(valId, fmt ? fmt(parseInt(this.value, 10)) : this.value);
});
slider.addEventListener('change', function() {
    delete slider.dataset.dragging;
    onCommit(parseInt(this.value, 10));
});
```

这是既有 RF PWR 判据的本意，属于顺手把经手的代码修对。

## 5. 边界与失败处理

| 情形 | 行为 |
| --- | --- |
| cookie 缺失/非数字/越界 | 回落 `radioState.squelch`，再回落 0，并夹到 0..100 |
| 电台未连接 | `sendCommand` 走既有路径，服务端回 `error` → 既有 toast；本地状态仍是乐观值 |
| `listen` 只读会话 | 服务端 `_listen` 白名单只放 freq/mode/memRecall，`squelch` 会被拒（4003/error）；页面本身不为此改动 |
| 另一台设备改了静噪 | Icom：5 s 内滑杆跟上；Yaesu：下一次 `fullState`/下发后跟上 |
| 拖动中收到回读 | 守卫跳过（`dataset.dragging === '1'`），松手后按最后一次 commit 的值 |

## 6. 缓存号（本仓硬规矩，漏了等于没发）

| 文件 | 改动 |
| --- | --- |
| `static/index.html:575` | `ft710_ui.js?v=39` → `?v=40` |
| `static/sw.js:2` | `CACHE = 'mrrc-v56'` → `'mrrc-v57'` |
| `static/sw.js` 预缓存 | `'/ft710_ui.js?v=39'` → `'/ft710_ui.js?v=40'` |

`ft710.css?v=27`、`ft710_main.js?v=46` 不动（本次没碰那两个文件）。

## 7. 测试

新增（`tests/test_server_ws_protocol.py`，紧挨既有的 `test_settings_panel_exposes_rf_power_slider`）：

- `test_settings_panel_exposes_squelch_slider`：断言 `showSettingsPanel(` 里出现
  `'slider-sql'`、`sendCommand('squelch'`、`ft710_sql`、`'squelch'` 进了 `settingsFields` 与
  `needSliders` 分支、以及拖动守卫存在。
- `test_settings_slider_marks_dragging_while_dragged`：断言 `addSlider` 里
  `slider.dataset.dragging = '1'` 与 `delete slider.dataset.dragging` 都在（判据不再空转）。

更新（缓存号 pin，全是字符串常量）：

| 文件 | 改动 |
| --- | --- |
| `tests/test_server_ws_protocol.py:250-257` | `ft710_ui.js?v=40`、`mrrc-v57` |
| `tests/test_spectrum_profile_server.py:450-463` | 同上；旧值 `?v=39` / `mrrc-v56` 移进 stale 名单 |
| `tests/test_ws_token_transport.py:184` | `mrrc-v57` |
| `tests/test_tx_liveness.py:345` | `mrrc-v57` |
| `tests/test_menu_version.py:111` | `mrrc-v57` |
| `tests/test_audio.py:151` | `mrrc-v57` |

验收：`python -m unittest discover -s tests` 全绿（用例数与模块数按实际结果更新 `tests/README.md`）。

**做不了的真机验证**（离线可完成的只是字符串与结构断言）：静噪真正压掉底噪需要一台电台 +
`FM` 模式；本仓没有 Web 前端测试运行器，所以 UI 层只到"源码级契约测试"为止——这一点在
CHANGELOG 里写明，不冒充"已在电台上验证"。

## 8. 版本与文档（发版时一次做完，本次提交不含）

- CHANGELOG 新标题 **`## [镜像线 v1.28.11 · FP7]`**。
  **为什么不是 `1.28.10 · FP7`**：`upgrade_core.check_box()` 的可用性只看
  `compare_versions(current, latest) < 0`（`upgrade_core.py:270`），`fixpack` 按设计
  **只显示不参与比较**（`upgrade_core.py:212`）；热修段同样要求 `current < patch.version`
  （`upgrade_core.py:238`）。版本号停在 `1.28.10` 的话，已装 `1.28.10 · FP6` 的盒子会一直
  看到「已最新」，这次修复永远送不到。
- `website/downloads/latest.json` 的 `boxLine`（version / fixpack / url / sha256 / size）
  必须与标题同步——`tests/test_release_artifacts.py::BoxLineFixPackTests` 拿两者对撞。
  造包（`dev_tools/make_box_source.py --fixpack FP7`）与部署走 `dual-platform-release` 流程。
- SDD `14-version-history.md` 追加一行。

## 9. 明确不做

- 不加 FT-710 / Yaesu 的 `SQ` 回读轮询（D-2），`backends/` 一行不改。
- 不在主界面加滑杆（D-1），不改 `listen` / `sdr` 页面（SDR 页那条静噪是 SDR 自己的解调器，
  与电台 CAT 无关）。
- 不改 `server.py` 的 `squelch` 分支（已钳位、已广播）。
- 不动安卓 / iOS 客户端（它们自己已有 `squelch` 字段）。
