from __future__ import annotations

import asyncio

import pytest

from ft8.mode import Ft8ModeController, WorkMode


class Harness:
    def __init__(self):
        self.radio = {"freq": 14_200_000, "mode": 0x02, "width": 20}
        self.snapshot = None
        self.engine_running = False
        self.browser_tx_enabled = True
        self.broadcasts: list[str] = []
        self.fail_on_start = False
        self.stop_calls = 0

    async def snapshot_ssb(self):
        self.snapshot = dict(self.radio)
        return self.snapshot

    async def apply_ft8_radio(self):
        self.radio = {"freq": 14_074_000, "mode": 0x0C, "width": 18}

    async def restore_ssb(self, snap):
        self.radio = dict(snap)

    async def start_engine(self):
        if self.fail_on_start:
            raise RuntimeError("dsp worker failed")
        self.engine_running = True

    async def stop_engine(self):
        self.engine_running = False
        self.stop_calls += 1

    def set_browser_tx(self, enabled: bool):
        self.browser_tx_enabled = enabled

    async def broadcast(self, mode: str):
        self.broadcasts.append(mode)


def _controller(h: Harness) -> Ft8ModeController:
    return Ft8ModeController(
        snapshot_ssb=h.snapshot_ssb, apply_ft8_radio=h.apply_ft8_radio,
        restore_ssb=h.restore_ssb, start_engine=h.start_engine,
        stop_engine=h.stop_engine, set_browser_tx=h.set_browser_tx,
        broadcast=h.broadcast,
    )


def test_switch_round_trip_restores_every_field():
    h = Harness()
    ctl = _controller(h)
    asyncio.run(ctl.switch_to_ft8())
    assert ctl.mode is WorkMode.FT8 and h.engine_running
    assert h.radio == {"freq": 14_074_000, "mode": 0x0C, "width": 18}
    assert h.browser_tx_enabled is False
    asyncio.run(ctl.switch_to_ssb())
    assert ctl.mode is WorkMode.SSB and not h.engine_running
    assert h.radio == h.snapshot
    assert h.browser_tx_enabled is True
    assert h.broadcasts == ["ft8", "ssb"]


def test_engine_start_failure_rolls_back_to_ssb():
    h = Harness()
    h.fail_on_start = True
    ctl = _controller(h)
    with pytest.raises(RuntimeError):
        asyncio.run(ctl.switch_to_ft8())
    assert ctl.mode is WorkMode.SSB
    assert h.radio == {"freq": 14_200_000, "mode": 0x02, "width": 20}
    assert h.browser_tx_enabled is True
    assert h.broadcasts == []          # never announced a state it did not reach


def test_double_switch_is_a_noop():
    h = Harness()
    ctl = _controller(h)
    asyncio.run(ctl.switch_to_ft8())
    asyncio.run(ctl.switch_to_ft8())
    assert h.broadcasts == ["ft8"]
    asyncio.run(ctl.switch_to_ssb())
    asyncio.run(ctl.switch_to_ssb())
    assert h.broadcasts == ["ft8", "ssb"]


def test_ssb_switch_stops_engine_before_restoring_radio():
    """Safety order: TX/engine teardown must precede the SSB radio restore."""
    order: list[str] = []

    async def snapshot_ssb():
        return {"freq": 14_200_000}

    async def apply_ft8_radio():
        order.append("apply-ft8")

    async def restore_ssb(snap):
        order.append("restore-ssb")

    async def start_engine():
        order.append("start-engine")

    async def stop_engine():
        order.append("stop-engine")

    ctl = Ft8ModeController(
        snapshot_ssb=snapshot_ssb, apply_ft8_radio=apply_ft8_radio,
        restore_ssb=restore_ssb, start_engine=start_engine, stop_engine=stop_engine,
        set_browser_tx=lambda enabled: None, broadcast=lambda mode: _noop(),
    )
    asyncio.run(ctl.switch_to_ft8())
    asyncio.run(ctl.switch_to_ssb())
    assert order == ["apply-ft8", "start-engine", "stop-engine", "restore-ssb"]


async def _noop() -> None:
    return None
