"""Bounded offline search for a public-ABI-observable A8 gate fixture."""

from __future__ import annotations

import ctypes as c
import itertools
import os
from pathlib import Path
import sys

os.environ.setdefault("OMP_STACKSIZE", "10M")

import numpy as np
from scipy.signal import resample_poly

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "tests" / "ft8" / "dsp"))

from test_ft8_improved_decode import improved_function  # noqa: E402
from test_ft8_standard_decode import Result, config  # noqa: E402


def encode(library: c.CDLL, message: bytes, frequency: float) -> np.ndarray:
    wave48 = np.zeros(606_720, dtype=np.float32)
    written = c.c_int32()
    sent = c.create_string_buffer(38)
    status = library.wsjt_ft8_encode(
        message,
        c.c_float(frequency),
        c.c_int32(48_000),
        wave48.ctypes.data_as(c.POINTER(c.c_float)),
        c.c_int32(wave48.size),
        c.byref(written),
        sent,
    )
    assert status == 0
    return resample_poly(wave48, 1, 4).astype(np.float32)


def decode(library: c.CDLL, samples: np.ndarray) -> list[tuple[str, int, float]]:
    cfg = config()
    cfg.profile = 3
    cfg.threads = 2
    cfg.cycles = 1
    cfg.flags = 1
    cfg.rx_frequency = 1500
    cfg.dx_call = b"K1ABC"
    cfg.dx_grid = b"FN42"
    results = (Result * 256)()
    count = c.c_int32()
    overflow = c.c_int32()
    status = improved_function(library)(
        samples.ctypes.data_as(c.POINTER(c.c_int16)),
        c.byref(cfg),
        1,
        results,
        256,
        c.byref(count),
        c.byref(overflow),
    )
    assert status == 0 and overflow.value == 0
    return [
        (
            results[index].text.decode().strip(),
            results[index].ap_type,
            float(results[index].frequency),
        )
        for index in range(count.value)
    ]


def make_slot(candidate: np.ndarray, ordinary: np.ndarray, ordinary_gain: float, candidate_gain: float) -> np.ndarray:
    slot = np.zeros(180_000, dtype=np.float32)
    slot[6_000 : 6_000 + candidate.size] += candidate_gain * candidate
    slot[6_000 : 6_000 + ordinary.size] += ordinary_gain * ordinary
    return np.clip(np.rint(slot * 18_000.0), -32768, 32767).astype(np.int16)


def search_direct_threshold(library: c.CDLL, candidate: np.ndarray) -> int:
    if not hasattr(library, "wsjt_test_ft8_a8d"):
        return -1
    hook = library.wsjt_test_ft8_a8d
    hook.argtypes = [c.POINTER(c.c_int16), c.c_int32, c.POINTER(c.c_char)]
    hook.restype = c.c_int32
    noise = np.random.default_rng(20260801).normal(0.0, 0.2, 180_000).astype(np.float32)
    for gain in (0.001, 0.002, 0.004, 0.008, 0.015, 0.03, 0.06, 0.12):
        slot = noise.copy()
        slot[6_000 : 6_000 + candidate.size] = gain * candidate
        samples = np.clip(np.rint(slot * 24_000.0), -32768, 32767).astype(np.int16)
        message = c.create_string_buffer(38)
        assert hook(samples.ctypes.data_as(c.POINTER(c.c_int16)), 1500, message) == 0
        cfg = config()
        cfg.flags = 0
        results = (Result * 256)()
        count = c.c_int32()
        overflow = c.c_int32()
        status = improved_function(library)(
            samples.ctypes.data_as(c.POINTER(c.c_int16)),
            c.byref(cfg),
            1,
            results,
            256,
            c.byref(count),
            c.byref(overflow),
        )
        assert status == 0 and overflow.value == 0
        ordinary_messages = {
            results[index].text.decode().strip() for index in range(count.value)
        }
        direct_message = message.value.decode().strip()
        print("DIRECT", gain, repr(direct_message), sorted(ordinary_messages))
        if direct_message == "CQ K1ABC FN42" and "CQ K1ABC FN42" not in ordinary_messages:
            return 0
    return 1


def main() -> int:
    library = c.CDLL(sys.argv[1])
    library.wsjt_ft8_encode.argtypes = [
        c.c_char_p,
        c.c_float,
        c.c_int32,
        c.POINTER(c.c_float),
        c.c_int32,
        c.POINTER(c.c_int32),
        c.POINTER(c.c_char),
    ]
    library.wsjt_ft8_encode.restype = c.c_int32
    candidate = encode(library, b"CQ K1ABC FN42", 1500.0)
    direct_result = search_direct_threshold(library, candidate)
    if direct_result >= 0:
        return direct_result
    near_ordinary = encode(library, b"CQ W9XYZ EN50", 1502.0)
    far_ordinary = encode(library, b"CQ W9XYZ EN50", 1504.0)
    gains = itertools.product((0.04, 0.07, 0.10, 0.15, 0.22), (0.45, 0.70, 1.0, 1.3))
    for candidate_gain, ordinary_gain in gains:
        near = decode(
            library,
            make_slot(candidate, near_ordinary, ordinary_gain, candidate_gain),
        )
        far = decode(
            library,
            make_slot(candidate, far_ordinary, ordinary_gain, candidate_gain),
        )
        near_a8 = [item for item in near if item[1] == 8]
        far_a8 = [item for item in far if item[1] == 8]
        if not near_a8 and far_a8:
            print(candidate_gain, ordinary_gain, repr(near), repr(far))
            return 0
    print("NO_STABLE_PUBLIC_FIXTURE")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
