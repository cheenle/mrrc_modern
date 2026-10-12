"""Rig seam expected by the safety controller.

The production implementation is ``ft8.platform.cat.PlatformRig`` (the
platform's ``CatController`` plus the shared TX gate).  MRRC-FT8's rigctld
client is deliberately not carried over: the platform already owns the
serial link, and the FT-710 hamlib workarounds that client contained are
obsolete here (``CatController`` speaks the radio's native CAT).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


class RigError(Exception):
    """A rig command failed; carries a stable code for interlocks."""

    def __init__(self, code: str, detail: str, rprt: int | None = None) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.rprt = rprt


@runtime_checkable
class RigClient(Protocol):
    """The surface the engine actually uses (safety PTT + the mode/slot data).

    ``set_ptt``/``get_ptt`` are expected to route through the platform TX
    gate; the frequency/mode methods carry the FT8 operating point.
    """

    async def set_ptt(self, transmit: bool) -> None: ...
    async def get_ptt(self) -> bool: ...
    async def get_frequency(self) -> int: ...
    async def set_frequency(self, frequency_hz: int) -> None: ...
    async def get_mode(self) -> tuple[str, int]: ...
    async def set_mode(self, mode: str, passband_hz: int) -> None: ...
    async def set_filter_width(self, hz: int) -> None: ...
