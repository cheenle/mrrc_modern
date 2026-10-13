"""FT8 receive tap on the platform's existing RX audio stream.

Takes the device-rate int16 blocks that ``server.py::_audio_rx_loop`` already
reads, resamples them to the decoder's 12 kHz domain in a streaming fashion
(block-boundary continuous), and delivers them to the UTC ring and the
optional spectrum hook.  No new device stream is opened.

UTC anchoring: the caller passes the epoch of each input block's first
sample; output blocks carry ``base_epoch + consumed_samples / dev_rate``, so
the epoch chain follows the input sample count (the hardware clock is the
timebase; the ring's resync absorbs scheduling jitter).
"""
from __future__ import annotations

import logging
import time

import numpy as np

from ft8.engine.audio_rx import DECODER_SAMPLE_RATE, UtcRing

log = logging.getLogger("mrrc.ft8.rx_tap")

SOXR_QUALITY = "HQ"


class RxTap:
    """Device-rate int16 in → 12 kHz int16 out (ring + optional hook)."""

    def __init__(self, ring: UtcRing | None, *, dev_rate: int = 44_100,
                 out_rate: int = DECODER_SAMPLE_RATE,
                 on_block=None) -> None:
        if dev_rate <= 0:
            raise ValueError("dev_rate must be positive")
        import soxr

        self._ring = ring
        self._dev_rate = dev_rate
        self._out_rate = out_rate
        self._on_block = on_block
        self._stream = soxr.ResampleStream(
            dev_rate, out_rate, 1, dtype="float32", quality=SOXR_QUALITY
        )
        # The output timeline is driven by an INTEGER sample index, not a
        # float epoch accumulator: round(epoch * rate) on a ~1.79e9 epoch
        # lands ±1 sample away from the ring's high-water mark every block,
        # and UtcRing records each of those as a gap — which makes read_slot
        # return None and skips two slots out of three (field finding).
        self._next_index: int | None = None
        self._logged_first = False
        self._rms_blocks = 0
        self._feed_count = 0
        self._rate_mark = time.monotonic()
        self._peak_seen = 0
        self._out_count = 0           # 12 kHz samples emitted so far
        self._consumed = 0            # input samples fed so far

    @property
    def consumed_input_samples(self) -> int:
        return self._consumed

    def feed(self, pcm: bytes | np.ndarray, block_epoch: float) -> np.ndarray:
        """Resample one device-rate block; returns the 12 kHz int16 samples."""
        x = np.frombuffer(pcm, dtype="<i2") if isinstance(pcm, (bytes, bytearray)) else np.asarray(pcm)
        if x.dtype != np.float32:
            x = x.astype(np.float32) / 32768.0
        if x.size == 0:
            return np.zeros(0, dtype="<i2")

        self._consumed += x.size
        if self._next_index is None:
            # The stream's timebase is its FIRST input block: soxr compensates
            # its own filter delay, so emitted sample n sits at
            # base + n / out_rate.  Anchored once as an integer.
            self._next_index = round(block_epoch * self._out_rate)
        y = self._stream.resample_chunk(x, last=False)
        block = np.clip(y * 32767.0, -32768.0, 32767.0).astype("<i2")
        if not block.size:
            # soxr buffers a fixed overlap before its first output; nothing is
            # delivered and nothing may be timestamped.
            return block
        out_epoch = self._next_index / self._out_rate
        self._next_index += block.size
        self._out_count += block.size
        if not self._logged_first:
            self._logged_first = True
            log.info("RX tap delivering (dev %d Hz -> %d Hz, first block %d samples @ %.3f)",
                     self._dev_rate, self._out_rate, block.size, out_epoch)
        self._rms_blocks += 1
        self._feed_count += 1
        if self._feed_count % 300 == 0:          # rate probe: blocks/second
            now = time.monotonic()
            rate = 300 / max(now - self._rate_mark, 0.001)
            ring = self._ring
            extra = ""
            if ring is not None:
                extra = (f" | ring base={ring.base} high={ring.high_water} "
                         f"gaps={ring.gap_count} dropped={ring.metrics.dropped_samples}")
            # Clock-drift probe: is the tap's own timeline anchored wrong
            # (constant offset) or advancing slower than wall clock?
            drift = time.time() - self._next_index / self._out_rate
            extra += f" | drift={drift:+.2f}s"
            log.info("RX tap: %d blocks fed (%.1f blocks/s), %d in / %d out samples%s",
                     self._feed_count, rate, self._consumed, self._out_count, extra)
            self._rate_mark = now
        if self._rms_blocks % 250 == 0:          # every ~5 s of audio
            rms = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))
            log.info("RX tap level: rms=%.0f peak=%d (12 kHz, last block)",
                     rms, int(np.max(np.abs(block))))
        if block.size > self._peak_seen:
            self._peak_seen = block.size
        if self._ring is not None:
            self._ring.write(block, out_epoch)
        if self._on_block is not None:
            self._on_block(block, out_epoch)
        return block

    def flush(self) -> np.ndarray:
        """Drain the resampler's buffered tail (call when stopping the tap)."""

        y = self._stream.resample_chunk(np.zeros(0, dtype=np.float32), last=True)
        block = np.clip(y * 32767.0, -32768.0, 32767.0).astype("<i2")
        if block.size:
            if self._next_index is None:
                self._next_index = 0
            out_epoch = self._next_index / self._out_rate
            self._next_index += block.size
            self._out_count += block.size
            if self._ring is not None:
                self._ring.write(block, out_epoch)
            if self._on_block is not None:
                self._on_block(block, out_epoch)
        return block
