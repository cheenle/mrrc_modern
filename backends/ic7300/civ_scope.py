"""
IC-7300 scope producer — in-process CI-V 0x27 0x00 waveform consumer
====================================================================
Implements the ``ScopeProducer`` protocol from ``backends.base`` for the
Icom IC-7300.  Unlike the FT-710 producer (FT4222 SPI subprocess), the
IC-7300 delivers its scope waveform as CI-V frames over the same serial
port as CAT, so this producer is a plain asyncio task: it drains parsed
``ScopeSegment``s from ``CivController.scope_queue`` (fed by the
reader-thread demux), reassembles complete 475-bin waveforms with
``ScopeAssembler``, scales/upsamples them to the shared 850-bin format,
and writes the result into the server's ``ScopeHandler`` in place —
mirroring how ``FT710ScopeProducer`` fills the same fields.

Field conventions (matched to the FT-710 frame metadata + frontend):

- ``spectrum_rx1``: 850 bins, 0-255 (CI-V bins are 0-160 → rescaled);
  ``spectrum_rx2`` is all zeros (the IC-7300 is single-receiver).
- ``scope_mode``: raw Icom mode code (0=center, 1=fixed, 2=scroll-c,
  3=scroll-f) — an int like the FT-710's ``scope_mode`` so the shared
  ``radioState.scope_mode`` path stays numeric.
- ``scope_span``: the UI span INDEX (reverse-mapped from the BCD
  half-span via ``SCOPE_SPAN_HZ``), not Hz — the frontend renders the
  frequency scale as ``SCOPE_SPAN_HZ[radioState.scope_span]`` and
  ``server.py`` round-trips the index into ``set_scope_span()``.
- ``scope_start_freq``: left-edge frequency in Hz.  Center mode:
  ``center_freq_hz - span_hz`` (the CI-V span is a HALF-span, so
  ±100 kHz at 14.074 MHz → 13_974_000); fixed mode: ``low_edge_hz``.

Reconnect survival: the only carried state is the assembler plus the
last info chunk — both are reset by the next seq-1 segment, so the
consumer keeps working across controller reconnects (the server
re-runs ``backend.init_scope()`` which re-enables scope output).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from backends.ft710.scope_frame import WF_SIZE
from backends.ic7300.civ_codec import (
    ScopeAssembler,
    ScopeSegment,
    SCOPE_AMPLITUDE_MAX,
    SCOPE_MODE_CENTER,
    SCOPE_MODE_SCROLL_C,
    scale_scope_bins,
    upsample_bins,
)
from backends.ic7300.config_ic7300 import SCOPE_SPAN_HZ

if TYPE_CHECKING:
    from backends.ic7300.civ_controller import CivController
    from scope_handler import ScopeHandler

logger = logging.getLogger("ic7300.scope")

OnFrameCallback = Callable[["ScopeHandler"], Awaitable[None]]

# Stall watchdog: warn once when no complete waveform arrives within
# this window while the producer is running (scope output disabled on
# the radio, CAT wedged, ...).  Module-level so tests can shorten it.
STALL_TIMEOUT_S = 3.0

_DEBUG_LOG_EVERY = 30          # waveforms between debug log lines

# Reverse map: half-span Hz -> UI span index (SCOPE_SPAN_HZ is idx->Hz).
_HALF_SPAN_TO_INDEX = {hz: idx for idx, hz in SCOPE_SPAN_HZ.items()}


class CivScopeProducer:
    """ScopeProducer for the IC-7300: in-process CI-V waveform consumer.

    Parameters
    ----------
    controller:
        The backend's ``CivController``; parsed scope segments arrive on
        its ``scope_queue``.
    scope:
        The server's shared ``ScopeHandler``; waveform fields are
        written into it in-place (may be None in tests).
    on_frame:
        Async callback invoked with the scope handler after each
        complete waveform (server.py merges scope metadata into
        RadioState there).
    """

    def __init__(
        self,
        controller: "CivController",
        scope: Optional["ScopeHandler"] = None,
        on_frame: Optional[OnFrameCallback] = None,
    ):
        self._civ = controller
        self._scope = scope
        self._on_frame = on_frame
        self._assembler = ScopeAssembler()
        self._task: Optional[asyncio.Task] = None
        # True between start() and stop() — gates the stall warning.
        self._active: bool = False
        # Info-chunk metadata latched from the current waveform's seq-1
        # segment (plain ints — nothing here breaks when the controller
        # reconnects and the queue keeps flowing).
        self._scope_mode: Optional[int] = None
        self._center_freq_hz: Optional[int] = None
        self._span_hz: Optional[int] = None
        self._low_edge_hz: Optional[int] = None
        self._high_edge_hz: Optional[int] = None
        self._waveforms: int = 0
        self._stall_warned: bool = False

    # ── ScopeProducer protocol ─────────────────────────────────────

    def set_on_frame(self, cb: OnFrameCallback) -> None:
        self._on_frame = cb

    def notify_tx(self, tx: bool) -> None:
        """No-op for the IC-7300: its CI-V scope stream is delivered over
        the serial CAT link and is NOT garbled by TX (unlike the FT-710's
        FT4222 SPI stream, which the pipe pauses during TX).  The method
        exists to satisfy the ScopeProducer protocol."""
        return None

    async def start(self) -> None:
        """Start the segment-consumer task (idempotent)."""
        self._active = True
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._consume_loop(), name="civ_scope")
        logger.info("CI-V scope consumer started")

    async def stop(self) -> None:
        """Stop the consumer when no spectrum clients remain."""
        self._active = False
        task = self._task
        self._task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        # Drain queued segments so a later start() cannot reassemble a
        # waveform across this scope-off/on boundary.
        while True:
            try:
                self._civ.scope_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if self._scope:
            self._scope._connected = False

    # ── Consumer loop ──────────────────────────────────────────────

    async def _consume_loop(self) -> None:
        queue = self._civ.scope_queue
        try:
            while True:
                try:
                    segment = await asyncio.wait_for(
                        queue.get(), timeout=STALL_TIMEOUT_S)
                except asyncio.TimeoutError:
                    self._check_stall()
                    continue
                if segment.is_division_start:
                    self._capture_info(segment)
                bins = self._assembler.feed(segment)
                if bins is None:
                    continue
                await self._handle_waveform(bins)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("CI-V scope consumer error: %s", e)

    def _check_stall(self) -> None:
        if not self._active or self._stall_warned:
            return
        self._stall_warned = True
        logger.warning(
            "CI-V scope stream stalled — no complete waveform for >%gs "
            "(scope data output enabled on the radio?)", STALL_TIMEOUT_S)

    def _capture_info(self, segment: ScopeSegment) -> None:
        """Latch the seq-1 info chunk for the waveform now assembling."""
        self._scope_mode = segment.scope_mode
        self._center_freq_hz = segment.center_freq_hz
        self._span_hz = segment.span_hz
        self._low_edge_hz = segment.low_edge_hz
        self._high_edge_hz = segment.high_edge_hz

    async def _handle_waveform(self, bins: list[int]) -> None:
        self._stall_warned = False
        self._waveforms += 1
        scope = self._scope
        # Out-of-range segments carry no bins (Icom omits the waveform);
        # nothing to display, but the stream is alive (stall resets).
        if scope is None or not bins:
            return

        scope.spectrum_rx1 = upsample_bins(
            scale_scope_bins(bins, SCOPE_AMPLITUDE_MAX, 255), WF_SIZE)
        scope.spectrum_rx2 = [0] * WF_SIZE  # single-receiver radio

        if self._scope_mode is not None:
            scope.scope_mode = self._scope_mode
            if self._scope_mode in (SCOPE_MODE_CENTER, SCOPE_MODE_SCROLL_C):
                if (self._center_freq_hz is not None
                        and self._span_hz is not None):
                    idx = _HALF_SPAN_TO_INDEX.get(self._span_hz)
                    if idx is not None:
                        scope.scope_span = idx
                    # Left-edge frequency (frontend freq-scale convention);
                    # the CI-V span is a half-span.
                    scope.scope_start_freq = (
                        self._center_freq_hz - self._span_hz)
            else:
                if self._low_edge_hz is not None:
                    scope.scope_start_freq = self._low_edge_hz

        now = time.time()
        if scope._last_frame_time > 0:
            dt = now - scope._last_frame_time
            if dt > 0:
                scope._fps = scope._fps * 0.9 + (1.0 / dt) * 0.1
        scope._last_frame_time = now
        scope._frame_count += 1
        scope.last_update = now

        if not scope._connected:
            scope._connected = True
            logger.info(
                "CI-V scope: first complete waveform — spectrum active "
                "(mode=%s, span=%s Hz, bins=%d)",
                self._scope_mode, self._span_hz, len(bins))
        if self._waveforms % _DEBUG_LOG_EVERY == 0:
            logger.debug(
                "CI-V scope: %d waveforms, fps=%.1f, mode=%s, span=%s",
                self._waveforms, scope._fps, self._scope_mode, self._span_hz)

        if self._on_frame:
            await self._on_frame(scope)
