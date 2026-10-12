"""Spectrum frames and the lossy fan-out input (§11.2, NFR-004, AD-007).

``SpectrumComputer`` turns the UTC-indexed 12 kHz int16 decoder stream into
fixed-cadence waterfall lines (target ~3.5 lines/s): Hann window, rFFT,
dB quantization to one byte per bin and a compact self-describing binary
frame for ``/ws/v1/waterfall``.  ``SpectrumFanout`` is the lossy delivery
input: every subscriber gets a bounded queue that drops the oldest frames
under backpressure, so a slow waterfall client can never delay state,
heartbeat or STOP TX traffic (SC8, §10).  Gap handling matches the ring: a
UTC discontinuity resets the line accumulator instead of bridging it.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

import numpy as np

from .audio_rx import DECODER_SAMPLE_RATE

DEFAULT_LINES_PER_SECOND = 3.5  # NFR-004 target cadence
DEFAULT_FFT_SIZE = 4_096
DB_FLOOR = -90.0
DB_CEILING = -30.0
DISPLAY_BANDWIDTH_HZ = 3_000.0
"""Audio span shown on the waterfall.  The 12 kHz stream carries 0..6 kHz,
but the FT8 passband is ~0..3 kHz; showing the full 6 kHz leaves the upper
half of the display blank, so only this band is emitted (the client maps
whatever bins it receives across the full canvas)."""

_FRAME_MAGIC = b"WF01"
_FRAME_HEADER = struct.Struct("<4sIQfH")
"""magic, sequence, epoch milliseconds, bin width Hz, bin count."""


@dataclass(frozen=True, slots=True)
class SpectrumFrame:
    """One quantized waterfall line covering ``0 .. display_bandwidth`` Hz."""

    seq: int
    epoch: float
    bin_hz: float
    bins: bytes

    def to_bytes(self) -> bytes:
        """Serialize to the compact binary wire frame (§10)."""

        return _FRAME_HEADER.pack(
            _FRAME_MAGIC,
            self.seq,
            round(self.epoch * 1_000),
            self.bin_hz,
            len(self.bins),
        ) + self.bins

    @classmethod
    def from_bytes(cls, payload: bytes) -> SpectrumFrame:
        """Parse one wire frame; raises ``ValueError`` on any inconsistency."""

        if len(payload) < _FRAME_HEADER.size:
            raise ValueError("short spectrum frame")
        magic, seq, epoch_ms, bin_hz, count = _FRAME_HEADER.unpack_from(payload)
        if magic != _FRAME_MAGIC:
            raise ValueError("bad spectrum frame magic")
        bins = payload[_FRAME_HEADER.size :]
        if len(bins) != count:
            raise ValueError("spectrum frame length mismatch")
        return cls(seq=seq, epoch=epoch_ms / 1_000, bin_hz=bin_hz, bins=bins)


class SpectrumComputer:
    """Fixed-cadence spectrum lines from the 12 kHz int16 UTC stream.

    ``push`` accepts arbitrary block sizes; line epochs derive from the
    absolute sample index, so chunking never shifts the cadence.  A UTC gap
    larger than one sample period resets the accumulator.
    """

    def __init__(
        self,
        *,
        lines_per_second: float = DEFAULT_LINES_PER_SECOND,
        fft_size: int = DEFAULT_FFT_SIZE,
        db_floor: float = DB_FLOOR,
        db_ceiling: float = DB_CEILING,
        display_bandwidth_hz: float = DISPLAY_BANDWIDTH_HZ,
    ) -> None:
        if not 0 < lines_per_second <= DECODER_SAMPLE_RATE:
            raise ValueError("lines_per_second out of range")
        if fft_size < 8 or fft_size & (fft_size - 1):
            raise ValueError("fft_size must be a power of two >= 8")
        if not db_floor < db_ceiling:
            raise ValueError("db_floor must be below db_ceiling")
        if not 0 < display_bandwidth_hz <= DECODER_SAMPLE_RATE / 2:
            raise ValueError("display bandwidth must be within 0..rate/2")
        self.fft_size = fft_size
        self.hop = round(DECODER_SAMPLE_RATE / lines_per_second)
        self.bin_hz = DECODER_SAMPLE_RATE / fft_size
        self._band_bins = int(display_bandwidth_hz / self.bin_hz) + 1
        self._floor = db_floor
        self._span = db_ceiling - db_floor
        self._window = np.hanning(fft_size).astype(np.float64)
        self._norm = 32_768.0 * float(fft_size)
        self._buf = np.zeros(0, dtype=np.int16)
        self._next_index: int | None = None  # absolute index of _buf[0]
        self._seq = 0

    def push(self, samples: np.ndarray, first_epoch: float) -> list[SpectrumFrame]:
        """Append one UTC-stamped block; return any completed lines."""

        pcm = np.asarray(samples)
        if pcm.dtype != np.int16 or pcm.ndim != 1:
            raise ValueError("waterfall input must be 1-D int16 at 12 kHz")
        start_index = round(first_epoch * DECODER_SAMPLE_RATE)
        if self._next_index is None or start_index != self._next_index + self._buf.size:
            self._buf = pcm.copy()  # gap or first block: restart the line
            self._next_index = start_index
        else:
            self._buf = np.concatenate([self._buf, pcm])
        frames: list[SpectrumFrame] = []
        while self._buf.size >= self.fft_size:
            window = self._buf[: self.fft_size]
            epoch = (self._next_index or 0) / DECODER_SAMPLE_RATE
            frames.append(self._analyze(window, epoch))
            advance = self.hop
            self._buf = self._buf[advance:]
            self._next_index = (self._next_index or 0) + advance
        return frames

    def _analyze(self, window: np.ndarray, epoch: float) -> SpectrumFrame:
        spectrum = np.abs(np.fft.rfft(window * self._window)) / self._norm
        db = 20.0 * np.log10(np.maximum(spectrum, 1e-12))
        # Emit only the FT8 passband (0..display_bandwidth); the client maps
        # these bins across the full canvas, so the upper blank half is gone.
        quantized = np.clip(
            (db[: self._band_bins] - self._floor) / self._span * 255.0, 0, 255
        )
        frame = SpectrumFrame(
            seq=self._seq,
            epoch=epoch,
            bin_hz=self.bin_hz,
            bins=quantized.astype(np.uint8).tobytes(),
        )
        self._seq += 1
        return frame


class SpectrumSubscription:
    """One subscriber's bounded, drop-oldest queue (NFR-004)."""

    def __init__(self, max_frames: int) -> None:
        self.queue: asyncio.Queue[SpectrumFrame] = asyncio.Queue(max_frames)
        self.dropped = 0
        self.received = 0
        self.closed = False

    def _offer(self, frame: SpectrumFrame) -> None:
        if self.closed:
            return
        if self.queue.full():
            try:
                self.queue.get_nowait()  # drop the oldest, keep the newest
            except asyncio.QueueEmpty:  # pragma: no cover - full() was true
                pass
            self.dropped += 1
        self.queue.put_nowait(frame)
        self.received += 1

    def close(self) -> None:
        """Detach from the fan-out; idempotent."""

        self.closed = True


class SpectrumFanout:
    """Lossy fan-out input: publish once, each subscriber keeps the newest."""

    def __init__(self, *, max_frames_per_subscriber: int = 8) -> None:
        if max_frames_per_subscriber < 1:
            raise ValueError("subscriber queue must hold at least one frame")
        self._max = max_frames_per_subscriber
        self._subscribers: list[SpectrumSubscription] = []
        self._dropped_retired = 0
        self.published = 0

    def subscribe(self) -> SpectrumSubscription:
        """Register a subscriber; call ``close`` to detach."""

        retired = [s for s in self._subscribers if s.closed]
        self._dropped_retired += sum(s.dropped for s in retired)
        self._subscribers = [s for s in self._subscribers if not s.closed]
        sub = SpectrumSubscription(self._max)
        self._subscribers.append(sub)
        return sub

    def publish(self, frame: SpectrumFrame) -> None:
        """Offer one frame to every live subscriber; never blocks."""

        self.published += 1
        for sub in self._subscribers:
            sub._offer(frame)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def total_dropped(self) -> int:
        """Aggregate dropped frames for NFR-076 health reporting."""

        return self._dropped_retired + sum(s.dropped for s in self._subscribers)
