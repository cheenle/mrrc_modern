"""
FTDI library discovery for FT-710 scope support.

The project is intended to run standalone. Prefer libraries copied into this
repository, while still allowing explicit environment overrides.

Supports macOS (.dylib), Linux (.so), and Windows (.dll).
"""
from pathlib import Path
from typing import Optional, List, Tuple
import os
import sys


# Repo root (this file lives in backends/ft710/): bundled resources such as
# lib/ and vendor/ftdi/ are located relative to the project root, both in
# source mode and inside PyInstaller bundles (_MEIPASS).
SCRIPT_DIR = Path(__file__).resolve().parents[2]
_DLL_DIRECTORY_HANDLES: list[object] = []


def get_resource_roots() -> List[Path]:
    """Return directories that may contain packaged runtime resources."""
    roots: List[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(SCRIPT_DIR)

    unique: List[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def _platform_suffixes() -> List[str]:
    """Return the shared-library extension and alternative suffixes for the
    current platform, ordered by preference."""
    if sys.platform == "darwin":
        return [".dylib"]
    elif sys.platform == "win32":
        return [".dll"]
    else:  # linux and other Unix
        return [".so", ".so.1", ".so.0"]


def _candidate_names() -> List[Tuple[str, str]]:
    """Return (ft4222_name, ftd2xx_name) pairs to try, in priority order.

    Each pair can use a different suffix (the discovery loop will substitute
    platform-appropriate extensions).  The first pair uses the base names;
    additional pairs provide versioned fallbacks."""
    pairs: List[Tuple[str, str]] = [
        # Original libraries (known to work best with FT4222 SPI)
        ("libft4222", "libftd2xx"),
    ]
    if sys.platform == "darwin":
        pairs.append(("libft4222", "libftd2xx.1.4.35"))
    elif sys.platform == "win32":
        pairs.append(("FT4222", "ftd2xx"))
        pairs.append(("libft4222", "ftd2xx64"))
    else:
        # Linux: try versioned and unversioned
        pairs.append(("libft4222", "libftd2xx.1"))
        pairs.append(("libft4222", "libftd2xx.0"))
    return pairs


def _libs_in_dir(directory: Path) -> Tuple[Optional[Path], Optional[Path]]:
    """Search a single directory for matching FT4222 + FTD2XX library files.

    Tries all suffixes appropriate for the current platform for each
    candidate name pair, returning the first matching pair found."""
    suffixes = _platform_suffixes()

    for ft4222_base, ftd2xx_base in _candidate_names():
        # Try the base name as-is first (handles versioned names like
        # libftd2xx.1.4.35.dylib where the suffix is part of the base)
        ft4222_candidate = directory / (ft4222_base + suffixes[0])
        ftd2xx_candidate = directory / (ftd2xx_base + suffixes[0])

        if ft4222_candidate.exists() and ftd2xx_candidate.exists():
            return ft4222_candidate, ftd2xx_candidate

        # Try alternate suffixes for the base names
        for suffix in suffixes[1:]:
            ft4222_alt = directory / (ft4222_base + suffix)
            ftd2xx_alt = directory / (ftd2xx_base + suffix)
            if ft4222_alt.exists() and ftd2xx_alt.exists():
                return ft4222_alt, ftd2xx_alt

    return None, None


def _linux_system_dirs() -> List[Path]:
    """Return platform-specific system library directories to search."""
    dirs: List[Path] = []
    if sys.platform == "linux":
        # Multiarch paths (architecture-specific)
        import platform as _platform
        machine = _platform.machine()
        if machine in ("armv7l", "armv6l"):
            dirs.append(Path("/usr/lib/arm-linux-gnueabihf"))
        elif machine == "aarch64":
            dirs.append(Path("/usr/lib/aarch64-linux-gnu"))
        elif machine == "x86_64":
            dirs.append(Path("/usr/lib/x86_64-linux-gnu"))
        # Generic system dirs
        dirs.extend([
            Path("/usr/local/lib"),
            Path("/usr/lib"),
            Path("/opt/wfview/lib"),
            Path("/usr/local/lib/wfview"),
        ])
        # Home directory installs
        home = Path.home()
        dirs.extend([
            home / ".local" / "lib",
            home / "wfview" / "build" / "lib",
        ])
    elif sys.platform == "darwin":
        dirs.extend([
            Path("/usr/local/lib"),
            Path("/opt/homebrew/lib"),
        ])
    elif sys.platform == "win32":
        dirs.extend([
            Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32",
            Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "FTDI",
        ])
    return dirs


def get_candidate_library_dirs() -> List[Path]:
    """Return FTDI library search directories in priority order."""
    candidate_dirs: List[Path] = []

    ftdi_dir = os.environ.get("FT710_FTDI_LIB_DIR")
    if ftdi_dir:
        candidate_dirs.append(Path(ftdi_dir))

    for root in get_resource_roots():
        candidate_dirs.extend([
            root / "lib",
            root / "vendor" / "ftdi",
            root / "vendor" / "ftdi" / "windows" / "bin" / "x64",
        ])

    candidate_dirs.extend(_linux_system_dirs())

    unique: List[Path] = []
    for directory in candidate_dirs:
        if directory not in unique:
            unique.append(directory)
    return unique


def configure_windows_dll_search_path() -> None:
    """Add candidate FTDI directories to the Windows DLL loader path."""
    if sys.platform != "win32":
        return
    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is None:
        return
    for directory in get_candidate_library_dirs():
        if directory.is_dir():
            handle = add_dll_directory(str(directory))
            _DLL_DIRECTORY_HANDLES.append(handle)


def find_ftdi_libraries() -> tuple[Optional[Path], Optional[Path]]:
    """Return FT4222 and FTD2XX library paths, or (None, None).

    Search order:
      1. Explicit env vars: FT710_FT4222_DYLIB / FT710_FTD2XX_DYLIB
         (also checked: FT710_FT4222_LIB / FT710_FTD2XX_LIB on non-macOS)
      2. FT710_FTDI_LIB_DIR env var
      3. Project lib/ directory
      4. Project vendor/ftdi/ directory
      5. Platform-specific system directories
    """
    # ── Explicit per-library env vars (cross-platform) ────────────────
    ft4222_key = "FT710_FT4222_DYLIB" if sys.platform == "darwin" else "FT710_FT4222_LIB"
    ftd2xx_key = "FT710_FTD2XX_DYLIB" if sys.platform == "darwin" else "FT710_FTD2XX_LIB"

    # Also check the original macOS-style names on all platforms (backward compat)
    for ft4222_env, ftd2xx_env in [
        ("FT710_FT4222_DYLIB", "FT710_FTD2XX_DYLIB"),
        ("FT710_FT4222_LIB", "FT710_FTD2XX_LIB"),
        ("FT710_FT4222_SO", "FT710_FTD2XX_SO"),
    ]:
        explicit_ft4222 = os.environ.get(ft4222_env)
        explicit_ftd2xx = os.environ.get(ftd2xx_env)
        if explicit_ft4222 and explicit_ftd2xx:
            ft4222 = Path(explicit_ft4222)
            ftd2xx = Path(explicit_ftd2xx)
            if ft4222.exists() and ftd2xx.exists():
                return ft4222, ftd2xx

    # ── Search ────────────────────────────────────────────────────────
    for directory in get_candidate_library_dirs():
        if not directory.is_dir():
            continue
        ft4222, ftd2xx = _libs_in_dir(directory)
        if ft4222 and ftd2xx:
            return ft4222, ftd2xx
    return None, None


def require_ftdi_libraries() -> tuple[Path, Path]:
    ft4222, ftd2xx = find_ftdi_libraries()
    if ft4222 is None or ftd2xx is None:
        suffix = _platform_suffixes()[0]
        raise FileNotFoundError(
            f"FTDI libraries not found. Put libft4222{suffix} and "
            f"libftd2xx{suffix} in ./lib or ./vendor/ftdi, or set "
            "FT710_FTDI_LIB_DIR."
        )
    return ft4222, ftd2xx


def get_ft4222_clock_divider() -> int:
    """Return FT4222 SPI clock divider enum value.

    wfview uses CLK_DIV_64 (enum value 6, 375 kHz) with SYS_CLK_24
    for FT-710 scope.  This is the project default — matches wfview
    exactly for proven stability and frame rate.

    Hardware experiments can override this without code edits by
    setting FT710_FT4222_CLK_DIV.

    FT4222 enum: 0=NONE 1=/2 2=/4 3=/8 4=/16 5=/32 6=/64 7=/128 8=/256 9=/512
    """
    raw = os.environ.get("FT710_FT4222_CLK_DIV", "6")  # CLK_DIV_64 (375 kHz, matches wfview default)
    try:
        value = int(raw)
    except ValueError:
        return 5
    return value if 1 <= value <= 9 else 5
