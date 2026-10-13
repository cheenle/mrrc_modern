"""FT8 work mode: the SSB ↔ FT8 switch, snapshot and engine lifecycle.

The mode is a server-wide singleton (design §4.5): one owner of the radio at
a time, all sessions observe the same state.  Switching is serialized; a
failed transition always lands back in SSB, never half-started.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum

log = logging.getLogger("mrrc.ft8.mode")


class WorkMode(StrEnum):
    SSB = "ssb"
    FT8 = "ft8"


class Ft8ModeController:
    """Global SSB ↔ FT8 switch.  All collaborators are injected (testable)."""

    def __init__(self, *, snapshot_ssb: Callable[[], Awaitable[dict]],
                 apply_ft8_radio: Callable[[], Awaitable[None]],
                 restore_ssb: Callable[[dict], Awaitable[None]],
                 start_engine: Callable[[], Awaitable[None]],
                 stop_engine: Callable[[], Awaitable[None]],
                 set_browser_tx: Callable[[bool], None],
                 broadcast: Callable[[str], Awaitable[None]],
                 engine_always_on: Callable[[], bool] | None = None) -> None:
        self._snapshot_ssb = snapshot_ssb
        self._apply_ft8_radio = apply_ft8_radio
        self._restore_ssb = restore_ssb
        self._start_engine = start_engine
        self._stop_engine = stop_engine
        self._set_browser_tx = set_browser_tx
        self._broadcast = broadcast
        # Always-on keeps the RX monitor running in both modes (TX still
        # needs an explicit arm), so decoding never stops across restarts.
        self._always_on = engine_always_on or (lambda: False)
        self._mode = WorkMode.SSB
        self._lock = asyncio.Lock()
        self._snapshot: dict | None = None

    @property
    def mode(self) -> WorkMode:
        return self._mode

    async def switch_to_ft8(self) -> None:
        async with self._lock:
            if self._mode is WorkMode.FT8:
                return
            self._snapshot = await self._snapshot_ssb()
            self._set_browser_tx(False)
            try:
                await self._apply_ft8_radio()
                if not self._always_on():
                    await self._start_engine()
            except Exception:
                log.exception("FT8 start failed; rolling back to SSB")
                await self._safe_rollback()
                raise
            self._mode = WorkMode.FT8
            await self._broadcast("ft8")

    async def switch_to_ssb(self) -> None:
        async with self._lock:
            if self._mode is WorkMode.SSB:
                return
            # Safety first: stop TX (and the engine unless it is always-on)
            # before restoring the SSB radio.
            if not self._always_on():
                await self._stop_engine()
            else:
                # The engine keeps running; only its transmitter goes away.
                self._set_browser_tx(True)
            if self._snapshot is not None:
                await self._restore_ssb(self._snapshot)
            self._set_browser_tx(True)
            self._mode = WorkMode.SSB
            await self._broadcast("ssb")

    async def _safe_rollback(self) -> None:
        try:
            await self._stop_engine()
        except Exception:
            log.exception("engine stop failed during rollback")
        if self._snapshot is not None:
            try:
                await self._restore_ssb(self._snapshot)
            except Exception:
                log.exception("SSB restore failed during rollback")
        self._set_browser_tx(True)
