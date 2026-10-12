"""Sequencer log record → canonical QSO store, durably (§7.5, UC-005).

The sequencer fires ``on_qso`` the moment a QSO completes; :class:`QsoLog`
owns the record from then on — out of reach of ``reply_to``/``start_cq``
resets.  The composition watchdog drains at most one record per tick;
persistent failures spill to a dead-letter file so a restart can retry
them.  Enqueue/drain/flush run on the event loop thread; the only blocking
call (``repository.record_qso``) is offloaded with ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from .repository import Repository
from .sequencer import QSORecord

log = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_PENDING_PATH = "data/qso-pending.jsonl"

_RECORD_FIELDS = tuple(QSORecord.__dataclass_fields__)


@dataclass
class QsoLog:
    """Durable completed-QSO queue drained to the canonical store."""

    repository: Repository
    pending_path: str = DEFAULT_PENDING_PATH
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    clock: Callable[[], float] = time.time
    _queue: deque[tuple[QSORecord, int]] = field(
        default_factory=deque, repr=False
    )

    @property
    def pending(self) -> int:
        """Records not yet persisted (health snapshot)."""

        return len(self._queue)

    def enqueue(self, record: QSORecord) -> None:
        """Accept one completed QSO from the sequencer's ``on_qso``."""

        self._queue.append((record, 0))

    async def drain_once(self) -> None:
        """Persist at most one record; retry, then spill after max attempts."""

        if not self._queue:
            return
        record, attempts = self._queue[0]
        try:
            await asyncio.to_thread(self.repository.record_qso, record)
        except Exception:
            attempts += 1
            if attempts >= self.max_attempts:
                self._queue.popleft()
                self._journal(record, attempts)
                log.error(
                    "QSO %s to %s spilled to dead-letter after %d attempts",
                    record.my_call,
                    record.dx_call,
                    attempts,
                )
            else:
                self._queue[0] = (record, attempts)
        else:
            self._queue.popleft()

    def recover(self) -> int:
        """Re-queue records journaled by an earlier failed run; clears the file."""

        records = self._read_journal()
        if records:
            log.warning(
                "recovering %d QSO record(s) from dead-letter journal %s",
                len(records),
                self.pending_path,
            )
        self._clear_journal()
        for recovered in records:
            self._queue.append((recovered, 0))
        return len(records)

    async def flush(self) -> None:
        """Best-effort persist of every queued and journaled record."""

        while self._queue:
            record, attempts = self._queue.popleft()
            try:
                await asyncio.to_thread(self.repository.record_qso, record)
            except Exception:
                self._journal(record, attempts)
        for recovered in self._read_journal():
            try:
                await asyncio.to_thread(self.repository.record_qso, recovered)
            except Exception:
                log.warning(
                    "shutdown flush could not persist QSO to %s", recovered.dx_call
                )
        self._clear_journal()

    # ---- internals -----------------------------------------------------

    def _journal(self, record: QSORecord, attempts: int) -> None:
        payload = {key: getattr(record, key) for key in _RECORD_FIELDS}
        payload["attempts"] = attempts
        payload["journaled_epoch"] = self.clock()
        try:
            with open(self.pending_path, "a") as stream:
                stream.write(json.dumps(payload) + "\n")
        except OSError:
            log.exception("could not write dead-letter journal %s", self.pending_path)

    def _read_journal(self) -> list[QSORecord]:
        try:
            with open(self.pending_path) as stream:
                lines = [line for line in stream if line.strip()]
        except FileNotFoundError:
            return []
        records: list[QSORecord] = []
        for line in lines:
            try:
                data = json.loads(line)
                records.append(
                    QSORecord(
                        **{key: data[key] for key in _RECORD_FIELDS if key in data}
                    )
                )
            except (ValueError, TypeError):
                log.warning("skipping malformed dead-letter line: %s", line.strip()[:80])
        return records

    def _clear_journal(self) -> None:
        try:
            with open(self.pending_path, "w"):
                pass
        except OSError:
            log.exception("could not clear dead-letter journal %s", self.pending_path)
