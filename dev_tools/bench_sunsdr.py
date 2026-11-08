#!/usr/bin/env python3
"""Measure the SunSDR2 DX hot path in seconds of CPU per second.

The W103D (Amlogic S905L3A, 4x Cortex-A53 @ 1.8 GHz) has no USB sound card
path for this radio: RX and TX are both software DSP.  This script measures
the three pieces that run continuously in ``backends/sunsdr``:

  * RX demodulator: ``rx_demod_block`` (3125 IQ samples, ~40 ms at 78k) per
    block, i.e. 25 blocks/s at the default rate;
  * 850-bin spectrum: 15 frames/s over the newest 2048 IQ samples;
  * TX modulator: 480-sample (10 ms) Hilbert hops, 100/s.

Run it on the box with the same interpreter the service uses:

    scp dev_tools/bench_sunsdr.py box:/tmp/
    ssh box '/opt/mrrc_modern/venv/bin/python /tmp/bench_sunsdr.py'

The printed total is a fraction of ONE core.  The repository's W103D
extrapolation (A53@1.8 ~= 0.57x Pi 4 single core; the existing server hot path
measures ~30% on W103D vs 1.53% on M2) puts the A53 factor at roughly 19x M2;
treat that as an estimate until the box itself runs this script.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from backends.sunsdr import dsp  # noqa: E402


def _tone_iq(rate: float, freq_after_shift: float, n: int) -> np.ndarray:
    """Complex tone that lands at ``freq_after_shift`` after the +IF shift."""
    k = np.arange(n, dtype=np.float64)
    return (0.2 * np.exp(
        2j * np.pi * (freq_after_shift - dsp.IF_OFFSET) * k / rate)
    ).astype(np.complex64)


def _pcm(n: int, rate: int, hz: float = 1000.0, amp: float = 0.5) -> bytes:
    t = np.arange(n) / float(rate)
    return np.rint(amp * 32767.0 * np.sin(2 * np.pi * hz * t)).astype(
        "<i2").tobytes()


def one_run(rate: int, seconds: float) -> tuple[float, dict[str, float]]:
    audio_rate = 48000
    tx_rate = rate / 2.0
    demod_block = 3125
    blocks = int(seconds * rate / demod_block)
    spec_frames = int(seconds * 15)
    tx_block = 480
    tx_blocks = int(seconds * audio_rate / tx_block)

    demod = dsp.AudioDemodulator(sample_rate=rate)
    demod.set_mode("USB")
    iq = _tone_iq(rate, 1500.0, demod_block)
    tx = dsp.TXModulator(input_rate=audio_rate, iq_rate=tx_rate)
    tx.reset()
    tx_pcm = _pcm(tx_block, audio_rate)
    spec_iq = _tone_iq(rate, 0.0, 2048)

    split: dict[str, float] = {}
    t0 = time.perf_counter()
    for _ in range(blocks):
        demod.demodulate(iq)
    split["rx_demod"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    for _ in range(spec_frames):
        dsp.iq_to_spectrum(spec_iq, rate, bins=850,
                           if_shift_hz=dsp.IF_OFFSET)
    split["spectrum"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    for _ in range(tx_blocks):
        tx.feed_audio(tx_pcm)
    split["tx_mod"] = time.perf_counter() - t0
    return sum(split.values()), split


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rate", type=int, default=78125,
                    choices=(39062, 78125, 156250, 312500))
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    one_run(args.rate, 0.5)              # warm imports / allocator
    best = min((one_run(args.rate, args.seconds)
                for _ in range(max(1, args.runs))), key=lambda r: r[0])
    wall, split = best
    print(f"SunSDR2 DX hot path @ {args.rate} S/s, "
          f"{args.seconds:.1f} s realtime")
    print(f"total: {wall / args.seconds * 100:.2f}% of one core")
    for name, seconds in sorted(split.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<10} {seconds / args.seconds * 100:6.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
