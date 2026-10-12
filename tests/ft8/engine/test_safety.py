"""Fault-matrix regressions for the central PTT authority (§15, NFR-050..058)."""

from __future__ import annotations

import asyncio
import threading

import numpy as np
import pytest

from ft8.engine.audio_tx import TxPlayer
from ft8.engine.rig import RigError
from ft8.engine.safety import (
    Interlock,
    SafetyConfig,
    SafetyController,
    SafetyEventKind,
    TxRefused,
)
from ft8.engine.sequencer import DisarmReason, Sequencer
from test_audio_tx import FakeOutputStream, run, wait_stream


class FakeRig:
    """RigClient stand-in recording PTT commands with failure injection."""

    def __init__(self, log: list[str] | None = None) -> None:
        self.calls: list[bool] = []
        self.fail_ons = 0   # next N PTT-on commands raise
        self.fail_offs = 0  # next N PTT-off commands raise
        self._log = log

    async def set_ptt(self, transmit: bool) -> None:
        self.calls.append(transmit)
        if self._log is not None:
            self._log.append("ptt_on" if transmit else "ptt_off")
        if transmit and self.fail_ons:
            self.fail_ons -= 1
            raise RigError("-1", "rigctld gone", -1)
        if not transmit and self.fail_offs:
            self.fail_offs -= 1
            raise RigError("-1", "rigctld gone", -1)


class ManualSleeper:
    """Controllable watchdog clock: fires only when the test releases it."""

    def __init__(self) -> None:
        self.pending: list[asyncio.Future[None]] = []

    async def __call__(self, delay: float) -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.pending.append(fut)
        await fut

    def release(self) -> None:
        fut = self.pending.pop(0)
        if not fut.done():
            fut.set_result(None)


class FakeClock:
    """Monotonic stand-in; every read advances by ``step`` seconds."""

    def __init__(self, step: float = 0.0) -> None:
        self.now = 1_000.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def make_controller(
    rig: FakeRig | None = None,
    *,
    config: SafetyConfig = SafetyConfig(),
    clock: FakeClock | None = None,
    sleeper: object | None = None,
    gated_stream: bool = False,
    log: list[str] | None = None,
) -> tuple[SafetyController, FakeRig, Sequencer]:
    """Wire a controller with a fake rig, fake stream and real sequencer."""

    def factory(**kwargs: object) -> FakeOutputStream:
        stream = FakeOutputStream(**kwargs)
        if gated_stream:
            stream.gate = threading.Event()
        if log is not None:
            original = stream.write

            def recording_write(block: np.ndarray) -> None:
                original(block)
                log.append("write")

            stream.write = recording_write  # type: ignore[method-assign]
        return stream

    rig = rig or FakeRig(log)
    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    kwargs: dict[str, object] = {"config": config}
    if clock is not None:
        kwargs["clock"] = clock
    if sleeper is not None:
        kwargs["sleeper"] = sleeper
    controller = SafetyController(
        rig,  # type: ignore[arg-type]
        TxPlayer(stream_factory=factory),
        sequencer=sequencer,
        **kwargs,  # type: ignore[arg-type]
    )
    return controller, rig, sequencer


def small_waveform(seconds: float = 0.1) -> np.ndarray:
    return np.zeros(int(48_000 * seconds), dtype=np.float32)


@pytest.fixture(autouse=True)
def _reset_stream_instances() -> None:
    FakeOutputStream.instances = []


def kinds(controller: SafetyController) -> list[SafetyEventKind]:
    return [e.kind for e in controller.events]


async def wait_for(predicate: object, tries: int = 1_000) -> None:
    for _ in range(tries):
        await asyncio.sleep(0)
        if predicate():  # type: ignore[operator]
            return
    raise AssertionError("condition not reached")


def test_startup_is_monitor_only_and_requests_ptt_off() -> None:
    async def main() -> None:
        controller, rig, sequencer = make_controller()
        sequencer.start_cq()
        await controller.start()
        assert not controller.armed
        assert not sequencer.tx_enabled
        assert rig.calls == [False]
        assert kinds(controller)[-1] == SafetyEventKind.STARTUP

    run(main())


def test_happy_transmit_keys_ptt_around_the_audio() -> None:
    async def main() -> None:
        log: list[str] = []
        controller, rig, _ = make_controller(clock=FakeClock(step=0.05), log=log)
        await controller.start()
        await controller.arm()
        base = len(log)  # startup/arm PTT-off requests precede the TX
        await controller.transmit(small_waveform())
        assert log[base] == "ptt_on"
        assert log[-1] == "ptt_off"
        assert "write" in log[base + 1 : -1]
        assert controller.armed  # one completed TX does not disarm
        assert controller.tx_count == 1
        assert controller.tx_seconds > 0.0
        assert SafetyEventKind.TX_START in kinds(controller)
        assert kinds(controller)[-1] == SafetyEventKind.TX_STOP

    run(main())


def test_transmit_refused_unless_armed() -> None:
    async def main() -> None:
        controller, rig, _ = make_controller()
        await controller.start()
        with pytest.raises(TxRefused, match="not armed"):
            await controller.transmit(small_waveform())
        assert rig.calls == [False]  # only the startup PTT-off

    run(main())


def test_transmit_refused_while_faulted() -> None:
    async def main() -> None:
        controller, _, sequencer = make_controller()
        await controller.start()
        await controller.arm()
        sequencer.start_cq()
        await controller.report_fault(Interlock.DSP, "worker crashed")
        assert not controller.armed
        assert not sequencer.tx_enabled
        with pytest.raises(TxRefused, match="interlock fault"):
            await controller.transmit(small_waveform())

    run(main())


def test_stop_tx_cancels_playback_disarms_and_is_idempotent() -> None:
    async def main() -> None:
        controller, rig, sequencer = make_controller(gated_stream=True)
        await controller.start()
        await controller.arm()
        sequencer.start_cq()
        task = asyncio.create_task(controller.transmit(small_waveform(1.0)))
        stream = await wait_stream()
        await wait_for(stream.write_started.is_set)
        await controller.stop_tx("observer")
        with pytest.raises(TxRefused, match="cancelled"):
            await asyncio.wait_for(task, timeout=2.0)
        assert not controller.armed
        assert not sequencer.tx_enabled
        assert rig.calls[-1] is False
        assert SafetyEventKind.STOP in kinds(controller)
        # duplicate STOP stays safe and keeps requesting PTT-off
        await controller.stop_tx("observer")
        assert controller.stop_count == 2
        assert rig.calls.count(False) >= 2

    run(main())


def test_ptt_on_failure_faults_cat_and_plays_nothing() -> None:
    async def main() -> None:
        rig = FakeRig()
        rig.fail_ons = 1
        controller, _, sequencer = make_controller(rig)
        await controller.start()
        await controller.arm()
        sequencer.start_cq()
        with pytest.raises(TxRefused, match="PTT-on failed"):
            await controller.transmit(small_waveform())
        assert controller.faults == {Interlock.CAT}
        assert not controller.armed
        assert not sequencer.tx_enabled
        assert FakeOutputStream.instances == []  # playback never started
        # the failed keying is followed by a successful best-effort PTT-off
        assert rig.calls == [False, True, False]
        assert not controller.ptt_uncertain
        assert SafetyEventKind.FAULT in kinds(controller)

    run(main())


def test_ptt_off_failure_marks_fault_and_rearm_recovers() -> None:
    async def main() -> None:
        rig = FakeRig()
        controller, _, _ = make_controller(rig)
        await controller.start()
        await controller.arm()
        rig.fail_offs = 1  # the tx-complete PTT-off fails once
        await controller.transmit(small_waveform())
        # the failure is audited and faulted; the finalize retry then succeeds
        assert SafetyEventKind.PTT_OFF_FAILED in kinds(controller)
        assert controller.faults == {Interlock.CAT}
        assert not controller.ptt_uncertain
        assert rig.calls[-1] is False
        # re-arm is refused until the CAT fault is manually cleared
        with pytest.raises(TxRefused, match="active faults"):
            await controller.arm()
        controller.clear_fault(Interlock.CAT)
        await controller.arm()
        assert controller.armed

    run(main())


def test_ptt_off_repeated_on_rig_reconnect() -> None:
    async def main() -> None:
        rig = FakeRig()
        controller, _, _ = make_controller(rig)
        await controller.start()
        await controller.arm()
        rig.fail_offs = 1  # rigctld dies just as STOP requests PTT-off
        await controller.stop_tx("disconnect")
        assert controller.ptt_uncertain
        await controller.on_rig_reconnect()  # rigctld is back (§15.3)
        assert not controller.ptt_uncertain
        assert rig.calls[-1] is False

    run(main())


def test_audio_device_loss_faults_and_releases_ptt() -> None:
    async def main() -> None:
        def factory(**kwargs: object) -> FakeOutputStream:
            stream = FakeOutputStream(**kwargs)
            stream.fail_with = OSError("usb audio gone")
            return stream

        rig = FakeRig()
        sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
        controller = SafetyController(
            rig,  # type: ignore[arg-type]
            TxPlayer(stream_factory=factory),
            sequencer=sequencer,
        )
        await controller.start()
        await controller.arm()
        sequencer.start_cq()
        with pytest.raises(TxRefused, match="usb audio gone"):
            await controller.transmit(small_waveform())
        assert controller.faults == {Interlock.AUDIO}
        assert not controller.armed
        assert not sequencer.tx_enabled
        assert rig.calls[-1] is False

    run(main())


def test_per_transmission_watchdog_trips_on_overrun() -> None:
    async def main() -> None:
        sleeper = ManualSleeper()
        controller, rig, sequencer = make_controller(
            sleeper=sleeper, gated_stream=True, config=SafetyConfig(per_tx_margin_s=0.5)
        )
        await controller.start()
        await controller.arm()
        sequencer.start_cq()
        task = asyncio.create_task(controller.transmit(small_waveform(1.0)))
        stream = await wait_stream()
        await wait_for(lambda: stream.write_started.is_set() and sleeper.pending)
        sleeper.release()  # the deadline fires while audio is stuck
        with pytest.raises(TxRefused, match="watchdog"):
            await asyncio.wait_for(task, timeout=3.0)
        assert controller.faults == {Interlock.AUDIO}
        assert not controller.armed
        assert not sequencer.tx_enabled
        assert SafetyEventKind.WATCHDOG in kinds(controller)
        assert SafetyEventKind.FAULT in kinds(controller)
        assert rig.calls[-1] is False

    run(main())


def test_aggregate_watchdog_caps_cumulative_keyed_time() -> None:
    async def main() -> None:
        clock = FakeClock(step=0.12)
        controller, _, _ = make_controller(
            clock=clock, config=SafetyConfig(aggregate_tx_s=0.15)
        )
        await controller.start()
        await controller.arm()
        await controller.transmit(small_waveform(0.1))
        # 0.12 s keyed so far; another 0.1 s would exceed the 0.15 s budget
        with pytest.raises(TxRefused, match="aggregate"):
            await controller.transmit(small_waveform(0.1))
        assert not controller.armed
        assert SafetyEventKind.WATCHDOG in kinds(controller)
        assert SafetyEventKind.STOP in kinds(controller)

    run(main())


def test_invalid_buffer_is_rejected_before_ptt() -> None:
    async def main() -> None:
        controller, rig, _ = make_controller()
        await controller.start()
        await controller.arm()
        before = list(rig.calls)
        with pytest.raises(ValueError):
            await controller.transmit(np.zeros(100, dtype=np.int16))
        assert rig.calls == before  # PTT never keyed

    run(main())


def test_health_snapshot_reports_state() -> None:
    async def main() -> None:
        controller, _, _ = make_controller()
        await controller.start()
        health = controller.health
        assert health["armed"] is False
        assert health["ptt_on"] is False
        assert health["faults"] == []
        await controller.arm()
        assert controller.health["armed"] is True

    run(main())


def test_stop_tx_maps_operator_reasons_to_manual_disarm() -> None:
    """M3: operator STOPs disarm as MANUAL; system reasons stay FAULT."""

    async def main() -> None:
        controller, _, sequencer = make_controller()
        await controller.start()
        sequencer.start_cq()
        await controller.stop_tx(f"api:operator")  # API priority STOP
        assert sequencer.disarm_reason is DisarmReason.MANUAL
        sequencer.start_cq()
        await controller.stop_tx()  # default "manual"
        assert sequencer.disarm_reason is DisarmReason.MANUAL
        sequencer.start_cq()
        await controller.stop_tx("lease_expired")  # dead-man path
        assert sequencer.disarm_reason is DisarmReason.FAULT

    run(main())


def test_report_fault_latches_per_interlock() -> None:
    """A latched fault repeats no side effects (dead worker errors every slot)."""

    async def main() -> None:
        controller, rig, _ = make_controller()
        await controller.start()
        await controller.report_fault(Interlock.DSP, "worker crashed")
        fault_events = kinds(controller).count(SafetyEventKind.FAULT)
        ptt_offs = rig.calls.count(False)
        await controller.report_fault(Interlock.DSP, "still down")
        assert kinds(controller).count(SafetyEventKind.FAULT) == fault_events
        assert rig.calls.count(False) == ptt_offs
        assert controller.faults == {Interlock.DSP}
        # a different interlock still applies the full fault matrix
        await controller.report_fault(Interlock.CLOCK, "clock jump")
        assert controller.faults == {Interlock.DSP, Interlock.CLOCK}

    run(main())
