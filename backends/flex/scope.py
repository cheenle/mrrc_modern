"""Spectrum producer for the FlexRadio backend.

``ScopeProducer`` over the engine's latest FFT frame (design §6).  Two
deliberate choices:

* **No waterfall stream is created.**  ``scope_handler.get_spectrum_binary()``
  builds wf1 *and* wf2 from ``spectrum_rx1``, so one panadapter FFT serves both
  displays — one less stream, one less sub-header format (the 36-byte tile
  parser is not written at all).
* **The pan is asked for a 256-pixel dBm aperture spanning -120..0 dB**, which
  makes a UI byte exactly ``255 - pixel`` (pinned in ``tests/test_flex_vita.py``
  and again here through the producer).

``notify_tx`` is a no-op: the VITA FFT stream does not stop while transmitting
(unlike the FT-710's FT4222 SPI link), so blinking the waterfall on every over
would be a regression, not a feature.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Dict, Optional

from backends.flex.engine import SmartSDREngine
from backends.ft710.scope_frame import WF_SIZE

if TYPE_CHECKING:
    from scope_handler import ScopeHandler

logger = logging.getLogger(__name__)

__all__ = ["FlexScopeProducer", "SPANS", "DEFAULT_SPAN_INDEX", "DEFAULT_FPS"]

OnFrameCallback = Callable[["ScopeHandler"], Awaitable[None]]

#: ``capabilities.scope_spans`` and the pan bandwidth for each index.  The
#: shapes match the FT-710's table (``{name, freq}``) so the frontend's axis
#: arithmetic does not need a Flex-specific branch.
SPANS: Dict[int, Dict[str, object]] = {
    0: {"name": "10 kHz", "freq": 10_000},
    1: {"name": "20 kHz", "freq": 20_000},
    2: {"name": "50 kHz", "freq": 50_000},
    3: {"name": "100 kHz", "freq": 100_000},
    4: {"name": "200 kHz", "freq": 200_000},
    5: {"name": "500 kHz", "freq": 500_000},
    6: {"name": "1 MHz", "freq": 1_000_000},
}
DEFAULT_SPAN_INDEX = 3
DEFAULT_FPS = 15.0


class FlexScopeProducer:
    """ScopeProducer over the shared engine's latest panadapter frame."""

    def __init__(self, engine: SmartSDREngine,
                 scope: Optional["ScopeHandler"] = None,
                 on_frame: Optional[OnFrameCallback] = None, *,
                 fps: float = DEFAULT_FPS) -> None:
        self._engine = engine
        self._scope = scope
        self._on_frame = on_frame
        self.span_index = DEFAULT_SPAN_INDEX
        self._period = 1.0 / max(1.0, float(fps))
        self._emit_task: Optional[asyncio.Task] = None
        self._running = False
        self.frames_emitted = 0

    # ── ScopeProducer protocol ───────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._emit_task = asyncio.create_task(self._emit_loop(),
                                              name="flex-scope")

    async def stop(self) -> None:
        self._running = False
        task = self._emit_task
        self._emit_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):   # noqa: BLE001
                pass

    def notify_tx(self, tx: bool) -> None:
        """No-op: the radio keeps sending FFT frames while transmitting.

        The parameter is named ``tx`` to match the ``ScopeProducer`` protocol —
        a protocol with a positional-only-looking name still type-checks by
        parameter name, and this one bit the first draft.
        """
        if tx:
            logger.debug("FlexRadio scope keeps streaming during TX")

    def set_on_frame(self, cb: Optional[OnFrameCallback]) -> None:
        self._on_frame = cb

    # ── span ─────────────────────────────────────────────────────

    @property
    def span_hz(self) -> int:
        entry = SPANS.get(self.span_index) or SPANS[DEFAULT_SPAN_INDEX]
        return int(entry["freq"])                     # type: ignore[arg-type]

    async def set_span(self, index: int) -> bool:
        """Select a span index; the pan bandwidth follows."""
        if index not in SPANS:
            return False
        self.span_index = int(index)
        return await self._engine.set_bandwidth(self.span_hz)

    # ── emit loop ────────────────────────────────────────────────

    async def _emit_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._period)
            try:
                await self._emit_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:                    # noqa: BLE001
                logger.warning("FlexRadio scope emit error: %s", exc)

    async def _emit_once(self) -> bool:
        scope = self._scope
        if not self._engine.connected:
            if scope is not None:
                scope._connected = False
            return False
        bins = self._engine.spectrum_bins()
        if bins is None:
            return False
        self.frames_emitted += 1
        if scope is None:
            return True
        span = self.span_hz
        center = self._engine.pan_center_hz or int(
            self._engine.slice_state().get("freq_hz", 0))
        scope._connected = True
        scope.spectrum_rx1 = bins[:WF_SIZE]
        scope.spectrum_rx2 = [0] * WF_SIZE
        scope.scope_mode = 0                       # CENTER
        scope.scope_span = self.span_index
        scope.scope_start_freq = int(center - span // 2)
        scope.last_update = time.time()
        scope._frame_count += 1
        if self._on_frame is not None:
            await self._on_frame(scope)
        return True
