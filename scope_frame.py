"""Backward-compatibility shim — scope frame parsing moved to
``backends/ft710/scope_frame.py``.  New code should import from there.
"""
from backends.ft710.scope_frame import *  # noqa: F401,F403
from backends.ft710.scope_frame import (  # noqa: F401
    SCOPE_FRAME_SIZE,
    WF_SIZE,
    SYNC_TAIL,
    SYNC_FULL,
    DATA_OFFSET,
    PIPE_PAYLOAD_VERSION,
    ScopeFrame,
    scope_mode_to_cat,
    parse_scope_frame,
    frame_quality,
    encode_pipe_payload,
    parse_pipe_payload,
)
