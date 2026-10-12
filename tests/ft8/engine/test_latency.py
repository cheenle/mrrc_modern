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
    histogram.record(3, 7, 0.2)
    histogram.record(3, 7, 0.7)
    histogram.record(3, 7, 3.1)
    histogram.record(0, 1, 1.234)

    snapshot = histogram.snapshot()
    assert set(snapshot) == {"p3/t7", "p0/t1"}
    series = snapshot["p3/t7"]
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
    assert snapshot["p0/t1"]["buckets"]["<1.5s"] == 1
    assert snapshot["p0/t1"]["max_seconds"] == 1.234


def test_histogram_rejects_negative_elapsed() -> None:
    with pytest.raises(ValueError, match="negative"):
        LatencyHistogram().record(3, 7, -0.1)


def test_supervisor_decoder_records_wall_elapsed() -> None:
    """A successful decode adds one sample keyed by profile/threads."""

    config = DecodeConfig(profile=3, threads=7)
    histogram = LatencyHistogram()
    samples = np.zeros(180_000, dtype=np.int16).tobytes()
    with SupervisorDecoder(
        FakeSupervisor(480), config, histogram=histogram
    ) as decoder:
        asyncio.run(decoder.decode(480, samples))

    snapshot = histogram.snapshot()
    assert list(snapshot) == ["p3/t7"]
    series = snapshot["p3/t7"]
    assert series["count"] == 1
    assert sum(series["buckets"].values()) == 1


@pytest.mark.skip(reason="web layer arrives with the REST移植 (task 12)")
def test_health_exposes_latency_and_deadline_misses() -> None:
    """NFR-002: health carries the histogram and the deadline-miss counter."""

    histogram = LatencyHistogram()
    histogram.record(3, 7, 0.4)
    state = SimpleNamespace(
        safety=SimpleNamespace(health={"armed": False}),
        lease=LeaseService(),
        auth=AuthService(hash_password("pw")),
        latency=histogram,
        orchestrator=SimpleNamespace(
            counters=SimpleNamespace(deadline_misses=2)
        ),
    )
    health = _health(state)
    assert health["decode_latency"]["p3/t7"]["count"] == 1
    assert health["deadline_misses"] == 2


@pytest.mark.skip(reason="web layer arrives with the REST移植 (task 12)")
def test_health_omits_latency_when_not_wired() -> None:
    state = SimpleNamespace(
        safety=SimpleNamespace(health={"armed": False}),
        lease=LeaseService(),
        auth=AuthService(hash_password("pw")),
        latency=None,
        orchestrator=None,
    )
    health = _health(state)
    assert "decode_latency" not in health
    assert "deadline_misses" not in health
