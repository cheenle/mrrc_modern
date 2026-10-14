# FT8 云端深路径 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让级联的**升级**在 hub 上执行（`cycles=3`，本地买不起的深度），盒子侧行为在云端不可用时与今天**逐字一致**，并以一次真实时隙的召回测量决定这条路是否继续。

**Architecture:** 云端复用它已经有的东西——hub 跑**同一个** `ft8/core/worker.py` + `WorkerSupervisor`，请求帧是 `decode` 去掉 `shm`、加 `pcm_bytes`（新帧类型 `decode_remote`），响应**原样转发** worker 的 `decode_ok`。盒子侧新增 `CloudDecoder`（与 `SupervisorDecoder` 同 `decode_path()` 协议），`CascadingDecoder` 多一个可选 `remote`：策略判定升级时先试云端、带 deadline，失败/超时/断网就落到现有的预算守卫 + 本地深路径。传输是**普通 HTTPS POST**（stdlib 两端，走既有 nginx 443 与既有 LE 证书，无新依赖、无新端口、无 WSS 版本面）。

**Tech Stack:** Python 3.13（`asyncio` / `http.client` / `threading` / `multiprocessing.shared_memory`）、`pytest`（`tests/ft8/`）、hub 侧 stdlib `http.server` + 同一份 `ft8/core/`、nginx（既有）、systemd。

**Spec:** `docs/superpowers/specs/2026-10-14-ft8-cloud-escalation-design.md`

## Global Constraints

- **测试运行器**：`tests/ft8/` 是 **pytest**，命令一律 `.venv/bin/python -m pytest tests/ft8 -q`（单文件/单用例加路径）。PATH 上没有 `python`。
- **不动 `ft8/dsp/`**：本计划只改 Python。四平台原生库**不需要**重编；任何一步如果"需要改 DSP 才能过"，停下、汇报，不要改。
- **OMP_STACKSIZE 铁律**：`os.environ.setdefault("OMP_STACKSIZE", "10M")` 必须在导入 NumPy / 加载 `libwsjt_core` 之前。本计划的文件不碰这段顺序，新增测试文件若 import numpy 必须放在同一位置之上。
- **唯一 ctypes 边界**：不新增第二个 `CDLL`；一切原生调用仍经 `ft8/core/binding.py` / `WorkerSupervisor`。
- **严格帧 schema**：新增帧类型必须走 `_FRAME_FIELDS` + `_exact_fields`（多一个少一个字段都拒），与既有类型同等强度。
- **默认关闭**：`MRRC_FT8_CLOUD_DECODE_URL` 为空时，级联的行为必须与今天**逐字一致**（这是 Task 4 的回归判据）。
- **不落盘音频**：hub 服务收到 PCM 只用即扔（不得写文件、不得进日志）。
- **提交前**：`python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 必须干净。
- **不得改动**：`static/ft710_main.js`、`static/ft710_ui.js`、`atr1000_tuner.json`、`mem_channels.json`、`website/js/global-nav.js`。

## Review Focus

spec 是愿景文档，下面这些是它没有逐条写、但任何合理的人都会期望程序正确处理的情形。每条都在对应 Task 的测试里钉住：

1. **跨机时钟**（spec §5.1）：`deadline_monotonic` 由盒子产生，对 hub **无意义**。若 hub 拿它当截止，会得到"立刻超时"或"永不超时"两种都错的行为。期望：hub 只用**自己的** `time.monotonic() + 本地超时` 构造 worker 帧。→ Task 6。
2. **body 长度与 `pcm_bytes` 不符**：必须拒绝整条请求（400），不得"用前 N 字节"继续解。→ Task 1（schema）+ Task 6（服务）。
3. **半开连接**：请求发出后对端消失，盒子的 `wait_for` 必须在 deadline 内放弃，且**丢弃该连接**（不得被下一个时隙复用）。→ Task 3。
4. **同一信号被两条路径以微小不同的 `freq/dt` 解出**：云端（`cycles=3`）与本地（`cycles=1`）对同一信号的 `freq/dt` 可能有亚赫兹差异，去重键是 5 Hz / 0.15 s 容差 —— 期望合并后**不重复**。→ Task 4。
5. **令牌缺失或错误**：服务必须 401 且**不读 body**；盒子侧当作 `CloudUnavailable` 落回本地，不退避重试到 deadline 之外。→ Task 3 + Task 6。
6. **`threads` 覆盖**：盒子传的是给 4×A53 选的线程数；hub 是 1 vCPU（实测 t1 0.476 s / t2 0.583 / t3 0.644）。期望：云端固定 1，**且 hub 再兜底一次 cap**。→ Task 3 + Task 6。

---

### 任务 1：`decode_remote` 帧类型

**文件：**
- 修改：`ft8/core/protocol.py`（`_FRAME_FIELDS`、`_validate_frame`，新增常量）
- 测试：`tests/ft8/core/test_protocol.py`（追加）

- [ ] **步骤 1：写失败的测试**

在 `tests/ft8/core/test_protocol.py` 末尾追加（`decode_config()` 已存在，直接复用）：

```python
def decode_remote_frame(**changes: object) -> dict[str, object]:
    frame = {
        "v": PROTOCOL_VERSION,
        "type": "decode_remote",
        "generation": 7,
        "request_id": 9,
        "slot_id": 31_234,
        "deadline_monotonic": 1234.5,
        "config": decode_config(),
        "pcm_bytes": 360_000,
    }
    frame.update(changes)
    return frame


def test_decode_remote_round_trips_through_the_exact_schema() -> None:
    frame = decode_remote_frame()
    assert decode_frame(encode_frame(frame)) == frame


@pytest.mark.parametrize("field", sorted(decode_remote_frame()))
def test_decode_remote_requires_every_field(field: str) -> None:
    frame = decode_remote_frame()
    del frame[field]
    with pytest.raises(FrameError, match="missing fields"):
        encode_frame(frame)


def test_decode_remote_rejects_a_field_from_the_local_decode_frame() -> None:
    with pytest.raises(FrameError, match="frame extra fields: shm"):
        encode_frame(decode_remote_frame(shm=decode_shm()))


@pytest.mark.parametrize("value", [0, 359_998, 360_002, "360000", True, 2**40])
def test_decode_remote_requires_the_exact_slot_length(value: object) -> None:
    with pytest.raises(FrameError, match="pcm_bytes"):
        encode_frame(decode_remote_frame(pcm_bytes=value))


def test_decode_remote_validates_the_config_exactly_like_a_local_decode() -> None:
    config = decode_config()
    config["sample_count"] = 12_345
    with pytest.raises(FrameError):
        encode_frame(decode_remote_frame(config=config))
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/ft8/core/test_protocol.py -q`
预期：新用例全部 FAIL，报 `FrameError: unsupported frame type`（老用例仍 PASS）。

- [ ] **步骤 3：实现最少的代码**

`ft8/core/protocol.py`：在 imports 下加一个常量（`FT8_RX_SAMPLES` 已在 `ft8.core.models` 里，protocol 与 models 同包）：

```python
from ft8.core.models import FT8_RX_SAMPLES
...
# The one payload a remote decode may carry: the exact 12 kHz int16 slot.
SLOT_PCM_BYTES = FT8_RX_SAMPLES * 2
```

`_FRAME_FIELDS` 增加一项（放在 `"decode"` 之后，保持"本地帧 / 远程帧"相邻）：

```python
    # Same decode, different envelope: the descriptor must NOT be here — the
    # array cannot travel as a local shm segment across machines.  The PCM is
    # the HTTP body, and pcm_bytes is the length contract for it.
    "decode_remote": _BASE_FIELDS
    | {"slot_id", "deadline_monotonic", "config", "pcm_bytes"},
```

`_validate_frame` 在 `elif frame_type == "decode":` 分支之后插入：

```python
    elif frame_type == "decode_remote":
        _nonnegative_int64(value["slot_id"], "slot_id")
        _finite_number(value["deadline_monotonic"], "deadline_monotonic", 0.0)
        _validate_config(value["config"])
        pcm_bytes = value["pcm_bytes"]
        if type(pcm_bytes) is not int or pcm_bytes != SLOT_PCM_BYTES:
            raise FrameError(f"pcm_bytes must be exactly {SLOT_PCM_BYTES}")
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/ft8/core/test_protocol.py -q`
预期：全绿（既有参数化用例 + 新增 4 类）。

- [ ] **步骤 5：Commit**

```bash
git add ft8/core/protocol.py tests/ft8/core/test_protocol.py
git commit -m "feat(ft8): a decode frame that can cross a machine boundary

decode_remote is decode minus the shared-memory descriptor plus the length
of the PCM that rides in the HTTP body — the descriptor is exactly the part
that cannot cross machines, so the difference is the transport and nothing
else.  The length is pinned to the one legal slot (180000 int16), and the
config is validated by the same function the local frame uses, so a remote
request cannot be laxer than a local one."
```

---

### 任务 2：抽出版本共享的 config 序列化与响应解析

**文件：**
- 修改：`ft8/engine/dsp_decode.py`
- 测试：`tests/ft8/engine/test_dsp_decode.py`（追加）

- [ ] **步骤 1：写失败的测试**

在 `tests/ft8/engine/test_dsp_decode.py` 末尾追加：

```python
from ft8.core.models import DecodeConfig, DecodePath
from ft8.engine.dsp_decode import (
    batch_from_decode_ok,
    decode_config_frame,
    decode_config_from_frame,
)


def test_the_config_frame_round_trips_through_the_wire_form() -> None:
    config = DecodeConfig(
        path=DecodePath.IMPROVED, profile=3, threads=3, cycles=3,
        my_call="BG7ZHS", dx_call="JA1ABC", dx_grid="PM95", qso_progress=2,
    )
    frame = decode_config_frame(config)
    # Protocol v1 requires a plain str for path (StrEnum fails its type check).
    assert frame["path"] == "improved"
    assert decode_config_from_frame(frame) == config


def test_batch_from_decode_ok_copies_every_field_the_worker_reports() -> None:
    response = {
        "type": "decode_ok",
        "slot_id": 42,
        "path": "improved",
        "results": [
            {
                "slot_id": 42, "sync": 1.5, "snr": -19, "dt": 0.2,
                "frequency": 1500.0, "text": "CQ JA1ABC PM95",
                "ap_type": 0, "quality": 0.5, "flags": 0,
            }
        ],
        "overflow": False,
        "elapsed_seconds": 1.25,
        "cpu_seconds": 3.5,
        "deadline_missed": False,
    }
    batch = batch_from_decode_ok(response)
    assert batch.slot_id == 42
    assert batch.path is DecodePath.IMPROVED
    assert batch.elapsed_seconds == 1.25
    assert batch.results[0].text == "CQ JA1ABC PM95"
    assert batch.overflow is False
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/ft8/engine/test_dsp_decode.py -q`
预期：ImportError（`decode_config_frame` 不存在）。

- [ ] **步骤 3：实现最少的代码**

在 `ft8/engine/dsp_decode.py` 里加两个模块级函数（放在 `slot_utc_hhmmss` 之后），并把 `SupervisorDecoder.decode_path()` 里的对应代码换成调用它们：

```python
def decode_config_frame(config: DecodeConfig) -> dict[str, object]:
    """Serialise one ``DecodeConfig`` exactly as Protocol v1's ``config`` field.

    One function for both callers (the local worker frame and the cloud
    request) is the mechanism that keeps the two paths from drifting: a field
    added to ``DecodeConfig`` cannot reach only one of them.
    """

    # Protocol v1 requires a plain str path (StrEnum fails its type check).
    return {**asdict(config), "path": str(config.path)}


def decode_config_from_frame(frame: dict[str, object]) -> DecodeConfig:
    """Inverse of :func:`decode_config_frame` — the hub side of the wire."""

    fields = {**frame, "path": DecodePath(str(frame["path"]))}
    return DecodeConfig(**fields)  # type: ignore[arg-type]


def batch_from_decode_ok(response: dict[str, object]) -> DecodeBatch:
    """Copy one validated ``decode_ok`` frame into an immutable batch."""

    return DecodeBatch(
        slot_id=int(response["slot_id"]),  # type: ignore[arg-type]
        path=DecodePath(str(response["path"])),
        results=tuple(
            DecodeResult(
                slot_id=int(item["slot_id"]),
                sync=float(item["sync"]),
                snr=int(item["snr"]),
                dt=float(item["dt"]),
                frequency=float(item["frequency"]),
                text=str(item["text"]),
                ap_type=int(item["ap_type"]),
                quality=float(item["quality"]),
                flags=int(item["flags"]),
            )
            for item in response["results"]  # type: ignore[union-attr]
        ),
        overflow=bool(response["overflow"]),
        elapsed_seconds=float(response["elapsed_seconds"]),
        deadline_missed=bool(response["deadline_missed"]),
    )
```

`decode_path()` 里：`config_frame = decode_config_frame(config)`（替换原来那两行），返回处改成 `return batch_from_decode_ok(response)`。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/ft8/engine/test_dsp_decode.py tests/ft8/engine/test_cascading_decoder.py -q`
预期：全绿（**既有用例一条都不许改**：这一步不允许有行为变更）。

- [ ] **步骤 5：Commit**

```bash
git add ft8/engine/dsp_decode.py tests/ft8/engine/test_dsp_decode.py
git commit -m "refactor(ft8): one config serialiser and one response parser

The cloud decoder needs to build the same config dict the worker receives
and to read the same decode_ok the worker returns.  Extracting both means a
field added to DecodeConfig cannot reach only one of the two transports —
which is the whole reason the cloud path can be trusted to mean the same
thing.  No behaviour change: every existing dsp_decode test is untouched."
```

---

### 任务 3：`CloudDecoder`

**文件：**
- 创建：`ft8/platform/cloud_decode.py`
- 测试：创建 `tests/ft8/platform/test_cloud_decode.py`

- [ ] **步骤 1：写失败的测试**

创建 `tests/ft8/platform/test_cloud_decode.py`：

```python
from __future__ import annotations

import base64
import http.server
import json
import threading
from typing import Any

import pytest

from ft8.core.models import DecodeConfig, DecodePath
from ft8.core.protocol import PROTOCOL_VERSION, encode_frame
from ft8.platform.cloud_decode import (
    CLOUD_THREADS,
    CloudDecoder,
    CloudUnavailable,
    cloud_decoder_from_environ,
)


def _ok_body(slot_id: int) -> bytes:
    return encode_frame(
        {
            "v": PROTOCOL_VERSION, "type": "decode_ok", "generation": 0,
            "request_id": 0, "slot_id": slot_id, "path": "improved",
            "results": [{
                "slot_id": slot_id, "sync": 1.0, "snr": -20, "dt": 0.1,
                "frequency": 1500.0, "text": "CQ N0CALL FN42",
                "ap_type": 0, "quality": 0.0, "flags": 0,
            }],
            "overflow": False, "elapsed_seconds": 1.2,
            "cpu_seconds": 1.2, "deadline_missed": False,
        }
    )


class _FakeHub:
    """A real HTTP server: the wire format is under test, not a mock."""

    def __init__(self, handler):
        self.seen: list[dict[str, Any]] = []
        self._handler = handler
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._make())

    def _make(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.seen.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "config": json.loads(base64.b64decode(self.headers["X-MRRC-Config"])),
                    "body": body,
                })
                status, payload = outer._handler()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # silence the test output
                pass

        return Handler

    def __enter__(self):
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/mrrc-decode"


def _decoder(server: _FakeHub, **kwargs) -> CloudDecoder:
    # http:// is accepted only by the tests: production requires https (asserted below).
    return CloudDecoder(server.url, "secret-token", cycles=3, **kwargs)


SLOT = bytes(360_000)


def test_a_clean_call_returns_the_batch_and_sends_the_slot() -> None:
    with _FakeHub(lambda: (200, _ok_body(7))) as hub:
        decoder = _decoder(hub)
        import asyncio

        batch = asyncio.run(decoder.decode_path(7, SLOT, DecodePath.IMPROVED))

    assert batch.slot_id == 7
    assert batch.results[0].text == "CQ N0CALL FN42"
    seen = hub.seen[0]
    assert seen["path"] == "/mrrc-decode"
    assert seen["auth"] == "Bearer secret-token"
    assert seen["body"] == SLOT
    assert seen["config"]["type"] == "decode_remote"
    assert seen["config"]["pcm_bytes"] == 360_000
    assert seen["config"]["slot_id"] == 7
    # The box's 3 threads are for a 4xA53; the hub is one core.
    assert seen["config"]["config"]["threads"] == CLOUD_THREADS == 1
    assert seen["config"]["config"]["cycles"] == 3


@pytest.mark.parametrize("status", [401, 503, 500, 404])
def test_a_non_200_answer_is_unavailable_not_an_exception_leak(status: int) -> None:
    with _FakeHub(lambda: (status, b"{}")) as hub:
        import asyncio

        with pytest.raises(CloudUnavailable, match=str(status)):
            asyncio.run(_decoder(hub).decode_path(7, SLOT, DecodePath.IMPROVED))


def test_a_dead_endpoint_is_unavailable() -> None:
    import asyncio

    decoder = CloudDecoder("http://127.0.0.1:9/mrrc-decode", "t", cycles=3)
    with pytest.raises(CloudUnavailable):
        asyncio.run(decoder.decode_path(7, SLOT, DecodePath.IMPROVED))


def test_a_wrong_length_slot_is_refused_before_the_network() -> None:
    import asyncio

    with _FakeHub(lambda: (200, _ok_body(7))) as hub:
        with pytest.raises(ValueError, match="360000"):
            asyncio.run(_decoder(hub).decode_path(7, SLOT[:-2], DecodePath.IMPROVED))
    assert hub.seen == []


def test_a_body_the_strict_schema_rejects_is_unavailable() -> None:
    # hub 回了一个 JSON，但不是 decode_ok（例如一个字面量错误对象）。
    with _FakeHub(lambda: (200, b'{"error": "boom"}')) as hub:
        import asyncio

        with pytest.raises(CloudUnavailable):
            asyncio.run(_decoder(hub).decode_path(7, SLOT, DecodePath.IMPROVED))


def test_the_url_must_be_https_in_production(monkeypatch) -> None:
    with pytest.raises(ValueError, match="https"):
        CloudDecoder("http://hub.example/mrrc-decode", "t")


def test_an_empty_environment_configures_nothing() -> None:
    assert cloud_decoder_from_environ({}) is None
    assert cloud_decoder_from_environ({"MRRC_FT8_CLOUD_DECODE_URL": "  "}) is None


def test_the_environment_builds_a_decoder_with_the_documented_defaults() -> None:
    decoder = cloud_decoder_from_environ({
        "MRRC_FT8_CLOUD_DECODE_URL": "https://www.vlsc.net/mrrc-decode",
        "MRRC_FT8_CLOUD_DECODE_TOKEN": "tok",
    })
    assert decoder is not None
    assert decoder.cycles == 3
    assert decoder.deadline_s == 2.5
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/ft8/platform/test_cloud_decode.py -q`
预期：collection error —— `No module named 'ft8.platform.cloud_decode'`。

- [ ] **步骤 3：实现模块**

创建 `ft8/platform/cloud_decode.py`：

```python
"""Cloud deep-path client: the same decode call, executed on the hub.

Design: ``docs/superpowers/specs/2026-10-14-ft8-cloud-escalation-design.md``.
The transport is a plain HTTPS POST through the existing nginx — no new
package, no new port, and the existing certificate verifies against the
system store, so nothing is pinned.  The request frame is ``decode_remote``
and the reply is the worker's own ``decode_ok``, so a cloud batch is parsed
by exactly the code that parses a local one.
"""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import time
from urllib.parse import urlsplit

from ft8.core.models import DecodeBatch, DecodeConfig, DecodePath
from ft8.core.protocol import PROTOCOL_VERSION, decode_frame
from ft8.engine.dsp_decode import batch_from_decode_ok, decode_config_frame
from ft8.engine.orchestrator import SLOT_SAMPLES_NBYTES

# The hub is a 1-vCPU box: measured 2026-10-14, one deep pass at profile 3
# costs 0.476 s at threads=1, and 0.583 / 0.644 s at threads=2 / 3 — the
# box's thread count was chosen for four A53 cores and is the wrong number
# to send here.  This is the only config override the cloud path makes.
CLOUD_THREADS = 1

# Defaults from the design (§5.5); the deadline is the *cascade's* business,
# carried here so a decoder built from the environment has a sane socket
# timeout without the caller having to pass one.
DEFAULT_CYCLES = 3
DEFAULT_DEADLINE_S = 2.5
_SOCKET_SLACK_S = 1.0


class CloudUnavailable(RuntimeError):
    """The cloud deep path could not produce a batch; fall back to the box."""


class CloudDecoder:
    """A ``decode_path`` provider backed by the hub's decode service."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        cycles: int = DEFAULT_CYCLES,
        deadline_s: float = DEFAULT_DEADLINE_S,
        config: DecodeConfig | None = None,
        allow_plain_http: bool = False,
        clock=time.monotonic,
    ) -> None:
        parts = urlsplit(url)
        if parts.scheme == "https":
            scheme_ok = True
        elif parts.scheme == "http" and allow_plain_http:
            scheme_ok = True
        else:
            scheme_ok = False
        if not scheme_ok or not parts.hostname or not parts.path:
            raise ValueError("cloud decode url must be https://host[:port]/path")
        if not token:
            raise ValueError("cloud decode token must not be empty")
        self._host = parts.hostname
        self._port = parts.port
        self._path = parts.path
        self._tls = parts.scheme == "https"
        self._token = token
        self._config = config or DecodeConfig()
        self._clock = clock
        self.cycles = int(cycles)
        self.deadline_s = float(deadline_s)
        self._connection: http.client.HTTPSConnection | None = None

    # -- wire -------------------------------------------------------------

    def _request_frame(self, slot_id: int, pcm_bytes: int) -> dict[str, object]:
        config = self._config.replace(
            path=DecodePath.IMPROVED,
            threads=CLOUD_THREADS,
            cycles=self.cycles,
        )
        return {
            "v": PROTOCOL_VERSION,
            "type": "decode_remote",
            "generation": 0,
            "request_id": 0,
            "slot_id": slot_id,
            "deadline_monotonic": self._clock() + self.deadline_s,
            "config": decode_config_frame(config),
            "pcm_bytes": pcm_bytes,
        }

    def _post(self, slot_id: int, samples: bytes) -> dict[str, object]:
        frame = self._request_frame(slot_id, len(samples))
        header = base64.b64encode(
            json.dumps(frame, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).decode("ascii")
        timeout = self.deadline_s + _SOCKET_SLACK_S
        try:
            if self._connection is None:
                factory = http.client.HTTPSConnection if self._tls else http.client.HTTPConnection
                self._connection = factory(self._host, self._port, timeout=timeout)
            connection = self._connection
            connection.timeout = timeout
            connection.request(
                "POST", self._path, body=samples,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "X-MRRC-Config": header,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(len(samples)),
                    "Connection": "keep-alive",
                },
            )
            response = connection.getresponse()
            payload = response.read()
            status = response.status
        except Exception as error:                       # noqa: BLE001
            self._drop()                                 # never reuse a half-dead socket
            raise CloudUnavailable(f"cloud decode request failed: {error}") from error
        if status != 200:
            # 5xx means the service is unhappy; drop the connection.  4xx is our
            # own fault (token, schema) and retrying will not help either.
            self._drop()
            raise CloudUnavailable(f"cloud decode returned HTTP {status}")
        framed = decode_frame(payload)                   # the same strict schema
        if framed.get("type") != "decode_ok":
            raise CloudUnavailable(f"cloud decode returned {framed.get('type')!r}")
        return framed

    def _drop(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:                            # noqa: BLE001
                pass

    # -- provider protocol ------------------------------------------------

    async def decode_path(
        self, slot_id: int, samples: bytes, path: DecodePath, *, threads: int | None = None
    ) -> DecodeBatch:
        if len(samples) != SLOT_SAMPLES_NBYTES:
            raise ValueError(f"decode requires exactly {SLOT_SAMPLES_NBYTES} bytes")
        response = await asyncio.to_thread(self._post, slot_id, samples)
        return batch_from_decode_ok(response)

    def close(self) -> None:
        self._drop()


def cloud_decoder_from_environ(environ) -> CloudDecoder | None:
    """Build a decoder from ``MRRC_FT8_CLOUD_DECODE_*``; None when disabled."""

    url = (environ.get("MRRC_FT8_CLOUD_DECODE_URL") or "").strip()
    if not url:
        return None
    token = (environ.get("MRRC_FT8_CLOUD_DECODE_TOKEN") or "").strip()
    if not token:
        raise ValueError("MRRC_FT8_CLOUD_DECODE_URL is set but the token is not")
    cycles = int((environ.get("MRRC_FT8_CLOUD_DECODE_CYCLES") or DEFAULT_CYCLES))
    deadline = float(
        environ.get("MRRC_FT8_CLOUD_DECODE_DEADLINE_S") or DEFAULT_DEADLINE_S
    )
    return CloudDecoder(url, token, cycles=cycles, deadline_s=deadline)
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/ft8/platform/test_cloud_decode.py -q`
预期：全绿。

- [ ] **步骤 5：Commit**

```bash
git add ft8/platform/cloud_decode.py tests/ft8/platform/test_cloud_decode.py
git commit -m "feat(ft8): a cloud decoder that is the same decode call, elsewhere

The socket timeout is the deadline plus a second of slack, and any failure
— refused connection, non-200, a body the strict schema rejects — becomes
CloudUnavailable, which is what the cascade will treat as 'use the box'.
The single override is threads=1: the box's 3 was chosen for four A53
cores, and the hub is one EPYC core where more threads are measurably
slower (0.476 vs 0.583/0.644 s)."
```

---

### 任务 4：级联接缝（`remote` + 计数）

**文件：**
- 修改：`ft8/engine/cascade.py`
- 测试：`tests/ft8/engine/test_cascading_decoder.py`（追加）

- [ ] **步骤 1：写失败的测试**

在 `tests/ft8/engine/test_cascading_decoder.py` 末尾追加（`_r` / `_b` / `PathAwareFake` 都是该文件已有的辅助，直接用；构造级联照抄既有用例的参数形状）：

```python
from ft8.platform.cloud_decode import CloudUnavailable


class RemoteFake:
    """A remote provider: a fixed batch or a raised error, plus a call count."""

    def __init__(self, batch: DecodeBatch | None = None, error: Exception | None = None) -> None:
        self.calls = 0
        self.batch = batch
        self.error = error

    async def decode_path(self, slot_id, samples, path, *, threads=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.batch


def _qso_cascade(inner, remote, **kwargs):
    """The file's own construction shape, with the AP context that forces an escalation."""

    return CascadingDecoder(
        inner, CascadePolicy(),
        cutoff=2.5,
        boundary_epoch_for=lambda s: 100.0,
        clock=kwargs.pop("clock", lambda: 100.0),
        ap_context=lambda: ("BA4EG", 2),
        remote=remote,
        **kwargs,
    )


def test_the_remote_provider_answers_the_escalation() -> None:
    fast = _b(DecodePath.FAST, _r("CQ AAA"))
    deep = _b(DecodePath.IMPROVED, _r("CQ BBB"))
    inner = PathAwareFake({DecodePath.FAST: fast, DecodePath.IMPROVED: deep})
    remote = RemoteFake(batch=_b(DecodePath.IMPROVED, _r("CQ JA1ABC")))
    decoder = _qso_cascade(inner, remote)

    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))

    assert remote.calls == 1
    assert decoder.cloud_ok == 1 and decoder.cloud_fallback == 0
    assert {r.text for r in out.results} == {"CQ AAA", "CQ JA1ABC"}
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST], (
        "the local deep path must not run when the cloud answered"
    )


def test_a_failed_remote_falls_back_to_the_local_deep_path() -> None:
    fast = _b(DecodePath.FAST, _r("CQ AAA"))
    deep = _b(DecodePath.IMPROVED, _r("CQ BBB"))
    inner = PathAwareFake({DecodePath.FAST: fast, DecodePath.IMPROVED: deep})
    remote = RemoteFake(error=CloudUnavailable("hub down"))
    decoder = _qso_cascade(inner, remote)

    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))

    assert remote.calls == 1
    assert decoder.cloud_fallback == 1 and decoder.cloud_ok == 0
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST, DecodePath.IMPROVED]
    assert {r.text for r in out.results} == {"CQ AAA", "CQ BBB"}
    assert out.escalated is True


def test_a_remote_failure_lands_on_the_same_budget_guard() -> None:
    """远端失败之后的语义 = 今天「预算太紧」：不跑本地、计一次 deadline_risk。"""

    fast = _b(DecodePath.FAST, _r("CQ AAA"))
    deep = _b(DecodePath.IMPROVED, _r("CQ BBB"))
    inner = PathAwareFake({DecodePath.FAST: fast, DecodePath.IMPROVED: deep})
    remote = RemoteFake(error=CloudUnavailable("hub down"))
    decoder = _qso_cascade(inner, remote, clock=lambda: 102.5)

    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))

    assert decoder.cloud_fallback == 1
    assert decoder.deadline_risk == 1, "exactly once — not once for the cloud and again for the box"
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST]
    assert {r.text for r in out.results} == {"CQ AAA"}


class SleepingRemote:
    async def decode_path(self, slot_id, samples, path, *, threads=None):
        await asyncio.sleep(5)
        raise AssertionError("unreachable")


def test_a_remote_that_never_answers_is_abandoned_at_the_deadline() -> None:
    fast = _b(DecodePath.FAST, _r("CQ AAA"))
    deep = _b(DecodePath.IMPROVED, _r("CQ BBB"))
    inner = PathAwareFake({DecodePath.FAST: fast, DecodePath.IMPROVED: deep})
    decoder = _qso_cascade(inner, SleepingRemote(), cloud_deadline_s=0.05)

    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))

    assert decoder.cloud_fallback == 1
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST, DecodePath.IMPROVED]


def test_a_cloud_result_a_few_hertz_off_is_not_a_duplicate() -> None:
    """同一信号在两条路径下的 freq/dt 会有亚赫兹差 —— 合并键的容差必须吃掉它。"""

    fast = _b(DecodePath.FAST, DecodeResult(1, 0.0, -20, 0.10, 1500.0, "CQ AAA", 0, 0.0, 0))
    remote = RemoteFake(batch=_b(DecodePath.IMPROVED,
                                 DecodeResult(1, 0.0, -21, 0.12, 1500.4, "CQ AAA", 0, 0.0, 0)))
    inner = PathAwareFake({DecodePath.FAST: fast})
    decoder = _qso_cascade(inner, remote)

    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))

    assert [r.text for r in out.results] == ["CQ AAA"], "deep wins, no duplicate row"
    assert out.results[0].snr == -21
```

> `remote=None` 的回归由该文件**现有**用例承担（它们仍按老签名构造）—— 这一步不允许改它们一个字。

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/ft8/engine/test_cascading_decoder.py -q`
预期：新用例 FAIL，`TypeError: __init__() got an unexpected keyword argument 'remote'`。

- [ ] **步骤 3：实现**

`ft8/engine/cascade.py`：构造函数加两个可选参数并初始化计数：

```python
        remote: object | None = None,
        cloud_deadline_s: float = 2.5,
...
        self._remote = remote
        self._cloud_deadline_s = cloud_deadline_s
        # 观测：云端成功的次数、落回本地的次数、最近一次云端墙上耗时。
        # 与 `deadline_risk` 并列，供 SDD §10 的日志与 /api/ft8/engine/stats。
        self.cloud_ok = 0
        self.cloud_fallback = 0
        self.cloud_seconds = 0.0
        # 每槽日志行要能把"这一批深路径是谁算的"区分出来（本地 / 云端）。
        self.last_deep_path = ""
```

`decode()` 在 `should_escalate` 之后、预算守卫之前插入：

```python
        if self._remote is not None:
            started = self._clock()
            try:
                batch_deep = await asyncio.wait_for(
                    self._remote.decode_path(slot_id, samples, DecodePath.IMPROVED),
                    timeout=self._cloud_deadline_s,
                )
            except (TimeoutError, CloudUnavailable, DecodeError):
                # 云端这条路的失败是**预期内**的：断网、hub 忙、超时。
                # 它不改变本时隙的深路径语义 —— 落回下面那条原路。
                self.cloud_fallback += 1
                logger.info("cloud escalation unavailable for slot %s; using the local path", slot_id)
            except Exception:
                self.cloud_fallback += 1
                logger.warning("cloud escalation failed for slot %s", slot_id, exc_info=True)
            else:
                self.cloud_ok += 1
                self.cloud_seconds = self._clock() - started
                self.last_deep_path = "cloud"
                return merge_batches(batch_fast, batch_deep)
```

顶部 imports 需要 `from ft8.platform.cloud_decode import CloudUnavailable`。本地那条路（下面那段一字不动的代码）在 `return merge_batches(batch_fast, batch_deep)` 之前同样置 `self.last_deep_path = "local"`。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/ft8/engine/test_cascading_decoder.py tests/ft8/engine/test_cascade_merge.py tests/ft8/engine/test_cascade_policy.py -q`
预期：全绿；**既有用例一条都不许改**（`remote=None` 的路径必须与原实现等价）。

- [ ] **步骤 5：Commit**

```bash
git add ft8/engine/cascade.py tests/ft8/engine/test_cascading_decoder.py
git commit -m "feat(ft8): an escalation can be answered by the cloud

The seam is one optional provider tried between the policy decision and the
budget guard.  Everything after a cloud failure is the code that was there
before — including the guard, which will now usually refuse and count one
deadline_risk, exactly as it does today when the budget is tight.  With the
provider absent the cascade is byte-for-byte its previous self."
```

---

### 任务 5：运行时接线、配置与观测

**文件：**
- 修改：`ft8/runtime.py`（`Ft8Runtime.__init__` 的级联构造、`cascade_stats`、每槽日志标记）
- 测试：`tests/ft8/test_runtime_cascade_wiring.py`（追加）

- [ ] **步骤 1：写失败的测试**

追加：

```python
def _cloud_runtime(monkeypatch, tmp_path, **env):
    """该文件自己的构造形状，加一个受控的云端环境变量集。"""

    monkeypatch.setattr(runtime_mod, "WorkerSupervisor", _FakeSupervisor)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return runtime_mod.Ft8Runtime(
        rig=_FakeRig(),
        tx_player=_FakeTxPlayer(),
        sequencer_factory=_FakeSequencer,
        data_dir=tmp_path,
    )


def test_a_configured_hub_reaches_the_live_cascade(monkeypatch, tmp_path) -> None:
    runtime = _cloud_runtime(
        monkeypatch, tmp_path,
        MRRC_FT8_CLOUD_DECODE_URL="https://www.vlsc.net/mrrc-decode",
        MRRC_FT8_CLOUD_DECODE_TOKEN="tok",
    )

    async def _run() -> None:
        await runtime.start()
        try:
            assert runtime._cascade._remote is not None
            assert runtime._cascade._cloud_deadline_s == 2.5
        finally:
            await runtime.stop()

    asyncio.run(_run())


def test_no_url_means_no_cloud_path(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("MRRC_FT8_CLOUD_DECODE_URL", raising=False)
    runtime = _cloud_runtime(monkeypatch, tmp_path)

    async def _run() -> None:
        await runtime.start()
        try:
            assert runtime._cascade._remote is None
        finally:
            await runtime.stop()

    asyncio.run(_run())


def test_the_cascade_stats_carry_the_cloud_counters(monkeypatch, tmp_path) -> None:
    runtime = _cloud_runtime(monkeypatch, tmp_path)

    async def _run() -> None:
        await runtime.start()
        try:
            stats = runtime.cascade_stats()
            assert {"cloud_ok", "cloud_fallback", "cloud_seconds"} <= set(stats)
        finally:
            await runtime.stop()

    asyncio.run(_run())
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/ft8/test_runtime_cascade_wiring.py -q`
预期：FAIL（`_cascade._remote` 不存在 / stats 缺键）。

- [ ] **步骤 3：实现**

`ft8/runtime.py`：

```python
from ft8.platform.cloud_decode import cloud_decoder_from_environ
...
        # 云端深路径（design A1）：只在配置了 URL 时存在。默认关闭时这一整段
        # 不产生任何对象，级联的行为与今天逐字一致。
        cloud = cloud_decoder_from_environ(os.environ)
        self._cloud = cloud
...
        self._cascade = CascadingDecoder(
            self.decoder,
            policy,
            cutoff=DEFAULT_DECISION_CUTOFF_SECONDS,
            boundary_epoch_for=boundary_epoch_for_slot,
            ap_context=lambda: (sequencer.my_call, _qso_progress(sequencer.state)),
            requested=lambda: self._deep_decode_requested,
            remote=cloud,
            cloud_deadline_s=(
                cloud.deadline_s if cloud is not None else 2.5
            ),
        )
```

`cascade_stats()` 里加四个键（照现有字段的写法；它们住在 cascade 上，所以 `_cascade` 一存在就都在）：

```python
        stats = {
            "deadline_risk": cascade.deadline_risk,
            "cloud_ok": cascade.cloud_ok,
            "cloud_fallback": cascade.cloud_fallback,
            "cloud_seconds": cascade.cloud_seconds,
        }
```

每槽日志行的路径标记读 `cascade.last_deep_path`（`"cloud"` / `"local"` / `""`）：`_on_batch` 里把 `" escalated"` 那个 token 换成 `" escalated/cloud"` 或 `" escalated/local"`（空则保持原样），`tests/ft8/test_runtime_slot_counters.py` 的 `_markers()` 现有断言据此扩充一格。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/ft8/test_runtime_cascade_wiring.py tests/ft8/test_runtime_slot_counters.py -q`
预期：全绿。

- [ ] **步骤 5：Commit**

```bash
git add ft8/runtime.py tests/ft8/test_runtime_cascade_wiring.py
git commit -m "feat(ft8): the cloud decoder reaches the runtime, off by default

MRRC_FT8_CLOUD_DECODE_URL decides whether a cloud provider exists at all;
with it unset the cascade is constructed exactly as before.  The three
counters join cascade_stats so the field question 'did the cloud ever
answer?' is answerable without a debugger."
```

---

### 任务 6：hub 侧服务 `mrrc-decode`

**文件：**
- 创建：`tools/hub_decode/service.py`、`tools/hub_decode/README.md`
- 创建：`tools/hub_decode/mrrc-decode.service`、`tools/hub_decode/nginx-mrrc-decode.conf`
- 测试：创建 `tests/ft8/test_hub_decode_service.py`

- [ ] **步骤 1：写失败的测试**

创建 `tests/ft8/test_hub_decode_service.py`（用假 worker，不起真解码；模块加载照抄 `tests/ft8/test_i9_field_twin.py` 的 `importlib.util` 方式 —— `tools/` 不是也不是包）：

```python
from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path

import pytest

from ft8.core.models import DecodeConfig
from ft8.core.protocol import PROTOCOL_VERSION, decode_frame
from ft8.engine.dsp_decode import decode_config_frame

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "hub_decode" / "service.py"


def _load_service():
    assert TOOL.is_file(), f"the hub service moved: {TOOL}"
    spec = importlib.util.spec_from_file_location("mrrc_hub_decode", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFIG = decode_config_frame(DecodeConfig(threads=3))


def _frame(**changes: object) -> dict[str, object]:
    frame = {
        "v": PROTOCOL_VERSION, "type": "decode_remote", "generation": 0,
        "request_id": 0, "slot_id": 7, "deadline_monotonic": 1234.5,
        "config": CONFIG, "pcm_bytes": 360_000,
    }
    frame.update(changes)
    return frame


class FakeWorker:
    """Stands in for the WorkerSupervisor: records the frame, returns decode_ok."""

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.seen: list[dict[str, object]] = []
        self.gate = gate

    def request(self, frame, timeout):
        self.seen.append(frame)
        if self.gate is not None:
            assert self.gate.wait(5.0)
        return {
            "v": PROTOCOL_VERSION, "type": "decode_ok", "generation": 0,
            "request_id": 0, "slot_id": frame["slot_id"],
            "path": frame["config"]["path"], "results": [], "overflow": False,
            "elapsed_seconds": 0.5, "cpu_seconds": 0.5, "deadline_missed": False,
        }


def _service(worker, **kwargs):
    return _load_service().DecodeService(
        worker, token="secret", monotonic=lambda: 500.0, **kwargs
    )


def test_a_valid_request_reaches_the_worker_and_returns_decode_ok() -> None:
    worker = FakeWorker()
    status, body = _service(worker).handle(
        _frame(), bytes(360_000), authorization="Bearer secret"
    )
    assert status == 200
    assert decode_frame(body)["type"] == "decode_ok"
    sent = worker.seen[0]
    assert sent["type"] == "decode"
    # 跨机时钟：worker 帧的 deadline 必须是**本机**的，不是请求里那个 1234.5。
    assert sent["deadline_monotonic"] == 510.0
    assert sent["config"]["threads"] == 1
    assert sent["shm"]["nbytes"] == 360_000


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "secret", ""])
def test_a_missing_or_wrong_token_is_401_and_reads_nothing(authorization) -> None:
    worker = FakeWorker()
    status, _ = _service(worker).handle(
        _frame(), bytes(360_000), authorization=authorization
    )
    assert status == 401
    assert worker.seen == []


@pytest.mark.parametrize("length", [0, 359_998, 360_002, 720_000])
def test_a_body_that_is_not_one_slot_is_400(length: int) -> None:
    worker = FakeWorker()
    status, _ = _service(worker).handle(
        _frame(), bytes(length), authorization="Bearer secret"
    )
    assert status == 400
    assert worker.seen == []


def test_a_local_decode_frame_is_rejected() -> None:
    worker = FakeWorker()
    frame = _frame()
    frame["type"] = "decode"          # the local frame carries a shm descriptor
    status, _ = _service(worker).handle(
        frame, bytes(360_000), authorization="Bearer secret"
    )
    assert status == 400
    assert worker.seen == []


def test_the_boxs_thread_choice_is_capped_to_the_hubs_cores() -> None:
    worker = FakeWorker()
    status, _ = _service(worker, max_threads=1).handle(
        _frame(), bytes(360_000), authorization="Bearer secret"
    )
    assert status == 200
    assert worker.seen[0]["config"]["threads"] == 1, "the box sent 3; the hub has one core"


def test_a_second_request_while_decoding_is_503_immediately() -> None:
    gate = threading.Event()
    worker = FakeWorker(gate=gate)
    service = _service(worker)
    first: list[int] = []
    thread = threading.Thread(
        target=lambda: first.append(
            service.handle(_frame(), bytes(360_000), authorization="Bearer secret")[0]
        )
    )
    thread.start()
    while not worker.seen:                     # wait for the decode to be in flight
        time.sleep(0.002)
    started = time.monotonic()
    status, _ = service.handle(_frame(), bytes(360_000), authorization="Bearer secret")
    elapsed = time.monotonic() - started
    gate.set()
    thread.join(5.0)
    assert status == 503
    assert elapsed < 0.5, "busy must answer, not wait for the running decode"
    assert first == [200]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m pytest tests/ft8/test_hub_decode_service.py -q`
预期：collection error。

- [ ] **步骤 3：实现服务**

`tools/hub_decode/service.py`（要点，完整实现按 spec §5.4）：

```python
"""mrrc-decode — the hub side of the FT8 cloud deep path.

One HTTP endpoint, one worker, one decode at a time.  The request is the
`decode_remote` frame (base64 in X-MRRC-Config) plus the slot as the body;
the answer is the worker's own `decode_ok`, forwarded verbatim so the box
parses it with the code it already trusts.
"""
```

实现要点（**与测试里的签名逐字对齐**）：`class DecodeService(worker, *, token: str, max_threads: int = 1, timeout_s: float = 10.0, monotonic=time.monotonic)`，方法 `handle(frame: dict, pcm: bytes, *, authorization: str | None) -> tuple[int, bytes]`；`ThreadingHTTPServer` + 非阻塞 `Lock`；`Authorization: Bearer` 不等即 401（**在碰 body 之前**）；长度必须等于 `SLOT_PCM_BYTES` 否则 400；`deadline_monotonic` 用**本机** `time.monotonic() + timeout_s` 重写（请求里那个不解释）；config 走 `decode_config_from_frame(...)` 并把 `threads` 夹到 `min(threads, max_threads)`；worker 帧继续用 `"type": "decode"` 与本地 shm 描述符，响应 `encode_frame(response)` 原样转发；握不到锁 → 503；任何异常 → 500 + 日志（**不打印 PCM**）。`main()` 只负责读令牌、启 `WorkerSupervisor`、绑 127.0.0.1:8990。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/ft8/test_hub_decode_service.py -q`
预期：全绿。

- [ ] **步骤 5：Commit**

```bash
git add tools/hub_decode tests/ft8/test_hub_decode_service.py
git commit -m "feat(hub): mrrc-decode runs the box's own worker for one slot at a time

The service is a transport: it validates the remote frame, writes the slot
into the local shared segment and forwards the worker's decode_ok unchanged.
The box's deadline is deliberately ignored — a monotonic clock means nothing
across machines, so the hub's own timeout is what bounds a request, and the
busy answer is 503 rather than a queue because the box has a local fallback."
```

---

### 任务 7：真实时隙落盘开关（召回测量的素材）

**文件：**
- 创建：`ft8/platform/slot_dump.py`
- 修改：`ft8/runtime.py:239` 的 `read_slot` 闭包（三行）
- 测试：创建 `tests/ft8/platform/test_slot_dump.py`

- [ ] **步骤 1：写失败的测试**

```python
def test_no_directory_means_no_dump(tmp_path, monkeypatch) -> None:
    dumper = SlotDumper.from_environ({})
    assert dumper is None


def test_each_slot_lands_once_with_its_id_in_the_name(tmp_path) -> None:
    dumper = SlotDumper.from_environ({"MRRC_FT8_SLOT_DUMP_DIR": str(tmp_path)})
    dumper.write(31_234, bytes(360_000))
    files = sorted(tmp_path.iterdir())
    assert [p.name for p in files] == ["slot_000000031234.raw"]
    assert files[0].stat().st_size == 360_000


def test_a_write_failure_never_raises_into_the_decode_path(tmp_path) -> None:
    # 用一个**文件**当输出目录：写入必然失败，且这个断言对 root 也成立
    # （只读目录对 root 无效，不能拿它当测试条件）。落盘是诊断，不是依赖。
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    dumper = SlotDumper.from_environ({"MRRC_FT8_SLOT_DUMP_DIR": str(blocker)})
    dumper.write(1, bytes(360_000))          # must not raise
    assert blocker.read_text() == "x"
```

- [ ] **步骤 2：运行测试验证失败** → collection error。

- [ ] **步骤 3：实现**

`SlotDumper`：`from_environ` 读 `MRRC_FT8_SLOT_DUMP_DIR`（空 = None）；`write(slot_id, samples)` 用 `Path.write_bytes`，异常只 `log.warning` 一次（不刷屏、不抛出）。`ft8/runtime.py` 的 `read_slot` 在 `return full` 之前调用 `self._slot_dump.write(slot_id, full)`（`None` 时是 no-op 调用 —— 用 `if self._slot_dump is not None` 避免每槽一次函数调用）。

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m pytest tests/ft8/platform/test_slot_dump.py -q`

- [ ] **步骤 5：Commit**

```bash
git add ft8/platform/slot_dump.py ft8/runtime.py tests/ft8/platform/test_slot_dump.py
git commit -m "feat(ft8): an off-by-default dump of the exact slot bytes

The recall measurement needs real slots, and the only place that holds one
is the slot source itself.  One 360 KB file per slot, only when the operator
sets the directory, and every failure is swallowed: a diagnostic must never
be able to stop a decode."
```

---

### 任务 8：召回测量装置

**文件：**
- 创建：`tools/ft8/cloud_recall_rig.py`
- 测试：创建 `tests/ft8/test_cloud_recall_rig.py`

- [ ] **步骤 1：写失败的测试**（只测纯函数：文件解析、集合差集、报表）

```python
from pathlib import Path

import pytest

from ft8.core.models import DecodeConfig, DecodePath
from ft8.engine.dsp_decode import decode_config_frame

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "ft8" / "cloud_recall_rig.py"


def _load_rig():
    assert TOOL.is_file(), f"the rig moved: {TOOL}"
    spec = importlib.util.spec_from_file_location("mrrc_cloud_recall_rig", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_slot_file_that_is_not_one_slot_is_refused(tmp_path) -> None:
    rig = _load_rig()
    path = tmp_path / "slot_000000000001.raw"
    path.write_bytes(bytes(359_998))
    with pytest.raises(ValueError, match="360000"):
        rig.load_slot(path)


def test_the_diff_is_per_slot_and_names_both_directions() -> None:
    rig = _load_rig()
    gained, lost = rig.diff({"CQ AAA", "CQ BBB"}, {"CQ BBB", "CQ CCC"})
    assert gained == {"CQ CCC"}
    assert lost == {"CQ AAA"}


def test_the_summary_prints_the_net_gain_that_decides_the_channel() -> None:
    """报表的最后一行就是"继续还是停"的判据（spec §12），所以它必须是定形字符串。"""

    rig = _load_rig()
    rows = [
        rig.SlotRun(slot_id=1, local=("CQ AAA",), hub_same=("CQ AAA",),
                    hub_deep=("CQ AAA", "CQ CCC")),
        rig.SlotRun(slot_id=2, local=("CQ DDD",), hub_same=("CQ DDD",),
                    hub_deep=("CQ DDD",)),
    ]
    text = rig.summarize(rows)
    assert "net gain (deep vs local): 1" in text
    assert "net gain (hub same depth vs local): 0" in text
    assert "slots: 2" in text
```

- [ ] **步骤 2：运行测试验证失败** → collection error。

- [ ] **步骤 3：实现**

CLI：`--slots DIR --app-root PATH [--hub URL --token T | --local] --cycles N --threads N`，逐槽跑并打印三张表（每槽消息集合、差集、SNR 分布），`--json` 落一份原始结果。判据输出必须是**定形**的（测试断言它）：最后三行分别是 `slots: N` / `net gain (hub same depth vs local): N` / `net gain (deep vs local): N`，即 spec §12 的成功/反例判据。纯函数拆成 `load_slot()` / `diff()` / `SlotRun` / `summarize()`，跑解码的那部分不进单测（它要真库）。

- [ ] **步骤 4：运行测试验证通过** 并**在开发机上做一次冒烟**（合成槽目录 → 本地配置）：

```bash
.venv/bin/python tools/ft8/cloud_recall_rig.py --slots /tmp/smoke --app-root "$PWD" --local
```

- [ ] **步骤 5：Commit**

```bash
git add tools/ft8/cloud_recall_rig.py tests/ft8/test_cloud_recall_rig.py
git commit -m "feat(ft8): the rig that decides whether depth buys anything

Three sets — the box's production configuration, the hub at the same depth,
and the hub at cycles=3 — differenced per slot.  The second set exists to
separate 'a different machine' from 'a deeper search', and the printed
bottom line is the net gain of the third over the first, because that
number is the entire justification for the channel."
```

---

### 任务 9：部署（操作员执行的部分明确标出）

**文件：**
- 修改：`tools/hub_decode/README.md`（部署步骤）

- [ ] **步骤 1：hub 上装服务**

```bash
# 在 hub 上（cheenle@www.vlsc.net）：
sudo mkdir -p /opt/mrrc-decode && sudo chown cheenle: /opt/mrrc-decode
# 把 ft8/ 子树 + tools/hub_decode/ 同步过去（树已在 ~/ft8bench/mrrc，直接用它也可）
# 令牌：
sudo install -d -m 700 /etc/mrrc-decode
head -c 32 /dev/urandom | base64 | sudo tee /etc/mrrc-decode/token >/dev/null
sudo chmod 600 /etc/mrrc-decode/token
# 单元与 nginx：
sudo install -m 644 tools/hub_decode/mrrc-decode.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mrrc-decode
# nginx：把 nginx-mrrc-decode.conf 的 location 插进 www.vlsc.net 的 server 块，
# 然后 sudo nginx -t && sudo systemctl reload nginx
```

- [ ] **步骤 2：验证（curl 就是验收工具）**

```bash
TOKEN=$(sudo cat /etc/mrrc-decode/token)
curl -s -o /dev/null -w '%{http_code}\n' https://www.vlsc.net/mrrc-decode -X POST   # 401（无令牌）
```

- [ ] **步骤 2b：入口延迟（spec R-1 的验收项）**：在**解码进行中**量 nginx 的响应

```bash
# 一边：盒子上把云端开关打开并等一个升级；另一边：每秒打一次具体页面
for i in $(seq 1 20); do curl -s -o /dev/null -w '%{time_starttransfer}\n' https://www.vlsc.net/mrrc_modern/; sleep 0.5; done
# 判据：解码那一栏期间不得出现量级变化（与解码前的同一命令对比）
```

- [ ] **步骤 3：盒子上打开开关**

```bash
# /opt/mrrc_modern/env/mrrc.env 追加（操作员）：
#   MRRC_FT8_CLOUD_DECODE_URL=https://www.vlsc.net/mrrc-decode
#   MRRC_FT8_CLOUD_DECODE_TOKEN=<同一令牌>
sudo systemctl restart mrrc-modern
```

- [ ] **步骤 4：失败演练（必须与关闭开关一致）**

```bash
ssh cheenle@www.vlsc.net 'sudo systemctl stop mrrc-decode'
# 观察盒子：升级应全部走本地，日志出现 cloud_fallback，解码条数不变
ssh cheenle@www.vlsc.net 'sudo systemctl start mrrc-decode'
```

- [ ] **步骤 5：Commit**（README 的部署命令与演练结论）

---

### 任务 10：召回测量与决策（操作员执行；这是 A1 的验收）

- [ ] **步骤 1：采集**：在盒子上设 `MRRC_FT8_SLOT_DUMP_DIR`，采清闲段与热闹段各 ≥100 槽（360 KB/槽 ⇒ 100 槽 ≈ 36 MB）。
- [ ] **步骤 2：三组跑分**：

```bash
.venv/bin/python tools/ft8/cloud_recall_rig.py --slots <dir> --app-root /opt/mrrc_modern --local
.venv/bin/python tools/ft8/cloud_recall_rig.py --slots <dir> --app-root /opt/mrrc_modern \
    --hub https://www.vlsc.net/mrrc-decode --token "$TOKEN" --cycles 1
.venv/bin/python tools/ft8/cloud_recall_rig.py --slots <dir> --app-root /opt/mrrc_modern \
    --hub https://www.vlsc.net/mrrc-decode --token "$TOKEN" --cycles 3
```

- [ ] **步骤 3：判据**：③ 相对 ① 的净增条数 > 0 且可复现 ⇒ 继续；否则**停止**（spec §12 的退出条件），把结论写进 `docs/ft8-w103d-cloud-escalation.md`。
- [ ] **步骤 4：现场延迟**：从盒子日志读 `cloud_seconds` 与每槽路径标记，确认没有一批越过 `boundary + 4.0 s`。
- [ ] **步骤 5：Commit**（测量报告）

---

### 任务 11：文档同步

**文件：**
- 修改：`SDD/09-architecture-overview.md`（解码路径的第三段）、`SDD/12-operational-model.md`（新服务/端口/日志）、`SDD/14-version-history.md`（V2.85 行追加 ⑬）、`AGENTS.md`（模块表加 `ft8/platform/cloud_decode.py` 与 `tools/hub_decode/`）、`tests/README.md`（计数）
- 修改：hub 仓 `mrrc_hub/deploy/README.md`（服务与 nginx route）

- [ ] **步骤 1：** 按上面逐文件更新，计数用实测值（`.venv/bin/python -m pytest tests/ft8 -q` 与 `python -m unittest discover -s tests` 的输出）。
- [ ] **步骤 2：** 跑文档门禁：`.venv/bin/python -m unittest tests.test_sdd_docs_consistency tests.test_release_artifacts -q`；网站页用 `python3 website/build_sdd.py` 重新生成（不得手改）。
- [ ] **步骤 3：** `python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged` 必须干净。
- [ ] **步骤 4：Commit**

```bash
git add SDD AGENTS.md tests/README.md website/sdd
git commit -m "docs(ft8): the third decode path, and where it lives"
```
