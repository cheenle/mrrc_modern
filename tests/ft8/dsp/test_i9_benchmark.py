"""I9 M1 benchmark: measured decode latency per profile/thread (SDD 13.5).

Opt-in (``MRRC_FT8_I9_BENCHMARK=1``): fresh-builds the native library, spawns
the real Worker through :class:`WorkerSupervisor` and times the production
request path on one deterministic busy synthetic slot.  The run writes a JSON
artifact (``MRRC_FT8_I9_JSON`` or the pytest tmp dir) and prints a markdown
summary for transcription into SDD/13; the default profile-3 Auto-thread
configuration must meet the provisional TX decision cutoff.
"""

from __future__ import annotations

import ctypes as c
import json
import os
import platform
import statistics
import time
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from typing import Iterator

os.environ.setdefault("OMP_STACKSIZE", "10M")

import numpy as np
import pytest
from scipy.signal import resample_poly

from ft8.core.models import auto_thread_count
from ft8.core.supervisor import WorkerSupervisor

benchmark_only = pytest.mark.skipif(
    os.environ.get("MRRC_FT8_I9_BENCHMARK") != "1",
    reason="I9 benchmark is opt-in via MRRC_FT8_I9_BENCHMARK=1",
)

THREAD_MATRIX = [1, 2, 4, 6, 8, 12]
WARMUP_REPS = 1
MEASURED_REPS = 3
# Provisional V1.0 ceiling: decode dispatch at slot end must leave the rest of
# the 15 s slot for decision, PTT/audio lead (I10 placeholder) and margin.
PROVISIONAL_CUTOFF_SECONDS = 2.5
REQUEST_TIMEOUT_SECONDS = 120.0

_SIGNALS = [
    ("CQ K1ABC FN42", 400.0),
    ("CQ W9XYZ EM57", 900.0),
    ("CQ JA1AAA PM95", 1500.0),
    ("CQ DL1BBB JO62", 2100.0),
    ("CQ VK2CCC QF56", 2700.0),
]


def _config(profile: int, threads: int, cycles: int) -> dict[str, object]:
    return {
        "path": "improved",
        "sample_rate": 12_000,
        "sample_count": 180_000,
        "profile": profile,
        "threads": threads,
        "cycles": cycles,
        "sensitivity": 2,
        "ap": True,
        "low_threshold": False,
        "wide_dx": False,
        "hide_duplicates": True,
        "qso_progress": 0,
        "rx_frequency": 1500,
        "tx_frequency": 1500,
        "low_frequency": 200,
        "high_frequency": 3000,
        "ap_width": 50,
        "utc_hhmmss": 120000,
        "my_call": "N0CALL",
        "dx_call": "",
        "dx_grid": "",
    }


@pytest.fixture(scope="session")
def busy_slot(raw_library: c.CDLL) -> np.ndarray:
    """Return a deterministic noisy 12 kHz int16 slot with five known CQs."""

    slot = np.zeros(180_000, dtype=np.float32)
    for message, frequency in _SIGNALS:
        wave48 = np.zeros(606_720, dtype=np.float32)
        written = c.c_int32()
        status = raw_library.wsjt_ft8_encode(
            message.encode("ascii"),
            frequency,
            48_000,
            wave48.ctypes.data_as(c.POINTER(c.c_float)),
            wave48.size,
            c.byref(written),
            c.create_string_buffer(38),
        )
        assert status == 0 and written.value == wave48.size
        wave12 = resample_poly(wave48, 1, 4).astype(np.float32)
        slot[6_000 : 6_000 + wave12.size] += wave12

    rng = np.random.default_rng(20_260_801)
    slot *= 4_800.0  # five signals share the former single-signal headroom
    slot += rng.normal(0.0, 2_000.0, slot.size).astype(np.float32)
    return np.clip(np.rint(slot), -32768, 32767).astype(np.int16)


@pytest.fixture(scope="session")
def benchmark_worker(
    raw_library_path: Path,
) -> Iterator[WorkerSupervisor]:
    supervisor = WorkerSupervisor(library_path=raw_library_path, stop_timeout=5.0)
    supervisor.start()
    try:
        yield supervisor
    finally:
        supervisor.stop()


def test_auto_thread_policy_clamps_to_supported_bounds() -> None:
    assert auto_thread_count(None) == min(max((os.cpu_count() or 2) - 1, 1), 12)
    assert auto_thread_count(1) == 1
    assert auto_thread_count(2) == 1
    assert auto_thread_count(8) == 7
    assert auto_thread_count(16) == 12
    assert auto_thread_count(64) == 12


@benchmark_only
def test_i9_latency_matrix(
    benchmark_worker: WorkerSupervisor,
    busy_slot: np.ndarray,
    tmp_path: Path,
) -> None:
    shm = SharedMemory(create=True, size=360_000)
    runs: list[dict[str, object]] = []
    try:
        pcm = np.ndarray((180_000,), dtype="<i2", buffer=shm.buf)
        pcm[:] = busy_slot
        descriptor = {
            "name": shm.name,
            "dtype": "<i2",
            "shape": [180_000],
            "nbytes": 360_000,
        }
        auto_threads = auto_thread_count()
        matrix = [
            (profile, threads, 1)
            for profile in range(5)
            for threads in THREAD_MATRIX
        ]
        # Default headline configuration and a worst-case cycles spot check.
        matrix.append((3, auto_threads, 1))
        matrix.append((3, auto_threads, 3))

        for profile, threads, cycles in matrix:
            samples_wall: list[float] = []
            samples_native: list[float] = []
            decoded_texts: set[str] = set()
            for rep in range(WARMUP_REPS + MEASURED_REPS):
                request = {
                    "type": "decode",
                    "slot_id": rep + 1,
                    "deadline_monotonic": time.monotonic() + 3_600.0,
                    "shm": descriptor,
                    "config": _config(profile, threads, cycles),
                }
                started = time.monotonic()
                response = benchmark_worker.request(
                    request, REQUEST_TIMEOUT_SECONDS
                )
                wall = time.monotonic() - started
                assert response["type"] == "decode_ok", response
                if rep >= WARMUP_REPS:
                    samples_wall.append(wall)
                    samples_native.append(float(response["elapsed_seconds"]))
                    decoded_texts.update(
                        result["text"]
                        for result in response["results"]  # type: ignore[index]
                    )
            runs.append(
                {
                    "profile": profile,
                    "threads": threads,
                    "cycles": cycles,
                    "wall_seconds": samples_wall,
                    "native_seconds": samples_native,
                    "known_decodes": sorted(
                        text for text in (s[0] for s in _SIGNALS) if text in decoded_texts
                    ),
                }
            )
    finally:
        shm.close()
        shm.unlink()

    headline = next(
        run
        for run in runs
        if run["profile"] == 3 and run["threads"] == auto_threads and run["cycles"] == 1
    )
    artifact = {
        "host": {
            "processor": platform.processor() or platform.machine(),
            "machine": platform.machine(),
            "platform": platform.platform(),
            "logical_cpus": os.cpu_count(),
            "python": platform.python_version(),
        },
        "policy": {
            "auto_threads": auto_threads,
            "provisional_cutoff_seconds": PROVISIONAL_CUTOFF_SECONDS,
        },
        "runs": runs,
    }
    output = Path(os.environ.get("MRRC_FT8_I9_JSON", tmp_path / "i9_results.json"))
    output.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")

    print(f"\nI9 benchmark artifact: {output}")
    print("| profile | threads | cycles | wall min/mean/max s | native mean s | known |")
    print("|---|---|---|---|---|---|")
    for run in runs:
        wall = run["wall_seconds"]  # type: ignore[assignment]
        print(
            f"| {run['profile']} | {run['threads']} | {run['cycles']} "
            f"| {min(wall):.3f}/{statistics.fmean(wall):.3f}/{max(wall):.3f} "
            f"| {statistics.fmean(run['native_seconds']):.3f} "  # type: ignore[arg-type]
            f"| {len(run['known_decodes'])}/{len(_SIGNALS)} |"
        )

    for run in runs:
        known = run["known_decodes"]
        assert len(known) >= 3, f"too few known decodes for {run}"
    headline_max = max(headline["wall_seconds"])  # type: ignore[arg-type]
    assert headline_max <= PROVISIONAL_CUTOFF_SECONDS, (
        f"profile 3 / {auto_threads} threads worst wall {headline_max:.3f} s "
        f"exceeds provisional cutoff {PROVISIONAL_CUTOFF_SECONDS} s"
    )
