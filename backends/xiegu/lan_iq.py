"""
Xiegu G7250 LAN IQ scope producer
=================================
The G7250 cannot stream a spectrum over CAT: it has no CI-V ``0x27`` scope
command (the X6100/X6200 don't either).  What it has instead is the radio's
"network baseband" feature — a raw **IQ stream over UDP** — which is also the
only thing Xiegu documents for remote use (their own path is HDSDR with an
``ExtIO_G7250.dll``, receive only).

Protocol (third-party reverse engineering, hardware-tagged in
``github.com/nathan86daniels-commits/xiegu-g7250-open-control``):

- The radio listens on UDP **50002** for one 14-byte tune frame
  ``AA 55 55 AA | 00 01 | frequency as uint64 big-endian`` (clamped to
  100 kHz - 54 MHz by the vendor DLL).
- The PC receives IQ on UDP **50001**: 768-byte payloads, ~250/s, each 192 x
  (int16 I, int16 Q), little-endian → **48 kS/s** ≈ ±24 kHz around the tune
  frequency.
- The LAN link carries no PTT, mode or CAT — those stay on the USB CI-V port.

This module turns that stream into the same ``ScopeHandler`` fields the FT-710
FT4222 producer and the CI-V producer fill, so ``server.py`` and the frontend
stay model-agnostic (``on_frame`` merging still happens in ``server.py``).

Honesty notes
-------------
* The producer is **off** unless ``MRRC_G7250_LAN_IP`` is set: without the LAN
  cable the backend reports ``scope_type="none"`` and the UI falls back to the
  synthetic S-meter waterfall, exactly like the Yaesu ASCII-CAT models.
* Nothing here has been run against a real G7250.  The frame layout, sample
  rate and IQ sense (``swap IQ`` if mirrored) are the community's findings, and
  the first hardware session is what turns them into facts — see the spec's
  verification checklist.
* The producer never touches PTT.  ``notify_tx`` is a no-op with a one-time
  debug log: unlike the FT-710's FT4222 stream, nothing suggests the radio
  garbles IQ during TX, and pausing a stream that never garbles would blink the
  waterfall on every over.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections import deque
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import numpy as np

from backends.ft710.scope_frame import WF_SIZE
from backends.xiegu.xiegu_profiles import (
    LAN_CONTROL_PORT,
    LAN_IQ_HALF_SPAN_HZ,
    LAN_IQ_PORT,
    LAN_IQ_RATE_HZ,
    LAN_TUNE_HEADER,
    LAN_TUNE_MAX_HZ,
    LAN_TUNE_MIN_HZ,
)

if TYPE_CHECKING:
    from scope_handler import ScopeHandler

logger = logging.getLogger("xiegu.lan_iq")

OnFrameCallback = Callable[["ScopeHandler"], Awaitable[None]]

# UI span index published in RadioState.scope_span.  The backend advertises
# exactly one span (LAN_IQ_HALF_SPAN_HZ) through capabilities.scope_spans, so
# index 0 is the whole story — the frontend rebuilds its span table from those
# capabilities and renders a ±24 kHz scale from this index.
LAN_SPAN_INDEX = 0

# Spectrum dynamics.  The IQ stream is uncalibrated (no dBm reference), so the
# mapping is a fixed window rather than a calibrated scale: everything below
# _FLOOR_DB renders black, everything above _CEIL_DB renders white.  Tuned so a
# quiet 20 m band with a few S9 signals uses most of the palette.
_FLOOR_DB = -95.0
_CEIL_DB = -35.0

# FFT size is independent of the RF sample rate: 2048 bins over 48 kHz is
# ~23 Hz/bin, finer than the 850 display bins it is resampled to.
_FFT_SIZE = 2048
_MAX_BUFFERED_SAMPLES = LAN_IQ_RATE_HZ * 2       # 2 s of slack, then drop oldest
_DEFAULT_FPS = 10.0

_DEBUG_PACKETS = 2000                            # packets between debug lines


# ── Protocol helpers (pure, unit-tested) ───────────────────────────

def build_tune_frame(freq_hz: int) -> bytes:
    """The 14-byte LAN tune frame for ``freq_hz`` (clamped like the vendor DLL)."""
    hz = max(LAN_TUNE_MIN_HZ, min(LAN_TUNE_MAX_HZ, int(freq_hz)))
    return LAN_TUNE_HEADER + struct.pack(">Q", hz)


def parse_iq_payload(payload: bytes) -> np.ndarray:
    """Decode one 768-byte IQ payload into complex float32 samples.

    Tolerates odd lengths (a truncated datagram yields the whole pairs it
    contains) so a single malformed packet cannot wedge the stream.
    """
    if len(payload) < 4:
        return np.empty(0, dtype=np.complex64)
    n_pairs = len(payload) // 4
    raw = np.frombuffer(payload[:n_pairs * 4], dtype="<i2").astype(np.float32)
    return (raw[0::2] + 1j * raw[1::2]) / 32768.0


def iq_to_spectrum(
    samples: np.ndarray,
    bins: int = WF_SIZE,
    *,
    fft_size: int = _FFT_SIZE,
    floor_db: float = _FLOOR_DB,
    ceil_db: float = _CEIL_DB,
) -> list[int]:
    """Uncalibrated power spectrum, ``bins`` values 0-255, DC in the middle.

    Hann-windowed FFT, magnitude in dB, then a linear dB window mapped to the
    0-255 the waterfall palette expects (the same range the FT-710 and CI-V
    producers deliver).
    """
    if samples.size == 0:
        return [0] * bins
    n = min(len(samples), fft_size)
    window = np.hanning(n)
    buf = np.zeros(fft_size, dtype=np.complex64)
    buf[:n] = samples[-n:]
    spectrum = np.fft.fftshift(np.fft.fft(buf * window))
    mag = np.abs(spectrum) / max(1, n)
    with np.errstate(divide="ignore"):
        db = 20.0 * np.log10(np.maximum(mag, 1e-12))
    scaled = (db - floor_db) / max(1e-9, ceil_db - floor_db)
    scaled = np.clip(scaled, 0.0, 1.0) * 255.0
    if bins == len(scaled):
        return scaled.astype(np.uint8).tolist()
    # Linear resample of the FFT onto the display grid.
    idx = np.linspace(0, len(scaled) - 1, bins)
    resampled = np.interp(idx, np.arange(len(scaled)), scaled)
    return np.clip(resampled, 0, 255).astype(np.uint8).tolist()


# ── Producer ───────────────────────────────────────────────────────

class G7250LanIQProducer:
    """``ScopeProducer`` implementation over the G7250 UDP IQ stream.

    Parameters
    ----------
    radio_ip:
        The radio's address (``MRRC_G7250_LAN_IP``).  Empty means "not
        configured" — call ``start()`` only when it is set.
    scope:
        The server's shared ``ScopeHandler``; waveform fields are written into
        it in place (may be None in tests).
    on_frame:
        Async callback invoked with the scope handler after each emitted frame
        (``server.py`` merges scope metadata into ``RadioState`` there).
    """

    def __init__(
        self,
        radio_ip: str,
        scope: Optional["ScopeHandler"] = None,
        on_frame: Optional[OnFrameCallback] = None,
        *,
        iq_port: int = LAN_IQ_PORT,
        control_port: int = LAN_CONTROL_PORT,
        half_span_hz: int = LAN_IQ_HALF_SPAN_HZ,
        fps: float = _DEFAULT_FPS,
        swap_iq: bool = False,
    ):
        self.radio_ip = str(radio_ip or "").strip()
        self._scope = scope
        self._on_frame = on_frame
        self._iq_port = int(iq_port)
        self._control_port = int(control_port)
        self._half_span_hz = int(half_span_hz)
        self._period = 1.0 / max(1.0, float(fps))
        self._swap_iq = bool(swap_iq)

        self._transport = None
        self._emit_task: Optional[asyncio.Task] = None
        self._running = False
        self._samples: deque = deque(maxlen=_MAX_BUFFERED_SAMPLES)
        self._pending = np.empty(0, dtype=np.complex64)
        self._tuned_hz: Optional[int] = None
        self._tx_pause_logged = False

        # Counters exposed for /api diagnostics and the hardware session.
        self.packets_received = 0
        self.bytes_received = 0
        self.frames_emitted = 0
        self.packets_dropped = 0
        self.tune_frames_sent = 0
        self.bound_port: Optional[int] = None
        self._rate_window_start = 0.0
        self._rate_window_packets = 0
        self.measured_rate_hz = 0.0

    # ── ScopeProducer protocol ─────────────────────────────────────

    async def start(self) -> None:
        if self._running or not self.radio_ip:
            return
        loop = asyncio.get_running_loop()
        try:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: _IQProtocol(self),
                local_addr=("0.0.0.0", self._iq_port),
            )
        except OSError as e:
            # Another instance (or HDSDR) already owns the port.  Stay down
            # instead of thrashing: the server keeps the synthetic waterfall.
            logger.error("G7250 LAN IQ: cannot bind UDP %d: %s",
                         self._iq_port, e)
            return
        sock = self._transport.get_extra_info("sockname")
        self.bound_port = sock[1] if sock else self._iq_port
        self._running = True
        self._rate_window_start = time.monotonic()
        self._rate_window_packets = 0
        self._emit_task = asyncio.create_task(self._emit_loop())
        logger.info("G7250 LAN IQ listening on UDP %d (radio %s:%d), "
                    "+/-%.0f kHz", self.bound_port, self.radio_ip,
                    self._control_port, self._half_span_hz / 1000.0)
        if self._tuned_hz:
            await self.tune(self._tuned_hz, force=True)

    async def stop(self) -> None:
        self._running = False
        if self._emit_task is not None:
            self._emit_task.cancel()
            try:
                await self._emit_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._emit_task = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def notify_tx(self, active: bool) -> None:
        """No-op: nothing suggests the G7250 garbles IQ during TX.

        Kept for the ``ScopeProducer`` contract and logged once so a future
        field report ("waterfall freezes on TX") has a hook to land on.
        """
        if active and not self._tx_pause_logged:
            self._tx_pause_logged = True
            logger.debug("G7250 LAN IQ keeps streaming during TX (no pause)")

    # ── Tuning ────────────────────────────────────────────────────

    async def tune(self, freq_hz: int, force: bool = False) -> bool:
        """Send the LAN tune frame so the IQ window follows the VFO.

        Returns False when there is nothing to send (not configured / not
        started yet); the frequency is remembered and re-sent on start.
        """
        hz = int(freq_hz)
        self._tuned_hz = hz
        if self._transport is None:
            return False
        if not force and self.tune_frames_sent and hz == self._last_sent_hz:
            return True
        frame = build_tune_frame(hz)
        try:
            self._transport.sendto(frame, (self.radio_ip, self._control_port))
        except OSError as e:
            logger.warning("G7250 LAN tune failed (%s): %s", self.radio_ip, e)
            return False
        self.tune_frames_sent += 1
        self._last_sent_hz = hz
        return True

    _last_sent_hz: Optional[int] = None

    # ── Datagram ingest ───────────────────────────────────────────

    def _ingest(self, payload: bytes) -> None:
        """Append one IQ payload and update the throughput counters."""
        samples = parse_iq_payload(payload)
        if samples.size == 0:
            return
        if self._swap_iq:
            samples = samples.imag + 1j * samples.real
        if len(self._samples) + samples.size > _MAX_BUFFERED_SAMPLES:
            # Slow consumer (paused UI, starved CPU): keep the newest IQ.
            drop = len(self._samples) + samples.size - _MAX_BUFFERED_SAMPLES
            dropped = 0
            while self._samples and dropped < drop:
                self._samples.popleft()
                dropped += 1
            self.packets_dropped += 1
        self._samples.extend(samples)
        self.packets_received += 1
        self.bytes_received += len(payload)
        self._rate_window_packets += 1
        if self.packets_received % _DEBUG_PACKETS == 0:
            logger.debug("G7250 LAN IQ: %d packets (%.0f/s), %d frames emitted",
                         self.packets_received, self.measured_rate_hz,
                         self.frames_emitted)

    def _update_rate(self, now: float) -> None:
        elapsed = now - self._rate_window_start
        if elapsed >= 1.0:
            self.measured_rate_hz = self._rate_window_packets / elapsed
            self._rate_window_packets = 0
            self._rate_window_start = now

    async def _emit_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._period)
            try:
                await self.emit_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:                                 # noqa: BLE001
                logger.warning("G7250 LAN IQ emit error: %s", e)

    async def emit_once(self) -> bool:
        """Convert the buffered IQ into one scope frame (True when emitted)."""
        self._update_rate(time.monotonic())
        if not self._samples:
            return False
        samples = np.asarray(self._samples, dtype=np.complex64)
        self._samples.clear()
        frame = iq_to_spectrum(samples, WF_SIZE)
        self.frames_emitted += 1
        scope = self._scope
        if scope is not None:
            scope.spectrum_rx1 = frame
            # Single receiver: the second trace stays empty, as on the IC-7300.
            scope.spectrum_rx2 = [0] * WF_SIZE
            scope.scope_mode = 0                     # CENTER
            scope.scope_span = LAN_SPAN_INDEX
            if self._tuned_hz:
                scope.scope_start_freq = self._tuned_hz - self._half_span_hz
            # s_meter stays at its -1 sentinel: the IQ stream carries no
            # S-meter, so the CAT poll owns that field (same rule as CivScope).
            scope.last_update = time.time()
            scope._frame_count += 1
            if self._on_frame is not None:
                await self._on_frame(scope)
        return True


class _IQProtocol(asyncio.DatagramProtocol):
    """Minimal UDP sink that hands payloads to the producer."""

    def __init__(self, producer: G7250LanIQProducer):
        self._producer = producer

    def datagram_received(self, data: bytes, addr) -> None:
        self._producer._ingest(data)

    def error_received(self, exc: Exception) -> None:      # pragma: no cover
        logger.debug("G7250 LAN IQ socket error: %s", exc)
