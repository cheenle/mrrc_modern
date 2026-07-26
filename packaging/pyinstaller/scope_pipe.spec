# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys


ROOT = Path(SPECPATH).parents[1]


# The vendor/ftdi runtime tree is platform-specific (see ft710_server.spec).
# When absent on macOS, scope_pipe falls back to the S-meter spectrum.
_ftdi_data = []
if sys.platform == "win32":
    _ftdi_root = ROOT / "vendor" / "ftdi" / "windows"
    if _ftdi_root.exists():
        _ftdi_data.append((str(_ftdi_root), "vendor/ftdi/windows"))
elif sys.platform == "darwin":
    _ftdi_root = ROOT / "vendor" / "ftdi" / "macos"
    if _ftdi_root.exists():
        _ftdi_data.append((str(_ftdi_root), "vendor/ftdi/macos"))


a = Analysis(
    [str(ROOT / "scope_pipe.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=_ftdi_data,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="scope_pipe",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
