from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Iterator

os.environ.setdefault("OMP_STACKSIZE", "10M")

import pytest

from ft8.core.protocol import (
    MAX_CONTROL_FRAME,
    FrameError,
    decode_frame,
    encode_frame,
)
from ft8.core.supervisor import (
    SupervisorState,
    WorkerFault,
    WorkerSupervisor,
)

# Sentinel slot IDs scripting the fake Worker below.  The fake never opens
# shared memory, so decode descriptors need only satisfy the frame schema.
SLOT_HANG = 999_999_990
SLOT_CRASH = 999_999_991
SLOT_STALE = 999_999_992
SLOT_CORRUPT = 999_999_993
SLOT_WRONG_TYPE = 999_999_994
SLOT_ERROR = 999_999_995
SLOT_ARM_IGNORE_SHUTDOWN = 999_999_996
SLOT_EXIT_AFTER_REPLY = 999_999_997


def _decode_ok(frame: dict[str, object], slot_id: int, generation: int) -> dict[str, object]:
    return {
        "v": 1,
        "type": "decode_ok",
        "generation": generation,
        "request_id": frame["request_id"],
        "slot_id": slot_id,
        "path": "improved",
        "results": [],
        "overflow": False,
        "elapsed_seconds": 0.0,
        "cpu_seconds": 0.0,
        "deadline_missed": False,
    }


def fake_worker_main(
    connection: object, generation: int, library_path: object
) -> None:
    """Protocol v1 fake Worker scripted by sentinel slot IDs."""

    ignore_shutdown = False
    while True:
        raw = connection.recv_bytes(MAX_CONTROL_FRAME + 1)  # type: ignore[attr-defined]
        frame = decode_frame(raw)
        base = {
            "v": 1,
            "generation": frame["generation"],
            "request_id": frame["request_id"],
        }
        frame_type = frame["type"]
        if frame_type == "shutdown":
            if ignore_shutdown:
                continue
            connection.send_bytes(encode_frame({**base, "type": "stopped"}))  # type: ignore[attr-defined]
            return
        if frame_type == "ping":
            connection.send_bytes(encode_frame({**base, "type": "pong"}))  # type: ignore[attr-defined]
            continue
        if frame_type != "decode":
            continue

        slot_id = frame["slot_id"]
        if slot_id == SLOT_HANG:
            time.sleep(3_600)
        if slot_id == SLOT_CRASH:
            os._exit(1)
        if slot_id == SLOT_CORRUPT:
            connection.send_bytes(b"not json")  # type: ignore[attr-defined]
            continue
        if slot_id == SLOT_STALE:
            reply = _decode_ok(frame, slot_id, generation + 1)
        elif slot_id == SLOT_WRONG_TYPE:
            reply = {**base, "type": "pong"}
        elif slot_id == SLOT_ERROR:
            reply = {**base, "type": "error", "code": "dsp_error", "detail": "DSP operation failed"}
        else:
            if slot_id == SLOT_ARM_IGNORE_SHUTDOWN:
                ignore_shutdown = True
            reply = _decode_ok(frame, slot_id, generation)
        connection.send_bytes(encode_frame(reply))  # type: ignore[attr-defined]
        if slot_id == SLOT_EXIT_AFTER_REPLY:
            os._exit(1)


def dying_worker_main(
    connection: object, generation: int, library_path: object
) -> None:
    """Worker that never becomes ready (spawn/verify failure injection)."""

    os._exit(1)


def config() -> dict[str, object]:
    return {
        "path": "improved",
        "sample_rate": 12_000,
        "sample_count": 180_000,
        "profile": 3,
        "threads": 1,
        "cycles": 1,
        "sensitivity": 2,
        "ap": True,
        "low_threshold": False,
        "wide_dx": False,
        "hide_duplicates": True,
        "qso_progress": 0,
        "rx_frequency": 1500,
        "tx_frequency": 1500,
        "low_frequency": 200,
        "high_frequency": 3000,
        "ap_width": 50,
        "utc_hhmmss": 120000,
        "my_call": "N0CALL",
        "dx_call": "",
        "dx_grid": "",
    }


def decode_request(slot_id: int) -> dict[str, object]:
    return {
        "type": "decode",
        "slot_id": slot_id,
        "deadline_monotonic": 999_999_999.0,
        "shm": {
            "name": "mrrc_supervisor_test_unused",
            "dtype": "<i2",
            "shape": [180_000],
            "nbytes": 360_000,
        },
        "config": config(),
    }


@pytest.fixture()
def supervisor_factory() -> Iterator[Callable[..., WorkerSupervisor]]:
    created: list[WorkerSupervisor] = []

    def factory(**changes: object) -> WorkerSupervisor:
        options: dict[str, object] = {
            "worker_target": fake_worker_main,
            "ping_timeout": 5.0,
            "stop_timeout": 1.0,
        }
        options.update(changes)
        supervisor = WorkerSupervisor(**options)  # type: ignore[arg-type]
        created.append(supervisor)
        return supervisor

    yield factory
    for supervisor in created:
        supervisor.stop()


def test_start_verifies_ping_and_reports_ready(
    supervisor_factory: Callable[..., WorkerSupervisor],
) -> None:
    supervisor = supervisor_factory()
    supervisor.start()

    assert supervisor.state == SupervisorState.READY
    assert supervisor.generation == 1
    assert supervisor.restart_count == 0

    response = supervisor.ping()
    assert response["type"] == "pong"
    assert response["generation"] == 1

    snapshot = supervisor.snapshot()
    assert snapshot.state == SupervisorState.READY
    assert snapshot.generation == 1
    assert snapshot.restart_count == 0
    assert snapshot.last_failure is None


def test_request_assigns_monotonic_ids_and_returns_decode_ok(
    supervisor_factory: Callable[..., WorkerSupervisor],
) -> None:
    supervisor = supervisor_factory()
    supervisor.start()

    first = supervisor.request(decode_request(7), 5.0)
    assert first["type"] == "decode_ok"
    assert first["generation"] == 1
    assert first["slot_id"] == 7
    assert first["request_id"] >= 2  # the start-verification ping consumed id 1

    second = supervisor.request(decode_request(8), 5.0)
    assert second["slot_id"] == 8
    assert second["request_id"] > first["request_id"]


def test_worker_error_frame_is_returned_without_restart(
    supervisor_factory: Callable[..., WorkerSupervisor],
) -> None:
    supervisor = supervisor_factory()
    supervisor.start()

    response = supervisor.request(decode_request(SLOT_ERROR), 5.0)
    assert response["type"] == "error"
    assert response["code"] == "dsp_error"
    assert supervisor.state == SupervisorState.READY
    assert supervisor.restart_count == 0


def test_invalid_outgoing_frame_raises_without_touching_worker(
    supervisor_factory: Callable[..., WorkerSupervisor],
) -> None:
    supervisor = supervisor_factory()
    supervisor.start()

    with pytest.raises(FrameError):
        supervisor.request({"type": "decode", "slot_id": 1}, 5.0)
    with pytest.raises(ValueError):
        supervisor.request({"type": "shutdown"}, 5.0)
    with pytest.raises(ValueError):
        supervisor.request({"type": "stopped"}, 5.0)
    with pytest.raises(ValueError):
        supervisor.request(decode_request(1), 0.0)

    assert supervisor.state == SupervisorState.READY
    assert supervisor.restart_count == 0
    assert supervisor.ping()["type"] == "pong"


def test_ipc_timeout_fails_closed_and_restarts_with_fresh_generation(
    supervisor_factory: Callable[..., WorkerSupervisor],
) -> None:
    events: list[object] = []
    supervisor = supervisor_factory(on_transition=events.append)
    supervisor.start()

    with pytest.raises(WorkerFault) as caught:
        supervisor.request(decode_request(SLOT_HANG), 0.3)
    fault = caught.value
    assert fault.code == "ipc_timeout"
    assert fault.generation == 1
    assert "traceback" not in str(fault).lower()

    assert supervisor.state == SupervisorState.READY
    assert supervisor.generation == 2
    assert supervisor.restart_count == 1
    assert any(
        event.state == SupervisorState.DEGRADED
        and event.last_failure is not None
        and event.last_failure.code == "ipc_timeout"
        for event in events
    )

    recovered = supervisor.request(decode_request(11), 5.0)
    assert recovered["type"] == "decode_ok"
    assert recovered["generation"] == 2
    # Start ping consumed id 1 and the timed-out request id 2; ids never reset.
    assert recovered["request_id"] > 2


def test_worker_crash_mid_request_is_detected_and_restarted(
    supervisor_factory: Callable[..., WorkerSupervisor],
) -> None:
    supervisor = supervisor_factory()
    supervisor.start()

    with pytest.raises(WorkerFault) as caught:
        supervisor.request(decode_request(SLOT_CRASH), 5.0)
    assert caught.value.code == "worker_exit"
    assert supervisor.state == SupervisorState.READY
    assert supervisor.generation == 2
    assert supervisor.restart_count == 1
    assert supervisor.request(decode_request(21), 5.0)["type"] == "decode_ok"


def test_worker_dead_before_request_is_detected_and_restarted(
    supervisor_factory: Callable[..., WorkerSupervisor],
) -> None:
    supervisor = supervisor_factory()
    supervisor.start()

    reply = supervisor.request(decode_request(SLOT_EXIT_AFTER_REPLY), 5.0)
    assert reply["type"] == "decode_ok"

    # The child may still be exiting: either the pre-send liveness check or the
    # pipe EOF after send must surface the same worker_exit fault.
    with pytest.raises(WorkerFault) as caught:
        supervisor.request(decode_request(31), 5.0)
    assert caught.value.code == "worker_exit"
    assert supervisor.state == SupervisorState.READY
    assert supervisor.generation == 2


def test_stale_generation_response_is_rejected_and_restarted(
    supervisor_factory: Callable[..., WorkerSupervisor],
) -> None:
    supervisor = supervisor_factory()
    supervisor.start()

    with pytest.raises(WorkerFault) as caught:
        supervisor.request(decode_request(SLOT_STALE), 5.0)
    assert caught.value.code == "stale_response"
    assert supervisor.state == SupervisorState.READY
    assert supervisor.generation == 2

    recovered = supervisor.request(decode_request(41), 5.0)
    assert recovered["generation"] == 2


def test_malformed_worker_bytes_trigger_protocol_fault_and_restart(
    supervisor_factory: Callable[..., WorkerSupervisor],
) -> None:
    supervisor = supervisor_factory()
    supervisor.start()

    with pytest.raises(WorkerFault) as caught:
        supervisor.request(decode_request(SLOT_CORRUPT), 5.0)
    assert caught.value.code == "protocol_corruption"
    assert supervisor.state == SupervisorState.READY
    assert supervisor.generation == 2


def test_unexpected_response_type_is_rejected_and_restarted(
    supervisor_factory: Callable[..., WorkerSupervisor],
) -> None:
    supervisor = supervisor_factory()
    supervisor.start()

    with pytest.raises(WorkerFault) as caught:
        supervisor.request(decode_request(SLOT_WRONG_TYPE), 5.0)
    assert caught.value.code == "unexpected_response"
    assert supervisor.state == SupervisorState.READY
    assert supervisor.generation == 2


def test_crash_looping_worker_degrades_after_bounded_restarts(
    supervisor_factory: Callable[..., WorkerSupervisor],
) -> None:
    supervisor = supervisor_factory(worker_target=dying_worker_main, ping_timeout=2.0)

    with pytest.raises(WorkerFault) as caught:
        supervisor.start()
    assert caught.value.code == "spawn_failed"
    assert supervisor.state == SupervisorState.DEGRADED

    with pytest.raises(WorkerFault) as caught_again:
        supervisor.request(decode_request(51), 5.0)
    assert caught_again.value.code == "spawn_failed"
    assert supervisor.state == SupervisorState.DEGRADED
    assert supervisor.restart_count == 1
    assert "traceback" not in str(caught_again.value).lower()


def test_stop_is_graceful_idempotent_and_blocks_requests(
    supervisor_factory: Callable[..., WorkerSupervisor],
) -> None:
    supervisor = supervisor_factory()
    supervisor.start()
    supervisor.stop()

    assert supervisor.state == SupervisorState.STOPPED
    supervisor.stop()
    assert supervisor.state == SupervisorState.STOPPED

    with pytest.raises(WorkerFault) as caught:
        supervisor.request(decode_request(61), 5.0)
    assert caught.value.code == "not_running"

    supervisor.start()
    assert supervisor.state == SupervisorState.READY
    assert supervisor.generation == 2


def test_stop_terminates_unresponsive_worker_without_raising(
    supervisor_factory: Callable[..., WorkerSupervisor],
) -> None:
    supervisor = supervisor_factory(stop_timeout=0.5)
    supervisor.start()
    assert (
        supervisor.request(decode_request(SLOT_ARM_IGNORE_SHUTDOWN), 5.0)["type"]
        == "decode_ok"
    )

    started = time.monotonic()
    supervisor.stop()
    assert time.monotonic() - started < 30.0
    assert supervisor.state == SupervisorState.STOPPED


def test_double_start_is_rejected(
    supervisor_factory: Callable[..., WorkerSupervisor],
) -> None:
    supervisor = supervisor_factory()
    supervisor.start()
    with pytest.raises(ValueError):
        supervisor.start()
    assert supervisor.state == SupervisorState.READY
