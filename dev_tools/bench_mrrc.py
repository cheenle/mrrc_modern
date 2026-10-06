#!/usr/bin/env python3
"""Measure the server's steady-state hot path, in seconds of CPU per second.

The design's performance section is an extrapolation: the hot path was
reproduced on a laptop, then scaled to the box by single-core ratio. This is
how that estimate gets replaced by a number from the box itself —

    scp dev_tools/bench_mrrc.py box:/tmp/ && ssh box \\
        '/opt/mrrc_modern/venv/bin/python /tmp/bench_mrrc.py'

One second of realtime work is: 50 RX frames (882 samples at 44.1 kHz,
resampled to 48 kHz, peak measured, Opus-encoded), 50 TX frames (decoded and
resampled back), 30 spectrum frames (4096-byte scope frame parsed, 850+850
values normalised and packed into the 1701-byte wire frame), and 10 state
broadcasts. Every call goes through the shipped module, so a change in them
moves this number.
"""
from __future__ import annotations

import json
import random
import struct
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from audio_resample import HW_RATE, OPUS_RATE, resample_pcm  # noqa: E402
from backends.ft710.scope_frame import (  # noqa: E402
    SCOPE_FRAME_SIZE,
    SYNC_TAIL,
    WF_SIZE,
    parse_scope_frame,
)

SECONDS = 6.0
RX_FRAMES = int(SECONDS * 50)
SPEC_FRAMES = int(SECONDS * 30)
STATE_FRAMES = int(SECONDS * 10)

_rnd = random.Random(20261006)
RAW = bytes(_rnd.randrange(256) for _ in range(SCOPE_FRAME_SIZE - 4)) + SYNC_TAIL
RX_PCM = np.random.default_rng(7).integers(-3000, 3000, 882, dtype=np.int16).tobytes()

STATE = {
    "type": "state",
    "vfo_a_freq": 14165000,
    "mode": "USB",
    "s_meter": 132,
    "s_unit": "S9",
    "s_meter_dbm": -13,
    "rf_power": 100,
    "bands": [
        {
            "name": n,
            "start": 14000000 + i * 1000000,
            "end": 14350000,
            "bsr": i,
            "default_freq": 14165000,
        }
        for i, n in enumerate(["160m", "80m", "60m", "40m", "30m", "20m"])
    ],
    "capabilities": {"scope_spans": [1, 2, 5, 10, 20, 50], "civ27": False},
}


def one_second() -> tuple[float, dict[str, float]]:
    """One second of realtime work. Returns (wall seconds, per-section split)."""
    from opus_rx import DEFAULT_BITRATE, RxOpusEncoder, TxOpusDecoder

    enc = RxOpusEncoder(bitrate=DEFAULT_BITRATE)
    dec = TxOpusDecoder()
    split: dict[str, float] = {}
    wall0 = time.perf_counter()

    t0 = time.perf_counter()
    pkts: list[bytes] = []
    for _ in range(RX_FRAMES):
        arr = np.frombuffer(RX_PCM, dtype=np.int16)
        int(np.abs(arr.astype(np.int32)).max())  # the silence watchdog
        pkts = enc.push(resample_pcm(RX_PCM, HW_RATE, OPUS_RATE))
    split["rx"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    for _ in range(RX_FRAMES):
        resample_pcm(dec.decode(pkts[0]), OPUS_RATE, HW_RATE)
    split["tx"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    for _ in range(SPEC_FRAMES):
        frame = parse_scope_frame(RAW)
        wf1 = bytes(min(255, max(0, v)) for v in frame.wf1[:WF_SIZE])
        wf2 = bytes(min(255, max(0, v)) for v in frame.wf2[:WF_SIZE])
        struct.pack("B", 1) + wf1 + wf2
    split["spectrum"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    for _ in range(STATE_FRAMES):
        json.dumps(STATE)
    split["state"] = time.perf_counter() - t0

    enc.close()
    dec.close()
    return time.perf_counter() - wall0, split


def main() -> int:
    import platform

    print(f"host   : {platform.machine()} / {platform.platform()}")
    print(f"python : {platform.python_version()} / numpy {np.__version__}")
    print(
        f"work   : {SECONDS:.0f} s of realtime per run "
        f"({RX_FRAMES} RX + {RX_FRAMES} TX + {SPEC_FRAMES} spectrum + {STATE_FRAMES} state)"
    )

    one_second()  # warm up imports, allocator and branch predictors
    wall, split = min((one_second() for _ in range(5)), key=lambda r: r[0])

    print(f"\nfastest run: {wall:.4f} s -> {wall / SECONDS * 100:.2f}% of one core\n")
    for name, seconds in sorted(split.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<9} {seconds / SECONDS * 100:6.2f}% of one core")
    print(f"  {'total':<9} {sum(split.values()) / SECONDS * 100:6.2f}% of one core")
    return 0


if __name__ == "__main__":
    sys.exit(main())
