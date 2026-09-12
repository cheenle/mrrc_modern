# 服务端 QSO 录音（增量 MP3）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**Goal:** 把 QSO 录音从浏览器搬到服务端：在 RX 设备域 PCM 与已解码的 TX 麦克风 PCM 上取音，按单一时钟轴（16 kHz 单声道、真实停顿补静音）**增量**编码成 MP3 边录边落盘，并在网页面板里提供列表／播放（支持 seek）／下载／删除。

**Architecture:** 新增协议中立模块 `recorder.py`（`_StreamingDecimator` + `RecordingSession` + 文件名/索引工具）；`server.py` 在 `_audio_rx_loop` 读取 PCM 之后与 `/WSaudioTX` 解码之后各加一个 tap，经**单写入者队列 + 专用 writer 任务**（`asyncio.to_thread` 编码写盘，事件循环零阻塞）；控制面复用已鉴权的 `/WSradio`（`set{field:"recording"}` + `recordingState` 广播 + `fullState` 字段），文件面走三个 REST 路由（列表／Range 流／删除）。

**Tech stack:** Python 3.13、`unittest`/`IsolatedAsyncioTestCase`、numpy、`lameenc`（新增，libmp3lame 预编译 wheel）、Starlette `FileResponse`（自带 Range）、FastAPI、原生 JavaScript、SDD Guardian。

**设计权威：** `docs/superpowers/specs/2026-09-12-server-side-recording-design.md`（commit `94420b9`）。

---

## File Structure

- **Create** `recorder.py`：`_StreamingDecimator`（FIR 抽取器，移植 mrrc）、`RecordingSession`（时间轴 + 增量 MP3）、`recording_name()`/`parse_recording_name()`、索引工具（`load_index`/`save_index`/`list_recordings`）。唯一职责：把 PCM 变成磁盘上的 MP3 与它的元数据，不做 I/O 之外的任何事。
- **Create** `tests/test_recorder.py`（会话/抽取器/索引/崩溃安全）、`tests/test_recorder_api.py`（REST 契约与路径安全）。
- **Modify** `config.py`：`RECORDINGS_BITRATE`、`RECORDINGS_MAX_SESSION_MIN`。
- **Modify** `server.py`：`RECORDINGS_DIR`/`RECORDINGS_INDEX`、writer 任务、RX/TX tap、`recording` set 命令、`recordingState` 广播 + `fullState`、三个 REST 路由、生命周期启停。
- **Modify** `static/index.html`（菜单项 + cache-bust）、`static/ft710_ui.js`（REC 按钮 + 录音面板 + `renderRecordingState` + 菜单分发）、`static/ft710_main.js`（`recordingState` 落状态；移除 `RXRecorder` 与 lame 加载器）、`static/sw.js`（版本）。
- **Delete** `static/modules/lame.js`（530 KB 死资源）。
- **Modify** `requirements.txt`、`packaging/pyinstaller/mrrc_modern_server.spec`、`windows/launcher.py`、`macos/launcher.py`。
- **Modify tests**：`tests/test_audio.py`、`tests/test_server_ws_protocol.py`、`tests/test_config.py`、`tests/README.md`。
- **Modify docs**：SDD `05/07/08/09/10/11/12/13/14`+`README`、`AGENTS.md`、`README.md`、`CHANGELOG.md`、`docs/OPERATION_GUIDE.md`、`docs/RASPBERRY_PI_GUIDE.md`、`website/guide.html`、`website/zh/guide.html`、`.agents/skills/sdd-guardian/harness/index.json`。

---

### 任务 1：`recorder.py` 的 FIR 抽取器

**文件：**

- 创建：`recorder.py`
- 创建：`tests/test_recorder.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_recorder.py`：

```python
"""Tests for the server-side recorder (spec 2026-09-12 §4/§5).

Everything here is hardware-free: PCM blocks are synthesised, sessions
write into a temporary directory, and the MP3 is inspected as a byte
stream (no decoder needed).
"""
import math
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from recorder import (
    RECORDING_RATE,
    RecordingSession,
    _StreamingDecimator,
    list_recordings,
    parse_recording_name,
    recording_name,
    save_index,
    load_index,
)


def sine_pcm(freq_hz: float, seconds: float, rate: int, amp: int = 12000) -> bytes:
    n = int(rate * seconds)
    return np.array(
        [int(amp * math.sin(2 * math.pi * freq_hz * i / rate)) for i in range(n)],
        dtype='<i2',
    ).tobytes()


def rms_int16(pcm: bytes) -> float:
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float64)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples ** 2)))


class StreamingDecimatorTests(unittest.TestCase):
    """Ported expectations from mrrc's recording-session tests."""

    def test_passband_voice_survives(self):
        # A 1 kHz tone must come through nearly unchanged (level-wise).
        dec = _StreamingDecimator(48000, RECORDING_RATE)
        pcm = sine_pcm(1000, 0.5, 48000)
        out = dec.process(np.frombuffer(pcm, dtype='<i2'))
        self.assertEqual(out.dtype, np.dtype('<i2'))
        # 48k -> 16k is 1:3, so a third of the samples come out.
        self.assertAlmostEqual(out.size, len(pcm) // 2 // 3, delta=2)
        self.assertGreater(rms_int16(out.tobytes()), 0.5 * rms_int16(pcm))

    def test_stopband_tone_is_rejected(self):
        # 15 kHz is above the 5.5 kHz recording band -> must be attenuated.
        dec = _StreamingDecimator(48000, RECORDING_RATE)
        pcm = sine_pcm(15000, 0.5, 48000)
        out = dec.process(np.frombuffer(pcm, dtype='<i2'))
        self.assertLess(rms_int16(out.tobytes()), 0.1 * rms_int16(pcm))

    def test_state_is_continuous_across_blocks(self):
        # Feeding one long block and feeding it in 20 ms pieces must agree.
        pcm = sine_pcm(800, 0.4, 48000)
        whole = _StreamingDecimator(48000, RECORDING_RATE).process(
            np.frombuffer(pcm, dtype='<i2'))
        piecewise = _StreamingDecimator(48000, RECORDING_RATE)
        samples = np.frombuffer(pcm, dtype='<i2')
        chunks = [piecewise.process(samples[i:i + 960])
                  for i in range(0, len(samples), 960)]
        joined = np.concatenate(chunks)
        self.assertEqual(joined.size, whole.size)
        # Filter state continuity: no discontinuity at block boundaries.
        self.assertLess(int(np.abs(joined.astype(np.int32)
                                   - whole.astype(np.int32)).max()), 64)

    def test_rejects_non_integer_ratio(self):
        with self.assertRaises(ValueError):
            _StreamingDecimator(44100, RECORDING_RATE)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_recorder.py" 2>&1 | tail -5`
预期：FAIL，`ModuleNotFoundError: No module named 'recorder'`

- [ ] **步骤 3：编写最少实现代码**

创建 `recorder.py`（本任务是抽取器部分，其余部分在后续任务补齐）：

```python
"""
Server-side QSO recorder
========================
Records RX (device-domain PCM, straight off the sound card) and TX
(decoded browser-mic PCM) onto one monotonic mono timeline and encodes
it incrementally to MP3 while recording.

Why the server owns this now: the previous browser recorder concatenated
whatever frames arrived, with no timestamps, so delivery jitter and the
jitter buffer's padding were baked into the file (reported symptom:
"trembling" playback).  Here every block carries a monotonic timestamp,
a genuine pause becomes silence, and 50 ms of scheduling jitter is
absorbed instead of turning into a hole.

Storage domain: the recording timeline is 16 kHz mono (same as the
sibling `mrrc` project, whose tooling consumes a `recordings/`
directory).  That is a WRITE-ONLY sink reached FROM the 48 kHz codec
domain: 44.1 kHz device audio goes through the sanctioned
`audio_resample` bridge, then a purpose-built anti-aliasing FIR
decimator produces 16 kHz.  Nothing from this module re-enters the codec
or device domain, so AD-011 still holds (see AD-017).

Crash safety: frames are written as they are produced; a killed process
leaves a playable MP3 (a missing Xing header does not stop browsers).
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("recorder")

#: Storage-domain sample rate (AD-017).  Not the codec or device rate.
RECORDING_RATE = 16000

#: mrrc-compatible file name: <freq kHz>_<YYYYmmdd>_<HHMMSS>.mp3
_NAME_RE = re.compile(r"^(\d{5})kHz_(\d{8})_(\d{6})\.mp3$")


class _StreamingDecimator:
    """Stateful FIR low-pass filter followed by integer-ratio decimation.

    Ported from the sibling `mrrc` project's recording_session.py: a
    windowed-sinc low pass removes everything above the recording band
    before samples are dropped, so decimation cannot alias high-frequency
    energy back into the voice band.
    """

    def __init__(self, source_rate: int, target_rate: int, ntaps: int = 96):
        if source_rate % target_rate != 0:
            raise ValueError("source_rate must be an integer multiple of target_rate")
        self.factor = source_rate // target_rate
        self._ntaps = ntaps
        n = np.arange(ntaps) - (ntaps - 1) / 2.0
        cutoff_hz = min(5500.0, target_rate * 0.4)
        coefficients = (
            2.0 * cutoff_hz / source_rate
            * np.sinc(2.0 * cutoff_hz / source_rate * n)
        )
        coefficients *= np.hamming(ntaps)
        coefficients /= np.sum(coefficients)
        self._coefficients = coefficients.astype(np.float64)
        self._state = np.zeros(ntaps - 1, dtype=np.float64)
        self._phase = 0

    def process(self, samples) -> np.ndarray:
        """Filter + decimate one block; returns int16 samples."""
        values = np.asarray(samples, dtype=np.float64)
        if values.size == 0:
            return np.zeros(0, dtype=np.int16)
        combined = np.concatenate((self._state, values))
        filtered = np.convolve(combined, self._coefficients)[
            self._ntaps - 1: self._ntaps - 1 + values.size
        ]
        self._state = combined[-(self._ntaps - 1):]
        output = filtered[self._phase::self.factor]
        self._phase = (self._phase - values.size) % self.factor
        return np.clip(np.rint(output), -32768, 32767).astype(np.int16)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_recorder.py" 2>&1 | tail -5`
预期：`Ran 4 tests` … `OK`

- [ ] **步骤 5：Commit**

```bash
git add recorder.py tests/test_recorder.py
git commit -m "feat(recorder): anti-aliasing FIR decimator (48k->16k storage domain)"
```

---

### 任务 2：文件名与索引工具

**文件：**

- 修改：`recorder.py`
- 测试：`tests/test_recorder.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_recorder.py`：

```python
class RecordingNameTests(unittest.TestCase):
    def test_generates_mrrc_compatible_name(self):
        name = recording_name(14_270_000, datetime(2026, 9, 12, 21, 4, 5))
        self.assertEqual(name, "14270kHz_20260912_210405.mp3")

    def test_zero_frequency_matches_mrrc(self):
        # CAT offline: mrrc writes 00000kHz; the panel shows "—".
        self.assertEqual(
            recording_name(0, datetime(2026, 9, 12, 21, 4, 5)),
            "00000kHz_20260912_210405.mp3")

    def test_parses_its_own_names(self):
        parsed = parse_recording_name("07050kHz_20260912_210405.mp3")
        self.assertEqual(parsed["freq_hz"], 7_050_000)
        self.assertEqual(parsed["date"], "20260912")
        self.assertEqual(parsed["time"], "210405")

    def test_rejects_other_names(self):
        for bad in ("x.mp3", "14270kHz_20260912_210405.wav",
                    "../14270kHz_20260912_210405.mp3",
                    "14270kHz_20260912_210405.mp3.mp3",
                    "/etc/passwd", "", "1427kHz_20260912_210405.mp3"):
            with self.subTest(name=bad):
                self.assertIsNone(parse_recording_name(bad))


class IndexTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.index = self.dir / "recordings.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip_and_prune(self):
        (self.dir / "07050kHz_20260912_210405.mp3").write_bytes(b"x" * 100)
        save_index(self.index, {"07050kHz_20260912_210405.mp3": {
            "freq_hz": 7_050_000, "started_at": "2026-09-12T21:04:05",
            "duration": 3.0, "bytes": 100}})
        loaded = load_index(self.index)
        self.assertIn("07050kHz_20260912_210405.mp3", loaded)

        # An entry whose file vanished is dropped from the listing.
        (self.dir / "07050kHz_20260912_210405.mp3").unlink()
        rows = list_recordings(self.dir, loaded, bitrate=64)
        self.assertEqual(rows, [])

    def test_listing_sorts_newest_first_and_sums_bytes(self):
        older = "07050kHz_20260912_200000.mp3"
        newer = "14270kHz_20260912_210000.mp3"
        (self.dir / older).write_bytes(b"x" * 200)
        (self.dir / newer).write_bytes(b"x" * 400)
        index = {
            older: {"freq_hz": 7_050_000, "started_at": "2026-09-12T20:00:00",
                    "duration": 1.0, "bytes": 200},
            newer: {"freq_hz": 14_270_000, "started_at": "2026-09-12T21:00:00",
                    "duration": 2.0, "bytes": 400},
        }
        rows = list_recordings(self.dir, index, bitrate=64)
        self.assertEqual([r["name"] for r in rows], [newer, older])
        self.assertEqual(rows[0]["freq_hz"], 14_270_000)
        self.assertEqual(rows[0]["duration"], 2.0)

    def test_duration_falls_back_to_size_for_unknown_files(self):
        # A file the operator dropped in themselves: no index entry, so
        # duration comes from the size and the configured CBR bitrate.
        name = "07050kHz_20260912_210405.mp3"
        (self.dir / name).write_bytes(b"x" * 8000)   # 8000*8/64000 = 1.0 s
        rows = list_recordings(self.dir, {}, bitrate=64)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["duration"], 1.0, places=3)
        self.assertEqual(rows[0]["freq_hz"], 7_050_000)

    def test_listing_ignores_foreign_files(self):
        (self.dir / "notes.txt").write_text("x")
        (self.dir / "random.mp3").write_bytes(b"x")
        self.assertEqual(list_recordings(self.dir, {}, bitrate=64), [])

    def test_missing_directory_is_empty_not_an_error(self):
        self.assertEqual(list_recordings(self.dir / "nope", {}, 64), [])
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_recorder.py" 2>&1 | tail -5`
预期：FAIL，`ImportError: cannot import name 'recording_name' from 'recorder'`

- [ ] **步骤 3：编写最少实现代码**

追加到 `recorder.py`：

```python
def recording_name(freq_hz: int, when: Optional[datetime] = None) -> str:
    """mrrc-compatible file name for a new recording."""
    when = when or datetime.now()
    khz = int(freq_hz / 1000) if freq_hz and freq_hz > 0 else 0
    return f"{khz:05d}kHz_{when:%Y%m%d_%H%M%S}.mp3"


def parse_recording_name(name: str) -> Optional[dict]:
    """Parse a recorder file name, or return None when it is not ours.

    This is the only accepted shape: the REST routes reject anything else,
    which is what keeps path traversal out of the recordings API.
    """
    match = _NAME_RE.match(name or "")
    if not match:
        return None
    return {
        "freq_hz": int(match.group(1)) * 1000,
        "date": match.group(2),
        "time": match.group(3),
    }


@dataclass
class RecordingInfo:
    """Metadata for one finished recording."""

    name: str
    freq_hz: int
    started_at: str
    duration: float
    bytes: int


def load_index(path: Path) -> dict:
    """Read the recordings index; a missing/corrupt file is an empty index."""
    import json
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    entries = data.get("recordings") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def save_index(path: Path, entries: dict) -> None:
    """Atomically write the recordings index (tmp file + replace)."""
    import json
    import os
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "recordings": entries}, indent=2))
    os.replace(tmp, path)


def list_recordings(directory: Path, index: dict, bitrate: int) -> list:
    """List our recordings, newest first, merging the index with the disk.

    The directory is the source of truth for existence (an index entry
    whose file is gone disappears from the list); the index supplies the
    exact duration, and anything without an entry (a file the operator
    copied in) falls back to size/bitrate arithmetic.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    rows = []
    for path in directory.iterdir():
        parsed = parse_recording_name(path.name)
        if parsed is None or not path.is_file():
            continue
        meta = index.get(path.name) or {}
        size = path.stat().st_size
        duration = meta.get("duration")
        if not isinstance(duration, (int, float)) or duration <= 0:
            duration = size * 8.0 / (bitrate * 1000) if bitrate > 0 else 0.0
        rows.append({
            "name": path.name,
            "freq_hz": meta.get("freq_hz", parsed["freq_hz"]),
            "started_at": meta.get("started_at")
            or f"{parsed['date'][:4]}-{parsed['date'][4:6]}-{parsed['date'][6:]}T"
               f"{parsed['time'][:2]}:{parsed['time'][2:4]}:{parsed['time'][4:]}",
            "duration": float(duration),
            "bytes": size,
        })
    rows.sort(key=lambda r: (r["started_at"], r["name"]), reverse=True)
    return rows
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_recorder.py" 2>&1 | tail -5`
预期：`Ran 14 tests` … `OK`

- [ ] **步骤 5：Commit**

```bash
git add recorder.py tests/test_recorder.py
git commit -m "feat(recorder): mrrc-compatible naming + recordings index helpers"
```

---

### 任务 3：`RecordingSession` —— 时间轴、空洞填充与增量 MP3

**文件：**

- 修改：`recorder.py`
- 测试：`tests/test_recorder.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_recorder.py`：

```python
def _session(dirpath: Path, **kw) -> RecordingSession:
    return RecordingSession(dirpath, bitrate=64, max_seconds=kw.pop("max_seconds", 60),
                            **kw)


class RecordingSessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_start_creates_a_file_and_refuses_a_second_session(self):
        s = _session(self.dir)
        self.assertTrue(s.start(freq_hz=14_270_000))
        self.assertTrue(s.active)
        self.assertFalse(s.start(freq_hz=7_050_000))     # already recording
        self.assertEqual(len(list(self.dir.glob("*.mp3"))), 1)
        info = s.stop()
        self.assertIsNotNone(info)
        self.assertFalse(s.active)
        self.assertEqual(info.freq_hz, 14_270_000)
        self.assertGreater(info.duration, 0.0)

    def test_file_grows_while_recording(self):
        s = _session(self.dir)
        s.start()
        for _ in range(40):                              # 40 x 20 ms = 0.8 s
            s.add_audio("rx", sine_pcm(1000, 0.02, 48000), 48000)
        size_mid = list(self.dir.glob("*.mp3"))[0].stat().st_size
        self.assertGreater(size_mid, 0)                  # already on disk
        info = s.stop()
        self.assertGreaterEqual(info.bytes, size_mid)

    def test_gap_is_filled_with_silence(self):
        s = _session(self.dir)
        s.start()
        base = time.monotonic_ns()
        block = sine_pcm(1000, 0.02, 48000)
        s.add_audio("rx", block, 48000, base)
        # 500 ms later -> the timeline must contain the hole as silence.
        s.add_audio("rx", block, 48000, base + 500_000_000)
        info = s.stop(now_ns=base + 520_000_000)
        self.assertAlmostEqual(info.duration, 0.52, places=2)
        # Silent gap => much smaller file than continuous tone of that length.
        continuous = _session(self.dir / "c")
        self.dir.joinpath("c").mkdir(exist_ok=True)
        continuous.start()
        for i in range(26):
            continuous.add_audio("rx", block, 48000, base + i * 20_000_000)
        cont_info = continuous.stop(now_ns=base + 520_000_000)
        self.assertLess(info.bytes, cont_info.bytes)

    def test_jitter_within_tolerance_does_not_punch_holes(self):
        s = _session(self.dir)
        s.start()
        base = time.monotonic_ns()
        block = sine_pcm(1000, 0.02, 48000)
        # 20 ms blocks arriving 10 ms late each: inside the 50 ms tolerance,
        # so they must stay contiguous (no silence inserted).
        for i in range(10):
            s.add_audio("rx", block, 48000, base + i * 30_000_000)
        info = s.stop(now_ns=base + 300_000_000)
        self.assertAlmostEqual(info.duration, 0.2, places=2)

    def test_source_switch_reanchors(self):
        s = _session(self.dir)
        s.start()
        base = time.monotonic_ns()
        block = sine_pcm(1000, 0.02, 48000)
        s.add_audio("rx", block, 48000, base)
        # TX starts 100 ms later: a new talk spurt, anchored at its own time.
        s.add_audio("tx", block, 48000, base + 100_000_000)
        info = s.stop(now_ns=base + 120_000_000)
        self.assertAlmostEqual(info.duration, 0.12, places=2)

    def test_44100_device_audio_is_accepted(self):
        s = _session(self.dir)
        s.start()
        base = time.monotonic_ns()
        # FT-710 device domain: 882 samples = 20 ms @44.1k
        s.add_audio("rx", sine_pcm(1000, 0.02, 44100), 44100, base)
        info = s.stop(now_ns=base + 20_000_000)
        self.assertGreater(info.duration, 0.0)

    def test_session_cap_stops_storing_but_not_the_file(self):
        s = RecordingSession(self.dir, bitrate=64, max_seconds=0.1)
        s.start()
        base = time.monotonic_ns()
        block = sine_pcm(1000, 0.02, 48000)
        accepted = [s.add_audio("rx", block, 48000, base + i * 20_000_000)
                    for i in range(20)]
        self.assertIn(False, accepted)                   # past the cap
        info = s.stop()
        self.assertLessEqual(info.duration, 0.11)

    def test_odd_length_pcm_is_trimmed_not_raised(self):
        # lameenc raises on byte-misaligned input; the recorder must never
        # let that reach the audio path.
        s = _session(self.dir)
        s.start()
        self.assertTrue(s.add_audio("rx", sine_pcm(1000, 0.02, 48000) + b"\x01", 48000))
        self.assertIsNotNone(s.stop())

    def test_abandoned_session_leaves_a_playable_prefix(self):
        # Crash safety: no stop(), no flush() — the bytes on disk must exist.
        s = _session(self.dir)
        s.start()
        for i in range(25):
            s.add_audio("rx", sine_pcm(1000, 0.02, 48000), 48000,
                        time.monotonic_ns() + i * 20_000_000)
        path = next(self.dir.glob("*.mp3"))
        self.assertGreater(path.stat().st_size, 0)
        s.close_without_finishing()                      # simulate abandonment
        self.assertGreater(path.stat().st_size, 0)

    def test_status_reports_progress(self):
        s = _session(self.dir)
        self.assertFalse(s.status()["recording"])
        s.start(freq_hz=7_050_000)
        base = time.monotonic_ns()
        s.add_audio("rx", sine_pcm(1000, 0.02, 48000), 48000, base)
        st = s.status(now_ns=base + 250_000_000)
        self.assertTrue(st["recording"])
        self.assertEqual(st["freq_hz"], 7_050_000)
        self.assertAlmostEqual(st["duration"], 0.25, places=2)
        self.assertTrue(st["name"].endswith(".mp3"))
        s.stop()

    def test_stop_when_idle_is_none(self):
        self.assertIsNone(_session(self.dir).stop())
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_recorder.py" 2>&1 | tail -5`
预期：FAIL，`ImportError: cannot import name 'RecordingSession' from 'recorder'`（或 `AttributeError: 'RecordingSession' has no attribute 'start'`）

- [ ] **步骤 3：编写最少实现代码**

追加到 `recorder.py`（`import json/os/lameenc` 放到文件顶部）：

```python
        # 顶部新增：
        #   import json
        #   import os
        #   import lameenc


class RecordingSession:
    """One QSO recording: a 16 kHz mono timeline written incrementally.

    All mutation happens on a single thread (server.py funnels calls
    through one writer task), so no lock is needed here.
    """

    #: Blocks closer than this to the previous one are treated as
    #: contiguous — this absorbs scheduler jitter instead of turning it
    #: into a silence gap (the property that makes playback smooth).
    CONTINUITY_TOLERANCE_SAMPLES = RECORDING_RATE * 5 // 100     # 50 ms
    #: Silence is encoded in chunks so a long pause does not allocate a
    #: single huge buffer.
    _SILENCE_CHUNK = RECORDING_RATE // 50                        # 20 ms

    def __init__(self, directory, bitrate: int = 64, max_seconds: float = 4 * 3600,
                 quality: int = 2):
        self.directory = Path(directory)
        self.bitrate = int(bitrate)
        self.max_seconds = float(max_seconds)
        self.quality = int(quality)
        self._enc = None
        self._fh = None
        self._active = False
        self._start_ns = None
        self._started_at = None
        self._freq_hz = 0
        self._name = None
        self._cursor = 0
        self._bytes = 0
        self._decimators = {}
        self._source_cursors = {}
        self._last_source = None

    # ── Lifecycle ──────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._active

    @property
    def max_samples(self) -> int:
        return int(RECORDING_RATE * self.max_seconds)

    def start(self, freq_hz: int = 0, now: Optional[datetime] = None) -> bool:
        """Begin a recording; False when one is already running."""
        if self._active:
            return False
        self.directory.mkdir(parents=True, exist_ok=True)
        self._name = recording_name(freq_hz, now)
        path = self.directory / self._name
        self._fh = open(path, "wb")
        self._enc = lameenc.Encoder()
        self._enc.set_channels(1)
        self._enc.set_in_sample_rate(RECORDING_RATE)
        self._enc.set_bit_rate(self.bitrate)
        self._enc.set_quality(self.quality)
        self._start_ns = time.monotonic_ns()
        self._started_at = (now or datetime.now()).isoformat(timespec="seconds")
        self._freq_hz = int(freq_hz or 0)
        self._cursor = 0
        self._bytes = 0
        self._decimators = {}
        self._source_cursors = {}
        self._last_source = None
        self._active = True
        logger.info("Recording started: %s (%d kbps, %d Hz mono)",
                    self._name, self.bitrate, RECORDING_RATE)
        return True

    def add_audio(self, source: str, pcm: bytes, source_rate: int,
                  timestamp_ns: Optional[int] = None) -> bool:
        """Place one PCM block on the timeline; False when ignored."""
        if not self._active or not pcm:
            return False
        if source_rate <= 0:
            raise ValueError("source_rate must be positive")
        # lameenc rejects non-int16-aligned input; trim here so a stray
        # odd byte can never raise into the audio path.
        if len(pcm) % 2:
            pcm = pcm[:-1]
        samples = np.frombuffer(pcm, dtype='<i2')
        if samples.size == 0:
            return False
        if source_rate != RECORDING_RATE:
            key = (source, int(source_rate))
            decimator = self._decimators.get(key)
            if decimator is None:
                decimator = _StreamingDecimator(int(source_rate), RECORDING_RATE)
                self._decimators[key] = decimator
            samples = decimator.process(samples)

        timestamp_ns = (time.monotonic_ns() if timestamp_ns is None
                        else int(timestamp_ns))
        offset = max(0, (timestamp_ns - self._start_ns) * RECORDING_RATE
                     // 1_000_000_000)
        expected = self._source_cursors.get(source)
        if (expected is not None and source == self._last_source
                and abs(offset - expected) <= self.CONTINUITY_TOLERANCE_SAMPLES):
            offset = expected
        if offset >= self.max_samples:
            return False
        samples = samples[: self.max_samples - offset]
        if samples.size == 0:
            return False
        self._write_silence_upto(offset)
        self._encode(samples)
        self._source_cursors[source] = offset + samples.size
        self._last_source = source
        return True

    def stop(self, now_ns: Optional[int] = None) -> Optional[RecordingInfo]:
        """Finish the recording: pad, flush, close and report."""
        if not self._active:
            return None
        now_ns = time.monotonic_ns() if now_ns is None else int(now_ns)
        stop_offset = min(max(0, (now_ns - self._start_ns) * RECORDING_RATE
                              // 1_000_000_000), self.max_samples)
        self._write_silence_upto(max(stop_offset, self._cursor))
        tail = b""
        try:
            tail = self._enc.flush()                    # final frames + Xing
        except Exception as e:                          # pragma: no cover
            logger.warning("MP3 flush failed: %s", e)
        if tail:
            self._fh.write(tail)
            self._bytes += len(tail)
        self._fh.close()
        info = RecordingInfo(
            name=self._name,
            freq_hz=self._freq_hz,
            started_at=self._started_at,
            duration=self._cursor / RECORDING_RATE,
            bytes=self._bytes,
        )
        logger.info("Recording stopped: %s (%.1fs, %d bytes)",
                    info.name, info.duration, info.bytes)
        self._reset()
        return info

    def close_without_finishing(self) -> None:
        """Abandon the session (shutdown/crash path): never flush."""
        if self._active and self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except OSError:
                pass
        self._reset()

    def _reset(self) -> None:
        self._active = False
        self._enc = None
        self._fh = None
        self._name = None
        self._start_ns = None
        self._started_at = None
        self._freq_hz = 0
        self._cursor = 0
        self._bytes = 0
        self._decimators = {}
        self._source_cursors = {}
        self._last_source = None

    # ── Timeline / encoding ────────────────────────────────────────

    def _write_silence_upto(self, offset_samples: int) -> None:
        """Encode silence until the timeline cursor reaches *offset*."""
        while self._cursor < offset_samples:
            count = min(self._SILENCE_CHUNK, offset_samples - self._cursor)
            self._encode(np.zeros(count, dtype=np.int16))

    def _encode(self, samples: np.ndarray) -> None:
        payload = np.ascontiguousarray(samples, dtype='<i2').tobytes()
        data = self._enc.encode(payload)
        if data:
            self._fh.write(data)
            self._bytes += len(data)
        self._cursor += len(samples)

    # ── Status ─────────────────────────────────────────────────────

    def status(self, now_ns: Optional[int] = None) -> dict:
        """Snapshot for the ``recordingState`` broadcast."""
        duration = 0.0
        if self._active and self._start_ns is not None:
            now_ns = time.monotonic_ns() if now_ns is None else int(now_ns)
            duration = max(0.0, (now_ns - self._start_ns) / 1_000_000_000)
        return {
            "recording": self._active,
            "freq_hz": self._freq_hz,
            "started_at": self._started_at,
            "duration": round(duration, 2),
            "name": self._name,
            "bytes": self._bytes,
        }
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_recorder.py" 2>&1 | tail -5`
预期：`Ran 25 tests` … `OK`

- [ ] **步骤 5：Commit**

```bash
git add recorder.py tests/test_recorder.py
git commit -m "feat(recorder): timeline session with silence gap fill + incremental MP3"
```

---

### 任务 4：配置与录音目录

**文件：**

- 修改：`config.py`（新增两个 env）
- 修改：`server.py`（`RECORDINGS_DIR` / `RECORDINGS_INDEX`，紧邻 `MEM_FILE`）
- 修改：`windows/launcher.py`、`macos/launcher.py`（用户数据目录）
- 测试：`tests/test_config.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_config.py`：

```python
class RecordingConfigTests(unittest.TestCase):
    def test_defaults(self):
        import config as config_module
        self.assertEqual(config_module.RECORDINGS_BITRATE, 64)
        self.assertEqual(config_module.RECORDINGS_MAX_SESSION_MIN, 240)

    def test_env_overrides(self):
        for name, value, expected in (
                ("MRRC_RECORDINGS_BITRATE", "96", 96),
                ("MRRC_RECORDINGS_MAX_SESSION_MIN", "0", 0),
                ("MRRC_RECORDINGS_MAX_SESSION_MIN", "30", 30)):
            with self.subTest(name=name, value=value):
                with patch.dict(os.environ, {name: value}):
                    importlib.reload(config)
                    attr = name.replace("MRRC_", "")
                    self.assertEqual(getattr(config, attr), expected)
        importlib.reload(config)

    def test_server_module_exposes_recording_paths(self):
        import server
        self.assertTrue(str(server.RECORDINGS_DIR).endswith("recordings"))
        self.assertEqual(server.RECORDINGS_INDEX.name, "recordings.json")

    def test_launchers_point_recordings_at_the_user_data_dir(self):
        for path in ("windows/launcher.py", "macos/launcher.py"):
            with self.subTest(launcher=path):
                source = (Path(__file__).resolve().parents[1] / path).read_text(
                    encoding="utf-8")
                self.assertIn('MRRC_RECORDINGS_DIR', source)
                self.assertIn('"recordings"', source)
```

（`tests/test_config.py` 当前**没有** `from pathlib import Path`，因此在本步骤里同时把 `from pathlib import Path` 加到 `import os` 之后——`test_launchers_point_recordings_at_the_user_data_dir` 要用它。）

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_config.py" 2>&1 | tail -5`
预期：FAIL，`AttributeError: module 'config' has no attribute 'RECORDINGS_BITRATE'`

- [ ] **步骤 3：编写最少实现代码**

`config.py`（放在 Unverified-Model Transmit Gate 段之后）：

```python
# ── Recording ───────────────────────────────────────────────────────
# 16 kHz mono MP3 written incrementally while recording (AD-017).  The
# session cap is a forgot-to-stop guard, not a retention policy: it stops
# the session and never deletes a file.
RECORDINGS_BITRATE = _env_int("MRRC_RECORDINGS_BITRATE", 64)
RECORDINGS_MAX_SESSION_MIN = _env_int("MRRC_RECORDINGS_MAX_SESSION_MIN", 240)
```

`server.py`（紧接 `MEM_FILE` 之后）：

```python
RECORDINGS_DIR = Path(_env("MRRC_RECORDINGS_DIR", str(_runtime_dir() / "recordings")))
RECORDINGS_INDEX = _runtime_dir() / "recordings.json"
```

`windows/launcher.py` 与 `macos/launcher.py`（各自紧邻 `MRRC_ATR1000_STORE` 那一行）：

```python
    env.setdefault("MRRC_RECORDINGS_DIR", str(user_data_dir() / "recordings"))
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_config.py" 2>&1 | tail -5`
预期：`OK`

- [ ] **步骤 5：Commit**

```bash
git add config.py server.py windows/launcher.py macos/launcher.py tests/test_config.py
git commit -m "feat(config): recording bitrate/session cap + per-platform recordings dir"
```

---

### 任务 5：server.py —— writer 任务与 RX/TX tap

**文件：**

- 修改：`server.py`（全局对象、writer 循环、RX tap、TX tap、生命周期）
- 测试：`tests/test_recorder_api.py`（新建，本任务先放 writer/tap 的单测）

- [ ] **步骤 1：编写失败的测试**

`tests/test_recorder_api.py`：

```python
"""Server-side wiring for the recorder (spec 2026-09-12 §4/§6).

No hardware and no HTTP client: the taps are ordinary functions, the
writer is an asyncio task driven with a fake session, and the ASGI
routes are exercised through Starlette's FileResponse directly.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import server
from recorder import RECORDING_RATE, RecordingSession


class RecordingWriterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.session = RecordingSession(self.dir, bitrate=64, max_seconds=60)

    def tearDown(self):
        self._tmp.cleanup()

    async def test_audio_blocks_are_encoded_in_order(self):
        server._rec_session = self.session
        calls = []
        real_add = self.session.add_audio

        def spy(source, pcm, rate, ts=None):
            calls.append(source)
            return real_add(source, pcm, rate, ts)

        self.session.add_audio = spy
        self.session.start(freq_hz=14_270_000)
        await server._recording_writer_loop(server._rec_queue, self.session)
        for _ in range(5):
            server._rec_enqueue("rx", np.zeros(960, dtype='<i2').tobytes(), 48000)
        server._rec_enqueue_stop()
        await asyncio.wait_for(server._rec_writer_task or asyncio.sleep(0), timeout=1)
        self.assertEqual(calls, ["rx"] * 5)
        self.assertFalse(self.session.active)

    async def test_queue_is_bounded_and_counts_drops(self):
        server._rec_session = self.session
        self.session.start()
        before = server._rec_dropped
        for _ in range(server.REC_QUEUE_MAX + 25):
            server._rec_enqueue("rx", b"\x00\x00" * 320, 48000)
        self.assertGreater(server._rec_dropped, before)
        self.assertLessEqual(server._rec_queue.qsize(), server.REC_QUEUE_MAX)


class RecorderTapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_rx_tap_enqueues_with_the_device_rate(self):
        seen = []
        with mock.patch.object(server, "_rec_enqueue",
                               side_effect=lambda *a: seen.append(a)):
            server._rec_tap_rx(b"\x00\x00" * 882, 44100)
        self.assertEqual(seen, [("rx", b"\x00\x00" * 882, 44100, None)])

    def test_rx_tap_skips_while_transmitting(self):
        seen = []
        with mock.patch.object(server, "_rec_enqueue",
                               side_effect=lambda *a: seen.append(a)), \
             mock.patch.object(server, "_recording_should_capture_rx",
                               return_value=False):
            server._rec_tap_rx(b"\x00\x00" * 882, 44100)
        self.assertEqual(seen, [])

    def test_rx_tap_never_raises_without_a_session(self):
        # The tap sits inside the audio loop: it must never raise.
        server._rec_tap_rx(b"\x00\x00" * 10, 48000)      # no active session
        server._rec_tap_tx(b"\x00\x00" * 10)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_recorder_api.py" 2>&1 | tail -5`
预期：FAIL，`AttributeError: module 'server' has no attribute '_rec_enqueue'`

- [ ] **步骤 3：编写最少实现代码**

`server.py` —— 新增全局与插件（放在 `MEM_FILE`/`RECORDINGS_DIR` 之后）：

```python
#: Bounded queue for recording blocks: a stalled encoder must not grow
#: memory without limit.  200 blocks ≈ 4 s of audio.
REC_QUEUE_MAX = 200
_rec_queue: asyncio.Queue = asyncio.Queue(maxsize=REC_QUEUE_MAX)
_rec_dropped = 0
_rec_failures = 0
_rec_writer_task: Optional[asyncio.Task] = None
_rec_session = RecordingSession(RECORDINGS_DIR,
                                bitrate=RECORDINGS_BITRATE,
                                max_seconds=RECORDINGS_MAX_SESSION_MIN * 60)


def _recording_should_capture_rx() -> bool:
    """RX is recorded only while not transmitting.

    During PTT/TUNE the radio returns sidetone/duplex audio; recording it
    would double the timeline (mrrc uses the same rule).
    """
    return not (radio is not None and radio.tx_status)


def _rec_enqueue(source: str, pcm: bytes, rate: int,
                 timestamp_ns: Optional[int] = None) -> None:
    """Queue one PCM block for the writer (never blocks, never raises)."""
    global _rec_dropped
    if not _rec_session.active:
        return
    try:
        _rec_queue.put_nowait((source, pcm, rate, timestamp_ns))
    except asyncio.QueueFull:
        _rec_dropped += 1
        if _rec_dropped % 50 == 1:
            logger.warning("Recording queue full — dropped %d block(s)", _rec_dropped)


def _rec_enqueue_stop() -> None:
    """Ask the writer to finish the session (idempotent)."""
    try:
        _rec_queue.put_nowait(("stop", None, 0, None))
    except asyncio.QueueFull:            # pragma: no cover - pathological
        logger.warning("Recording queue full on stop — finishing directly")


def _rec_tap_rx(pcm: bytes, device_rate: int) -> None:
    """RX tap: called from the audio loop with device-domain PCM."""
    if not pcm or not _recording_should_capture_rx():
        return
    _rec_enqueue("rx", pcm, device_rate)


def _rec_tap_tx(pcm: bytes) -> None:
    """TX tap: called with decoded browser-mic PCM (48 kHz)."""
    if not pcm:
        return
    _rec_enqueue("tx", pcm, TX_RATE)


async def _recording_writer_loop(queue: asyncio.Queue,
                                 session: RecordingSession) -> None:
    """Single writer: serialises every session mutation off the event loop.

    One task owns the session, so the timeline cursor and the MP3 encoder
    are never touched concurrently and encoder CPU never blocks audio.
    """
    global _rec_failures
    while True:
        source, pcm, rate, timestamp_ns = await queue.get()
        if source == "stop":
            return
        try:
            await asyncio.to_thread(session.add_audio, source, pcm, rate,
                                    timestamp_ns)
        except Exception as e:
            _rec_failures += 1
            if _rec_failures in (1, 10, 100):
                logger.warning("Recording block failed (%d so far): %s",
                               _rec_failures, e)
```

`TX_RATE` 是 `opus_rx` 已有的常量（48000，Opus 解码输出率）——把 server.py 的 `from opus_rx import (...)` 那一行补上它，不要新建常量：

```python
from opus_rx import (
    RxOpusEncoder, TxOpusDecoder,
    AUDIO_TAG_PCM, AUDIO_TAG_OPUS, DEFAULT_BITRATE,
    TX_RATE,
)
```

RX tap 接入 `_audio_rx_loop`（把 idle-skip 条件与读取之后各改一处）：

```python
                # ── Idle: no clients and not recording → skip PCM read ──
                # PCM read + resample + Opus encode is the #1 CPU
                # consumer when idle.  A recording still needs the audio,
                # so it keeps the loop awake.
                if not audio_rx_clients and not _rec_session.active:
                    ...（原样保留 idle 分支）
                    continue

                pcm = audio.read_rx_chunk()
                audio.note_rx_chunk(pcm)
                _rec_tap_rx(pcm, audio._rx_dev_rate) if pcm else None
```

TX tap 接入 `ws_audio_tx` 的两处解码之后（Opus 与 PCM 分支）：

```python
                if tag == AUDIO_TAG_OPUS and _opus_tx_decoder:
                    pcm = _opus_tx_decoder.decode(frame)
                    if pcm and audio:
                        _rec_tap_tx(pcm)
                        audio.feed_tx_audio(pcm)
```

```python
                elif tag == AUDIO_TAG_PCM:
                    ...
                        _rec_tap_tx(pcm)
                        audio.feed_tx_audio(pcm)
```

生命周期：在 `_audio_rx_task` 创建处附近启动 writer，在 shutdown 段取消并收尾：

```python
    _rec_writer_task = asyncio.create_task(
        _recording_writer_loop(_rec_queue, _rec_session), name="rec_writer")
```

```python
        # Shutdown: finish the active recording without flushing so the
        # partial file stays playable, then stop the writer.
        _rec_enqueue_stop()
        if _rec_writer_task:
            try:
                await asyncio.wait_for(_rec_writer_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                _rec_writer_task.cancel()
        _rec_session.close_without_finishing()
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_recorder_api.py" 2>&1 | tail -5`
运行：`.venv/bin/python -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)"`
预期：新测试 OK，全量无回归

- [ ] **步骤 5：Commit**

```bash
git add server.py tests/test_recorder_api.py
git commit -m "feat(server): recording writer task + RX/TX taps (idle-skip keeps recording alive)"
```

---

### 任务 6：WS 控制（`recording` 命令、`recordingState` 广播、`fullState` 字段）

**文件：**

- 修改：`server.py`
- 测试：`tests/test_server_ws_protocol.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_server_ws_protocol.py`：

```python
class RecordingControlTests(unittest.IsolatedAsyncioTestCase):
    """REC is a server-side session (spec 2026-09-12 §6)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        import server as server_module
        self.server = server_module
        self._saved = server_module._rec_session
        self.session = server_module.RecordingSession(
            Path(self._tmp.name), bitrate=64, max_seconds=60)
        server_module._rec_session = self.session

    def tearDown(self):
        self.server._rec_session = self._saved
        self._tmp.cleanup()

    async def test_start_and_stop_over_the_control_channel(self):
        ws = _fake_ws()
        await self.server._execute_set_command("recording", True, ws)
        self.assertTrue(self.session.active)
        await self.server._execute_set_command("recording", False, ws)
        self.assertFalse(self.session.active)
        self.assertEqual(ws.errors(), [])

    async def test_recording_works_without_a_connected_radio(self):
        # Recording depends on USB audio, not CAT (deliberate: the radio
        # guard at the top of _execute_set_command must not block it).
        ws = _fake_ws()
        with mock.patch.object(self.server, "cat", None):
            await self.server._execute_set_command("recording", True, ws)
        self.assertTrue(self.session.active)
        self.assertEqual(ws.errors(), [])

    async def test_second_start_is_reported_not_silent(self):
        ws = _fake_ws()
        await self.server._execute_set_command("recording", True, ws)
        await self.server._execute_set_command("recording", True, ws)
        self.assertTrue(any("already" in m.lower() for m in ws.errors()))

    async def test_state_message_carries_the_recording_fields(self):
        self.session.start(freq_hz=7_050_000)
        msg = self.server._full_state_message({"vfo_a_freq": 7_050_000}, [])
        self.assertIn("recording", msg)
        self.assertTrue(msg["recording"]["recording"])
        self.assertEqual(msg["recording"]["freq_hz"], 7_050_000)


class RecordingSetRoutingTests(unittest.TestCase):
    def test_recording_branch_precedes_the_radio_guard(self):
        import inspect
        source = inspect.getsource(server._execute_set_command)
        self.assertIn('field == "recording"', source)
        self.assertLess(source.index('field == "recording"'),
                        source.index("Radio not connected"))
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_server_ws_protocol.py" 2>&1 | tail -5`
预期：FAIL，`AssertionError: False is not true`（会话未启动）或 `KeyError: 'recording'`

- [ ] **步骤 3：编写最少实现代码**

`_execute_set_command` **开头**（在 `cat is None` 守卫之前）插入：

```python
    # Recording is a server-side session over USB audio, not a radio
    # command: handle it before the "radio not connected" guard so a
    # working sound card with a broken CAT link can still record.
    if field == "recording":
        on = value is True or str(value).lower() == "true"
        if on:
            if not _rec_session.start(freq_hz=radio.active_freq if radio else 0):
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": "Recording already in progress",
                }))
            else:
                logger.info("Recording started by client")
        else:
            _rec_enqueue_stop()
        await _broadcast_recording_state()
        return
```

新增广播函数与节流 tick（放在 `_broadcast_mem_channels` 附近）：

```python
async def _broadcast_recording_state() -> None:
    """Push the recording session snapshot to every control client."""
    if not ctrl_clients:
        return
    payload = json.dumps({"type": "recordingState",
                          "recording": _rec_session.status()})
    dead = set()
    for ws in list(ctrl_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    ctrl_clients -= dead
```

（`ctrl_clients` 就是 `_broadcast_state` 用的那一个集合，`server.py:81` 定义。）

`_full_state_message` 增加一个字段：

```python
        "recording": _rec_session.status(),
```

RX 音频循环里的 1 Hz 刷新（`_loop_count % 50 == 0` 处）：

```python
            if _loop_count % 50 == 0 and _rec_session.active:
                await _broadcast_recording_state()
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_server_ws_protocol.py" 2>&1 | tail -5`
预期：`OK`

- [ ] **步骤 5：Commit**

```bash
git add server.py tests/test_server_ws_protocol.py
git commit -m "feat(server): recording set command + recordingState broadcast"
```

---

### 任务 7：REST 路由（列表 / Range 流 / 删除）

**文件：**

- 修改：`server.py`
- 测试：`tests/test_recorder_api.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_recorder_api.py`：

```python
class RecordingsRestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._saved_dir = server.RECORDINGS_DIR
        self._saved_index = server.RECORDINGS_INDEX
        server.RECORDINGS_DIR = self.dir
        server.RECORDINGS_INDEX = self.dir.parent / f"{self.dir.name}-index.json"
        self.name = "14270kHz_20260912_210405.mp3"
        (self.dir / self.name).write_bytes(b"ID3" + b"x" * 8000)

    def tearDown(self):
        server.RECORDINGS_DIR = self._saved_dir
        server.RECORDINGS_INDEX = self._saved_index
        self._tmp.cleanup()

    def test_path_helper_accepts_our_names_only(self):
        self.assertEqual(server._recording_path(self.name),
                         (self.dir / self.name).resolve())
        for bad in ("../server.py", "/etc/passwd", "x.mp3",
                    "14270kHz_20260912_210405.mp3.bak", ""):
            with self.subTest(name=bad):
                self.assertIsNone(server._recording_path(bad))

    def test_listing_endpoint_shape(self):
        payload = server._recordings_payload()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["recordings"][0]["name"], self.name)
        self.assertEqual(payload["recordings"][0]["freq_hz"], 14_270_000)
        self.assertGreaterEqual(payload["total_bytes"], 8000)

    def test_delete_endpoint_removes_the_file_and_updates_the_index(self):
        import asyncio
        ok = asyncio.run(server._delete_recording(self.name))
        self.assertTrue(ok)
        self.assertFalse((self.dir / self.name).exists())
        self.assertEqual(server._recordings_payload()["count"], 0)

    def test_delete_refuses_a_missing_file(self):
        import asyncio
        self.assertFalse(asyncio.run(server._delete_recording("07050kHz_20260912_210405.mp3")))

    def test_delete_refuses_the_active_recording(self):
        import asyncio
        session = RecordingSession(self.dir, bitrate=64, max_seconds=60)
        saved = server._rec_session
        server._rec_session = session
        try:
            session.start(freq_hz=14_270_000)
            active_name = session.status()["name"]
            self.assertFalse(asyncio.run(server._delete_recording(active_name)))
        finally:
            session.close_without_finishing()
            server._rec_session = saved

    def test_audio_stream_supports_range_requests(self):
        import asyncio
        captured = {}

        async def send(message):
            captured.setdefault("messages", []).append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        response = server._recording_response(self.name)
        self.assertIsNotNone(response)
        scope = {"type": "http", "method": "GET",
                 "path": f"/api/recordings/{self.name}",
                 "headers": [(b"range", b"bytes=0-99")]}
        asyncio.run(response(scope, receive, send))
        start = captured["messages"][0]
        self.assertEqual(start["status"], 206)
        headers = dict(start["headers"])
        self.assertIn(b"content-range", {k.lower(): v for k, v in headers.items()})
        self.assertEqual(dict(headers)[b"accept-ranges"], b"bytes")
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_recorder_api.py" 2>&1 | tail -5`
预期：FAIL，`AttributeError: module 'server' has no attribute '_recording_path'`

- [ ] **步骤 3：编写最少实现代码**

`server.py`（放在 `/api/mem_channels` 路由附近）：

```python
def _recording_path(name: str) -> Optional[Path]:
    """Resolve a recording name to a path inside RECORDINGS_DIR, or None.

    The name must be one this server would generate (see
    recorder.parse_recording_name) and the resolved path must stay inside
    the recordings directory — the I8 lesson applied here.
    """
    if parse_recording_name(name) is None:
        return None
    try:
        resolved = (RECORDINGS_DIR / name).resolve()
        if not resolved.is_relative_to(RECORDINGS_DIR.resolve()):
            return None
    except (OSError, ValueError):
        return None
    return resolved


def _recordings_payload() -> dict:
    """List recordings with the index merged in (spec §6)."""
    rows = list_recordings(RECORDINGS_DIR, load_index(RECORDINGS_INDEX),
                           RECORDINGS_BITRATE)
    active = _rec_session.status()["name"] if _rec_session.active else None
    for row in rows:
        row["recording"] = row["name"] == active
    return {"recordings": rows, "count": len(rows),
            "total_bytes": sum(r["bytes"] for r in rows)}


async def _delete_recording(name: str) -> bool:
    """Delete one recording; False when unknown or currently recording."""
    path = _recording_path(name)
    if path is None or not path.is_file():
        return False
    if _rec_session.active and _rec_session.status()["name"] == name:
        return False
    try:
        path.unlink()
    except OSError as e:
        logger.warning("Cannot delete recording %s: %s", name, e)
        return False
    index = load_index(RECORDINGS_INDEX)
    if index.pop(name, None) is not None:
        save_index(RECORDINGS_INDEX, index)
    logger.info("Recording deleted: %s", name)
    await _broadcast_recording_state()
    return True


def _recording_response(name: str):
    """Range-capable FileResponse for one recording (None when invalid)."""
    path = _recording_path(name)
    if path is None or not path.is_file():
        return None
    return FileResponse(path, media_type="audio/mpeg", filename=name)


@app.get("/api/recordings", include_in_schema=False)
async def api_recordings():
    """List recordings (auth enforced by auth_middleware)."""
    return JSONResponse(_recordings_payload())


@app.get("/api/recordings/{name}", include_in_schema=False)
async def api_recording_file(name: str):
    """Stream one recording; Starlette's FileResponse handles Range/seek."""
    response = _recording_response(name)
    if response is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return response


@app.delete("/api/recordings/{name}", include_in_schema=False)
async def api_recording_delete(name: str):
    """Delete one recording (409 while it is the active recording)."""
    if _rec_session.active and _rec_session.status()["name"] == name:
        return JSONResponse({"error": "recording in progress"}, status_code=409)
    if not await _delete_recording(name):
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True})
```

顶部 import 增补：`from fastapi.responses import FileResponse`（若尚未导入）、`from recorder import (RecordingSession, list_recordings, load_index, parse_recording_name, save_index)`。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_recorder_api.py" 2>&1 | tail -5`
预期：`OK`（含 206 + `content-range` 断言）

- [ ] **步骤 5：Commit**

```bash
git add server.py tests/test_recorder_api.py
git commit -m "feat(server): recordings REST routes (list, Range stream, delete) with path containment"
```

---

### 任务 8：前端 —— REC 按钮与录音面板

**文件：**

- 修改：`static/index.html`（菜单项）、`static/ft710_ui.js`（按钮/面板/渲染/菜单分发）
- 修改：`static/ft710_main.js`（`recordingState` 落状态）
- 测试：`tests/test_server_ws_protocol.py`（前端契约）

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_server_ws_protocol.py`：

```python
class RecordingsPanelContractTests(unittest.TestCase):
    def test_menu_has_a_recordings_entry(self):
        html = Path("static/index.html").read_text(encoding="utf-8")
        self.assertIn('data-action="recordings"', html)

    def test_ui_exposes_the_panel_and_reads_server_state(self):
        js = Path("static/ft710_ui.js").read_text(encoding="utf-8")
        self.assertIn("function showRecordingsPanel(", js)
        self.assertIn("case 'recordings':", js)
        self.assertIn("/api/recordings", js)
        self.assertIn("sendCommand('recording'", js)
        # The button state comes from the server, not a local recorder.
        self.assertIn("radioState.recording", js)
        self.assertNotIn("window.RXRecorder", js)

    def test_main_stores_the_recording_state_message(self):
        js = Path("static/ft710_main.js").read_text(encoding="utf-8")
        self.assertIn('"recordingState"', js)
        self.assertNotIn("window.RXRecorder", js)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_server_ws_protocol.py" 2>&1 | tail -4`
预期：FAIL，`AssertionError: 'data-action="recordings"' not found`

- [ ] **步骤 3：编写最少实现代码**

`static/index.html`：在 `data-action="memory-manage"` 那一项之后插入

```html
          <li><a href="#" class="menu-item" data-action="recordings">录音 Recordings</a></li>
```

`static/ft710_main.js` —— `handleMessage` 里新增分支（放在 `memChannels` 分支旁）：

```js
  case "recordingState":
   radioState.recording = msg.recording || {recording: false};
   if (typeof renderRecordingState === "function") renderRecordingState();
   if (typeof refreshRecordingsPanel === "function") refreshRecordingsPanel();
   break;
```

`static/ft710_ui.js`：

```js
// ── REC button: server-side session (spec 2026-09-12 §7) ─────────────
// The button reflects the server's session state, which is broadcast to
// every client, so two browsers always agree.
function renderRecordingState() {
    const recordBtn = document.getElementById('btn-record');
    if (!recordBtn) return;
    const rec = (radioState && radioState.recording) || {recording: false};
    recordBtn.disabled = false;
    recordBtn.classList.toggle('record-active', !!rec.recording);
    recordBtn.textContent = rec.recording ? 'STOP' : 'REC';
    recordBtn.title = rec.recording
        ? '服务端录制中（RX+TX，MP3 16 kHz）· 已录 ' + _formatDuration(rec.duration || 0)
        : '服务端录制：点击开始，录音文件保存在服务端并可回放/下载';
}

function _formatDuration(seconds) {
    const total = Math.max(0, Math.floor(seconds || 0));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const mm = String(m).padStart(2, '0');
    const ss = String(s).padStart(2, '0');
    return h > 0 ? h + ':' + mm + ':' + ss : mm + ':' + ss;
}

function _formatBytes(bytes) {
    const n = Number(bytes) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1024 * 1024 * 1024) return (n / 1048576).toFixed(1) + ' MB';
    return (n / 1073741824).toFixed(2) + ' GB';
}
```

REC 按钮点击（替换 `ft710_ui.js` 里 `if (window.RXRecorder) {...}` 那段）：

```js
        recordBtn.addEventListener('click', function() {
            const rec = (radioState && radioState.recording) || {recording: false};
            sendCommand('recording', !rec.recording);
        });
```

菜单分发（`handleMenuAction`）新增：

```js
        case 'recordings':
            showRecordingsPanel();
            break;
```

录音面板（放在 `showMemoryManager` 之后；全部 `createElement`，无 innerHTML）：

```js
// ── Recordings panel: list / play / download / delete ────────────────
// Server-side recordings (spec 2026-09-12 §7).  The audio element is a
// single instance whose src is set on demand; Starlette serves Range, so
// seeking inside a long QSO works.
var _recordingsAudio = null;

function showRecordingsPanel() {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    const content = document.createElement('div');
    content.className = 'modal-content';
    content.id = 'recordings-content';

    const title = document.createElement('div');
    title.className = 'modal-title';
    title.textContent = '录音 Recordings';
    content.appendChild(title);

    const summary = document.createElement('div');
    summary.id = 'recordings-summary';
    summary.style.cssText = 'font-size:12px;color:#9ca3af;padding:0 4px 8px;';
    summary.textContent = '加载中…';
    content.appendChild(summary);

    const list = document.createElement('div');
    list.id = 'recordings-list';
    content.appendChild(list);

    if (!_recordingsAudio) {
        _recordingsAudio = document.createElement('audio');
        _recordingsAudio.controls = true;
        _recordingsAudio.preload = 'none';
        _recordingsAudio.id = 'recordings-player';
        _recordingsAudio.style.cssText = 'width:100%;margin:8px 0;';
    }
    content.appendChild(_recordingsAudio);

    const refresh = document.createElement('button');
    refresh.className = 'modal-close';
    refresh.textContent = '刷新 Refresh';
    refresh.style.marginRight = '8px';
    refresh.addEventListener('click', refreshRecordingsPanel);
    const close = document.createElement('button');
    close.className = 'modal-close';
    close.textContent = '关闭 Close';
    close.addEventListener('click', function() { overlay.remove(); });
    content.append(refresh, close);

    overlay.appendChild(content);
    document.body.appendChild(overlay);
    refreshRecordingsPanel();
}

function refreshRecordingsPanel() {
    const list = document.getElementById('recordings-list');
    if (!list) return;
    const summary = document.getElementById('recordings-summary');
    fetch('/api/recordings')
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(data) {
            if (!data) return;
            if (summary) {
                summary.textContent = data.count + ' 条 · 共 ' +
                    _formatBytes(data.total_bytes);
            }
            list.replaceChildren();
            if (!data.recordings.length) {
                const empty = document.createElement('div');
                empty.style.cssText = 'font-size:12px;color:#9ca3af;padding:8px 4px;';
                empty.textContent = '暂无录音';
                list.appendChild(empty);
                return;
            }
            data.recordings.forEach(function(rec) {
                list.appendChild(_recordingsRow(rec));
            });
        })
        .catch(function() {});
}

function _recordingsRow(rec) {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;align-items:center;' +
        'justify-content:space-between;padding:8px;border-bottom:1px solid #444;';

    const info = document.createElement('div');
    info.style.cssText = 'font-size:12px;line-height:1.5;flex:1 1 55%;';
    const head = document.createElement('div');
    const khz = Math.round((rec.freq_hz || 0) / 1000);
    head.textContent = (khz > 0 ? khz + ' kHz' : '—') + ' · ' +
        (rec.started_at || '').replace('T', ' ') + (rec.recording ? ' · 录制中' : '');
    head.style.color = rec.recording ? '#ef4444' : '#f59e0b';
    const meta = document.createElement('div');
    meta.style.color = '#9ca3af';
    meta.textContent = _formatDuration(rec.duration) + ' · ' + _formatBytes(rec.bytes);
    info.append(head, meta);

    const actions = document.createElement('div');
    actions.style.cssText = 'display:flex;gap:6px;';
    const play = document.createElement('button');
    play.textContent = '播放';
    play.style.cssText = 'background:#0ea5e9;color:#fff;border:none;border-radius:4px;padding:4px 8px;';
    play.addEventListener('click', function() {
        if (_recordingsAudio) {
            _recordingsAudio.src = '/api/recordings/' + encodeURIComponent(rec.name);
            _recordingsAudio.play().catch(function() {});
        }
    });
    const dl = document.createElement('a');
    dl.textContent = '下载';
    dl.href = '/api/recordings/' + encodeURIComponent(rec.name);
    dl.setAttribute('download', rec.name);
    dl.style.cssText = 'color:#0ea5e9;font-size:12px;align-self:center;';
    const del = document.createElement('button');
    del.textContent = '删除';
    del.style.cssText = 'background:#ef4444;color:#fff;border:none;border-radius:4px;padding:4px 8px;';
    if (rec.recording) {
        del.disabled = true;
        del.style.opacity = '0.4';
    } else {
        del.addEventListener('click', function() {
            if (!window.confirm('删除录音 ' + rec.name + ' ?')) return;
            fetch('/api/recordings/' + encodeURIComponent(rec.name),
                  {method: 'DELETE'})
                .then(function(r) {
                    if (!r.ok && typeof showToast === 'function') {
                        showToast(r.status === 409 ? '该录音正在录制中' : '删除失败');
                    }
                    refreshRecordingsPanel();
                })
                .catch(function() {});
        });
    }
    actions.append(play, dl, del);
    row.append(info, actions);
    return row;
}
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_server_ws_protocol.py" 2>&1 | tail -4`
运行：`node --check static/ft710_ui.js && node --check static/ft710_main.js && echo JS_OK`
预期：`OK` / `JS_OK`

- [ ] **步骤 5：Commit**

```bash
git add static/index.html static/ft710_ui.js static/ft710_main.js tests/test_server_ws_protocol.py
git commit -m "feat(ui): REC drives the server session + recordings panel (list/play/download/delete)"
```

---

### 任务 9：前端 —— 移除浏览器录音与 lame.js（含 cache-bust）

**文件：**

- 修改：`static/ft710_main.js`、`static/ft710_ui.js`、`static/index.html`、`static/sw.js`
- 删除：`static/modules/lame.js`
- 测试：`tests/test_audio.py`、`tests/test_server_ws_protocol.py`

- [ ] **步骤 1：编写失败的测试**

改写 `tests/test_audio.py` 里的 4 条录音断言（`RxRecordingTests`）：

```python
class ServerSideRecordingContractTests(unittest.TestCase):
    """V2.42: recording moved to the server; the browser must not encode."""

    def test_record_button_sits_next_to_tune(self):
        html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
        footer_start = html.index('class="ptt-side"')
        ptt_footer = html[footer_start:footer_start + 400]
        self.assertIn('id="btn-record"', ptt_footer)
        self.assertLess(ptt_footer.index('id="btn-tune"'),
                        ptt_footer.index('id="btn-record"'))

    def test_browser_recorder_is_gone(self):
        main = (REPO_ROOT / "static" / "ft710_main.js").read_text(encoding="utf-8")
        self.assertNotIn("window.RXRecorder", main)
        self.assertNotIn("lamejs", main)
        self.assertNotIn("feedRXRecorderFrame", main)
        self.assertNotIn("/modules/lame.js", main)

    def test_lamejs_asset_is_deleted(self):
        self.assertFalse((REPO_ROOT / "static" / "modules" / "lame.js").exists())

    def test_rec_button_talks_to_the_server(self):
        ui = (REPO_ROOT / "static" / "ft710_ui.js").read_text(encoding="utf-8")
        self.assertIn("sendCommand('recording'", ui)
```

同时把 `tests/test_server_ws_protocol.py` 的 cache-bust 钉住断言改成（main v29 / ui v31 / sw `mrrc-v32`）：

```python
        self.assertIn('/ft710_main.js?v=29', index_source)
        self.assertIn('/ft710_ui.js?v=31', index_source)
        self.assertIn("const CACHE = 'mrrc-v32'", sw_source)
        self.assertIn("'/ft710_main.js?v=29'", sw_source)
        self.assertIn("'/ft710_ui.js?v=31'", sw_source)
```

并把 `tests/test_audio.py:151` 的 `mrrc-v31` 改为 `mrrc-v32`。

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_audio.py" 2>&1 | tail -4`
预期：FAIL（`window.RXRecorder` 仍在）

- [ ] **步骤 3：编写最少实现代码**

`static/ft710_main.js`：删除 `RX_MP3_BITRATE`、`RX_RECORDER_MIME`、`RX_RECORDER_EXT`、`_loadLame`、`_f32ToInt16`、`_downloadRecording`、`window.RXRecorder` 整块、`feedRXRecorderFrame`、`feedTXRecorderFrame` 及其三处调用（TX 采集路径里的 `feedTXRecorderFrame(...)` 行，保留其余逻辑）；`decodeRxAudioFrame` 调用点只保留播放/入队路径。

`static/ft710_ui.js`：删除对 `window.RXRecorder` 的其余引用（本任务后应零命中）。

`static/index.html`：`/ft710_main.js?v=28` → `v=29`；`/ft710_ui.js?v=30` → `v=31`。

`static/sw.js`：`const CACHE = 'mrrc-v31'` → `'mrrc-v32'`；两处资源串同步为 `v=29`/`v=31`。

```bash
git rm static/modules/lame.js
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_audio.py" 2>&1 | tail -4`
运行：`.venv/bin/python -m unittest discover -s tests -p "test_server_ws_protocol.py" 2>&1 | tail -4`
运行：`grep -rn "RXRecorder\|lamejs\|lame.js" static/ tests/ | grep -v Binary || echo CLEAN`
预期：`OK` / `OK` / `CLEAN`

- [ ] **步骤 5：Commit**

```bash
git add -A static tests/test_audio.py tests/test_server_ws_protocol.py
git commit -m "refactor(ui): delete the browser recorder and lame.js (530 KB) + cache-bust"
```

---

### 任务 10：依赖与打包

**文件：**

- 修改：`requirements.txt`、`packaging/pyinstaller/mrrc_modern_server.spec`
- 测试：`tests/test_windows_packaging_files.py`

- [ ] **步骤 1：编写失败的测试**

追加到 `tests/test_windows_packaging_files.py`：

```python
class RecordingDependencyPackagingTests(unittest.TestCase):
    def test_lameenc_is_pinned_in_requirements(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("lameenc", text)

    def test_pyinstaller_collects_the_lameenc_extension(self):
        spec = (ROOT / "packaging" / "pyinstaller"
                / "mrrc_modern_server.spec").read_text(encoding="utf-8")
        self.assertIn("lameenc", spec)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_windows_packaging_files.py" 2>&1 | tail -4`
预期：FAIL

- [ ] **步骤 3：编写最少实现代码**

`requirements.txt`（在 Numeric Processing 段之后新增一段）：

```
# ── Recording (server-side QSO MP3) ───────────────────────────────────────
# Bundles libmp3lame as a prebuilt wheel (no ffmpeg dependency):
# macOS arm64/x86_64, Windows x64, Linux aarch64 (Raspberry Pi image).
lameenc>=1.8.0
```

`packaging/pyinstaller/mrrc_modern_server.spec`（hiddenimports 之后，新增 hiddenimports 条目 + 动态收集）：

```python
    hiddenimports=[
        ...现有条目...,
        "lameenc",
        "lameenc._lameenc",
    ],
```

- [ ] **步骤 4：运行测试验证通过 + 本机冻结自检**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_windows_packaging_files.py" 2>&1 | tail -4`
运行：`.venv/bin/python -c "import lameenc, lameenc._lameenc; print('lameenc OK')"`
预期：`OK` / `lameenc OK`

- [ ] **步骤 5：Commit**

```bash
git add requirements.txt packaging/pyinstaller/mrrc_modern_server.spec tests/test_windows_packaging_files.py
git commit -m "build: add lameenc dependency + PyInstaller collection"
```

---

### 任务 11：文档同步

**文件：**

- 修改：`SDD/05`、`SDD/07`、`SDD/08`、`SDD/09`、`SDD/10`、`SDD/11`、`SDD/12`、`SDD/13`、`SDD/14`、`SDD/README.md`
- 修改：`AGENTS.md`、`README.md`、`tests/README.md`、`CHANGELOG.md`
- 修改：`docs/OPERATION_GUIDE.md`、`docs/RASPBERRY_PI_GUIDE.md`
- 修改：`website/guide.html`、`website/zh/guide.html`、`.agents/skills/sdd-guardian/harness/index.json`

- [ ] **步骤 1：取得准确计数**

运行：`.venv/bin/python -m unittest discover -s tests 2>&1 | grep -E "^Ran"`（记录 N）
运行：`ls tests/test_*.py | wc -l`（记录模块数）

- [ ] **步骤 2：SDD 逐章**

- `SDD/08`：新增 **AD-017**（标题/类型/状态/决策/问题/理由/后果），决策文本必须写清"16 kHz 是**存储域**、由 44.1→48（`audio_resample`）→48→16（FIR 抽取）到达、且是**只写汇点**，不回灌 codec/device 域"；`8.16 Decision Summary` 表加一行。
- `SDD/05`：新增 **NFR-066**（录音：16 kHz mono MP3 ≤64 kbps、时长=时间轴长度±50 ms、占用可在面板观测、单会话上限可配）；NFR-041 追加三个 env。
- `SDD/07`：`§7.2` 实体表加 `RecordingSession`、`RecordingFile`。
- `SDD/09`：音频链路（RX/TX 两处）加"recording tap"，注明录制不参与播放路径。
- `SDD/10`/`SDD/11`：新增 Recorder 服务/组件行（`recorder.py` + `server.py` 的 writer 任务）。
- `SDD/12`：接口表加三条 REST 路由；运维表加 `MRRC_RECORDINGS_DIR` 及其默认值/平台位置。
- `SDD/13`：新增 **R10**（磁盘增长 —— **刻意接受**，缓解=面板总占用 + 手动删除）与 **A8**（`lameenc` 各平台预编译 wheel 可用，打包时验证）。
- `SDD/14`：新条目 V2.42（含：颤抖根因、增量编码、16 kHz、lameenc、面板、无所有权、无自动清理、测试计数、**硬件边界**：真机 RX tap 质量与冻结包内 lameenc 未验证）。
- `SDD/README.md`：Quick Facts 版本号与状态行。

- [ ] **步骤 3：仓库内文档**

- `AGENTS.md`：模块表加 `recorder.py`（一句话职责 + "16 kHz 存储域，见 AD-017"）；env 表加三个 `MRRC_RECORDINGS_*`；`server.py` 行的 WS/REST 面补 `recording`/`recordingState`/三条路由。
- `README.md`：功能段落把"浏览器录制并下载"改为"服务端录制 + 面板回放/下载/删除"，env 表加三行。
- `tests/README.md`：计数与新增模块说明。
- `CHANGELOG.md`：Unreleased 段新增录音条目。
- `docs/OPERATION_GUIDE.md`：录音操作（REC/面板/保留策略/磁盘位置）。
- `docs/RASPBERRY_PI_GUIDE.md`：录音落盘位置（`/opt/mrrc_modern/recordings`）与 SD 卡容量提示。
- `website/guide.html` + `website/zh/guide.html`：把"停止时浏览器自动下载…500KB MP3 编码器"那段改为服务端录制与面板说明（保持中英一致）。
- `.agents/skills/sdd-guardian/harness/index.json`：新增 `recorder.py` / 录音主题的知识路由（AD-017、NFR-066、R10、§9.3、`audio-16k-rate` 的作用域说明）。

- [ ] **步骤 4：重新生成网站 SDD 页面**

运行：`python3 website/build_sdd.py`
预期：`website/sdd/*.html` 更新（含新的 V2.42 条目与 AD-017）

- [ ] **步骤 5：Commit**

```bash
git add SDD/ AGENTS.md README.md tests/README.md CHANGELOG.md docs/OPERATION_GUIDE.md docs/RASPBERRY_PI_GUIDE.md website/ .agents/skills/sdd-guardian/harness/index.json
git commit -m "docs(sdd): V2.42 — server-side recording (AD-017, NFR-066, R10/A8)"
```

---

### 任务 12：最终验证

- [ ] **步骤 1：全量测试**

运行：`.venv/bin/python -m unittest discover -s tests 2>&1 | grep -E "^(Ran|OK|FAILED)"`
预期：`OK`，计数与 `tests/README.md` 一致

- [ ] **步骤 2：语法与静态检查**

运行：`python3 -m py_compile recorder.py server.py config.py windows/launcher.py macos/launcher.py && echo COMPILE_OK`
运行：`node --check static/ft710_main.js && node --check static/ft710_ui.js && node --check static/sw.js && echo JS_OK`
运行：`git diff --check && echo DIFF_CLEAN`

- [ ] **步骤 3：SDD Guardian**

运行：`python3 .agents/skills/sdd-guardian/harness/sdd_context.py check recorder.py server.py config.py`
预期：clean（`audio-16k-rate` 不应命中 —— 它只作用于四个音频通路文件；若命中，说明放错了文件，必须修而不是加豁免）

- [ ] **步骤 4：端到端自检（无硬件，用合成音频）**

运行：

```bash
.venv/bin/python - <<'PY'
import tempfile, time
from pathlib import Path
import numpy as np
from recorder import RecordingSession, list_recordings

d = Path(tempfile.mkdtemp())
s = RecordingSession(d, bitrate=64, max_seconds=60)
s.start(freq_hz=14_270_000)
tone = (np.sin(2*np.pi*1000*np.arange(960)/48000) * 12000).astype('<i2').tobytes()
base = time.monotonic_ns()
for i in range(50):                      # 1 s of tone with a 0.5 s hole
    ts = base + (i * 20_000_000 if i < 25 else 500_000_000 + i * 20_000_000)
    s.add_audio("rx", tone, 48000, ts)
info = s.stop()
print(f"{info.name} duration={info.duration:.2f}s bytes={info.bytes}")
print("listing:", [(r["name"], round(r["duration"], 2)) for r in list_recordings(d, {}, 64)])
raw = (d / info.name).read_bytes()
print("starts with an MP3 frame sync:", raw[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xfa', b'ID3'))
PY
```

预期：打印文件名、`duration≈1.5`、`bytes > 0`，且 `starts with an MP3 frame sync: True`

- [ ] **步骤 5：硬件边界声明**

在最终报告中明确：**未验证**项 = 真机 RX tap 的音频连续性（依赖声卡/`restart_rx()` 路径）、TX 侧实测音质、mac DMG 与 Win 安装包内 `lameenc` 的加载与录制、树莓派 SD 卡上的长时间录制。这些必须在对应平台上各录一段实测（Mac 本地可做，Windows 需 KVM VM，真机需电台）。

- [ ] **步骤 6：最终 Commit（若步骤 1–5 产生修正）**

```bash
git add -A
git commit -m "test(recorder): final verification adjustments"
```

---

## 自检记录

**1. 规格覆盖度：**

| 规格章节 | 覆盖任务 |
| --- | --- |
| §2 D1（面板/列表/播放/下载/删除/多客户端可见） | 任务 6（广播）、7（REST）、8（面板） |
| §2 D2（lameenc，无 ffmpeg） | 任务 3、10 |
| §2 D3（16 kHz + mrrc 命名） | 任务 1、2、3 |
| §2 D4（全部保留，仅手动删除） | 任务 3（无自动删除）、7（DELETE 路由）、11（R10 docs） |
| §2 D5（增量编码落盘） | 任务 3（`_encode`/`flush`/崩溃安全测试） |
| §2 D6（WS 控制 + REST 文件面） | 任务 6、7 |
| §2 D7（无所有权） | 任务 6（任何客户端可启停；无 owner 逻辑） |
| §2 D8（不自动录制） | 未实现任何自动触发 ✓ |
| §2 D9（无后处理） | 任务 3（无归一化/裁剪代码） |
| §3 颤抖根因 | 任务 1–3（时间戳、容差、空洞填充） |
| §4 数据流与 tap 点 / idle-skip | 任务 5（含 idle-skip 回归测试） |
| §4 线程模型 / 失败隔离 | 任务 5（单 writer + `to_thread` + 失败计数） |
| §5 会话上限 | 任务 3（`max_seconds`）、4（env） |
| §5 索引与外部文件 | 任务 2（`list_recordings` 合并磁盘与索引） |
| §6 WS 协议（set + recordingState + fullState + 1 Hz） | 任务 6 |
| §6 REST 三路由 + Range + 409 | 任务 7 |
| §6 路径安全（白名单 + containment） | 任务 7（`_recording_path` + 测试） |
| §7 前端（按钮/面板/移除/缓存版本） | 任务 8、9 |
| §8 依赖与打包（requirements/spec/launcher） | 任务 4（launcher）、10（requirements/spec） |
| §9 测试计划 | 任务 1–4、5–7、8–10 的测试 |
| §10 文档同步 | 任务 11 |
| §11 已接受风险与边界 | 任务 11（R10/A8）、12（边界声明） |
| §12 非目标 | 未引入 ffmpeg/转写/新 WS 端点/音频域改动 ✓ |
| §13 SDD 追溯 | 任务 11 |

**2. 占位符扫描：** 每个代码步骤都给出完整实现；无 "TODO"、"待定"、"类似任务 N"。唯一省略处是任务 5 里"（原样保留 idle 分支）"——那是**保留**既有的 5 行日志代码，不是待填内容；实现时不得改动那 5 行。

**3. 类型一致性：**

- `RecordingSession`：任务 3 定义 `start(freq_hz, now)`/`add_audio(source, pcm, source_rate, timestamp_ns)`/`stop(now_ns)`/`close_without_finishing()`/`status(now_ns)`/`active`/`max_samples`；任务 4–7 只使用这些名字。
- `RecordingInfo`：`name/freq_hz/started_at/duration/bytes`，任务 3 定义、任务 7 的索引与响应只读这些字段。
- 模块级函数 `recording_name`/`parse_recording_name`/`load_index`/`save_index`/`list_recordings`：任务 2 定义，任务 7 使用一致。
- `server.py` 全局：`_rec_session`/`_rec_queue`/`_rec_writer_task`/`_rec_dropped`/`_rec_failures`/`REC_QUEUE_MAX`/`_rec_enqueue`/`_rec_enqueue_stop`/`_rec_tap_rx`/`_rec_tap_tx`/`_recording_should_capture_rx`/`_recording_writer_loop`/`_broadcast_recording_state`/`_recording_path`/`_recordings_payload`/`_delete_recording`/`_recording_response`：任务 5–7 定义，任务 8 的前端通过 `/api/recordings` 间接使用（字段名 `recordings/count/total_bytes/name/freq_hz/started_at/duration/bytes/recording` 在任务 6–8 一致）。
- `radioState.recording`：任务 6 广播 `recording` 对象、任务 8 读取同一对象（`{recording, freq_hz, started_at, duration, name, bytes}`）。
- `MRRC_RECORDINGS_DIR`/`RECORDINGS_DIR`/`RECORDINGS_INDEX`/`RECORDINGS_BITRATE`/`RECORDINGS_MAX_SESSION_MIN`：任务 4 定义，任务 5–7 使用一致。
- `RX`/`TX` 源标记：`"rx"`/`"tx"` 贯穿 recorder、tap、writer、索引（不出现 `"RX"` 变体）。
