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
# CA bundle for outbound HTTPS: net_tls.py resolves it under the resource roots, so it
# ships exactly like the other vendor trees (and is platform-independent).
_ca_root = ROOT / "vendor" / "ca"
if _ca_root.exists():
    _vendor_data.append((str(_ca_root), "vendor/ca"))
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


# FT8 work mode: the native DSP library and its runtime dependencies, staged by
# the platform build script into build/ft8/<platform>/ (the Mac build rewrites
# its load commands to @loader_path first; Windows copies the MinGW runtime
# DLLs next to wsjt_core.dll).  Destination "." is deliberate: it is the
# PyInstaller _internal root, which is exactly what app_root() returns at
# runtime — see ft8/core/worker.py:default_library_path().
#
# Missing is not fatal here (the same deal the vendor trees above make): the
# server then runs SSB-only, with FT8 reporting a missing library.  Each build
# script asserts its own staging directory exists before calling PyInstaller,
# so a real packaging stop is caught there, not silently shipped from here.
if sys.platform == "darwin":
    _ft8_root = ROOT / "build" / "ft8" / "macos"
else:
    _ft8_root = ROOT / "build" / "ft8" / "windows"
_ft8_data = [(str(_ft8_root), ".")] if _ft8_root.exists() else []

# ft8/data/cty.dat (the DXCC database) is read through ft8.core.paths.app_root()
# — "repo root, or _MEIPASS when frozen" — and the staging directory above
# carries only the **native libraries**.  Without this entry a packaged build
# resolves the database to a path that does not exist inside the bundle: the
# loader logs the traceback and degrades to an empty db, so entity names and
# the new-DXCC highlight are dead for every user of an installer.  v1.27.0
# shipped that way, which is why it is written down instead of inferred.
_ft8_assets = [(str(ROOT / "ft8" / "data"), "ft8/data")]


# default.env: the launcher seeds the user env from the platform-appropriate
# template (windows/ for Win, macos/ for macOS). Do not ship the other
# platform's template.
if sys.platform == "darwin":
    _config_env_data = [(str(ROOT / "macos" / "default.env"), "macos")]
else:
    _config_env_data = [(str(ROOT / "windows" / "default.env"), "windows")]


a = Analysis(
    [str(ROOT / "server.py")],
    pathex=[str(ROOT), str(ROOT / "macos")],
    binaries=[],
    datas=[
        (str(ROOT / "static"), "static"),
        (str(ROOT / "mem_channels.json"), "."),
        *_config_env_data,
        (str(ROOT / "windows" / "launcher.py"), "windows"),
        *_vendor_data,
        *_ft8_data,
        *_ft8_assets,
    ],
    hiddenimports=[
        # First-run auto-config (launcher + setup endpoint)
        "macos.first_run",
        # Cloud Hub onboarding (settings dialog -> portal -> tunnel).  server.py
        # imports this module, but name it anyway: the v1.24.0 build shipped
        # without the import and every 云端申请 answered 500 with NameError.
        "cloud_hub",
        # Support diagnostics bundle (spec 2026-09-17)
        "support_bundle",
        # Spectrum bandwidth tiers (AD-025).  server.py imports it statically so
        # analysis would find it — named anyway after the v1.24.0 cloud_hub
        # incident shipped a build whose import was missing.
        "spectrum_profile",
        # Serial / audio runtime
        "serial",
        "serial.tools.list_ports",
        "serial.tools.list_ports_osx",
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
        "backends.ic7300.civ_profiles",
        "backends.ic7300.civ_scope",
        "backends.ic7300.config_ic7300",
        "backends.yaesu.backend",
        "backends.yaesu.cat_core",
        "backends.yaesu.yaesu_profiles",
        # lameenc is a single compiled extension module (not a package) and
        # is imported lazily inside the recorder, so name it explicitly.
        "lameenc",
        # FT8 work mode (feat/ft8).  server.py imports ft8.* statically and
        # PyInstaller follows those, but the engine pulls scipy (filters) and
        # soxr (the 44.1 kHz → 12 kHz RX tap resampler) from inside functions,
        # and a lazily imported native extension is exactly the shape that has
        # shipped missing before (cloud_hub, v1.24.0).
        "ft8",
        "ft8.core.binding",
        "ft8.core.protocol",
        "ft8.core.supervisor",
        "ft8.core.worker",
        "ft8.engine.orchestrator",
        "ft8.engine.dsp_decode",
        "ft8.engine.dsp_encode",
        "ft8.engine.audio_rx",
        "ft8.engine.waterfall",
        "ft8.platform.rx_tap",
        "ft8.platform.tx_sink",
        "ft8.platform.cat",
        "ft8.platform.settings",
        "ft8.web.ft8_api",
        "ft8.web.ft8_ws",
        "scipy",
        "scipy.signal",
        "scipy.special",
        "soxr",
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
