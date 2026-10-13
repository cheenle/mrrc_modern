from __future__ import annotations

import ctypes as c
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("OMP_STACKSIZE", "10M")

import numpy as np
import pytest
from scipy.signal import resample_poly


ROOT = Path(__file__).parents[3]
SUFFIX = ".dll" if os.name == "nt" else (".dylib" if sys.platform == "darwin" else ".so")
FORTRAN_COMPILER = "gfortran-mp-13" if sys.platform == "darwin" else "gfortran"

# Shared busy-slot signal table (moved here from test_i9_benchmark.py so both the
# I9 benchmark and the fast-path decode tests build the same fixture).
_SIGNALS = [
    ("CQ K1ABC FN42", 400.0),
    ("CQ W9XYZ EM57", 900.0),
    ("CQ JA1AAA PM95", 1500.0),
    ("CQ DL1BBB JO62", 2100.0),
    ("CQ VK2CCC QF56", 2700.0),
]


@pytest.fixture(autouse=True)
def _windows_dsp_dll_search_path() -> None:
    """On Windows, let ctypes find the MinGW runtime DLLs next to gfortran.

    Python 3.8+ secure DLL loading ignores PATH, so fresh test builds of
    libwsjt_core.dll cannot resolve libgfortran-5/libfftw3f_threads/etc.
    without an explicit add_dll_directory.
    """
    if os.name == "nt":
        import shutil

        compiler = shutil.which("gfortran")
        if compiler:
            os.add_dll_directory(str(Path(compiler).resolve().parent))
    yield


ENCODE_ARGTYPES = [
    c.c_char_p,
    c.c_float,
    c.c_int32,
    c.POINTER(c.c_float),
    c.c_int32,
    c.POINTER(c.c_int32),
    c.POINTER(c.c_char),
]


@pytest.fixture(scope="session")
def raw_library_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Configure and build a fresh native library for DSP tests.

    Skipped on Windows: the Fortran decode allocates hundreds of KB of
    automatic arrays on the caller stack, which overflows the 1 MB Windows
    main-thread default (macOS/Linux give 8 MB+).  The packaged server runs
    decode on a large-stack thread (server/core/binding.py ``_call_native``);
    that path is validated by the binding E2E and by the macOS suite.
    """
    if os.name == "nt":
        pytest.skip("native raw-ctypes decode requires >1 MB stack on Windows")
    build = tmp_path_factory.mktemp("ft8-dsp-build")
    build_type = os.environ.get("MRRC_FT8_DSP_BUILD_TYPE", "Release")
    assert build_type in {"Debug", "Release"}
    subprocess.run(
        [
            "cmake",
            "-S",
            str(ROOT / "ft8" / "dsp"),
            "-B",
            str(build),
            f"-DCMAKE_Fortran_COMPILER={FORTRAN_COMPILER}",
            f"-DCMAKE_BUILD_TYPE={build_type}",
        ],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        ["cmake", "--build", str(build), "-j"],
        cwd=ROOT,
        check=True,
    )

    library_path = build / f"libwsjt_core{SUFFIX}"
    assert library_path.is_file(), f"fresh build did not create {library_path}"
    return library_path


@pytest.fixture(scope="session")
def raw_library(raw_library_path: Path) -> c.CDLL:
    """Load the fresh native library with the encode signature declared."""
    library = c.CDLL(str(raw_library_path))
    library.wsjt_ft8_encode.argtypes = ENCODE_ARGTYPES
    library.wsjt_ft8_encode.restype = c.c_int32
    return library


@pytest.fixture(scope="session")
def cq_slot(raw_library: c.CDLL) -> np.ndarray:
    """Return a deterministic 12 kHz int16 slot containing a known FT8 CQ."""
    wave48 = np.zeros(606_720, dtype=np.float32)
    written = c.c_int32()
    sent = c.create_string_buffer(38)
    status = raw_library.wsjt_ft8_encode(
        b"CQ K1ABC FN42",
        1500.0,
        48_000,
        wave48.ctypes.data_as(c.POINTER(c.c_float)),
        wave48.size,
        c.byref(written),
        sent,
    )
    assert status == 0
    assert written.value == wave48.size
    assert sent.value.decode().strip() == "CQ K1ABC FN42"

    wave12 = resample_poly(wave48, 1, 4).astype(np.float32)
    slot = np.zeros(180_000, dtype=np.float32)
    slot[6_000 : 6_000 + wave12.size] = wave12
    return np.clip(np.rint(slot * 24_000.0), -32768, 32767).astype(np.int16)


@pytest.fixture(scope="session")
def signals() -> list[tuple[str, float]]:
    """The known CQ messages/frequencies the busy slot is built from."""
    return _SIGNALS


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


class _Config(c.Structure):
    _fields_ = [
        ("struct_size", c.c_int32), ("sample_rate", c.c_int32),
        ("sample_count", c.c_int32), ("profile", c.c_int32),
        ("threads", c.c_int32), ("cycles", c.c_int32),
        ("sensitivity", c.c_int32), ("flags", c.c_int32),
        ("qso_progress", c.c_int32), ("rx_frequency", c.c_int32),
        ("tx_frequency", c.c_int32), ("low_frequency", c.c_int32),
        ("high_frequency", c.c_int32), ("ap_width", c.c_int32),
        ("utc_hhmmss", c.c_int32), ("reserved", c.c_int32),
        ("my_call", c.c_char * 13), ("dx_call", c.c_char * 13),
        ("dx_grid", c.c_char * 7), ("padding", c.c_char * 3),
    ]


class _Result(c.Structure):
    _fields_ = [
        ("slot_id", c.c_int64), ("sync", c.c_float),
        ("dt", c.c_float), ("frequency", c.c_float),
        ("quality", c.c_float), ("snr", c.c_int32),
        ("ap_type", c.c_int32), ("flags", c.c_int32),
        ("reserved", c.c_int32), ("text", c.c_char * 38),
        ("padding", c.c_char * 2),
    ]


@pytest.fixture
def call_fast():
    """Invoke ``wsjt_ft8_decode_fast`` with the shared 12 kHz / 180 000-sample config.

    Shared by the fast-path unit tests (``test_ft8_fast_decode.py``) and the
    reference-parity test (``test_ft8_fast_parity.py``) so there is a single
    call convention: 200-3000 Hz window, capacity >= WSJT_RESULT_CAPACITY.
    """

    def _call(library, samples: np.ndarray, capacity: int = 256):
        cfg = _Config()
        cfg.struct_size = c.sizeof(_Config)
        cfg.sample_rate = 12_000
        cfg.sample_count = 180_000
        cfg.low_frequency = 200
        cfg.high_frequency = 3_000
        results = (_Result * capacity)()
        count = c.c_int32()
        overflow = c.c_int32()
        status = library.wsjt_ft8_decode_fast(
            samples.ctypes.data_as(c.POINTER(c.c_int16)),
            c.byref(cfg), 1, results, capacity,
            c.byref(count), c.byref(overflow),
        )
        return status, results, count.value, overflow.value

    return _call
