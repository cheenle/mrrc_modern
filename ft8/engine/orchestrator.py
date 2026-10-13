"""UTC slot orchestration: slot identity, decode dispatch and decision cutoff.

SDD AD-006 and §9.2/§9.4: slot identity is always ``floor(epoch / TRperiod)``
computed from an injectable epoch clock — relative timers may wake the loop
but never define protocol phase.  At every slot boundary the orchestrator
polls the injected source for the just-ended 12 kHz slot, dispatching the
decode as soon as the real audio blocks have landed in the ring,
dispatches one decode through the injected decoder and applies the I9
decision cutoff: a batch that arrives after slot end + cutoff is
display-only for that slot, is counted as a deadline miss and is never fed
to the sequencer, so a late result cannot trigger a late TX
(NFR-001/NFR-009).

TX keying is deliberately absent here: the sequencer is only *fed* with
on-time messages.  Arming, TX-slot eligibility and PTT belong to the safety
and TX pipeline slices (SDD chapter 15).
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from ft8.core.models import DecodeBatch, DecodeResult
from ft8.engine.msgparse import ParsedMessage, parse_message
from ft8.engine.sequencer import Sequencer

FT8_PERIOD_SECONDS = 15.0
FT4_PERIOD_SECONDS = 7.5
# I9 (SDD §13.4): provisional TX decision cutoff, measured on Apple M2.
DEFAULT_DECISION_CUTOFF_SECONDS = 2.5
# Real audio lands one block (~85 ms) plus scheduling after its sample time;
# reading exactly at the boundary always misses the slot tail (found by the
# foreground/launchd deployment: every slot skipped with a healthy ring).
# Dispatch therefore polls the slot source from the boundary until the data
# is actually present, and ``DELIVERY_GRACE_SECONDS`` is only the *timeout*
# beyond which a slot that never lands is treated as missing.  This reads
# as early as physically possible instead of waiting a fixed grace, which
# widens the operator's manual-reply window against the slot's fit deadline
# (UC-003, I9); the decision cutoff above still has ~2 s of headroom.
DELIVERY_GRACE_SECONDS = 0.4
# Poll cadence while waiting for the slot's last block to land in the ring.
SLOT_POLL_SECONDS = 0.025
SLOT_SAMPLES_NBYTES = 360_000  # exact 12 kHz int16 mono slot (AD-004)


def slot_id_for(epoch: float, period: float = FT8_PERIOD_SECONDS) -> int:
    """Return the UTC slot identity ``floor(epoch / period)`` (AD-006)."""

    return math.floor(epoch / period)


def slot_start_epoch(slot_id: int, period: float = FT8_PERIOD_SECONDS) -> float:
    """Return the epoch at which the given slot starts."""

    return slot_id * period


def boundary_epoch_for_slot(slot_id: int, period: float = FT8_PERIOD_SECONDS) -> float:
    """本时隙**结束**的 epoch —— 决策截止就是从这里往后算的。

    等于下一个时隙的开始（`slot_start_epoch(slot_id + 1)`）。`_slot_ended` 的
    `late = finished > boundary_epoch + decision_cutoff`（`:258`）用的就是这个量；
    写成 `slot_start_epoch(slot_id)` 会早整整一个时隙（15 s），预算守卫从此永不放行。
    """

    return slot_start_epoch(slot_id + 1, period)


def slot_parity(slot_id: int) -> int:
    """Return the even/odd TX eligibility parity of a slot (0 = even)."""

    return slot_id % 2


class SlotDecoder(Protocol):
    """One decode of an exact 12 kHz int16 slot, returning a native batch."""

    async def decode(self, slot_id: int, samples: bytes) -> DecodeBatch: ...


@dataclass(frozen=True, slots=True)
class SlotMessage:
    """One decode result paired with its parsed message structure."""

    result: DecodeResult
    parsed: ParsedMessage


@dataclass(frozen=True, slots=True)
class SlotDecode:
    """One completed slot decode as broadcast to state/decode consumers."""

    slot_id: int
    dispatched_epoch: float
    finished_epoch: float
    late: bool
    batch: DecodeBatch
    messages: tuple[SlotMessage, ...]


@dataclass(slots=True)
class OrchestratorCounters:
    """Running totals behind the NFR-002 deadline visibility."""

    slots_started: int = 0
    slots_skipped: int = 0
    decodes: int = 0
    deadline_misses: int = 0
    decode_errors: int = 0


class Orchestrator:
    """UTC slot loop binding slot source, decoder and sequencer together.

    All time sources are injectable: ``clock`` supplies UTC epoch seconds and
    ``sleep_until`` awaits an epoch; production wiring uses ``time.time`` and
    ``asyncio.sleep``, tests drive a fake clock deterministically.
    """

    def __init__(
        self,
        decoder: SlotDecoder,
        slot_source: Callable[[int], bytes | None],
        sequencer: Sequencer,
        *,
        period: float = FT8_PERIOD_SECONDS,
        decision_cutoff: float = DEFAULT_DECISION_CUTOFF_SECONDS,
        delivery_grace: float = DELIVERY_GRACE_SECONDS,
        early_decode_s: float = 0.0,
        slot_poll_interval: float = SLOT_POLL_SECONDS,
        clock: Callable[[], float] = time.time,
        sleep_until: Callable[[float], Awaitable[None]] | None = None,
        on_slot_start: Callable[[int], None] | None = None,
        on_decode: Callable[[SlotDecode], None] | None = None,
        on_decode_error: Callable[[int, Exception], None] | None = None,
    ) -> None:
        if period <= 0 or decision_cutoff <= 0:
            raise ValueError("period and decision cutoff must be positive")
        if decision_cutoff >= period:
            raise ValueError("decision cutoff must be smaller than the slot period")
        if not 0.0 <= early_decode_s < period:
            raise ValueError("early_decode_s must be within [0, period)")
        if delivery_grace < 0:
            raise ValueError("delivery grace must not be negative")
        if slot_poll_interval <= 0:
            raise ValueError("slot poll interval must be positive")
        self._decoder = decoder
        self._slot_source = slot_source
        self._sequencer = sequencer
        self._period = period
        self._decision_cutoff = decision_cutoff
        self._delivery_grace = delivery_grace
        self._early_decode_s = early_decode_s
        self._slot_poll_interval = slot_poll_interval
        self._clock = clock
        self._sleep_until = sleep_until or self._asyncio_sleep_until
        self._on_slot_start = on_slot_start
        self._on_decode = on_decode
        self._on_decode_error = on_decode_error
        self.counters = OrchestratorCounters()
        self._running = False
        self._announced_slot: int | None = None

    async def _asyncio_sleep_until(self, epoch: float) -> None:
        delay = epoch - self._clock()
        if delay > 0:
            await asyncio.sleep(delay)

    def stop(self) -> None:
        """Stop the loop after the current boundary wait."""

        self._running = False

    async def run(self) -> None:
        """Run the slot loop until :meth:`stop` or task cancellation."""

        self._running = True
        _trace = 0
        while self._running:
            current = slot_id_for(self._clock(), self._period)
            # An FT8 waveform is over by 12.64 s, so the tail of the slot is
            # silence and waiting for it is pure latency.  ``early_decode_s``
            # stops the wait that much sooner (the slot source pads the tail);
            # ``slot_end`` stays the *nominal* end for lateness accounting.
            slot_end = slot_start_epoch(current + 1, self._period)
            boundary = slot_end - self._early_decode_s
            _trace += 1
            if _trace <= 3 or _trace % 20 == 0:
                print(f"[orch-trace] loop={_trace} now={self._clock():.3f} slot={current} "
                      f"boundary={boundary:.3f} announced={self._announced_slot}", flush=True)
            if current != self._announced_slot:
                self._announced_slot = current
                self.counters.slots_started += 1
                if self._on_slot_start is not None:
                    self._on_slot_start(current)
            await self._sleep_until(boundary)
            if not self._running:
                break
            samples = await self._wait_for_slot(current, boundary)
            if samples is None:
                self.counters.slots_skipped += 1
                if _trace <= 6 or _trace % 20 == 0:
                    ring = getattr(self._slot_source, "__self__", None)
                    info = ""
                    if ring is not None:
                        info = (f" ring_base={ring.base} high={ring.high_water} "
                                f"gaps={ring.gap_count} need=[{current * 180000},"
                                f"{current * 180000 + 180000})")
                    print(f"[orch-trace] slot={current} SKIPPED (no data within grace){info}", flush=True)
                continue
            if _trace <= 3 or _trace % 20 == 0:
                print(f"[orch-trace] slot={current} samples={len(samples)} -> decode", flush=True)
            await self._slot_ended(current, slot_end, samples)
            # An early dispatch leaves the clock inside the slot we just
            # handled (decode itself usually covers the remainder).  Consume
            # any leftover so the next iteration advances to the NEXT slot
            # instead of decoding this one twice.
            if self._early_decode_s > 0 and self._clock() < slot_end:
                await self._sleep_until(slot_end)

    async def _wait_for_slot(self, slot_id: int, boundary_epoch: float) -> bytes | None:
        """Return the ended slot's samples as soon as they land in the ring.

        The capture writes the last block of a slot roughly one block (~85 ms)
        plus scheduling after the sample time, so polling at the boundary
        dispatches the decode earlier than a fixed grace (UC-003 manual-reply
        window) while remaining robust to jitter — a slot that never lands
        within ``delivery_grace`` of the boundary returns ``None``.
        """

        deadline = boundary_epoch + self._delivery_grace
        while True:
            samples = self._slot_source(slot_id)
            if samples is not None:
                return samples
            now = self._clock()
            if now >= deadline:
                return None
            await self._sleep_until(min(now + self._slot_poll_interval, deadline))

    async def _slot_ended(self, slot_id: int, boundary_epoch: float, samples: bytes) -> None:
        if len(samples) != SLOT_SAMPLES_NBYTES:
            raise ValueError(
                f"slot source must return exactly {SLOT_SAMPLES_NBYTES} bytes"
            )
        dispatched = self._clock()
        try:
            batch = await self._decoder.decode(slot_id, samples)
        except Exception as error:
            self.counters.decode_errors += 1
            if self._on_decode_error is not None:
                self._on_decode_error(slot_id, error)
            return
        finished = self._clock()
        if batch.slot_id != slot_id:
            error = ValueError(
                f"decode returned slot {batch.slot_id}, expected {slot_id}"
            )
            self.counters.decode_errors += 1
            if self._on_decode_error is not None:
                self._on_decode_error(slot_id, error)
            return

        late = finished > boundary_epoch + self._decision_cutoff
        messages = tuple(
            SlotMessage(result, parse_message(result.text))
            for result in batch.results
        )
        self.counters.decodes += 1
        if late:
            # NFR-001/NFR-009: display-only; never fed to the sequencer.
            self.counters.deadline_misses += 1
        else:
            for message in messages:
                self._sequencer.on_message(
                    message.parsed, snr_db=message.result.snr
                )
        if self._on_decode is not None:
            self._on_decode(
                SlotDecode(
                    slot_id=slot_id,
                    dispatched_epoch=dispatched,
                    finished_epoch=finished,
                    late=late,
                    batch=batch,
                    messages=messages,
                )
            )
