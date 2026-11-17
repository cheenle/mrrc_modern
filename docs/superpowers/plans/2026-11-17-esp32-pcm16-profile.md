# ESP32-C3 PCM16 单模音频档位 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留现有 48 kHz 服务端主总线和全部旧客户端行为的前提下，为 PCM16 单模 ESP32-C3 提供按音频 socket 协商的 16 kHz RX/TX 通路。

**Architecture:** `audio_profile.py` 负责纯协议解析与应答，`audio_resample.py` 负责有状态抗混叠转换和 20 ms PCM16 分帧；`server.py` 按 socket 分流 RX，并让 TX 全链路显式携带输入采样率。Legacy 默认路径继续使用 Opus48/PCM48，PCM16 使用新标签 `0x02`，绝不按连接重新解释旧标签。

**Tech Stack:** Python 3、FastAPI WebSocket、NumPy、现有 soxr 依赖、unittest；SDD/AGENTS/网站生成文档同步。

**设计权威:** `docs/superpowers/specs/2026-11-17-voice-16k-design.md`（AD-038）。本计划只覆盖 `mrrc_modern` 服务端；ESP32 固件工程获得路径后另写固件计划，本计划 Task 10 给出对接验收。

---

## 文件结构

| 文件 | 责任 |
| --- | --- |
| `audio_profile.py`（新建） | `audioCaps` 白名单解析、profile/PCM16 常量、成功与拒绝应答；纯标准库 |
| `audio_resample.py` | 保留现有无状态 legacy SRC；新增 soxr LQ 流式转换和 PCM16 20 ms 分帧 |
| `opus_rx.py` | 重新导出 `AUDIO_TAG_PCM16`，现有 Opus48 常量不变 |
| `server.py` | RX/TX socket profile 生命周期、RX 分组 fan-out、TX 输入率与失败强制 RX |
| `audio_handler.py` | PyAudio legacy 48 kHz 路径不变；PCM16 直接转换到后端设备率 |
| `backends/{icom_net,flex,sunsdr,yaesu/scu_lan}/audio.py` | 透传或实现显式 `input_rate`，不改变各自外部协议 |
| `tests/test_audio_profile.py`（新建） | 纯协议、协商、RX 分流、TX 协商与生命周期测试 |
| `tests/test_audio_resample.py`（新建） | 流式 SRC、抗混叠、跨块一致性、PCM16 分帧 |
| `tests/test_pcm16_backends.py`（新建） | Icom/SCU 委托、Flex、SunSDR 的显式输入率及流重置 |
| `tests/test_audio.py`、`test_recorder_api.py`、后端音频测试 | legacy 回归和各后端输入率测试 |
| `SDD/*`、`AGENTS.md`、`README.md`、`tests/README.md` | AD-038、协议和测试事实同步 |

## Task 1: 把“禁止任何 16 kHz”守护改成 PCM16 边界守护

**Files:**
- Modify: `.agents/skills/sdd-guardian/harness/constraints.json:170-188`
- Modify: `.agents/skills/sdd-guardian/SKILL.md:69-70`
- Modify: `.agents/skills/sdd-guardian/references/constraint-catalog.md:19`

- [ ] **Step 1: 替换过时阻断规则**

将 `audio-16k-rate` 整个对象替换为以下 info 规则。保留命中提醒，但不再阻止经过协商的 PCM16：

```json
{
  "id": "audio-pcm16-profile",
  "title": "48k main bus; 16k only at the negotiated PCM16 boundary",
  "severity": "info",
  "sdd_ref": "AD-011; AD-038",
  "scope": [
    "audio_handler.py",
    "audio_resample.py",
    "opus_rx.py",
    "static/tx_opus_worker.js",
    "static/tx_capture_worklet.js",
    "static/rx_worklet_processor.js",
    "static/ft710_main.js"
  ],
  "exclude_scope": [],
  "patterns": [
    "\\b16000\\b",
    "\\b320\\s*samples"
  ],
  "message": "The main voice bus stays 48 kHz. 16 kHz is allowed only for the negotiated 0x02 PCM16 profile and its explicit streaming-SRC boundary; never retag PCM48 or reopen a USB device at an assumed rate."
}
```

- [ ] **Step 2: 同步守护说明**

将 SKILL 的音频 golden rule 改为：

```text
- Audio: the main voice bus stays 48 kHz. The negotiated ESP32 PCM16 profile
  uses tag 0x02 and explicit streaming SRC at the server/backend boundary
  (AD-038); never reinterpret legacy 0x00 PCM48 as 16 kHz.
```

将 constraint catalog 的对应行改为：

```markdown
| `audio-pcm16-profile` | 48 kHz main bus; 16 kHz only at the negotiated PCM16 boundary | AD-011, AD-038 | Legacy clients stay on Opus48/PCM48. PCM16 is a per-audio-socket profile with self-describing tag `0x02`; USB and external-protocol rates remain explicit. |
```

- [ ] **Step 3: 验证守护不再错误阻断**

Run:

```bash
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check audio_resample.py audio_handler.py
```

Expected: `SDD-GUARDIAN: clean — no constraint violations.`；info 可以出现，退出码必须为 0。

- [ ] **Step 4: Commit**

```bash
git add .agents/skills/sdd-guardian/harness/constraints.json .agents/skills/sdd-guardian/SKILL.md .agents/skills/sdd-guardian/references/constraint-catalog.md
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "chore(harness): 允许协商式 PCM16 音频边界"
```

## Task 2: 建立 PCM16 协议常量与能力应答

**Files:**
- Create: `audio_profile.py`
- Modify: `opus_rx.py:39-42`
- Test: `tests/test_audio_profile.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_audio_profile.py`：

```python
import json
import unittest

import audio_profile
from opus_rx import AUDIO_TAG_OPUS, AUDIO_TAG_PCM, AUDIO_TAG_PCM16


class AudioCapsTests(unittest.TestCase):
    def test_pcm16_constants(self):
        self.assertEqual(AUDIO_TAG_PCM16, 0x02)
        self.assertEqual(len({AUDIO_TAG_PCM, AUDIO_TAG_OPUS, AUDIO_TAG_PCM16}), 3)
        self.assertEqual(audio_profile.PCM16_RATE, 16000)
        self.assertEqual(audio_profile.PCM16_FRAME_BYTES, 640)

    def test_only_the_exact_pcm16_caps_are_accepted(self):
        text = '{"type":"audioCaps","profile":"pcm16"}'
        self.assertEqual(audio_profile.parse_caps(text), audio_profile.PROFILE_PCM16)
        for bad in (None, b"{}", "not json", "{}",
                    '{"type":"audioCaps","profile":"pcm48"}',
                    '{"type":"other","profile":"pcm16"}'):
            self.assertIsNone(audio_profile.parse_caps(bad))

    def test_accepted_response_describes_the_wire_format(self):
        payload = json.loads(audio_profile.accepted_caps())
        self.assertEqual(payload, {
            "type": "audioCaps", "profile": "pcm16", "accepted": True,
            "rate": 16000, "channels": 1, "format": "s16le", "frameMs": 20,
        })

    def test_rejected_response_names_the_reason(self):
        payload = json.loads(audio_profile.rejected_caps("pcm16-tx-unsupported"))
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["reason"], "pcm16-tx-unsupported")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行并确认失败**

Run: `.venv/bin/python -m unittest tests.test_audio_profile -v`
Expected: `ModuleNotFoundError: No module named 'audio_profile'`。

- [ ] **Step 3: 实现纯协议模块**

创建 `audio_profile.py`：

```python
"""Per-socket main-voice audio profiles (AD-038); stdlib only."""
from __future__ import annotations

import json

PROFILE_LEGACY = "legacy"
PROFILE_PCM16 = "pcm16"
PCM16_RATE = 16000
PCM16_FRAME_MS = 20
PCM16_FRAME_BYTES = PCM16_RATE * PCM16_FRAME_MS // 1000 * 2
AUDIO_TAG_PCM16 = 0x02


def parse_caps(text: str | bytes | None) -> str | None:
    if not isinstance(text, str):
        return None
    try:
        msg = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get("type") != "audioCaps" or msg.get("profile") != PROFILE_PCM16:
        return None
    return PROFILE_PCM16


def accepted_caps() -> str:
    return json.dumps({
        "type": "audioCaps", "profile": PROFILE_PCM16, "accepted": True,
        "rate": PCM16_RATE, "channels": 1, "format": "s16le",
        "frameMs": PCM16_FRAME_MS,
    }, separators=(",", ":"))


def rejected_caps(reason: str) -> str:
    return json.dumps({
        "type": "audioCaps", "profile": PROFILE_PCM16,
        "accepted": False, "reason": reason,
    }, separators=(",", ":"))
```

在 `opus_rx.py` 标签区加入：

```python
from audio_profile import AUDIO_TAG_PCM16
```

- [ ] **Step 4: 运行新测试和 legacy 标签测试**

Run: `.venv/bin/python -m unittest tests.test_audio_profile tests.test_audio.CodecTagTests tests.test_audio.OpusConstantsTests -v`
Expected: 全部 `OK`，`RX_RATE=48000`、`FRAME_SAMPLES=960`、`DEFAULT_BITRATE=64000` 不变。

- [ ] **Step 5: Commit**

```bash
git add audio_profile.py opus_rx.py tests/test_audio_profile.py
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(audio): 添加 PCM16 档位协议"
```

## Task 3: 实现抗混叠流式转换和 PCM16 分帧

**Files:**
- Modify: `audio_resample.py`
- Test: `tests/test_audio_resample.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_audio_resample.py`：

```python
import unittest
import numpy as np

from audio_profile import AUDIO_TAG_PCM16
from audio_resample import Pcm16RxEncoder, StreamingPcmResampler


def tone(rate, freq, seconds=1.0):
    t = np.arange(round(rate * seconds), dtype=np.float64) / rate
    return (np.sin(2 * np.pi * freq * t) * 30000).astype("<i2")


class StreamingResamplerTests(unittest.TestCase):
    def test_chunking_matches_one_shot_stream(self):
        pcm = tone(48000, 1000).astype("<i2").tobytes()
        whole = StreamingPcmResampler(48000, 16000)
        expected = whole.process(pcm) + whole.flush()
        chunked = StreamingPcmResampler(48000, 16000)
        actual = bytearray()
        for offset in range(0, len(pcm), 1782):
            actual.extend(chunked.process(pcm[offset:offset + 1782]))
        actual.extend(chunked.flush())
        self.assertEqual(bytes(actual), expected)
        self.assertEqual(len(expected), 16000 * 2)

    def test_downsample_rejects_out_of_band_tone(self):
        source = tone(48000, 12000)
        rs = StreamingPcmResampler(48000, 16000)
        out = np.frombuffer(rs.process(source.astype("<i2").tobytes()) + rs.flush(), dtype="<i2")
        self.assertLess(np.sqrt(np.mean(out[1600:].astype(np.float64) ** 2)), 100.0)

    def test_pcm16_encoder_emits_only_complete_tagged_frames(self):
        enc = Pcm16RxEncoder()
        frames = []
        pcm = tone(48000, 1000).astype("<i2").tobytes()
        for offset in range(0, len(pcm), 1920):
            frames.extend(enc.push(pcm[offset:offset + 1920]))
        frames.extend(enc.flush())
        self.assertEqual(len(frames), 50)
        for frame in frames:
            self.assertEqual(len(frame), 641)
            self.assertEqual(frame[0], AUDIO_TAG_PCM16)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行并确认失败**

Run: `.venv/bin/python -m unittest tests.test_audio_resample -v`
Expected: `ImportError: cannot import name 'Pcm16RxEncoder'`。

- [ ] **Step 3: 实现流式类**

在 `audio_resample.py` 保留现有 `resample_pcm()` 不变，并追加：

```python
from audio_profile import AUDIO_TAG_PCM16, PCM16_FRAME_BYTES


class StreamingPcmResampler:
    def __init__(self, in_rate: int, out_rate: int):
        if in_rate <= 0 or out_rate <= 0 or in_rate == out_rate:
            raise ValueError("SRC requires two different positive rates")
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self._stream = None
        self.reset()

    def reset(self):
        import soxr
        self._stream = soxr.ResampleStream(
            self.in_rate, self.out_rate, 1, dtype="float32", quality="LQ")

    def _convert(self, pcm: bytes, last: bool) -> bytes:
        usable = len(pcm) - len(pcm) % 2
        samples = np.frombuffer(pcm[:usable], dtype="<i2")
        audio = self._stream.resample_chunk(samples.astype(np.float32), last=last)
        return np.clip(np.rint(audio), -32768, 32767).astype("<i2").tobytes()

    def process(self, pcm: bytes) -> bytes:
        return self._convert(pcm, last=False)

    def flush(self) -> bytes:
        return self._convert(b"", last=True)


class Pcm16RxEncoder:
    def __init__(self):
        self._resampler = StreamingPcmResampler(48000, 16000)
        self._tail = b""

    def _frames(self, converted: bytes) -> list[bytes]:
        buf = self._tail + converted
        frames = []
        offset = 0
        while len(buf) - offset >= PCM16_FRAME_BYTES:
            frames.append(bytes([AUDIO_TAG_PCM16]) + buf[offset:offset + PCM16_FRAME_BYTES])
            offset += PCM16_FRAME_BYTES
        self._tail = buf[offset:]
        return frames

    def push(self, pcm: bytes) -> list[bytes]:
        return self._frames(self._resampler.process(pcm))

    def flush(self) -> list[bytes]:
        return self._frames(self._resampler.flush())

    def reset(self):
        self._resampler.reset()
        self._tail = b""
```

- [ ] **Step 4: 验证新测试与 legacy SRC 回归**

Run: `.venv/bin/python -m unittest tests.test_audio_resample tests.test_audio.ParameterizedRateTests tests.test_audio.TxDeviceDomainTests -v`
Expected: 全部 `OK`；旧 `resample_pcm(960↔882)` 测试不变。

- [ ] **Step 5: Commit**

```bash
git add audio_resample.py tests/test_audio_resample.py
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(audio): 添加抗混叠 PCM16 流式编码器"
```

## Task 4: RX 按 socket 协商并分组 fan-out

**Files:**
- Modify: `server.py:123-127,2251-2280,2348-2517,6329-6354`
- Modify: `tests/test_audio.py:413-465`
- Test: `tests/test_audio_profile.py`

- [ ] **Step 1: 写失败测试**

向 `tests/test_audio_profile.py` 增加：

```python
from unittest import mock
import server


class RxProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_audio = server.audio
        self.old_encoder = getattr(server, "_pcm16_rx_encoder", None)
        server._audio_rx_profiles = {}
        server._audio_tx_profiles = {}

    def tearDown(self):
        server.audio = self.old_audio
        server._pcm16_rx_encoder = self.old_encoder
        server._audio_rx_profiles = {}
        server._audio_tx_profiles = {}

    def test_last_pcm16_listener_resets_the_encoder(self):
        ws = object()
        encoder = mock.Mock()
        server._pcm16_rx_encoder = encoder
        server._audio_rx_profiles = {ws: "pcm16"}
        server._audio_rx_profiles.pop(ws)
        server._reset_pcm16_rx_encoder_if_idle()
        encoder.reset.assert_called_once_with()
        self.assertIsNone(server._pcm16_rx_encoder)

    def test_pcm16_only_skips_legacy_opus(self):
        audio = mock.Mock()
        audio.encode_rx_audio.side_effect = AssertionError("legacy Opus must not run")
        server.audio = audio
        encoder = mock.Mock()
        encoder.push.return_value = [b"\x02pcm"]
        server._pcm16_rx_encoder = encoder
        legacy, pcm16 = server._encode_rx_for_profiles(b"pcm48", set(), {object()})
        self.assertEqual((legacy, pcm16), ([], [b"\x02pcm"]))

    def test_mixed_listeners_share_one_conversion_per_group(self):
        audio = mock.Mock()
        audio.encode_rx_audio.return_value = [b"\x01opus"]
        server.audio = audio
        encoder = mock.Mock()
        encoder.push.return_value = [b"\x02pcm"]
        server._pcm16_rx_encoder = encoder
        legacy, pcm16 = server._encode_rx_for_profiles(
            b"pcm48", {object(), object()}, {object(), object()})
        self.assertEqual((legacy, pcm16), ([b"\x01opus"], [b"\x02pcm"]))
        audio.encode_rx_audio.assert_called_once_with(b"pcm48")
        encoder.push.assert_called_once_with(b"pcm48")

    async def test_rx_caps_are_per_socket(self):
        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=False)
        self.assertEqual(server._audio_rx_profiles[ws], "pcm16")
        ws.send_text.assert_awaited_once()
        self.assertNotIn(ws, server._audio_tx_profiles)


if __name__ == "__main__":
    unittest.main()
```

文件已有 `unittest.main()` 时，把测试类插入其前，不重复添加入口。

- [ ] **Step 2: 运行并确认失败**

Run: `.venv/bin/python -m unittest tests.test_audio_profile.RxProfileTests -v`
Expected: `AttributeError`（`_encode_rx_for_profiles` / `_handle_audio_caps` 尚不存在）。

- [ ] **Step 3: 实现 profile 状态、协商和分组编码**

在 server 的音频客户端状态附近加入：

```python
from audio_profile import PROFILE_PCM16, accepted_caps, rejected_caps, parse_caps
from audio_resample import Pcm16RxEncoder, StreamingPcmResampler

_audio_rx_profiles: dict[WebSocket, str] = {}
_audio_tx_profiles: dict[WebSocket, str] = {}
_pcm16_rx_encoder = None


def _pcm16_tx_supported() -> bool:
    if audio is None:
        return False
    try:
        if not getattr(audio, "tx_audio_available", True):
            return False
        device_rate = int(audio.device_rates["tx"])
        if device_rate != 16000:
            StreamingPcmResampler(16000, device_rate)
    except (AttributeError, KeyError, TypeError, ValueError, OSError, ImportError):
        return False
    return True


async def _handle_audio_caps(ws: WebSocket, text: str, *, tx: bool) -> None:
    profile = parse_caps(text)
    if profile != PROFILE_PCM16:
        return
    if tx and not _pcm16_tx_supported():
        await ws.send_text(rejected_caps("pcm16-tx-unsupported"))
        return
    profiles = _audio_tx_profiles if tx else _audio_rx_profiles
    profiles[ws] = profile
    await ws.send_text(accepted_caps())


def _reset_pcm16_rx_encoder_if_idle() -> None:
    global _pcm16_rx_encoder
    if PROFILE_PCM16 in _audio_rx_profiles.values():
        return
    if _pcm16_rx_encoder is not None:
        _pcm16_rx_encoder.reset()
        _pcm16_rx_encoder = None


def _encode_pcm16_rx(pcm: bytes) -> list[bytes]:
    global _pcm16_rx_encoder
    if _pcm16_rx_encoder is None:
        _pcm16_rx_encoder = Pcm16RxEncoder()
    return _pcm16_rx_encoder.push(pcm)


def _encode_rx_for_profiles(pcm, legacy_clients, pcm16_clients):
    legacy_frames = audio.encode_rx_audio(pcm) if legacy_clients else []
    pcm16_frames = _encode_pcm16_rx(pcm) if pcm16_clients else []
    return legacy_frames, pcm16_frames


async def _close_pcm16_listeners(clients):
    for ws in clients:
        try:
            await ws.close(code=4004, reason="PCM16 resampler unavailable")
        except Exception:
            pass
        audio_rx_clients.discard(ws)
        _audio_rx_profiles.pop(ws, None)
    _reset_pcm16_rx_encoder_if_idle()
```

在 `ws_audio_rx` 的 `await ws.receive()` 后处理文本，并在 `finally` 清理 profile 后重置空闲转换器：

```python
msg = await ws.receive()
if msg.get("type") == "websocket.disconnect":
    break
if msg.get("text"):
    await _handle_audio_caps(ws, msg["text"], tx=False)

# finally 中，现有 audio_rx_clients.discard(ws) 之后：
_audio_rx_profiles.pop(ws, None)
_reset_pcm16_rx_encoder_if_idle()
```

- [ ] **Step 4: 替换 RX 广播块**

在 `_audio_rx_loop` 中保留录音、FT8、本地扬声器和 TX 静音逻辑，用以下分组逻辑替换原 `_needs_frames`、统一 `frames` 和统一发送块：

```python
pcm16_clients = {ws for ws in audio_rx_clients
                 if _audio_rx_profiles.get(ws) == PROFILE_PCM16}
legacy_clients = set(audio_rx_clients) - pcm16_clients
frames = pcm16_frames = None
if not _radio_txing and (legacy_clients or pcm16_clients):
    pcm_for_clients = _rx_pcm_for_clients(pcm)
    try:
        frames, pcm16_frames = _encode_rx_for_profiles(
            pcm_for_clients, legacy_clients, pcm16_clients)
    except Exception as exc:
        logger.warning("PCM16 RX conversion failed: %s", exc)
        await _close_pcm16_listeners(pcm16_clients)
        pcm16_clients = set()
        pcm16_frames = None
        frames = (audio.encode_rx_audio(pcm_for_clients)
                  if legacy_clients else None)

dead = set()
if frames:
    dead |= await _send_audio_frames_to_clients(frames, legacy_clients)
if pcm16_frames:
    dead |= await _send_audio_frames_to_clients(pcm16_frames, pcm16_clients)
if dead:
    logger.warning("Removed %d dead/slow audio clients", len(dead))
    audio_rx_clients -= dead
    for ws in dead:
        _audio_rx_profiles.pop(ws, None)
    _reset_pcm16_rx_encoder_if_idle()
```

将 `tests/test_audio.py` 中要求 `_needs_frames` 包含录音/FT8 的旧源码断言改为调用上述分组函数的行为断言：PCM16-only 不编码 Opus，legacy-only 继续调用 `audio.encode_rx_audio()`，TX 时两组都不发送。不得删除现有“录音/FT8 让采集保持唤醒”的断言。

- [ ] **Step 5: 运行测试**

Run: `.venv/bin/python -m unittest tests.test_audio_profile tests.test_audio_resample tests.test_audio.RXBackpressureTests tests.test_audio_rx_pacing -v`
Expected: 全部 `OK`，legacy 发送超时隔离测试仍通过。

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_audio.py tests/test_audio_profile.py
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(audio): PCM16 RX 按连接协商分流"
```

## Task 5: TX 服务端与 PyAudio 显式携带输入率

**Files:**
- Modify: `server.py:1046-1050,1791-1816`
- Modify: `audio_handler.py:1096-1109`及 TX start/stop 重置点
- Test: `tests/test_audio.py`、`tests/test_recorder_api.py`、`tests/test_cq_server.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_audio.py::ParameterizedRateTests` 增加：

```python
    def test_tx_pcm16_uses_streaming_direct_device_bridge(self):
        frame = b"\x01\x02" * 320
        h = self._tx_handler(44100)
        with patch("audio_handler.StreamingPcmResampler") as cls:
            cls.return_value.process.return_value = b"\x03\x04" * 882
            h.feed_tx_audio(frame, input_rate=16000)
        cls.assert_called_once_with(16000, 44100)
        self.assertEqual(len(h._tx_queue[0]), 882 * 2)

    def test_legacy_tx_does_not_construct_streaming_bridge(self):
        h = self._tx_handler(44100)
        with patch("audio_handler.StreamingPcmResampler") as cls:
            h.feed_tx_audio(b"\x01\x02" * 960)
        cls.assert_not_called()
```

在 `tests/test_recorder_api.py` 增加：

```python
    def test_tx_tap_preserves_pcm16_input_rate(self):
        server._rec_tap_tx(_block(320), rate=16000)
        source, _pcm, rate, _ts = server._rec_queue.get_nowait()
        self.assertEqual((source, rate), ("tx", 16000))
```

在 CQ server 测试中给 fake audio 增加 `feed_tx_audio(self, pcm, input_rate=48000)` 记录，并断言默认调用为 48000、PCM16 调用为 16000；已有只收 `pcm` 的 fake 必须继续工作。

- [ ] **Step 2: 运行并确认失败**

Run: `.venv/bin/python -m unittest tests.test_audio.ParameterizedRateTests tests.test_recorder_api tests.test_cq_server -v`
Expected: PCM16 用例因 `input_rate` / `StreamingPcmResampler` 不存在而失败。

- [ ] **Step 3: 修改服务端入口**

使用以下完整函数签名和核心路径，legacy 调用保持只传位置参数：

```python
def _rec_tap_tx(pcm: bytes, rate: int = TX_RATE) -> None:
    if pcm:
        _rec_enqueue("tx", pcm, rate)


async def _feed_tx_from_uplink(pcm: bytes, input_rate: int = TX_RATE) -> None:
    global _tx_cq_mic_drops
    if _ft8_transmitting():
        return
    if _cq_player is not None and _cq_player.is_calling:
        _tx_cq_mic_drops += 1
        return
    if not audio:
        return
    if input_rate <= 0:
        raise ValueError("input_rate must be positive")
    pcm, clipped = audio_levels.apply_gain(pcm, _audio_levels.tx_mic_db)
    _audio_levels_clip.note("tx", len(pcm) // 2, clipped,
                            gain_db=_audio_levels.tx_mic_db,
                            now=time.monotonic())
    _rec_tap_tx(pcm, rate=input_rate)
    if input_rate == TX_RATE:
        audio.feed_tx_audio(pcm)
    else:
        audio.feed_tx_audio(pcm, input_rate=input_rate)
```

- [ ] **Step 4: 修改 PyAudio feed**

在 `AudioHandler` 顶部导入 `StreamingPcmResampler`，在 `start_tx()` 和 `stop_tx()` 将 `_tx_input_resampler`、`_tx_input_resampler_rates` 重置为 `None`。将 feed 开头改为：

```python
def feed_tx_audio(self, pcm: bytes, input_rate: int = RX_RATE):
    if not pcm or len(pcm) < 2:
        return
    if input_rate == RX_RATE:
        data = pcm if self._tx_dev_rate == RX_RATE else resample_pcm(
            pcm, RX_RATE, self._tx_dev_rate)
    elif input_rate == self._tx_dev_rate:
        data = pcm
    else:
        rates = (input_rate, self._tx_dev_rate)
        if getattr(self, "_tx_input_resampler_rates", None) != rates:
            self._tx_input_resampler = StreamingPcmResampler(*rates)
            self._tx_input_resampler_rates = rates
        data = self._tx_input_resampler.process(pcm)
    if not data:
        return
    # 保留现有 queue、peak、drop 和统计代码，不再改采样率。
```

`__new__` 测试对象缺少新属性时由 `getattr` 保护；legacy `resample_pcm` 分支必须逐字节保持原行为。

- [ ] **Step 5: 运行测试**

Run: `.venv/bin/python -m unittest tests.test_audio tests.test_recorder_api tests.test_cq_server -v`
Expected: 全部 `OK`，包括 legacy 默认调用、录音率、队列上限和 CQ/PTT 回归。

- [ ] **Step 6: Commit**

```bash
git add server.py audio_handler.py tests/test_audio.py tests/test_recorder_api.py tests/test_cq_server.py
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(audio): TX 输入显式支持 PCM16"
```

## Task 6: 各网络后端接受显式输入率

**Files:**
- Modify: `backends/icom_net/audio.py:261-263`
- Modify: `backends/yaesu/scu_lan/audio.py:369-371`
- Modify: `backends/flex/audio.py:280-292`及 start/stop 重置点
- Modify: `backends/sunsdr/audio.py:214-224`及 start/stop 重置点
- Test: `tests/test_pcm16_backends.py`（新建）
- Test legacy: `tests/test_scu_lan_scope_audio.py`、`tests/test_flex_audio.py`、`tests/test_sunsdr2dx_backend.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_pcm16_backends.py`，加入以下可执行用例：

```python
import threading
import unittest
from unittest import mock
import numpy as np
from backends.icom_net.audio import IcomNetAudioHandler
from backends.yaesu.scu_lan.audio import ScuLanAudioHandler
from backends.flex.audio import FlexAudioHandler
from backends.sunsdr.audio import SunSDRAudioHandler


class Pcm16BackendTests(unittest.TestCase):
    def test_delegates_preserve_legacy_and_forward_pcm16(self):
        for cls in (IcomNetAudioHandler, ScuLanAudioHandler):
            with self.subTest(cls=cls.__name__):
                tx = mock.Mock()
                handler = cls.__new__(cls)
                handler._tx = tx
                handler.feed_tx_audio(b"48")
                tx.feed_tx_audio.assert_called_once_with(b"48")
                handler.feed_tx_audio(b"16", input_rate=16000)
                tx.feed_tx_audio.assert_called_with(b"16", input_rate=16000)

    def test_flex_pcm16_uses_a_16_to_24k_stream(self):
        handler = FlexAudioHandler.__new__(FlexAudioHandler)
        handler._closed = False
        handler._engine = mock.Mock()
        handler._engine.tx_stream_ready = True
        handler._tx_encoder = mock.Mock()
        handler._tx_encoder.push.return_value = []
        handler._down2 = mock.Mock()
        handler._tx_pcm16_resampler = None
        handler._tx_lock = threading.Lock()
        handler._tx_written = handler._tx_write_err = handler._tx_peak = 0
        pcm = np.zeros(320, dtype="<i2").tobytes()
        with mock.patch("backends.flex.audio.StreamingPcmResampler") as cls:
            cls.return_value.process.return_value = np.zeros(480, dtype="<i2").tobytes()
            handler.feed_tx_audio(pcm, input_rate=16000)
        cls.assert_called_once_with(16000, 24000)
        handler._tx_encoder.push.assert_called_once()
        handler._down2.process.assert_not_called()

    def test_sunsdr_pcm16_converts_before_engine(self):
        handler = SunSDRAudioHandler.__new__(SunSDRAudioHandler)
        handler._closed = False
        handler._engine = mock.Mock()
        handler._tx_pcm16_resampler = None
        handler._tx_lock = threading.Lock()
        handler._tx_written = handler._tx_peak = 0
        converted = b"\x01\x00" * 960
        with mock.patch("backends.sunsdr.audio.StreamingPcmResampler") as cls:
            cls.return_value.process.return_value = converted
            handler.feed_tx_audio(b"\x00\x00" * 320, input_rate=16000)
        cls.assert_called_once_with(16000, 48000)
        handler._engine.feed_tx_audio.assert_called_once_with(converted)

    def test_network_tx_resampler_reset_helper(self):
        for cls in (FlexAudioHandler, SunSDRAudioHandler):
            with self.subTest(cls=cls.__name__):
                handler = cls.__new__(cls)
                handler._tx_pcm16_resampler = mock.Mock()
                handler._reset_tx_pcm16_resampler()
                self.assertIsNone(handler._tx_pcm16_resampler)
```

在 `start_tx()` 与 `stop_tx()` 的现有测试中断言它们调用 `_reset_tx_pcm16_resampler()`；测试只使用 fake resampler/engine，不依赖电台。

- [ ] **Step 2: 运行并确认失败**

Run: `.venv/bin/python -m unittest tests.test_pcm16_backends tests.test_scu_lan_scope_audio tests.test_flex_audio tests.test_sunsdr2dx_backend -v`
Expected: PCM16 用例出现意外 `input_rate` 或流式桥不存在。

- [ ] **Step 3: 修改两个委托适配器**

IcomNet 和 SCU 使用同一形式，SCU 的 `tx_audio_available=False` 继续阻止成功协商：

```python
def feed_tx_audio(self, pcm: bytes, input_rate: int = RX_RATE) -> None:
    if self._tx is None:
        return
    if input_rate == RX_RATE:
        self._tx.feed_tx_audio(pcm)
    else:
        self._tx.feed_tx_audio(pcm, input_rate=input_rate)
```

- [ ] **Step 4: 修改 Flex 和 SunSDR**

Flex 保留 48 kHz `_down2` 分支，新增：

```python
def feed_tx_audio(self, pcm: bytes, input_rate: int = RX_RATE) -> None:
    if self._closed or not pcm or len(pcm) < 2:
        return
    encoder = self._tx_encoder
    if encoder is None or not self._engine.tx_stream_ready:
        return
    mono = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2")
    if input_rate == RX_RATE:
        converted = self._down2.process(mono)
        pcm24 = np.clip(np.rint(converted), -32768, 32767).astype("<i2")
    elif input_rate == 16000:
        if getattr(self, "_tx_pcm16_resampler", None) is None:
            self._tx_pcm16_resampler = StreamingPcmResampler(16000, 24000)
        pcm24 = np.frombuffer(self._tx_pcm16_resampler.process(pcm), dtype="<i2")
    else:
        raise ValueError(f"unsupported TX input rate: {input_rate}")
    if pcm24.size == 0:
        return
    stereo = np.repeat(pcm24, 2)
    # 后续 encoder.push / VITA / stats 保持原实现。
```

SunSDR 保留 legacy 直喂，新增：

```python
def feed_tx_audio(self, pcm: bytes, input_rate: int = RX_RATE):
    if self._closed or not pcm or len(pcm) < 2:
        return
    if input_rate == RX_RATE:
        converted = pcm
    elif input_rate == 16000:
        if getattr(self, "_tx_pcm16_resampler", None) is None:
            self._tx_pcm16_resampler = StreamingPcmResampler(16000, AUDIO_RATE)
        converted = self._tx_pcm16_resampler.process(pcm)
    else:
        raise ValueError(f"unsupported TX input rate: {input_rate}")
    if not converted:
        return
    self._engine.feed_tx_audio(converted)
    peak = int(np.abs(np.frombuffer(converted, dtype=np.int16)).max())
    # 后续统计保留；written 按 converted 的 48 kHz 样本数计算。
```

两个类都导入 `StreamingPcmResampler`，并定义同一个重置辅助函数；`start_tx()`、`stop_tx()` 在各自现有生命周期点调用它：

```python
def _reset_tx_pcm16_resampler(self) -> None:
    self._tx_pcm16_resampler = None
```

- [ ] **Step 5: 运行后端测试**

Run: `.venv/bin/python -m unittest tests.test_pcm16_backends tests.test_scu_lan_scope_audio tests.test_flex_audio tests.test_sunsdr2dx_backend tests.test_icom_net_audio -v`
Expected: 全部 `OK`；Flex 外部仍为 24 kHz/70 kbps，SunSDR DSP 仍为 48 kHz。

- [ ] **Step 6: Commit**

```bash
git add backends/icom_net/audio.py backends/yaesu/scu_lan/audio.py backends/flex/audio.py backends/sunsdr/audio.py tests/test_pcm16_backends.py tests/test_scu_lan_scope_audio.py tests/test_flex_audio.py tests/test_sunsdr2dx_backend.py tests/test_icom_net_audio.py
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(audio): 网络后端接入 PCM16 输入率"
```

## Task 7: TX 协商、0x02 分支与转换失败强制 RX

**Files:**
- Modify: `server.py:6359-6478`
- Test: `tests/test_audio_profile.py`

- [ ] **Step 1: 写失败测试**

增加：

```python
class TxProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_audio = server.audio
        server._audio_tx_profiles = {}
        server._audio_rx_profiles = {}

    def tearDown(self):
        server.audio = self.old_audio
        server._audio_tx_profiles = {}
        server._audio_rx_profiles = {}

    async def test_tx_accept_is_independent_from_rx(self):
        server.audio = mock.Mock()
        server.audio.device_rates = {"tx": 48000}
        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=True)
        self.assertEqual(server._audio_tx_profiles[ws], "pcm16")
        self.assertNotIn(ws, server._audio_rx_profiles)

    async def test_unavailable_backend_is_rejected(self):
        server.audio = None
        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=True)
        self.assertNotIn(ws, server._audio_tx_profiles)
        payload = json.loads(ws.send_text.await_args.args[0])
        self.assertEqual(payload["reason"], "pcm16-tx-unsupported")

    async def test_missing_soxr_is_rejected_before_ptt(self):
        server.audio = mock.Mock()
        server.audio.device_rates = {"tx": 48000}
        ws = mock.Mock()
        ws.send_text = mock.AsyncMock()
        with mock.patch.object(server, "StreamingPcmResampler", side_effect=ImportError):
            await server._handle_audio_caps(ws, '{"type":"audioCaps","profile":"pcm16"}', tx=True)
        self.assertNotIn(ws, server._audio_tx_profiles)
        payload = json.loads(ws.send_text.await_args.args[0])
        self.assertFalse(payload["accepted"])

    async def test_pcm16_conversion_error_releases_ptt(self):
        cat = mock.Mock()
        cat.connected = True
        cat.set_ptt = mock.AsyncMock()
        with mock.patch.object(server, "cat", cat), \
             mock.patch.object(server, "radio", mock.Mock()), \
             mock.patch.object(server, "scheduler", mock.Mock()):
            await server._force_rx_for_audio_error("bad PCM16")
        cat.set_ptt.assert_awaited_once_with(False)
```

另加源码/行为用例：`ws_audio_tx` 对 `bytes([AUDIO_TAG_PCM16]) + frame` 调用 `_feed_tx_from_uplink(frame, input_rate=16000)`，对旧 `AUDIO_TAG_PCM` 仍默认 48000。

- [ ] **Step 2: 运行并确认失败**

Run: `.venv/bin/python -m unittest tests.test_audio_profile.TxProfileTests -v`
Expected: `_force_rx_for_audio_error` 不存在，TX 协商用例失败。

- [ ] **Step 3: 实现强制 RX 辅助函数**

```python
async def _force_rx_for_audio_error(reason: str) -> None:
    global _ptt_key_ws
    logger.warning("TX audio conversion failed — forcing RX: %s", reason)
    try:
        if cat and cat.connected:
            await cat.set_ptt(False)
    except Exception as exc:
        logger.error("PCM16 force-RX failed: %s", exc)
    _ptt_key_ws = None
    radio.update(tx_status=0, power_meter=0, alc_meter=0,
                 swr_meter=0, comp_meter=0, id_meter=0)
    if scheduler:
        scheduler.skip_next_poll("tx_status", 1.0)
```

- [ ] **Step 4: 接入 WS TX**

在文本处理开头调用 `_handle_audio_caps(ws, text, tx=True)`，并在 `finally` 增加 `_audio_tx_profiles.pop(ws, None)`。二进制解码加入：

```python
elif tag == AUDIO_TAG_PCM16:
    if audio:
        try:
            await _feed_tx_from_uplink(frame, input_rate=16000)
            _tx_session_decoded += 1
        except Exception as exc:
            _tx_session_decode_fail += 1
            await _force_rx_for_audio_error(str(exc))
```

在 import 区同时导入 `AUDIO_TAG_PCM16`。旧 Opus、PCM48、untagged 分支保持原样；`ptt:false` 处理不得读取 profile。

- [ ] **Step 5: 运行协商与 PTT 回归**

Run: `.venv/bin/python -m unittest tests.test_audio_profile tests.test_audio tests.test_server_ws_protocol -v`
Expected: 全部 `OK`，包括单 owner、非 owner 丢帧、断开强制 RX 和释放路径。

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_audio_profile.py
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "feat(audio): PCM16 TX 协商与安全释放"
```

## Task 8: 同步 AD-038、协议文档与测试计数

**Files:**
- Modify: `SDD/08-architecture-decisions.md`
- Modify: `SDD/05-non-functional-requirements.md`
- Modify: `SDD/06-use-case-model.md`
- Modify: `SDD/09-architecture-overview.md`
- Modify: `SDD/11-component-model.md`
- Modify: `SDD/14-version-history.md`
- Modify: `SDD/README.md`
- Modify: `AGENTS.md`、`README.md`、`docs/PROJECT_MAP.md`、`tests/README.md`
- Generate: `website/sdd/**`

- [ ] **Step 1: 写入 AD-038 与协议事实**

在 SDD/08 索引 `AD-037` 后新增 `AD-038`，正文追加：

```markdown
## AD-038: ESP32-C3 PCM16 单模音频档位

| Attribute | Value |
| --- | --- |
| Type | Architectural / Audio transport |
| Status | Implemented offline；ESP32 与真机验收单独记录 |
| Decision | 主语音总线继续维持 48 kHz。新增 per-audio-socket `audioCaps` 协商和 `0x02 = 16 kHz Int16 mono PCM`；未声明 socket 的 Opus48/PCM48 行为不变。RX 对 PCM16 组每拍共享一次 soxr LQ 48→16 转换，PCM16-only 不编码 Opus。TX 全链路显式携带 `input_rate`，由后端直接转到设备率；PCM16 转换失败强制 RX。 |
| Alternatives | 全局 16 kHz 会无谓改动全部客户端；在 `/WSradio` 会话上协商会混淆同 token 的多个音频 socket；复用 `0x00` 会让帧失去自描述性；QQ 降采样实测不抑制 12 kHz 混叠，禁用。 |
| Consequences | ESP32 PCM16 单模固件可移除 libopus，双向各 256 kbps；旧服务端不应答时固件必须报错，TX 未确认不得 key-up。PTT owner、队列水位与释放路径不变。 |
```

同步：AD-004 标签表加入 `0x02`；NFR-004 增加 PCM16 256 kbps；UC-002 增加 PCM16 分支；SDD 9.2/9.3/9.4 写明 per-socket 协商和输入率；SDD/11 注册 `AudioProfile`；AGENTS 增加 `audio_profile.py` 并更新 `audio_resample.py`/server 责任；README 的 WS 格式加入 `0x02`；PROJECT_MAP 将 `audio_profile.py` 放入音频行。

- [ ] **Step 2: 更新 SDD 版本与测试事实**

在 SDD/14 首行表头后新增 `SDD V3.26 | 2026-11-17 | Kimi | 新增 AD-038：按音频 socket 协商的 ESP32-C3 PCM16 单模档位；主总线与旧客户端保持 48 kHz，记录 LQ 抗混叠和 TX 安全释放。`，SDD/README 的 `SDD Version` 改为 `V3.26`，Status 改为本次 PCM16 摘要。运行以下命令取得真实模块/用例数并更新 `tests/README.md`，不得照抄旧总数：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
import unittest
suite = unittest.defaultTestLoader.discover("tests")
def count(s):
    return sum(count(x) for x in s) if isinstance(s, unittest.TestSuite) else 1
print("modules", len(list(Path("tests").glob("test_*.py"))))
print("tests", count(suite))
PY
```

- [ ] **Step 3: 生成网站 SDD 并跑文档测试**

Run:

```bash
python3 website/build_sdd.py
.venv/bin/python -m unittest tests.test_sdd_docs_consistency tests.test_release_artifacts -v
```

Expected: 全部 `OK`；AD 索引存在 AD-038，无 AD 编号冲突。

- [ ] **Step 4: Commit**

```bash
git add SDD AGENTS.md README.md docs/PROJECT_MAP.md tests/README.md website/sdd
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
git commit -m "docs(audio): 记录 AD-038 PCM16 档位"
```

## Task 9: 全量离线验证与交付前检查

**Files:**
- Verify only；若发现回归，回到对应 Task 修复并补回归测试

- [ ] **Step 1: 编译与完整 unittest**

Run:

```bash
.venv/bin/python -m py_compile audio_profile.py audio_resample.py audio_handler.py opus_rx.py server.py backends/icom_net/audio.py backends/flex/audio.py backends/sunsdr/audio.py backends/yaesu/scu_lan/audio.py
.venv/bin/python -m unittest discover -s tests -v
```

Expected: 编译无输出，unittest `OK`；不得把新增失败归为无关问题。

- [ ] **Step 2: FT8 独立套件**

Run: `.venv/bin/python -m pytest tests/ft8 -q`
Expected: 全绿或仅项目既有硬件/平台 skip；重点核对 TX 默认 48 kHz 与 RX tap 不变。

- [ ] **Step 3: SDD 守护与发布离线规则**

Run:

```bash
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
python3 .agents/skills/dual-platform-release/harness/release_check.py
```

Expected: 守护 `clean`，release check `0 failing`。

- [ ] **Step 4: 检查交付树**

Run: `git status --short`
Expected: 执行 worktree 中只有本计划明确保留的文件；测试日志、缓存和临时 WAV 不提交。若在用户主工作区执行，不得触碰原有 website、bp、ref 等未提交内容。

## Task 10: 真机、ESP32 对接与性能验收

**Files:**
- Verify: 实际服务端、FT-710、IC-7300/MK2、目标 Wi-Fi/Cloud Hub、独立 ESP32 固件工程
- Record: 在 `docs/superpowers/specs/2026-11-17-voice-16k-design.md` 末尾追加实际验收结果

- [ ] **Step 1: 验证协商和旧客户端共存**

启动实际服务端，分别用 PCM16 测试客户端和现有浏览器连接同一账号：

```json
{"type":"audioCaps","profile":"pcm16"}
```

Expected: PCM16 socket 收到 `accepted:true` 和 641 字节的 `0x02` 帧；浏览器继续 Opus48；同 token 两 socket 不串 profile。PCM16-only 时日志/ mock 计数确认 Opus 未编码。

- [ ] **Step 2: 验证旧服务端拒绝行为**

让 PCM16 单模客户端连接不含 AD-038 的服务端。
Expected: RX/TX 均在 2.0 秒协商窗口后明确提示升级；TX 无麦克风帧、无 key-up、无静音载波。早到的 Opus 帧不得造成误判或播放。

- [ ] **Step 3: FT-710 与 Icom USB 真机语音**

通过现有启动器分别选择 FT-710 和 IC-7300/MK2，不写死或提交串口路径。发送 1 kHz 测试音和连续语音各 60 秒。
Expected: FT-710 走 16→44.1 kHz，Icom 走 16→48 kHz；监听接收机音调/时长正确，`decode_fail=0`、`write_err=0`、`queue_drops=0`，释放后无长尾。

- [ ] **Step 4: 录音、FT8、CQ 和混合听众回归**

Expected: PCM16 TX 录音时长/音调正确；FT8 仍 48 kHz TX，CQ 仍 48 kHz 输入；PCM16 与浏览器同时收听各自正常；owner 断开和所有 PTT 释放路径均强制 RX。

- [ ] **Step 5: W103D/Wi-Fi 性能与 ESP32 资源**

Expected: 双向各约 256 kbps 下无持续丢帧或事件循环 lag；记录 `/api/perf`、`session_metrics`、Wi-Fi/Hub 丢包。ESP32 独立计划完成后，以 map 文件、Flash、静态 RAM、最小空闲堆和高水位对比证明资源收益，未实测前不宣称具体 KB 数字。

- [ ] **Step 6: 记录结果**

在 spec 末尾追加 `Implementation outcome`：列出已测电台、系统、声卡率、客户端版本、网络路径、测试计数、性能数据，以及未覆盖硬件。未上真机的 Flex/SunSDR 等明确标为未验证。
