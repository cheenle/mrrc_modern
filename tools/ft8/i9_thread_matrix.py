#!/usr/bin/env python3
"""Field twin of the I9 benchmark: the threads x CPU-time matrix on a box with no test tree.

`tests/ft8/dsp/test_i9_benchmark.py` is the harness of record, but it needs pytest and the
`tests/` tree — and `packaging/common/stage-tree.sh` excludes `tests/`, `tools/` and
`dev_tools/`, so a production box has none of them.  Yet "what did the extra cores actually
buy?" can only be answered **on that box**: the answer is a property of the A53, not of the
developer's laptop.

So this script depends on numpy and the installed `ft8.core.*` only, drives the same
`WorkerSupervisor` the server drives, and prints the same table.  It is deliberately **not** a
second implementation of the *measurement*: the slot construction mirrors
`tests/ft8/dsp/conftest.py::busy_slot` (same five CQs, same frequencies, same amplitudes, same
noise seed), the request mirrors `test_i9_benchmark.py::_config`, and the only reason the
resampler is hand-written instead of `scipy.signal.resample_poly` is that scipy is not a
runtime dependency of this product and the one thing needed here is a 4:1 decimation of a
synthetic waveform.

It reads the **Worker process's own CPU time** (`decode_ok.cpu_seconds`), which is the whole
point: wall time cannot show fake parallelism.  The deep path's OpenMP region recomputes the
entire symbol-spectrum per thread, so more threads means more total CPU while the wall clock
stops falling.  `docs/ft8-w103d-decode-perf-analysis.md` 第 0 梯队 (section 4) and section 6
ask for exactly this column.

Usage (on the box, with the app root's own venv — no ``cd`` needed, see ``enter_app_root``):

    sudo -u mrrc env OMP_STACKSIZE=10M \\
        /opt/mrrc_modern/venv/bin/python /tmp/i9_thread_matrix.py --app-root /opt/mrrc_modern

This runs tens of decodes back to back and will peg every core of the box for the duration
(roughly 1-3 minutes on a W103D).  Do not run it while the operator is transmitting.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

# Iron law (AGENTS.md, ft8): this must precede the numpy import and the native library load.
# The Fortran sync stage allocates large automatic arrays on the thread stack, and a shortage
# there manifests as a segfault rather than an exception.
os.environ.setdefault("OMP_STACKSIZE", "10M")

import numpy as np  # noqa: E402

# Same table as tests/ft8/dsp/conftest.py::_SIGNALS, in the same order.
SIGNALS = [
    ("CQ K1ABC FN42", 400.0),
    ("CQ W9XYZ EM57", 900.0),
    ("CQ JA1AAA PM95", 1500.0),
    ("CQ DL1BBB JO62", 2100.0),
    ("CQ VK2CCC QF56", 2700.0),
]
NOISE_SEED = 20_260_801
SLOT_SAMPLES = 180_000
WAVE48_SAMPLES = 606_720
SLOT_OFFSET = 6_000
CPU_DEADLINE = 3_600.0


def decimate4(wave48: np.ndarray) -> np.ndarray:
    """48 kHz -> 12 kHz the way `resample_poly(wave, 1, 4)` does: low-pass, then take every 4th.

    A 65-tap Hamming-windowed sinc with its cutoff at a quarter of the input Nyquist.  The
    result is only ever fed to the decoder as a synthetic busy band, so what matters is that it
    is the same construction on every machine and that the five CQs survive it.
    """

    taps = 65
    half = taps // 2
    offsets = np.arange(-half, half + 1, dtype=np.float64)
    kernel = np.sinc(offsets / 4.0) * np.hamming(taps)
    kernel /= kernel.sum()
    filtered = np.convolve(wave48.astype(np.float64), kernel, mode="same")
    return filtered[::4].astype(np.float32)


def build_busy_slot(binding: object) -> np.ndarray:
    """Five known CQs plus noise, as int16 — mirroring the pytest fixture byte for byte."""

    slot = np.zeros(SLOT_SAMPLES, dtype=np.float32)
    for message, frequency in SIGNALS:
        wave48 = np.zeros(WAVE48_SAMPLES, dtype=np.float32)
        binding.encode(message, frequency, 48_000, wave48)
        wave12 = decimate4(wave48)
        slot[SLOT_OFFSET : SLOT_OFFSET + wave12.size] += wave12

    rng = np.random.default_rng(NOISE_SEED)
    slot *= 4_800.0  # five signals share the former single-signal headroom
    slot += rng.normal(0.0, 2_000.0, slot.size).astype(np.float32)
    return np.clip(np.rint(slot), -32768, 32767).astype(np.int16)


def decode_config(profile: int, threads: int, cycles: int) -> dict[str, object]:
    """Mirror of tests/ft8/dsp/test_i9_benchmark.py::_config."""

    return {
        "path": "improved",
        "sample_rate": 12_000,
        "sample_count": SLOT_SAMPLES,
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


def host_facts() -> dict[str, object]:
    """Whatever the box will tell us about itself without needing extra tooling."""

    facts: dict[str, object] = {"logical_cpus": os.cpu_count()}
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.lower().startswith("model name"):
                    facts["cpu_model"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    for key, path in (
        ("governor", "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
        ("cur_freq_khz", "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"),
    ):
        try:
            with open(path, encoding="utf-8") as handle:
                facts[key] = handle.read().strip()
        except OSError:
            pass
    for index in range(8):
        path = f"/sys/class/thermal/thermal_zone{index}/temp"
        try:
            with open(path, encoding="utf-8") as handle:
                facts[f"thermal_zone{index}_millic"] = handle.read().strip()
        except OSError:
            break
    return facts


def run_matrix(
    supervisor: object,
    descriptor: dict[str, object],
    profiles: list[int],
    threads_matrix: list[int],
    cycles: int,
    warmup: int,
    reps: int,
) -> list[dict[str, object]]:
    runs: list[dict[str, object]] = []
    for profile in profiles:
        for threads in threads_matrix:
            walls: list[float] = []
            natives: list[float] = []
            cpus: list[float] = []
            decoded: set[str] = set()
            for rep in range(warmup + reps):
                request = {
                    "type": "decode",
                    "slot_id": rep + 1,
                    "deadline_monotonic": time.monotonic() + CPU_DEADLINE,
                    "shm": descriptor,
                    "config": decode_config(profile, threads, cycles),
                }
                started = time.monotonic()
                response = supervisor.request(request, 300.0)  # type: ignore[attr-defined]
                wall = time.monotonic() - started
                if response["type"] != "decode_ok":
                    raise SystemExit(f"worker replied {response['type']}: {response}")
                if rep >= warmup:
                    walls.append(wall)
                    natives.append(float(response["elapsed_seconds"]))
                    cpus.append(float(response["cpu_seconds"]))
                    decoded.update(result["text"] for result in response["results"])
            known = [text for text, _ in SIGNALS if text in decoded]
            runs.append(
                {
                    "profile": profile,
                    "threads": threads,
                    "cycles": cycles,
                    "wall_seconds": walls,
                    "native_seconds": natives,
                    "cpu_seconds": cpus,
                    "known_decodes": known,
                    "all_decodes": len(decoded),
                }
            )
            print(
                f"  p{profile} t{threads:<2} wall {statistics.fmean(walls):.3f}s "
                f"cpu {statistics.fmean(cpus):.3f}s  known {len(known)}/{len(SIGNALS)}",
                flush=True,
            )
    return runs


def enter_app_root(path: str) -> str:
    """Move the process to where production runs it, and make ``ft8`` importable from there.

    The working directory is not cosmetic.  The vendored decoder resolves its callsign
    database as ``trim(share_dir)//'/ALLCALL7.TXT'`` with ``share_dir = '.'``
    (``ft8/dsp/vendor/prog_args.f90:4``) — relative to the **process cwd** — and the service
    unit pins ``WorkingDirectory=$MRRC_HOME`` (``packaging/common/mrrc-service-layer.sh:200``),
    so production always finds it.  Measured 2026-10-14: starting elsewhere costs nothing on
    the clock (0.193 s / 0.452 s either way, profile 3, threads 3), but the decoder then runs
    without that database and says so in one line the pytest harness never prints.  The twin's
    claim is "the deployed decoder", so it starts where the deployed decoder starts.

    A relative ``--app-root`` is resolved *before* the chdir, so ``.`` means where the operator
    typed it rather than where a previous call left the process.
    """
    root = os.path.abspath(path)
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-root", default="/opt/mrrc_modern",
                        help="directory containing the installed ft8/ package")
    parser.add_argument("--profiles", default="3", help="comma-separated decode profiles")
    parser.add_argument("--threads", default="1,2,3,4,6,8,12",
                        help="comma-separated thread counts")
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--json", default="", help="write the raw artifact here")
    args = parser.parse_args(argv)

    app_root = enter_app_root(args.app_root)

    from ft8.core.binding import CoreBinding
    from ft8.core.supervisor import WorkerSupervisor
    from ft8.core.worker import default_library_path

    library = default_library_path()
    print(f"app root : {app_root}")
    print(f"library  : {library}")
    if not os.path.isfile(library):
        print("!! the native library is not there — is FT8 built on this box?")
        return 1
    facts = host_facts()
    for key, value in facts.items():
        print(f"{key:<22}: {value}")

    binding = CoreBinding(library)
    print("\nbuilding the busy slot (five known CQs)...", flush=True)
    slot = build_busy_slot(binding)

    from multiprocessing.shared_memory import SharedMemory

    profiles = [int(item) for item in args.profiles.split(",") if item.strip()]
    threads_matrix = [int(item) for item in args.threads.split(",") if item.strip()]

    shm = SharedMemory(create=True, size=SLOT_SAMPLES * 2)
    supervisor = WorkerSupervisor(library_path=library, stop_timeout=5.0)
    try:
        pcm = np.ndarray((SLOT_SAMPLES,), dtype="<i2", buffer=shm.buf)
        pcm[:] = slot
        descriptor = {
            "name": shm.name,
            "dtype": "<i2",
            "shape": [SLOT_SAMPLES],
            "nbytes": SLOT_SAMPLES * 2,
        }
        print(f"\nrunning {len(profiles) * len(threads_matrix)} cells "
              f"({args.warmup} warmup + {args.reps} measured each)...", flush=True)
        supervisor.start()
        runs = run_matrix(
            supervisor, descriptor, profiles, threads_matrix,
            args.cycles, args.warmup, args.reps,
        )
    finally:
        supervisor.stop()
        shm.close()
        shm.unlink()

    print("\n| profile | threads | cycles | wall min/mean/max s | native mean s "
          "| cpu mean s | cpu/native | known |")
    print("|---|---|---|---|---|---|---|---|")
    for run in runs:
        wall = run["wall_seconds"]
        native = statistics.fmean(run["native_seconds"])
        cpu = statistics.fmean(run["cpu_seconds"])
        efficiency = cpu / native if native else float("nan")
        print(
            f"| {run['profile']} | {run['threads']} | {run['cycles']} "
            f"| {min(wall):.3f}/{statistics.fmean(wall):.3f}/{max(wall):.3f} "
            f"| {native:.3f} | {cpu:.3f} | {efficiency:.2f}x "
            f"| {len(run['known_decodes'])}/{len(SIGNALS)} |"
        )
    print(
        "\ncpu/native is the parallelism actually achieved.  A column that keeps climbing "
        "while\nwall stops falling is the sync stage being recomputed per thread "
        "(analysis section 8.2)."
    )

    if args.json:
        artifact = {
            "host": facts,
            "library": str(library),
            "policy": {"cycles": args.cycles, "warmup": args.warmup, "reps": args.reps},
            "runs": runs,
        }
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(artifact, handle, indent=2)
            handle.write("\n")
        print(f"artifact: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
