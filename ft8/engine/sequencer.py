"""Single-QSO sequencer state machine for standard FT8/FT4 HF QSOs.

Distilled from the wsjtx-3.0.2 ``widgets/mainwindow.cpp`` auto-sequence rules
with every contest branch removed (SDD AD-012, UC-004/UC-005, chapter 15)::

    CQ side:   CQ → grid reply → report → R+report → RR73 → 73 → log
    answerer:  grid reply → report → R+report → RR73 → 73 → log

Every QSO exchange message gets one initial send plus at most three
retransmissions (NFR-055); exhausting the budget disarms TX and retains the
partner context.  CQ calling repeats until answered or explicitly stopped
(UC-004).  Receiving RR73/73 logs the QSO (UC-005); the answerer still sends
one courtesy 73, repeated only when the partner repeats its final message.
The state machine touches no audio, PTT or network: it maps decoded messages
to the text the next eligible TX slot should carry, and nothing else.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from .msgparse import ParsedMessage, addressed_to, base_call

# Standard-message reports are protocol-limited to -50..+50 dB (wsjtx-3.0.2
# lib/77bit/packjt77.f90: isnr = irpt - 35 with the -50..-31 +101 wrap).
REPORT_MIN, REPORT_MAX = -50, 50
DEFAULT_REPORT_DB = -10

# Audio offset (Hz) used when no partner offset is known — CQ calling and
# legacy clients without ``freq`` (UC-003: a reply otherwise transmits on the
# offset the partner's message was decoded at, WSJT-X split behaviour).
DEFAULT_TX_AUDIO_FREQUENCY = 1500.0


class QSOState(StrEnum):
    """One-QSO sequence phase; maps to WSJT-X Tx1–Tx5 message kinds."""

    IDLE = "idle"
    CALLING = "calling"              # repeating CQ
    REPLYING = "replying"            # Tx1: DX MY GRID
    REPORT = "report"                # Tx2: DX MY -nn
    ROGER_REPORT = "roger_report"    # Tx3: DX MY R-nn
    ROGERS = "rogers"                # Tx4: DX MY RR73
    SIGNOFF = "signoff"              # Tx5: DX MY 73
    DONE = "done"                    # logged and disarmed


class DisarmReason(StrEnum):
    """Why TX was disarmed; drives UI presentation and audit."""

    MANUAL = "manual"
    FAULT = "fault"
    RETRY_EXHAUSTED = "retry_exhausted"
    PARTNER_LOST = "partner_lost"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class QsoContext:
    """Radio context the sequencer records into a completed QSO.

    Injected by the composition root (keeps the sequencer hardware-agnostic);
    ``freq_hz``/``band`` are the dial frequency and ADIF band at QSO start.
    """

    freq_hz: int = 0
    band: str = ""


@dataclass(frozen=True, slots=True)
class QSORecord:
    """Accumulated QSO data for one completed exchange, fed to ADIF logging."""

    my_call: str
    my_grid: str
    dx_call: str
    dx_grid: str = ""
    report_sent: int | None = None
    report_rcvd: int | None = None
    started_utc: str = ""
    mode: str = "FT8"
    freq_hz: int = 0
    band: str = ""


def _fmt_report(snr_db: int) -> str:
    snr_db = max(REPORT_MIN, min(REPORT_MAX, snr_db))
    return f"{snr_db:+03d}"


@dataclass
class Sequencer:
    """One-QSO auto sequencer.

    Feed every addressed-to-me decoded message to :meth:`on_message` once per
    decode cycle, query :meth:`next_tx_message` before each eligible TX slot,
    and drive :meth:`start_cq` / :meth:`reply_to` / :meth:`stop` from explicit
    operator or safety events.
    """

    my_call: str
    my_grid: str
    state: QSOState = QSOState.IDLE
    dx_call: str = ""
    dx_grid: str = ""
    report_sent: int | None = None   # report I owe the partner (their SNR)
    report_rcvd: int | None = None   # report the partner sent me
    tx_enabled: bool = False
    max_retransmissions: int = 3     # NFR-055: one initial send plus three
    disarm_reason: DisarmReason | None = None
    tx_phase: int = 0                # 0 = even slots, 1 = odd (UC-003)
    tx_frequency: float = DEFAULT_TX_AUDIO_FREQUENCY  # audio offset (UC-003)
    _tx_count: int = field(default=0, repr=False)
    _signoff_sent: bool = field(default=False, repr=False)
    # Anti-QRM auto-stop on the partner turning to another station.  CQ-sourced
    # QSOs keep it (the answering station lost interest); an operator-clicked
    # reply (reply_to) disables it — the reply keeps retrying within the
    # NFR-055 budget until the partner answers or the operator stops it.
    _partner_loss_stop: bool = field(default=True, repr=False)
    clock: Callable[[], float] = time.time
    context: Callable[[], QsoContext] = lambda: QsoContext()
    on_qso: Callable[[QSORecord], None] | None = None
    # Fired on every QSO lifecycle end (stop or complete) with the reason.
    # Lets the app clear UI selection state (P7: state.selected leaked and
    # permanently gated band-hunt/auto-call behind `selected is None`).
    on_stop: Callable[[DisarmReason], None] | None = None
    _qso_logged: bool = field(default=False, repr=False)
    _qso_started_epoch: float | None = field(default=None, repr=False)

    # ---- external triggers ------------------------------------------

    def start_cq(self, tx_frequency: float = DEFAULT_TX_AUDIO_FREQUENCY) -> None:
        """Arm repeated CQ calling until a caller answers (UC-004).

        ``tx_frequency`` is the audio offset the CQ transmits on; callers
        pass an unoccupied offset picked near the station default (the
        ``FrequencyOccupancy`` helper) so a CQ does not overlap another
        signal.
        """

        self._reset_partner()
        self.state = QSOState.CALLING
        self.tx_frequency = tx_frequency
        self.tx_enabled = True
        self._partner_loss_stop = True  # CQ-sourced: stop when they turn away

    def reply_to(
        self,
        msg: ParsedMessage,
        snr_db: int,
        *,
        tx_phase: int = 0,
        tx_frequency: float = DEFAULT_TX_AUDIO_FREQUENCY,
    ) -> None:
        """Arm a reply to an operator-selected CQ/call message (UC-003).

        ``tx_phase`` is the parity the reply must transmit on (0 = even slots,
        1 = odd): UC-003 requires the slot opposite the one the partner's
        message was heard in, so the caller passes ``1 - (slot_id % 2)``.
        ``tx_frequency`` is the audio offset the partner was decoded at; the
        reply must leave on that same offset so the partner's receiver pairs
        it (WSJT-X split behaviour), defaulting to the station's CQ offset.
        """

        if not msg.from_call:
            return
        self._reset_partner()
        self._qso_started_epoch = self.clock()
        self.tx_phase = tx_phase
        self.tx_frequency = tx_frequency
        self.dx_call = msg.from_call
        self.dx_grid = msg.grid
        self.report_sent = snr_db
        self.state = QSOState.REPLYING
        self.tx_enabled = True
        # Operator intent: a targeted reply persists even when the partner
        # turns to another station (2026-08-10 field request).
        self._partner_loss_stop = False

    def stop(self, reason: DisarmReason = DisarmReason.MANUAL) -> None:
        """Disarm TX immediately; the partner context is retained."""

        self.tx_enabled = False
        if self.state != QSOState.DONE:
            self.state = QSOState.IDLE
        self.disarm_reason = reason
        if self.on_stop is not None:
            self.on_stop(reason)

    # ---- cycle driving ------------------------------------------------

    def on_message(self, msg: ParsedMessage, snr_db: int | None = None) -> None:
        """Process one decoded message from the current cycle."""

        if not self.tx_enabled or self.state in {QSOState.IDLE, QSOState.DONE}:
            return
        if not addressed_to(msg, self.my_call):
            # Partner turned to call someone else: anti-QRM auto-stop, the
            # mainwindow auto_sequence rule.  Disabled for operator-clicked
            # replies — the operator wants to keep calling that station.
            if (
                self._partner_loss_stop
                and self.dx_call
                and msg.from_call
                and msg.to_call
                and base_call(msg.from_call) == base_call(self.dx_call)
                and base_call(msg.to_call) != base_call(self.my_call)
            ):
                self.stop(DisarmReason.PARTNER_LOST)
            return
        if self.dx_call and base_call(msg.from_call) != base_call(self.dx_call):
            return  # a different station is calling us mid-QSO; ignore it

        # A relevant partner message is progress: the retransmission budget
        # restarts, matching the vendor retransmission behaviour.
        self._tx_count = 0
        if msg.grid:
            self.dx_grid = msg.grid
        if msg.report_db is not None:
            self.report_rcvd = msg.report_db

        match self.state:
            case QSOState.CALLING:
                # Any addressed reply answers the CQ.
                if msg.from_call:
                    self.dx_call = msg.from_call
                    self.report_sent = snr_db
                    self._qso_started_epoch = self.clock()
                    self.state = QSOState.REPORT
            case QSOState.REPLYING:
                # Partner answered our grid call with a report.
                if msg.report_db is not None:
                    self.state = QSOState.ROGER_REPORT
            case QSOState.REPORT:
                if msg.has_roger and msg.report_db is not None:
                    self.state = QSOState.ROGERS
                elif msg.eom == "RRR":
                    self.state = QSOState.ROGERS
                elif msg.eom in {"RR73", "73"}:
                    self._enter_signoff()
            case QSOState.ROGER_REPORT:
                if msg.is_eom:
                    self._enter_signoff()
            case QSOState.ROGERS:
                if msg.is_eom:
                    self._complete()
            case QSOState.SIGNOFF:
                # Partner repeated its final message (our 73 was not copied):
                # allow exactly one more courtesy 73.
                if msg.is_eom:
                    self._signoff_sent = False

    def next_tx_message(self) -> str | None:
        """Return the text to transmit in the coming eligible TX slot.

        Called exactly once per eligible TX slot; repeated identical returns
        are the retransmissions counted by the NFR-055 budget.
        """

        if not self.tx_enabled:
            return None
        match self.state:
            case QSOState.CALLING:
                # UC-004: CQ repeats until answered or explicitly stopped.
                return f"CQ {self.my_call} {self.my_grid}"
            case QSOState.SIGNOFF if self._signoff_sent:
                # Partner stayed silent after our courtesy 73: the logged QSO
                # is over.
                self._complete()
                return None
            case QSOState.IDLE | QSOState.DONE:
                return None

        if self._tx_count >= 1 + self.max_retransmissions:
            self.stop(DisarmReason.RETRY_EXHAUSTED)
            return None
        self._tx_count += 1

        report = _fmt_report(
            self.report_sent if self.report_sent is not None else DEFAULT_REPORT_DB
        )
        match self.state:
            case QSOState.REPLYING:
                return f"{self.dx_call} {self.my_call} {self.my_grid}"
            case QSOState.REPORT:
                return f"{self.dx_call} {self.my_call} {report}"
            case QSOState.ROGER_REPORT:
                return f"{self.dx_call} {self.my_call} R{report}"
            case QSOState.ROGERS:
                return f"{self.dx_call} {self.my_call} RR73"
            case QSOState.SIGNOFF:
                self._signoff_sent = True
                return f"{self.dx_call} {self.my_call} 73"
            case _:
                return None

    # ---- internals -----------------------------------------------------

    def _enter_signoff(self) -> None:
        # UC-005: receiving RR73/73 completes and logs the QSO; one courtesy
        # 73 remains to be transmitted.
        self._ensure_log()
        self.state = QSOState.SIGNOFF

    def _complete(self) -> None:
        self._ensure_log()
        self.tx_enabled = False
        self.state = QSOState.DONE
        self.disarm_reason = DisarmReason.COMPLETE
        if self.on_stop is not None:
            self.on_stop(DisarmReason.COMPLETE)

    def _ensure_log(self) -> None:
        # UC-005: fire the completed record exactly once, immediately out of
        # sequencer state so partner resets cannot lose it.
        if self._qso_logged:
            return
        self._qso_logged = True
        ctx = self.context()
        started = self._qso_started_epoch
        record = QSORecord(
            my_call=self.my_call,
            my_grid=self.my_grid,
            dx_call=self.dx_call,
            dx_grid=self.dx_grid,
            report_sent=self.report_sent,
            report_rcvd=self.report_rcvd,
            started_utc=(
                time.strftime("%H%M%S", time.gmtime(started))
                if started is not None
                else ""
            ),
            freq_hz=ctx.freq_hz,
            band=ctx.band,
        )
        if self.on_qso is not None:
            self.on_qso(record)

    def _reset_partner(self) -> None:
        self.dx_call = ""
        self.dx_grid = ""
        self.report_sent = None
        self.report_rcvd = None
        self.disarm_reason = None
        self.tx_phase = 0  # CQ and unknown-slot replies default to even
        self.tx_frequency = DEFAULT_TX_AUDIO_FREQUENCY
        self._tx_count = 0
        self._signoff_sent = False
        self._qso_logged = False
        self._qso_started_epoch = None
