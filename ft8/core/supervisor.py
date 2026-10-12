"""Parent-side supervision of the DSP Worker process.

SDD AD-003, sections 9.6, 10.4 and 11.1: the supervisor is the sole
parent-side owner of the Worker process and its control pipe.  It spawns the
Worker, verifies health with a protocol ping, enforces per-request IPC
timeouts, fails every request closed on crash/corruption and restarts the
Worker with a fresh generation so stale responses from a previous incarnation
are never accepted.  The class is synchronous and serialized by one internal
lock; the asyncio engine drives it through ``asyncio.to_thread``.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from multiprocessing.connection import Connection

from ft8.core.protocol import (
    MAX_CONTROL_FRAME,
    FrameError,
    decode_frame,
    encode_frame,
)
from ft8.core.worker import worker_main

WorkerTarget = Callable[[Connection, int, str | os.PathLike[str] | None], None]

_EXPECTED_RESPONSES = {
    "ping": frozenset({"pong", "error"}),
    "decode": frozenset({"decode_ok", "error"}),
    "encode": frozenset({"encode_ok", "error"}),
}


class SupervisorState(StrEnum):
    """Lifecycle state reported to the engine and safety controller."""

    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class FailureReport:
    """Sanitized description of the most recent Worker fault."""

    code: str
    detail: str
    generation: int


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    """Point-in-time supervision state for health/audit reporting."""

    state: SupervisorState
    generation: int
    restart_count: int
    last_failure: FailureReport | None


class WorkerFault(Exception):
    """One request failed closed because the Worker could not serve it."""

    __slots__ = ("code", "detail", "generation")

    def __init__(self, code: str, detail: str, generation: int) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.generation = generation


class WorkerSupervisor:
    """Spawn, health-check, time-box and restart one DSP Worker.

    ``request()`` returns the Worker's response frame, including sanitized
    application-level ``error`` frames.  Transport and health failures raise
    :class:`WorkerFault` and trigger one bounded restart attempt; the caller
    treats the in-flight request as lost (a missed slot for decodes).

    ``on_transition``, when given, is invoked synchronously with a
    :class:`HealthSnapshot` on every state change and must not call back into
    the supervisor.
    """

    def __init__(
        self,
        library_path: str | os.PathLike[str] | None = None,
        *,
        worker_target: WorkerTarget = worker_main,
        ping_timeout: float = 5.0,
        stop_timeout: float = 10.0,
        on_transition: Callable[[HealthSnapshot], None] | None = None,
    ) -> None:
        if ping_timeout <= 0 or stop_timeout <= 0:
            raise ValueError("supervisor timeouts must be positive")
        self._library_path = library_path
        self._worker_target = worker_target
        self._ping_timeout = ping_timeout
        self._stop_timeout = stop_timeout
        self._on_transition = on_transition

        self._lock = threading.Lock()
        self._state = SupervisorState.STOPPED
        self._generation = 0
        self._restart_count = 0
        self._last_failure: FailureReport | None = None
        self._next_request_id = 0
        self._process: mp.Process | None = None
        self._connection: Connection | None = None

    @property
    def state(self) -> SupervisorState:
        """Current lifecycle state."""

        return self._state

    @property
    def generation(self) -> int:
        """Current Worker generation; increments on every (re)start."""

        return self._generation

    @property
    def restart_count(self) -> int:
        """Number of restart attempts since the initial spawn."""

        return self._restart_count

    def snapshot(self) -> HealthSnapshot:
        """Return an immutable copy of the current supervision state."""

        with self._lock:
            return self._snapshot_locked()

    def start(self) -> None:
        """Spawn the Worker and verify readiness with a protocol ping."""

        with self._lock:
            if self._process is not None:
                raise ValueError("supervisor is already started")
            self._spawn_locked(initial=True)

    def stop(self) -> None:
        """Shut the Worker down gracefully, escalating to kill; idempotent."""

        with self._lock:
            self._stop_locked()

    def ping(self, timeout: float | None = None) -> dict[str, object]:
        """Round-trip one protocol ping, defaulting to the startup timeout."""

        return self.request({"type": "ping"}, timeout or self._ping_timeout)

    def request(self, frame: dict[str, object], timeout: float) -> dict[str, object]:
        """Send one request frame and return the correlated response.

        The frame supplies the type-specific fields; ``v``, ``generation``
        and ``request_id`` are always assigned here.  Worker ``error`` frames
        are returned; transport/health failures raise :class:`WorkerFault`.
        """

        if timeout <= 0:
            raise ValueError("request timeout must be positive")
        frame_type = frame.get("type")
        if frame_type not in _EXPECTED_RESPONSES:
            raise ValueError("frame type is not a supervisor request")
        with self._lock:
            self._ensure_ready_locked()
            request_id = self._allocate_request_id_locked()
            full_frame = {
                **frame,
                "v": 1,
                "generation": self._generation,
                "request_id": request_id,
            }
            raw = encode_frame(full_frame)
            try:
                return self._roundtrip_locked(full_frame, raw, timeout)
            except WorkerFault as fault:
                self._fail_locked(fault)
                raise

    def _snapshot_locked(self) -> HealthSnapshot:
        return HealthSnapshot(
            self._state, self._generation, self._restart_count, self._last_failure
        )

    def _transition_locked(self, state: SupervisorState) -> None:
        if state == self._state:
            return
        self._state = state
        if self._on_transition is not None:
            self._on_transition(self._snapshot_locked())

    def _allocate_request_id_locked(self) -> int:
        self._next_request_id += 1
        return self._next_request_id

    def _ensure_ready_locked(self) -> None:
        if self._state == SupervisorState.READY:
            return
        if self._state == SupervisorState.STOPPED:
            raise WorkerFault(
                "not_running", "supervisor is not running", self._generation
            )
        self._spawn_locked(initial=False)

    def _roundtrip_locked(
        self, frame: dict[str, object], raw: bytes, timeout: float
    ) -> dict[str, object]:
        process = self._process
        connection = self._connection
        assert process is not None and connection is not None
        generation = self._generation
        if not process.is_alive():
            raise WorkerFault(
                "worker_exit",
                f"worker process exited with status {process.exitcode}",
                generation,
            )
        try:
            connection.send_bytes(raw)
        except (OSError, EOFError):
            raise WorkerFault(
                "worker_exit",
                f"worker process exited with status {process.exitcode}",
                generation,
            ) from None
        if not connection.poll(timeout):
            raise WorkerFault(
                "ipc_timeout",
                f"no worker response within {timeout:.3f} s",
                generation,
            )
        try:
            reply_raw = connection.recv_bytes(MAX_CONTROL_FRAME + 1)
        except (OSError, EOFError):
            raise WorkerFault(
                "worker_exit",
                f"worker process exited with status {process.exitcode}",
                generation,
            ) from None
        try:
            reply = decode_frame(reply_raw)
        except FrameError:
            raise WorkerFault(
                "protocol_corruption",
                "worker sent an invalid control frame",
                generation,
            ) from None
        if (
            reply["generation"] != generation
            or reply["request_id"] != frame["request_id"]
        ):
            raise WorkerFault(
                "stale_response",
                "response does not correlate with the current request",
                generation,
            )
        if reply["type"] not in _EXPECTED_RESPONSES[frame["type"]]:  # type: ignore[index]
            raise WorkerFault(
                "unexpected_response",
                f"worker answered {frame['type']} with {reply['type']}",
                generation,
            )
        return reply

    def _fail_locked(self, fault: WorkerFault) -> None:
        self._last_failure = FailureReport(fault.code, fault.detail, fault.generation)
        self._transition_locked(SupervisorState.DEGRADED)
        try:
            self._spawn_locked(initial=False)
        except WorkerFault:
            pass  # stays DEGRADED; the next request retries one bounded spawn

    def _spawn_locked(self, *, initial: bool) -> None:
        if not initial:
            self._restart_count += 1
        self._teardown_locked()
        self._generation += 1
        self._transition_locked(SupervisorState.STARTING)
        try:
            self._spawn_process_locked()
            ping = {
                "v": 1,
                "type": "ping",
                "generation": self._generation,
                "request_id": self._allocate_request_id_locked(),
            }
            try:
                self._roundtrip_locked(ping, encode_frame(ping), self._ping_timeout)
            except WorkerFault as fault:
                raise WorkerFault(
                    "spawn_failed",
                    f"worker did not become ready: {fault.detail}",
                    self._generation,
                ) from None
        except WorkerFault as fault:
            self._teardown_locked()
            self._last_failure = FailureReport(
                fault.code, fault.detail, self._generation
            )
            self._transition_locked(SupervisorState.DEGRADED)
            raise
        except OSError:
            self._teardown_locked()
            detail = "worker process could not be started"
            self._last_failure = FailureReport(
                "spawn_failed", detail, self._generation
            )
            self._transition_locked(SupervisorState.DEGRADED)
            raise WorkerFault("spawn_failed", detail, self._generation) from None
        self._transition_locked(SupervisorState.READY)

    def _spawn_process_locked(self) -> None:
        context = mp.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        process = context.Process(
            target=self._worker_target,
            name=f"mrrc-dsp-worker-g{self._generation}",
            args=(child, self._generation, self._library_path),
            daemon=True,
        )
        try:
            process.start()
        except Exception:
            parent.close()
            child.close()
            raise
        child.close()
        self._process = process
        self._connection = parent

    def _teardown_locked(self) -> None:
        process, connection = self._process, self._connection
        self._process = None
        self._connection = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if process is None:
            return
        if process.is_alive():
            process.terminate()
            process.join(self._stop_timeout)
            if process.is_alive():
                process.kill()
                process.join(self._stop_timeout)
        else:
            process.join(self._stop_timeout)

    def _stop_locked(self) -> None:
        process, connection = self._process, self._connection
        if process is not None and connection is not None and process.is_alive():
            try:
                frame = {
                    "v": 1,
                    "type": "shutdown",
                    "generation": self._generation,
                    "request_id": self._allocate_request_id_locked(),
                }
                connection.send_bytes(encode_frame(frame))
                if connection.poll(self._stop_timeout):
                    connection.recv_bytes(MAX_CONTROL_FRAME + 1)
            except (OSError, EOFError, FrameError):
                pass
            process.join(self._stop_timeout)
            if process.is_alive():
                process.terminate()
                process.join(self._stop_timeout)
            if process.is_alive():
                process.kill()
                process.join(self._stop_timeout)
        elif process is not None:
            process.join(self._stop_timeout)
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        self._process = None
        self._connection = None
        self._transition_locked(SupervisorState.STOPPED)
