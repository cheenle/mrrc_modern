"""
Icom network-radio protocol (RS-BA1) — the G7250's LAN port
===========================================================
The radio's network side is not a raw IQ stream: it is Icom's network-radio
protocol, with a login/token handshake and three UDP streams (control, CI-V
tunnel, audio).  Evidence and the design record:
``docs/superpowers/specs/2026-11-12-icom-network-audio-design.md``.

- ``protocol.py`` — pure packet codecs (no sockets, no asyncio).

This package is deliberately model-agnostic: the protocol belongs to Icom, so a
future IC-705/7610-over-LAN backend can reuse it unchanged.
"""
