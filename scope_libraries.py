"""Backward-compatibility shim — FTDI library discovery moved to
``backends/ft710/scope_libraries.py``.  New code should import from there.
"""
from backends.ft710.scope_libraries import *  # noqa: F401,F403
from backends.ft710.scope_libraries import (  # noqa: F401
    get_resource_roots,
    get_candidate_library_dirs,
    configure_windows_dll_search_path,
    find_ftdi_libraries,
    require_ftdi_libraries,
    get_ft4222_clock_divider,
)
