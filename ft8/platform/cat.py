"""PlatformRig: the engine's rig seam over the platform's CatController.

Mode/frequency/filter widths go straight to ``CatController`` (the platform
already owns the serial link; no hamlib).  PTT does NOT touch CAT directly —
it goes through the platform TX gate, which owns the keying sequence, the
audio stream start/stop and the TX owner bookkeeping.
"""
from __future__ import annotations

from typing import Any, Protocol

from backends.ft710.config_ft710 import FILTER_WIDTHS_NARROW
from ft8.engine.rig import RigError

FT710_MODE_NUM = {"LSB": 0x01, "USB": 0x02, "CW-U": 0x03, "FM": 0x04, "AM": 0x05,
                  "RTTY-L": 0x06, "CW-L": 0x07, "DATA-L": 0x08, "RTTY-U": 0x09,
                  "DATA-FM": 0x0A, "FM-N": 0x0B, "DATA-U": 0x0C, "AM-N": 0x0D,
                  "PSK": 0x0E, "DATA-FM-N": 0x0F}
_FT710_MODE_NAME = {v: k for k, v in FT710_MODE_NUM.items()}

DEFAULT_PASSBAND_HZ = 3000


class TxGate(Protocol):
    async def key(self) -> bool: ...
    async def unkey(self, graceful: bool = False) -> None: ...
    @property
    def keyed(self) -> bool: ...


class PlatformRig:
    """``RigClient`` over ``CatController`` + the platform TX gate."""

    def __init__(self, cat: Any, tx_gate: TxGate, *, rig_mode: str = "DATA-U",
                 passband_hz: int = DEFAULT_PASSBAND_HZ) -> None:
        self._cat = cat
        self._gate = tx_gate
        self._rig_mode = rig_mode
        self._passband_hz = passband_hz

    # ---- PTT: the platform gate is the only path -------------------------
    async def set_ptt(self, transmit: bool) -> None:
        if transmit:
            ok = await self._gate.key()
            if not ok:
                raise RigError("PTT", "platform TX gate refused to key")
        else:
            await self._gate.unkey(graceful=False)

    async def get_ptt(self) -> bool:
        return bool(self._gate.keyed)

    # ---- frequency -------------------------------------------------------
    async def get_frequency(self) -> int:
        value = await self._cat.get_frequency()
        if value is None:
            raise RigError("CAT", "no frequency reply")
        return int(value)

    async def set_frequency(self, frequency_hz: int) -> None:
        if not await self._cat.set_frequency(int(frequency_hz)):
            raise RigError("CAT", f"set_frequency({frequency_hz}) refused")

    # ---- mode ------------------------------------------------------------
    async def get_mode(self) -> tuple[str, int]:
        num = await self._cat.get_mode()
        name = _FT710_MODE_NAME.get(int(num), "USB") if num is not None else "USB"
        return name, self._passband_hz

    async def set_mode(self, mode: str, passband_hz: int = DEFAULT_PASSBAND_HZ) -> None:
        if not mode:
            mode = self._rig_mode
        num = FT710_MODE_NUM.get(mode.upper())
        if num is None:
            raise RigError("CAT", f"unknown mode {mode!r}")
        self._passband_hz = int(passband_hz) or DEFAULT_PASSBAND_HZ
        if not await self._cat.set_mode(num):
            raise RigError("CAT", f"set_mode({mode}) refused")

    async def set_filter_width(self, hz: int) -> None:
        index = min(FILTER_WIDTHS_NARROW, key=lambda row: abs(row[1] - int(hz)))[0]
        if not await self._cat.set_filter_width(index):
            raise RigError("CAT", f"set_filter_width({hz} Hz) refused")
