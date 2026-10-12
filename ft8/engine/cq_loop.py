"""Automatic CQ loop controller.

Wraps the sequencer without modifying its state machine: completed or
failed QSOs re-arm CQ calling; manual/fault disarms, lease loss and the
idle timeout stop the loop.  The controller polls on the composition
watchdog and never touches PTT, audio or the network itself.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

from .sequencer import (
    DEFAULT_TX_AUDIO_FREQUENCY,
    DisarmReason,
    QSOState,
    Sequencer,
)

DEFAULT_IDLE_TIMEOUT_S = 600
MIN_IDLE_TIMEOUT_S = 60
MAX_IDLE_TIMEOUT_S = 3_600

_REARM_REASONS = frozenset({DisarmReason.RETRY_EXHAUSTED, DisarmReason.PARTNER_LOST})
_STOP_REASONS = frozenset({DisarmReason.MANUAL, DisarmReason.FAULT})


class LoopStopReason(StrEnum):
    TIMEOUT = "timeout"
    MANUAL = "manual"
    FAULT = "fault"
    LEASE_LOST = "lease_lost"
    ARM_REFUSED = "arm_refused"


@dataclass
class CqLoopController:
    """Observe sequencer transitions; re-CQ or stop, per the transition table."""

    sequencer: Sequencer
    arm: Callable[[], Awaitable[None]]
    lease_alive: Callable[[], bool]
    clock: Callable[[], float]
    idle_timeout: Callable[[], int]
    on_audit: Callable[[str, str], None]
    pick_frequency: Callable[[], float] = lambda: DEFAULT_TX_AUDIO_FREQUENCY
    active: bool = False
    _last_progress: float = field(default=0.0)
    _observed: tuple[QSOState, DisarmReason | None] = field(
        default=(QSOState.IDLE, None)
    )

    async def start(self) -> None:
        """Arm via the normal safety path and begin CQ calling; idempotent."""

        if self.active:
            return
        try:
            await self.arm()
        except Exception:
            self.on_audit("cq_loop_stop", LoopStopReason.ARM_REFUSED.value)
            return
        self.sequencer.start_cq(tx_frequency=self.pick_frequency())
        self.active = True
        self._last_progress = self.clock()
        self._observed = (self.sequencer.state, self.sequencer.disarm_reason)
        self.on_audit("cq_loop_start", str(self.idle_timeout()))

    def stop(self, reason: LoopStopReason) -> None:
        """Terminate the loop (TX state itself is owned by safety/sequencer)."""

        if not self.active:
            return
        self.active = False
        self.on_audit("cq_loop_stop", reason.value)

    def tick(self) -> None:
        """One watchdog poll: lease gate, transition table, idle timeout."""

        if not self.active:
            return
        if not self.lease_alive():
            self.stop(LoopStopReason.LEASE_LOST)
            return
        observed = (self.sequencer.state, self.sequencer.disarm_reason)
        state, reason = observed
        if observed != self._observed:
            self._observed = observed
            if state is QSOState.DONE:
                self._last_progress = self.clock()
                self.sequencer.start_cq(tx_frequency=self.pick_frequency())
            elif reason in _REARM_REASONS:
                self.sequencer.start_cq(tx_frequency=self.pick_frequency())  # failed QSO: re-CQ, no timer reset
            elif reason in _STOP_REASONS:
                self.stop(
                    LoopStopReason.FAULT
                    if reason is DisarmReason.FAULT
                    else LoopStopReason.MANUAL
                )
                return
            self._observed = (self.sequencer.state, self.sequencer.disarm_reason)
        if self.clock() - self._last_progress > self.idle_timeout():
            self.sequencer.stop(DisarmReason.MANUAL)
            self.stop(LoopStopReason.TIMEOUT)

    def status(self) -> dict[str, object]:
        """Snapshot view for the state broadcast."""

        remaining = 0
        if self.active:
            remaining = max(
                0,
                round(self.idle_timeout() - (self.clock() - self._last_progress)),
            )
        return {"active": self.active, "idle_remaining_s": remaining}
