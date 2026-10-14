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
    batch_from_decode_ok,
    decode_config_frame,
    decode_config_from_frame,
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


# --- decode_path: the per-request path/threads override (Task 11) ----------
#
# ``decode`` is a thin wrapper over ``decode_path``; the cascade decoder calls
# ``decode_path`` directly to select the fast or deep WSJT-X path and to pin
# the deep path's thread count.  The frame's ``config`` is the whole
# ``DecodeConfig`` expanded (``asdict``), so ``threads`` is present on every
# request whether or not the caller overrides it — the override only decides
# which value that field carries.


def test_decode_path_fast_sets_the_frame_path() -> None:
    supervisor = FakeSupervisor(decode_ok(480))
    with SupervisorDecoder(supervisor, DecodeConfig()) as decoder:
        run(decoder.decode_path(480, b"\x00" * 360_000, DecodePath.FAST))

    config = supervisor.frames[0]["config"]
    assert config["path"] == "fast"


def test_decode_path_improved_with_threads_overwrites_threads() -> None:
    supervisor = FakeSupervisor(decode_ok(480))
    with SupervisorDecoder(supervisor, DecodeConfig()) as decoder:
        run(
            decoder.decode_path(
                480, b"\x00" * 360_000, DecodePath.IMPROVED, threads=3
            )
        )

    config = supervisor.frames[0]["config"]
    assert config["path"] == "improved"
    assert config["threads"] == 3


def test_decode_path_without_threads_keeps_the_configured_value() -> None:
    """Not passing ``threads`` leaves the configured value alone.

    ``threads`` is in the frame on every request (the whole ``DecodeConfig``
    is expanded into ``config``), so this pins the value, not the key's
    presence: the frame must carry the ``DecodeConfig``'s own ``threads``,
    not a hardcoded default.  The fixture uses a non-default value (7) so the
    assertion proves pass-through rather than "happens to equal 1".
    """

    supervisor = FakeSupervisor(decode_ok(480))
    with SupervisorDecoder(supervisor, DecodeConfig(threads=7)) as decoder:
        run(decoder.decode_path(480, b"\x00" * 360_000, DecodePath.FAST))

    config = supervisor.frames[0]["config"]
    assert config["threads"] == 7


def test_decode_is_a_thin_wrapper_over_decode_path() -> None:
    """``decode`` must not change any behaviour versus the explicit path call."""

    config = DecodeConfig()
    supervisor = FakeSupervisor(decode_ok(480))
    samples = b"\x00" * 360_000
    with SupervisorDecoder(supervisor, config) as decoder:
        via_decode = run(decoder.decode(480, samples))
        via_path = run(decoder.decode_path(480, samples, config.path))

    assert via_decode == via_path
    assert supervisor.frames[0]["config"] == supervisor.frames[1]["config"]


def test_the_config_frame_round_trips_through_the_wire_form() -> None:
    """One serialiser for both transports — a field cannot reach only one of them."""

    config = DecodeConfig(
        path=DecodePath.IMPROVED, profile=3, threads=3, cycles=3,
        my_call="BG7ZHS", dx_call="JA1ABC", dx_grid="PM95", qso_progress=2,
    )
    frame = decode_config_frame(config)
    # Protocol v1 requires a plain str path (StrEnum fails its type check).
    assert frame["path"] == "improved"
    assert decode_config_from_frame(frame) == config


def test_batch_from_decode_ok_copies_every_field_the_worker_reports() -> None:
    batch = batch_from_decode_ok(decode_ok(42, text="CQ JA1ABC PM95"))

    assert (batch.slot_id, batch.path, batch.overflow, batch.deadline_missed) == (
        42, DecodePath.IMPROVED, False, False,
    )
    assert batch.elapsed_seconds == 0.25
    result = batch.results[0]
    assert (result.text, result.snr, result.dt, result.frequency) == (
        "CQ JA1ABC PM95", -12, 0.1, 1500.0,
    )
    assert (result.sync, result.ap_type, result.quality, result.flags) == (1.5, 1, 0.9, 0)


def test_the_worker_frame_is_built_by_the_shared_serialiser() -> None:
    """Pin the mechanism: the local path must use the very builder the cloud does."""

    config = DecodeConfig(profile=2, threads=3, cycles=1)
    supervisor = FakeSupervisor(decode_ok(480))
    with SupervisorDecoder(supervisor, config) as decoder:
        run(decoder.decode_path(480, b"\x00" * 360_000, DecodePath.IMPROVED, threads=3))

    expected = decode_config_frame(
        config.replace(utc_hhmmss=slot_utc_hhmmss(480), path=DecodePath.IMPROVED, threads=3)
    )
    assert supervisor.frames[0]["config"] == expected
