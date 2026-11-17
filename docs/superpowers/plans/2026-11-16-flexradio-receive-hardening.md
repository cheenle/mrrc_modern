# FlexRadio Receive Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变现有单声道客户端协议和 TX 门禁的前提下，修正 Flex VITA/MultiFlex/重连，接入原生 waterfall，并为 FT8 提供独立 DAX 音频。

**Architecture:** 保持 `vita → session → engine → audio/scope → backend` 的单向依赖。字节正确性和分片覆盖放在 `vita.py`，资源归属与连接代际放在 `engine.py`，DAX 通过可选 handler hook 接入通用 RX loop，wf2 承载原生 waterfall，旧客户端继续读 wf1。

**Tech Stack:** Python 3、asyncio、NumPy、stdlib `unittest`、FastAPI WebSocket、现有 JavaScript classic scripts；不新增依赖。

**Spec:** `docs/superpowers/specs/2026-11-16-flexradio-receive-hardening-design.md`

**工作纪律:** 每个任务先写失败测试，再最小实现。除非用户另行明确授权，不执行 git commit、不打发布包、不部署。

---

### Task 1: 修正 FFT 子头与唯一 bin 覆盖

**Files:**
- Modify: `backends/flex/vita.py`
- Test: `tests/test_flex_vita.py`

- [ ] **Step 1: 用真实 12 字节布局重写测试构造器并增加回归**

```python
@staticmethod
def _segment_payload(start, bins, total, frame_index=1):
    head = struct.pack(">HHHHI", start, len(bins), 2, total, frame_index)
    return head + struct.pack(f">{len(bins)}H", *bins)

def test_frame_index_is_uint32(self):
    seg = vita.parse_fft_segment(self._segment_payload(0, [1, 2], 2, 0x00020001))
    self.assertEqual(seg.frame_index, 0x00020001)
    self.assertEqual(seg.subheader_bytes, 12)

def test_duplicate_segments_do_not_complete_a_frame(self):
    asm = vita.FftAssembler()
    seg = vita.parse_fft_segment(self._segment_payload(0, [11, 22], 4, 7))
    self.assertIsNone(asm.feed(seg))
    self.assertIsNone(asm.feed(seg))

def test_late_segment_does_not_replace_the_new_frame(self):
    asm = vita.FftAssembler()
    asm.feed(vita.parse_fft_segment(self._segment_payload(0, [1, 2], 4, 8)))
    old = vita.parse_fft_segment(self._segment_payload(0, [9, 9], 4, 7))
    self.assertIsNone(asm.feed(old))
    done = asm.feed(vita.parse_fft_segment(self._segment_payload(2, [3, 4], 4, 8)))
    self.assertEqual(done.tolist(), [1, 2, 3, 4])
```

- [ ] **Step 2: 证明旧实现失败**

Run: `.venv/bin/python -m unittest tests.test_flex_vita.FftTests -v`
Expected: `uint32` 被截断，重复分片错误返回 `[11, 22, 0, 0]`。

- [ ] **Step 3: 实现严格解析、覆盖表与迟到检测**

```python
class BinCoverage:
    def __init__(self, total: int) -> None:
        self._seen = bytearray(total)
        self.unique = 0

    def mark(self, start: int, count: int) -> bool:
        if start < 0 or count <= 0 or start + count > len(self._seen):
            return False
        fresh = False
        for index in range(start, start + count):
            if not self._seen[index]:
                self._seen[index] = 1
                self.unique += 1
                fresh = True
        return fresh

    @property
    def complete(self) -> bool:
        return bool(self._seen) and self.unique == len(self._seen)


def _is_late_frame(current: int, incoming: int) -> bool:
    return 1 <= (current - incoming) & 0xFFFFFFFF <= 8
```

`parse_fft_segment()` 使用 `struct.unpack_from(">HHHHI", payload, 0)`，只接受 `bin_size == 2` 且 `len(payload) == 12 + num_bins * 2` 的输入。`FftAssembler.feed()` 在每帧持有 `BinCoverage`，旧 frame 直接返回 `None`，新 frame 才 reset，覆盖完整才返回数组。

- [ ] **Step 4: 运行 FFT 与引擎回归**

Run: `.venv/bin/python -m unittest tests.test_flex_vita tests.test_flex_engine -v`
Expected: 全部 PASS；无错误布局构造器残留。

---

### Task 2: 增加原生 waterfall tile 编解码与 auto-black

**Files:**
- Modify: `backends/flex/vita.py`
- Test: `tests/test_flex_vita.py`

- [ ] **Step 1: 添加 tile 黄金测试**

```python
def _tile_payload(*, low_hz=14_000_000, bin_bw=100, timecode=9,
                  auto_black=13_000, bins=(12_800, 13_056, 14_080)):
    head = struct.pack(
        ">qqIHHIIHH",
        low_hz * (1 << 20), bin_bw * (1 << 20), 40, len(bins), 1,
        timecode, auto_black, len(bins), 0,
    )
    return head + struct.pack(f">{len(bins)}h", *bins)


def test_waterfall_tile_decodes_frequency_intensity_and_auto_black(self):
    tile = vita.parse_waterfall_tile(_tile_payload())
    self.assertEqual(tile.low_freq_hz, 14_000_000)
    self.assertEqual(tile.bin_bandwidth_hz, 100)
    self.assertEqual(tile.timecode, 9)
    self.assertEqual(tile.auto_black, 13_000)
    self.assertEqual(tile.intensity, (100.0, 102.0, 110.0))


def test_waterfall_auto_black_uses_the_radio_curve(self):
    ui = vita.waterfall_to_ui((100.0, 102.0, 110.0), auto_black=13_000)
    self.assertEqual(len(ui), 3)
    self.assertEqual(ui[0], 0)
    self.assertTrue(0 < ui[1] < ui[2] <= 255)
```

另加 `height=2`、截断 payload、重复 tile 分片均不得发布的测试。

- [ ] **Step 2: 证明当前没有该能力**

Run: `.venv/bin/python -m unittest tests.test_flex_vita -v`
Expected: `AttributeError: ... has no attribute parse_waterfall_tile`。

- [ ] **Step 3: 实现 tile dataclass、严格解析和映射**

```python
@dataclass(frozen=True)
class WaterfallTile:
    start_bin: int
    total_bins: int
    timecode: int
    auto_black: int
    low_freq_hz: int
    bin_bandwidth_hz: int
    intensity: Tuple[float, ...]


def waterfall_to_ui(values, *, auto_black: int,
                    color_gain: int = 50, offset: int = 50) -> np.ndarray:
    low = min(65535.0, max(0.0,
        float(auto_black) + (50 - int(offset)) * 0.5 * 128.0))
    num = (100.0 - int(color_gain)) / 100.0 * np.cbrt(65535.0 - low)
    high = max(low + 100.0, low + num ** 3)
    scaled = (np.asarray(values, dtype=np.float64) - low / 128.0) / ((high - low) / 128.0)
    return np.clip(np.rint(np.clip(scaled, 0.0, 1.0) * 255.0), 0, 255).astype(np.uint8)
```

解析使用 36 字节子头，频率除以 `1 << 20`，仅接受 `height == 1`、`width == total_bins` 且 payload 长度精确的数据。复用 Task 1 的覆盖表和 8 帧迟到窗口。

- [ ] **Step 4: 运行纯字节测试**

Run: `.venv/bin/python -m unittest tests.test_flex_vita -v`
Expected: 全部 PASS。

---

### Task 3: 每 pan 保存真实显示状态

**Files:**
- Modify: `backends/flex/engine.py`
- Modify: `backends/flex/scope.py`
- Test: `tests/test_flex_engine.py`
- Test: `tests/test_flex_scope.py`

- [ ] **Step 1: 添加外国 pan 和真实编码参数回归**

```python
def test_foreign_pan_cannot_change_our_axis_or_decode_scale(self):
    self.engine.pan_id = 0x40000001
    own = protocol.parse_status(
        "S0|display pan 0x40000001 center=14.074000 bandwidth=0.100000 "
        "min_dbm=-140 max_dbm=-50 x_pixels=850 y_pixels=256")
    foreign = protocol.parse_status(
        "S0|display pan 0x40000000 center=7.100000 bandwidth=0.500000 "
        "min_dbm=-135 max_dbm=-40 x_pixels=50 y_pixels=20")
    self.engine._handle_status(own)
    self.engine._handle_status(foreign)
    self.assertEqual(self.engine.pan_center_hz, 14_074_000)
    self.assertEqual(self.engine.bandwidth_hz, 100_000)
    self.assertEqual(self.engine.pan_decode_scale(),
                     (-140.0, -50.0, 850, 256))
```

- [ ] **Step 2: 证明旧实现被外国 pan 覆盖**

Run: `.venv/bin/python -m unittest tests.test_flex_engine -v`
Expected: center 变为 7.100 MHz，bandwidth 变为 500 kHz，且无 `pan_decode_scale()`。

- [ ] **Step 3: 实现 per-pan 状态与只读投影**

```python
def _update_pan(self, status: Status, obj: str = "") -> None:
    pan_id = _first_int(obj.split()[-1]) if obj else None
    if pan_id is None:
        return
    entry = self.pans.setdefault(pan_id, {})
    for wire, field in (("center", "center_hz"), ("bandwidth", "bandwidth_hz")):
        value = status.number(wire)
        if value is not None and value > 0:
            entry[field] = int(round(value * 1e6))
    for wire, field in (("min_dbm", "min_dbm"), ("max_dbm", "max_dbm"),
                        ("x_pixels", "x_pixels"), ("y_pixels", "y_pixels")):
        value = status.number(wire)
        if value is not None:
            entry[field] = value if wire.endswith("dbm") else int(value)
    handle = status.get("client_handle")
    if handle:
        entry["client_handle"] = handle
    entry["created"] = True


def pan_decode_scale(self):
    pan = self.pans.get(self.pan_id, {})
    return (float(pan.get("min_dbm", -140.0)),
            float(pan.get("max_dbm", -50.0)),
            int(pan.get("x_pixels", 850)),
            int(pan.get("y_pixels", 256)))
```

`pan_center_hz` 和 `bandwidth_hz` 改为只读当前 `pan_id` 的属性；`scope.py` 用该属性及实际 decode scale，不再读取其他 pan 的推送。

- [ ] **Step 4: 运行引擎和 scope 测试**

Run: `.venv/bin/python -m unittest tests.test_flex_engine tests.test_flex_scope -v`
Expected: 全部 PASS。

---

### Task 4: slice 归属与连接代际完整重建

**Files:**
- Modify: `backends/flex/engine.py`
- Modify: `backends/flex/backend.py`
- Test: `tests/test_flex_engine.py`
- Test: `tests/test_flex_backend.py`

- [ ] **Step 1: 添加归属与重连失败测试**

```python
async def test_slice_on_another_pan_is_not_a_valid_control_target(self):
    self.engine.pan_id = 0x40000001
    self.engine.slices[0] = {"pan_id": 0x40000000, "client_handle": "0x1"}
    self.engine.own_slice = 0
    self.assertFalse(self.engine.has_valid_own_slice())

async def test_reconnect_invalidates_stream_ids_and_ready_flags(self):
    self.engine.rx_stream_id = 0x04000008
    self.engine.tx_stream_id = 0x84000000
    self.engine.rx_stream_ready = self.engine.tx_stream_ready = True
    self.engine._invalidate_connection_resources()
    self.assertEqual((self.engine.rx_stream_id, self.engine.tx_stream_id), (0, 0))
    self.assertFalse(self.engine.rx_stream_ready)
    self.assertFalse(self.engine.tx_stream_ready)
```

另断言 `_slice_id()` 在无合法 slice 时抛 `SessionError`，不得回退 `min(slices)`；`ensure_slice()` 不发送 `active=1`。

- [ ] **Step 2: 证明旧实现失败**

Run: `.venv/bin/python -m unittest tests.test_flex_engine tests.test_flex_backend -v`
Expected: 无 `has_valid_own_slice()` / `_invalidate_connection_resources()`，且旧代码返回别人的 slice。

- [ ] **Step 3: 实现失效和重协调**

```python
def has_valid_own_slice(self) -> bool:
    state = self.slices.get(self.own_slice)
    return bool(state and state.get("pan_id") == self.pan_id)


def _invalidate_connection_resources(self) -> None:
    self.pan_id = self.waterfall_id = 0
    self.rx_stream_id = self.tx_stream_id = 0
    self.rx_stream_ready = self.tx_stream_ready = False
    self.own_slice = None
    self.slices.clear()
    self.pans.clear()
    self.rx_audio.clear()
    self.rx_samples = 0
    self._assembler = vita.FftAssembler()
```

`_connect_once()` 在握手后使 generation 递增；任何 pan/slice/stream 失败均 close session、清 `self.session` 和 `connected`。重连先 `_invalidate_connection_resources()`。`ensure_slice()` 只考虑 `state.pan_id == self.pan_id` 且属于本 generation 的对象，不发 `active=1`。

- [ ] **Step 4: 运行生命周期测试**

Run: `.venv/bin/python -m unittest tests.test_flex_engine tests.test_flex_session tests.test_flex_backend -v`
Expected: 全部 PASS。

---

### Task 5: AGC、滤波器、实际跨度和 RF gain 量程以电台为准

**Files:**
- Modify: `backends/flex/engine.py`
- Modify: `backends/flex/backend.py`
- Modify: `radio_state.py`
- Modify: `static/ft710_ui.js`
- Test: `tests/test_flex_backend.py`
- Test: `tests/test_flex_engine.py`

- [ ] **Step 1: 添加真实状态回归**

```python
def test_agc_and_filter_come_from_the_radio(self):
    self.engine.own_slice = 0
    self.engine.slices[0] = {
        "mode": "USB", "agc_mode": "slow", "filter_lo": 100, "filter_hi": 2800,
    }
    controller = FlexController(FlexBackend(host="127.0.0.1"))
    controller.engine = self.engine
    self.assertEqual(controller._radio_agc(), 3)
    self.assertEqual(controller._radio_filter_hz(), 2700)


def test_rf_gain_range_is_parsed_per_scu(self):
    ranges = self.engine.parse_rf_gain_info("-8,32,8,0,16,32")
    self.assertEqual(ranges, [(-8, 32, 8), (0, 16, 32)])
```

- [ ] **Step 2: 证明旧实现使用本地假定值**

Run: `.venv/bin/python -m unittest tests.test_flex_backend -v`
Expected: 新方法不存在，旧 `get_agc()` 返回构造默认值。

- [ ] **Step 3: 实现回读与量程解析**

```python
@staticmethod
def parse_rf_gain_info(body: str):
    values = [int(token.strip()) for token in body.split(",")]
    if len(values) % 3:
        raise ValueError("RF gain range must contain complete triplets")
    return [tuple(values[index:index + 3]) for index in range(0, len(values), 3)]
```

`initial_state_sync()` 从 `agc_mode`、`filter_lo/hi` 推导；AGC-T 未上报时返回 `None`，不写 65。连接时查询 `display pan rfgain_info <pan>`，量程未知时 `has_rf_gain=False`。`RadioState` 增加 `scope_span_hz`，Flex 发布实际值；`_scopeAxis()` 优先使用它。

- [ ] **Step 4: 运行状态与前端契约测试**

Run: `.venv/bin/python -m unittest tests.test_flex_backend tests.test_radio_state tests.test_server_ws_protocol -v`
Expected: 全部 PASS。

---

### Task 6: 引擎接收原生 waterfall 并保留新鲜度

**Files:**
- Modify: `backends/flex/engine.py`
- Modify: `backends/flex/scope.py`
- Test: `tests/test_flex_engine.py`
- Test: `tests/test_flex_scope.py`

- [ ] **Step 1: 添加原生行与 stale 测试**

```python
def test_native_waterfall_replaces_only_our_wf2_and_advances_once(self):
    self.engine.pan_id = 0x40000001
    self.engine.waterfall_id = 0x42000001
    packet = _packet(vita.PCC_WATERFALL, _tile_payload(), stream_id=0x42000001)
    self.engine._handle_packet(packet)
    row = self.engine.waterfall_row()
    self.assertIsNotNone(row)
    first_generation = row.generation
    self.engine._handle_packet(packet)
    self.assertEqual(self.engine.waterfall_row().generation, first_generation)

async def test_stale_scope_does_not_increment_public_frame_count(self):
    # 注入一帧后，将最后接收时间调到 stale 阈值之前。
    self.engine._last_spectrum_rx = 0.0
    self.assertFalse(await self.producer._emit_once())
```

- [ ] **Step 2: 证明旧实现不支持 tile 且重复发旧帧**

Run: `.venv/bin/python -m unittest tests.test_flex_engine tests.test_flex_scope -v`
Expected: 无 `waterfall_row()`，旧 producer 继续递增 `_frame_count`。

- [ ] **Step 3: 实现原生行缓存和新鲜 generation**

```python
@dataclass(frozen=True)
class NativeWaterfallRow:
    generation: int
    ui_bins: Any
    low_freq_hz: int
    high_freq_hz: int
    auto_black: int
    received_monotonic: float
```

`_handle_packet()` 仅接受 `header.stream_id == waterfall_id` 的 tile，调用 Task 2 的 assembler，完整新 timecode 才递增 generation。`scope.py` 将 wf1 设为 FFT、wf2 设为原生行，分别发布 start/span；stale 或两个平面都无新 generation 时返回 `False`，不增加 `_frame_count`。

- [ ] **Step 4: 运行接收测试**

Run: `.venv/bin/python -m unittest tests.test_flex_engine tests.test_flex_scope -v`
Expected: 全部 PASS。

---

### Task 7: 浏览器用 wf1 画 FFT、wf2 画真实瀑布

**Files:**
- Modify: `static/ft710_main.js`
- Modify: `static/ft710_ui.js`
- Modify: `backends/base.py`
- Modify: `backends/flex/backend.py`
- Modify: `radio_state.py`
- Modify: `server.py`
- Modify: `static/index.html`
- Modify: `static/sw.js`
- Test: `tests/test_server_ws_protocol.py`
- Test: `tests/test_spectrum_profile_server.py`

- [ ] **Step 1: 添加前端协议契约**

```python
def test_native_waterfall_uses_wf2_and_short_frames_do_not_synthesize_rows(self):
    main = Path("static/ft710_main.js").read_text(encoding="utf-8")
    ui = Path("static/ft710_ui.js").read_text(encoding="utf-8")
    self.assertIn("renderWaterfallRow(wf1, wf2)", main)
    self.assertIn("function renderWaterfallRow(wf1, wf2 = null)", ui)
    self.assertIn("scope_native_waterfall", ui)
    self.assertIn("if (wf2) drawWaterfallSamples(wf2);", ui)
```

- [ ] **Step 2: 证明旧前端只接收一个数组**

Run: `.venv/bin/python -m unittest tests.test_server_ws_protocol -v`
Expected: 契约失败。

- [ ] **Step 3: 实现兼容渲染与独立轴**

```javascript
function handleSpectrumBinary(buffer) {
    const data = new Uint8Array(buffer);
    if (data.length < 851 || data[0] < 1 || data[0] > 2) return;
    const wf1 = Array.from(data.slice(1, 851));
    const wf2 = data.length >= 1701 ? Array.from(data.slice(851, 1701)) : null;
    renderWaterfallRow(wf1, wf2);
}
```

`renderWaterfallRow(wf1, wf2 = null)` 总是先 `renderFFTPlot(wf1)`；只有 `radioCapabilities.scope_native_waterfall && radioState.native_waterfall && wf2` 时才用 wf2 画行，其他后端仍用 wf1。新增 `scope_native_waterfall` 能力和 `native_waterfall` 状态；waterfall 轴使用 `waterfall_start_freq/span_hz`，短帧不前进原生瀑布。

- [ ] **Step 4: 更新缓存 pin 并运行前端回归**

Run: `node --check static/ft710_main.js && node --check static/ft710_ui.js`
Run: `.venv/bin/python -m unittest tests.test_server_ws_protocol tests.test_spectrum_profile_server tests.test_menu_version tests.test_audio tests.test_tx_liveness tests.test_ws_token_transport -v`
Expected: 全部 PASS，1701/851 B 契约不变。

---

### Task 8: DAX 通道分配与独立音频队列

**Files:**
- Modify: `backends/flex/engine.py`
- Modify: `backends/flex/audio.py`
- Test: `tests/test_flex_engine.py`
- Test: `tests/test_flex_audio.py`

- [ ] **Step 1: 添加 DAX 生命周期和隔离测试**

```python
async def test_dax_uses_a_free_channel_and_cleans_only_its_own_binding(self):
    self.engine.own_slice = 0
    self.engine.pan_id = 0x40000001
    self.engine.slices = {
        0: {"pan_id": self.engine.pan_id, "dax": 0},
        1: {"pan_id": 0x40000000, "dax": 1},
    }
    ok = await self.engine.start_dax()
    self.assertTrue(ok)
    self.assertEqual(self.engine.dax_channel, 2)
    await self.engine.stop_dax()
    self.assertEqual(self.engine.dax_channel, 0)


def test_decoder_resampler_and_pcm_carry_are_not_shared_with_speaker(self):
    handler = FlexAudioHandler(self.engine)
    self.assertIsNot(handler._up2, handler._decoder_up2)
    self.assertIsNot(handler._rx_pcm, handler._decoder_pcm)
```

- [ ] **Step 2: 证明当前没有独立 DAX**

Run: `.venv/bin/python -m unittest tests.test_flex_engine tests.test_flex_audio -v`
Expected: `start_dax` / decoder 状态不存在。

- [ ] **Step 3: 实现 DAX 启动、过滤、读取和清理**

```python
async def start_dax(self) -> bool:
    if self.dax_stream_ready:
        return True
    if not self.has_valid_own_slice() or self.session is None:
        return False
    used = {int(state.get("dax", 0)) for state in self.slices.values()}
    free = next((channel for channel in range(1, 5) if channel not in used), None)
    if free is None:
        return False
    slice_id = self.own_slice
    if not await self.session.send_ok(f"slice set {slice_id} dax={free}"):
        return False
    reply = await self.session.send(f"stream create type=dax_rx dax_channel={free}")
    stream_id = _first_int(reply.body) if reply.ok else None
    if not stream_id:
        await self.session.send_ok(f"slice set {slice_id} dax=0")
        return False
    self.dax_channel = free
    self.dax_stream_id = stream_id
    self.dax_stream_ready = True
    return True
```

`audio.py` 新增 `start_decoder_rx/stop_decoder_rx/read_decoder_rx_chunk/decoder_rx_source`，通过现有 `_run()` 桥接；decoder 使用独立 queue、半带重采样器和 PCM carry。`dax-pending` 不读 speaker。

- [ ] **Step 4: 运行 DAX 与既有音频回归**

Run: `.venv/bin/python -m unittest tests.test_flex_engine tests.test_flex_audio -v`
Expected: 全部 PASS，speaker 路径结果不变。

---

### Task 9: server 的 FT8 tap 使用可选 decoder hook

**Files:**
- Modify: `server.py`
- Test: `tests/test_flex_audio.py`
- Test: `tests/test_audio.py`

- [ ] **Step 1: 添加可选 hook 的 fake handler 测试**

```python
class DecoderHandler(FakeAudio):
    def __init__(self):
        super().__init__()
        self.decoder_started = False

    def start_decoder_rx(self):
        self.decoder_started = True
        return True

    def stop_decoder_rx(self):
        self.decoder_started = False

    def read_decoder_rx_chunk(self):
        return b"\x02\x00" * 960

    def decoder_rx_source(self):
        return "dax"
```

验证 FT8 tap 激活时启动 decoder、收到 decoder PCM 而非 speaker PCM；无 hook 的旧 fake 不调用任何新方法且仍收到原 PCM。

- [ ] **Step 2: 证明旧 server 只能给 FT8 speaker PCM**

Run: `.venv/bin/python -m unittest tests.test_audio -v`
Expected: decoder 测试失败。

- [ ] **Step 3: 在 RX loop 接入生命周期和独立 tap**

```python
if _ft8_tap_live and not _decoder_rx_live:
    start_decoder = getattr(audio, "start_decoder_rx", None)
    _decoder_rx_live = bool(start_decoder()) if start_decoder else False
elif not _ft8_tap_live and _decoder_rx_live:
    stop_decoder = getattr(audio, "stop_decoder_rx", None)
    if stop_decoder:
        stop_decoder()
    _decoder_rx_live = False

ft8_pcm = pcm
read_decoder = getattr(audio, "read_decoder_rx_chunk", None)
if _ft8_tap_live and read_decoder and audio.decoder_rx_source() != "speaker-mix":
    ft8_pcm = read_decoder()
```

`dax-pending` 时 `ft8_pcm=None`；`speaker-mix` 才使用原 `pcm`。lifespan 和重连走同一 stop hook。

- [ ] **Step 4: 运行 RX 消费者回归**

Run: `.venv/bin/python -m unittest tests.test_audio tests.test_flex_audio -v`
Expected: 全部 PASS。

---

### Task 10: VITA 序列、音频时间轴和 UDP 缓冲

**Files:**
- Modify: `backends/flex/session.py`
- Modify: `backends/flex/engine.py`
- Modify: `backends/flex/vita.py`
- Test: `tests/test_flex_session.py`
- Test: `tests/test_flex_engine.py`

- [ ] **Step 1: 添加乱序、重复和缺包时间轴测试**

```python
def test_audio_gap_preserves_the_sample_clock(self):
    self.engine.rx_stream_id = 0x04000008
    self.engine._handle_packet(_audio_packet(seq=1, value=0.25))
    self.engine._handle_packet(_audio_packet(seq=3, value=0.50))
    pcm = self.engine.read_all_rx_mono()
    expected_samples = 256 * 3
    self.assertEqual(sum(block.size for block in pcm), expected_samples)


def test_duplicate_audio_packet_is_delivered_once(self):
    self.engine.rx_stream_id = 0x04000008
    packet = _audio_packet(seq=1, value=0.25)
    self.engine._handle_packet(packet)
    self.engine._handle_packet(packet)
    self.assertEqual(self.engine.audio_packets, 1)
```

- [ ] **Step 2: 证明旧实现丢时间且重复计数**

Run: `.venv/bin/python -m unittest tests.test_flex_engine -v`
Expected: 缺包只有两段样本，重复包 `audio_packets == 2`。

- [ ] **Step 3: 实现每流有界重排和精确补零**

```python
@dataclass
class StreamOrder:
    last_sequence: int = -1
    pending: dict = field(default_factory=dict)

    def accept(self, sequence: int):
        if self.last_sequence < 0:
            self.last_sequence = sequence
            return 0, True
        distance = (sequence - self.last_sequence - 1) & 0x0F
        if distance == 0:
            self.last_sequence = sequence
            return 0, True
        if distance >= 8:
            return 0, False
        self.last_sequence = sequence
        return distance, True
```

生产实现使用 30 ms 定时 flush，不以 Task 的简化立即跳窗代替。PCM 每个缺包按该流最近包样本数补零，拼接处最多 5 ms 淡出/淡入。`_open_udp()` 请求 4 MiB `SO_RCVBUF` 并把实际值写入 stats。

- [ ] **Step 4: 运行序列与 uvloop 回归**

Run: `.venv/bin/python -m unittest tests.test_flex_engine tests.test_flex_session tests.test_flex_uvloop_compat -v`
Expected: 全部 PASS。

---

### Task 11: TX 离线安全链

**Files:**
- Modify: `backends/flex/vita.py`
- Modify: `backends/flex/engine.py`
- Modify: `backends/flex/backend.py`
- Modify: `_diag_flex.py`
- Test: `tests/test_flex_vita.py`
- Test: `tests/test_flex_engine.py`
- Test: `tests/test_flex_backend.py`

- [ ] **Step 1: 添加 TX 回归**

```python
def test_opus_packet_count_wraps_mod_16(self):
    counts = []
    for index in range(18):
        packet = vita.build_opus_packet(0x84000000, b"abc", packet_count=index)
        counts.append((struct.unpack(">I", packet[:4])[0] >> 16) & 0x0F)
    self.assertEqual(counts[-3:], [15, 0, 1])


def test_interlock_leaving_tx_stops_the_pacer(self):
    self.engine.tx_active = True
    self.engine._handle_interlock(SimpleNamespace(state="READY", previous="TRANSMITTING"))
    self.assertFalse(self.engine.tx_active)


def test_timeout_zero_is_disabled_and_milliseconds_are_converted(self):
    self.assertTrue(self.engine.tx_timeout_disabled)
    self.engine._handle_status(protocol.parse_status("S0|interlock timeout=30000"))
    self.assertEqual(self.engine.tx_timeout_s, 30.0)
    self.assertFalse(self.engine.tx_timeout_disabled)
```

- [ ] **Step 2: 证明旧实现失败**

Run: `.venv/bin/python -m unittest tests.test_flex_vita tests.test_flex_engine tests.test_flex_backend -v`
Expected: 无序号参数、timeout 不转毫秒、READY 不停 pacer。

- [ ] **Step 3: 实现序号、停止、SC_MIC 和 key 顺序**

`build_opus_packet(..., packet_count=0)` 将序号写入 word0；engine 持有 `_tx_packet_count = (_tx_packet_count + 1) & 0x0F`。`tx_timeout_disabled` 是 `tx_timeout_s == 0` 的只读属性，初始 timeout 为 0，收到 `interlock timeout=<ms>` 后保存 `value / 1000.0`。`key(True)` 在 `slice set tx=1` 和 `start_tx()` 成功后才 `xmit 1`。`SC_MIC` watchdog 在队列有音频但表无能量或证据缺失时 `xmit 0` 并设置 latch。`READY/RECEIVE/TX_FAULT/TIMEOUT` 停止 pacer。`_diag_flex.py` 对 timeout=0 明确报错，对 30000 ms 显示 30 s。

- [ ] **Step 4: 运行 TX 门禁和 PTT 通用回归**

Run: `.venv/bin/python -m unittest tests.test_flex_vita tests.test_flex_engine tests.test_flex_backend tests.test_tx_liveness tests.test_backend_controller_contract -v`
Expected: 全部 PASS，`VERIFIED` 仍为 `False`。

---

### Task 12: 文档、全量回归与真机只读验收

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `SDD/08-architecture-decisions.md`
- Modify: `SDD/09-architecture-overview.md`
- Modify: `SDD/14-version-history.md`
- Modify: `SDD/README.md`
- Modify: `tests/README.md`

- [ ] **Step 1: 同步真实行为**

记录 per-pan 状态、原生 wf2、DAX decoder hook、连接代际、timeout 毫秒、TX 仍门禁；不得把离线测试写成 TX 真机验证。

- [ ] **Step 2: 运行全量 Python 回归**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: 全绿；跳过项只能是既有平台/硬件条件。

- [ ] **Step 3: 运行前端和守护检查**

Run: `node --check static/ft710_main.js && node --check static/ft710_ui.js`
Run: `python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged`
Expected: JS 语法通过，guardian `clean` 或仅有可说明的既有警告。

- [ ] **Step 4: 真机只读验收**

使用持久盒子通道，不发射、不打开 TX 门禁：

1. 确认 850×256 FFT 与原生 `0x8004` tile 都到达，timecode 持续推进。
2. 比较 tile 的 `low/bin_bw` 与浏览器刻度和点击落点。
3. 启停 FT8，确认 DAX 通道创建/释放、无 `speaker-mix` 冒充 DAX。
4. 重启服务，确认旧 pan/slice/stream 不残留，外国 pan 参数不改变本机轴。
5. 确认 `interlock timeout=0` 在日志中明确告警；不把该告警当成已修改电台设置。

- [ ] **Step 5: 检查工作区并报告**

Run: `git status --short`
Expected: 只出现本任务修改和既有用户文件；删除测试产生的临时输出，不提交、不发布。
