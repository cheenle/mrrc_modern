from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[3]


def test_ft8lib_cmake_included_and_sourced() -> None:
    root = (ROOT / "ft8" / "dsp" / "CMakeLists.txt").read_text(encoding="utf-8")
    assert "cmake/ft8lib.cmake" in root
    assert "${FT8LIB_SOURCES}" in root.split("add_library(wsjt_core")[1].split(")")[0]

    module = (ROOT / "ft8" / "dsp" / "cmake" / "ft8lib.cmake").read_text(encoding="utf-8")
    assert "set(FT8LIB_SOURCES" in module
    for needed in ("ft8/decode.c", "common/monitor.c", "fft/kiss_fftr.c"):
        assert needed in module


def test_ft8lib_include_dir_present() -> None:
    root = (ROOT / "ft8" / "dsp" / "CMakeLists.txt").read_text(encoding="utf-8")
    includes = root.split("target_include_directories(wsjt_core PRIVATE", 1)[1].split(")", 1)[0]
    assert "${FT8LIB_LIB}" in includes

    module = (ROOT / "ft8" / "dsp" / "cmake" / "ft8lib.cmake").read_text(encoding="utf-8")
    assert 'set(FT8LIB_LIB "${CMAKE_CURRENT_SOURCE_DIR}/vendor-ft8lib")' in module
