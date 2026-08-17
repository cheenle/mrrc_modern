# -*- mode: python ; coding: utf-8 -*-
#
# macOS menu-bar launcher for MRRC Modern.
#
# onefile + windowed (no console) so the rumps/PyObjC menu-bar app runs without
# a Terminal window. onefile is deliberate: the launcher has no `_internal/` of
# its own, so it cannot collide with the server's `_internal/` that build.sh
# copies alongside it in Contents/MacOS/.
#
# This spec produces a bare onefile executable named MRRC-Modern-Launcher.
# build.sh then hand-assembles the .app bundle (Contents/Info.plist +
# Contents/MacOS/...).  We deliberately do NOT use PyInstaller's BUNDLE target:
# onefile + BUNDLE is deprecated ("clashes with macOS's security") and produces
# a malformed bundle.
#
# The runtime files (MRRC-Modern-Server onedir, scope_pipe, default.env,
# mem_channels.json, vendor/ftdi/macos) are NOT bundled here — build.sh copies
# them into the assembled .app after PyInstaller produces this exe.

import os
from pathlib import Path


ROOT = Path(SPECPATH).parents[1]

# Injected by build.sh from the top CHANGELOG entry; "0.0.0" only when run by
# hand without the env var set.
VERSION = os.environ.get("MRRC_VERSION", "0.0.0").lstrip("v")


a = Analysis(
    [str(ROOT / "macos" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "rumps",
        "objc",
        "AppKit",
        "Foundation",
        "PyObjCTools",
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
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
