"""Application-root resolution that works in-source and PyInstaller-frozen.

PyInstaller 6 onedir unpacks datas and the bytecode into ``_internal/`` and
points ``sys._MEIPASS`` there; ``Path(__file__)`` for PYZ-bundled modules does
not reliably exist on disk.  Every read-only bundled asset (cty.dat, web
static, desktop dist, wsjt_core.dll) is resolved through :func:`app_root` so
the same code works from a checkout and from the frozen installer.
"""
from __future__ import annotations

import sys
from pathlib import Path


def app_root() -> Path:
    """Repo root in source; PyInstaller ``_internal/`` when frozen."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]
