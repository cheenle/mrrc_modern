"""Backward-compatibility shim — the FT-710 CAT controller moved to
``backends/ft710/cat_controller.py``.  New code should import from there.
"""
from backends.ft710.cat_controller import *  # noqa: F401,F403
from backends.ft710.cat_controller import CatController

__all__ = ["CatController"]
