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
