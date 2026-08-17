# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-folder spec for the MRRC Modern server.

Bundles the FastAPI server, both radio backends, the static web UI, default
configuration, and Windows vendor runtime files.  The companion
mrrc_modern_launcher.spec builds the user-facing desktop launcher.
"""
from pathlib import Path
import sys


ROOT = Path(SPECPATH).parents[1]
DIST_ROOT = ROOT / "dist" / "windows" / "_pyinstaller"


# Vendor runtime files are platform-specific.  On Windows we ship the FTDI
# DLLs needed for FT-710 FT4222 true spectrum and the libopus DLL used for
# RX/TX Opus audio.  On macOS we ship the .dylib tree if present.  Missing
# vendor files are non-fatal: the FT-710 spectrum falls back to S-meter and
# Opus audio falls back to PCM/silence.
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
    [str(ROOT / "server.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "static"), "static"),
        (str(ROOT / "mem_channels.json"), "."),
        (str(ROOT / "windows" / "default.env"), "windows"),
        (str(ROOT / "windows" / "launcher.py"), "windows"),
        *_vendor_data,
    ],
    hiddenimports=[
        # Serial / audio runtime
        "serial",
        "pyaudio",
        "numpy",
        # Web stack
        "fastapi",
        "websockets",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        # TLS bootstrap (used by launcher, but keep it resolvable in the bundle)
        "ssl_bootstrap",
        "cryptography",
        # Optional tuner client
        "atr1000_client",
        "atr1000_tuner",
        # Both radio backends and their submodules
        "backends",
        "backends.base",
        "backends.ft710.backend",
        "backends.ft710.cat_controller",
        "backends.ft710.config_ft710",
        "backends.ft710.scope_frame",
        "backends.ft710.scope_libraries",
        "backends.ft710.scope_pipe",
        "backends.ft710.scope_producer",
        "backends.ic7300.backend",
        "backends.ic7300.civ_codec",
        "backends.ic7300.civ_controller",
        "backends.ic7300.civ_scope",
        "backends.ic7300.config_ic7300",
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
    name="MRRC-Modern-Server",
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
    name="MRRC-Modern-Server",
)
