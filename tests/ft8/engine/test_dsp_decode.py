from __future__ import annotations

import asyncio
from multiprocessing.shared_memory import SharedMemory

import numpy as np
import pytest

from ft8.core.models import DecodeConfig, DecodePath
from ft8.core.protocol import encode_frame
from ft8.engine.dsp_decode import (
    DecodeError,
    SupervisorDecoder,
    slot_utc_hhmmss,
)


class FakeSupervisor:
    """Captures the request frame and answers with a scripted response."""

    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.frames: list[dict[str, object]] = []
        self.timeouts: list[float] = []

    def request(self, frame: dict[str, object], timeout: float) -> dict[str, object]:
        # The decoder supplies type-specific fields; the real supervisor adds
        # correlation fields. Validate the completed frame against Protocol v1.
        encode_frame({**frame, "v": 1, "generation": 1, "request_id": 1})
        self.frames.append(frame)
        self.timeouts.append(timeout)
        return self.response


def decode_ok(slot_id: int, text: str = "CQ K1ABC FN42") -> dict[str, object]:
    return {
        "v": 1,
        "type": "decode_ok",
        "generation": 1,
        "request_id": 2,
        "slot_id": slot_id,
        "path": "improved",
        "results": [
            {
                "slot_id": slot_id,
                "sync": 1.5,
                "snr": -12,
                "dt": 0.1,
                "frequency": 1500.0,
                "text": text,
                "ap_type": 1,
                "quality": 0.9,
                "flags": 0,
            }
        ],
        "overflow": False,
        "elapsed_seconds": 0.25,
        "deadline_missed": False,
    }


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def test_slot_utc_hhmmss_mapping() -> None:
    assert slot_utc_hhmmss(0) == 0
    assert slot_utc_hhmmss(4) == 100          # 00:01:00
    assert slot_utc_hhmmss(480) == 20_000     # 02:00:00
    assert slot_utc_hhmmss(5_759) == 235_945  # 23:59:45
    assert slot_utc_hhmmss(5_760) == 0        # wraps at midnight
    assert slot_utc_hhmmss(2, period=7.5) == 15  # FT4 slot 2 → 00:00:15


def test_decode_builds_schema_valid_frame_and_converts_batch() -> None:
    supervisor = FakeSupervisor(decode_ok(480))
    samples = np.arange(180_000, dtype=np.int16).tobytes()
    with SupervisorDecoder(supervisor, DecodeConfig()) as decoder:
        batch = run(decoder.decode(480, samples))

        assert len(supervisor.frames) == 1
        frame = supervisor.frames[0]
        assert frame["type"] == "decode"
        assert frame["slot_id"] == 480
        shm = frame["shm"]
        assert shm["dtype"] == "<i2" and shm["shape"] == [180_000]
        assert shm["nbytes"] == 360_000
        config = frame["config"]
        assert type(config["path"]) is str and config["path"] == "improved"
        assert config["utc_hhmmss"] == 20_000
        assert config["sample_rate"] == 12_000 and config["sample_count"] == 180_000

        segment = SharedMemory(name=str(shm["name"]), create=False)
        try:
            assert bytes(segment.buf[: len(samples)]) == samples
        finally:
            segment.close()

    assert batch.slot_id == 480
    assert batch.path == DecodePath.IMPROVED
    assert batch.elapsed_seconds == 0.25
    assert batch.deadline_missed is False
    assert batch.overflow is False
    assert len(batch.results) == 1
    item = batch.results[0]
    assert item.text == "CQ K1ABC FN42"
    assert item.snr == -12
    assert item.ap_type == 1
    assert type(item.snr) is int and type(item.sync) is float


def test_error_frame_raises_decode_error() -> None:
    response = {
        "v": 1,
        "type": "error",
        "generation": 1,
        "request_id": 2,
        "code": "dsp_error",
        "detail": "DSP operation failed",
    }
    supervisor = FakeSupervisor(response)
    with SupervisorDecoder(supervisor) as decoder:
        with pytest.raises(DecodeError) as caught:
            run(decoder.decode(1, b"\x00" * 360_000))
    assert caught.value.code == "dsp_error"
    assert "traceback" not in str(caught.value).lower()


def test_wrong_sample_length_is_rejected_before_supervisor() -> None:
    supervisor = FakeSupervisor(decode_ok(1))
    with SupervisorDecoder(supervisor) as decoder:
        with pytest.raises(ValueError):
            run(decoder.decode(1, b"\x00" * 100))
    assert supervisor.frames == []


def test_segment_is_reused_and_close_unlinks() -> None:
    supervisor = FakeSupervisor(decode_ok(1))
    decoder = SupervisorDecoder(supervisor)
    run(decoder.decode(1, b"\x00" * 360_000))
    run(decoder.decode(2, b"\x01" * 360_000))
    names = [frame["shm"]["name"] for frame in supervisor.frames]  # type: ignore[index]
    assert names[0] == names[1]

    decoder.close()
    with pytest.raises(FileNotFoundError):
        SharedMemory(name=str(names[0]), create=False)
    decoder.close()  # idempotent


def test_timeout_and_deadline_are_forwarded() -> None:
    supervisor = FakeSupervisor(decode_ok(1))
    with SupervisorDecoder(supervisor, request_timeout=12.5) as decoder:
        run(decoder.decode(1, b"\x00" * 360_000))
    assert supervisor.timeouts == [12.5]
    deadline = supervisor.frames[0]["deadline_monotonic"]
    assert isinstance(deadline, float)
