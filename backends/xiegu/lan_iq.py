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
import contextlib
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

#: How long the stream may stay dry before the tune frame is repeated.
#:
#: The 14-byte frame is also the "stream to me" request: a radio that never
#: got one (nothing was tuned yet) or that rebooted sends nothing at all, and
#: the waterfall falls back to the S-meter synthesiser with no other symptom.
#: Field case 2026-11-12 (bundle `20261112-114916-7b99`): bound socket,
#: `S-meter fallback` 0.4 s later, four sessions in a row.
RETUNE_WHILE_DRY_S = 5.0

_DEBUG_PACKETS = 2000                            # packets between debug lines
_NO_IQ_WARN_S = 5.0                              # silence before the one warning


# ── Protocol helpers (pure, unit-tested) ───────────────────────────

def build_tune_frame(freq_hz: int) -> bytes:
    """The 14-byte LAN tune frame for ``freq_hz`` (clamped like the vendor DLL)."""
    try:
        hz = int(freq_hz)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not a frequency: {freq_hz!r}") from exc
    hz = max(LAN_TUNE_MIN_HZ, min(LAN_TUNE_MAX_HZ, hz))
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
    # Window the WHOLE FFT and zero-fill past the samples available.  Windowing
    # only n values left a (fft_size,) buffer times an (n,) window, which
    # raises "operands could not be broadcast together" on every block that
    # arrived short — the emit loop logged it and drew nothing (found while
    # driving the LAN audio path, 2026-11-12).
    window = np.hanning(fft_size)
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
        try:
            self._iq_port = int(iq_port)
            self._control_port = int(control_port)
            self._half_span_hz = int(half_span_hz)
            self._period = 1.0 / max(1.0, float(fps))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"G7250 LAN IQ: bad port/span/fps ({exc})") from exc
        self._swap_iq = bool(swap_iq)

        #: Every IQ block, for whoever wants the same samples the FFT gets.
        #: The RX demodulator is the only consumer today (`audio.py`): one
        #: socket, one tune frame, two products — that is the whole point of
        #: the network baseband.
        self.on_iq: Optional[Callable[[np.ndarray], None]] = None

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
        # No-IQ watchdog: a bound socket that never receives anything is the
        # signature of a wrong address, an unplugged cable or a firewall — and
        # the customer reads it as "the waterfall is broken".  One warning per
        # dry streak, one all-clear.
        self._dry_since: Optional[float] = None
        self._dry_logged = False
        self._last_retune_at = 0.0

    # ── Late attachment ──────────────────────────────────────────

    def attach(
        self,
        scope: Optional["ScopeHandler"] = None,
        on_frame: Optional[OnFrameCallback] = None,
    ) -> None:
        """Hand over the server's scope handler after construction.

        The backend builds this producer from ``create_audio_handler()`` (the
        RX demodulator needs the socket more than the waterfall does — audio
        has no fallback, the waterfall has the S-meter synthesiser), but the
        server only has a ``ScopeHandler`` later in its lifespan.  Returning
        the same instance from ``create_scope_producer()`` keeps one socket and
        one tune-frame stream for both products.
        """
        if scope is not None:
            self._scope = scope
        if on_frame is not None:
            self._on_frame = on_frame

    def keeps_running(self) -> bool:
        """The server must not stop this stream when the last waterfall closes.

        On every other model the scope producer is the only user of its stream,
        so ``ws_spectrum`` stops it when the last socket leaves.  Here the same
        UDP socket is the RX audio source: stopping it would silence the radio
        for every listener (FT8 and the recorder included) because nobody has
        the waterfall open.
        """
        return True

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
        # Give the radio RETUNE_WHILE_DRY_S to answer that request before the
        # dry-stream watchdog repeats it.
        self._last_retune_at = time.monotonic()

    async def stop(self) -> None:
        self._running = False
        task, self._emit_task = self._emit_task, None
        if task is not None:
            task.cancel()
            # suppress() is the documented way to await a task you cancelled:
            # the CancelledError here is the one we asked for.
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def set_on_frame(self, cb: OnFrameCallback) -> None:
        """``ScopeProducer`` contract: register the per-frame async callback."""
        self._on_frame = cb

    def notify_tx(self, tx: bool) -> None:
        """No-op: nothing suggests the G7250 garbles IQ during TX.

        Kept for the ``ScopeProducer`` contract and logged once so a future
        field report ("waterfall freezes on TX") has a hook to land on.
        """
        if tx and not self._tx_pause_logged:
            self._tx_pause_logged = True
            logger.debug("G7250 LAN IQ keeps streaming during TX (no pause)")

    # ── Tuning ────────────────────────────────────────────────────

    async def tune(self, freq_hz: int, force: bool = False) -> bool:
        """Async form of :meth:`tune_now` (kept for the ``ScopeProducer`` path)."""
        return self.tune_now(freq_hz, force)

    def tune_now(self, freq_hz: int, force: bool = False) -> bool:
        """Send the LAN tune frame so the IQ window follows the VFO.

        Synchronous on purpose: the production path to this is a **CI-V
        controller callback** (``_G7250CivController``) that already runs on the
        event loop, and it cannot await.  Returns False when there is nothing to
        send yet (not configured / socket not open); the frequency is remembered
        and re-sent by ``start()``.
        """
        try:
            hz = int(freq_hz)
        except (TypeError, ValueError):
            logger.warning("G7250 LAN tune: %r is not a frequency", freq_hz)
            return False
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

    @property
    def tuned_hz(self) -> Optional[int]:
        """Where the IQ window is currently asked to be (None = never asked)."""
        return self._tuned_hz

    _last_sent_hz: Optional[int] = None

    # ── Datagram ingest ───────────────────────────────────────────

    def _ingest(self, payload: bytes) -> None:
        """Append one IQ payload and update the throughput counters."""
        samples = parse_iq_payload(payload)
        if samples.size == 0:
            return
        if self._swap_iq:
            # i/q swap written without .real/.imag: 1j * conj(i + jq) = q + ji.
            samples = (1j * np.conj(samples)).astype(np.complex64)
        if self._dry_logged:
            self._dry_logged = False
            self._dry_since = None
            logger.info("G7250 LAN IQ: packets are arriving again")
        if self.on_iq is not None:
            try:
                self.on_iq(samples)
            except Exception as exc:                  # noqa: BLE001
                # A demodulator that raises must not wedge the waterfall:
                # the spectrum is the product the customer watches.
                logger.warning("G7250 LAN IQ audio consumer failed: %s", exc)
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
            if self._rate_window_packets == 0 and not self._dry_logged:
                if self._dry_since is None:
                    self._dry_since = now
                elif now - self._dry_since >= _NO_IQ_WARN_S:
                    self._dry_logged = True
                    logger.warning(
                        "G7250 LAN IQ: no packets for %.0f s from %s "
                        "(bound UDP %s) — check the cable/switch, the address, "
                        "and that inbound UDP %d is allowed; a running HDSDR "
                        "owns this port too",
                        now - self._dry_since, self.radio_ip,
                        self.bound_port, self._iq_port)
            elif self._rate_window_packets:
                self._dry_since = None
            self._rate_window_packets = 0
            self._rate_window_start = now

    async def _emit_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._period)
            try:
                await self.emit_once()
                await self._maybe_resend_tune()
            except Exception as e:                                 # noqa: BLE001
                # CancelledError is a BaseException since 3.8, so cancellation
                # still propagates through this handler untouched.
                logger.warning("G7250 LAN IQ emit error: %s", e)

    async def _maybe_resend_tune(self, now: Optional[float] = None) -> bool:
        """Ask the radio again while nothing is arriving.

        Sending the tune frame *is* asking the radio to stream (it carries the
        frequency and, implicitly, "to this address").  It used to go out only
        when the operator tuned, so a freshly started server could sit on a
        bound socket forever with a synthetic waterfall — measured in the field
        (2026-11-12, BG7YQV): four sessions, four `listening on UDP 50001`,
        four `S-meter fallback`, zero packets.  Repeats are rate-limited and
        stop as soon as packets arrive.
        """
        if self._transport is None or not self._tuned_hz:
            return False
        if self.packets_received:
            return False
        stamp = time.monotonic() if now is None else now
        if stamp - self._last_retune_at < RETUNE_WHILE_DRY_S:
            return False
        self._last_retune_at = stamp
        return await self.tune(self._tuned_hz, force=True)

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
