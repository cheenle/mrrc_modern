"""Slot-boundary TX driver: sequencer message → encode → gated transmit.

The orchestrator announces every slot start; this driver transmits only on
the sequencer's current phase (FT8 TX/RX alternation), pulls at most one
message per eligible slot from the sequencer (driving the NFR-055 budget),
encodes it through the supervised Worker and hands the waveform to the safety
controller.  Every encode uses the sequencer's per-QSO ``tx_frequency`` — the
offset the partner's message was decoded at for a Reply (UC-003 split
behaviour), the station default for a CQ — so the partner's receiver pairs
the transmission.  Encode failures are counted and reported through the error
hook (the composition layer latches the DSP interlock); a ``TxRefused``
from the safety controller is only counted — refusal or abort by the
safety authority (STOP, disarm, watchdog, latched interlock) is the safety
system working as designed, and real CAT/audio faults already latch inside
``transmit`` before it raises.  The driver never retries and never touches
PTT itself.

The decision is provisional (I9): when the sequencer is idle at slot start —
the common case before the operator taps a Reply — the driver keeps the
slot's TX window open until ``decision_cutoff`` seconds into the slot,
polling so a Reply transmits as soon as it is armed instead of waiting for
the cutoff.  FT8's fixed 12.64 s waveform in a 15 s slot physically caps the
latest usable start at ~2.4 s; the fit guard below refuses to start a
waveform that would overrun the slot (which is undecodable at the partner
and deafens the next slot's RX), so a Reply armed past that deadline simply
falls to the next eligible slot.  The parity is re-checked before every
transmit, so a reply whose phase does not fit the current slot waits for the
next one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .safety import TxRefused
from .sequencer import Sequencer

_log = logging.getLogger("mrrc-ft8.tx")

DEFAULT_TX_PERIOD_SECONDS = 15.0  # FT8 slot; slot_start = slot_id * period
# Operator-selected decision window (2026-08-03): how long after a candidate
# appears the operator may still click Reply.  The fit guard caps the latest
# usable start, so clicks inside this window but past the fit deadline defer
# to the next eligible slot instead of overrunning.
TX_DECISION_CUTOFF_SECONDS = 5.0
TX_WAVEFORM_SECONDS = 12.64  # MAX_TX_SAMPLES / 48 kHz
# Encode round-trip to the DSP worker; the fit guard reserves it so a click
# inside the window never overruns the slot (field 2026-08-10: the check used
# to pass at ~2.0 s and the waveform then started 0.5 s later — overrun).
TX_ENCODE_SECONDS = 0.5
TX_FIT_MARGIN_SECONDS = 0.1  # encode + start ramp before the waveform
TX_POLL_SECONDS = 0.1


@dataclass
class TxDriver:
    """One-message-per-eligible-slot transmission pump.

    The TX parity follows the sequencer's per-QSO ``tx_phase`` (UC-003): the
    phase is the slot opposite the one the partner was heard in, so a reply to
    an even-slot message transmits on odd slots and vice versa.  CQ always
    runs on the default even phase.  ``period``, ``decision_cutoff``, ``clock``
    and ``sleep`` are injectable so tests stay deterministic.
    """

    sequencer: Sequencer
    encoder: Any       # SupervisorEncoder; duck-typed for tests
    safety: Any        # SafetyController; duck-typed for tests
    period: float = DEFAULT_TX_PERIOD_SECONDS
    decision_cutoff: float = TX_DECISION_CUTOFF_SECONDS
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    counters: dict[str, int] = field(
        default_factory=lambda: {"tx_attempts": 0, "tx_failed": 0}
    )
    # Optional observer for the composition layer (desktop client shows the
    # transmitted message live).  Fired only after a successful transmit.
    on_transmitted: Callable[[int, str, float], None] | None = None
    _tx_in_flight: bool = field(default=False, repr=False)
    # Pre-encoded waveform cache (2026-08-10): a reply armed in the decision
    # window is encoded immediately; if the current slot can no longer fit it
    # (past the fit deadline), the waveform is kept here and transmitted at
    # the next eligible slot boundary without re-encoding — the boundary TX
    # path shrinks from ~+0.65 s to ~+0.1 s, and the fit check is honest
    # (the waveform's true length is known, not an estimate).
    _pending: tuple[str, Any] | None = field(default=None, repr=False)

    async def on_slot_start(self, slot_id: int) -> None:
        """Handle one orchestrator slot-start announcement."""

        if self._tx_in_flight:
            # The previous encode is still running (a slow round trip can
            # span a same-parity recurrence); skipping this slot is the
            # correct degradation — the sequencer retransmits next slot.
            return
        # The parity is decided only once the sequencer has a QSO in mind
        # (tx_enabled).  While idle there is no phase yet — a manual Reply
        # armed later in this slot may target either parity, so keep the
        # window open instead of rejecting the slot on the default phase.
        if self.sequencer.tx_enabled and slot_id % 2 != self.sequencer.tx_phase:
            return

        # A pre-encoded waveform from a deferred reply: transmit it now if the
        # sequencer still wants the same message (no encode round-trip).
        if self._pending is not None:
            pending_msg, waveform = self._pending
            self._pending = None
            if pending_msg == self.sequencer.next_tx_message():
                _log.debug(
                    "tx slot %d: transmitting pre-encoded %r", slot_id, pending_msg
                )
                await self._transmit(slot_id, pending_msg, waveform=waveform)
                return
            _log.debug("tx slot %d: pending message changed, dropping cache", slot_id)

        message = self.sequencer.next_tx_message()
        if message is not None:
            await self._transmit(slot_id, message)
            return

        # Provisional (I9): no message ready yet — the operator may still tap
        # a Reply.  Keep this slot's TX window open until the decision
        # cutoff, polling so a Reply transmits as soon as it is armed.  The
        # fit guard refuses to start a waveform that would overrun the slot.
        slot_start = slot_id * self.period
        slot_end = slot_start + self.period
        deadline = slot_start + self.decision_cutoff
        while True:
            now = self.clock()
            if now >= deadline:
                _log.debug("tx window slot %d: decision cutoff reached, no reply", slot_id)
                return
            if now + TX_WAVEFORM_SECONDS + TX_FIT_MARGIN_SECONDS >= slot_end:
                _log.debug(
                    "tx window slot %d: past the %.2f s fit deadline, deferring to the next slot",
                    slot_id,
                    slot_end - TX_WAVEFORM_SECONDS - TX_FIT_MARGIN_SECONDS - slot_start,
                )
                return
            message = self.sequencer.next_tx_message()
            if message is not None:
                if slot_id % 2 != self.sequencer.tx_phase:
                    _log.debug(
                        "tx window slot %d: armed reply targets the other parity", slot_id
                    )
                    return
                # Encode immediately so the fit check is honest (the
                # waveform's true start time is known) and, if this slot is
                # already too late, the waveform is cached for the next
                # eligible boundary instead of being re-encoded there.
                waveform = await self.encoder.encode(
                    message, self.sequencer.tx_frequency, slot_id=slot_id
                )
                if self.clock() + TX_WAVEFORM_SECONDS + TX_FIT_MARGIN_SECONDS < slot_end:
                    _log.debug(
                        "tx window slot %d: reply armed at +%.2f s, transmitting",
                        slot_id, now - slot_start,
                    )
                    await self._transmit(slot_id, message, waveform=waveform)
                    return
                self._pending = (message, waveform)
                _log.debug(
                    "tx window slot %d: past fit deadline — waveform cached for "
                    "the next eligible slot",
                    slot_id,
                )
                return
            await self.sleep(min(TX_POLL_SECONDS, max(0.0, deadline - now)))

    async def _transmit(
        self, slot_id: int, message: str, *, waveform: Any | None = None
    ) -> None:
        self.counters["tx_attempts"] += 1
        self._tx_in_flight = True
        try:
            # UC-003: encode on the sequencer's audio offset — the partner's
            # decoded offset for a Reply, the station default for a CQ — so
            # the partner's receiver pairs the transmission.  A pre-encoded
            # waveform (deferred-reply cache) skips the worker round-trip.
            if waveform is None:
                waveform = await self.encoder.encode(
                    message, self.sequencer.tx_frequency, slot_id=slot_id
                )
            await self.safety.transmit(waveform)
            if self.on_transmitted is not None:
                self.on_transmitted(slot_id, message, self.sequencer.tx_frequency)
        except TxRefused:
            # Refused or aborted by the safety authority (STOP cancel,
            # disarm, watchdog, latched interlock): not a DSP fault, and
            # real CAT/audio faults already latched inside transmit().
            self.counters["tx_failed"] += 1
        except Exception as error:
            # Encode-path failures mirror the RX path in orchestrator.py:
            # WorkerFault (transport/health) or OSError (shared memory)
            # beyond TxEncodeError; all are counted, none propagate.
            self.counters["tx_failed"] += 1
            self.on_tx_error(slot_id, error)
        finally:
            self._tx_in_flight = False

    def on_tx_error(self, slot_id: int, error: Exception) -> None:
        """Hook for composition-layer audit/logging; default is a no-op."""
