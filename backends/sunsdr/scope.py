"""SunSDR2 DX scope producer
===========================
Adapts the Task-3 engine's rolling-IQ spectrum to the server's shared
``ScopeHandler`` so ``server.py`` and the frontend stay model-agnostic
(same contract as the Xiegu G7250's ``lan_iq`` producer):

- 850 bins into ``spectrum_rx1``; the single receiver leaves ``spectrum_rx2``
  empty; ``scope_mode`` is CENTER (the VFO sits at the centre of the IQ
  window by construction — the device places it at ``-IF_OFFSET_HZ`` inside
  the stream and the DSP mixes it to 0).
- ``scope_span`` is the IQ-rate index (0..3); ``scope_start_freq`` is the
  current VFO minus the span's half-width, so the waterfall axis matches
  what the browser computes from ``capabilities.scope_spans``.
- ``start()``/``stop()`` only control THIS producer's emit task.  The engine
  is owned by the backend (audio and FT8 may still be consuming it), so
  stopping the waterfall never stops the radio session.
- ``notify_tx`` is a deliberate no-op: unlike the FT-710's FT4222 SPI
  stream, nothing suggests the SunSDR2 garbles its own IQ while
  transmitting — pausing would blink the waterfall on every over.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from backends.ft710.scope_frame import WF_SIZE

if TYPE_CHECKING:
    from scope_handler import ScopeHandler

from backends.sunsdr.engine import SunSDREngine

logger = logging.getLogger(__name__)

OnFrameCallback = Callable[["ScopeHandler"], Awaitable[None]]

# UI span index -> IQ stream half-width (Hz).  These are the four real
# SAMPLE_RATES halved and rounded the same way the protocol table derives
# them: 39062/2=19531, 78125/2=39062 (floor), 156250/2=78125, 312500/2=156250.
# ``capabilities.scope_spans`` publishes this same map.
SCOPE_HALF_SPANS: dict[int, int] = {
    0: 19531,     # 39k IQ  (39062 Hz stream)
    1: 39062,     # 78k IQ  (78125 Hz stream, default)
    2: 78125,     # 156k IQ (156250 Hz stream)
    3: 156250,    # 312k IQ (312500 Hz stream)
}
DEFAULT_SPAN_INDEX = 1
DEFAULT_FPS = 15.0


class SunSDRScopeProducer:
    """ScopeProducer over the shared SunSDREngine's newest IQ samples."""

    def __init__(self, engine: SunSDREngine,
                 scope: Optional["ScopeHandler"] = None,
                 on_frame: Optional[OnFrameCallback] = None, *,
                 fps: float = DEFAULT_FPS):
        self._engine = engine
        self._scope = scope
        self._on_frame = on_frame
        # Written by the controller on set_scope_span(); read at emit time.
        self.span_index: int = DEFAULT_SPAN_INDEX
        self._period = 1.0 / max(1.0, float(fps))
        self._emit_task: Optional[asyncio.Task] = None
        self._running = False
        self.frames_emitted = 0
        self._tx_noop_logged = False

    # ── ScopeProducer protocol ───────────────────────────────────

    async def start(self) -> None:
        """Start the emit task.  Idempotent; never boots or rebinds the
        engine — that is the backend connection's job."""
        if self._running:
            return
        self._running = True
        self._emit_task = asyncio.create_task(self._emit_loop(),
                                              name="sunsdr-scope")

    async def stop(self) -> None:
        """Stop only this producer's emit task; the engine keeps running
        (RX audio / FT8 / recording may still be consuming it)."""
        self._running = False
        task = self._emit_task
        self._emit_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def notify_tx(self, active: bool) -> None:
        """No-op: nothing suggests the SunSDR2 garbles IQ during TX."""
        if active and not self._tx_noop_logged:
            self._tx_noop_logged = True
            logger.debug("SunSDR2 DX scope keeps streaming during TX (no pause)")

    def set_on_frame(self, cb: Optional[OnFrameCallback]) -> None:
        self._on_frame = cb

    # ── Emit loop ────────────────────────────────────────────────

    async def _emit_loop(self):
        while self._running:
            await asyncio.sleep(self._period)
            try:
                await self._emit_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:                          # noqa: BLE001
                logger.warning("SunSDR2 DX scope emit error: %s", e)

    async def _emit_once(self) -> bool:
        """Build one frame from the engine's rolling IQ.  Emits nothing
        while the engine is down — an idle waterfall must not look like
        signal-free RF."""
        if not self._engine.connected:
            if self._scope is not None:
                self._scope._connected = False
            return False
        frame = self._engine.get_spectrum(WF_SIZE)
        self.frames_emitted += 1
        scope = self._scope
        if scope is None:
            return True
        scope._connected = True
        scope.spectrum_rx1 = frame
        scope.spectrum_rx2 = [0] * WF_SIZE
        scope.scope_mode = 0                     # CENTER
        scope.scope_span = self.span_index
        scope.scope_start_freq = (int(self._engine.freq)
                                  - SCOPE_HALF_SPANS[self.span_index])
        # s_meter stays at its -1 sentinel: the S-meter belongs to the CAT
        # poll path (from filtered IQ energy), not to scope frames.
        scope.last_update = time.time()
        scope._frame_count += 1
        if self._on_frame is not None:
            await self._on_frame(scope)
        return True
