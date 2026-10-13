"""Latency histogram and NFR-002 health exposure tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from ft8.core.models import DecodeConfig
from ft8.core.protocol import encode_frame
from ft8.engine.dsp_decode import SupervisorDecoder
from ft8.engine.latency import (
    BUCKET_BOUNDS_SECONDS,
    LatencyHistogram,
    bucket_index,
)
from ft8.engine.orchestrator import DEFAULT_DECISION_CUTOFF_SECONDS
from ft8.web.ft8_api import _health


class FakeSupervisor:
    """Answers every request with one fixed decode_ok frame."""

    def __init__(self, slot_id: int) -> None:
        self.response: dict[str, object] = {
            "v": 1,
            "type": "decode_ok",
            "generation": 1,
            "request_id": 1,
            "slot_id": slot_id,
            "path": "improved",
            "results": [],
            "overflow": False,
            "elapsed_seconds": 0.25,
            "deadline_missed": False,
        }

    def request(self, frame: dict[str, object], timeout: float) -> dict[str, object]:
        encode_frame({**frame, "v": 1, "generation": 1, "request_id": 1})
        return self.response


def test_bucket_index_boundaries() -> None:
    assert bucket_index(0.0) == 0
    assert bucket_index(0.499) == 0
    assert bucket_index(0.5) == 1
    assert bucket_index(2.499) == 4
    # 2.5 不再是溢出：截止抬到 4.0 之后它是 <3s 桶的一员。
    assert bucket_index(2.5) == 5
    assert bucket_index(3.999) == len(BUCKET_BOUNDS_SECONDS) - 1
    assert bucket_index(4.0) == len(BUCKET_BOUNDS_SECONDS)  # overflow
    assert bucket_index(60.0) == len(BUCKET_BOUNDS_SECONDS)


def test_histogram_snapshot_per_configuration() -> None:
    histogram = LatencyHistogram()
    histogram.record("improved", 3, 7, 0.2)
    histogram.record("improved", 3, 7, 0.7)
    histogram.record("improved", 3, 7, 3.1)
    histogram.record("improved", 0, 1, 1.234)

    snapshot = histogram.snapshot()
    assert set(snapshot) == {"improved/p3/t7", "improved/p0/t1"}
    series = snapshot["improved/p3/t7"]
    assert series["count"] == 3
    assert series["buckets"] == {
        "<0.5s": 1,
        "<1s": 1,
        "<1.5s": 0,
        "<2s": 0,
        "<2.5s": 0,
        "<3s": 0,
        "<3.5s": 1,
        "<4s": 0,
        ">=4s": 0,
    }
    assert series["max_seconds"] == 3.1
    assert snapshot["improved/p0/t1"]["buckets"]["<1.5s"] == 1
    assert snapshot["improved/p0/t1"]["max_seconds"] == 1.234


def test_histogram_rejects_negative_elapsed() -> None:
    with pytest.raises(ValueError, match="negative"):
        LatencyHistogram().record("improved", 3, 7, -0.1)


def test_supervisor_decoder_records_wall_elapsed() -> None:
    """A successful decode adds one sample keyed by path/profile/threads."""

    config = DecodeConfig(profile=3, threads=7)
    histogram = LatencyHistogram()
    samples = np.zeros(180_000, dtype=np.int16).tobytes()
    with SupervisorDecoder(
        FakeSupervisor(480), config, histogram=histogram
    ) as decoder:
        asyncio.run(decoder.decode(480, samples))

    snapshot = histogram.snapshot()
    assert list(snapshot) == ["improved/p3/t7"]
    series = snapshot["improved/p3/t7"]
    assert series["count"] == 1
    assert sum(series["buckets"].values()) == 1


def test_histogram_separates_fast_from_deep() -> None:
    """同一次运行里两条路径各自的分布必须分开，否则快路径会把深路径的水位冲淡。"""

    histogram = LatencyHistogram()
    histogram.record("fast", 3, 3, 0.15)
    histogram.record("improved", 3, 3, 2.2)

    assert set(histogram.snapshot()) == {"fast/p3/t3", "improved/p3/t3"}
    assert histogram.snapshot()["fast/p3/t3"]["max_seconds"] == 0.15


def test_histogram_keeps_profile_and_threads_as_dimensions() -> None:
    """深路径调参要看得见 —— 同一个 path 下不同 profile/threads 仍是不同的桶。"""

    histogram = LatencyHistogram()
    histogram.record("improved", 3, 3, 2.2)
    histogram.record("improved", 1, 1, 3.23)

    assert set(histogram.snapshot()) == {"improved/p3/t3", "improved/p1/t1"}


def test_health_exposes_latency_and_deadline_misses() -> None:
    """NFR-002: health carries the histogram and the deadline-miss counter."""

    histogram = LatencyHistogram()
    histogram.record("improved", 3, 7, 0.4)
    state = SimpleNamespace(
        safety=SimpleNamespace(health={"armed": False}),
        lease=None,                                     # 平台的角色取代了 FT8 lease
        auth=SimpleNamespace(session_count=lambda: 0),  # _health 只调 session_count()
        latency=histogram,                              # 已接线：直方图直接给
        orchestrator=SimpleNamespace(counters=SimpleNamespace(deadline_misses=2)),
    )
    health = _health(state)
    assert health["decode_latency"]["improved/p3/t7"]["count"] == 1
    assert health["deadline_misses"] == 2


def test_health_omits_latency_when_not_wired() -> None:
    state = SimpleNamespace(
        safety=SimpleNamespace(health={"armed": False}),
        lease=None,                                     # 平台的角色取代了 FT8 lease
        auth=SimpleNamespace(session_count=lambda: 0),  # _health 只调 session_count()
        latency=None,                                   # 未接线：两个键都必须缺席
        orchestrator=None,
    )
    health = _health(state)
    assert "decode_latency" not in health
    assert "deadline_misses" not in health


def test_health_exposes_cascade_counters_when_the_cascade_is_wired() -> None:
    """预算挡下升级的次数必须能从 health 读到 —— 这正是「阈值是不是太紧」的证据。"""

    state = SimpleNamespace(
        safety=SimpleNamespace(health={"armed": False}),
        lease=None,
        auth=SimpleNamespace(session_count=lambda: 0),
        latency=None,
        orchestrator=None,
        # **普通 dict，不是 lambda。** `_health` 用 `getattr(runtime, "cascade_stats", None)`
        # 取值后**直接**塞进 `health["cascade"]`（见下面实现），而 `cascade_stats` 在
        # `Ft8Runtime` 上是 `@property`。写成 lambda 的话 `_health` 原样返回一个函数对象，
        # 断言 `["deadline_risk"]` 就变成 `TypeError: 'function' object is not subscriptable`
        # —— 一个从第一行就注定跑不过的用例（R77 的形状）。轻量替身只需长得像**读完之后的结果**。
        runtime=SimpleNamespace(decoder=None, cascade_stats={"deadline_risk": 4}),
    )
    assert _health(state)["cascade"]["deadline_risk"] == 4


def test_cascade_stats_property_covers_both_branches() -> None:
    """`cascade_stats` 的两个分支都要被**这个属性本身**跑到，而不是被替身绕过。

    上面那条用例用的是 SimpleNamespace，证明不了 `Ft8Runtime.cascade_stats` 存在。
    引擎未启动时 `_cascade` 还没有（`object.__new__` 构造出的实例就是这种形状，
    `tests/ft8/test_server_wiring.py:136` 已有先例），必须返回 None 而不是抛
    `AttributeError` —— 否则 `/api/ft8/health` 在引擎没起来时 500。

    用 `getattr(self, "_cascade", None)` 而不是 `self._cascade is None` 就是为了这个：
    Task 11 的 `__init__` 建了 `self._cascade`，但「引擎未启动」这个状态在真实运行里
    是存在的（构造到 `start()` 之间、以及构造路径中途失败），不能只靠 `__init__` 兜。
    """

    from ft8.runtime import Ft8Runtime

    unwired = object.__new__(Ft8Runtime)
    assert unwired.cascade_stats is None

    wired = object.__new__(Ft8Runtime)
    wired._cascade = SimpleNamespace(deadline_risk=4)
    assert wired.cascade_stats == {"deadline_risk": 4}


def test_bucket_bounds_track_the_decision_cutoff() -> None:
    """直方图的量程必须**跟着截止走** —— 这正是它存在的理由。

    `latency.py` 的模块 docstring 说桶对齐到 I9 决策截止，那就不能只对齐一次：
    截止从 2.5 抬到 4.0 之后，若最后一个边界还停在 2.5，任何落在 2.5–4.0 s 的
    解码都掉进溢出桶 —— 一个 2.6 s 与一个 3.9 s 的解码读数完全一样，而它们
    离截止的远近截然不同。断言两个**常量**的关系，而不是写死 4.0。
    """

    assert BUCKET_BOUNDS_SECONDS[-1] == DEFAULT_DECISION_CUTOFF_SECONDS, (
        f"latency buckets top out at {BUCKET_BOUNDS_SECONDS[-1]}s but the I9 "
        f"decision cutoff is {DEFAULT_DECISION_CUTOFF_SECONDS}s; decodes in "
        f"between would be indistinguishable in the overflow bucket"
    )


def test_elapsed_between_the_old_and_new_cutoff_is_not_overflow() -> None:
    """2.5–4.0 s 之间的解码必须落在**正常桶**里，且彼此可分辨。"""

    histogram = LatencyHistogram()
    histogram.record("improved", 3, 3, 2.6)
    histogram.record("improved", 3, 3, 3.9)

    buckets = histogram.snapshot()["improved/p3/t3"]["buckets"]
    overflow_label = next(label for label in buckets if label.startswith(">="))
    assert buckets[overflow_label] == 0, f"2.6s/3.9s must not be overflow: {buckets}"
    assert bucket_index(2.6) != bucket_index(3.9)
