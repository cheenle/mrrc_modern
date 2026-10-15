"""Production SlotDecoder: exact slot → shared memory → supervised Worker.

SDD §9.2 steps 5–6 and §11.4: the engine requests DSP only through the
supervisor; exact RX arrays travel in one reusable parent-owned shared-memory
segment described by the fixed Protocol v1 descriptor and never enter the
control frame.  The blocking supervisor round trip runs in
``asyncio.to_thread`` so the engine loop is never stalled by DSP.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import asdict
from multiprocessing.shared_memory import SharedMemory
from typing import Any

from ft8.core.models import (
    DecodeBatch,
    DecodeConfig,
    DecodePath,
    DecodeResult,
)
from ft8.core.supervisor import WorkerSupervisor
from ft8.engine.orchestrator import SLOT_SAMPLES_NBYTES

#: Supplies the a-priori fields a decode should carry *this slot* — keys are
#: ``DecodeConfig`` field names.  A provider, not a value: ``dx_call``/``dx_grid``
#: change mid-QSO, so anything resolved once at startup would go stale (and a
#: stale a-priori is exactly the case docs/ft8-dl-speculative-notes.md §4.3
#: measures).
ApContextProvider = Callable[[], dict[str, object]]

DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0


class DecodeError(Exception):
    """The Worker returned a sanitized application-level error frame."""

    __slots__ = ("code", "detail")

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def slot_utc_hhmmss(slot_id: int, period: float = 15.0) -> int:
    """Return the slot start's UTC time as HHMMSS for the decode config."""

    seconds = int(slot_id * period) % 86_400
    hours, remainder = divmod(seconds, 3_600)
    minutes, secs = divmod(remainder, 60)
    return hours * 10_000 + minutes * 100 + secs


def decode_config_frame(config: DecodeConfig) -> dict[str, object]:
    """Serialise one ``DecodeConfig`` exactly as Protocol v1's ``config`` field.

    One function for both callers — the local worker frame and the cloud
    request — is the mechanism that keeps the two paths from drifting: a field
    added to ``DecodeConfig`` cannot reach only one of them.
    """

    # Protocol v1 requires a plain str path (StrEnum fails its type check).
    return {**asdict(config), "path": str(config.path)}


def decode_config_from_frame(frame: dict[str, object]) -> DecodeConfig:
    """Inverse of :func:`decode_config_frame` — the hub side of the wire."""

    fields = {**frame, "path": DecodePath(str(frame["path"]))}
    return DecodeConfig(**fields)  # type: ignore[arg-type]


def decode_ok_frame(
    batch: DecodeBatch, *, cpu_seconds: float = 0.0
) -> dict[str, object]:
    """Inverse of :func:`batch_from_decode_ok` — for callers that *compose* one.

    The cloud service answers with a union of several decodes, so it cannot
    forward a worker reply verbatim; building the frame here keeps the wire
    shape in the one module that already owns it (``decode_ok`` is validated
    against an exact schema, so a field missed here is a rejected answer).
    """

    return {
        "type": "decode_ok",
        "slot_id": batch.slot_id,
        "path": str(batch.path),
        "results": [
            {
                "slot_id": result.slot_id,
                "sync": result.sync,
                "snr": result.snr,
                "dt": result.dt,
                "frequency": result.frequency,
                "text": result.text,
                "ap_type": result.ap_type,
                "quality": result.quality,
                "flags": result.flags,
            }
            for result in batch.results
        ],
        "overflow": batch.overflow,
        "elapsed_seconds": batch.elapsed_seconds,
        "cpu_seconds": float(cpu_seconds),
        "deadline_missed": batch.deadline_missed,
    }


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


class SupervisorDecoder:
    """SlotDecoder implementation backed by one WorkerSupervisor.

    A single decode segment is reused across requests: the supervisor
    serializes every request, and the Worker maps it read-only, closes it and
    never unlinks it.  Call :meth:`close` (or use the context manager) to
    release the segment.
    """

    def __init__(
        self,
        supervisor: WorkerSupervisor,
        config: DecodeConfig | None = None,
        *,
        period: float = 15.0,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        histogram: Any = None,
        ap_context: ApContextProvider | None = None,
    ) -> None:
        if request_timeout <= 0:
            raise ValueError("request timeout must be positive")
        self._supervisor = supervisor
        self._config = config or DecodeConfig()
        self._period = period
        self._request_timeout = request_timeout
        self._monotonic = monotonic
        self._histogram = histogram
        self._ap_context = ap_context
        self._shm: SharedMemory | None = None

    def _ensure_segment(self) -> SharedMemory:
        if self._shm is None:
            self._shm = SharedMemory(create=True, size=SLOT_SAMPLES_NBYTES)
        return self._shm

    async def decode(self, slot_id: int, samples: bytes) -> DecodeBatch:
        """Decode one exact 12 kHz int16 slot into a native-result batch.

        Thin wrapper over :meth:`decode_path` using the configured path, so the
        two entry points can never diverge.
        """

        return await self.decode_path(slot_id, samples, self._config.path)

    async def decode_path(
        self,
        slot_id: int,
        samples: bytes,
        path: DecodePath,
        *,
        threads: int | None = None,
    ) -> DecodeBatch:
        """Decode one slot, overriding the path and (optionally) the threads.

        The cascade decoder selects the fast path on every request and the
        deep WSJT-X path — at a fixed thread count — only when it escalates.
        ``threads`` stays ``None`` by default: the frame's ``config`` is the
        whole ``DecodeConfig`` expanded, so omitting it must leave the
        configured ``threads`` untouched rather than substitute a default.
        """

        if len(samples) != SLOT_SAMPLES_NBYTES:
            raise ValueError(f"decode requires exactly {SLOT_SAMPLES_NBYTES} bytes")
        shm = self._ensure_segment()
        shm.buf[: len(samples)] = samples
        changes: dict[str, object] = {
            "utc_hhmmss": slot_utc_hhmmss(slot_id, self._period),
            "path": path,
        }
        if threads is not None:
            changes["threads"] = threads
        if self._ap_context is not None:
            # Consulted per slot, never cached: the partner and the QSO stage
            # change while the engine runs.  ``None`` (the default, and every
            # box without MRRC_FT8_AP_A_PRIORI) leaves the frame exactly as
            # before — AP stays enabled-but-starved, which is today's behaviour.
            changes.update(self._ap_context())
        config = self._config.replace(**changes)
        config_frame = decode_config_frame(config)
        frame = {
            "type": "decode",
            "slot_id": slot_id,
            "deadline_monotonic": self._monotonic() + self._request_timeout,
            "shm": {
                "name": shm.name,
                "dtype": "<i2",
                "shape": [180_000],
                "nbytes": SLOT_SAMPLES_NBYTES,
            },
            "config": config_frame,
        }
        started = self._monotonic()
        response = await asyncio.to_thread(
            self._supervisor.request, frame, self._request_timeout
        )
        if response["type"] == "error":
            raise DecodeError(str(response["code"]), str(response["detail"]))
        if self._histogram is not None:
            self._histogram.record(
                str(config.path),
                config.profile,
                config.threads,
                self._monotonic() - started,
            )
        return batch_from_decode_ok(response)

    def close(self) -> None:
        """Close and unlink the shared decode segment; idempotent."""

        if self._shm is not None:
            shm, self._shm = self._shm, None
            shm.close()
            shm.unlink()

    def __enter__(self) -> SupervisorDecoder:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
