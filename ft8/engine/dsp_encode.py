"""Production TX encoder: message → shared memory → supervised Worker.

Mirror of ``dsp_decode.SupervisorDecoder`` for the Protocol v1 ``encode``
request (SDD §10, §11.4): one reusable caller-owned 606,720-sample float32
segment carries the 48 kHz waveform back; the blocking supervisor round
trip runs in ``asyncio.to_thread`` so the engine loop never stalls.

Unlike ``decode``, the Protocol v1 ``encode`` frame carries no slot or
deadline metadata (SDD §10: message/frequency/48 kHz only); ``slot_id``
is caller-side correlation for the engine's TX bookkeeping, and the
supervisor's per-request IPC timeout bounds the round trip.
"""

from __future__ import annotations

import asyncio
from multiprocessing.shared_memory import SharedMemory

import numpy as np

from ft8.core.supervisor import WorkerSupervisor

TX_SAMPLE_RATE = 48_000
TX_SAMPLES = 606_720
TX_NBYTES = TX_SAMPLES * 4
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0


class TxEncodeError(Exception):
    """The Worker returned a sanitized application-level error frame."""

    __slots__ = ("code", "detail")

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class SupervisorEncoder:
    """Encode standard FT8 messages through the supervised Worker.

    A single TX segment is reused across requests: the supervisor
    serializes every request, and the Worker maps it, closes it and never
    unlinks it.  Call :meth:`close` (or use the context manager) to
    release the segment.

    Callers must not overlap :meth:`encode` calls: the class does not
    enforce serialization; the engine awaits encodes sequentially.
    """

    def __init__(
        self,
        supervisor: WorkerSupervisor,
        *,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if request_timeout <= 0:
            raise ValueError("request timeout must be positive")
        self._supervisor = supervisor
        self._request_timeout = request_timeout
        self._shm: SharedMemory | None = None

    def _ensure_segment(self) -> SharedMemory:
        if self._shm is None:
            self._shm = SharedMemory(create=True, size=TX_NBYTES)
        return self._shm

    async def encode(
        self, message: str, frequency: float, *, slot_id: int = 0
    ) -> np.ndarray:
        """Encode one message; returns a caller-owned float32 waveform copy.

        ``slot_id`` is accepted for caller-side slot correlation only; it
        does not enter the Protocol v1 wire frame.  Calls must not overlap;
        the engine awaits encodes sequentially.
        """

        shm = self._ensure_segment()
        frame = {
            "type": "encode",
            "message": message,
            "frequency": frequency,
            "sample_rate": TX_SAMPLE_RATE,
            "shm": {
                "name": shm.name,
                "dtype": "<f4",
                "shape": [TX_SAMPLES],
                "nbytes": TX_NBYTES,
            },
        }
        response = await asyncio.to_thread(
            self._supervisor.request, frame, self._request_timeout
        )
        if response["type"] == "error":
            raise TxEncodeError(str(response["code"]), str(response["detail"]))
        if response["type"] != "encode_ok":
            raise TxEncodeError(
                "unexpected_response",
                f"worker answered encode with {response['type']}",
            )
        return np.frombuffer(shm.buf[:TX_NBYTES], dtype="<f4").astype(np.float32)

    def close(self) -> None:
        """Close and unlink the shared TX segment; idempotent."""

        if self._shm is not None:
            shm, self._shm = self._shm, None
            shm.close()
            shm.unlink()

    def __enter__(self) -> SupervisorEncoder:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
