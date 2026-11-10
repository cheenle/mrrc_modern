"""
FTDI library discovery for FT-710 scope support.

The project is intended to run standalone. Prefer libraries copied into this
repository, while still allowing explicit environment overrides.

Supports macOS (.dylib), Linux (.so), and Windows (.dll).
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple
import ctypes
import hashlib
import os
import sys

from config import _env


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

    ftdi_dir = _env("MRRC_FTDI_LIB_DIR")
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
      1. Explicit env vars: MRRC_FT4222_DYLIB / MRRC_FTD2XX_DYLIB
         (also checked: MRRC_FT4222_LIB / MRRC_FTD2XX_LIB on non-macOS)
      2. MRRC_FTDI_LIB_DIR env var
      3. Project lib/ directory
      4. Project vendor/ftdi/ directory
      5. Platform-specific system directories
    """
    # ── Explicit per-library env vars (cross-platform) ────────────────
    ft4222_key = "MRRC_FT4222_DYLIB" if sys.platform == "darwin" else "MRRC_FT4222_LIB"
    ftd2xx_key = "MRRC_FTD2XX_DYLIB" if sys.platform == "darwin" else "MRRC_FTD2XX_LIB"

    # Also check the original macOS-style names on all platforms (backward compat)
    for ft4222_env, ftd2xx_env in [
        ("MRRC_FT4222_DYLIB", "MRRC_FTD2XX_DYLIB"),
        ("MRRC_FT4222_LIB", "MRRC_FTD2XX_LIB"),
        ("MRRC_FT4222_SO", "MRRC_FTD2XX_SO"),
    ]:
        explicit_ft4222 = _env(ft4222_env)
        explicit_ftd2xx = _env(ftd2xx_env)
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
            "MRRC_FTDI_LIB_DIR."
        )
    return ft4222, ftd2xx


def _sha256(path: Path) -> str:
    """Content digest, read in chunks (the FTDI library is ~550 KB)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_ftdi_pair(ft4222: Path, ftd2xx: Path) -> Tuple[str, str]:
    """Say how the two library slots relate to each other.

    Returns ``(relationship, note)``; *relationship* is one of
    ``"same-inode"`` (a symlink or hard link: one file, one dlopen),
    ``"same-content"`` (a byte-identical **copy** — two inodes, two dlopens),
    ``"distinct"`` or ``"unknown"``.

    ``"same-content"`` is the one that bites: FTDI's Linux LibFT4222 build
    statically links libftd2xx and re-exports its symbols, so the tree ships
    ``libftd2xx.so -> libft4222.so`` (``packaging/box/vendor-ftdi.md``). A
    ``cp`` in place of that symlink looks identical on disk but gives dlopen a
    second object with its own D2XX device table — see
    :func:`load_ftdi_libraries` for what that does to ``FT4222_SPIMaster_Init``.
    """
    try:
        a = ft4222.stat()
        b = ftd2xx.stat()
    except OSError as exc:
        return "unknown", f"pair metadata unreadable ({exc})"
    if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino):
        return "same-inode", ""
    if a.st_size != b.st_size:
        return "distinct", ""
    try:
        same = _sha256(ft4222) == _sha256(ftd2xx)
    except OSError as exc:
        return "distinct", f"content compare failed ({exc})"
    if same:
        return "same-content", (
            "libftd2xx.so is a COPY of libft4222.so, not the symlink the tree "
            "ships (packaging/box/vendor-ftdi.md)"
        )
    return "distinct", ""


def _exports_d2xx(lib: "ctypes.CDLL") -> bool:
    """True when this library carries the D2XX API itself (symbol lookup)."""
    try:
        getattr(lib, "FT_OpenEx")
        return True
    except AttributeError:
        return False


@dataclass(frozen=True)
class FtdiLibraries:
    """The loaded FTDI pair, plus how the two slots relate.

    ``d2xx`` and ``ft4222`` are the *same object* whenever one library serves
    both APIs — which is the normal case for the shipped aarch64 build and is
    what makes a copied ``libftd2xx.so`` harmless.
    """

    d2xx: "ctypes.CDLL"
    ft4222: "ctypes.CDLL"
    relationship: str
    note: str

    @property
    def shared(self) -> bool:
        return self.d2xx is self.ft4222


def load_ftdi_libraries(ft4222_path: Path, ftd2xx_path: Path) -> FtdiLibraries:
    """dlopen the FTDI pair so both API sets see **one** D2XX device table.

    The FT4222 API keeps its handles inside the D2XX instance it was built
    against, so ``FT_OpenEx`` and ``FT4222_SPIMaster_Init`` have to run in the
    same loaded object:

    * a self-contained build (Linux and macOS LibFT4222 re-export ``FT_*``) is
      used for **both** slots;
    * a thin wrapper build (no ``FT_*`` export) leaves D2XX in the second file,
      which the wrapper itself links against — the loader then hands back the
      object that file resolves to anyway.

    Measured 2026-11-10 (W103D + FT-710): with a *copy* in the second slot the
    two instances did not share state, and ``FT4222_SPIMaster_Init`` answered
    1000 — ``FT4222_DEVICE_NOT_SUPPORTED``, "this device is not a FT4222
    chip" — right after a successful ``FT_OpenEx``. Every ``scope_pipe`` start
    then failed with ``fatal:cannot_open_device``, and the waterfall quietly
    degraded to the S-meter synthesiser. The pipe that was already running
    (its libraries mapped before the copy appeared) kept streaming, so the
    symptom only surfaced at the next restart.

    Raises ``OSError`` when a library cannot be loaded, exactly like ``CDLL``.
    """
    f4 = ctypes.CDLL(str(ft4222_path))
    relationship, note = classify_ftdi_pair(ft4222_path, ftd2xx_path)
    if _exports_d2xx(f4):
        return FtdiLibraries(d2xx=f4, ft4222=f4,
                             relationship=relationship, note=note)
    d2xx = ctypes.CDLL(str(ftd2xx_path))
    return FtdiLibraries(d2xx=d2xx, ft4222=f4,
                         relationship=relationship, note=note)


def get_ft4222_clock_divider() -> int:
    """Return FT4222 SPI clock divider enum value.

    wfview uses CLK_DIV_64 (enum value 6, 375 kHz) with SYS_CLK_24
    for FT-710 scope.  This is the project default — matches wfview
    exactly for proven stability and frame rate.

    Hardware experiments can override this without code edits by
    setting MRRC_FT4222_CLK_DIV.

    FT4222 enum: 0=NONE 1=/2 2=/4 3=/8 4=/16 5=/32 6=/64 7=/128 8=/256 9=/512
    """
    raw = _env("MRRC_FT4222_CLK_DIV", "6")  # CLK_DIV_64 (375 kHz, matches wfview default)
    try:
        value = int(raw)
    except ValueError:
        return 5
    return value if 1 <= value <= 9 else 5
