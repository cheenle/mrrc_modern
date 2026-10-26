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


def test_unknown_mode_falls_back_to_a_mode_the_radio_has():
    """An unrecognised mode name must not abort the switch.

    The FT8 settings store is user-editable and defaults to a Yaesu mode
    (``DATA-U``); a radio without it must still start.  This replaces an earlier
    test that asserted a ``RigError`` — raising left FT8 unusable on every
    Icom model (bg7kxr, 2026-10-26).
    """
    cat, gate = FakeCat(), FakeGate()
    rig = PlatformRig(cat, gate, rig_mode="DATA-U")
    asyncio.run(rig.set_mode("FM-NO", 3000))
    assert cat.mode == 0x02          # fell back to the FT-710 USB register


def test_yaesu_data_u_maps_to_icom_usb():
    cat, gate = FakeCat(), FakeGate()
    rig = PlatformRig(cat, gate, rig_mode="DATA-U", backend=FakeIcomBackend())
    asyncio.run(rig.set_mode("DATA-U", 3000))
    assert cat.mode == 0x01          # Icom USB, the closest thing to DATA-U
    assert rig._rig_mode == "USB"    # the resolved name is remembered


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


# ── IC-7300 (Icom CI-V) tables ───────────────────────────────────────────
# Measured 2026-10-26 on bg7kxr: the rig used the FT-710 tables for both the
# mode byte and the filter index, so FT8 never started —
# "RigError: CAT: set_filter_width(3000 Hz) refused", POST /api/ft8/mode → 500.


class FakeIcomBackend:
    """The two tables IC7300Backend exposes (backends/ic7300/backend.py:137-160).

    ``mode_name_to_num`` is a **property** on the real backend (base.py:210);
    mirroring that here is what catches a call site that wrongly invokes it.
    """

    @property
    def mode_name_to_num(self):
        # Icom CI-V: USB is 0x01 and AM is 0x02 — the reverse of Yaesu, where
        # USB is 0x02 and AM is 0x05.
        return {"LSB": 0x00, "USB": 0x01, "AM": 0x02, "CW-U": 0x03,
                "RTTY-L": 0x04, "FM": 0x05, "CW-L": 0x07, "RTTY-U": 0x08}

    def filter_tables(self):
        # FIL1/FIL2/FIL3 only; there is no 21-entry width ladder.
        return {"voice": [(1, 3000), (2, 2400), (3, 1800)],
                "narrow": [(1, 500), (2, 300), (3, 250)],
                "narrowModes": ["CW-U", "CW-L", "RTTY-L", "RTTY-U"]}


def test_icom_mode_byte_comes_from_the_backend_not_yaesu():
    cat, gate = FakeCat(), FakeGate()
    rig = PlatformRig(cat, gate, rig_mode="USB", backend=FakeIcomBackend())
    asyncio.run(rig.set_mode("USB", 3000))
    assert cat.mode == 0x01          # Icom USB — the Yaesu table would send 0x02 (AM)
    asyncio.run(rig.set_mode("AM", 3000))
    assert cat.mode == 0x02


def test_icom_filter_width_maps_onto_fil1_3():
    cat, gate = FakeCat(), FakeGate()
    rig = PlatformRig(cat, gate, rig_mode="USB", backend=FakeIcomBackend())
    asyncio.run(rig.set_filter_width(3000))
    assert ("width", 1) in cat.commands      # nearest to 3000 Hz is FIL1
    asyncio.run(rig.set_filter_width(250))
    assert ("width", 3) in cat.commands      # 250 Hz is the narrowest, FIL3


def test_get_mode_reads_the_backend_reverse_table():
    cat, gate = FakeCat(), FakeGate()
    rig = PlatformRig(cat, gate, backend=FakeIcomBackend())
    cat.mode = 0x01                  # Icom USB
    name, _ = asyncio.run(rig.get_mode())
    assert name == "USB"
    cat.mode = 0x00                  # Icom LSB
    name, _ = asyncio.run(rig.get_mode())
    assert name == "LSB"


def test_backend_without_tables_falls_back_to_the_ft710_ladder():
    class Bare:
        pass

    cat, gate = FakeCat(), FakeGate()
    rig = PlatformRig(cat, gate, rig_mode="DATA-U", backend=Bare())
    asyncio.run(rig.set_filter_width(3000))
    assert ("width", 18) in cat.commands     # FT-710 ladder still used as a fallback


def test_mode_table_may_also_be_a_plain_method():
    """A backend that exposes ``mode_name_to_num`` as a method must work too.

    The real ones are properties; accepting both is what keeps a future backend
    from silently falling back to the Yaesu table (the failure is invisible —
    it only shows up as "FT8 selects the wrong mode on my radio").
    """

    class Plain:
        def mode_name_to_num(self):
            return {"USB": 0x01, "AM": 0x02}

        def filter_tables(self):
            return {"narrow": [(1, 3000)]}

    cat, gate = FakeCat(), FakeGate()
    rig = PlatformRig(cat, gate, rig_mode="USB", backend=Plain())
    asyncio.run(rig.set_mode("USB", 3000))
    assert cat.mode == 0x01
