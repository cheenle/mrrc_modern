from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest


# Export-map inspection (nm/dyld_info) and the version-script/unexports
# machinery are Unix-only; the Windows DLL ABI is exercised by the packaging
# smoke test instead (launcher loads wsjt_core.dll via the bundled server).
pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="native ABI export-map tests require Unix toolchain"
)


ROOT = Path(__file__).parents[3]
BUILD = ROOT / "ft8" / "dsp" / "build"
EXPECTED_CAPABILITIES = (
    "abi=1 results=256 rx=12000/180000 "
    "tx=48000/606720 profiles=0x1f"
)
EXPECTED_DYNAMIC_EXPORTS = {
    "wsjt_get_abi_info",
    "wsjt_ft8_encode",
    "wsjt_ft8_decode_standard",
    "wsjt_ft8_decode_improved",
}


@pytest.fixture(scope="module")
def built_library() -> Path:
    subprocess.run(
        [
            "cmake",
            "-S",
            str(ROOT / "ft8" / "dsp"),
            "-B",
            str(BUILD),
            "-DCMAKE_Fortran_COMPILER=gfortran-mp-13",
            "-DCMAKE_BUILD_TYPE=Release",
        ],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        ["cmake", "--build", str(BUILD), "-j"],
        cwd=ROOT,
        check=True,
    )

    suffix = ".dylib" if os.uname().sysname == "Darwin" else ".so"
    library = BUILD / f"libwsjt_core{suffix}"
    assert library.is_file(), f"configured build did not create {library}"
    return library


def _defined_dynamic_exports(library: Path) -> set[str]:
    system = os.uname().sysname
    if system == "Darwin":
        command = ["xcrun", "dyld_info", "-exports", str(library)]
    elif system == "Linux":
        command = ["nm", "-D", "--defined-only", str(library)]
    else:
        raise AssertionError(f"unsupported ABI smoke platform: {system}")

    result = subprocess.run(command, check=True, text=True, capture_output=True)
    if system == "Darwin":
        return {
            fields[1].removeprefix("_")
            for line in result.stdout.splitlines()
            if len(fields := line.split()) >= 2 and fields[0].startswith("0x")
        }
    return {
        line.split()[-1]
        for line in result.stdout.splitlines()
        if line.split()
    }


def test_library_exports_only_planned_entry_points(built_library: Path) -> None:
    assert _defined_dynamic_exports(built_library) == EXPECTED_DYNAMIC_EXPORTS


def test_export_control_change_relinks_library(built_library: Path) -> None:
    control_name = (
        "wsjt_core.unexports"
        if os.uname().sysname == "Darwin"
        else "wsjt_core.exports.map"
    )
    control_file = BUILD / control_name
    assert control_file.is_file(), f"configured build did not create {control_file}"

    library_mtime = built_library.stat().st_mtime_ns
    library_mtime_second = library_mtime // 1_000_000_000
    while time.time_ns() // 1_000_000_000 <= library_mtime_second:
        time.sleep(0.01)
    os.utime(control_file)
    assert control_file.stat().st_mtime_ns // 1_000_000_000 > library_mtime_second

    subprocess.run(
        ["cmake", "--build", str(BUILD), "-j"],
        cwd=ROOT,
        check=True,
    )

    assert built_library.stat().st_mtime_ns > library_mtime


def test_c_header_matches_fortran_abi(built_library: Path) -> None:
    executable = BUILD / "abi_smoke"
    subprocess.run(
        [
            "cc",
            str(ROOT / "tests" / "ft8" / "dsp" / "abi_smoke.c"),
            "-I",
            str(ROOT / "ft8" / "dsp"),
            "-L",
            str(BUILD),
            "-lwsjt_core",
            "-o",
            str(executable),
        ],
        check=True,
    )

    library_path_variable = (
        "DYLD_LIBRARY_PATH" if os.uname().sysname == "Darwin" else "LD_LIBRARY_PATH"
    )
    env = os.environ | {library_path_variable: str(BUILD)}
    result = subprocess.run(
        [str(executable)],
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, (
        f"abi_smoke exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert result.stdout.strip() == EXPECTED_CAPABILITIES, (
        f"unexpected abi_smoke stdout: {result.stdout!r}\n"
        f"stderr:\n{result.stderr}"
    )
