"""QsoLog durable queue regressions: retry, dead-letter, recover, flush."""

from __future__ import annotations

import asyncio
import sqlite3

from ft8.engine.qso_log import QsoLog
from ft8.engine.repository import Repository
from ft8.engine.sequencer import QSORecord


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def record(dx_call: str = "K1ABC") -> QSORecord:
    return QSORecord(
        my_call="M0XX",
        my_grid="IO91",
        dx_call=dx_call,
        dx_grid="FN42",
        report_sent=-12,
        report_rcvd=-8,
        started_utc="221320",
        mode="FT8",
        freq_hz=14_074_000,
        band="20m",
    )


class FlakyRepo:
    """record_qso fails the first ``fail_first`` calls, then succeeds."""

    def __init__(self, fail_first: int = 0) -> None:
        self.fail_first = fail_first
        self.written: list[QSORecord] = []

    def record_qso(self, qso: QSORecord) -> int:
        if self.fail_first > 0:
            self.fail_first -= 1
            raise sqlite3.OperationalError("database is locked")
        self.written.append(qso)
        return len(self.written)


def test_enqueue_then_drain_persists_once() -> None:
    repo = FlakyRepo()
    qlog = QsoLog(repo)
    qlog.enqueue(record())
    assert qlog.pending == 1
    run(qlog.drain_once())
    assert qlog.pending == 0
    assert repo.written == [record()]


def test_failed_write_retries_without_loss() -> None:
    repo = FlakyRepo(fail_first=2)
    qlog = QsoLog(repo)
    qlog.enqueue(record())
    run(qlog.drain_once())  # attempt 1 fails
    run(qlog.drain_once())  # attempt 2 fails
    assert qlog.pending == 1
    run(qlog.drain_once())  # attempt 3 succeeds
    assert qlog.pending == 0
    assert len(repo.written) == 1


def test_exhausted_attempts_spill_to_journal(tmp_path) -> None:
    repo = FlakyRepo(fail_first=999)
    qlog = QsoLog(repo, pending_path=str(tmp_path / "pending.jsonl"), max_attempts=3)
    qlog.enqueue(record())
    for _ in range(3):
        run(qlog.drain_once())
    assert qlog.pending == 0
    assert tmp_path.joinpath("pending.jsonl").exists()
    assert "K1ABC" in tmp_path.joinpath("pending.jsonl").read_text(encoding="utf-8")


def test_recover_reads_journal_into_queue(tmp_path) -> None:
    repo = FlakyRepo(fail_first=999)
    qlog = QsoLog(repo, pending_path=str(tmp_path / "pending.jsonl"), max_attempts=2)
    qlog.enqueue(record())
    run(qlog.drain_once())
    run(qlog.drain_once())
    assert qlog.pending == 0  # spilled

    fresh = QsoLog(Repository(":memory:"), pending_path=str(tmp_path / "pending.jsonl"))
    assert fresh.recover() == 1
    assert fresh.pending == 1
    run(fresh.drain_once())
    assert fresh.pending == 0
    assert fresh.repository.list_qsos()[0].dx_call == "K1ABC"
    # Journal cleared so a second restart cannot duplicate.
    assert tmp_path.joinpath("pending.jsonl").read_text(encoding="utf-8") == ""


def test_flush_persists_remaining_queue(tmp_path) -> None:
    repo = Repository(":memory:")
    qlog = QsoLog(repo, pending_path=str(tmp_path / "pending.jsonl"))
    qlog.enqueue(record())
    run(qlog.flush())
    assert qlog.pending == 0
    assert len(repo.list_qsos()) == 1


def test_recover_skips_malformed_lines(tmp_path) -> None:
    journal = tmp_path / "pending.jsonl"
    journal.write_text('{"dx_call":"K1ABC","my_call":"M0XX","my_grid":"IO91"}\nnot json\n')
    qlog = QsoLog(Repository(":memory:"), pending_path=str(journal))
    assert qlog.recover() == 1
