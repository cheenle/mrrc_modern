"""JTDX wsjtx_log.adi → canonical store incremental import (AD-014 store).

WSJT-X/JTDX append every completed QSO to the ADIF export live, so the
parser must tolerate a half-written trailing line (no ``<eor>`` yet) and
malformed records.  Dedupe key ``(dx_call, utc date, started_utc, band)``
collapses JTDX's same-second duplicate attempts and keeps re-syncs
idempotent; cross-source by default so a live QSO is never re-imported.
Records are additionally matched against stored rows by the sync's
completion-time window (dx_call + equal-or-empty band + ±120 s), so a QSO
that RUMLogNG already holds is never re-imported even when the ADIF
``TIME_ON`` differs from the stored ``started_utc`` by a minute or so.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .repository import Repository
from .sequencer import QSORecord

log = logging.getLogger(__name__)

_FIELD_RE = re.compile(r"<([a-z_]+):\d+>([^<]*)")
_EOH = "<eoh>"
_EOR = "<eor>"


def parse_adif(text: str) -> list[dict[str, str]]:
    """Parse an ADIF export into per-record field dicts (tolerant)."""

    records: list[dict[str, str]] = []
    for line in text.splitlines():
        if _EOH in line or _EOR not in line:
            continue  # header line / half-written trailing record
        fields: dict[str, str] = {}
        for tag, value in _FIELD_RE.findall(line):
            fields[tag] = value.strip()
        if fields:
            records.append(fields)
    return records


def _parse_report(value: str | None) -> int | None:
    """``'+00'`` / ``'-15'`` → int; anything else → None."""

    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _utc_epoch(date_yyyymmdd: str, time_hhmmss: str) -> float:
    """``'20230227'`` + ``'005730'`` → UTC epoch; 0.0 on garbage."""

    try:
        dt = datetime.strptime(f"{date_yyyymmdd} {time_hhmmss}", "%Y%m%d %H%M%S")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def map_record(
    fields: dict[str, str], *, my_call: str, my_grid: str
) -> tuple[QSORecord, float] | None:
    """One ADIF record → ``(QSORecord, completed_epoch)``; None when the
    record lacks a valid call/qso_date/time_on."""

    call = fields.get("call", "").strip()
    qso_date = fields.get("qso_date", "").strip()
    time_on = fields.get("time_on", "").strip()
    if not call or len(qso_date) != 8 or not qso_date.isdigit():
        return None
    if len(time_on) != 6 or not time_on.isdigit():
        return None
    freq_hz = 0
    freq = fields.get("freq", "").strip()
    if freq:
        try:
            freq_hz = round(float(freq) * 1_000_000)
        except ValueError:
            pass
    date_off = fields.get("qso_date_off", qso_date).strip() or qso_date
    time_off = fields.get("time_off", time_on).strip() or time_on
    record = QSORecord(
        my_call=my_call,
        my_grid=my_grid.upper(),
        dx_call=call,
        dx_grid=fields.get("gridsquare", "").strip(),
        report_sent=_parse_report(fields.get("rst_sent")),
        report_rcvd=_parse_report(fields.get("rst_rcvd")),
        started_utc=time_on,
        mode=fields.get("mode", "FT8").strip() or "FT8",
        freq_hz=freq_hz,
        band=fields.get("band", "").strip(),
    )
    return record, _utc_epoch(date_off, time_off)


def dedupe_key(
    record: QSORecord, epoch: float, qso_date: str
) -> tuple[str, str, str, str]:
    """Collision key: same call + UTC date + start second + band = one QSO."""

    if len(qso_date) == 8 and qso_date.isdigit():
        date = qso_date
    elif epoch:
        date = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y%m%d")
    else:
        date = ""
    return (record.dx_call, date, record.started_utc, record.band)


def _near_window(
    index: dict[str, list[tuple[str, float]]],
    dx_call: str,
    band: str,
    epoch: float,
    window: float = 120.0,
) -> bool:
    """True when a stored QSO matches dx_call + band within the window.

    Same predicate as the sync's cross-source dedupe: band equal or empty on
    either side is a wildcard, and completion times within ``window`` seconds
    describe one QSO.
    """

    for stored_band, stored_epoch in index.get(dx_call, ()):
        if (
            (not stored_band or not band or stored_band == band)
            and abs(stored_epoch - epoch) <= window
        ):
            return True
    return False


@dataclass
class SyncReport:
    parsed: int
    inserted: int
    skipped: int
    error: str | None = None


def sync_jtdx_log(
    repository: Repository,
    path: str,
    *,
    my_call: str,
    my_grid: str,
) -> SyncReport:
    """Read + dedupe + import; never raises on file/parse issues.

    Runs on a worker thread via ``asyncio.to_thread`` in main.py; keeps one
    audit row per sync (``jtdx_import``, detail ``inserted=.. skipped=..``).
    """

    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            text = stream.read()
    except OSError as exc:
        return SyncReport(0, 0, 0, error=str(exc))

    records = parse_adif(text)
    existing = repository.dedupe_keys()
    near = repository.qso_epoch_index()
    seen: set[tuple[str, str, str, str]] = set()
    to_insert: list[tuple[QSORecord, float]] = []
    skipped = 0
    for fields in records:
        mapped = map_record(fields, my_call=my_call, my_grid=my_grid)
        if mapped is None:
            skipped += 1
            continue
        record, epoch = mapped
        key = dedupe_key(record, epoch, fields.get("qso_date", ""))
        if (
            key in existing
            or key in seen
            or _near_window(near, record.dx_call, record.band, epoch)
        ):
            skipped += 1
            continue
        seen.add(key)
        near.setdefault(record.dx_call, []).append((record.band, epoch))
        to_insert.append((record, epoch))

    inserted = repository.import_qsos(to_insert) if to_insert else 0
    repository.record_audit(
        actor="system",
        operation="jtdx_import",
        detail=f"inserted={inserted} skipped={skipped}",
    )
    log.info(
        "jtdx sync: parsed=%d inserted=%d skipped=%d", len(records), inserted, skipped
    )
    return SyncReport(len(records), inserted, skipped)
