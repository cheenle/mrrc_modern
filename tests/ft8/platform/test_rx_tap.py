"""RX tap: device-rate blocks in, 12 kHz ring/spectrum out."""
from __future__ import annotations

import numpy as np

from ft8.engine.audio_rx import UtcRing
from ft8.platform.rx_tap import RxTap


def _tone(rate: int, seconds: float, hz: float) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (0.5 * np.sin(2 * np.pi * hz * t) * 32767).astype(np.int16)


def test_output_rate_is_12k_and_continuous_across_blocks() -> None:
    ring = UtcRing(seconds=10)
    tap = RxTap(ring, dev_rate=44100)
    blocks = [b for b in np.array_split(_tone(44100, 2.0, 1000.0), 97) if b.size]
    epoch = 1000.0
    for b in blocks:
        tap.feed(b.tobytes(), epoch)
        epoch += b.size / 44100.0
    # 2 s in => ~24000 samples at 12 kHz; soxr's fixed overlap holds back
    # ~25 ms (286 samples measured at HQ) and the tap never fabricates samples.
    assert abs((ring.high_water - ring.base) - 24000) < 600
    assert ring.gap_count == 0


def test_block_split_matches_whole_buffer() -> None:
    """Feeding in chunks must equal feeding the same signal at once."""
    whole = RxTap(None, dev_rate=44100)
    signal = _tone(44100, 1.0, 700.0)
    y_whole = whole.feed(signal.tobytes(), 0.0)
    split = RxTap(None, dev_rate=44100)
    parts = [split.feed(b.tobytes(), 0.0) for b in np.array_split(signal, 61)]
    y_split = np.concatenate(parts)
    assert abs(y_whole.size - y_split.size) <= 1
    n = min(y_whole.size, y_split.size)
    assert np.max(np.abs(y_whole[:n].astype(np.int32) - y_split[:n].astype(np.int32))) <= 2


def test_spectrum_hook_receives_the_same_12k_blocks() -> None:
    seen: list[tuple[int, float]] = []
    ring = UtcRing(seconds=10)
    tap = RxTap(ring, dev_rate=44100, on_block=lambda s, e: seen.append((s.size, e)))
    tap.feed(_tone(44100, 0.2, 500.0).tobytes(), 500.0)
    assert seen and all(size > 0 for size, _ in seen)


def test_epoch_chain_is_driven_by_emitted_samples() -> None:
    """Consecutive output blocks must advance by exactly size / 12 kHz.

    soxr buffers a fixed overlap, so the first blocks may emit nothing at
    all; the epoch chain must start at the first emitted block and never
    run ahead of the audio actually delivered.
    """
    seen: list[tuple[int, float]] = []
    tap = RxTap(None, dev_rate=44100, on_block=lambda s, e: seen.append((s.size, e)))
    for i in range(40):                        # 0.8 s — enough to clear the buffering
        tap.feed(_tone(44100, 0.02, 400.0).tobytes(), 1000.0 + i * 0.02)
    assert seen, "no output emitted"
    assert seen[0][1] == 1000.0                # first block anchors at its input epoch
    for (size, epoch), (_, next_epoch) in zip(seen, seen[1:]):
        assert abs((next_epoch - epoch) - size / 12000) < 1e-9


def test_epoch_timeline_has_no_rounding_gaps():
    """Ten seconds of 20 ms blocks must produce ZERO ring gaps.

    Regression (field finding 2026-10-13): the float accumulator
    ``base + n/rate`` made ``round(epoch*rate)`` land ±1 sample away from the
    ring's high-water on most blocks; UtcRing recorded every one of those as a
    gap, ``read_slot`` then returned None, and two slots out of three were
    silently skipped — the decoder looked dead while the waterfall kept
    scrolling.
    """
    ring = UtcRing(seconds=20)
    tap = RxTap(ring, dev_rate=48000)
    block = _tone(48000, 0.02, 700.0).tobytes()
    epoch = 1000.0
    for i in range(500):                      # 10 s of 20 ms blocks
        tap.feed(block, epoch + i * 0.02)
    assert ring.gap_count == 0
    assert (ring.high_water - (ring.base or 0)) >= 119_000
