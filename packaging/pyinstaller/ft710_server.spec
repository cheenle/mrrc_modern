# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys


ROOT = Path(SPECPATH).parents[1]
DIST_ROOT = ROOT / "dist" / "windows" / "_pyinstaller"


# The vendor/ftdi runtime tree is platform-specific. Ship the Windows DLLs
# only on Windows; on macOS ship the .dylib tree if present (scope falls back
# to the S-meter spectrum when it is absent, so omission is non-fatal).
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
    [str(ROOT / "server.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "static"), "static"),
        (str(ROOT / "mem_channels.json"), "."),
        *_ftdi_data,
    ],
    hiddenimports=[
        "serial",
        "pyaudio",
        "atr1000_client",
        "atr1000_tuner",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
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
    [],
    exclude_binaries=True,
    name="ft710-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ft710-server",
)
