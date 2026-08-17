# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file spec for the FT-710 FT4222 scope pipe worker.

The worker lives in ``backends/ft710/scope_pipe.py`` and is launched either
as a module (``python -m backends.ft710.scope_pipe``) in source mode or as
the bundled ``scope_pipe.exe`` in a frozen distribution.
"""
from pathlib import Path
import sys


ROOT = Path(SPECPATH).parents[1]


# The scope pipe only needs FTDI libraries; Opus is irrelevant here, but
# keeping the same vendor-tree bundling pattern as the server avoids surprises.
_vendor_data = []
if sys.platform == "win32":
    _ftdi_root = ROOT / "vendor" / "ftdi" / "windows"
    if _ftdi_root.exists():
        _vendor_data.append((str(_ftdi_root), "vendor/ftdi/windows"))
    _opus_root = ROOT / "vendor" / "opus" / "windows"
    if _opus_root.exists():
        _vendor_data.append((str(_opus_root), "vendor/opus/windows"))
elif sys.platform == "darwin":
    _ftdi_root = ROOT / "vendor" / "ftdi" / "macos"
    if _ftdi_root.exists():
        _vendor_data.append((str(_ftdi_root), "vendor/ftdi/macos"))


a = Analysis(
    [str(ROOT / "backends" / "ft710" / "scope_pipe.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=_vendor_data,
    hiddenimports=[
        "backends.ft710.scope_frame",
        "backends.ft710.scope_libraries",
    ],
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
