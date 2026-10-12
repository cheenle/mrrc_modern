from __future__ import annotations

import ctypes as c
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest
from scipy.signal import resample_poly

from test_ft8_improved_decode import _rounded_key, improved_function
from test_ft8_standard_decode import Result, config


ROOT = Path(__file__).parents[3]
SUFFIX = ".dll" if os.name == "nt" else (".dylib" if sys.platform == "darwin" else ".so")
FORTRAN_COMPILER = "gfortran-mp-13" if sys.platform == "darwin" else "gfortran"


@pytest.fixture
def a8_hook_library(tmp_path_factory: pytest.TempPathFactory) -> c.CDLL:
    if os.name == "nt":
        pytest.skip("native raw-ctypes decode requires >1 MB stack on Windows")
    build = tmp_path_factory.mktemp("ft8-a8-hook-build")
    subprocess.run(
        [
            "cmake",
            "-S",
            str(ROOT / "ft8" / "dsp"),
            "-B",
            str(build),
            f"-DCMAKE_Fortran_COMPILER={FORTRAN_COMPILER}",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DMRRC_FT8_TEST_HOOKS=ON",
        ],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(["cmake", "--build", str(build), "-j"], cwd=ROOT, check=True)
    library = c.CDLL(str(build / f"libwsjt_core{SUFFIX}"))
    assert hasattr(library, "wsjt_test_ft8_a8d")
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
    return library


def _decode_keys(
    raw_library: c.CDLL,
    slot: np.ndarray,
    *,
    profile: int,
    threads: int,
    cycles: int,
) -> set[tuple[str, int, int]]:
    cfg = config()
    cfg.profile = profile
    cfg.threads = threads
    cfg.cycles = cycles
    results = (Result * 256)()
    count = c.c_int32(-1)
    overflow = c.c_int32(-1)
    status = improved_function(raw_library)(
        slot.ctypes.data_as(c.POINTER(c.c_int16)),
        c.byref(cfg),
        11_908_800,
        results,
        256,
        c.byref(count),
        c.byref(overflow),
    )
    assert status == 0
    assert overflow.value == 0
    return {_rounded_key(results[index]) for index in range(count.value)}


def _decode_results(
    raw_library: c.CDLL,
    slot: np.ndarray,
    cfg: object,
) -> list[Result]:
    results = (Result * 256)()
    count = c.c_int32(-1)
    overflow = c.c_int32(-1)
    status = improved_function(raw_library)(
        slot.ctypes.data_as(c.POINTER(c.c_int16)),
        c.byref(cfg),
        11_908_800,
        results,
        256,
        c.byref(count),
        c.byref(overflow),
    )
    assert status == 0
    assert overflow.value == 0
    return list(results[: count.value])


def _encoded_wave(raw_library: c.CDLL, message: bytes, frequency: float) -> np.ndarray:
    wave48 = np.zeros(606_720, dtype=np.float32)
    written = c.c_int32()
    sent = c.create_string_buffer(38)
    status = raw_library.wsjt_ft8_encode(
        message,
        frequency,
        48_000,
        wave48.ctypes.data_as(c.POINTER(c.c_float)),
        wave48.size,
        c.byref(written),
        sent,
    )
    assert status == 0
    assert written.value == wave48.size
    return resample_poly(wave48, 1, 4).astype(np.float32)


@pytest.fixture(scope="module")
def boundary_slot(raw_library: c.CDLL) -> np.ndarray:
    """Two nearby signals on opposite sides of the 2-way 1600 Hz boundary."""
    left = _encoded_wave(raw_library, b"CQ K1ABC FN42", 1575.0)
    right = _encoded_wave(raw_library, b"CQ W9XYZ EN50", 1625.0)
    slot = np.zeros(180_000, dtype=np.float32)
    slot[6_000 : 6_000 + left.size] += left
    slot[6_000 : 6_000 + right.size] += 0.42 * right
    return np.clip(np.rint(slot * 22_000.0), -32768, 32767).astype(np.int16)


def test_improved_fails_closed_when_runtime_cannot_form_requested_team(
    raw_library: c.CDLL,
) -> None:
    environment = os.environ.copy()
    environment.update(
        OMP_DYNAMIC="FALSE",
        OMP_STACKSIZE="10M",
        OMP_THREAD_LIMIT="2",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests" / "ft8" / "dsp" / "team_limit_probe.py"),
            str(raw_library._name),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_partition_helper_covers_every_legal_band_exactly_once(tmp_path: Path) -> None:
    executable = tmp_path / ("partition-probe.exe" if os.name == "nt" else "partition-probe")
    subprocess.run(
        [
            FORTRAN_COMPILER,
            str(ROOT / "ft8" / "dsp" / "wsjt_partition.f90"),
            str(ROOT / "tests" / "ft8" / "dsp" / "partition_probe.f90"),
            "-o",
            str(executable),
        ],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run([str(executable)], cwd=tmp_path, check=True)


def test_a8_gate_uses_strict_three_hz_threshold_and_atomic_clear(tmp_path: Path) -> None:
    executable = tmp_path / ("a8-gate-probe.exe" if os.name == "nt" else "a8-gate-probe")
    subprocess.run(
        [
            FORTRAN_COMPILER,
            "-fopenmp",
            str(ROOT / "ft8" / "dsp" / "wsjt_a8_gate.f90"),
            str(ROOT / "tests" / "ft8" / "dsp" / "a8_gate_probe.f90"),
            "-o",
            str(executable),
        ],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run([str(executable)], cwd=tmp_path, check=True)


@pytest.mark.xfail(
    strict=True,
    reason="fixed weak direct-A8 fixture is not fresh-process reproducible",
)
def test_direct_a8_recovers_fixed_weak_expected_message_three_times(
    a8_hook_library: c.CDLL,
) -> None:
    hook = a8_hook_library.wsjt_test_ft8_a8d
    hook.argtypes = [
        c.POINTER(c.c_int16),
        c.c_int32,
        c.POINTER(c.c_char),
    ]
    hook.restype = c.c_int32
    wave = _encoded_wave(a8_hook_library, b"CQ K1ABC FN42", 1500.0)
    slot = np.random.default_rng(20260801).normal(
        0.0, 0.2, 180_000
    ).astype(np.float32)
    slot[6_000 : 6_000 + wave.size] += 0.002 * wave
    samples = np.clip(np.rint(slot * 24_000.0), -32768, 32767).astype(np.int16)
    decoded = []
    for _ in range(3):
        message = c.create_string_buffer(38)
        status = hook(
            samples.ctypes.data_as(c.POINTER(c.c_int16)),
            1500,
            message,
        )
        assert status == 0
        decoded.append(message.value.decode().strip())
    assert decoded == ["CQ K1ABC FN42"] * 3


def test_direct_a8_recovers_clean_expected_message_three_times(
    a8_hook_library: c.CDLL,
) -> None:
    hook = a8_hook_library.wsjt_test_ft8_a8d
    hook.argtypes = [
        c.POINTER(c.c_int16),
        c.c_int32,
        c.POINTER(c.c_char),
    ]
    hook.restype = c.c_int32
    wave = _encoded_wave(a8_hook_library, b"CQ K1ABC FN42", 1500.0)
    slot = np.zeros(180_000, dtype=np.float32)
    slot[6_000 : 6_000 + wave.size] = 0.08 * wave
    samples = np.clip(np.rint(slot * 24_000.0), -32768, 32767).astype(np.int16)

    for _ in range(3):
        message = c.create_string_buffer(38)
        assert hook(
            samples.ctypes.data_as(c.POINTER(c.c_int16)),
            1500,
            message,
        ) == 0
        assert message.value.decode().strip() == "CQ K1ABC FN42"


def test_ap_context_grid_hash_and_flag_changes_do_not_leak_between_requests(
    raw_library: c.CDLL,
    cq_slot: np.ndarray,
) -> None:
    empty = np.zeros_like(cq_slot)

    def run(dx_call: bytes, dx_grid: bytes, flags: int, samples: np.ndarray) -> set[tuple[str, int, int]]:
        cfg = config()
        cfg.profile = 3
        cfg.threads = 2
        cfg.cycles = 1
        cfg.flags = flags
        cfg.dx_call = dx_call
        cfg.dx_grid = dx_grid
        return {_rounded_key(item) for item in _decode_results(raw_library, samples, cfg)}

    first = run(b"K1ABC", b"FN42", 1, cq_slot)
    repeated = run(b"K1ABC", b"FN42", 1, cq_slot)
    switched_grid = run(b"K1ABC", b"EM10", 1, cq_slot)
    nonstandard = run(b"PJ4/K1ABC", b"", 1, cq_slot)
    ap_off = run(b"K1ABC", b"FN42", 0, cq_slot)

    assert repeated == first
    for decoded in (first, switched_grid, nonstandard, ap_off):
        assert "CQ K1ABC FN42" in {key[0] for key in decoded}
    assert run(b"K1ABC", b"FN42", 1, empty) == set()
    assert run(b"PJ4/K1ABC", b"", 1, empty) == set()
    assert run(b"K1ABC", b"FN42", 0, empty) == set()


def test_near_rx_ordinary_decode_has_no_observable_a8_result(
    raw_library: c.CDLL,
    cq_slot: np.ndarray,
) -> None:
    cfg = config()
    cfg.profile = 3
    cfg.threads = 2
    cfg.flags = 1
    cfg.dx_call = b"K1ABC"
    cfg.dx_grid = b"FN42"

    decoded = _decode_results(raw_library, cq_slot, cfg)

    assert "CQ K1ABC FN42" in {item.text.decode().strip() for item in decoded}
    assert all(item.ap_type != 8 for item in decoded)


@pytest.mark.parametrize(
    ("profile", "cycles"),
    [(0, 1), (1, 2), (2, 3), (3, 1), (4, 3)],
)
def test_band_local_multisignal_decode_matches_single_thread_normalized_set(
    raw_library: c.CDLL,
    boundary_slot: np.ndarray,
    profile: int,
    cycles: int,
) -> None:
    single = _decode_keys(
        raw_library,
        boundary_slot,
        profile=profile,
        threads=1,
        cycles=cycles,
    )
    parallel = _decode_keys(
        raw_library,
        boundary_slot,
        profile=profile,
        threads=2,
        cycles=cycles,
    )
    expected_messages = {"CQ K1ABC FN42", "CQ W9XYZ EN50"}
    assert expected_messages <= {key[0] for key in single}
    assert parallel == single


def test_repeated_parallel_regions_keep_decoder_workspaces_isolated(
    raw_library: c.CDLL,
    cq_slot: np.ndarray,
) -> None:
    expected = _decode_keys(
        raw_library,
        cq_slot,
        profile=4,
        threads=1,
        cycles=3,
    )
    assert "CQ K1ABC FN42" in {key[0] for key in expected}

    for _ in range(4):
        assert _decode_keys(
            raw_library,
            cq_slot,
            profile=4,
            threads=4,
            cycles=3,
        ) == expected
