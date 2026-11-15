# FTDX10 / FTDX101 ACC 频谱源 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让 Yaesu `YaesuBackend` 能从一块挂在 ACC 口上的转接板（USB-CDC / TCP / UDP 三种连法）取频谱，作为 `ScopeProducer` 喂进现有瀑布链路，且默认关闭时行为与今天逐字节相同。

**架构：** `backends/yaesu/acc_scope/` 三件：`frames.py`（`66 CC FF` 流式解码 + UDP 分片重组，纯字节）、`sources.py`（三种源，各自重连，异步生成器）、`producer.py`（`ScopeProducer` 实现，写 `ScopeHandler` 的 10 个字段 → 现有 `_on_scope_frame` → `/WSspectrum`）。4096 字节帧的解析**复用** `backends/ft710/scope_frame.py::parse_scope_frame`，下游零改动。

**技术栈：** Python 3 标准库 + `pyserial`（已在依赖里）；`unittest`（**不是 pytest**）；无新增第三方依赖、无新增原生库。

**设计记录：** `docs/superpowers/specs/2026-11-15-ftdx10-acc-scope-design.md`（§号在下文引用）。

---

## 开工前必读（零上下文的工程师需要知道的仓库事实）

1. **测试只跑 unittest**：`python -m unittest tests.test_xxx -v`，从仓库根目录执行。`tests/` 没有 `__init__.py`，但 `from tests.acc_scope_fixtures import ...` 可用（namespace package，仓里已有先例 `from tests.test_net_wifi import FakeNmcli`）。
2. **要复用的三个既有接口**（不要改它们）：
   - `backends/ft710/scope_frame.py::parse_scope_frame(frame: bytes, require_sync: bool = True) -> ScopeFrame`，字段：`wf1, wf2, scope_mode, scope_span, preamp, attenuator, mode, s_meter, vfoa_freq, vfoa_freq_bin, scope_start_freq`。常量 `SCOPE_FRAME_SIZE=4096`、`WF_SIZE=850`、`SYNC_TAIL=b"\xff\x01\xee\x01"`、`DATA_OFFSET=2900`。
   - `scope_handler.py::ScopeHandler` 的字段（第 69–93 行）：`spectrum_rx1/2`、`scope_mode`、`scope_span`、`preamp`、`attenuator`、`mode`、`s_meter`、`vfoa_freq`、`scope_start_freq`、`last_update`、`_frame_count`、`_last_frame_time`、`_fps`、`_connected`。
   - `backends/base.py::ScopeProducer`（第 127 行）的四个方法：`start/stop/notify_tx/set_on_frame`；`keeps_running()` 是可选方法（第 152 行的 `scope_keeps_running()` 会探测它）。
3. **接线点**：`backends/yaesu/backend.py:137` 的 `create_scope_producer()`（目前只在 SCU-LAN 分支返回非 None）与第 168 行起的 `capabilities` 属性。
4. **env 读取只用 `config.py` 的私有 helper**：`_env` / `_env_int` / `_env_port`（`_env_choice` **只接受整数**，不能用于 `off|serial|tcp|udp`）。
5. **不要碰 `CHANGELOG.md` 的顶部标题**：`make_box_source.version_of()` 从 `## [镜像线 vX.Y.Z · FPn]` 解析版本，删改它会让发版流程失明。本计划的文档同步只写 SDD / AGENTS / tests/README。
6. **不要格式化 `static/ft710_main.js` / `static/ft710_ui.js`**。
7. 提交前跑一次 SDD 守卫：`python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged`。
8. **无真机**：本计划所有测试都在合成帧上跑。任何「已真机验证」的说法都不允许写进代码注释或文档（设计记录 §7 的 5 条验收清单是留给真机到货时的）。

---

## 文件结构

| 文件 | 职责 | 新建/修改 |
| --- | --- | --- |
| `backends/yaesu/acc_scope/__init__.py` | 包导出（`AccScopeProducer`、三个源） | 新建 |
| `backends/yaesu/acc_scope/frames.py` | `66 CC FF` 流式解码、UDP 分片重组、`DecodedFrame` | 新建 |
| `backends/yaesu/acc_scope/sources.py` | `SerialSource` / `TcpSource` / `UdpSource` + `autodetect_serial_port` + `same_device` | 新建 |
| `backends/yaesu/acc_scope/producer.py` | `AccScopeProducer`（生产 `ScopeHandler` 的字段） | 新建 |
| `backends/yaesu/backend.py` | `_acc_scope_config()` + `create_scope_producer()` 分支 + `capabilities.scope_type` | 修改（第 137、168 行附近） |
| `config.py` | 六个 `ACC_SCOPE_*` 常量（放在 SCU-LAN 块之后，第 409 行后） | 修改 |
| `docs/ftdx10_acc_scope.md` | 操作手册：接线、危险警告、烧录指引、三种连法、排障、真机 5 条验收 | 新建 |
| `tests/acc_scope_fixtures.py` | 合成 4096 帧 + 造流工具（无真机时的离线真值） | 新建 |
| `tests/test_acc_scope_frames.py` | 解码器与分片重组 | 新建 |
| `tests/test_acc_scope_sources.py` | 三种源的重连 / HELLO / 分片 | 新建 |
| `tests/test_acc_scope_producer.py` | 字段写对、计数、幂等、tail 只告警 | 新建 |
| `tests/test_acc_scope_contract.py` | `off` → None、串口守卫、`scope_type=="acc"` | 新建 |

按职责拆文件而不是按层：`frames.py` 是唯一需要逐字节推理的地方（所以它没有 socket、没有事件循环），`sources.py` 只关心连接与重连，`producer.py` 只关心生命周期与字段。三者可以分别理解、分别测试。

---

### 任务 1：合成帧工厂 + 流式解码器

**文件：**

- 创建：`tests/acc_scope_fixtures.py`
- 创建：`tests/test_acc_scope_frames.py`
- 创建：`backends/yaesu/acc_scope/__init__.py`
- 创建：`backends/yaesu/acc_scope/frames.py`

- [ ] **步骤 1：先造离线真值（合成帧工厂）**

`tests/acc_scope_fixtures.py`：

```python
"""合成 ACC 转接板帧 —— 这条线没有真机，所以这个工厂就是离线真值。

4096 字节的布局照 `backends/ft710/scope_frame.py::parse_scope_frame` 的输入：
0–849 wf1（线上取反）、850–1699 wf2、2900 起 150 字节元数据、4092 起 sync tail。
这份工厂之所以可信，是因为**吃它的解析器**在 FT-710 真机上验证过。
"""
from __future__ import annotations

import struct

from backends.ft710.scope_frame import (
    DATA_OFFSET, SCOPE_FRAME_SIZE, SYNC_TAIL, WF_SIZE,
)
from backends.yaesu.acc_scope.frames import MAGIC, xor_sum


def make_frame(
    *,
    wf1_prefix: bytes = b"",
    wf1_peak: int = 0,
    span: int = 5,
    scope_mode: int = 0,
    s_meter: int = 90,
    start_freq: int = 7_000_000,
    mode_byte: int = 0x02,
    tail: bytes = SYNC_TAIL,
) -> bytes:
    """一帧电台数据，原样（转接板不改内容）。"""
    buf = bytearray(SCOPE_FRAME_SIZE)
    prefix = bytes(wf1_prefix)[:WF_SIZE]
    buf[0:len(prefix)] = prefix
    buf[len(prefix)] = wf1_peak & 0xFF            # 线上取反的约定：0 最强
    buf[WF_SIZE:WF_SIZE * 2] = bytes(WF_SIZE)
    data = bytearray(150)
    data[17] = scope_mode
    data[32] = span
    data[60] = mode_byte
    data[110] = s_meter
    struct.pack_into(">I", data, 144, start_freq)
    buf[DATA_OFFSET:DATA_OFFSET + 150] = data
    buf[SCOPE_FRAME_SIZE - len(tail):] = tail
    return bytes(buf)


def framed(payload: bytes, *, xor_ok: bool = True) -> bytes:
    """MAGIC + 2 字节大端长度 + XOR + payload（流式源用的线格式）。"""
    checksum = xor_sum(payload)
    if not xor_ok:
        checksum ^= 0xFF
    return MAGIC + struct.pack(">H", len(payload)) + bytes([checksum]) + payload


def split(data: bytes, chunk: int) -> list:
    """按固定大小切读，用来模拟「一帧被读边界切开」。"""
    return [data[i:i + chunk] for i in range(0, len(data), chunk)]


def udp_fragments(payload: bytes, *, seq: int = 0, part_size: int = 1366) -> list:
    """固件实际发的三个 UDP 分片（头是 seq + part，不含 XOR）。"""
    parts = [payload[i:i + part_size] for i in range(0, len(payload), part_size)]
    return [MAGIC + bytes([seq, index]) + part for index, part in enumerate(parts)]
```

- [ ] **步骤 2：写第一个失败测试**

`tests/test_acc_scope_frames.py`：

```python
"""`66 CC FF` 组帧 —— 电台与瀑布之间唯一的一层。"""
import unittest

from backends.yaesu.acc_scope.frames import (
    FRAME_SIZE, MAGIC, MAX_PAYLOAD, FrameDecoder, xor_sum,
)
from tests.acc_scope_fixtures import framed, make_frame, split


class FrameDecoderTests(unittest.TestCase):
    def test_single_frame_in_one_read(self):
        payload = make_frame()
        frames = FrameDecoder().feed(framed(payload))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].payload, payload)
        self.assertTrue(frames[0].tail_ok)
```

- [ ] **步骤 3：运行它，确认失败**

```bash
python -m unittest tests.test_acc_scope_frames -v
```

预期：`ModuleNotFoundError: No module named 'backends.yaesu.acc_scope'`

- [ ] **步骤 4：建包与常量**

`backends/yaesu/acc_scope/__init__.py`：

```python
"""ACC 口的频谱转接板（FTDX10 / FTDX101）——设计记录 2026-11-15。

转接板把电台 ACC 上的 SPI 流（4096 字节/帧，与 FT-710 同布局）重新打包成
`66 CC FF` 帧，经 USB-CDC / TCP / UDP 送进盒子。本包只做**接收**：
CAT 与音频仍然走盒子到电台的 USB（串口 + 声卡），这条线只补频谱那一项。
"""
from backends.yaesu.acc_scope.producer import AccScopeProducer
from backends.yaesu.acc_scope.sources import SerialSource, TcpSource, UdpSource

__all__ = ["AccScopeProducer", "SerialSource", "TcpSource", "UdpSource"]
```

（这一步先只写 `frames.py`，`__init__.py` 的导入在任务 6 之后才成立 —— 本步骤只创建 `frames.py`，`__init__.py` 留到最后一步补，见步骤 9。）

`backends/yaesu/acc_scope/frames.py`（第一版：常量 + `xor_sum` + 最小 `FrameDecoder`）：

```python
"""转接板 → 主机 的两种线格式，纯字节逻辑（无 socket、无事件循环）。

流式（USB-CDC / TCP）
    MAGIC(3) | len(2, 大端) | XOR(1) | payload(len)
UDP 分片
    MAGIC(3) | seq(1) | part(1) | 分片        三个分片拼回 4096

对齐规则来自转接板作者的说明：搜 MAGIC → 长度 > MAX_PAYLOAD 就前移 1 字节重搜 →
等 len+6 到齐 → 对 payload 做 XOR → 通过则输出，失败则丢弃并继续搜。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

MAGIC = b"\x66\xcc\xff"
HEADER_SIZE = 6
#: 略高于 4096：误匹配帧头时最多损失 1 帧，而不会把整条流带偏。
MAX_PAYLOAD = 4600
#: 电台侧一包的长度（FTDX10 与 FT-710 相同）。
FRAME_SIZE = 4096
#: UDP 通道把一帧切成三片（1366 + 1366 + 1364）。
UDP_PARTS = 3
#: FT-710 帧尾。FTDX10 上未验证，所以只用于诊断（见 DecodedFrame.tail_ok）。
SYNC_TAIL = b"\xff\x01\xee\x01"


def xor_sum(payload: bytes) -> int:
    """payload 所有字节的 XOR（固件侧就是这么算的）。"""
    value = 0
    for byte in payload:
        value ^= byte
    return value


@dataclass
class DecodedFrame:
    """一帧解出来的结果。`tail_ok` 只用于诊断，不参与取舍。"""

    payload: bytes
    tail_ok: bool


class FrameDecoder:
    """流式解码器：`feed()` 收任意切片，返回本次解出的帧。

    计数器是给日志和测试看的：`bad_xor`/`bad_length` 说明流里有假帧头或不干净的数据，
    `wrong_size` 说明转接板在重新切帧（我们只认 4096），`tail_mismatch` 说明这台电台的
    帧与 FT-710 不完全一样 —— 那是元数据偏移不可信的**第一个**信号。
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.frames = 0
        self.bad_xor = 0
        self.bad_length = 0
        self.wrong_size = 0
        self.tail_mismatch = 0

    def feed(self, data: bytes) -> List[DecodedFrame]:
        out: List[DecodedFrame] = []
        if data:
            self._buf.extend(data)
        while True:
            index = self._buf.find(MAGIC)
            if index < 0:
                # MAGIC 可能被读边界切开：留最后 2 字节，其余丢掉。
                if len(self._buf) > len(MAGIC) - 1:
                    del self._buf[:len(self._buf) - (len(MAGIC) - 1)]
                return out
            if index:
                del self._buf[:index]
            if len(self._buf) < HEADER_SIZE:
                return out
            length = (self._buf[3] << 8) | self._buf[4]
            if length > MAX_PAYLOAD:
                self.bad_length += 1
                del self._buf[0]
                continue
            total = HEADER_SIZE + length
            if len(self._buf) < total:
                return out
            payload = bytes(self._buf[HEADER_SIZE:total])
            if xor_sum(payload) != self._buf[5]:
                self.bad_xor += 1
                del self._buf[0]
                continue
            del self._buf[:total]
            if len(payload) != FRAME_SIZE:
                self.wrong_size += 1
                continue
            tail_ok = payload.endswith(SYNC_TAIL)
            if not tail_ok:
                self.tail_mismatch += 1
            self.frames += 1
            out.append(DecodedFrame(payload=payload, tail_ok=tail_ok))
```

- [ ] **步骤 5：运行测试，确认通过**

```bash
python -m unittest tests.test_acc_scope_frames -v
```

预期：`1 test ... OK`

- [ ] **步骤 6：补齐边界测试（含 UDP 重组，任务 2 的代码也一并写）**

把下面这些方法加进 `FrameDecoderTests`：

```python
    def test_frame_split_across_reads(self):
        payload = make_frame()
        wire = framed(payload)
        decoder = FrameDecoder()
        out = []
        for chunk in split(wire, 7):
            out.extend(decoder.feed(chunk))
        self.assertEqual([f.payload for f in out], [payload])

    def test_magic_split_across_reads(self):
        wire = framed(make_frame())
        decoder = FrameDecoder()
        out = []
        for chunk in split(wire, 2):        # 每次读都可能把 MAGIC 切成两半
            out.extend(decoder.feed(chunk))
        self.assertEqual(len(out), 1)

    def test_junk_before_magic_is_skipped(self):
        payload = make_frame()
        decoder = FrameDecoder()
        out = decoder.feed(b"\x00\x11\x22" + framed(payload))
        self.assertEqual([f.payload for f in out], [payload])

    def test_bad_xor_is_dropped_and_the_next_frame_survives(self):
        payload = make_frame()
        junk = MAGIC + b"\x00\x10" + b"\xff" + bytes(16)
        decoder = FrameDecoder()
        out = decoder.feed(junk + framed(payload))
        self.assertEqual([f.payload for f in out], [payload])
        self.assertEqual(decoder.bad_xor, 1)

    def test_oversize_length_resyncs(self):
        payload = make_frame()
        decoder = FrameDecoder()
        out = decoder.feed(MAGIC + b"\x99\x99" + b"\x00" + framed(payload))
        self.assertEqual(len(out), 1)
        self.assertEqual(decoder.bad_length, 1)

    def test_payload_that_is_not_4096_is_dropped(self):
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(framed(b"abc")), [])
        self.assertEqual(decoder.wrong_size, 1)

    def test_tail_mismatch_is_flagged_but_kept(self):
        payload = make_frame(tail=b"\x00\x00\x00\x00")
        frames = FrameDecoder().feed(framed(payload))
        self.assertEqual(len(frames), 1)
        self.assertFalse(frames[0].tail_ok)

    def test_fake_magic_inside_the_payload_does_not_break_decoding(self):
        payload = make_frame(wf1_prefix=MAGIC)
        decoder = FrameDecoder()
        out = decoder.feed(framed(payload))
        self.assertEqual([f.payload for f in out], [payload])

    def test_xor_sum_against_a_hand_computed_value(self):
        self.assertEqual(xor_sum(b"\x01\x02\x03\x04"), 0x04)
        self.assertEqual(xor_sum(b""), 0x00)

    def test_payload_size_constant_matches_the_radio(self):
        self.assertEqual(len(make_frame()), FRAME_SIZE)
        self.assertLess(FRAME_SIZE, MAX_PAYLOAD)
```

再加 `UdpReassemblerTests`：

```python
from backends.yaesu.acc_scope.frames import UdpReassembler, UDP_PARTS
from tests.acc_scope_fixtures import udp_fragments


class UdpReassemblerTests(unittest.TestCase):
    def test_three_fragments_make_one_frame(self):
        payload = make_frame()
        reasm = UdpReassembler()
        out = []
        for part in udp_fragments(payload):
            out.extend(reasm.feed(part))
        self.assertEqual([f.payload for f in out], [payload])

    def test_out_of_order_fragments_are_tolerated(self):
        payload = make_frame()
        parts = udp_fragments(payload)
        reasm = UdpReassembler()
        out = []
        for part in (parts[2], parts[0], parts[1]):
            out.extend(reasm.feed(part))
        self.assertEqual(len(out), 1)

    def test_a_missing_fragment_drops_the_whole_frame(self):
        payload = make_frame()
        parts = udp_fragments(payload)
        reasm = UdpReassembler()
        out = []
        out.extend(reasm.feed(parts[0]))
        out.extend(reasm.feed(parts[1]))
        self.assertEqual(out, [])
        # 下一帧（新 seq）必须完好
        for part in udp_fragments(make_frame(), seq=1):
            out.extend(reasm.feed(part))
        self.assertEqual(len(out), 1)
        self.assertEqual(reasm.dropped, 1)

    def test_duplicate_fragment_is_harmless(self):
        payload = make_frame()
        parts = udp_fragments(payload)
        reasm = UdpReassembler()
        out = []
        for part in (parts[0], parts[0], parts[1], parts[2]):
            out.extend(reasm.feed(part))
        self.assertEqual(len(out), 1)

    def test_foreign_datagram_is_counted(self):
        reasm = UdpReassembler()
        self.assertEqual(reasm.feed(b"\x00" * 10), [])
        self.assertEqual(reasm.bad_datagram, 1)

    def test_part_index_out_of_range_is_rejected(self):
        reasm = UdpReassembler()
        self.assertEqual(reasm.feed(MAGIC + bytes([0, UDP_PARTS]) + b"x"), [])
        self.assertEqual(reasm.bad_datagram, 1)

    def test_wrong_total_size_is_dropped(self):
        reasm = UdpReassembler()
        out = []
        for part in udp_fragments(b"\x00" * 3000):
            out.extend(reasm.feed(part))
        self.assertEqual(out, [])
```

- [ ] **步骤 7：在 `frames.py` 末尾加 `UdpReassembler`**

```python
class UdpReassembler:
    """UDP 通道的分片归组：`seq` 一变就丢弃没拼满的帧，缺片不猜。

    UDP 头是 `MAGIC | seq | part`，**没有 XOR**（固件的 UDP 通道就是这么发的），
    所以这里靠「三片拼出来必须恰好 4096」来判真伪。
    """

    def __init__(self, parts: int = UDP_PARTS, frame_size: int = FRAME_SIZE) -> None:
        self._parts = parts
        self._frame_size = frame_size
        self._seq: Optional[int] = None
        self._slots: List[Optional[bytes]] = [None] * parts
        self.frames = 0
        self.dropped = 0
        self.bad_datagram = 0

    def feed(self, datagram: bytes) -> List[DecodedFrame]:
        out: List[DecodedFrame] = []
        if len(datagram) < 5 or not datagram.startswith(MAGIC):
            self.bad_datagram += 1
            return out
        seq, part = datagram[3], datagram[4]
        if part >= self._parts:
            self.bad_datagram += 1
            return out
        if seq != self._seq:
            if self._seq is not None and any(s is not None for s in self._slots):
                self.dropped += 1
            self._seq = seq
            self._slots = [None] * self._parts
        self._slots[part] = datagram[5:]
        if any(slot is None for slot in self._slots):
            return out
        payload = b"".join(slot for slot in self._slots if slot is not None)
        self._slots = [None] * self._parts
        if len(payload) != self._frame_size:
            self.dropped += 1
            return out
        tail_ok = payload.endswith(SYNC_TAIL)
        self.frames += 1
        out.append(DecodedFrame(payload=payload, tail_ok=tail_ok))
        return out
```

- [ ] **步骤 8：跑全部**

```bash
python -m unittest tests.test_acc_scope_frames -v
```

预期：`19 tests ... OK`（`FrameDecoderTests` 11 项 + `UdpReassemblerTests` 8 项）

- [ ] **步骤 9：补包导出（此时 producer/sources 还不存在，所以先只导出 frames）**

`backends/yaesu/acc_scope/__init__.py`：

```python
"""ACC 口的频谱转接板（FTDX10 / FTDX101）——设计记录 2026-11-15。

转接板把电台 ACC 上的 SPI 流（4096 字节/帧，与 FT-710 同布局）重新打包成
`66 CC FF` 帧，经 USB-CDC / TCP / UDP 送进盒子。本包只做**接收**：
CAT 与音频仍然走盒子到电台的 USB，这条线只补频谱那一项。
"""
```

（任务 6 结束时再把 `AccScopeProducer` 与三个源加进 `__all__`。）

- [ ] **步骤 10：Commit**

```bash
git add backends/yaesu/acc_scope/ tests/acc_scope_fixtures.py tests/test_acc_scope_frames.py
git commit -m "feat(acc_scope): 66 CC FF 流式解码与 UDP 分片重组 + 合成帧工厂"
```

---

### 任务 2：TCP 源（含重连）

**文件：**

- 创建：`backends/yaesu/acc_scope/sources.py`
- 修改：`tests/test_acc_scope_sources.py`（新建）

- [ ] **步骤 1：写失败测试**

`tests/test_acc_scope_sources.py`：

```python
"""三种转接板输入源：重连、HELLO 注册、分片归组。全部异步、无真实硬件。"""
import asyncio
import unittest

from backends.yaesu.acc_scope.frames import DecodedFrame
from backends.yaesu.acc_scope.sources import TcpSource
from tests.acc_scope_fixtures import framed, make_frame


class FakeReader:
    """按顺序吐 chunk；吐完按 `end` 的取值收尾。"""

    def __init__(self, chunks, end="reset"):
        self._chunks = list(chunks)
        self._end = end

    async def read(self, _n):
        if self._chunks:
            return self._chunks.pop(0)
        if self._end == "reset":
            raise ConnectionResetError("adapter went away")
        return b""


class FakeWriter:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


async def _next(agen, timeout=1.0):
    return await asyncio.wait_for(agen.__anext__(), timeout=timeout)


class TcpSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_frames_are_decoded_from_a_tcp_stream(self):
        payload = make_frame()
        calls = []

        async def connect(host, port):
            calls.append((host, port))
            return FakeReader([framed(payload)]), FakeWriter()

        source = TcpSource("192.0.2.10", 51234, connect=connect, retry_s=0.01)
        agen = source.frames()
        try:
            frame = await _next(agen)
            self.assertIsInstance(frame, DecodedFrame)
            self.assertEqual(frame.payload, payload)
        finally:
            await source.aclose()
            await agen.aclose()
        self.assertEqual(calls, [("192.0.2.10", 51234)])

    async def test_a_dropped_connection_is_retried(self):
        payload = make_frame()
        calls = []

        async def connect(host, port):
            calls.append(1)
            if len(calls) == 1:
                return FakeReader([]), FakeWriter()        # 立刻 reset
            return FakeReader([framed(payload)], end="eof"), FakeWriter()

        source = TcpSource("192.0.2.10", 51234, connect=connect, retry_s=0.01)
        agen = source.frames()
        try:
            frame = await _next(agen, timeout=2.0)
            self.assertEqual(frame.payload, payload)
        finally:
            await source.aclose()
            await agen.aclose()
        self.assertGreaterEqual(len(calls), 2)
        self.assertGreaterEqual(source.reconnects, 1)

    async def test_connect_failure_is_retried_instead_of_raising(self):
        payload = make_frame()
        calls = []

        async def connect(host, port):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("connection refused")
            return FakeReader([framed(payload)], end="eof"), FakeWriter()

        source = TcpSource("192.0.2.10", 51234, connect=connect, retry_s=0.01)
        agen = source.frames()
        try:
            frame = await _next(agen, timeout=2.0)
            self.assertEqual(frame.payload, payload)
        finally:
            await source.aclose()
            await agen.aclose()
        self.assertEqual(len(calls), 3)
```

- [ ] **步骤 2：运行，确认失败**

```bash
python -m unittest tests.test_acc_scope_sources -v
```

预期：`ModuleNotFoundError: No module named 'backends.yaesu.acc_scope.sources'`

- [ ] **步骤 3：写 `sources.py`（第一版：公共部件 + `TcpSource`）**

```python
"""转接板的三条输入源，每条都是一个**无限**异步生成器，自己负责重连。

- `SerialSource`：USB-CDC（pyserial），盒子侧的 `/dev/ttyACM*` 或 Windows 的 `COMx`
- `TcpSource`：转接板的 `:51234`，无握手，断线重连
- `UdpSource`：转接板的 `:51235`，先发 `HELLO` 注册，再收三个分片/帧

生产者只看见「帧来了」，重连、退避、注册都在这里。重试间隔与 HELLO 间隔都是
**构造参数**（测试传毫秒级，绝不 sleep 秒级）。
"""
from __future__ import annotations

import asyncio
import glob
import logging
import os
from typing import AsyncIterator, Callable, List, Optional

from backends.yaesu.acc_scope.frames import (
    MAGIC, DecodedFrame, FrameDecoder, UdpReassembler,
)

logger = logging.getLogger("mrrc.backend.yaesu.acc_scope")

#: 断开后多久再试。盒子贴电台，所以 1 s 足够快，也不至于把日志刷满。
RETRY_S = 1.0
#: UDP 注册的重发间隔：转接板重启后会忘记对端，重发比断线重连便宜。
HELLO_INTERVAL_S = 5.0
#: 串口读超时：决定重连与停止的响应速度，不是数据速率。
SERIAL_TIMEOUT_S = 0.2


def _offer(queue: asyncio.Queue, item) -> None:
    """不阻塞地放入；满了丢**最旧**的（频谱要最新的那一帧）。"""
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:            # pragma: no cover - 竞态兜底
            pass
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:                 # pragma: no cover - 竞态兜底
        pass


def autodetect_serial_port() -> Optional[str]:
    """找一个 USB-CDC 设备（Linux/macOS）。Windows 必须显式配置，不做猜测。"""
    if os.name == "nt":
        return None
    for pattern in ("/dev/ttyACM*", "/dev/cu.usbmodem*"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


def same_device(left: str, right: str) -> bool:
    """两个设备路径是不是同一个（守 CAT 口用的，宁可保守）。"""
    if not left or not right:
        return False
    if os.path.normcase(left) == os.path.normcase(right):
        return True
    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
            os.path.realpath(right))
    except OSError:                            # pragma: no cover - 路径都不可解析
        return False


class TcpSource:
    """转接板的 TCP 通道：连上就收流，断了退避重连。"""

    def __init__(self, host: str, port: int, *,
                 connect: Optional[Callable] = None,
                 retry_s: float = RETRY_S) -> None:
        self._host = host
        self._port = port
        self._connect = connect or asyncio.open_connection
        self._retry_s = retry_s
        self._closed = False
        self.decoder = FrameDecoder()
        self.reconnects = 0

    async def aclose(self) -> None:
        self._closed = True

    async def frames(self) -> AsyncIterator[DecodedFrame]:
        while not self._closed:
            try:
                reader, writer = await self._connect(self._host, self._port)
            except OSError as exc:
                logger.warning("acc_scope: TCP %s:%s unreachable (%s) — retrying "
                               "in %.1f s", self._host, self._port, exc,
                               self._retry_s)
                await asyncio.sleep(self._retry_s)
                continue
            logger.info("acc_scope: TCP connected %s:%s", self._host, self._port)
            try:
                while not self._closed:
                    chunk = await reader.read(65536)
                    if not chunk:
                        logger.warning("acc_scope: TCP stream from %s:%s closed",
                                       self._host, self._port)
                        break
                    for frame in self.decoder.feed(chunk):
                        yield frame
            except (OSError, asyncio.IncompleteReadError) as exc:
                logger.warning("acc_scope: TCP read failed (%s) — reconnecting",
                               exc)
            finally:
                self.reconnects += 1
                try:
                    writer.close()
                except OSError:                # pragma: no cover - 已关闭
                    pass
            if not self._closed:
                await asyncio.sleep(self._retry_s)
```

- [ ] **步骤 4：跑测试确认通过**

```bash
python -m unittest tests.test_acc_scope_sources -v
```

预期：`3 tests ... OK`

- [ ] **步骤 5：Commit**

```bash
git add backends/yaesu/acc_scope/sources.py tests/test_acc_scope_sources.py
git commit -m "feat(acc_scope): TCP 源与退避重连"
```

---

### 任务 3：串口源（USB-CDC）与 CAT 口守卫

**文件：**

- 修改：`backends/yaesu/acc_scope/sources.py`
- 修改：`tests/test_acc_scope_sources.py`

- [ ] **步骤 1：写失败测试（追加到 `tests/test_acc_scope_sources.py`）**

```python
from backends.yaesu.acc_scope.sources import (
    SerialSource, autodetect_serial_port, same_device,
)


class FakeSerial:
    """最小 pyserial 替身：read() 按顺序吐 chunk，空了就抛（模拟拔线）。"""

    def __init__(self, chunks, fail_after=True):
        self._chunks = list(chunks)
        self._fail_after = fail_after
        self.closed = False

    def read(self, _n):
        if self._chunks:
            return self._chunks.pop(0)
        if self._fail_after:
            raise OSError("device unplugged")
        return b""

    def close(self):
        self.closed = True


class SerialSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_frames_are_decoded_from_a_serial_stream(self):
        payload = make_frame()
        opened = []

        def factory(port, baud):
            opened.append((port, baud))
            return FakeSerial([framed(payload)], fail_after=False)

        source = SerialSource("/dev/ttyACM9", baud=115200,
                              serial_factory=factory, retry_s=0.01)
        agen = source.frames()
        try:
            frame = await _next(agen)
            self.assertEqual(frame.payload, payload)
        finally:
            await source.aclose()
            await agen.aclose()
        self.assertEqual(opened, [("/dev/ttyACM9", 115200)])

    async def test_a_disappearing_device_is_reopened(self):
        payload = make_frame()
        opened = []

        def factory(port, baud):
            opened.append(port)
            if len(opened) == 1:
                return FakeSerial([], fail_after=True)      # 立刻抛
            return FakeSerial([framed(payload)], fail_after=False)

        source = SerialSource("/dev/ttyACM9", serial_factory=factory, retry_s=0.01)
        agen = source.frames()
        try:
            frame = await _next(agen, timeout=2.0)
            self.assertEqual(frame.payload, payload)
        finally:
            await source.aclose()
            await agen.aclose()
        self.assertGreaterEqual(len(opened), 2)
        self.assertGreaterEqual(source.reconnects, 1)

    async def test_open_failure_is_retried_instead_of_raising(self):
        payload = make_frame()
        opened = []

        def factory(port, baud):
            opened.append(port)
            if len(opened) < 3:
                raise OSError("permission denied")
            return FakeSerial([framed(payload)], fail_after=False)

        source = SerialSource("/dev/ttyACM9", serial_factory=factory, retry_s=0.01)
        agen = source.frames()
        try:
            await _next(agen, timeout=2.0)
        finally:
            await source.aclose()
            await agen.aclose()
        self.assertEqual(len(opened), 3)


class DeviceGuardTests(unittest.TestCase):
    def test_same_device_matches_identical_paths(self):
        self.assertTrue(same_device("/dev/ttyACM0", "/dev/ttyACM0"))
        self.assertFalse(same_device("/dev/ttyACM0", "/dev/ttyACM1"))
        self.assertFalse(same_device("", "/dev/ttyACM0"))

    def test_autodetect_returns_none_on_windows(self):
        import unittest.mock as mock
        with mock.patch("backends.yaesu.acc_scope.sources.os.name", "nt"):
            self.assertIsNone(autodetect_serial_port())
```

- [ ] **步骤 2：运行，确认失败**

```bash
python -m unittest tests.test_acc_scope_sources -v
```

预期：`ImportError: cannot import name 'SerialSource' from 'backends.yaesu.acc_scope.sources'`

- [ ] **步骤 3：在 `sources.py` 里加 `SerialSource`**

```python
def _default_serial_factory(port: str, baud: int):
    """pyserial，已经在依赖里（CAT 用它）。导入放在函数内，方便替身注入。"""
    import serial
    return serial.Serial(port, baud, timeout=SERIAL_TIMEOUT_S)


class SerialSource:
    """USB-CDC 转接板：读流，设备消失就重开。

    打开失败（设备还没枚举出来）与读失败（拔线、供电抖动）走同一条退避路径 ——
    盒子可能比转接板先起来，这是常态，不是异常。
    """

    def __init__(self, port: str, *, baud: int = 115200,
                 serial_factory: Optional[Callable] = None,
                 retry_s: float = RETRY_S) -> None:
        self._port = port
        self._baud = baud
        self._factory = serial_factory or _default_serial_factory
        self._retry_s = retry_s
        self._closed = False
        self.decoder = FrameDecoder()
        self.reconnects = 0

    async def aclose(self) -> None:
        self._closed = True

    async def frames(self) -> AsyncIterator[DecodedFrame]:
        while not self._closed:
            try:
                handle = await asyncio.to_thread(
                    self._factory, self._port, self._baud)
            except (OSError, RuntimeError) as exc:
                logger.warning("acc_scope: cannot open %s (%s) — retrying in "
                               "%.1f s", self._port, exc, self._retry_s)
                await asyncio.sleep(self._retry_s)
                continue
            logger.info("acc_scope: serial adapter open on %s @%d",
                        self._port, self._baud)
            try:
                while not self._closed:
                    chunk = await asyncio.to_thread(handle.read, 4096)
                    if chunk:
                        for frame in self.decoder.feed(chunk):
                            yield frame
            except OSError as exc:
                logger.warning("acc_scope: %s read failed (%s) — reopening",
                               self._port, exc)
            finally:
                self.reconnects += 1
                try:
                    handle.close()
                except OSError:                # pragma: no cover - 已关闭
                    pass
            if not self._closed:
                await asyncio.sleep(self._retry_s)
```

- [ ] **步骤 4：跑测试确认通过**

```bash
python -m unittest tests.test_acc_scope_sources -v
```

预期：`8 tests ... OK`

- [ ] **步骤 5：Commit**

```bash
git add backends/yaesu/acc_scope/sources.py tests/test_acc_scope_sources.py
git commit -m "feat(acc_scope): USB-CDC 串口源 + 设备路径守卫与自动探测"
```

---

### 任务 4：UDP 源（HELLO 注册 + 分片）

**文件：**

- 修改：`backends/yaesu/acc_scope/sources.py`
- 修改：`tests/test_acc_scope_sources.py`

- [ ] **步骤 1：写失败测试（追加）**

```python
from backends.yaesu.acc_scope.sources import UdpSource
from tests.acc_scope_fixtures import udp_fragments


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendto(self, data, addr=None):
        self.sent.append(data)

    def close(self):
        self.closed = True


class UdpSourceTests(unittest.IsolatedAsyncioTestCase):
    async def _make(self, interval=0.01):
        source = UdpSource("192.0.2.10", 51235, hello_interval_s=interval)

        async def factory(queue):
            return FakeTransport()

        source._endpoint_factory = factory
        return source

    async def test_three_fragments_become_one_frame(self):
        payload = make_frame()
        source = await self._make()
        agen = source.frames()
        try:
            task = asyncio.ensure_future(_next(agen))
            await asyncio.sleep(0)
            for part in udp_fragments(payload):
                source.queue.put_nowait(part)
            frame = await asyncio.wait_for(task, timeout=1.0)
            self.assertEqual(frame.payload, payload)
        finally:
            await source.aclose()
            await agen.aclose()

    async def test_hello_is_re_sent_while_idle(self):
        source = await self._make(interval=0.01)
        agen = source.frames()
        task = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0.06)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        await source.aclose()
        await agen.aclose()
        self.assertGreaterEqual(len(source.transport.sent), 2)
        self.assertTrue(all(b == b"HELLO" for b in source.transport.sent))
```

- [ ] **步骤 2：运行，确认失败**

```bash
python -m unittest tests.test_acc_scope_sources -v
```

预期：`ImportError: cannot import name 'UdpSource'`

- [ ] **步骤 3：在 `sources.py` 里加 UDP 部分**

```python
class _HelloProtocol(asyncio.DatagramProtocol):
    """收数据报 → 丢进有界队列（满则丢最旧）。"""

    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue

    def datagram_received(self, data: bytes, addr) -> None:   # noqa: ARG002
        _offer(self._queue, data)


class UdpSource:
    """转接板的 UDP 通道：`HELLO` 注册 → 分片归组 → 整帧。

    转接板重启后会忘记对端（它只记住最后一个 `HELLO` 的源地址），所以空闲时
    定期重发比「断线重连」更管用：队列里没数据就说明该重新报名了。
    """

    def __init__(self, host: str, port: int, *,
                 hello_interval_s: float = HELLO_INTERVAL_S,
                 queue_size: int = 64) -> None:
        self._host = host
        self._port = port
        self._hello_s = hello_interval_s
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._endpoint_factory = self._default_endpoint
        self._closed = False
        self.reassembler = UdpReassembler()
        self.transport = None
        self.reconnects = 0
        self.hellos = 0

    #: 测试注入点：给出 `Callable[[asyncio.Queue], Awaitable[transport]]`。
    @property
    def queue(self) -> asyncio.Queue:
        return self._queue

    async def _default_endpoint(self, queue: asyncio.Queue):
        loop = asyncio.get_running_loop()
        protocol = _HelloProtocol(queue)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol, remote_addr=(self._host, self._port),
            local_addr=("0.0.0.0", 0))
        return transport

    async def aclose(self) -> None:
        self._closed = True
        if self.transport is not None:
            try:
                self.transport.close()
            except OSError:                    # pragma: no cover - 已关闭
                pass

    async def frames(self) -> AsyncIterator[DecodedFrame]:
        try:
            self.transport = await self._endpoint_factory(self._queue)
        except OSError as exc:
            logger.warning("acc_scope: UDP endpoint on %s:%s failed (%s)",
                           self._host, self._port, exc)
            return
        logger.info("acc_scope: UDP channel ready (HELLO → %s:%s)",
                    self._host, self._port)
        try:
            while not self._closed:
                try:
                    datagram = await asyncio.wait_for(
                        self._queue.get(), timeout=self._hello_s)
                except asyncio.TimeoutError:
                    self.transport.sendto(b"HELLO")
                    self.hellos += 1
                    continue
                for frame in self.reassembler.feed(datagram):
                    yield frame
        finally:
            self.reconnects += 1
            if self.transport is not None:
                self.transport.close()
```

注意：`self._endpoint_factory` 是**赋值**属性（测试直接覆盖），所以类里不要把它定义成方法名冲突的 property。

- [ ] **步骤 4：跑测试确认通过**

```bash
python -m unittest tests.test_acc_scope_sources -v
```

预期：`10 tests ... OK`

- [ ] **步骤 5：Commit**

```bash
git add backends/yaesu/acc_scope/sources.py tests/test_acc_scope_sources.py
git commit -m "feat(acc_scope): UDP 源（HELLO 注册 + 三片归组）"
```

---

### 任务 5：`AccScopeProducer`

**文件：**

- 创建：`backends/yaesu/acc_scope/producer.py`
- 创建：`tests/test_acc_scope_producer.py`
- 修改：`backends/yaesu/acc_scope/__init__.py`（补导出）

- [ ] **步骤 1：写失败测试**

`tests/test_acc_scope_producer.py`：

```python
"""生产者：把解出来的帧写进 ScopeHandler 的字段，别的什么都不做。"""
import asyncio
import time
import unittest

from backends.yaesu.acc_scope.frames import DecodedFrame
from backends.yaesu.acc_scope.producer import AccScopeProducer
from tests.acc_scope_fixtures import make_frame


class FakeScope:
    """ScopeHandler 的最小替身（字段名与 scope_handler.py:69-93 一致）。"""

    def __init__(self):
        self.spectrum_rx1 = [0] * 850
        self.spectrum_rx2 = [0] * 850
        self.s_meter = -1
        self.vfoa_freq = 0
        self.vfoa_freq_bin = 0
        self.mode = 0
        self.scope_mode = 0
        self.scope_span = 0
        self.preamp = 0
        self.attenuator = 0
        self.scope_start_freq = 0
        self.last_update = 0.0
        self._frame_count = 0
        self._last_frame_time = 0.0
        self._fps = 0.0
        self._connected = False


class FakeSource:
    """按顺序吐预设帧，吐完就等（模拟「流还在，只是暂时没数据」）。"""

    def __init__(self, frames):
        self._frames = list(frames)
        self.closed = False

    async def aclose(self):
        self.closed = True

    async def frames(self):
        for frame in self._frames:
            yield frame
        while not self.closed:
            await asyncio.sleep(0.01)


class AccScopeProducerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fields_are_written_and_on_frame_fires(self):
        payload = make_frame(span=5, s_meter=99, start_freq=7_050_000)
        scope = FakeScope()
        seen = []

        async def on_frame(handler):
            seen.append(handler._frame_count)

        producer = AccScopeProducer(
            FakeSource([DecodedFrame(payload, tail_ok=True)]), scope, on_frame)
        await producer.start()
        await asyncio.sleep(0.05)
        await producer.stop()

        self.assertEqual(scope.spectrum_rx1, [~b & 0xFF for b in payload[:850]])
        self.assertEqual(scope.scope_span, 5)
        self.assertEqual(scope.s_meter, 99)
        self.assertEqual(scope.scope_start_freq, 7_050_000)
        self.assertEqual(scope._frame_count, 1)
        self.assertTrue(scope._connected is False)   # stop() 之后必须掉线
        self.assertEqual(seen, [1])

    async def test_an_out_of_range_span_becomes_the_sentinel(self):
        payload = make_frame(span=200)
        scope = FakeScope()
        producer = AccScopeProducer(FakeSource([DecodedFrame(payload, True)]), scope)
        await producer.start()
        await asyncio.sleep(0.05)
        await producer.stop()
        self.assertEqual(scope.scope_span, -1)

    async def test_an_impossible_start_frequency_becomes_the_sentinel(self):
        payload = make_frame(start_freq=0xFFFFFFFF)
        scope = FakeScope()
        producer = AccScopeProducer(FakeSource([DecodedFrame(payload, True)]), scope)
        await producer.start()
        await asyncio.sleep(0.05)
        await producer.stop()
        self.assertEqual(scope.scope_start_freq, -1)

    async def test_a_missing_tail_is_counted_but_the_frame_is_used(self):
        payload = make_frame()
        scope = FakeScope()
        producer = AccScopeProducer(
            FakeSource([DecodedFrame(payload, tail_ok=False)]), scope)
        await producer.start()
        await asyncio.sleep(0.05)
        await producer.stop()
        self.assertEqual(scope._frame_count, 1)
        self.assertEqual(producer.snapshot()["tail_bad"], 1)

    async def test_start_and_stop_are_idempotent(self):
        scope = FakeScope()
        producer = AccScopeProducer(FakeSource([]), scope)
        await producer.start()
        await producer.start()
        await producer.stop()
        await producer.stop()
        self.assertIsNone(producer._task)

    async def test_fps_is_smoothed_not_a_division_by_zero(self):
        frames = [DecodedFrame(make_frame(), True) for _ in range(3)]
        scope = FakeScope()
        producer = AccScopeProducer(FakeSource(frames), scope)
        await producer.start()
        await asyncio.sleep(0.05)
        await producer.stop()
        # 三帧挨着来，所以 fps 会被 EWMA 推得很高 —— 这里只要求「不是 0、不是 inf/NaN」，
        # 上界是假的（帧间隔由测试决定，不是由电台决定）。
        self.assertGreater(scope._fps, 0)
        self.assertEqual(scope._fps, scope._fps)      # NaN 会不等于自己

    def test_keeps_running_is_false(self):
        self.assertFalse(AccScopeProducer(FakeSource([])).keeps_running())
```

- [ ] **步骤 2：运行，确认失败**

```bash
python -m unittest tests.test_acc_scope_producer -v
```

预期：`ModuleNotFoundError: No module named 'backends.yaesu.acc_scope.producer'`

- [ ] **步骤 3：写 `producer.py`**

```python
"""`ScopeProducer` 的 ACC 转接板实现。

职责只有三件：把源里的帧解出来、写进共享的 `ScopeHandler`、在停止时把
`_connected` 拉回 False。帧的解析**复用** FT-710 那一份（`parse_scope_frame`），
所以 FT-710 的 11 项解析测试同时也是这一层的回归网。

写入的字段与 `backends/ft710/scope_producer.py::_read_pipe` 和
`backends/yaesu/scu_lan/scope.py` 的对应段落逐字段同形 —— 服务端
`_on_scope_frame`、30 Hz 广播、`spectrum_profile` 都不需要知道多了一种源。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from backends.ft710.scope_frame import parse_scope_frame
from backends.yaesu.acc_scope.frames import SYNC_TAIL, DecodedFrame

logger = logging.getLogger("mrrc.backend.yaesu.acc_scope")

OnFrameCallback = Callable[[Any], Awaitable[None]]

#: FT-710 的 span 表只有 0…9（`config_ft710.SCOPE_SPANS`）。超出范围说明元数据
#: 偏移不对 —— 写 -1 让 `_on_scope_frame` 的 `>= 0` 门跳过，CAT 继续当真值。
SPAN_MAX = 9
#: 一个「看起来像频率」的窗口：1 kHz … 54 MHz。
START_FREQ_MIN = 1_000
START_FREQ_MAX = 54_000_000


class AccScopeProducer:
    """ScopeProducer for an ACC adapter board (66 CC FF frames)."""

    def __init__(self, source, scope=None, on_frame: Optional[OnFrameCallback] = None,
                 *, label: str = "acc") -> None:
        self._source = source
        self._scope = scope
        self._on_frame = on_frame
        self._label = label
        self._task: Optional[asyncio.Task] = None
        self._closed = False
        self._frames = 0
        self._parse_errors = 0
        self._tail_bad = 0
        self._tail_warned = False
        self._tx = False

    # ── ScopeProducer protocol ─────────────────────────────────────

    def set_on_frame(self, cb: OnFrameCallback) -> None:
        self._on_frame = cb

    def notify_tx(self, tx: bool) -> None:
        """Is the radio transmitting?  Recorded, not acted on (v1).

        The FT-710 pauses SPI reads during TX because its scope stream garbles;
        an adapter-based stream is the radio's own SPI as well, so the *firmware*
        is the right place to decide. Here we only remember the state so a log
        line can explain a gap.
        """
        self._tx = bool(tx)

    def keeps_running(self) -> bool:
        """No: the adapter's frames are read by nobody else.

        The box sits next to the radio, so the stream only exists for the
        waterfall — `server.ws_spectrum` stops it with the last socket.
        """
        return False

    # ── lifecycle ──────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._closed = False
        self._task = asyncio.create_task(self._run(), name="acc-scope")

    async def stop(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await self._source.aclose()

    async def _run(self) -> None:
        try:
            async for frame in self._source.frames():
                await self._handle(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # noqa: BLE001
            logger.warning("acc_scope: reader stopped (%s: %s)",
                           type(exc).__name__, exc)
        finally:
            scope = self._scope
            if scope is not None and getattr(scope, "_connected", False):
                scope._connected = False
                logger.info("acc_scope: stream stopped — spectrum falls back")

    # ── one frame ──────────────────────────────────────────────────

    async def _handle(self, frame: DecodedFrame) -> None:
        if not frame.tail_ok:
            self._tail_bad += 1
            if not self._tail_warned:
                self._tail_warned = True
                logger.warning(
                    "acc_scope: %d-byte frame does not end with the FT-710 sync "
                    "tail (%s). The FTDX10 frame may differ, so span/mode/S-meter "
                    "from it are suspect — CAT stays the source of truth. Frames "
                    "are still accepted.", len(frame.payload), SYNC_TAIL.hex())
        try:
            parsed = parse_scope_frame(frame.payload, require_sync=False)
        except (ValueError, IndexError) as exc:
            self._parse_errors += 1
            logger.debug("acc_scope: unparsable frame (%s)", exc)
            return

        scope = self._scope
        if scope is None:
            return

        scope.spectrum_rx1 = parsed.wf1
        scope.spectrum_rx2 = parsed.wf2
        scope.scope_mode = parsed.scope_mode
        span = parsed.scope_span
        scope.scope_span = span if 0 <= span <= SPAN_MAX else -1
        scope.preamp = parsed.preamp
        scope.attenuator = parsed.attenuator
        scope.mode = parsed.mode
        scope.s_meter = parsed.s_meter
        scope.vfoa_freq = parsed.vfoa_freq
        start = parsed.scope_start_freq
        scope.scope_start_freq = (
            start if START_FREQ_MIN <= start <= START_FREQ_MAX else -1)

        now = time.time()
        if scope._last_frame_time > 0:
            delta = now - scope._last_frame_time
            if delta > 0:
                scope._fps = scope._fps * 0.9 + (1.0 / delta) * 0.1
        scope._last_frame_time = now
        scope._frame_count += 1
        scope.last_update = now
        if not getattr(scope, "_connected", False):
            scope._connected = True
            logger.info("acc_scope: first frame from the adapter (%s) — "
                        "spectrum active (span=%d)", self._label, scope.scope_span)

        self._frames += 1
        if self._on_frame is not None:
            await self._on_frame(scope)

    # ── diagnostics ────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """JSON-safe counters for the log line, tests and the support bundle."""
        decoder = getattr(self._source, "decoder", None)
        return {
            "label": self._label,
            "frames": self._frames,
            "tx": self._tx,
            "parse_errors": self._parse_errors,
            "tail_bad": self._tail_bad,
            "bad_xor": getattr(decoder, "bad_xor", 0),
            "bad_length": getattr(decoder, "bad_length", 0),
            "wrong_size": getattr(decoder, "wrong_size", 0),
            "reconnects": getattr(self._source, "reconnects", 0),
            "hellos": getattr(self._source, "hellos", 0),
        }
```

- [ ] **步骤 4：补 `__init__.py` 的导出**

```python
from backends.yaesu.acc_scope.producer import AccScopeProducer
from backends.yaesu.acc_scope.sources import SerialSource, TcpSource, UdpSource

__all__ = ["AccScopeProducer", "SerialSource", "TcpSource", "UdpSource"]
```

- [ ] **步骤 5：跑测试确认通过**

```bash
python -m unittest tests.test_acc_scope_producer -v
```

预期：`7 tests ... OK`

- [ ] **步骤 6：Commit**

```bash
git add backends/yaesu/acc_scope/ tests/test_acc_scope_producer.py
git commit -m "feat(acc_scope): ScopeProducer 实现（字段写入、span/频率兜底、tail 只告警）"
```

---

### 任务 6：`config.py` 的 env 与 `YaesuBackend` 接线

**文件：**

- 修改：`config.py`（在 SCU-LAN 块后，第 409 行之后插入）
- 修改：`backends/yaesu/backend.py`（第 137 行 `create_scope_producer`、第 168 行 `capabilities`、`__init__` 里加 `_acc_source = None`）
- 创建：`tests/test_acc_scope_contract.py`

- [ ] **步骤 1：写失败测试**

`tests/test_acc_scope_contract.py`：

```python
"""接线契约：默认关闭、串口守卫、scope_type、capabilities 不撒谎。"""
import unittest
from unittest.mock import patch

from backends.yaesu import backend as yb


class AccScopeContractTests(unittest.TestCase):
    def test_off_is_the_default_and_changes_nothing(self):
        with patch("config.ACC_SCOPE", "off"):
            b = yb.FTDX10Backend("/dev/null")
            self.assertIsNone(b.create_scope_producer())
            self.assertEqual(b.capabilities.scope_type, "none")

    def test_an_unknown_mode_falls_back_to_none(self):
        with patch("config.ACC_SCOPE", "carrier-pigeon"):
            b = yb.FTDX10Backend("/dev/null")
            self.assertIsNone(b.create_scope_producer())

    def test_serial_mode_reports_acc_and_returns_a_producer(self):
        with patch("config.ACC_SCOPE", "serial"), \
             patch("config.ACC_SCOPE_SERIAL", "/dev/ttyACM9"):
            b = yb.FTDX10Backend("/dev/null")
            producer = b.create_scope_producer()
            self.assertIsNotNone(producer)
            self.assertEqual(b.capabilities.scope_type, "acc")
            self.assertEqual(b.capabilities.scope_spans, {})

    def test_the_cat_port_is_never_stolen(self):
        with patch("config.ACC_SCOPE", "serial"), \
             patch("config.ACC_SCOPE_SERIAL", "/dev/null"):
            b = yb.FTDX10Backend("/dev/null")
            self.assertIsNone(b.create_scope_producer())
            self.assertEqual(b.capabilities.scope_type, "none")

    def test_tcp_mode_without_a_host_is_refused(self):
        with patch("config.ACC_SCOPE", "tcp"), \
             patch("config.ACC_SCOPE_HOST", ""):
            b = yb.FTDX10Backend("/dev/null")
            self.assertIsNone(b.create_scope_producer())

    def test_tcp_mode_with_a_host_returns_a_producer(self):
        with patch("config.ACC_SCOPE", "tcp"), \
             patch("config.ACC_SCOPE_HOST", "192.0.2.10"):
            b = yb.FTDX101MPBackend("/dev/null")
            self.assertIsNotNone(b.create_scope_producer())
            self.assertEqual(b.capabilities.scope_type, "acc")

    def test_the_config_is_resolved_once_not_per_capability_read(self):
        with patch("config.ACC_SCOPE", "tcp"), \
             patch("config.ACC_SCOPE_HOST", "192.0.2.10"):
            b = yb.FTDX10Backend("/dev/null")
            first = b.capabilities.scope_type
            second = b.capabilities.scope_type
            self.assertEqual(first, second)
            self.assertEqual(b._acc_cfg, ("tcp", "192.0.2.10"))
```

- [ ] **步骤 2：运行，确认失败**

```bash
python -m unittest tests.test_acc_scope_contract -v
```

预期：`AttributeError`/`AssertionError`（`ACC_SCOPE` 不存在，`_acc_cfg` 不存在）

- [ ] **步骤 3：在 `config.py` 加常量**

紧接 `SCU_LAN_PORT` 那行之后插入：

```python
# ── Yaesu ACC 频谱转接板（FTDX10 / FTDX101，设计 2026-11-15）─────────
#: `off` | `serial` | `tcp` | `udp`。默认关：关掉时行为与没有这条线逐字节相同。
#: 这是「只补频谱」的开关 —— CAT 与音频仍然走盒子到电台的 USB。
ACC_SCOPE = (_env("MRRC_ACC_SCOPE", "off") or "off").strip().lower()
#: `serial` 用：设备路径。留空则在 Linux/macOS 上自动找 `/dev/ttyACM*`；
#: Windows 必须显式给 `COMx`（不做猜测，宁可报错也不打开错误的设备）。
ACC_SCOPE_SERIAL = (_env("MRRC_ACC_SCOPE_SERIAL", "") or "").strip()
#: `tcp` / `udp` 用：转接板的地址。
ACC_SCOPE_HOST = (_env("MRRC_ACC_SCOPE_HOST", "") or "").strip()
ACC_SCOPE_TCP_PORT = _env_port("MRRC_ACC_SCOPE_TCP_PORT", 51234)
ACC_SCOPE_UDP_PORT = _env_port("MRRC_ACC_SCOPE_UDP_PORT", 51235)
#: USB-CDC 的名义波特率（CDC 不看这个值，保留是为了 pyserial 与日志）。
ACC_SCOPE_BAUD = _env_int("MRRC_ACC_SCOPE_BAUD", 115200)
```

- [ ] **步骤 4：在 `backend.py` 加解析、守卫与分支**

在 `__init__` 里 `self._scope = None` 之后加一行 `self._acc_cfg = None`（`None` 表示「还没解析过」；解析结果用 `("off", "")` 表示关闭，避免每次读 capabilities 都重复告警）。

在 `create_scope_producer` 之前插入：

```python
    # ── ACC 频谱转接板（设计 2026-11-15）───────────────────────────

    def _acc_scope_config(self):
        """``(kind, target)`` 或 ``None``；只解析一次，告警只打一次。

        三种关掉的路径都要**说出原因**：一个「配置了但没生效」的开关比没有更糟 ——
        用户在界面上只会看到 S-meter 合成的瀑布，没有任何线索。
        """
        if self._acc_cfg is not None:
            return None if self._acc_cfg[0] == "off" else self._acc_cfg
        kind = str(getattr(config, "ACC_SCOPE", "off") or "off").strip().lower()
        target = ""
        if kind not in ("serial", "tcp", "udp"):
            if kind != "off":
                logger.warning("MRRC_ACC_SCOPE=%r is not off|serial|tcp|udp — "
                               "spectrum stays on the S-meter fallback", kind)
            self._acc_cfg = ("off", "")
            return None
        if self._scu_client is not None:
            # 两种接线互斥：SCU-LAN10 占着电台的 USB 口，盒子这边没有适配器，
            # 再开一个串口源只会在日志里刷「打不开设备」。
            logger.info("MRRC_ACC_SCOPE ignored: this wiring uses the SCU-LAN10")
            self._acc_cfg = ("off", "")
            return None
        if kind == "serial":
            from backends.yaesu.acc_scope.sources import autodetect_serial_port
            target = (str(getattr(config, "ACC_SCOPE_SERIAL", "") or "").strip()
                      or (autodetect_serial_port() or ""))
            if not target:
                logger.warning(
                    "MRRC_ACC_SCOPE=serial but no adapter was found — set "
                    "MRRC_ACC_SCOPE_SERIAL (Windows needs an explicit COMx); "
                    "spectrum stays on the S-meter fallback")
                self._acc_cfg = ("off", "")
                return None
            from backends.yaesu.acc_scope.sources import same_device
            if same_device(target, self._port):
                logger.warning(
                    "MRRC_ACC_SCOPE_SERIAL=%s is the CAT port — refusing to open "
                    "it (the radio's serial link would break)", target)
                self._acc_cfg = ("off", "")
                return None
        else:
            target = str(getattr(config, "ACC_SCOPE_HOST", "") or "").strip()
            if not target:
                logger.warning(
                    "MRRC_ACC_SCOPE=%s but MRRC_ACC_SCOPE_HOST is empty — "
                    "spectrum stays on the S-meter fallback", kind)
                self._acc_cfg = ("off", "")
                return None
        self._acc_cfg = (kind, target)
        return self._acc_cfg
```

在 `create_scope_producer` 里，把方法体改成：

```python
    def create_scope_producer(self, scope_handler=None, on_frame=None):
        """The spectrum source for this wiring: SCU-LAN10, ACC adapter, or None."""
        if self._scu_client is not None:
            from backends.yaesu.scu_lan.scope import ScuLanScopeProducer
            self._scope = ScuLanScopeProducer(
                self._scu_client, cat=self._cat, scope=scope_handler,
                on_frame=on_frame)
            return self._scope
        acc = self._acc_scope_config()
        if acc is None:
            return None
        from backends.yaesu.acc_scope.producer import AccScopeProducer
        from backends.yaesu.acc_scope.sources import (
            SerialSource, TcpSource, UdpSource,
        )
        kind, target = acc
        if kind == "serial":
            source = SerialSource(target, baud=config.ACC_SCOPE_BAUD)
        elif kind == "tcp":
            source = TcpSource(target, config.ACC_SCOPE_TCP_PORT)
        else:
            source = UdpSource(target, config.ACC_SCOPE_UDP_PORT)
        self._acc_source = source
        self._scope = AccScopeProducer(source, scope_handler, on_frame,
                                       label=kind)
        logger.info("%s: spectrum from an ACC adapter (%s %s) — experimental, "
                    "not hardware-verified (see docs/ftdx10_acc_scope.md)",
                    self._display_name, kind, target)
        return self._scope
```

`capabilities` 里的两行改成：

```python
            scope_type=("scu_lan" if self._scu_client is not None
                        else ("acc" if self._acc_scope_config() is not None
                              else "none")),
            # ACC 的 span 表未真机验证，所以不发布名字（空表 = 界面不显示档位名）。
            scope_spans=(dict(SS_SPAN_HZ) if self._scu_client is not None
                         else {}),
```

- [ ] **步骤 5：跑测试确认通过**

```bash
python -m unittest tests.test_acc_scope_contract -v
python -m unittest tests.test_yaesu_backend -v
```

预期：前者 `7 tests ... OK`；后者全绿（`test_no_scope_producer` 仍然通过，因为默认 `off`）。

- [ ] **步骤 6：Commit**

```bash
git add config.py backends/yaesu/backend.py tests/test_acc_scope_contract.py
git commit -m "feat(acc_scope): env 开关、CAT 口守卫与 YaesuBackend 接线"
```

---

### 任务 7：操作手册 `docs/ftdx10_acc_scope.md`

**文件：**

- 创建：`docs/ftdx10_acc_scope.md`

- [ ] **步骤 1：写手册**

成稿内容（照抄即可，行文与仓里其它客户文档一致）：

````markdown
# FTDX10 / FTDX101 的瀑布：ACC 转接板

> **状态：实验功能，尚未在真机上验证。** 协议来自开源实现（见文末来源），
> 本仓的实现只在合成数据上测过。接上真机后请按文末「验收清单」逐条确认。

## 1. 这是什么，为什么不是 SCU-LAN10

Yaesu 新机型（FTDX10、FTDX101D/MP、FT-710）的瀑布只有两条路：买一台 SCU-LAN10，
或者在电台背后 13-pin ACC 口的 SPI 上挂一块转接板。这条线走后者，而且**只拿频谱**：

| 功能 | 走哪里 |
| --- | --- |
| 频率、模式、功率、PTT、记忆频道 | 电台的 USB 串口（CAT） |
| 收发音频 | 电台的 USB 声卡 |
| 瀑布 / 频谱 | **ACC 口 + 转接板**（本文） |

所以：一条 USB 线 + 一根 5 芯线到 ACC，不需要 SCU-LAN10。
FTDX101MP 的 `Rear Select` 菜单请保持指向 **USB**（指向 DATA 会让 USB 音频变哑，
且那条路是本产品在用的）。ACC 的音频针我们**不接、不用**。

## 2. 接线

| ACC（13-pin） | 转接板（ESP32-S3） | 说明 |
| --- | --- | --- |
| Pin 5（CLK） | GPIO10 | 电台是 SPI 主机，2 MHz |
| Pin 4（MOSI） | GPIO11 | 电台 → 转接板 |
| Pin 6（CS） | GPIO13 | 低有效 |
| Pin 12（电台 TX） | GPIO38 | 心跳接收，115200 8N1 |
| Pin 11（电台 RX） | GPIO39 | 心跳回复（也可把 11 与 12 直接短接） |
| GND | GND | **必须共地** |

> ⚠️ **ACC 的 Pin 8 是 13.8 V，内部只有一只 3.15 A 的**一次性**保险丝。**
> 短路的后果是拆机更换保险丝（已有人踩过）。接线与插拔前先关电台；
> 引脚不要用裸线试探，用压好的针脚或端子。

用电台自己的 USB 口给 CAT/音频用，转接板走第二条 USB（或 Wi-Fi，见 §4）。

## 3. 打开这一路（盒子侧）

在盒子的环境文件里加（或用「连接设置」页写入）：

```
MRRC_ACC_SCOPE=serial            # off | serial | tcp | udp
MRRC_ACC_SCOPE_SERIAL=/dev/ttyACM0   # 留空则自动找 /dev/ttyACM*
```

重启服务端。启动日志里应当出现：

```
acc_scope: serial adapter open on /dev/ttyACM0 @115200
acc_scope: first frame from the adapter (serial) — spectrum active (span=5)
```

界面上的瀑布就从「S-meter 合成」变成真实频谱。**没有这两行**说明没接上，
按 §6 排障；界面本身不会弹出错误（与 FT-710 的 FT4222 行为一致）。

## 4. 三种连法

| `MRRC_ACC_SCOPE` | 需要的另一项 | 适用 |
| --- | --- | --- |
| `serial` | `MRRC_ACC_SCOPE_SERIAL` | 转接板 USB 直连盒子（推荐） |
| `tcp` | `MRRC_ACC_SCOPE_HOST=<板子 IP>` | 转接板走 Wi-Fi，端口 51234 |
| `udp` | `MRRC_ACC_SCOPE_HOST=<板子 IP>` | 同上，端口 51235（本端每 5 s 发一次 HELLO 注册） |

Wi-Fi 只用于省一根线，仍是「盒子与电台在同一处」的部署；两台不在一个房间的
部署不是这条线的事。

## 5. 烧录与配网

转接板用的是开源项目的固件（ESP32-S3）：
<https://github.com/SLTPLAN/FTDX10_WF_HUB>

- Arduino IDE 开发板选 `ESP32S3 Dev Module`，**USB Mode = USB-OTG**；
- Wi-Fi 只支持 2.4 GHz，配网：USB 口发 `WIFI_SET <SSID> <PASSWORD>`，或长按 BOOT；
- 固件的 USB 日志口与数据口是两个 USB：日志口 115200 可看心跳/帧统计，
  数据口才是本产品要连的那个。

## 6. 排障

| 现象 | 先看 |
| --- | --- |
| 启动日志没有 `serial adapter open` | 设备路径对不对；`ls /dev/ttyACM*`；Linux 权限（`dialout` 组） |
| 有 `open` 但没有 `first frame` | 转接板没在收数据：心跳有没有回（短接 Pin11/12 试试）；ACC 排线是否插到底；SPI 三根线是否接反 |
| 瀑布有，但是倒的（强信号朝下） | 帧内取反约定与 FT-710 不同 —— 这是设计记录 §7 验收清单第 4 条，需要改实现，不是配置 |
| 日志里 `does not end with the FT-710 sync tail` | 帧能解但帧尾不同：说明元数据偏移不可信，频率/档位以 CAT 为准（此时频谱仍然可用） |
| `is the CAT port — refusing` | 把 `MRRC_ACC_SCOPE_SERIAL` 改成适配器的那一个，别指向电台的串口 |
| 瀑布很慢 / 一顿一顿 | 启动日志的 fps；`MRRC_ACC_SCOPE=tcp` 换 UDP 试（固件的 UDP 通道更快） |

## 7. 真机验收清单（五条，按顺序）

1. 接上转接板 → 启动日志出现 `serial adapter open` 与 `first frame`；
2. `/WSspectrum` 是真实瀑布（强信号会随天线/调谐变化，不是一条随 S 值变的假谱）；
3. 在电台面板上换档位（SPAN）→ 界面档位跟着变（说明元数据可用；否则见 §6 第 4 行）；
4. 强弱方向正确：强信号峰朝上；
5. 发射一次（短时）→ 瀑布停止/恢复，日志无 `bad_xor` 尖峰。

五条全过之后，才可以把 `docs/superpowers/specs/2026-11-15-ftdx10-acc-scope-design.md`
§7 的「只有真机能定」划掉，并考虑把 FTDX10 的 `provenance` 更新。

## 8. 来源与许可

- 协议与接线事实：`SLTPLAN/FTDX10_WF`、`SLTPLAN/FTDX10_WF_HUB`（作者在 FTDX10 上实测）。
  两个仓库都没有 LICENSE 声明，因此本仓只取协议事实、代码未复制。
- 4096 字节帧的解析沿用 `backends/ft710/scope_frame.py`（来自 wfview 的公开实现，重述）。
````

- [ ] **步骤 2：确认没有 markdown 静默损坏**

```bash
python -m unittest tests.test_release_artifacts -v
```

预期：全绿（这份文档不含版本号、不在 artifact 清单里；若失败，说明清单需要跟着改）

- [ ] **步骤 3：Commit**

```bash
git add docs/ftdx10_acc_scope.md
git commit -m "docs(acc_scope): FTDX10/FTDX101 ACC 转接板操作手册（含 ACC 13.8V 警告与真机验收清单）"
```

---

### 任务 8：文档同步（SDD / AGENTS / tests/README）与全量验证

**文件：**

- 修改：`SDD/08-architecture-decisions.md`（追加 AD-034）
- 修改：`SDD/14-version-history.md`（追加一条）
- 修改：`AGENTS.md`（后端表格一行 + 环境变量一句）
- 修改：`tests/README.md`（用例与模块计数）
- 修改：`docs/superpowers/specs/2026-11-15-ftdx10-acc-scope-design.md`（把 §8 的「假时钟」改成「毫秒级可注入间隔」，与实现一致）

- [ ] **步骤 1：追加 AD-034**

`SDD/08-architecture-decisions.md` 末尾追加：

```markdown
## AD-034: ACC 频谱源 —— 转接板路线（FTDX10 / FTDX101）

| Attribute | Value |
| --------- | ----- |
| Type | Architectural |
| Status | Implemented (experimental, not hardware-verified) |
| Decision | 频谱从电台背后 13-pin ACC 的 SPI 取，经 `66 CC FF` 帧进入 `AccScopeProducer`；CAT 与音频继续走 USB |

**Problem**: Yaesu FTDX10/FTDX101 的瀑布只有 SCU-LAN10 或 ACC SPI 两条路。SCU-LAN10
路线（AD 见 `docs/superpowers/specs/2026-11-15-scu-lan-backend-design.md`）卡在 TX 音频
内层头，不能发话；而客户没有 SCU-LAN10 时完全没有瀑布。

**Rationale**: ACC 路线把三条链路解耦 —— CAT 走 USB 串口（已实现）、音频走 USB 声卡
（已实现、双向）、只有频谱走 ACC。转接板只搬字节，4096 字节帧与 FT-710 相同，所以
`backends/ft710/scope_frame.py::parse_scope_frame` 直接复用，服务端 `_on_scope_frame`、
30 Hz 广播与 `spectrum_profile` 一行不改。FTDX101MP 的 `Rear Select` 让后部音频在
USB 与 DATA/ACC 之间二选一，这是「只做频谱」的技术依据，不只是工作量考虑。

**Consequences**: 新增 `backends/yaesu/acc_scope/`（解码 / 三个源 / producer）。
默认 `MRRC_ACC_SCOPE=off`，关闭时行为与之前逐字节相同；串口源拒绝与
`MRRC_SERIAL_PORT` 相同的设备（CAT 口不可抢）。未真机验证：帧尾是否 `FF 01 EE 01`、
元数据偏移、真帧率、FTDX101 的 wf2 —— 因此 `verified` 保持 `False`、TX 门禁不变，
且 `capabilities.scope_type="acc"` 只用于让界面知道自己有频谱。
```

- [ ] **步骤 2：追加版本历史**

`SDD/14-version-history.md` 末尾追加（日期用当天）：

```markdown
### 2026-11-15 — AD-034 ACC 频谱源（实验）

- 新增 `backends/yaesu/acc_scope/`：`66 CC FF` 解码（流式 + UDP 分片）、
  USB-CDC / TCP / UDP 三个源、`AccScopeProducer`。
- `config.py` 新增 `MRRC_ACC_SCOPE*` 六个变量，默认 `off`。
- `YaesuBackend.create_scope_producer()` 增加 ACC 分支；`capabilities.scope_type`
  增加 `"acc"`。
- 手册 `docs/ftdx10_acc_scope.md`；设计记录
  `docs/superpowers/specs/2026-11-15-ftdx10-acc-scope-design.md`。
- 未真机验证（帧尾、元数据偏移、真帧率、FTDX101 双通道）。
```

- [ ] **步骤 3：AGENTS.md 两处**

在 `backends/yaesu/` 那一行的表格单元末尾追加一句：

```
**ACC 频谱（实验，AD-034）**：`backends/yaesu/acc_scope/` 是「不买 SCU-LAN10 也能有瀑布」的第二条路 ——
`frames.py` 解转接板的 `66 CC FF` 帧（流式 6 字节头 / UDP 三片），`sources.py` 三条源各自重连
（USB-CDC 串口、TCP :51234、UDP :51235 先发 HELLO），`producer.py` 写 `ScopeHandler` 的字段后
交给未改动的 `_on_scope_frame`。只补频谱：CAT 与音频仍走 USB。`MRRC_ACC_SCOPE=off` 时逐字节维持原行为；
串口源拒绝打开 CAT 口。4096 帧解析复用 `backends/ft710/scope_frame.py`，真机未验证（`verified` 仍为 False）。
```

在「Environment variables」那一句里追加：

```
`MRRC_ACC_SCOPE`（`off`/`serial`/`tcp`/`udp`，默认 `off`）、`MRRC_ACC_SCOPE_SERIAL`、`MRRC_ACC_SCOPE_HOST`、`MRRC_ACC_SCOPE_TCP_PORT`（51234）、`MRRC_ACC_SCOPE_UDP_PORT`（51235）
```

- [ ] **步骤 4：跑全量测试并更新计数**

```bash
python -m unittest discover -s tests 2>&1 | tail -5
```

把输出的 `Ran N tests` 与模块数写进 `tests/README.md`（该文件已声明「总数以 `unittest` 为准」，
不要在这里复述别的数字）。

- [ ] **步骤 5：语法与守卫**

```bash
python -m py_compile backends/yaesu/acc_scope/*.py config.py backends/yaesu/backend.py
git add -A
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
```

预期：`py_compile` 无输出；守卫无 `✗ [block]`（`cat-direct-serial-io` 不适用于本包：
那是**转接板**的设备流，不是电台 CAT 串口，且代码里没有任何 CAT 帧的解析）。

- [ ] **步骤 6：Commit**

```bash
git commit -m "docs(acc_scope): SDD AD-034 + 版本历史 + AGENTS 表格 + 测试计数"
```

---

## 计划自检

**1. 规格覆盖度**（逐节对照 `2026-11-15-ftdx10-acc-scope-design.md`）

| 规格章节 | 实现它的任务 |
| --- | --- |
| §2.1 电台侧协议（只作为事实依据） | 任务 1 的合成帧工厂（布局、tail、取反） |
| §2.2 转接板两种线格式 + `MAX_PAYLOAD` + 对齐规则 | 任务 1（流式）、任务 4（UDP 分片） |
| §2.3 固件三条约束 | 任务 7 手册 §5（烧录指引）—— 这三条是 P2 的输入，本期不改代码 |
| §3 目录与职责 | 任务 1/2/3/4/5 的文件清单 |
| §4 数据流（下游零改动） | 任务 5（只写既有字段）+ 任务 6（不动 server.py） |
| §5 配置与串口守卫 | 任务 6（`config.py` + `_acc_scope_config` + 契约测试） |
| §6 八个失效模式 | 任务 2/3/4（重连、HELLO）、任务 5（幂等、`stop` 掉线）、任务 1（假帧头、缺片） |
| §7 六条未知 + 5 条真机验收 | 任务 7 手册 §6/§7；`verified` 与 TX 门禁不动（任务 8 的 AD 文本） |
| §8 四个测试模块 + 合成帧工厂 | 任务 1/2/3/4/5/6 的测试文件 |
| §9 不做的事 | 计划里没有固件 / PCB / 双通道 / FT4222 任务 |
| §10 许可 | 任务 7 手册 §8；AD 文本 `Rationale` |
| §11 分期 P1 | 全部任务即 P1 |

**2. 占位符扫描**：无「待定 / TODO / 类似任务 N」；每个代码步骤都有可粘贴的完整代码，命令都写了预期输出。

**3. 类型一致性**：

- `DecodedFrame(payload, tail_ok)` 在任务 1 定义、任务 2/4/5 使用，字段名一致。
- `FrameDecoder` 的计数器名（`bad_xor`/`bad_length`/`wrong_size`/`tail_mismatch`/`frames`）在任务 1 定义、任务 5 的 `snapshot()` 读取，名字一致。
- `UdpReassembler(queue)` 与 `UdpSource.queue` 的关系：任务 4 的测试用 `source.queue.put_nowait(...)`，类里以 `queue` property 暴露同名对象。
- `AccScopeProducer(source, scope, on_frame, *, label)` 与任务 6 的调用点一致。
- `_acc_cfg` 是任务 6 测试里读的字段名，`__init__` 初始化、`_acc_scope_config()` 写入。

**4. 与规格的两处**有意**偏差（已在任务 8 的步骤里同步回规格）**：

- 规格 §8 写「假时钟」，实现用的是「毫秒级可注入间隔」（`retry_s` / `hello_interval_s`）—— 语义相同、代码更简单；任务 8 步骤 1 之前先改规格那一句。
- 规格 §6 只提到 span 的合法性窗口；实现对 `scope_start_freq` 也加了同样的窗口（`1 kHz…54 MHz`，否则 `-1`），因为元数据偏移一旦不对，那两个字段会一起坏。

---

## 执行交接

计划已保存到 `docs/superpowers/plans/2026-11-15-ftdx10-acc-scope.md`。两种执行方式：

1. **子代理驱动（推荐）** —— 每个任务一个新子代理，任务间审查，快速迭代（必需子技能：`superpowers:subagent-driven-development`）
2. **内联执行** —— 在当前会话里按 `superpowers:executing-plans` 批量执行并设检查点

选哪种？
