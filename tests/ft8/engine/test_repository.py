"""Persistence regressions: canonical QSO, audited void, retention (AD-014)."""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from ft8.engine.repository import (
    AUDIT_RETENTION_S,
    DECODE_RETENTION_S,
    QsoStatus,
    Repository,
    VoidWindowExpired,
)
from ft8.engine.sequencer import QSORecord


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def sample_record(dx_call: str = "K1ABC") -> QSORecord:
    return QSORecord(
        my_call="M0XX",
        my_grid="IO91",
        dx_call=dx_call,
        dx_grid="FN42",
        report_sent=-12,
        report_rcvd=-8,
        started_utc="120000",
        mode="FT8",
        freq_hz=14_074_000,
        band="20m",
    )


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture()
def repo(clock: FakeClock) -> Repository:
    return Repository(":memory:", clock=clock)


def test_fresh_database_migrates_and_reopen_preserves(tmp_path: Path) -> None:
    path = str(tmp_path / "qso.db")
    clock = FakeClock()
    first = Repository(path, clock=clock)
    qso_id = first.record_qso(sample_record())
    first.close()

    second = Repository(path, clock=clock)
    stored = second.get_qso(qso_id)
    assert stored is not None
    assert stored.dx_call == "K1ABC"
    assert stored.status is QsoStatus.COMPLETED
    second.close()


def test_record_qso_round_trip_all_fields(repo: Repository, clock: FakeClock) -> None:
    qso_id = repo.record_qso(sample_record())
    stored = repo.get_qso(qso_id)
    assert stored is not None
    assert (stored.my_call, stored.my_grid) == ("M0XX", "IO91")
    assert (stored.dx_call, stored.dx_grid) == ("K1ABC", "FN42")
    assert (stored.report_sent, stored.report_rcvd) == (-12, -8)
    assert (stored.mode, stored.freq_hz, stored.band) == ("FT8", 14_074_000, "20m")
    assert stored.completed_epoch == clock.now
    assert stored.void_actor is None
    events = repo.qso_events(qso_id)
    assert [e[1] for e in events] == ["completed"]


def test_audited_void_within_window(repo: Repository, clock: FakeClock) -> None:
    qso_id = repo.record_qso(sample_record())
    clock.advance(15.0)  # inside the 30 s undo window
    repo.void_qso(qso_id, actor="session-abc", reason="wrong grid logged")

    stored = repo.get_qso(qso_id)
    assert stored is not None
    assert stored.status is QsoStatus.VOID
    assert stored.void_actor == "session-abc"
    assert stored.void_reason == "wrong grid logged"
    # the void QSO stays auditable: trail plus audit entry
    assert [e[1] for e in repo.qso_events(qso_id)] == ["completed", "void"]
    assert repo.count_rows("audit_event") == 1
    # ...but hidden from the non-void listing used for ADIF
    assert repo.list_qsos(include_void=False) == []
    assert len(repo.list_qsos(include_void=True)) == 1


def test_fresh_database_has_source_column(repo: Repository) -> None:
    qso_id = repo.record_qso(sample_record())
    assert repo.get_qso(qso_id).source == "live"  # type: ignore[union-attr]


def test_v1_database_migrates_to_v2_preserving_rows(tmp_path: Path) -> None:
    """An existing v1 store (no source column) migrates to v2 in place."""

    path = str(tmp_path / "legacy.db")
    # Build a v1 schema exactly as v1 shipped (no source column).
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE qso (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            my_call TEXT NOT NULL, my_grid TEXT NOT NULL,
            dx_call TEXT NOT NULL, dx_grid TEXT NOT NULL DEFAULT '',
            report_sent INTEGER, report_rcvd INTEGER,
            started_utc TEXT NOT NULL DEFAULT '', mode TEXT NOT NULL DEFAULT 'FT8',
            freq_hz INTEGER NOT NULL DEFAULT 0, band TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, completed_epoch REAL NOT NULL,
            void_actor TEXT, void_reason TEXT
        );
        INSERT INTO qso (my_call, my_grid, dx_call, status, completed_epoch)
        VALUES ('M0XX', 'IO91', 'K1ABC', 'completed', 1700000000.0);
        PRAGMA user_version = 1;
        """
    )
    conn.close()

    repo = Repository(path, clock=clock)
    stored = repo.get_qso(1)
    assert stored is not None
    assert stored.source == "live"  # default for pre-existing rows
    assert stored.dx_call == "K1ABC"  # row survived the migration
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    conn.close()
    repo.close()


def test_import_qsos_bulk_sets_source_and_epoch(repo: Repository) -> None:
    records = [
        (sample_record("K1ABC"), 1700000000.0),
        (sample_record("JA1YAD"), 1700000100.0),
    ]
    assert repo.import_qsos(records) == 2
    first = repo.get_qso(1)
    assert first is not None
    assert first.source == "jtdx"
    assert first.completed_epoch == 1700000000.0
    assert repo.count_rows("qso") == 2


def test_dedupe_keys_cross_source(repo: Repository) -> None:
    repo.record_qso(sample_record("LIVE1"))  # source=live
    repo.import_qsos([(sample_record("JTDX1"), 1700000000.0)])  # source=jtdx
    keys = repo.dedupe_keys()
    # 1700000000 = 2023-11-14 22:13:20 UTC -> utc date 20231114
    assert ("LIVE1", "20231114", "120000", "20m") in keys
    assert ("JTDX1", "20231114", "120000", "20m") in keys
    assert len(keys) == 2


def test_list_qsos_since_days_windows(repo: Repository, clock: FakeClock) -> None:
    clock.now = 1_800_000_000.0  # fixed reference
    repo.record_qso(sample_record("FRESH"), completed_epoch=clock.now)
    repo.record_qso(sample_record("OLD"), completed_epoch=clock.now - 8 * 86_400)
    recent = repo.list_qsos(since_days=7)
    assert [q.dx_call for q in recent] == ["FRESH"]
    assert len(repo.list_qsos()) == 2  # no window = all
    assert len(repo.list_qsos(include_void=False)) == 2  # existing kwargs intact


def test_void_after_window_is_refused(repo: Repository, clock: FakeClock) -> None:
    qso_id = repo.record_qso(sample_record())
    clock.advance(31.0)
    with pytest.raises(VoidWindowExpired):
        repo.void_qso(qso_id, actor="session-abc", reason="too late")
    assert repo.get_qso(qso_id).status is QsoStatus.COMPLETED  # type: ignore[union-attr]


def test_void_rejects_non_completed_and_unknown(repo: Repository) -> None:
    qso_id = repo.record_qso(sample_record(), status=QsoStatus.ACTIVE)
    with pytest.raises(ValueError, match="completed"):
        repo.void_qso(qso_id, actor="a", reason="r")
    with pytest.raises(KeyError):
        repo.void_qso(999, actor="a", reason="r")


def test_double_void_is_refused(repo: Repository, clock: FakeClock) -> None:
    qso_id = repo.record_qso(sample_record())
    clock.advance(5.0)
    repo.void_qso(qso_id, actor="a", reason="r")
    with pytest.raises(ValueError, match="completed"):
        repo.void_qso(qso_id, actor="a", reason="again")


def test_abort_active_qsos_marks_aborted_restart(repo: Repository) -> None:
    active = repo.record_qso(sample_record("W1AW"), status=QsoStatus.ACTIVE)
    done = repo.record_qso(sample_record())
    assert repo.abort_active_qsos() == 1
    assert repo.get_qso(active).status is QsoStatus.ABORTED_RESTART  # type: ignore[union-attr]
    assert repo.get_qso(done).status is QsoStatus.COMPLETED  # type: ignore[union-attr]
    assert [e[1] for e in repo.qso_events(active)] == ["active", "aborted_restart"]
    assert repo.abort_active_qsos() == 0  # idempotent


def test_retention_deletes_only_expired_decode_and_audit(
    repo: Repository, clock: FakeClock
) -> None:
    clock.now = 1_000_000.0
    repo.record_decode_event(slot_id=1, message="CQ K1ABC FN42", snr_db=-15)
    repo.record_audit(actor="a", operation="login_failure")
    repo.record_qso(sample_record())
    clock.now += DECODE_RETENTION_S + 1  # past decode retention, inside audit
    repo.record_decode_event(slot_id=2, message="CQ W1AW FN31", snr_db=-10)

    deleted = repo.enforce_retention()
    assert deleted == {"decode_event": 1, "audit_event": 0}  # audit lives 90 d
    assert repo.count_rows("decode_event") == 1
    assert repo.count_rows("audit_event") == 1
    assert repo.count_rows("qso") == 1  # QSO data never expires

    clock.now += AUDIT_RETENTION_S  # far past both windows
    assert repo.enforce_retention() == {"decode_event": 1, "audit_event": 1}


def test_settings_round_trip_json(repo: Repository) -> None:
    assert repo.get_setting("profile") is None
    repo.set_setting("profile", 3)
    repo.set_setting("band_table", {"20m": 14_074_000})
    assert repo.get_setting("profile") == 3
    assert repo.get_setting("band_table") == {"20m": 14_074_000}
    repo.set_setting("profile", 1)  # overwrite
    assert repo.get_setting("profile") == 1


def test_count_rows_rejects_unknown_table(repo: Repository) -> None:
    with pytest.raises(ValueError):
        repo.count_rows("qso; DROP TABLE qso")


def test_close_is_idempotent(repo: Repository) -> None:
    repo.close()
    repo.close()


def test_worked_calls_groups_base_and_excludes_void(tmp_path: Path) -> None:
    """worked_calls() feeds the new-DXCC filter: base calls only, void excluded."""

    path = str(tmp_path / "worked.db")
    repo = Repository(path)
    repo.record_qso(QSORecord(my_call="M0XX", my_grid="IO91", dx_call="K1ABC"))
    repo.record_qso(QSORecord(my_call="M0XX", my_grid="IO91", dx_call="K1ABC/P"))
    repo.record_qso(QSORecord(my_call="M0XX", my_grid="IO91", dx_call="JA1YAD"))
    qso_id = repo.record_qso(QSORecord(my_call="M0XX", my_grid="IO91", dx_call="F4XYZ"))
    repo.void_qso(qso_id, actor="t", reason="dup")
    worked = repo.worked_calls()
    assert worked == {"K1ABC", "JA1YAD"}  # suffix stripped, void excluded
    repo.close()


@pytest.mark.skipif(
    os.name == "nt",
    reason="Windows cannot atomically replace an open SQLite file (os.replace on an open file raises WinError 5)",
)
def test_external_db_replace_does_not_wedge_writes(tmp_path: Path) -> None:
    """AD-014: an external process replacing the db file mid-run must not
    permanently wedge the repository.

    Regression for the production incident: ``mrrc-ft8.db`` was swapped out
    under the live server (inode change), and every subsequent write failed
    with ``OperationalError: attempt to write a readonly database``
    (SQLite's DBMOVED guard).  The repository must detect the replaced file
    and transparently reopen it so QSO/audit writes keep flowing.
    """

    path = str(tmp_path / "qso.db")
    repo = Repository(path)
    repo.record_audit(actor="a", operation="first")

    # Simulate the production incident: an external tool atomically replaces
    # the db file (new inode) while the repository still holds the old one.
    # A copy keeps the full schema + data, exactly like the real swap.
    swapped = str(tmp_path / "qso.db.new")
    shutil.copy2(path, swapped)
    os.replace(swapped, path)

    # The old connection now points at an unlinked inode; SQLite refuses to
    # write (DBMOVED).  The repository must recover transparently.
    repo.record_audit(actor="b", operation="after_swap")
    ops = [a["operation"] for a in repo.audit_events()]
    assert "after_swap" in ops
    assert ops[0] == "after_swap"  # newest first


def test_dxcc_dirty_flag_tracks_qso_writes(repo: Repository, clock: FakeClock) -> None:
    assert repo.dxcc_dirty is True  # 初始需要计算
    repo.record_qso(sample_record())
    assert repo.dxcc_dirty is True
    repo.dxcc_dirty = False
    repo.list_qsos()  # 读不置
    assert repo.dxcc_dirty is False
    repo.import_qsos([(sample_record("W6AER"), 1700000000.0)])
    assert repo.dxcc_dirty is True
    repo.dxcc_dirty = False
    repo.void_qso(1, actor="a", reason="dup")
    assert repo.dxcc_dirty is True
