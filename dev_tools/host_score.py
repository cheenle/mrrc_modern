#!/usr/bin/env python3
"""Score a candidate host against the MRRC Modern server's hot path.

``dev_tools/bench_mrrc.py`` measures the real thing, but it needs the repo, a
built venv and libopus. This answers the question you ask *before* that one —
"is this box worth bothering with?" — and it needs nothing but a Python 3.9+
interpreter (verified against 3.9.6 with numpy absent; nothing here imports the
repository). On a box with nothing installed:

    scp dev_tools/host_score.py candidate:/tmp/
    ssh candidate python3 /tmp/host_score.py

## What it measures, and why those things

The workloads mirror the server's measured hot path rather than being a generic
benchmark. The dominant *Python-level* cost in the server is the spectrum
normalisation: every scope frame runs 850 + 850 integer compares through
`min()`/`max()` inside a generator, thirty times a second (design §2.5). That is
GIL-bound interpreter work, so it scales with single-thread performance and with
nothing else. The state broadcast (10 JSON dumps per second) is the second
measurable piece; the numpy resampler is measured when numpy is present.

## What it cannot measure, and how the total is still estimated

libopus runs in C behind ctypes, and it is the single largest slice of the
measured total (about three quarters of it). This script does not ship libopus,
so it scales that slice instead: it measures the machine's pure-Python score
against the reference machine's, and applies that ratio to the reference's
whole measured total. C code and interpreter code do not scale identically, so
the result is an estimate — which is why the reference and its measurement are
printed alongside it, and why `bench_mrrc.py` remains the thing to run once the
box is actually up.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time

# ── The workload, defined once so the reference and a candidate measure the
# same thing ────────────────────────────────────────────────────────────────
SPECTRUM_FRAMES = 30      # the server's spectrum broadcast rate
STATE_FRAMES = 10         # its CAT-driven state broadcast rate
WF_SIZE = 850             # wfview's waterfall width; two of them per frame
FRAME_SIZE = 4096         # the raw FT-710 scope frame

#: The reference machine: where the server's hot path was measured end to end
#: (`dev_tools/bench_mrrc.py` reported 1.53% of one core). `pure_python_seconds`
#: is this script's own workload on that machine, measured 2026-10-06.
REFERENCE = {
    "machine": "Apple M2, macOS 26.3, CPython 3.13.14, numpy 2.5.1",
    "pure_python_seconds": 0.003705,     # measured by this script on that host
    "hot_path_percent_of_one_core": 1.53,
}

#: The measured split on the reference (bench_mrrc.py), for the printed table.
REFERENCE_SPLIT = {"rx": 0.96, "spectrum": 0.37, "tx": 0.19, "state": 0.01}


def _wf_pair(seed: int = 7) -> list[bytes]:
    """The two waterfall rows a real frame carries, as the parser yields them."""
    rnd = random.Random(seed)
    return [[rnd.randrange(256) for _ in range(WF_SIZE)] for _ in range(2)]


def _raw_frame(seed: int = 11) -> bytes:
    """A 4096-byte scope frame, the input `parse_scope_frame` actually gets."""
    rnd = random.Random(seed)
    return bytes(rnd.randrange(256) for _ in range(FRAME_SIZE))


def _state_document() -> dict:
    """A fullState-shaped document: nested lists of dicts, not a flat dict.

    Size and shape matter for JSON cost, so this is the real shape (bands are
    objects with five keys, capabilities carries a list) rather than a toy.
    """
    return {
        "type": "state",
        "vfo_a_freq": 14165000,
        "mode": "USB",
        "s_meter": 132,
        "s_unit": "S9",
        "s_meter_dbm": -13,
        "rf_power": 100,
        "rf_gain": 120,
        "mic_gain": 30,
        "bands": [
            {
                "name": name,
                "start": 14000000 + i * 1000000,
                "end": 14350000,
                "bsr": i,
                "default_freq": 14165000,
            }
            for i, name in enumerate(
                ["160m", "80m", "60m", "40m", "30m", "20m", "17m", "15m", "12m", "10m", "6m"]
            )
        ],
        "capabilities": {"scope_spans": [1, 2, 5, 10, 20, 50, 100, 200], "civ27": False},
    }


def one_second_of_python() -> float:
    """Seconds to do the server's Python-level work for one second of realtime.

    Spectrum parse + normalisation and state JSON only — the costs that are
    pure interpreter work, and therefore the ones that scale with a
    candidate's single-thread performance.

    The parse step is the same two list comprehensions
    `backends/ft710/scope_frame.py` runs (`[~b & 0xFF for b in slice]`),
    inlined so this script needs nothing from the repository. It is measured
    rather than skipped because it is not free: on the reference host it is
    about a third of this function's time.
    """
    frame = _raw_frame()
    rows = _wf_pair()
    state = _state_document()
    started = time.perf_counter()
    for _ in range(SPECTRUM_FRAMES):
        wf1 = [~b & 0xFF for b in frame[0:WF_SIZE]]
        wf2 = [~b & 0xFF for b in frame[WF_SIZE:WF_SIZE * 2]]
        for row in (wf1, wf2):
            bytes(min(255, max(0, v)) for v in row)
    for _ in range(STATE_FRAMES):
        json.dumps(state)
    return time.perf_counter() - started


def one_second_of_numpy() -> float | None:
    """Seconds for the resampler work, or None when numpy is unavailable.

    The server resamples 50 RX and 50 TX frames per second (882 <-> 960
    samples). Measured separately because numpy is optional here: a candidate
    without it is not failed, it is reported as unscored on this row.
    """
    try:
        import numpy as np
    except ImportError:
        return None

    src = np.random.default_rng(7).integers(-3000, 3000, 882).astype(np.float32)
    t_in = np.arange(882, dtype=np.float32) / 44100.0
    t_out = np.arange(960, dtype=np.float32) / 48000.0
    started = time.perf_counter()
    for _ in range(100):
        interp = np.interp(t_out, t_in, src).astype(np.float32)
        np.clip(np.round(interp), -32768, 32767).astype(np.int16).tobytes()
    return time.perf_counter() - started


def score(runs: int = 5) -> dict:
    """Best-of-N timing for each workload. Returns seconds, not a rating."""
    one_second_of_python()  # warm up imports, allocator, branch predictors
    one_second_of_numpy()
    best_python = min(one_second_of_python() for _ in range(runs))
    numpy_times = [t for t in (one_second_of_numpy() for _ in range(runs)) if t is not None]
    return {
        "pure_python_seconds": best_python,
        "numpy_seconds": min(numpy_times) if numpy_times else None,
    }


def verdict(percent: float) -> str:
    """A band, so the number arrives with an opinion attached."""
    if percent < 15:
        return "comfortable — plenty of headroom"
    if percent < 40:
        return "fine for one client; watch it with several"
    if percent < 70:
        return "tight — expect jitter under load, and no room for anything else"
    return "too slow — audio will not keep up"


def main(argv: list[str] | None = None) -> int:
    import platform

    parser = argparse.ArgumentParser(prog="host_score.py", description=__doc__)
    parser.add_argument("--runs", type=int, default=5, help="best-of-N (default 5)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--reference-python-seconds",
        type=float,
        default=None,
        help="override the reference machine's pure-Python time (for re-deriving)",
    )
    args = parser.parse_args(argv)

    if args.reference_python_seconds:
        REFERENCE["pure_python_seconds"] = args.reference_python_seconds

    measured = score(args.runs)
    reference = REFERENCE["pure_python_seconds"]
    if not reference:
        print(
            "This copy has no reference measurement baked in. Run it once with\n"
            "--reference-python-seconds <seconds> from the reference machine.",
            file=sys.stderr,
        )
        return 2

    ratio = measured["pure_python_seconds"] / reference
    percent = REFERENCE["hot_path_percent_of_one_core"] * ratio
    out = {
        "host": f"{platform.machine()} / {platform.platform()}",
        "python": platform.python_version(),
        "pure_python_seconds": round(measured["pure_python_seconds"], 6),
        "numpy_seconds": round(measured["numpy_seconds"], 6) if measured["numpy_seconds"] else None,
        "reference": REFERENCE["machine"],
        "reference_pure_python_seconds": reference,
        "ratio_vs_reference": round(ratio, 3),
        "estimated_hot_path_percent": round(percent, 2),
        "verdict": verdict(percent),
    }

    if args.json:
        print(json.dumps(out, indent=2))
        return 0

    print(f"host    : {out['host']}")
    print(f"python  : {out['python']}"
          f"{'' if measured['numpy_seconds'] else '  (numpy absent — resampler not scored)'}")
    print()
    print(f"  one second of the server's Python work: {out['pure_python_seconds']:.6f} s")
    if measured["numpy_seconds"] is not None:
        print(f"  one second of the resampler work    : {measured['numpy_seconds']:.6f} s")
    print()
    print(f"  reference ({REFERENCE['machine']})")
    print(f"    same Python work there: {reference:.6f} s"
          f"  →  this host is {ratio:.2f}x slower")
    print(f"    its measured hot path : {REFERENCE['hot_path_percent_of_one_core']}% of one core"
          f"  ({', '.join(f'{k} {v}%' for k, v in REFERENCE_SPLIT.items())})")
    print()
    print(f"  ⇒ estimated hot path on this host: {percent:.1f}% of one core")
    print(f"    {verdict(percent)}")
    print()
    print("  Estimate, not a measurement: the C-code slices (libopus above all) are")
    print("  scaled by the Python ratio rather than measured. Once the box is up,")
    print("  dev_tools/bench_mrrc.py gives the real number.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
