"""Contract tests for the native DSP CMake toolchain settings."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[3]
TOOLCHAIN = ROOT / "ft8" / "dsp" / "toolchain.cmake"


def test_toolchain_preserves_flags_and_is_idempotent(tmp_path: Path) -> None:
    """MRRC flags must append once without replacing caller-provided flags."""
    probe = tmp_path / "toolchain_probe.cmake"
    probe.write_text(
        f'''set(CMAKE_Fortran_FLAGS_DEBUG "-sentinel-debug")
set(CMAKE_Fortran_FLAGS_RELEASE "-sentinel-release")
include("{TOOLCHAIN.as_posix()}")
include("{TOOLCHAIN.as_posix()}")
message(STATUS "DEBUG_FLAGS=${{CMAKE_Fortran_FLAGS_DEBUG}}")
message(STATUS "RELEASE_FLAGS=${{CMAKE_Fortran_FLAGS_RELEASE}}")
'''
    )

    completed = subprocess.run(
        ["cmake", "-P", str(probe)],
        check=True,
        capture_output=True,
        text=True,
    )
    output = completed.stdout + completed.stderr
    output_lines = output.splitlines()

    assert (
        "-- DEBUG_FLAGS=-sentinel-debug -O0 -g -fcheck=all -fbacktrace"
        in output_lines
    )
    assert "-- RELEASE_FLAGS=-sentinel-release -O3" in output_lines
