# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file spec for the MRRC Modern desktop launcher."""
from pathlib import Path


ROOT = Path(SPECPATH).parents[1]


a = Analysis(
    [str(ROOT / "windows" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "windows" / "default.env"), "windows"),
    ],
    hiddenimports=[
        "ssl_bootstrap",
        "cryptography",
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
    name="MRRC-Modern-Launcher",
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
