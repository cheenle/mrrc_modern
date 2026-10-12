from __future__ import annotations

import asyncio

from ft8.engine.cq_loop import CqLoopController, LoopStopReason
from ft8.engine.msgparse import parse_message
from ft8.engine.sequencer import DisarmReason, QSOState, Sequencer


class FakeArm:
    def __init__(self, refuse: bool = False) -> None:
        self.calls = 0
        self.refuse = refuse

    async def __call__(self) -> None:
        self.calls += 1
        if self.refuse:
            from ft8.engine.safety import TxRefused

            raise TxRefused("faulted")


def make_controller(
    *,
    lease: bool = True,
    timeout: int = 600,
    arm: FakeArm | None = None,
    pick_frequency: object | None = None,
) -> tuple[Sequencer, CqLoopController, list[float], list[tuple[str, str]]]:
    sequencer = Sequencer(my_call="M0XX", my_grid="IO91")
    now = [1000.0]
    audits: list[tuple[str, str]] = []
    controller = CqLoopController(
        sequencer,
        arm=arm or FakeArm(),
        lease_alive=lambda: lease,
        clock=lambda: now[0],
        idle_timeout=lambda: timeout,
        on_audit=lambda op, detail: audits.append((op, detail)),
        **({} if pick_frequency is None else {"pick_frequency": pick_frequency}),
    )
    return sequencer, controller, now, audits


def start(controller: CqLoopController) -> None:
    asyncio.run(controller.start())


def test_done_rearms_cq_and_resets_idle_timer() -> None:
    sequencer, controller, now, _audits = make_controller()
    start(controller)
    assert sequencer.state == QSOState.CALLING
    sequencer.on_message(parse_message("M0XX K1ABC FN42"), snr_db=-12)
    sequencer.on_message(parse_message("M0XX K1ABC R-10"), snr_db=-10)
    sequencer.on_message(parse_message("M0XX K1ABC RR73"), snr_db=-9)
    sequencer.next_tx_message()
    sequencer.next_tx_message()  # DONE
    now[0] += 590  # close to the 600 s timeout
    controller.tick()
    assert sequencer.state == QSOState.CALLING  # re-armed
    now[0] += 590
    controller.tick()
    assert controller.active  # timer reset on DONE: still running


def test_retry_exhausted_rearms_without_resetting_timer() -> None:
    sequencer, controller, now, _audits = make_controller()
    start(controller)
    sequencer.on_message(parse_message("M0XX K1ABC FN42"), snr_db=-12)
    sequencer.max_retransmissions = 0
    sequencer.next_tx_message()  # initial report send
    sequencer.next_tx_message()  # budget exhausted
    assert sequencer.disarm_reason == DisarmReason.RETRY_EXHAUSTED
    controller.tick()
    assert sequencer.state == QSOState.CALLING and controller.active
    now[0] += 601
    controller.tick()
    assert not controller.active  # no DONE → timeout still fires


def test_partner_lost_rearms() -> None:
    sequencer, controller, _now, _audits = make_controller()
    start(controller)
    sequencer.on_message(parse_message("M0XX K1ABC FN42"), snr_db=-12)
    sequencer.on_message(parse_message("W1AW K1ABC -08"), snr_db=-8)
    assert sequencer.disarm_reason == DisarmReason.PARTNER_LOST
    controller.tick()
    assert sequencer.state == QSOState.CALLING and controller.active


def test_manual_disarm_stops_loop() -> None:
    sequencer, controller, _now, audits = make_controller()
    start(controller)
    sequencer.stop(DisarmReason.MANUAL)  # TX off
    controller.tick()
    assert not controller.active
    assert ("cq_loop_stop", "manual") in audits


def test_fault_disarm_stops_loop() -> None:
    sequencer, controller, _now, audits = make_controller()
    start(controller)
    sequencer.stop(DisarmReason.FAULT)
    controller.tick()
    assert not controller.active
    assert ("cq_loop_stop", "fault") in audits


def test_lease_loss_stops_loop() -> None:
    sequencer, controller, _now, audits = make_controller(lease=True)
    start(controller)
    controller.lease_alive = lambda: False  # lease dropped
    controller.tick()
    assert not controller.active
    assert ("cq_loop_stop", "lease_lost") in audits


def test_idle_timeout_stops_and_disarms() -> None:
    sequencer, controller, now, audits = make_controller(timeout=60)
    start(controller)
    now[0] += 61
    controller.tick()
    assert not controller.active
    assert sequencer.tx_enabled is False
    assert ("cq_loop_stop", "timeout") in audits


def test_start_is_idempotent_and_audited() -> None:
    _sequencer, controller, _now, audits = make_controller()
    start(controller)
    start(controller)
    assert audits.count(("cq_loop_start", "600")) == 1


def test_arm_refusal_fails_start() -> None:
    _sequencer, controller, _now, audits = make_controller(arm=FakeArm(refuse=True))
    start(controller)
    assert not controller.active
    assert ("cq_loop_stop", "arm_refused") in audits


def test_status_shape() -> None:
    _sequencer, controller, now, _audits = make_controller(timeout=600)
    start(controller)
    now[0] += 100
    status = controller.status()
    assert status == {"active": True, "idle_remaining_s": 500}


def test_cq_start_uses_injected_frequency_picker() -> None:
    """UC-004: the loop asks the composition root for an unoccupied offset
    (FrequencyOccupancy picker) instead of hard-coding 1500 Hz."""

    sequencer, controller, _now, _audits = make_controller(
        pick_frequency=lambda: 1435.0
    )
    start(controller)
    assert sequencer.tx_frequency == 1435.0
    assert controller.active


def test_cq_rearm_re_picks_frequency() -> None:
    """DONE re-CQ asks the picker again — the offset can move between QSOs."""

    picks: list[float] = [1200.0, 1377.0]
    sequencer, controller, _now, _audits = make_controller(
        pick_frequency=lambda: picks.pop(0)
    )
    start(controller)
    assert sequencer.tx_frequency == 1200.0
    sequencer.on_message(parse_message("M0XX K1ABC FN42"))   # caller answers CQ
    sequencer.next_tx_message()                                # Tx1 grid reply
    sequencer.on_message(parse_message("M0XX K1ABC R-12"))  # partner report → ROGERS
    sequencer.next_tx_message()                                # Tx4 RR73
    sequencer.on_message(parse_message("M0XX K1ABC RR73"))  # → DONE (complete)
    assert sequencer.state is QSOState.DONE
    controller.tick()  # DONE → re-CQ with a fresh pick
    assert sequencer.tx_frequency == 1377.0
