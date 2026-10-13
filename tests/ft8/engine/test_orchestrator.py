from __future__ import annotations

import asyncio

import pytest

from ft8.core.models import DecodeBatch, DecodePath, DecodeResult
from ft8.engine.cascade import (
    DEFAULT_DEEP_LOWER_BOUND_SECONDS,
    DEFAULT_SAFETY_MARGIN_SECONDS,
)
from ft8.engine.orchestrator import (
    DEFAULT_DECISION_CUTOFF_SECONDS,
    DELIVERY_GRACE_SECONDS,
    FAST_PATH_FINISH_DELAY_SECONDS,
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


def make_batch(
    slot_id: int, texts: list[str] | None = None, *,
    path: DecodePath = DecodePath.IMPROVED, escalated: bool = False,
) -> DecodeBatch:
    return DecodeBatch(
        slot_id=slot_id,
        path=path,
        results=tuple(make_result(text, slot_id=slot_id) for text in (texts or [])),
        overflow=False,
        elapsed_seconds=0.25,
        deadline_missed=False,
        escalated=escalated,
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
        path: DecodePath = DecodePath.IMPROVED,
        escalated: bool = False,
    ) -> None:
        self.clock = clock
        self.texts = texts or []
        self.delay = delay
        self.error = error
        self.wrong_slot = wrong_slot
        self.path = path
        self.escalated = escalated
        self.calls: list[tuple[int, bytes]] = []

    async def decode(self, slot_id: int, samples: bytes) -> DecodeBatch:
        self.calls.append((slot_id, samples))
        if self.delay:
            self.clock.advance(self.delay)
        if self.error is not None:
            raise self.error
        return make_batch(slot_id + 1 if self.wrong_slot else slot_id, self.texts,
                          path=self.path, escalated=self.escalated)


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


def test_boundary_epoch_for_slot_uses_the_passed_period() -> None:
    """`period` 必须真的耦合进边界：FT4 形状的调用方传 7.5，边界与预算随之缩放。

    `period` 是可选参数、默认 FT8 的 15 s；若实现里忽略它、恒按 15 s 算，
    一个 `period=7.5` 的调用方拿到的边界会是 `(slot_id + 1) * 15`，预算守卫
    （cascade.py 的 `_remaining_budget`）于是被静默重新标定，而没有任何测试会红。
    """

    assert boundary_epoch_for_slot(0, period=7.5) == (0 + 1) * 7.5
    assert boundary_epoch_for_slot(5, period=7.5) == (5 + 1) * 7.5


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


def test_escalated_slot_finishing_after_the_deep_path_still_reaches_the_sequencer() -> None:
    """升级槽的结果必须能喂给 sequencer —— 这正是把截止放宽到 4.0 的理由。

    现场算术（W103D）：快路径结果落在边界后 ``FAST_PATH_FINISH_DELAY_SECONDS``（~0.48 s），
    深路径再跑 ``DEFAULT_DEEP_LOWER_BOUND_SECONDS``（~2.3 s），于是本槽的结果在边界后
    ~2.78 s 才回来。截止仍是 2.5 s 时它被判 late、只显示、永不喂给 sequencer —— 而
    ``escalate_on_ap_context`` 命中的恰恰是 QSO 中的时隙，于是 QSO 只能等操作员手动推进。

    这里把「快路径落点 + 深路径下界」当成解码耗时，断言它**不** late 且报文真的到了
    sequencer。耗时**全部由常量推导**（绝不写死 2.5 / 4.0）：把截止重新调错，这条与
    ``test_late_result_is_display_only_and_never_fed``（从截止推导延迟）一起红。
    """

    deep_worst_case = FAST_PATH_FINISH_DELAY_SECONDS + DEFAULT_DEEP_LOWER_BOUND_SECONDS
    # 前提：这个耗时必须落在截止之内 —— 否则本用例测的根本不是「级联的第二次解码能按时回来」。
    assert deep_worst_case <= DEFAULT_DECISION_CUTOFF_SECONDS

    sequencer = Sequencer("N0CALL", "FN42")
    sequencer.start_cq()
    harness = Harness(sequencer=sequencer)
    harness.decoder.texts = ["N0CALL K1ABC FN42"]
    harness.decoder.delay = deep_worst_case
    harness.run(decodes=1)

    assert len(harness.events) == 1
    assert harness.events[0].late is False
    assert harness.orchestrator.counters.deadline_misses == 0
    # 结果真的进了 sequencer：QSO 推进到发报告（不是停在 CALLING 干等）。
    assert sequencer.state == QSOState.REPORT
    assert sequencer.dx_call == "K1ABC"


def test_decision_cutoff_leaves_room_for_the_deep_path() -> None:
    """决策截止必须**容得下深路径** —— 这正是当初标错的量，也是本用例存在的理由。

    cascade 的预算守卫（``CascadingDecoder._remaining_budget``）在快路径跑完后才调用，
    它算的是 ``cutoff − (clock() − boundary) − safety_margin``；而现场实测快路径结果
    落在边界后 ``FAST_PATH_FINISH_DELAY_SECONDS``，那时 ``clock() − boundary`` 就是这个
    值。于是守卫肯放行深路径的充要条件是：

        cutoff − 快路径落点 − safety_margin ≥ 深路径下界

    2.5 s 的截止在这条不等式上差 ~0.33 s，于是 W103D 上每一次策略批准的升级都被
    「budget too tight」拒掉（一段 2.5 h 的现场日志里 263 次拒绝、零升级），页面只剩
    快路径。这条就是当初缺的那把尺：只把截止改小（例如退回 2.5），它立刻红。

    三个量全部来自**真实的**常量定义（截止在 orchestrator.py，下界与安全余量在
    cascade.py），快路径落点也是一个具名模块常量 —— 任何一处再被标错，这里都会红。
    """

    remaining = (
        DEFAULT_DECISION_CUTOFF_SECONDS
        - FAST_PATH_FINISH_DELAY_SECONDS
        - DEFAULT_SAFETY_MARGIN_SECONDS
    )
    assert remaining >= DEFAULT_DEEP_LOWER_BOUND_SECONDS, (
        f"decision cutoff {DEFAULT_DECISION_CUTOFF_SECONDS}s leaves only "
        f"{remaining:.2f}s for the deep path after the fast path finishes at "
        f"+{FAST_PATH_FINISH_DELAY_SECONDS}s; the cascade needs "
        f"{DEFAULT_DEEP_LOWER_BOUND_SECONDS}s, so every escalation would be "
        f"refused by the budget guard"
    )


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

    ``early_decode_s`` only shifts the *wait*; lateness is judged against
    ``boundary_epoch + decision_cutoff`` (``DEFAULT_DECISION_CUTOFF_SECONDS``
    by default), so starting the decode earlier must not eat into that cutoff.
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

    # Lateness is measured against the slot's real end: a result inside the
    # decision window (nominal end + DEFAULT_DECISION_CUTOFF_SECONDS) must not
    # be flagged as a deadline miss.
    assert orchestrator.counters.deadline_misses == 0
    for event in events:
        nominal_end = slot_start_epoch(event.slot_id + 1)
        assert event.dispatched_epoch < nominal_end
        assert event.dispatched_epoch < nominal_end + DEFAULT_DECISION_CUTOFF_SECONDS


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


def test_counters_split_fast_from_escalated_slots() -> None:
    """fast/escalated 必须是两个互斥的桶 —— 升级率 = escalations / decodes。"""

    harness = Harness(epoch=7.5)
    harness.decoder.path = DecodePath.FAST
    harness.decoder.escalated = False
    harness.decoder.texts = ["CQ K1ABC FN42"]
    harness.run(decodes=3)

    counters = harness.orchestrator.counters
    assert counters.decodes == 3
    assert counters.fast_decodes == 3
    assert counters.escalations == 0


def test_escalated_slot_is_counted_as_escalation_not_as_fast() -> None:
    harness = Harness(epoch=7.5)
    # 合并后的批次：path 仍是 FAST（深路径一条没多），但升级**发生过**
    harness.decoder.path = DecodePath.FAST
    harness.decoder.escalated = True
    harness.decoder.texts = ["CQ K1ABC FN42"]
    harness.run(decodes=1)

    counters = harness.orchestrator.counters
    assert counters.escalations == 1
    assert counters.fast_decodes == 0, (
        "升级过的槽不算 fast —— 否则 fast_decodes 失去意义，等于 decodes"
    )


def test_fallback_slot_counts_as_neither_fast_nor_escalated() -> None:
    """快路径全败、补跑深路径的那种批次，两个桶都不进 —— 这不是漏记，是有意的。

    `fast_decodes + escalations <= decodes` 允许取不等号全靠这一条：那个批次的
    path 是 IMPROVED（深路径跑的），但它没升级，所以算进 fast 会把「快路径单独
    解完多少槽」抬高；算进 escalations 又等于声称本槽多付了一次深解码 —— 而它
    付的是**唯一**那次。将来若有人把 elif 改成 else「顺手补全」，升级率就会被
    这个桶稀释，而升级率正是判断阈值松紧的主指标。
    """

    harness = Harness(epoch=7.5)
    harness.decoder.path = DecodePath.IMPROVED
    harness.decoder.escalated = False
    harness.decoder.texts = ["CQ K1ABC FN42"]
    harness.run(decodes=2)

    counters = harness.orchestrator.counters
    assert counters.decodes == 2
    assert counters.fast_decodes == 0
    assert counters.escalations == 0
