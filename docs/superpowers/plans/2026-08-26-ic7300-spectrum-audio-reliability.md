# IC-7300 频谱与音频可靠性修复实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。当前工作区已有用户改动；只做精确编辑，不覆盖无关 diff。未经用户明确要求不创建 commit。

**目标：** 让 IC-7300/MK2 默认以 115200 baud 正常连接，以新鲜数据稳定输出约 20–30 FPS 频谱，提供正确的 CI-V 现场诊断和可核对的声卡链路日志，同时保持 FT-710 行为不变。

**架构：** 配置层根据后端选择默认 baud；CI-V demux 使用有界“最新数据优先”队列；共享 WebSocket 广播以 30 Hz 调度且真实频谱只发送新帧；诊断脚本复用正式 CI-V codec；AudioHandler 只增加设备元数据日志，不改变 DSP/采样链。

**技术栈：** Python 3.12+、asyncio、FastAPI WebSocket、pyserial、PyAudio、unittest、SDD Guardian。

---

## 文件结构

- 修改 `config.py`：按 `MRRC_RADIO_MODEL` 选择默认 CAT/CI-V baud。
- 修改 `backends/ic7300/civ_controller.py`：有界 scope 分片队列与最旧分片丢弃。
- 修改 `server.py`：30 Hz 频谱调度与真实新帧去重。
- 修改 `_diag_ic7300_scope.py`：复用正式 CI-V codec，移除伪校验和，退出时关闭数据输出。
- 修改 `audio_handler.py`：设备 host API、默认率、实际率和匹配层级日志。
- 创建 `tests/test_ic7300_runtime_reliability.py`：本次修复的 12 个硬件无关回归测试。
- 修改 `SDD/09-architecture-overview.md`、`SDD/12-operational-model.md`、`SDD/14-version-history.md`、`SDD/README.md`：架构与版本同步。
- 修改 `README.md`、`docs/OPERATION_GUIDE.md`、`AGENTS.md`、`tests/README.md`：运行配置、职责和测试统计同步。

### 任务 1：后端感知的默认 baud

**文件：**
- 修改：`tests/test_ic7300_runtime_reliability.py`
- 修改：`config.py:45-54`

- [ ] **步骤 1：创建失败测试**

在新测试模块中用隔离子进程导入 `config`，避免模块缓存污染：

```python
class BackendBaudDefaultsTests(unittest.TestCase):
    def _baud(self, model, **extra):
        env = os.environ.copy()
        for key in ("MRRC_RADIO_MODEL", "MRRC_BAUD_RATE", "FT710_BAUD_RATE"):
            env.pop(key, None)
        env["MRRC_RADIO_MODEL"] = model
        env.update(extra)
        out = subprocess.check_output(
            [sys.executable, "-c", "import config; print(config.BAUD_RATE)"],
            cwd=REPO_ROOT,
            env=env,
            text=True,
        )
        return int(out.strip())

    def test_ft710_default_baud_is_38400(self):
        self.assertEqual(self._baud("ft710"), 38400)

    def test_icom_defaults_are_115200(self):
        for model in ("ic7300", "ic7300mk2"):
            with self.subTest(model=model):
                self.assertEqual(self._baud(model), 115200)

    def test_explicit_baud_overrides_backend_default(self):
        self.assertEqual(
            self._baud("ic7300", MRRC_BAUD_RATE="57600"), 57600)
```

- [ ] **步骤 2：验证红灯**

运行：

```bash
venv/bin/python -m unittest \
  tests.test_ic7300_runtime_reliability.BackendBaudDefaultsTests -v
```

预期：IC 两个 model 的默认值断言失败，实际为 38400；FT-710 与显式覆盖通过。

- [ ] **步骤 3：最小实现**

在 `config.py` 的 `RADIO_MODEL` 之后定义后端默认值，并用于 `BAUD_RATE`：

```python
_DEFAULT_BAUD_BY_MODEL = {
    "ft710": 38400,
    "ic7300": 115200,
    "ic7300mk2": 115200,
}
DEFAULT_BAUD_RATE = _DEFAULT_BAUD_BY_MODEL.get(RADIO_MODEL, 38400)
BAUD_RATE = _env_int("MRRC_BAUD_RATE", DEFAULT_BAUD_RATE)
```

删除原来的固定 `BAUD_RATE = ...38400`，保留显式环境变量和旧前缀兼容逻辑。

- [ ] **步骤 4：验证绿灯**

重复步骤 2 命令，预期 3 个测试方法全部通过。

### 任务 2：修复 CI-V 现场诊断脚本

**文件：**
- 修改：`tests/test_ic7300_runtime_reliability.py`
- 修改：`_diag_ic7300_scope.py`

- [ ] **步骤 1：添加失败测试**

```python
class IC7300DiagnosticProtocolTests(unittest.TestCase):
    def test_frequency_query_has_no_checksum_byte(self):
        import _diag_ic7300_scope as diag
        self.assertEqual(
            diag.build_frame(0x03, to=0x94),
            bytes.fromhex("FE FE 94 E0 03 FD"),
        )

    def test_standard_radio_reply_parses_and_decodes_frequency(self):
        import _diag_ic7300_scope as diag
        parser = diag.CivFrameParser()
        frames = parser.feed(bytes.fromhex(
            "FE FE E0 94 03 00 40 07 14 00 FD"
        ))
        self.assertEqual(len(frames), 1)
        self.assertEqual(diag.decode_freq_bcd(frames[0].data), 14_074_000)
```

- [ ] **步骤 2：验证红灯**

运行：

```bash
venv/bin/python -m unittest \
  tests.test_ic7300_runtime_reliability.IC7300DiagnosticProtocolTests -v
```

预期：首个测试显示错误的额外字节 `77`；第二个测试因脚本自有 checksum parser 丢弃标准帧而失败。

- [ ] **步骤 3：最小实现**

在 `_diag_ic7300_scope.py` 中删除重复的 `build_frame()` 和 `parse_frames()`，改为：

```python
from backends.ic7300.civ_codec import (
    CivFrameParser,
    build_frame,
    decode_freq_bcd,
    parse_scope_segment,
)
```

主循环使用 `parser.feed(chunk)`；频率用 `decode_freq_bcd(frame.data[:5])`；scope 用 `parse_scope_segment(frame)` 判断 sequence-1。所有字段改用 `CivFrame.to/from_addr/command/data`。`finally` 中发送：

```python
ser.write(build_frame(0x27, bytes((0x11, 0x00)), to=CIV_TO))
ser.flush()
ser.close()
```

文档头同步为 CI-V 无 checksum，并保留 PTT 只读、不发送 key-up 命令的安全说明。

- [ ] **步骤 4：验证绿灯**

重复步骤 2 命令，预期 2 个测试通过。

### 任务 3：限制 CI-V scope 分片积压

**文件：**
- 修改：`tests/test_ic7300_runtime_reliability.py`
- 修改：`backends/ic7300/civ_controller.py:37-48,149-188,334-350`

- [ ] **步骤 1：添加失败测试**

```python
class ScopeQueueFreshnessTests(unittest.TestCase):
    def test_scope_queue_is_bounded(self):
        ctl = CivController("/dev/null")
        self.assertGreater(ctl.scope_queue.maxsize, 0)

    def test_full_queue_drops_oldest_and_keeps_latest(self):
        ctl = CivController("/dev/null")
        for seq in range(ctl.scope_queue.maxsize + 5):
            ctl._enqueue_scope_segment(seq)
        self.assertEqual(ctl.scope_queue.qsize(), ctl.scope_queue.maxsize)
        self.assertEqual(ctl.scope_queue.get_nowait(), 5)
        newest = None
        while not ctl.scope_queue.empty():
            newest = ctl.scope_queue.get_nowait()
        self.assertEqual(newest, ctl.scope_queue.maxsize + 4)
        self.assertEqual(ctl.scope_queue_drops, 5)
```

- [ ] **步骤 2：验证红灯**

运行：

```bash
venv/bin/python -m unittest \
  tests.test_ic7300_runtime_reliability.ScopeQueueFreshnessTests -v
```

预期：`maxsize` 为 0，且 `_enqueue_scope_segment` 不存在。

- [ ] **步骤 3：最小实现**

在 `civ_controller.py` 定义：

```python
SCOPE_QUEUE_MAX_SEGMENTS = 44  # four complete 11-segment USB waveforms
```

构造函数使用 `asyncio.Queue(maxsize=SCOPE_QUEUE_MAX_SEGMENTS)`，初始化 `scope_queue_drops = 0`。新增 `_enqueue_scope_segment()`：满时 `get_nowait()` 丢最旧值、递增计数并在第 1 次及每 100 次记录 WARNING，然后 `put_nowait(segment)`。`_demux()` 改为调用该方法。

- [ ] **步骤 4：验证绿灯和既有组装测试**

```bash
venv/bin/python -m unittest \
  tests.test_ic7300_runtime_reliability.ScopeQueueFreshnessTests \
  tests.test_civ_controller tests.test_civ_scope -v
```

预期：全部通过；scope reader 永不因队列满而阻塞。

### 任务 4：恢复 20–30 FPS 且避免重复真实帧

**文件：**
- 修改：`tests/test_ic7300_runtime_reliability.py`
- 修改：`server.py:523-593`

- [ ] **步骤 1：添加失败测试**

```python
class SpectrumBroadcastPolicyTests(unittest.TestCase):
    def test_broadcast_target_is_30_fps(self):
        self.assertEqual(server.SPECTRUM_BROADCAST_FPS, 30)

    def test_real_scope_frame_is_not_repeated(self):
        scope = _FakeScope(connected=True, frame_count=7, payload=b"frame-7")
        payload, cursor = server._new_real_spectrum_frame(scope, -1)
        self.assertEqual((payload, cursor), (b"frame-7", 7))
        self.assertEqual(server._new_real_spectrum_frame(scope, cursor), (None, cursor))

    def test_new_real_scope_frame_advances_cursor(self):
        scope = _FakeScope(connected=True, frame_count=8, payload=b"frame-8")
        self.assertEqual(
            server._new_real_spectrum_frame(scope, 7),
            (b"frame-8", 8),
        )
```

`_FakeScope` 只实现 `connected`、`_frame_count` 和 `get_spectrum_binary()`。

- [ ] **步骤 2：验证红灯**

运行：

```bash
venv/bin/python -m unittest \
  tests.test_ic7300_runtime_reliability.SpectrumBroadcastPolicyTests -v
```

预期：常量和 helper 不存在。

- [ ] **步骤 3：最小实现**

在 `server.py` 增加：

```python
SPECTRUM_BROADCAST_FPS = 30


def _new_real_spectrum_frame(scope_handler, last_frame_count):
    frame_count = scope_handler._frame_count
    if frame_count == last_frame_count:
        return None, last_frame_count
    return scope_handler.get_spectrum_binary(), frame_count
```

`_broadcast_spectrum_loop()` 使用 `interval = 1.0 / SPECTRUM_BROADCAST_FPS` 和 `last_real_frame_count = -1`。真实 scope 走 helper；返回 `None` 时只 sleep，不发送。fallback 每周期调用 `update_from_radio_state()` 并发送。日志把固定 “FT4222” 改为通用 “real scope”。

- [ ] **步骤 4：验证绿灯与 WS 协议回归**

```bash
venv/bin/python -m unittest \
  tests.test_ic7300_runtime_reliability.SpectrumBroadcastPolicyTests \
  tests.test_server_ws_protocol tests.test_scope_pipe_restart -v
```

预期：全部通过；WebSocket payload 格式不变。

### 任务 5：增强音频设备链路日志

**文件：**
- 修改：`tests/test_ic7300_runtime_reliability.py`
- 修改：`audio_handler.py:101-179,202-359,472-697`

- [ ] **步骤 1：添加失败测试**

```python
class AudioDeviceDiagnosticsTests(unittest.TestCase):
    def test_device_summary_includes_host_api_and_rates(self):
        handler = AudioHandler.__new__(AudioHandler)
        handler._pa = _FakePyAudioForDiagnostics()
        summary = handler._device_summary(0, actual_rate=48000, channels=1)
        self.assertIn("host=Core Audio", summary)
        self.assertIn("default=44100Hz", summary)
        self.assertIn("actual=48000Hz", summary)
        self.assertIn("channels=1", summary)

    def test_device_summary_survives_missing_host_api_metadata(self):
        handler = AudioHandler.__new__(AudioHandler)
        handler._pa = _FakePyAudioForDiagnostics(missing_host=True)
        self.assertIn("host=unknown", handler._device_summary(0, 48000, 1))
```

Fake 提供 `get_device_info_by_index()` 和可选失败的 `get_host_api_info_by_index()`。

- [ ] **步骤 2：验证红灯**

```bash
venv/bin/python -m unittest \
  tests.test_ic7300_runtime_reliability.AudioDeviceDiagnosticsTests -v
```

预期：`_device_summary` 不存在。

- [ ] **步骤 3：最小实现**

新增容错 `_device_summary(device_index, actual_rate, channels)`，从设备的 `hostApi` 读取 host API 名称，缺失/异常时使用 `unknown`。在：

- `_init_pyaudio()` 的枚举行记录 host；
- `start_rx()` 成功日志记录 summary；
- `start_tx()` 成功日志记录 summary；
- generic USB 匹配文案使用 `radio USB audio input/output`；
- 多设备警告指向 `MRRC_AUDIO_RX_DEVICE` / `MRRC_AUDIO_TX_DEVICE`，不再只写旧 FT710 名称。

选择顺序和采样率计算不变。

- [ ] **步骤 4：验证绿灯和完整音频测试**

```bash
venv/bin/python -m unittest \
  tests.test_ic7300_runtime_reliability.AudioDeviceDiagnosticsTests \
  tests.test_audio -v
```

预期：全部通过；既有 IC 48 kHz passthrough 和 FT-710 44.1 kHz SRC 测试保持绿色。

### 任务 6：文档同步

**文件：**
- 修改：`SDD/09-architecture-overview.md`
- 修改：`SDD/12-operational-model.md`
- 修改：`SDD/14-version-history.md`
- 修改：`SDD/README.md`
- 修改：`README.md`
- 修改：`docs/OPERATION_GUIDE.md`
- 修改：`AGENTS.md`
- 修改：`tests/README.md`

- [ ] **步骤 1：同步架构与运维事实**

记录：IC 默认 115200、显式环境覆盖；CI-V queue=44 分片并丢最旧；真实频谱约 30 Hz 且只发新帧；音频日志字段。将 SDD Quick Facts 从 V2.23 升到 V2.24，在版本历史顶部新增 2026-08-26 条目。

- [ ] **步骤 2：同步用户运行示例**

README 与操作指南明确：

```bash
MRRC_RADIO_MODEL=ic7300 \
MRRC_SERIAL_PORT=/dev/cu.usbserial-... \
MRRC_BAUD_RATE=115200 \
python server.py
```

MK2 使用 `IC7300MK2_CIV_ADDR=0xB6`；音频冲突时用 `MRRC_AUDIO_RX_DEVICE` / `MRRC_AUDIO_TX_DEVICE` 锁定。

- [ ] **步骤 3：同步测试统计**

将 `tests/README.md` 更新为 617 tests / 30 modules，新增 `test_ic7300_runtime_reliability.py` 的 12 项职责说明。

### 任务 7：完整验证

- [ ] **步骤 1：LSP 与静态诊断**

```text
lsp_diagnostics(paths=[config.py, civ_controller.py, server.py,
_diag_ic7300_scope.py, audio_handler.py,
tests/test_ic7300_runtime_reliability.py], serverScope="primary")
```

只接受没有本次新增 blocking error；记录并区分项目既有 Pyright 告警。

- [ ] **步骤 2：运行完整测试**

```bash
venv/bin/python -m unittest discover -s tests -v
```

预期：617 tests，0 failures，0 errors；可选依赖缺失只允许既有 skip。

- [ ] **步骤 3：编译检查**

```bash
venv/bin/python -m py_compile \
  config.py server.py audio_handler.py _diag_ic7300_scope.py \
  backends/ic7300/civ_controller.py \
  tests/test_ic7300_runtime_reliability.py
```

预期：退出码 0，无输出。

- [ ] **步骤 4：SDD Guardian 与诊断汇总**

```bash
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check \
  config.py server.py audio_handler.py _diag_ic7300_scope.py \
  backends/ic7300/civ_controller.py
```

随后运行 `lens_diagnostics(mode="all")`，确保本次编辑文件无 blocking error。不要暂存或提交，除非用户另行要求。

- [ ] **步骤 5：生成实机验收命令**

向用户提供 IC-7300 与 MK2 的安全启动命令、修复后的 scope 诊断命令，以及应出现的关键日志：115200 baud、正确 CI-V 地址、scope complete waveform、RX/TX actual=48000Hz、TX session queue_drops=0。
