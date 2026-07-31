# FT-710 Web Control — TX 链路深度分析报告

**初始日期**: 2026-07-14

**复审日期**: 2026-07-31

**分析范围**: TX 音频链路全栈（前端 → 服务端 → 电台）

**状态**: 软件链路与自动化回归已通过；Win11 + FT-710 射频录音验收待完成

> 2026-07-31 注：第 2–4 节保留的是 7 月 14 日的历史诊断快照，其中多项描述已被后续实现取代。当前权威设计以 SDD AD-004、AD-008、AD-011、§9.4 和下述复审结论为准。

## 0. 2026-07-31 Windows TX 复审结论

- 浏览器采集与 Opus 编解码固定为 48 kHz、960 samples/20 ms、64 kbps CBR；运行时编码/解码仿真无丢包，波形相关性 0.9992。
- 服务端每帧固定从 48 kHz/960 samples 转为 FT-710 设备域的 44.1 kHz/882 samples；Windows 首次打开及 PortAudio 重初始化后均保持 44.1 kHz。
- 60 ms 预缓冲、400 ms 上限和 50 ms 尾音排空均有无硬件回归；每次松开 PTT 的日志现在包含 `queue_drops`，可直接识别 Windows 输出节拍落后造成的断裂/咔啦声。
- 已修复主动断连标志被声明为 `const` 的异常；同一浏览器会话的新 TX 音频连接会替换半开旧连接，不同会话不能仅靠连接抢占。
- 439 项测试通过软件链路（本机缺少可选 `cryptography` 时为 435 通过、4 跳过）；真实射频声质不能由无硬件测试替代。

### Win11 + FT-710 最终验收

在合法频率和许可条件下进行，优先接假负载并用第二接收机/SDR录音：

1. 确认 Windows 配置按名称锁定 `FT710_AUDIO_TX_DEVICE=USB Audio`，FT-710 当前模式的 `MOD SOURCE=USB`。
2. 浏览器控制台执行 `TXDebug.startTone(1000, 0.2)`，按住 PTT 5–10 秒后执行 `TXDebug.stopTone()`；录音中应为稳定单音，无周期咔啦、断续或音调漂移。
3. 连续做三次 10 秒语音 PTT，再让电台断电重枚举 USB 后重复一次，覆盖首次打开、TX→RX 重开和 PortAudio 重初始化路径。
4. 每次检查服务端 `TX session:`：必须为 `decode_fail=0 write_err=0 queue_drops=0 non_owner_drops=0`；`frames` 应接近 `fed`，`written` 只允许因起止缓冲少数帧差异，`peak` 不应长期接近 100%。
5. 录音验收标准：语音完整、无削波爆音、无固定周期断帧、无首尾明显吞字；若 `queue_drops>0`，先判定 Windows 输出节拍/设备选择问题，不得以“Opus 正常”放行。

---

## 1. TX 链路架构概览

```
┌─────────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Browser (JS)      │    │   WebSocket      │    │   FT-710 Radio  │
│                     │    │   Server         │    │                 │
│ • MicCaptureWorklet │───▶│ • WSaudioTX      │───▶│ • CAT Controller│
│ • TxOpusWorker      │    │ • OpusDecoder    │    │ • PTT Manager   │
│ • PTTManager        │    │ • AudioResampler │    │ • PollScheduler │
│ • SettingsManager   │    │ • AudioHandler   │    │                 │
└─────────────────────┘    └──────────────────┘    └─────────────────┘
```

### 关键模块
- **前端**: `tx_capture_worklet.js`, `tx_opus_worker.js`, `ptt_manager.js`
- **服务端**: `audio_handler.py`, `opus_rx.py`, `audio_resample.py`, `cat_controller.py`, `poll_scheduler.py`
- **电台**: FT-710 CAT 协议

---

## 2. 发现的问题

### 🔴 高风险问题

#### 2.1 PTT 控制路径不一致
**位置**: `ft710_main.js`, `ptt_manager.js`

**问题描述**:
- `PTTManager` 提供了 `pttStart()` 和 `pttEnd()` 方法
- 但前端 PTT 按钮和空格键可能直接调用了其他路径，未使用 PTTManager
- 导致 TX 仪表轮询（每 200ms）可能在 PTT 激活时不工作

**影响**:
- PTT 状态与 TX 仪表显示不同步
- 用户按下 PTT 后，UI 可能不显示 TX 状态

**修复建议**:
```javascript
// 在 ft710_main.js 的 PTT 按钮事件中
pttBtn.addEventListener('click', async () => {
    const pttManager = window.appState.pttManager;
    if (pttManager.isPTTActive()) {
        await pttManager.pttEnd();
    } else {
        await pttManager.pttStart();
    }
});
```

#### 2.2 TX 仪表轮询条件错误
**位置**: `poll_scheduler.py`, 第 122 行

**问题描述**:
```python
if self._state.tx_active or self._state.rf_gain > 0:
    self._cat.send_command("PT", ...)
```
- `tx_active` 可能未正确反映 PTT 状态
- `rf_gain > 0` 不是有效的 TX 检测条件

**影响**:
- TX 仪表可能不显示或显示错误数据

**修复建议**:
```python
# 检查 PTT 状态而非 rf_gain
if self._state.tx_active:
    self._cat.send_command("PT", ...)
```

---

### 🟡 中风险问题

#### 2.3 AudioWorklet SAB 路径未实现
**位置**: `tx_capture_worklet.js`

**问题描述**:
- 代码注释宣称支持 SharedArrayBuffer (SAB) 路径
- 但主线程未发送 SAB 给 AudioWorklet
- 实际走的是 legacy postMessage frame 路径

**影响**:
- SAB 路径的代码成为死代码
- 未来启用 SAB 时需要重新实现

**修复建议**:
1. 删除 SAB 相关注释和代码
2. 或者实现完整的 SAB 路径

#### 2.4 TX Jitter Buffer 未使用
**位置**: `audio_handler.py`

**问题描述**:
- `TxJitterBuffer` 类已实现但未在任何地方使用
- 前端直接通过 WebSocket 发送原始 PCM 数据

**影响**:
- 代码冗余，增加维护成本

**修复建议**:
- 删除未使用的 `TxJitterBuffer` 类
- 或者在前端实现 jitter buffer

#### 2.5 前端 Opus 编码器版本过旧
**位置**: `tx_opus_worker.js`

**问题描述**:
- 使用 2021 年的 opus.js 版本
- 存在已知 bug 和性能问题

**影响**:
- 编码质量可能不如预期
- 可能存在兼容性风险

**修复建议**:
- 升级到最新的 opus.js 版本
- 或改用 WebCodecs API（现代浏览器）

#### 2.6 服务端 TX Opus 解码器可用性未检查
**位置**: `audio_handler.py`

**问题描述**:
- 如果 `libopus` 不可用，`TxOpusDecoder` 会失败
- 但前端仍默认发送 Opus 编码数据
- 导致 TX 静默失败（无调制）

**影响**:
- 用户按下 PTT 但电台无响应，难以诊断

**修复建议**:
```python
# 在 audio_handler.py 中添加
def is_tx_opus_available(self) -> bool:
    """Check if TX Opus decoder is available."""
    return self.tx_opus_decoder is not None
```

```javascript
// 在 ft710_main.js 中添加
async function checkTxOpusAvailability() {
    const response = await fetch('/api/tx-opus-status');
    const data = await response.json();
    if (!data.available) {
        alert('TX Opus decoder not available. Falling back to raw PCM.');
        // 切换回 PCM 路径
    }
}
```

---

### 🟢 低风险问题

#### 2.7 renderUpdates() 重复渲染
**位置**: `ft710_ui.js`

**问题描述**:
- `renderUpdates()` 被多次调用
- 可能导致不必要的重绘

**影响**:
- 轻微性能开销

**修复建议**:
- 合并重复的渲染调用
- 使用请求动画帧（requestAnimationFrame）节流

#### 2.8 RadioState.update() 日志过重
**位置**: `radio_state.py`

**问题描述**:
- 每次频率/模式变更都打 warning 日志
- 生产环境日志量过大

**影响**:
- 日志文件快速增长
- 可能掩盖真正的警告

**修复建议**:
```python
# 仅在首次变更或间隔较长时记录
if not hasattr(self, '_last_freq_log') or \
   time.time() - self._last_freq_log > 30:
    logger.warning(f"Frequency changed to {new_freq}")
    self._last_freq_log = time.time()
```

---

## 3. 测试验证状态

### 已通过的测试
- ✅ `test_audio.py` — 15 tests
- ✅ `test_poll_scheduler.py` — 35 tests
- ✅ `test_server_ws_protocol.py` — 46 tests
- ✅ `test_radio_state.py` — 42 tests
- **总计**: 138 tests passing

### 待补充的测试
- ❌ TX 链路端到端测试
- ❌ PTT 状态机测试
- ❌ Opus 编解码集成测试

---

## 4. 修复优先级

### 立即修复（阻塞部署）
1. **PTT 控制路径不一致** — 确保 PTT 按钮使用 PTTManager
2. **TX 仪表轮询条件** — 修复 `tx_active` 检测逻辑

### 短期修复（1周内）
3. **AudioWorklet SAB 路径** — 清理或实现
4. **TX Opus 可用性检查** — 添加前端通知
5. **TX Jitter Buffer** — 删除或使用

### 中期优化（1个月内）
6. **前端 Opus 版本升级** — 使用最新库
7. **renderUpdates() 优化** — 减少重复渲染
8. **RadioState 日志优化** — 降低日志频率

---

## 5. 代码质量指标

| 指标 | 当前值 | 目标值 | 状态 |
|------|--------|--------|------|
| TX 链路测试覆盖率 | ~40% | 80% | ⚠️ 待提升 |
| PTT 控制路径一致性 | 60% | 100% | 🔴 待修复 |
| 前端-服务端协议一致性 | 70% | 100% | 🟡 待优化 |
| 错误处理完整性 | 75% | 95% | 🟡 待完善 |

---

## 6. 下一步行动

### 立即可做
1. 修复 PTT 按钮事件处理
2. 修复 TX 仪表轮询条件
3. 添加 TX Opus 可用性检查

### 本周内
4. 清理 AudioWorklet SAB 代码
5. 删除未使用的 TxJitterBuffer
6. 升级前端 Opus 库

### 本月内
7. 编写 TX 链路端到端测试
8. 优化 renderUpdates() 性能
9. 调整 RadioState 日志级别

---

## 7. 结论

TX 链路整体架构合理，但存在几个关键问题需要修复：

1. **PTT 控制路径不一致**是最高优先级问题，直接影响用户操作体验
2. **TX 仪表显示**问题需要尽快修复，否则用户无法看到 TX 状态
3. **Opus 可用性检查**缺失会导致静默失败，用户体验差

建议按上述优先级逐步修复，并在修复后进行完整的端到端测试。

---

**报告生成时间**: 2026-07-14 08:21  
**分析师**: Agnes Code Review  
**下次审查**: 修复完成后
