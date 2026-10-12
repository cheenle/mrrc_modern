"""JTDX ADIF import: tolerant parser + field mapping (spec §3.2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ft8.engine.adif_import import dedupe_key, map_record, parse_adif, sync_jtdx_log
from ft8.engine.repository import Repository
from ft8.engine.sequencer import QSORecord

SAMPLE = (
    "<call:6>BG4UCZ <gridsquare:4>PM02 <mode:3>FT8 <rst_sent:3>+00 "
    "<rst_rcvd:3>+04 <qso_date:8>20230227 <time_on:6>005730 "
    "<qso_date_off:8>20230227 <time_off:6>005829 <band:3>20m "
    "<freq:9>14.075500 <station_callsign:5>BG1SB <my_gridsquare:4>ON80 "
    "<tx_pwr:3>100 <comment:3>1st <eor>"
)


def test_parse_adif_skips_header_and_half_written_trailing_line() -> None:
    text = "WSJT-X ADIF Export<eoh>\n" + SAMPLE + "\n" + "<call:6>HALF<eor-not-yet"
    records = parse_adif(text)
    assert len(records) == 1
    assert records[0]["call"] == "BG4UCZ"


def test_parse_adif_drops_malformed_line() -> None:
    text = "garbage-no-tags<eor>\n" + SAMPLE
    records = parse_adif(text)
    assert len(records) == 1


def test_map_record_full_mapping() -> None:
    fields = parse_adif(SAMPLE)[0]
    mapped = map_record(fields, my_call="BG1SB", my_grid="on80da")
    assert mapped is not None
    record, epoch = mapped
    assert record.my_call == "BG1SB"
    assert record.my_grid == "ON80DA"  # normalized uppercase
    assert record.dx_call == "BG4UCZ"
    assert record.dx_grid == "PM02"
    assert (record.report_sent, record.report_rcvd) == (0, 4)  # '+00' '+04'
    assert record.started_utc == "005730"
    assert record.mode == "FT8"
    assert record.freq_hz == 14_075_500
    assert record.band == "20m"
    assert epoch == 1677459509.0  # 2023-02-27 00:58:29 UTC


def test_map_record_tolerates_missing_optional_fields() -> None:
    fields = {
        "call": "JA1YAD",
        "qso_date": "20230301",
        "time_on": "010203",
        "band": "40m",
    }
    mapped = map_record(fields, my_call="M0XX", my_grid="IO91")
    assert mapped is not None
    record, epoch = mapped
    assert record.dx_grid == ""
    assert record.report_sent is None
    assert record.report_rcvd is None
    assert record.mode == "FT8"
    assert record.freq_hz == 0
    assert epoch == 1677632523.0  # 2023-03-01 01:02:03 UTC


def test_map_record_rejects_unparseable() -> None:
    assert map_record({}, my_call="M0XX", my_grid="IO91") is None
    assert map_record(
        {"call": "K1ABC", "qso_date": "bad", "time_on": "nope"},
        my_call="M0XX",
        my_grid="IO91",
    ) is None  # qso_date/time_on must be valid digits


def test_dedupe_key_uses_adif_date() -> None:
    fields = parse_adif(SAMPLE)[0]
    record, epoch = map_record(fields, my_call="BG1SB", my_grid="ON80DA")
    assert record is not None
    key = dedupe_key(record, epoch, fields["qso_date"])
    assert key == ("BG4UCZ", "20230227", "005730", "20m")


# ---- sync_jtdx_log ------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def repository_fake() -> Repository:
    return Repository(":memory:", clock=_Clock())


def _sample(
    call: str,
    qso_date: str = "20230227",
    time_on: str = "005730",
    time_off: str = "005900",
    band: str = "20m",
    freq: str = "14.075500",
) -> str:
    return (
        f"<call:{len(call)}>{call} <gridsquare:0> <mode:3>FT8 "
        f"<qso_date:8>{qso_date} <time_on:6>{time_on} "
        f"<qso_date_off:8>{qso_date} <time_off:6>{time_off} "
        f"<band:{len(band)}>{band} <freq:{len(freq)}>{freq} "
        f"<station_callsign:5>BG1SB <my_gridsquare:5>ON80DA <eor>"
    )


def test_sync_first_run_inserts_all(tmp_path: Path, repository_fake: Repository) -> None:
    path = tmp_path / "wsjtx_log.adi"
    path.write_text(_sample("K1ABC") + "\n" + _sample("JA1YAD"))
    report = sync_jtdx_log(
        repository_fake, path, my_call="BG1SB", my_grid="ON80DA"
    )
    assert report.parsed == 2
    assert report.inserted == 2
    assert report.skipped == 0
    assert report.error is None
    assert repository_fake.count_rows("qso") == 2


def test_sync_second_run_is_idempotent(tmp_path: Path, repository_fake: Repository) -> None:
    path = tmp_path / "wsjtx_log.adi"
    path.write_text(_sample("K1ABC"))
    sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    report = sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    assert report.inserted == 0
    assert report.skipped == 1
    assert repository_fake.count_rows("qso") == 1


def test_sync_appended_records_import_only_delta(
    tmp_path: Path, repository_fake: Repository
) -> None:
    path = tmp_path / "wsjtx_log.adi"
    path.write_text(_sample("K1ABC"))
    sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    with open(path, "a") as f:
        f.write("\n" + _sample("W1AW"))
    report = sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    assert report.inserted == 1
    assert [r.dx_call for r in repository_fake.list_qsos()] == ["W1AW", "K1ABC"]


def test_sync_collapses_same_second_duplicates(
    tmp_path: Path, repository_fake: Repository
) -> None:
    """JTDX same-second duplicate attempts (different freq) count once."""

    path = tmp_path / "wsjtx_log.adi"
    path.write_text(
        _sample("ON3SGU", freq="21.075500")
        + "\n"
        + _sample("ON3SGU", freq="21.075500").replace("005900", "005859")
    )
    report = sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    assert report.inserted == 1
    assert report.skipped == 1


def test_sync_missing_file_reports_error_not_crash(
    tmp_path: Path, repository_fake: Repository
) -> None:
    report = sync_jtdx_log(
        repository_fake, str(tmp_path / "nope.adi"), my_call="BG1SB", my_grid="ON80DA"
    )
    assert report.error is not None
    assert report.inserted == 0


def test_sync_never_reimports_live_qso(
    tmp_path: Path, repository_fake: Repository
) -> None:
    """Cross-source dedupe: a live-completed QSO in the ADIF is skipped."""

    repository_fake.record_qso(
        QSORecord(
            my_call="BG1SB",
            my_grid="ON80DA",
            dx_call="K1ABC",
            started_utc="005730",
            band="20m",
            freq_hz=14_075_500,
        ),
        completed_epoch=1677459540.0,  # 2023-02-27 00:59:00 UTC (same date)
    )
    path = tmp_path / "wsjtx_log.adi"
    path.write_text(_sample("K1ABC"))
    report = sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    assert report.inserted == 0
    assert repository_fake.count_rows("qso") == 1


def test_sync_midnight_crosser_is_idempotent(
    tmp_path: Path, repository_fake: Repository
) -> None:
    """A QSO starting 23:59:45 and ending 00:00:59 next day must dedupe.

    Regression: ``dedupe_keys`` used to derive the UTC date from
    ``completed_epoch`` while the ADIF side keys on ``qso_date`` (start
    date), so every midnight crosser was re-imported on every sync and
    then re-pushed to RUMLogNG.
    """

    record = (
        "<call:5>CX1FK <gridsquare:0> <mode:3>FT8 "
        "<qso_date:8>20230305 <time_on:6>235945 "
        "<qso_date_off:8>20230306 <time_off:6>000059 "
        "<band:3>20m <freq:9>14.075500 "
        "<station_callsign:5>BG1SB <my_gridsquare:5>ON80DA <eor>"
    )
    path = tmp_path / "wsjtx_log.adi"
    path.write_text(record)
    first = sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    assert first.inserted == 1
    second = sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    assert second.inserted == 0
    assert second.skipped == 1
    assert repository_fake.count_rows("qso") == 1


def test_sync_skips_qso_already_stored_near_completion(
    tmp_path: Path, repository_fake: Repository
) -> None:
    """Cross-source dedupe by completion window (sync predicate).

    Regression: RUMLogNG-pulled rows derive started_utc from the completion
    time, while the ADIF TIME_ON is the real start (~1 min earlier), so the
    (call, date, started_utc, band) key never matched and every hourly sync
    re-imported the QSO as a second row; the sync then pushed that row into
    RUMLogNG, creating duplicates there.
    """

    repository_fake.record_qso(
        QSORecord(
            my_call="BG1SB", my_grid="ON80DA", dx_call="K1ABC",
            started_utc="005959", band="20m", freq_hz=14_075_500,
        ),
        completed_epoch=1677459599.0,  # 2023-02-27 00:59:59 UTC
        source="rumlog",
    )
    path = tmp_path / "wsjtx_log.adi"
    path.write_text(_sample("K1ABC", time_on="005830", time_off="005959"))
    report = sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    assert report.inserted == 0
    assert report.skipped == 1
    assert repository_fake.count_rows("qso") == 1


def test_sync_imports_qso_outside_completion_window(
    tmp_path: Path, repository_fake: Repository
) -> None:
    """The completion-window dedupe must not swallow a genuinely different
    QSO by the same station more than 120 s away."""

    repository_fake.record_qso(
        QSORecord(
            my_call="BG1SB", my_grid="ON80DA", dx_call="K1ABC",
            started_utc="005730", band="20m", freq_hz=14_075_500,
        ),
        completed_epoch=1677459509.0,  # 2023-02-27 00:58:29 UTC
    )
    path = tmp_path / "wsjtx_log.adi"
    path.write_text(_sample("K1ABC", time_on="010130", time_off="010159"))
    report = sync_jtdx_log(repository_fake, path, my_call="BG1SB", my_grid="ON80DA")
    assert report.inserted == 1
    assert repository_fake.count_rows("qso") == 2
