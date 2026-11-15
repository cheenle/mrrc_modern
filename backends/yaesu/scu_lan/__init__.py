"""
SCU-LAN10 transport layer (Yaesu LAN unit)
==========================================
`protocol.py` is the wire format, `scope_format.py` turns a spectrum body into
the server's bin line; `client.py` (session state machine), `transport.py`
(serial-port lookalike for `YaesuCatController`), `audio.py` and `scope.py`
(scope producer) are the remaining slices — see
docs/superpowers/specs/2026-11-15-scu-lan-backend-design.md.
"""
from __future__ import annotations

from backends.yaesu.scu_lan import protocol, scope_format

__all__ = ["protocol", "scope_format"]
