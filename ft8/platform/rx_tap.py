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
        self._base_epoch: float | None = None
        self._logged_first = False
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
        if self._base_epoch is None:
            # The stream's timebase is its FIRST input block: soxr compensates
            # its own filter delay, so emitted sample n sits at
            # base_epoch + n / out_rate.
            self._base_epoch = block_epoch
        y = self._stream.resample_chunk(x, last=False)
        block = np.clip(y * 32767.0, -32768.0, 32767.0).astype("<i2")
        if not block.size:
            # soxr buffers a fixed overlap before its first output; nothing is
            # delivered and nothing may be timestamped.
            return block
        if not self._logged_first:
            self._logged_first = True
            log.info("RX tap delivering (dev %d Hz -> %d Hz, first block %d samples @ %.3f)",
                     self._dev_rate, self._out_rate, block.size, self._base_epoch)
        out_epoch = self._base_epoch + self._out_count / self._out_rate
        self._out_count += block.size
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
            if self._base_epoch is None:
                self._base_epoch = 0.0
            out_epoch = self._base_epoch + self._out_count / self._out_rate
            self._out_count += block.size
            if self._ring is not None:
                self._ring.write(block, out_epoch)
            if self._on_block is not None:
                self._on_block(block, out_epoch)
        return block
