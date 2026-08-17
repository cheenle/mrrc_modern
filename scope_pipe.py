"""Backward-compatibility shim — the scope pipe worker moved to
``backends/ft710/scope_pipe.py``.  This shim stays runnable so existing
PyInstaller specs (which use this file as the entry point) keep working.
"""
from backends.ft710.scope_pipe import *  # noqa: F401,F403
from backends.ft710.scope_pipe import (  # noqa: F401
    apply_control_line,
    emit_status,
    resync_device,
    open_device,
    close_device,
    main,
)

if __name__ == "__main__":
    main()
