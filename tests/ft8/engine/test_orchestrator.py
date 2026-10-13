from __future__ import annotations

import asyncio

import pytest

from ft8.core.models import DecodeBatch, DecodePath, DecodeResult
from ft8.engine.orchestrator import (
    DEFAULT_DECISION_CUTOFF_SECONDS,
    DELIVERY_GRACE_SECONDS,
    FT4_PERIOD_SECONDS,
    FT8_PERIOD_SECONDS,
    Orchestrator,
    SlotDecode,
    boundary_epoch_for_slot,
    slot_id_for,
    slot_parity,
    slot_start_epoch,
)
from ft8.engine.sequencer import QSOState, Sequencer


class FakeClock:
    def __init__(self, epoch: float) -> None:
        self.epoch = epoch

    def __call__(self) -> float:
        return self.epoch

    def advance(self, seconds: float) -> None:
        self.epoch += seconds

    async def sleep_until(self, epoch: float) -> None:
        self.epoch = max(self.epoch, epoch)


def make_result(text: str, snr: int = -10, slot_id: int = 0) -> DecodeResult:
    return DecodeResult(
        slot_id=slot_id,
        sync=1.5,
        snr=snr,
        dt=0.0,
        frequency=1500.0,
        text=text,
        ap_type=0,
        quality=1.0,
        flags=0,
    )


def make_batch(slot_id: int, texts: list[str] | None = None) -> DecodeBatch:
    return DecodeBatch(
        slot_id=slot_id,
        path=DecodePath.IMPROVED,
        results=tuple(make_result(text, slot_id=slot_id) for text in (texts or [])),
        overflow=False,
        elapsed_seconds=0.25,
        deadline_missed=False,
    )


class FakeDecoder:
    def __init__(
        self,
        clock: FakeClock,
        *,
        texts: list[str] | None = None,
        delay: float = 0.0,
        error: Exception | None = None,
        wrong_slot: bool = False,
    ) -> None:
        self.clock = clock
        self.texts = texts or []
        self.delay = delay
        self.error = error
        self.wrong_slot = wrong_slot
        self.calls: list[tuple[int, bytes]] = []

    async def decode(self, slot_id: int, samples: bytes) -> DecodeBatch:
        self.calls.append((slot_id, samples))
        if self.delay:
            self.clock.advance(self.delay)
        if self.error is not None:
            raise self.error
        return make_batch(slot_id + 1 if self.wrong_slot else slot_id, self.texts)


class Harness:
    """Deterministic orchestrator wiring around a FakeClock."""

    def __init__(
        self,
        *,
        epoch: float = 0.0,
        decoder: FakeDecoder | None = None,
        sequencer: Sequencer | None = None,
        slots: dict[int, bytes | None] | None = None,
    ) -> None:
        self.clock = FakeClock(epoch)
        self.decoder = decoder or FakeDecoder(self.clock)
        self.sequencer = sequencer or Sequencer("N0CALL", "FN42")
        self.events: list[SlotDecode] = []
        self.errors: list[tuple[int, Exception]] = []
        self.started: list[int] = []
        pcm = b"\x00" * 360_000
        if slots is None:
            source = lambda slot_id: pcm
        else:
            source = lambda slot_id: slots.get(slot_id, pcm)
        self.orchestrator = Orchestrator(
            self.decoder,
            source,
            self.sequencer,
            clock=self.clock,
            sleep_until=self.clock.sleep_until,
            on_slot_start=self.started.append,
            on_decode=self._on_decode,
            on_decode_error=self._on_decode_error,
        )
        self._stop_after_decodes: int | None = None
        self._stop_after_errors: int | None = None

    def _on_decode(self, event: SlotDecode) -> None:
        self.events.append(event)
        if self._stop_after_decodes is not None and len(self.events) >= self._stop_after_decodes:
            self.orchestrator.stop()

    def _on_decode_error(self, slot_id: int, error: Exception) -> None:
        self.errors.append((slot_id, error))
        if self._stop_after_errors is not None and len(self.errors) >= self._stop_after_errors:
            self.orchestrator.stop()

    def run(self, *, decodes: int | None = None, errors: int | None = None) -> None:
        self._stop_after_decodes = decodes
        self._stop_after_errors = errors
        asyncio.run(self.orchestrator.run())


def test_slot_math_floor_identity() -> None:
    assert slot_id_for(0.0) == 0
    assert slot_id_for(14.999_999) == 0
    assert slot_id_for(15.0) == 1
    assert slot_id_for(1_755_000_000.0) == 117_000_000
    assert slot_id_for(-0.001) == -1
    assert slot_id_for(7.499_999, FT4_PERIOD_SECONDS) == 0
    assert slot_id_for(7.5, FT4_PERIOD_SECONDS) == 1


def test_slot_start_and_parity() -> None:
    assert slot_start_epoch(0) == 0.0
    assert slot_start_epoch(5) == 75.0
    assert slot_start_epoch(117_000_000) == 1_755_000_000.0
    assert [slot_parity(s) for s in range(4)] == [0, 1, 0, 1]


def test_boundary_epoch_for_slot_is_the_slots_end() -> None:
    """决策截止从时隙**结束**算起，不是从开始算起。"""

    assert boundary_epoch_for_slot(100, 15.0) == slot_start_epoch(101, 15.0)
    assert boundary_epoch_for_slot(100, 15.0) != slot_start_epoch(100, 15.0)
    assert boundary_epoch_for_slot(100, 15.0) - boundary_epoch_for_slot(99, 15.0) == 15.0


def test_config_validation() -> None:
    clock = FakeClock(0.0)
    decoder = FakeDecoder(clock)
    sequencer = Sequencer("N0CALL", "FN42")
    source = lambda slot_id: None
    with pytest.raises(ValueError):
        Orchestrator(decoder, source, sequencer, period=0.0)
    with pytest.raises(ValueError):
        Orchestrator(decoder, source, sequencer, decision_cutoff=0.0)
    with pytest.raises(ValueError):
        Orchestrator(
            decoder,
            source,
            sequencer,
            period=FT8_PERIOD_SECONDS,
            decision_cutoff=FT8_PERIOD_SECONDS,
        )


def test_loop_dispatches_each_ended_slot_and_announces_starts() -> None:
    harness = Harness(epoch=7.5)  # mid-slot 0
    harness.decoder.texts = ["CQ K1ABC FN42"]
    harness.run(decodes=3)

    assert [call[0] for call in harness.decoder.calls] == [0, 1, 2]
    assert harness.started == [0, 1, 2]
    assert harness.orchestrator.counters.slots_started == 3
    assert harness.orchestrator.counters.decodes == 3
    assert harness.orchestrator.counters.deadline_misses == 0
    assert harness.orchestrator.counters.decode_errors == 0
    for event in harness.events:
        assert not event.late
        assert event.batch.slot_id == event.slot_id
        assert event.messages[0].parsed.is_cq
        assert event.messages[0].result.snr == -10
        # Dispatch happens at the slot boundary: the source here serves the
        # slot immediately, so decode starts as early as physically possible
        # (no fixed delivery grace is added; the grace is only a timeout).
        assert event.dispatched_epoch == pytest.approx(
            slot_start_epoch(event.slot_id + 1)
        )


def test_dispatch_waits_for_the_slot_data_not_a_fixed_grace() -> None:
    """Decode starts the instant the slot's data lands, not at boundary + grace.

    Regression for the manual-reply catch window (UC-003/I9): a fixed 0.4 s
    delivery grace shaves ~0.3 s off the operator's window against the slot's
    fit deadline; polling dispatches as early as the ring actually serves it.
    """

    clock = FakeClock(7.5)
    decoder = FakeDecoder(clock, texts=["CQ K1ABC FN42"])
    pcm = b"\x00" * 360_000
    events: list[SlotDecode] = []
    orchestrator = Orchestrator(
        decoder,
        lambda slot_id: (
            pcm if clock() >= slot_start_epoch(slot_id + 1) + 0.1 else None
        ),
        Sequencer("N0CALL", "FN42"),
        clock=clock,
        sleep_until=clock.sleep_until,
        on_slot_start=lambda slot_id: None,
        on_decode=lambda event: (
            events.append(event),
            orchestrator.stop() if len(events) == 2 else None,
        ),
    )
    asyncio.run(orchestrator.run())

    assert [call[0] for call in decoder.calls] == [0, 1]
    assert orchestrator.counters.slots_skipped == 0
    for event in events:
        data_ready = slot_start_epoch(event.slot_id + 1) + 0.1
        # Dispatch lands at data-ready (within one poll interval), never at
        # the old boundary + 0.4 s grace.
        assert data_ready <= event.dispatched_epoch <= data_ready + 0.03


def test_slot_that_never_lands_is_skipped_after_the_grace_timeout() -> None:
    """A slot whose data never arrives is skipped (bounded wait), not hung."""

    clock = FakeClock(7.5)
    decoder = FakeDecoder(clock, texts=["CQ K1ABC FN42"])
    pcm = b"\x00" * 360_000
    events: list[SlotDecode] = []
    orchestrator = Orchestrator(
        decoder,
        lambda slot_id: None if slot_id == 0 else pcm,
        Sequencer("N0CALL", "FN42"),
        clock=clock,
        sleep_until=clock.sleep_until,
        on_slot_start=lambda slot_id: None,
        on_decode=lambda event: (events.append(event), orchestrator.stop()),
    )
    asyncio.run(orchestrator.run())

    assert [call[0] for call in decoder.calls] == [1]  # slot 0 never decoded
    assert orchestrator.counters.slots_skipped == 1
    assert orchestrator.counters.decodes == 1
    assert events[0].slot_id == 1
    assert events[0].dispatched_epoch == pytest.approx(
        slot_start_epoch(2)  # slot 1 ends at 30.0; data served immediately
    )


def test_on_time_results_are_fed_to_the_sequencer() -> None:
    sequencer = Sequencer("N0CALL", "FN42")
    sequencer.start_cq()
    harness = Harness(sequencer=sequencer)
    harness.decoder.texts = ["N0CALL K1ABC FN42", "CQ W9XYZ EM57"]
    harness.run(decodes=1)

    assert sequencer.state == QSOState.REPORT
    assert sequencer.dx_call == "K1ABC"
    assert sequencer.report_sent == -10  # the decode SNR


def test_late_result_is_display_only_and_never_fed() -> None:
    sequencer = Sequencer("N0CALL", "FN42")
    sequencer.start_cq()
    harness = Harness(sequencer=sequencer)
    harness.decoder.texts = ["N0CALL K1ABC FN42"]
    harness.decoder.delay = DEFAULT_DECISION_CUTOFF_SECONDS + 0.5
    harness.run(decodes=1)

    assert len(harness.events) == 1
    assert harness.events[0].late is True
    assert harness.orchestrator.counters.decodes == 1
    assert harness.orchestrator.counters.deadline_misses == 1
    assert sequencer.state == QSOState.CALLING  # no late-triggered TX state


def test_slot_mismatch_is_an_error_event() -> None:
    harness = Harness()
    harness.decoder.wrong_slot = True
    harness.run(errors=1)

    assert harness.events == []
    assert len(harness.errors) == 1
    assert harness.errors[0][0] == 0
    assert isinstance(harness.errors[0][1], ValueError)
    assert harness.orchestrator.counters.decode_errors == 1
    assert harness.orchestrator.counters.decodes == 0


def test_decoder_exception_is_reported_and_the_loop_continues() -> None:
    harness = Harness()
    harness.decoder.error = RuntimeError("worker gone")
    healthy = FakeDecoder(harness.clock, texts=["CQ K1ABC FN42"])

    original_on_error = harness._on_decode_error

    def on_error(slot_id: int, error: Exception) -> None:
        original_on_error(slot_id, error)
        harness.orchestrator._decoder = healthy  # supervisor restarted upstream

    harness.orchestrator._on_decode_error = on_error
    harness.run(decodes=1)

    assert len(harness.errors) == 1
    assert isinstance(harness.errors[0][1], RuntimeError)
    assert len(harness.events) == 1
    assert harness.events[0].slot_id == 1
    assert harness.orchestrator.counters.decode_errors == 1
    assert harness.orchestrator.counters.decodes == 1


def test_missing_slot_samples_are_skipped_without_decode() -> None:
    pcm = b"\x00" * 360_000
    harness = Harness(slots={0: None, 1: pcm})
    harness.run(decodes=1)

    assert [call[0] for call in harness.decoder.calls] == [1]
    assert harness.orchestrator.counters.slots_skipped == 1
    assert len(harness.events) == 1


def test_wrong_sample_length_fails_loudly() -> None:
    harness = Harness()
    bad = Orchestrator(
        harness.decoder,
        lambda slot_id: b"\x00" * 100,
        harness.sequencer,
        clock=harness.clock,
        sleep_until=harness.clock.sleep_until,
    )
    with pytest.raises(ValueError, match="exactly"):
        asyncio.run(bad.run())


def test_early_decode_dispatches_before_the_nominal_slot_end() -> None:
    """early_decode_s starts the decode before the slot's 15 s mark.

    An FT8 waveform ends at 12.64 s, so the tail of the slot is silence.
    Waiting for it is pure latency; with early_decode_s the orchestrator
    stops waiting that much earlier and the source (which pads the tail)
    serves the slot as soon as the waveform is in.
    """

    clock = FakeClock(7.5)
    decoder = FakeDecoder(clock, texts=["CQ K1ABC FN42"])
    pcm = b"\x00" * 360_000
    events: list[SlotDecode] = []
    orchestrator = Orchestrator(
        decoder,
        # Data becomes available 1.2 s BEFORE the slot's nominal end.
        lambda slot_id: (
            pcm if clock() >= slot_start_epoch(slot_id + 1) - 1.2 else None
        ),
        Sequencer("N0CALL", "FN42"),
        clock=clock,
        sleep_until=clock.sleep_until,
        early_decode_s=1.2,
        on_slot_start=lambda slot_id: None,
        on_decode=lambda event: (
            events.append(event),
            orchestrator.stop() if len(events) == 2 else None,
        ),
    )
    asyncio.run(orchestrator.run())

    assert [call[0] for call in decoder.calls] == [0, 1]
    assert orchestrator.counters.slots_skipped == 0
    for event in events:
        nominal_end = slot_start_epoch(event.slot_id + 1)
        # Dispatched before the slot even ends — that is the whole point.
        assert event.dispatched_epoch < nominal_end


def test_early_decode_does_not_mark_results_late() -> None:
    """Lateness is measured against the slot's real end, not the early edge.

    The decision cutoff (default 5 s) is the TX fit window measured from the
    slot boundary; shifting the wait must not eat into it.
    """

    clock = FakeClock(7.5)
    decoder = FakeDecoder(clock, texts=["CQ K1ABC FN42"])
    pcm = b"\x00" * 360_000
    events: list[SlotDecode] = []
    orchestrator = Orchestrator(
        decoder,
        lambda slot_id: (
            pcm if clock() >= slot_start_epoch(slot_id + 1) - 1.2 else None
        ),
        Sequencer("N0CALL", "FN42"),
        clock=clock,
        sleep_until=clock.sleep_until,
        early_decode_s=1.2,
        on_slot_start=lambda slot_id: None,
        on_decode=lambda event: (
            events.append(event),
            orchestrator.stop() if len(events) == 2 else None,
        ),
    )
    asyncio.run(orchestrator.run())

    # Lateness is measured against the slot's real end: finishing well before
    # deadline (nominal end + 5 s) must not be flagged as a deadline miss.
    assert orchestrator.counters.deadline_misses == 0
    for event in events:
        nominal_end = slot_start_epoch(event.slot_id + 1)
        assert event.dispatched_epoch < nominal_end
        assert event.dispatched_epoch < nominal_end + 5.0


def test_early_decode_zero_preserves_upstream_behaviour() -> None:
    """The default (0) keeps waiting for the full slot — upstream parity."""

    clock = FakeClock(7.5)
    decoder = FakeDecoder(clock, texts=["CQ K1ABC FN42"])
    pcm = b"\x00" * 360_000
    events: list[SlotDecode] = []
    orchestrator = Orchestrator(
        decoder,
        lambda slot_id: (
            pcm if clock() >= slot_start_epoch(slot_id + 1) else None
        ),
        Sequencer("N0CALL", "FN42"),
        clock=clock,
        sleep_until=clock.sleep_until,
        on_slot_start=lambda slot_id: None,
        on_decode=lambda event: (
            events.append(event),
            orchestrator.stop() if len(events) == 2 else None,
        ),
    )
    asyncio.run(orchestrator.run())

    for event in events:
        assert event.dispatched_epoch >= slot_start_epoch(event.slot_id + 1)


class EarlyWakeClock:
    """A clock whose ``sleep_until`` wakes a tick BEFORE its target.

    That is what the real one does: asyncio runs a timer whose deadline is
    within the loop's clock resolution, so ``asyncio.sleep`` may return
    early — and it sleeps on the monotonic clock while slot identity comes
    from the wall clock, so the two drift apart.  Time still passes; the
    wait just does not promise the clock reached the target.
    """

    def __init__(self, epoch: float, tick: float = 0.0005) -> None:
        self.epoch = epoch
        self.tick = tick

    def __call__(self) -> float:
        return self.epoch

    def advance(self, seconds: float) -> None:
        self.epoch += seconds

    async def sleep_until(self, epoch: float) -> None:
        delay = epoch - self.epoch
        if delay <= 0:
            return
        self.epoch += max(delay - self.tick, self.tick)


def test_a_sleep_that_wakes_early_does_not_decode_the_same_slot_twice() -> None:
    """时隙身份只许前进：``sleep_until`` 早醒一次也不能重解刚才的时隙。

    现场报告 2026-10-13（快路径上线当天）：FT8 页面每个时隙出现重复行，服务端
    日志里同一个 slot_id 被解码两次。机制是早派发 —— ``early_decode_s`` 让解码在
    时隙名义结束前就派发，回填的那次 ``sleep_until(slot_end)`` 若早返回，墙钟便
    仍在该时隙内，``floor(clock / period)`` 于是又指回刚解完的同一个时隙，而槽源
    此时已经能立刻供数，于是原地重解。深路径（约 4 s）总在时隙结束后才结束，
    这条路径从来不会走到，所以它一直藏着。
    """

    clock = EarlyWakeClock(7.5)
    decoder = FakeDecoder(clock, texts=["CQ K1ABC FN42"])
    pcm = b"\x00" * 360_000
    events: list[SlotDecode] = []
    orchestrator = Orchestrator(
        decoder,
        lambda slot_id: pcm,
        Sequencer("N0CALL", "FN42"),
        clock=clock,
        sleep_until=clock.sleep_until,
        early_decode_s=1.2,
        on_slot_start=lambda slot_id: None,
        on_decode=lambda event: (
            events.append(event),
            orchestrator.stop() if len(events) == 3 else None,
        ),
    )
    asyncio.run(orchestrator.run())

    assert [call[0] for call in decoder.calls] == [0, 1, 2]
    assert [event.slot_id for event in events] == [0, 1, 2]
    assert orchestrator.counters.decodes == 3
