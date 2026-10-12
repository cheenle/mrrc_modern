"""Central PTT authority and TX safety controller (§15, NFR-050..058).

This is the only module that may key PTT or cancel TX audio (§11.4).  It
rechecks the authority chain immediately before keying (§15.2), enforces
the per-transmission and aggregate TX watchdogs (§15.4, NFR-057), applies
the fault matrix (§15.5) and provides the idempotent, priority STOP that
never blocks on a PTT confirmation loop (§15.3, NFR-052/053).

Lease/session authorization (layers 1–4) and slot eligibility (layer 7)
are enforced by the web and orchestration layers; this controller owns the
CAT/audio/DSP/clock interlocks (layer 6) and the actual keying (layer 8).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

from .audio_tx import TX_SAMPLE_RATE, TxAudioError, TxPlayer, validate_tx_buffer
from .rig import RigClient, RigError
from .sequencer import DisarmReason, Sequencer


class Interlock(StrEnum):
    """Health inputs whose fault disarms TX (§15.5)."""

    CAT = "cat"
    AUDIO = "audio"
    DSP = "dsp"
    CLOCK = "clock"


class SafetyEventKind(StrEnum):
    STARTUP = "startup"
    ARM = "arm"
    TX_START = "tx_start"
    TX_STOP = "tx_stop"
    STOP = "stop"
    PTT_OFF = "ptt_off"
    PTT_OFF_FAILED = "ptt_off_failed"
    WATCHDOG = "watchdog"
    FAULT = "fault"
    CLEARED = "cleared"


@dataclass(frozen=True, slots=True)
class SafetyEvent:
    """One audit/health event (NFR-070: every TX start/stop/reason)."""

    kind: SafetyEventKind
    detail: str
    epoch: float


@dataclass(frozen=True, slots=True)
class SafetyConfig:
    """Watchdog budgets (§15.4)."""

    per_tx_margin_s: float = 2.0
    """Per-transmission deadline = waveform duration plus this margin."""

    aggregate_tx_s: float = 600.0
    """Maximum cumulative keyed time per armed session."""


class TxRefused(Exception):
    """The authority chain rejected or aborted a transmission."""


def _disarm_reason(reason: str) -> DisarmReason:
    """Map a STOP reason to sequencer semantics (§15.3).

    Operator-initiated STOPs — the default ``"manual"`` and the API's
    ``"api:<actor>"`` — disarm as :data:`DisarmReason.MANUAL`; system
    reasons (dead-man, watchdogs, shutdown) stay :data:`DisarmReason.FAULT`
    so downstream audits (e.g. the CQ loop) see the true origin.
    """

    if reason == "manual" or reason.startswith("api:"):
        return DisarmReason.MANUAL
    return DisarmReason.FAULT


class SafetyController:
    """Sole PTT authority; all TX flows through :meth:`transmit`.

    ``sleeper`` is injectable (defaults to ``asyncio.sleep``) so watchdog
    regressions are deterministic; ``clock`` is monotonic for durations and
    ``wall`` stamps audit events.  ``on_event`` receives every
    :class:`SafetyEvent` for audit/health fan-out.
    """

    def __init__(
        self,
        rig: RigClient,
        player: TxPlayer,
        *,
        sequencer: Sequencer | None = None,
        config: SafetyConfig = SafetyConfig(),
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleeper: Callable[[float], Awaitable[object]] = asyncio.sleep,
        on_event: Callable[[SafetyEvent], None] | None = None,
    ) -> None:
        self._rig = rig
        self._player = player
        self._sequencer = sequencer
        self._config = config
        self._clock = clock
        self._wall = wall
        self._sleeper = sleeper
        self._on_event = on_event
        self.armed = False
        self.ptt_on = False
        self.ptt_uncertain = False
        self.faults: set[Interlock] = set()
        self.tx_seconds = 0.0
        self.tx_count = 0
        self.stop_count = 0
        self._events: list[SafetyEvent] = []

    # ---- inspection ---------------------------------------------------

    @property
    def events(self) -> tuple[SafetyEvent, ...]:
        """Audit events in emission order."""

        return tuple(self._events)

    @property
    def health(self) -> dict[str, object]:
        """Snapshot for NFR-076 health reporting."""

        return {
            "armed": self.armed,
            "ptt_on": self.ptt_on,
            "ptt_uncertain": self.ptt_uncertain,
            "faults": sorted(f.value for f in self.faults),
            "tx_seconds": round(self.tx_seconds, 3),
            "tx_count": self.tx_count,
            "stop_count": self.stop_count,
        }

    def _emit(self, kind: SafetyEventKind, detail: str) -> None:
        event = SafetyEvent(kind=kind, detail=detail, epoch=self._wall())
        self._events.append(event)
        if self._on_event is not None:
            self._on_event(event)

    # ---- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        """Startup path: disarmed, monitor-only, best-effort PTT off (NFR-058)."""

        self.armed = False
        if self._sequencer is not None:
            self._sequencer.stop(DisarmReason.MANUAL)
        await self._ptt_off_best_effort("startup")
        self._emit(SafetyEventKind.STARTUP, "monitor-only; TX disarmed")

    async def arm(self) -> None:
        """Manual (re-)arm after the operator resolves any fault (§15.5).

        Refuses while interlocks are faulted; retries best-effort PTT-off
        first when the line state is uncertain (§15.3 reconnect rule).
        """

        if self.faults:
            names = ",".join(sorted(f.value for f in self.faults))
            raise TxRefused(f"cannot arm with active faults: {names}")
        if self.ptt_uncertain and not await self._ptt_off_best_effort("pre-arm"):
            raise TxRefused("PTT state uncertain and PTT-off failed; staying disarmed")
        self.armed = True
        self.tx_seconds = 0.0
        self._emit(SafetyEventKind.ARM, "TX armed by operator")

    def disarm(self, reason: DisarmReason = DisarmReason.MANUAL) -> None:
        """Drop the armed flag and disarm the sequencer; never touches PTT."""

        self.armed = False
        if self._sequencer is not None:
            self._sequencer.stop(reason)

    # ---- STOP ------------------------------------------------------------

    async def stop_tx(self, reason: str = "manual") -> None:
        """Idempotent priority STOP (§15.3): any session, never raises."""

        self.stop_count += 1
        self._player.cancel()
        self.disarm(_disarm_reason(reason))
        await self._ptt_off_best_effort(f"stop:{reason}")
        self._emit(SafetyEventKind.STOP, reason)

    # ---- interlocks -------------------------------------------------------

    async def report_fault(self, source: Interlock, detail: str) -> None:
        """Apply the fault matrix: cancel audio, disarm, PTT-off request.

        Faults latch per interlock: re-reporting an already-faulted source
        is a no-op, so a dead DSP Worker failing every slot cannot repeat
        the PTT-off request or flood the audit log.  :meth:`clear_fault`
        releases the latch.
        """

        if source in self.faults:
            return
        self.faults.add(source)
        self._player.cancel()
        self.disarm(DisarmReason.FAULT)
        await self._ptt_off_best_effort(f"fault:{source.value}")
        self._emit(SafetyEventKind.FAULT, f"{source.value}: {detail}")

    def clear_fault(self, source: Interlock) -> None:
        """Mark a repaired, verified interlock healthy again (manual path)."""

        if source in self.faults:
            self.faults.discard(source)
            self._emit(SafetyEventKind.CLEARED, source.value)

    async def on_rig_reconnect(self) -> None:
        """Repeat best-effort PTT-off after rigctld reconnect (§15.3)."""

        if self.ptt_uncertain or self.ptt_on:
            await self._ptt_off_best_effort("rig-reconnect")

    # ---- gated transmission ------------------------------------------------

    async def transmit(self, samples: np.ndarray) -> None:
        """Key PTT, play one bounded waveform, release PTT.

        The authority chain is rechecked immediately before keying; any
        failure cancels audio, requests PTT-off and disarms.  Raises
        ``TxRefused`` when the transmission did not run to completion.
        """

        pcm = validate_tx_buffer(samples)
        duration = pcm.size / TX_SAMPLE_RATE
        if self.faults:
            names = ",".join(sorted(f.value for f in self.faults))
            raise TxRefused(f"interlock fault active: {names}")
        if not self.armed:
            raise TxRefused("TX not armed")
        if self.ptt_uncertain and not await self._ptt_off_best_effort("pre-tx"):
            raise TxRefused("PTT state uncertain; refusing new TX")
        if self.tx_seconds + duration > self._config.aggregate_tx_s:
            self._emit(SafetyEventKind.WATCHDOG, "aggregate TX budget exhausted")
            await self.stop_tx("aggregate_watchdog")
            raise TxRefused("aggregate TX watchdog tripped")

        try:
            await self._rig.set_ptt(True)
        except RigError as exc:
            self.ptt_uncertain = True
            await self.report_fault(Interlock.CAT, f"PTT-on failed: {exc}")
            raise TxRefused(f"PTT-on failed: {exc}") from exc
        self.ptt_on = True
        self._emit(SafetyEventKind.TX_START, f"{duration:.2f}s waveform")
        started = self._clock()

        play_task: asyncio.Task[object] = asyncio.create_task(self._player.play(pcm))
        watchdog_task = asyncio.ensure_future(
            self._sleeper(duration + self._config.per_tx_margin_s)
        )
        try:
            done, pending = await asyncio.wait(
                {play_task, watchdog_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if play_task not in done:
                self._player.cancel()
                await asyncio.wait({play_task}, timeout=1.0)
                self._emit(SafetyEventKind.WATCHDOG, "per-transmission deadline")
                await self.report_fault(Interlock.AUDIO, "TX deadline exceeded")
                raise TxRefused("per-transmission watchdog tripped")
            result = play_task.result()
            self.tx_seconds += self._clock() - started
            self.tx_count += 1
            await self._ptt_off_best_effort("tx-complete")
            if getattr(result, "cancelled", False):
                self._emit(SafetyEventKind.TX_STOP, "cancelled")
                raise TxRefused("playback cancelled")
            self._emit(SafetyEventKind.TX_STOP, f"completed {duration:.2f}s")
        except TxAudioError as exc:
            await self.report_fault(Interlock.AUDIO, str(exc))
            raise TxRefused(str(exc)) from exc
        finally:
            watchdog_task.cancel()
            if self.ptt_on:
                await self._ptt_off_best_effort("tx-finalize")
                self.ptt_on = False

    # ---- PTT helpers ---------------------------------------------------

    async def _ptt_off_best_effort(self, why: str) -> bool:
        """Request PTT-off once; never block on verification (NFR-053)."""

        try:
            await self._rig.set_ptt(False)
        except RigError as exc:
            self.ptt_uncertain = True
            self.faults.add(Interlock.CAT)
            self._emit(SafetyEventKind.PTT_OFF_FAILED, f"{why}: {exc}")
            return False
        self.ptt_on = False
        self.ptt_uncertain = False
        self._emit(SafetyEventKind.PTT_OFF, why)
        return True
