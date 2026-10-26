"""PlatformRig: the engine's rig seam over the platform's CatController.

Mode/frequency/filter widths go straight to ``CatController`` (the platform
already owns the serial link; no hamlib).  PTT does NOT touch CAT directly —
it goes through the platform TX gate, which owns the keying sequence, the
audio stream start/stop and the TX owner bookkeeping.

**Mode numbers and filter widths are per-backend, never per-family.**  The
FT-710 and the Icom CI-V radios disagree on both:

* mode bytes — Yaesu ``LSB=0x01/USB=0x02/DATA-U=0x0C`` vs Icom
  ``LSB=0x00/USB=0x01/AM=0x02``.  Sending the Yaesu ``0x02`` to an IC-7300
  selects **AM**, not USB, and FT8 then transmits into the wrong passband.
* filter indexes — the FT-710 has 21 selectable widths (50…4000 Hz) while an
  IC-7300 has three (FIL1/FIL2/FIL3).  Mapping 3000 Hz through the FT-710
  table yields index 18, which ``IC7300Backend.set_filter_width`` rejects
  outright (``index not in (1, 2, 3)`` → ``False``).

Measured 2026-10-26 on bg7kxr (IC-7300, v1.28.8): every FT8 start failed with
``RigError: CAT: set_filter_width(3000 Hz) refused`` and
``POST /api/ft8/mode`` answered 500 — FT8 was unusable on that model.

So when the caller passes ``backend`` we take both tables from it; only a
backend-less rig (unit tests, legacy call sites) falls back to the FT-710
tables.  The hardware capabilities are the single source of truth.
"""
from __future__ import annotations

from typing import Any, Protocol

from backends.ft710.config_ft710 import FILTER_WIDTHS_NARROW
from ft8.engine.rig import RigError

FT710_MODE_NUM = {"LSB": 0x01, "USB": 0x02, "CW-U": 0x03, "FM": 0x04, "AM": 0x05,
                  "RTTY-L": 0x06, "CW-L": 0x07, "DATA-L": 0x08, "RTTY-U": 0x09,
                  "DATA-FM": 0x0A, "FM-N": 0x0B, "DATA-U": 0x0C, "AM-N": 0x0D,
                  "PSK": 0x0E, "DATA-FM-N": 0x0F}

DEFAULT_PASSBAND_HZ = 3000


class TxGate(Protocol):
    async def key(self) -> bool: ...
    async def unkey(self, graceful: bool = False) -> None: ...
    @property
    def keyed(self) -> bool: ...


class PlatformRig:
    """``RigClient`` over ``CatController`` + the platform TX gate."""

    def __init__(self, cat: Any, tx_gate: TxGate, *, rig_mode: str = "DATA-U",
                 passband_hz: int = DEFAULT_PASSBAND_HZ, backend: Any = None) -> None:
        self._cat = cat
        self._gate = tx_gate
        self._rig_mode = rig_mode
        self._passband_hz = passband_hz
        # The RadioBackend that owns ``cat``.  Optional so existing call sites
        # and the unit tests keep working; when present it decides what a mode
        # name and a filter width mean (see the module docstring).
        self._backend = backend

    # ---- per-backend tables ---------------------------------------------
    def _mode_map(self) -> dict[str, int]:
        """Mode name -> that radio's own mode byte.

        ``RadioBackend.mode_name_to_num`` is a **property** (base.py:210, and
        every backend overrides it as one), not a method.  Reading it with ``()``
        raises ``TypeError: 'dict' object is not callable`` — which the guard
        below would swallow into the FT-710 fallback, i.e. straight back to the
        bug this method exists to fix.  Accept both shapes so a future backend
        written as a plain method also works.
        """
        if self._backend is not None:
            try:
                table = self._backend.mode_name_to_num
                if callable(table):
                    table = table()
            except Exception:                                   # noqa: BLE001
                table = None
            if table:
                return dict(table)
        return dict(FT710_MODE_NUM)

    def _narrow_table(self) -> list[tuple[int, int]]:
        """(filter index, width Hz) rows for this radio's narrow filter."""
        if self._backend is not None:
            try:
                tables = self._backend.filter_tables()
            except Exception:                                   # noqa: BLE001
                tables = None
            narrow = (tables or {}).get("narrow")
            if narrow:
                return list(narrow)
        return list(FILTER_WIDTHS_NARROW)

    def _resolve_mode(self, mode: str) -> str:
        """Map a requested mode onto one this radio actually has.

        The FT8 settings store defaults to ``rig_mode="DATA-U"`` — a Yaesu
        mode.  An IC-7300 has no DATA sub-modes at all (its CI-V mode list is
        LSB/USB/AM/CW/RTTY/FM), so passing ``DATA-U`` straight through raised
        ``RigError: CAT: unknown mode 'DATA-U'`` and FT8 still refused to start
        after the filter bug was fixed (bg7kxr, 2026-10-26).

        Preference order: the requested mode; an exact data mode; the plain
        sideband that carries the same traffic; anything the radio offers.
        """
        table = self._mode_map()
        wanted = (mode or self._rig_mode or "").upper()
        if wanted in table:
            return wanted
        # ``DATA-U`` on an Icom is just USB with the PC audio routed to it.
        stem = wanted[:-2] if wanted.endswith(("-U", "-L")) else wanted
        for candidate in (wanted, "DATA-" + stem, stem, "USB", "LSB", "CW-U"):
            if candidate in table:
                return candidate
        return next(iter(table), "USB")

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
        if num is None:
            return "USB", self._passband_hz
        name = {v: k for k, v in self._mode_map().items()}.get(int(num), "USB")
        return name, self._passband_hz

    async def set_mode(self, mode: str, passband_hz: int = DEFAULT_PASSBAND_HZ) -> None:
        name = self._resolve_mode(mode)
        num = self._mode_map().get(name)
        if num is None:                                         # unreachable, defensive
            raise RigError("CAT", f"unknown mode {mode!r}")
        self._rig_mode = name
        self._passband_hz = int(passband_hz) or DEFAULT_PASSBAND_HZ
        if not await self._cat.set_mode(num):
            raise RigError("CAT", f"set_mode({name}) refused")

    async def set_filter_width(self, hz: int) -> None:
        table = self._narrow_table()
        index = min(table, key=lambda row: abs(row[1] - int(hz)))[0]
        if not await self._cat.set_filter_width(index):
            raise RigError("CAT", f"set_filter_width({hz} Hz) refused (index {index})")
