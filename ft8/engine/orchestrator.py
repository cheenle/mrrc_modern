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

from ft8.core.models import DecodeBatch, DecodePath, DecodeResult
from ft8.engine.msgparse import ParsedMessage, parse_message
from ft8.engine.sequencer import Sequencer

FT8_PERIOD_SECONDS = 15.0
FT4_PERIOD_SECONDS = 7.5
# I9 (SDD §13.4): decision cutoff — a batch whose decode finishes later than
# ``boundary + this`` is display-only (see ``_slot_ended``) and never reaches
# the sequencer, so it can never trigger a late TX (NFR-001/NFR-009).
#
# With the cascade this cutoff has to cover **two** decodes, not one: it must be
# ≥ (the delay before the second path can start) + (the slowest path that may
# run) + margin.  The fast path's result lands ``FAST_PATH_FINISH_DELAY_SECONDS``
# after the slot boundary (below), and the deep (WSJT-X improved, threads=3)
# path then needs ``DEFAULT_DEEP_LOWER_BOUND_SECONDS`` — so the 2.5 s this was
# originally sized to (a single decode path, measured on an M2) never admitted
# the cascade's own second path: ``cascade._remaining_budget`` refused every
# policy-approved escalation ("budget too tight"; 263 refusals / 0 escalations
# over one ~2.5 h W103D field window), silently leaving the box on the fast path
# alone.  The value below leaves ~1.17 s of guard slack (cutoff − 0.48 − 0.05 ≈
# 3.47 vs the 2.3 s bound) while the worst case still finishes inside the window
# (0.48 + 2.25 ≈ 2.73 s), so an escalated slot's decodes now reach the sequencer
# instead of being dropped as display-only.
#
# It stays far below the 15 s period (``Orchestrator.__init__`` rejects
# ``decision_cutoff >= period``), and it is the TX *fit guard* — not this cutoff
# — that prevents a late transmission (``tx_driver.py``).
DEFAULT_DECISION_CUTOFF_SECONDS = 4.0
# The fast path's result lands this long *after* the slot boundary, measured on
# the W103D (2026-10-13): that slot's fast decode published at ``17:42:00,481``
# while its boundary was ``1791884518.800`` (nominal end ``17:42:00.0``) — a
# ~0.48 s delay.  This is the ``clock() − boundary`` term that
# ``CascadingDecoder._remaining_budget`` subtracts before it can spend anything
# on the deep path, so the decision cutoff has to cover it *plus* the deep
# path's duration; the guard is in ``tests/ft8/engine/test_orchestrator.py``.
FAST_PATH_FINISH_DELAY_SECONDS = 0.48
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

# AP（先验）解码的 quality 分界，用于把 ap_low_quality 计数与 ap_decodes 分开。
# 取值依据来自解码器自己的语义而不是拍脑袋：ft8_a8d 在 plog < -147 时把 qual
# 降到 0.16（PLOG_MIN = -160），而实测里 iaptype 3 那一路散布在 0.07-0.83
# （docs/ft8-dl-speculative-notes.md §4.4）。0.5 正好落在「a8 的高置信 1.0」与
# 「a8 自己认为可疑的 0.16」之间，所以它数的是「解码器自己都没把握的那部分」。
AP_LOW_QUALITY = 0.5


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
    # True for the fast-path answer published while the deep answer is still in
    # flight (spec 2026-10-15, two-stage).  The slot is published twice and the
    # second publication replaces the first (state.js keys rows by slot_id), so
    # the operator sees the band ~1.5 s earlier and the complete list later.
    provisional: bool = False


@dataclass(slots=True)
class OrchestratorCounters:
    """Running totals behind the NFR-002 deadline visibility."""

    slots_started: int = 0
    slots_skipped: int = 0
    decodes: int = 0
    deadline_misses: int = 0
    decode_errors: int = 0
    # 本槽的答案是快路径给出的：策略或预算拦下了升级，**也包括**深路径跑了但没
    # 给出结果（失败、或没解出新报文）—— 那时答案仍然是快路径那一份。
    fast_decodes: int = 0
    # 深路径的结果真的并进了本槽（本槽为此多付了一次深解码）。升级率
    # = escalations / decodes，是判断阈值松紧的主指标。
    escalations: int = 0
    # AP（先验）解码产出：本槽最终批次里 ap_type != 0 的报文数。
    # 必须能被数出来，否则打开了 MRRC_FT8_AP_A_PRIORI 也无从判断它到底有没有
    # 起作用——§4.4 的 20/120 是在合成信号上量的，线上只有这两个数。
    ap_decodes: int = 0
    # 其中 quality < AP_LOW_QUALITY 的条数。§4.4 量到 a8 的 quality 全是 1.0，
    # 而 iaptype 3 那一路散布在 0.07–0.83，所以「AP 解出的东西有多 shaky」是
    # 上线后第一个要看的数（设计笔记 §7.1 判据 3）。
    ap_low_quality: int = 0


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
        # Two-stage publication is opt-in on the decoder: a plain SupervisorDecoder
        # has no fast answer to publish early, and a decoder that does gets the
        # orchestrator's own formatter (so a provisional row is identical to a
        # final one, minus the sequencer and the counters).
        sink = getattr(decoder, "set_provisional_sink", None)
        if sink is not None:
            sink(self._on_provisional)
        self._on_decode_error = on_decode_error
        self.counters = OrchestratorCounters()
        self._running = False
        self._announced_slot: int | None = None
        self._dispatched_slot: int | None = None

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
            if self._dispatched_slot is not None and current <= self._dispatched_slot:
                # We are still inside a slot whose decode we already
                # dispatched.  That is the normal state right after an early
                # dispatch (``early_decode_s`` hands control back before the
                # nominal slot end), and it is only safe if the clock really
                # has crossed the boundary — which ``sleep_until`` does not
                # promise: asyncio fires its timer up to one clock resolution
                # early, and it sleeps on the monotonic clock while slot
                # identity is derived from the wall clock.  When the wake-up
                # lands a millisecond short, ``floor(clock / period)`` names
                # the slot we just finished, the slot source already serves it
                # instantly, and the loop decoded it a second time (field
                # report 2026-10-13: every fast-path slot duplicated on the
                # FT8 page).  The deep path never woke up early enough to get
                # here, so the race stayed hidden until the fast path landed.
                # Slot identity may only move forward; the wait is not the
                # invariant.  ``slot_end`` is strictly ahead of the clock —
                # ``current`` was derived from that same clock one line ago.
                await self._sleep_until(slot_end)
                continue
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
            # Claim the slot BEFORE the decode: whatever the decode does
            # (succeed, fail or raise), this slot is spent.  A slot whose data
            # never landed is deliberately not claimed — the next iteration
            # retries it while it is still the current one.
            self._dispatched_slot = current
            await self._slot_ended(current, slot_end, samples)

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

    def _on_provisional(self, slot_id: int, batch: DecodeBatch) -> None:
        """Publish the fast answer while the deep one is still running.

        Deliberately *not* the full slot path: no counters (a slot is decoded
        once, whatever it is published as) and **no sequencer** — the state
        machine keeps seeing exactly one batch per slot, the complete one, so
        latency improves without the sequencing logic learning a new timetable.
        The UI replaces rows by slot_id, so the later, fuller publication is a
        correction rather than a duplicate.
        """

        if self._on_decode is None:
            return
        finished = self._clock()
        messages = tuple(
            SlotMessage(result, parse_message(result.text)) for result in batch.results
        )
        self._on_decode(
            SlotDecode(
                slot_id=slot_id,
                dispatched_epoch=finished,
                finished_epoch=finished,
                late=False,
                batch=batch,
                messages=messages,
                provisional=True,
            )
        )

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
        for message in messages:
            if message.result.ap_type:
                self.counters.ap_decodes += 1
                if message.result.quality < AP_LOW_QUALITY:
                    self.counters.ap_low_quality += 1
        if batch.escalated:
            self.counters.escalations += 1
        elif batch.path is DecodePath.FAST:
            # 只在**没升级且答案是快路径**时才算 fast。快路径整个失败而补跑深路径
            # 的那种批次（CascadingDecoder 的 fallback）两者都不算 —— 它不是快路径的
            # 功劳，也没升级，所以 fast_decodes + escalations <= decodes 是有意的。
            self.counters.fast_decodes += 1
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
