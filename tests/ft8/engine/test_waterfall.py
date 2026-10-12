"""Regressions for spectrum frames and the lossy fan-out (NFR-004, SC8)."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from ft8.engine.audio_rx import DECODER_SAMPLE_RATE
from ft8.engine.waterfall import (
    SpectrumComputer,
    SpectrumFanout,
    SpectrumFrame,
)


def tone(freq_hz: float, seconds: float, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(round(seconds * DECODER_SAMPLE_RATE)) / DECODER_SAMPLE_RATE
    return np.round(amplitude * 32_767 * np.sin(2 * np.pi * freq_hz * t)).astype(np.int16)


def make_computer(fft_size: int = 2_048, lines_per_second: float = 5.0) -> SpectrumComputer:
    return SpectrumComputer(fft_size=fft_size, lines_per_second=lines_per_second)


def test_tone_peaks_at_the_expected_bin() -> None:
    computer = make_computer()
    frames = computer.push(tone(1_500.0, 1.0, amplitude=0.01), first_epoch=0.0)
    assert frames
    bins = np.frombuffer(frames[0].bins, dtype=np.uint8)
    peak = int(bins.argmax())
    assert abs(peak * frames[0].bin_hz - 1_500.0) <= frames[0].bin_hz
    assert bins[peak] > 100  # clear tone well above the floor


def test_spectrum_spans_only_the_ft8_passband() -> None:
    """The emitted bins cover ~3 kHz (the FT8 passband), not the full 6 kHz
    of the 12 kHz stream — the display would otherwise be half blank."""

    computer = make_computer()
    frames = computer.push(tone(1_500.0, 1.0), first_epoch=0.0)
    frame = frames[0]
    span_hz = len(frame.bins) * frame.bin_hz
    assert abs(span_hz - 3_000.0) <= frame.bin_hz  # within one bin of 3 kHz
    assert span_hz < 6_000.0  # not the full 0..rate/2


def test_silence_quantizes_to_zero() -> None:
    computer = make_computer()
    frames = computer.push(np.zeros(4_096, dtype=np.int16), first_epoch=0.0)
    assert frames
    assert max(frames[0].bins) == 0


def test_cadence_and_epochs_are_block_size_independent() -> None:
    pcm = tone(900.0, 2.0)
    one_shot = make_computer().push(pcm, first_epoch=100.0)

    chunked = make_computer()
    frames: list[SpectrumFrame] = []
    offset = 0
    for block in (1_000, 7, 4_096, 513):
        part = pcm[offset : offset + block]
        frames += chunked.push(part, first_epoch=100.0 + offset / DECODER_SAMPLE_RATE)
        offset += block
    frames += chunked.push(
        pcm[offset:], first_epoch=100.0 + offset / DECODER_SAMPLE_RATE
    )

    assert [(f.seq, f.epoch, f.bins) for f in frames] == [
        (f.seq, f.epoch, f.bins) for f in one_shot
    ]
    epochs = [f.epoch for f in one_shot]
    assert np.allclose(np.diff(epochs), 0.2, atol=1 / DECODER_SAMPLE_RATE)
    assert [f.seq for f in one_shot] == list(range(len(one_shot)))


def test_utc_gap_resets_the_line_instead_of_bridging() -> None:
    computer = make_computer()
    computer.push(np.zeros(1_024, dtype=np.int16), first_epoch=0.0)
    frames = computer.push(np.zeros(8_192, dtype=np.int16), first_epoch=5.0)
    assert frames  # only post-gap samples form lines
    assert all(f.epoch >= 5.0 for f in frames)


def test_binary_frame_round_trip_and_size() -> None:
    frame = SpectrumFrame(seq=7, epoch=1_700_000_000.25, bin_hz=5.859375, bins=bytes(300))
    wire = frame.to_bytes()
    assert len(wire) == 22 + 300  # compact fixed header plus one byte per bin
    again = SpectrumFrame.from_bytes(wire)
    assert again.seq == 7
    assert abs(again.epoch - frame.epoch) < 1e-3
    assert again.bin_hz == frame.bin_hz
    assert again.bins == frame.bins


@pytest.mark.parametrize(
    "payload",
    [b"", b"WF02" + bytes(30), SpectrumFrame(1, 0.0, 1.0, bytes(10)).to_bytes()[:-1]],
)
def test_corrupt_frames_are_rejected(payload: bytes) -> None:
    with pytest.raises(ValueError):
        SpectrumFrame.from_bytes(payload)


def test_invalid_computer_inputs_fail_fast() -> None:
    with pytest.raises(ValueError):
        SpectrumComputer(fft_size=1_000)  # not a power of two
    with pytest.raises(ValueError):
        SpectrumComputer(db_floor=-10.0, db_ceiling=-90.0)
    computer = make_computer()
    with pytest.raises(ValueError):
        computer.push(np.zeros(100, dtype=np.float32), first_epoch=0.0)


def frame(seq: int) -> SpectrumFrame:
    return SpectrumFrame(seq=seq, epoch=0.0, bin_hz=1.0, bins=bytes([seq % 256]))


def test_slow_subscriber_drops_oldest_and_keeps_newest() -> None:
    fanout = SpectrumFanout(max_frames_per_subscriber=2)
    slow = fanout.subscribe()
    fast = fanout.subscribe()
    for seq in range(5):
        fanout.publish(frame(seq))
        fast.queue.get_nowait()  # the fast client drains every frame

    assert fanout.published == 5
    kept = [slow.queue.get_nowait().seq for _ in range(slow.queue.qsize())]
    assert kept == [3, 4]
    assert slow.dropped == 3
    assert fanout.total_dropped == 3


def test_closed_subscriber_stops_receiving_and_detaches() -> None:
    fanout = SpectrumFanout()
    sub = fanout.subscribe()
    fanout.publish(frame(1))
    sub.close()
    sub.close()  # idempotent
    fanout.publish(frame(2))
    assert sub.queue.qsize() == 1
    fanout.subscribe()  # prunes the closed subscription
    assert fanout.subscriber_count == 1
    assert fanout.total_dropped == 0


def test_subscription_queue_never_blocks_publishing() -> None:
    async def main() -> None:
        fanout = SpectrumFanout(max_frames_per_subscriber=1)
        fanout.subscribe()
        await asyncio.wait_for(
            asyncio.to_thread(
                lambda: [fanout.publish(frame(s)) for s in range(1_000)]
            ),
            timeout=1.0,
        )
        assert fanout.total_dropped == 999

    asyncio.run(main())
