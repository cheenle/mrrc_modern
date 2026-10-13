from __future__ import annotations

from ft8.engine.msgparse import parse_message
from ft8.engine.sequencer import (
    DEFAULT_TX_AUDIO_FREQUENCY,
    DisarmReason,
    QSOState,
    QSORecord,
    QsoContext,
    Sequencer,
)

MY_CALL = "N0CALL"
MY_GRID = "FN42"
DX = "K1ABC"


def make(**kwargs: object) -> Sequencer:
    return Sequencer(my_call=MY_CALL, my_grid=MY_GRID, **kwargs)  # type: ignore[arg-type]


def feed(seq: Sequencer, text: str, snr_db: int | None = None) -> None:
    seq.on_message(parse_message(text), snr_db=snr_db)


def test_cq_side_full_qso() -> None:
    captured: list[QSORecord] = []
    seq = make(
        clock=lambda: 1_700_000_000.0,  # 2023-11-14T22:13:20Z
        context=lambda: QsoContext(freq_hz=14_074_000, band="20m"),
    )
    seq.on_qso = captured.append
    seq.start_cq()
    assert seq.state == QSOState.CALLING
    # CQ repeats without a retransmission budget (UC-004).
    for _ in range(10):
        assert seq.next_tx_message() == "CQ N0CALL FN42"

    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    assert seq.state == QSOState.REPORT
    assert seq.dx_call == DX
    assert seq.dx_grid == "FN42"
    assert seq.next_tx_message() == "K1ABC N0CALL -12"

    feed(seq, "N0CALL K1ABC R-05")
    assert seq.state == QSOState.ROGERS
    assert seq.next_tx_message() == "K1ABC N0CALL RR73"

    feed(seq, "N0CALL K1ABC 73")
    assert seq.state == QSOState.DONE
    assert seq.disarm_reason == DisarmReason.COMPLETE
    assert seq.next_tx_message() is None

    assert len(captured) == 1
    record = captured[0]
    assert record.dx_call == DX
    assert record.dx_grid == "FN42"
    assert record.report_sent == -12
    assert record.report_rcvd == -5
    # Snapped to the slot boundary (WSJT-X TIME_ON semantics).
    assert record.started_utc == "221315"
    assert record.freq_hz == 14_074_000
    assert record.band == "20m"


def test_answerer_full_qso_logs_on_rr73_and_finishes_after_one_73() -> None:
    captured: list[QSORecord] = []
    seq = make()
    seq.on_qso = captured.append
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-15)
    assert seq.state == QSOState.REPLYING
    assert seq.next_tx_message() == "K1ABC N0CALL FN42"

    feed(seq, "N0CALL K1ABC -09")
    assert seq.state == QSOState.ROGER_REPORT
    assert seq.next_tx_message() == "K1ABC N0CALL R-15"

    feed(seq, "N0CALL K1ABC RR73")
    assert seq.state == QSOState.SIGNOFF
    # UC-005: RR73 already completes and logs the QSO.
    assert seq.next_tx_message() == "K1ABC N0CALL 73"
    # Partner stayed silent: the logged QSO is over.
    assert seq.next_tx_message() is None
    assert seq.state == QSOState.DONE
    assert seq.disarm_reason == DisarmReason.COMPLETE

    assert len(captured) == 1
    assert captured[0].report_sent == -15
    assert captured[0].report_rcvd == -9


def test_reply_carries_the_opposite_tx_phase() -> None:
    """UC-003 "opposite TX slot": a reply transmits on the parity opposite
    the slot the partner's message was heard in."""

    seq = make()
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-10, tx_phase=1)
    assert seq.tx_phase == 1  # message heard in an even slot -> reply on odd
    assert seq.state == QSOState.REPLYING
    assert seq.next_tx_message() == "K1ABC N0CALL FN42"

    seq2 = make()
    seq2.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-10, tx_phase=0)
    assert seq2.tx_phase == 0  # message heard in an odd slot -> reply on even


def test_start_cq_resets_tx_phase_to_even() -> None:
    """The CQ loop always transmits on the default (even) phase."""

    seq = make()
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-10, tx_phase=1)
    assert seq.tx_phase == 1
    seq.start_cq()
    assert seq.tx_phase == 0
    assert seq.state == QSOState.CALLING


def test_answerer_resends_73_only_when_partner_repeats_rr73() -> None:
    seq = make()
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-10)
    feed(seq, "N0CALL K1ABC -09")
    feed(seq, "N0CALL K1ABC RR73")
    assert seq.next_tx_message() == "K1ABC N0CALL 73"

    feed(seq, "N0CALL K1ABC RR73")  # partner did not copy our 73
    assert seq.next_tx_message() == "K1ABC N0CALL 73"
    assert seq.next_tx_message() is None
    assert seq.state == QSOState.DONE


def test_retry_exhaustion_disarms_and_retains_context() -> None:
    captured: list[QSORecord] = []
    seq = make()
    seq.on_qso = captured.append
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-10)
    # One initial send plus three retransmissions (NFR-055).
    for _ in range(4):
        assert seq.next_tx_message() == "K1ABC N0CALL FN42"
    assert seq.next_tx_message() is None
    assert seq.state == QSOState.IDLE
    assert seq.disarm_reason == DisarmReason.RETRY_EXHAUSTED
    assert seq.dx_call == DX  # context retained for the operator
    assert len(captured) == 0  # retry exhaustion never fires on_qso


def test_repeated_report_from_partner_resets_rogers_budget() -> None:
    captured: list[QSORecord] = []
    seq = make()
    seq.on_qso = captured.append
    seq.start_cq()
    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    feed(seq, "N0CALL K1ABC R-05")
    assert seq.state == QSOState.ROGERS
    for _ in range(3):
        assert seq.next_tx_message() == "K1ABC N0CALL RR73"
    # Partner repeats R-05 (did not copy our RR73): budget restarts.
    feed(seq, "N0CALL K1ABC R-05")
    assert seq.state == QSOState.ROGERS
    for _ in range(4):
        assert seq.next_tx_message() == "K1ABC N0CALL RR73"
    assert seq.next_tx_message() is None
    assert seq.disarm_reason == DisarmReason.RETRY_EXHAUSTED
    assert len(captured) == 0  # retry exhaustion never fires on_qso


def test_partner_calling_someone_else_auto_stops() -> None:
    seq = make()
    seq.start_cq()
    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    assert seq.state == QSOState.REPORT

    feed(seq, "W9XYZ K1ABC FN42")  # partner turned to another station
    assert seq.state == QSOState.IDLE
    assert seq.disarm_reason == DisarmReason.PARTNER_LOST
    assert seq.next_tx_message() is None


def test_third_station_addressing_us_is_ignored_mid_qso() -> None:
    seq = make()
    seq.start_cq()
    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    feed(seq, "N0CALL W9XYZ -12")  # addressed to us, but not the partner
    assert seq.state == QSOState.REPORT
    assert seq.dx_call == DX


def test_free_text_and_unaddressed_messages_are_ignored() -> None:
    seq = make()
    seq.start_cq()
    feed(seq, "TNX BOB")
    feed(seq, "W9XYZ JA1AAA -12")
    feed(seq, "CQ W9XYZ EM57")
    assert seq.state == QSOState.CALLING
    assert seq.dx_call == ""


def test_manual_stop_retains_context_and_blocks_tx() -> None:
    seq = make()
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-10)
    seq.stop()
    assert seq.state == QSOState.IDLE
    assert seq.disarm_reason == DisarmReason.MANUAL
    assert seq.dx_call == DX
    assert seq.next_tx_message() is None


def test_rrr_after_report_advances_to_rogers() -> None:
    seq = make()
    seq.start_cq()
    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    feed(seq, "N0CALL K1ABC RRR")
    assert seq.state == QSOState.ROGERS
    assert seq.next_tx_message() == "K1ABC N0CALL RR73"


def test_cq_side_accepts_rr73_shortcut_after_report() -> None:
    captured: list[QSORecord] = []
    seq = make()
    seq.on_qso = captured.append
    seq.start_cq()
    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    feed(seq, "N0CALL K1ABC RR73")
    assert seq.state == QSOState.SIGNOFF
    assert seq.next_tx_message() == "K1ABC N0CALL 73"
    assert seq.next_tx_message() is None
    assert seq.state == QSOState.DONE
    assert len(captured) == 1
    assert captured[0].report_rcvd is None


def test_done_is_terminal() -> None:
    seq = make()
    seq.start_cq()
    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    feed(seq, "N0CALL K1ABC R-05")
    feed(seq, "N0CALL K1ABC 73")
    assert seq.state == QSOState.DONE
    feed(seq, "N0CALL K1ABC 73")
    assert seq.next_tx_message() is None
    assert seq.state == QSOState.DONE


def test_report_formatting_clamps_to_protocol_range() -> None:
    seq = make()
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=99)
    feed(seq, "N0CALL K1ABC -09")
    assert seq.next_tx_message() == "K1ABC N0CALL R+50"

    seq2 = make()
    seq2.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-99)
    feed(seq2, "N0CALL K1ABC -09")
    assert seq2.next_tx_message() == "K1ABC N0CALL R-50"


def test_partner_grid_is_captured_from_first_reply() -> None:
    seq = make()
    seq.start_cq()
    feed(seq, "N0CALL K1ABC EM57", snr_db=-12)
    assert seq.dx_grid == "EM57"
    record_source = seq.next_tx_message()
    assert record_source == "K1ABC N0CALL -12"


def test_missing_grid_stays_empty_through_completion() -> None:
    captured: list[QSORecord] = []
    seq = make()
    seq.on_qso = captured.append
    seq.reply_to(parse_message("CQ K1ABC"), snr_db=-10)
    feed(seq, "N0CALL K1ABC -09")
    feed(seq, "N0CALL K1ABC RR73")
    assert seq.next_tx_message() == "K1ABC N0CALL 73"
    assert seq.next_tx_message() is None
    assert len(captured) == 1
    assert captured[0].dx_grid == ""


def drive_cq_qso(sequencer: Sequencer) -> None:
    """CQ side full exchange mirroring test_cq_side_full_qso (ends DONE)."""
    sequencer.start_cq()
    feed(sequencer, "N0CALL K1ABC FN42", snr_db=-12)
    feed(sequencer, "N0CALL K1ABC R-05", snr_db=-5)
    feed(sequencer, "N0CALL K1ABC 73", snr_db=-5)


def test_completed_qso_fires_on_qso_exactly_once() -> None:
    captured: list[QSORecord] = []
    seq = make()
    seq.on_qso = captured.append
    drive_cq_qso(seq)
    assert len(captured) == 1
    assert captured[0].dx_call == "K1ABC"


def test_log_record_survives_immediate_reply_reset() -> None:
    # The observed bug: reply_to()/_reset_partner() wiped a completed-but-
    # unlogged record.  With on_qso fired at completion the record is already
    # out of sequencer state.
    captured: list[QSORecord] = []
    seq = make()
    seq.on_qso = captured.append
    drive_cq_qso(seq)
    seq.reply_to(parse_message("CQ W1AW FN42"), snr_db=-10)  # new QSO
    assert len(captured) == 1
    assert captured[0].dx_call == "K1ABC"


def test_log_record_carries_start_time_and_context() -> None:
    captured: list[QSORecord] = []
    seq = make(
        clock=lambda: 1_700_000_000.0,  # 2023-11-14T22:13:20Z
        context=lambda: QsoContext(freq_hz=14_074_000, band="20m"),
    )
    seq.on_qso = captured.append
    drive_cq_qso(seq)
    record = captured[0]
    # Snapped to the slot boundary: 22:13:20 is 5 s into the 22:13:15 slot.
    assert record.started_utc == "221315"
    assert record.freq_hz == 14_074_000
    assert record.band == "20m"


# ---- TX audio offset follows the partner (UC-003, regression: TN8GD) ----

def test_reply_to_records_partner_tx_frequency() -> None:
    """A reply must leave on the audio offset the partner was heard on, so
    the partner's receiver pairs it (WSJT-X split behaviour)."""

    seq = make()
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-10, tx_frequency=843.0)
    assert seq.tx_frequency == 843.0


def test_reply_to_defaults_tx_frequency_when_omitted() -> None:
    """Older callers without the partner offset keep the historical 1500 Hz."""

    seq = make()
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-10)
    assert seq.tx_frequency == DEFAULT_TX_AUDIO_FREQUENCY


def test_start_cq_resets_tx_frequency_to_default() -> None:
    """A fresh CQ never inherits the previous QSO's partner offset."""

    seq = make()
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-10, tx_frequency=843.0)
    seq.start_cq()
    assert seq.tx_frequency == DEFAULT_TX_AUDIO_FREQUENCY


def test_operator_clicked_reply_keeps_retrying_when_partner_turns_away() -> None:
    """A manually-clicked reply must NOT auto-stop when the partner calls
    another station — it keeps retrying within the NFR-055 budget (field
    request 2026-08-10: the reply was being killed by PARTNER_LOST the moment
    the target worked someone else in a pileup)."""

    seq = make()
    seq.reply_to(parse_message("CQ K1ABC FN42"), snr_db=-10)
    assert seq.state == QSOState.REPLYING

    # The target calls a third station (not us) — must NOT disarm.
    feed(seq, "W9XYZ K1ABC FN42")
    assert seq.state == QSOState.REPLYING
    assert seq.tx_enabled is True
    assert seq.disarm_reason is None

    # The reply keeps being scheduled within the retransmission budget.
    reply_text = f"K1ABC {MY_CALL} {MY_GRID}"
    for _ in range(4):  # NFR-055: one initial send plus three retransmissions
        assert seq.next_tx_message() == reply_text
    assert seq.next_tx_message() is None  # budget exhausted
    assert seq.disarm_reason == DisarmReason.RETRY_EXHAUSTED


def run_full_qso_at(epoch: float) -> QSORecord:
    captured: list[QSORecord] = []
    seq = make(clock=lambda: epoch)
    seq.on_qso = captured.append
    seq.start_cq()
    feed(seq, "N0CALL K1ABC FN42", snr_db=-12)
    feed(seq, "N0CALL K1ABC R-05")
    feed(seq, "N0CALL K1ABC 73")
    assert captured, "QSO did not complete"
    return captured[0]


def test_qso_started_utc_snaps_to_the_slot_boundary() -> None:
    """TIME_ON must be the slot boundary, matching WSJT-X semantics.

    WSJT-X stores ``now - fmod(now.second(), TRperiod)`` (mainwindow.cpp
    ``m_dateTimeQSOOn``): the start of the slot the operator was in when the
    QSO began, not the raw wall clock, which would sit up to 14 s into the
    slot and fail to match WSJT-X/JTDX logs for the same contact.
    """

    # 22:13:46 UTC — one second into the 22:13:45 slot.
    record = run_full_qso_at(1_700_000_026.0)
    assert record.started_utc == "221345"        # not "221346"


def test_qso_started_utc_snaps_at_every_offset_in_the_slot() -> None:
    """Any second inside a slot maps to the same TIME_ON."""

    base = 1_700_000_025.0                       # 22:13:45 slot start
    for offset in (0.0, 3.0, 7.5, 10.0, 14.9):
        assert run_full_qso_at(base + offset).started_utc == "221345"
