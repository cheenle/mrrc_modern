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
    assert bucket_index(2.5) == len(BUCKET_BOUNDS_SECONDS)  # overflow
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
        ">=2.5s": 1,
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
