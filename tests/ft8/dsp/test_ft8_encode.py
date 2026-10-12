from __future__ import annotations

import ctypes as c
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest


ROOT = Path(__file__).parents[3]
SUFFIX = ".dll" if os.name == "nt" else (".dylib" if sys.platform == "darwin" else ".so")
FORTRAN_COMPILER = "gfortran-mp-13" if sys.platform == "darwin" else "gfortran"
ENCODE_ARGTYPES = [
    c.c_char_p,
    c.c_float,
    c.c_int32,
    c.POINTER(c.c_float),
    c.c_int32,
    c.POINTER(c.c_int32),
    c.POINTER(c.c_char),
]


@pytest.fixture(scope="module")
def encode_library(tmp_path_factory: pytest.TempPathFactory) -> c.CDLL:
    build = tmp_path_factory.mktemp("ft8-encode-build")
    subprocess.run(
        [
            "cmake",
            "-S",
            str(ROOT / "ft8" / "dsp"),
            "-B",
            str(build),
            f"-DCMAKE_Fortran_COMPILER={FORTRAN_COMPILER}",
            "-DCMAKE_BUILD_TYPE=Release",
        ],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        ["cmake", "--build", str(build), "-j"],
        cwd=ROOT,
        check=True,
    )

    library = build / f"libwsjt_core{SUFFIX}"
    assert library.is_file(), f"fresh build did not create {library}"
    loaded = c.CDLL(str(library))
    loaded.wsjt_ft8_encode.argtypes = ENCODE_ARGTYPES
    loaded.wsjt_ft8_encode.restype = c.c_int32
    return loaded


def _outputs() -> tuple[np.ndarray, c.c_int32, c.Array[c.c_char]]:
    sent = c.create_string_buffer(b"\x7f" * 38, 38)
    return np.zeros(606_720, dtype=np.float32), c.c_int32(-1), sent


def test_encode_produces_exact_48k_ft8_waveform(encode_library: c.CDLL) -> None:
    wave, written, sent = _outputs()

    status = encode_library.wsjt_ft8_encode(
        b"CQ K1ABC FN42",
        1500.0,
        48_000,
        wave.ctypes.data_as(c.POINTER(c.c_float)),
        wave.size,
        c.byref(written),
        sent,
    )

    assert status == 0
    assert written.value == 606_720
    assert sent.value.decode().strip() == "CQ K1ABC FN42"
    assert np.isfinite(wave).all()
    assert 0.90 <= float(np.max(np.abs(wave))) <= 1.0
    assert np.count_nonzero(wave) > 500_000


@pytest.mark.parametrize(
    ("sample_rate", "capacity", "expected"),
    [(44_100, 606_720, 3), (48_000, 606_719, 6)],
)
def test_encode_rejects_wrong_rate_and_capacity(
    encode_library: c.CDLL,
    sample_rate: int,
    capacity: int,
    expected: int,
) -> None:
    wave, written, sent = _outputs()

    status = encode_library.wsjt_ft8_encode(
        b"CQ K1ABC FN42",
        1500.0,
        sample_rate,
        wave.ctypes.data_as(c.POINTER(c.c_float)),
        capacity,
        c.byref(written),
        sent,
    )

    assert status == expected
    assert written.value == 0
    assert sent.raw == b"\0" * 38


@pytest.mark.parametrize("missing", ["message", "wave", "written", "sent"])
def test_encode_rejects_null_pointers_without_crashing(
    encode_library: c.CDLL,
    missing: str,
) -> None:
    wave, written, sent = _outputs()
    arguments: list[object] = [
        b"CQ K1ABC FN42",
        1500.0,
        48_000,
        wave.ctypes.data_as(c.POINTER(c.c_float)),
        wave.size,
        c.byref(written),
        sent,
    ]
    arguments[{"message": 0, "wave": 3, "written": 5, "sent": 6}[missing]] = None

    assert encode_library.wsjt_ft8_encode(*arguments) == 1
    if missing != "written":
        assert written.value == 0
    if missing != "sent":
        assert sent.raw == b"\0" * 38


def test_encode_rejects_bad_message(encode_library: c.CDLL) -> None:
    wave, written, sent = _outputs()

    status = encode_library.wsjt_ft8_encode(
        b"CQ <K1ABC>",
        1500.0,
        48_000,
        wave.ctypes.data_as(c.POINTER(c.c_float)),
        wave.size,
        c.byref(written),
        sent,
    )

    assert status == 7
    assert written.value == 0
    assert sent.raw == b"\0" * 38
