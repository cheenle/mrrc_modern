from __future__ import annotations

import asyncio
from multiprocessing.shared_memory import SharedMemory

import numpy as np
import pytest

from ft8.core.protocol import encode_frame
from ft8.engine.dsp_encode import SupervisorEncoder, TxEncodeError

TX_NBYTES = 606_720 * 4


class FakeSupervisor:
    """Captures the request frame and answers with a scripted response.

    When ``fill`` is given, each request also attaches to the segment
    named in the frame, writes the pattern and closes it (never unlinks —
    the parent owns unlink), exactly like the real Worker.
    """

    def __init__(
        self, response: dict[str, object], fill: np.ndarray | None = None
    ) -> None:
        self.response = response
        self.fill = fill
        self.frames: list[dict[str, object]] = []
        self.timeouts: list[float] = []

    def request(self, frame: dict[str, object], timeout: float) -> dict[str, object]:
        encode_frame({**frame, "v": 1, "generation": 1, "request_id": 1})
        self.frames.append(frame)
        self.timeouts.append(timeout)
        if self.fill is not None:
            descriptor = frame["shm"]
            shm = SharedMemory(name=descriptor["name"], create=False)  # type: ignore[index]
            try:
                np.ndarray((606_720,), dtype="<f4", buffer=shm.buf)[:] = self.fill
            finally:
                shm.close()
        return self.response


def encode_ok(message: str = "CQ M0XX IO91") -> dict[str, object]:
    return {
        "v": 1,
        "type": "encode_ok",
        "generation": 1,
        "request_id": 2,
        "message": message,
        "sample_rate": 48_000,
        "sample_count": 606_720,
    }


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def test_encode_frame_contract_and_waveform_copy() -> None:
    pattern = np.arange(606_720, dtype=np.float32)
    supervisor = FakeSupervisor(encode_ok(), fill=pattern)
    with SupervisorEncoder(supervisor) as encoder:  # type: ignore[arg-type]
        waveform = run(encoder.encode("CQ M0XX IO91", 1500.0, slot_id=42))
        waveform[:] = -1.0  # mutating the copy must not touch the segment
        refreshed = run(encoder.encode("CQ M0XX IO91", 1500.0, slot_id=43))
    frame = supervisor.frames[0]
    assert frame["type"] == "encode"
    assert frame["message"] == "CQ M0XX IO91"
    assert frame["frequency"] == 1500.0
    assert frame["sample_rate"] == 48_000
    assert frame["shm"]["dtype"] == "<f4"
    assert frame["shm"]["shape"] == [606_720]
    assert frame["shm"]["nbytes"] == TX_NBYTES
    assert isinstance(waveform, np.ndarray)
    assert waveform.dtype == np.float32 and waveform.shape == (606_720,)
    assert waveform.base is None  # caller-owned copy, not a segment view
    np.testing.assert_array_equal(refreshed, pattern)


def test_segment_reused_across_requests() -> None:
    supervisor = FakeSupervisor(encode_ok())
    with SupervisorEncoder(supervisor) as encoder:  # type: ignore[arg-type]
        run(encoder.encode("CQ M0XX IO91", 1500.0, slot_id=1))
        run(encoder.encode("M0XX K1ABC -12", 1500.0, slot_id=2))
    names = {f["shm"]["name"] for f in supervisor.frames}
    assert len(names) == 1


def test_error_frame_raises_tx_encode_error() -> None:
    response = {
        "v": 1, "type": "error", "generation": 1, "request_id": 2,
        "code": "dsp_error", "detail": "encode failed",
    }
    supervisor = FakeSupervisor(response)
    with SupervisorEncoder(supervisor) as encoder:  # type: ignore[arg-type]
        with pytest.raises(TxEncodeError) as excinfo:
            run(encoder.encode("BAD", 1500.0, slot_id=1))
    assert excinfo.value.code == "dsp_error"


def test_unexpected_response_type_raises_tx_encode_error() -> None:
    response = {"v": 1, "type": "pong", "generation": 1, "request_id": 2}
    supervisor = FakeSupervisor(response)
    with SupervisorEncoder(supervisor) as encoder:  # type: ignore[arg-type]
        with pytest.raises(TxEncodeError) as excinfo:
            run(encoder.encode("CQ M0XX IO91", 1500.0, slot_id=1))
    assert excinfo.value.code == "unexpected_response"


def test_close_is_idempotent() -> None:
    with SupervisorEncoder(FakeSupervisor(encode_ok())) as encoder:  # type: ignore[arg-type]
        run(encoder.encode("CQ M0XX IO91", 1500.0, slot_id=1))
    encoder.close()
