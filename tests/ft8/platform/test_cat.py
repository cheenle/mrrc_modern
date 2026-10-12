"""PlatformRig: the engine's rig seam over the platform's CatController."""
from __future__ import annotations

import asyncio

import pytest

from ft8.platform.cat import PlatformRig


class FakeCat:
    def __init__(self):
        self.freq = 14_074_000
        self.mode = 0x02
        self.filter_index = 20
        self.commands: list[tuple] = []

    async def get_frequency(self, vfo="A"):
        return self.freq

    async def set_frequency(self, hz, vfo="A"):
        self.commands.append(("freq", hz))
        self.freq = hz
        return True

    async def get_mode(self, timeout=None):
        return self.mode

    async def set_mode(self, mode_num: int):
        self.commands.append(("mode", mode_num))
        self.mode = mode_num
        return True

    async def set_filter_width(self, index: int):
        self.commands.append(("width", index))
        self.filter_index = index
        return True


class FakeGate:
    def __init__(self):
        self.keyed = False
        self.calls: list[str] = []

    async def key(self) -> bool:
        self.calls.append("key")
        self.keyed = True
        return True

    async def unkey(self, graceful: bool = False) -> None:
        self.calls.append("unkey")
        self.keyed = False


class RefusingGate(FakeGate):
    async def key(self) -> bool:
        self.calls.append("key-refused")
        return False


def test_mode_mapping_data_u_and_usb():
    cat, gate = FakeCat(), FakeGate()
    rig = PlatformRig(cat, gate, rig_mode="DATA-U")
    asyncio.run(rig.set_mode("DATA-U", 3000))
    assert cat.mode == 0x0C
    rig2 = PlatformRig(cat, gate, rig_mode="USB")
    asyncio.run(rig2.set_mode("USB", 3000))
    assert cat.mode == 0x02


def test_empty_mode_falls_back_to_configured_rig_mode():
    cat, gate = FakeCat(), FakeGate()
    rig = PlatformRig(cat, gate, rig_mode="DATA-U")
    asyncio.run(rig.set_mode("", 3000))
    assert cat.mode == 0x0C


def test_unknown_mode_raises_rig_error():
    from ft8.engine.rig import RigError

    rig = PlatformRig(FakeCat(), FakeGate(), rig_mode="DATA-U")
    with pytest.raises(RigError):
        asyncio.run(rig.set_mode("FM-NO", 3000))


def test_filter_width_hz_maps_to_nearest_narrow_index():
    cat, gate = FakeCat(), FakeGate()
    rig = PlatformRig(cat, gate, rig_mode="DATA-U")
    asyncio.run(rig.set_filter_width(3000))
    assert ("width", 18) in cat.commands        # FILTER_WIDTHS_NARROW: 18 -> 3000 Hz


def test_ptt_goes_through_the_platform_gate():
    cat, gate = FakeCat(), FakeGate()
    rig = PlatformRig(cat, gate, rig_mode="DATA-U")
    asyncio.run(rig.set_ptt(True))
    assert gate.keyed is True
    asyncio.run(rig.set_ptt(False))
    assert gate.keyed is False
    assert gate.calls == ["key", "unkey"]


def test_refused_key_raises_rig_error():
    from ft8.engine.rig import RigError

    rig = PlatformRig(FakeCat(), RefusingGate(), rig_mode="DATA-U")
    with pytest.raises(RigError):
        asyncio.run(rig.set_ptt(True))


def test_get_mode_reports_configured_passband():
    cat, gate = FakeCat(), FakeGate()
    rig = PlatformRig(cat, gate, rig_mode="DATA-U")
    cat.mode = 0x0C
    name, passband = asyncio.run(rig.get_mode())
    assert name == "DATA-U"
    assert passband == 3000
