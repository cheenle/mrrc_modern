"""
A serial-like transport over the Icom network CI-V tunnel
=========================================================
``CivController`` reads a byte stream from a port and writes frames to it.  On
the G7250 the port that carries CI-V is not a COM port at all — measured
2026-11-12, the USB side answers as ``0xA4`` (a G90 subset with **no** ``0x27``
spectrum) while the network tunnel answers as ``0xA0`` with a full IC-7300
waterfall.  So the controller is handed this object instead of a
``serial.Serial`` and never learns the difference.

Contract (everything ``CivController`` uses):

``open()``                 attach to the client's CI-V callback
``read(n)``                block up to ``read_timeout``, return up to ``n`` bytes
``write(b)``               hand the frame to the client (thread-safe)
``close()``                detach
``reset_input_buffer()``   drop whatever queued while we were not listening

Two details matter:

* ``write`` is called from the asyncio default executor (``to_thread``), while
  the client's datagram transports belong to the event loop — so writes go
  through ``send_civ_threadsafe`` rather than touching the socket directly.
* ``read`` is called from ``CivController``'s own reader thread and must
  **block** with a timeout, exactly like a serial port's ``timeout=0.1``.  A
  ``queue.Queue`` with a timeout gives that for free.
"""
from __future__ import annotations

import logging
import queue
from typing import Deque, Optional
from collections import deque

import serial

logger = logging.getLogger("icom.net.transport")

_READ_TIMEOUT_S = 0.1


class NetworkCivTransport:
    """Serial-port lookalike backed by ``IcomNetworkClient``'s CI-V tunnel."""

    def __init__(self, client, *, read_timeout: float = _READ_TIMEOUT_S) -> None:
        self._client = client
        try:
            self._read_timeout = float(read_timeout)
        except (TypeError, ValueError):
            self._read_timeout = _READ_TIMEOUT_S
        self._queue: "queue.Queue[bytes]" = queue.Queue()
        self._pending: Deque[bytearray] = deque()
        self._open = False
        self._closed = False

    # ── lifecycle ──────────────────────────────────────────────────

    def open(self) -> None:
        """Sync open (tests / a transport that is already running)."""
        if self._open:
            return
        self._open = True
        self._client.on_civ = self._on_frame
        # Opening the tunnel is idempotent; the client keeps re-sending the
        # open packet until a frame actually arrives.
        self._client.open_civ()

    async def open_async(self, timeout: float = 10.0) -> None:
        """Open from the event loop: start the LAN session, then the tunnel.

        ``CivController.connect()`` uses this when it is available — a bare
        ``open()`` cannot wait, and the CAT link is useless until the radio has
        opened a stream, so the wait belongs here rather than in a retry loop
        somewhere above.
        """
        self._open = True
        self._closed = False
        self._client.on_civ = self._on_frame
        await self._client.ensure_started()
        if not await self._client.wait_until_streaming(timeout):
            raise serial.SerialException(
                f"the radio at {getattr(self._client, 'host', '?')} did not "
                f"open a stream within {timeout:.0f} s")
        self._client.open_civ()

    def close(self) -> None:
        self._closed = True
        if self._client is not None:
            current = self._client.on_civ
            # Bound methods are re-created per access: compare ownership, not
            # identity, or a closed transport leaves its callback installed.
            if getattr(current, "__self__", None) is self:
                self._client.on_civ = None

    # ── serial surface ─────────────────────────────────────────────

    def _on_frame(self, frame: bytes) -> None:
        if frame:
            self._queue.put(bytes(frame))

    def read(self, size: int = 256) -> bytes:
        """Block for at most ``read_timeout`` and return up to ``size`` bytes."""
        if self._closed:
            return b""
        if not self._pending:
            try:
                self._pending.append(bytearray(self._queue.get(timeout=self._read_timeout)))
            except queue.Empty:
                return b""
        chunk = self._pending[0]
        take = min(size, len(chunk))
        out = bytes(chunk[:take])
        del chunk[:take]
        if not chunk:
            self._pending.popleft()
        return out

    def write(self, data: bytes) -> int:
        if self._closed or self._client is None:
            raise serial.SerialException("CI-V transport is closed")
        self._client.send_civ_threadsafe(bytes(data))
        return len(data)

    def reset_input_buffer(self) -> None:
        self._pending.clear()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    @property
    def is_open(self) -> bool:
        return self._open and not self._closed

    @property
    def port(self) -> str:
        return f"icom-net:{getattr(self._client, 'host', '?')}"
