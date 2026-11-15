"""
A serial-port lookalike over the SCU-LAN10 CAT tunnel
=====================================================
``YaesuCatController`` writes bytes to a port and reads the answer back; on an
SCU-LAN10 radio there is no port at all — CAT travels as UDP payloads on
port+1.  So the controller is handed this object instead of a
``serial.Serial`` and never learns the difference.  Same trick, same contract
as ``backends/icom_net/transport.py`` (the G7250 precedent), with the Yaesu
side's own quirks.

Contract (everything ``YaesuCatController`` uses):

``is_open``                 attribute, checked before every write/read
``open()`` / ``close()``    attach to / detach from the client's CAT callback
``write(b)``                hand the text to the client (called from a thread)
``flush()``                 no-op
``reset_input_buffer()``    drop whatever queued while we were not listening
``in_waiting``              number of buffered bytes
``read(n)``                 block up to ``read_timeout``, return up to ``n``

Two details decide the error semantics, and both come from
``YaesuCatController._is_device_fatal`` (cat_core.py:114-126), which is the
field-proven classifier:

* a **lost session is transient** — the client reconnects on its own, so it
  raises ``serial.SerialTimeoutException`` (classified transient: retry, do
  not latch ``serial_connected=False``, which would tell the operator the USB
  cable was unplugged);
* only **this object being unusable** raises ``serial.SerialException``.

``write`` is called from ``asyncio.to_thread`` while the datagram transports
belong to the event loop, so the hand-off goes through
``ScuLanClient.send_cat_threadsafe`` rather than touching a socket directly.
A repeat write is a *new* ``cmd_seq`` (the client assigns one per call), which
is what makes a retried `TX0;` act — the unit dedupes byte-identical resends.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

import serial

logger = logging.getLogger("yaesu.scu_lan.transport")

#: Same as the serial path's ``timeout=0.1``; the controller polls every 10 ms.
_READ_TIMEOUT_S = 0.1


class ScuLanCatTransport:
    """Serial-port lookalike backed by `ScuLanClient`'s CAT channel."""

    def __init__(self, client, *, read_timeout: float = _READ_TIMEOUT_S) -> None:
        self._client = client
        try:
            self._read_timeout = float(read_timeout)
        except (TypeError, ValueError):
            self._read_timeout = _READ_TIMEOUT_S
        self.timeout = self._read_timeout
        self._cond = threading.Condition()
        self._buffer = bytearray()
        self._open = False
        self._attached: Optional[object] = None

    # ── lifecycle ──────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        """Start receiving CAT frames into this transport's buffer."""
        if self._open:
            return
        self._open = True
        self._attached = getattr(self._client, "on_cat", None)
        self._client.on_cat = self.feed
        logger.debug("SCU-LAN10 CAT transport attached")

    def close(self) -> None:
        """Detach.  Idempotent, like ``serial.Serial.close``."""
        if not self._open:
            return
        self._open = False
        current = getattr(self._client, "on_cat", None)
        # A bound method is recreated on every attribute access, so identity
        # has to be checked through __self__ — `current is self.feed` is never
        # true, and a failed check would leave frames buffering into a closed
        # transport.
        if getattr(current, "__self__", None) is self:
            self._client.on_cat = self._attached      # type: ignore[assignment]
        self._attached = None
        self.reset_input_buffer()
        logger.debug("SCU-LAN10 CAT transport detached")

    # ── serial surface ────────────────────────────────────────────

    def write(self, data: bytes) -> int:
        """Hand one CAT command (or a burst) to the session."""
        if not self._open:
            raise serial.SerialException("SCU-LAN10 CAT transport is closed")
        payload = bytes(data)
        text = payload.decode("ascii", "replace")
        if not self._client.send_cat_threadsafe(text):
            # Transient by design: the session comes back by itself, and
            # latching "device gone" would misreport a LAN hiccup as an
            # unplugged USB cable.
            raise serial.SerialTimeoutException(
                "SCU-LAN10 session is down")
        return len(payload)

    def flush(self) -> None:
        """UDP has no output buffer to drain."""

    def reset_input_buffer(self) -> None:
        """Drop everything buffered — the controller calls this before a write."""
        with self._cond:
            self._buffer.clear()

    @property
    def in_waiting(self) -> int:
        with self._cond:
            return len(self._buffer)

    def read(self, size: int = 1) -> bytes:
        """Block up to ``read_timeout`` for ``size`` bytes; return what arrived."""
        if size <= 0:
            return b""
        with self._cond:
            self._cond.wait_for(lambda: len(self._buffer) >= size,
                                timeout=self._read_timeout)
            chunk = bytes(self._buffer[:size])
            del self._buffer[:size]
            return chunk

    # ── feeding (called by the client, on the event loop) ─────────

    def feed(self, text: str) -> None:
        """One complete CAT frame from the session, as text."""
        if not text:
            return
        data = text.encode("ascii", "replace")
        with self._cond:
            self._buffer.extend(data)
            self._cond.notify_all()

    # ── diagnostics ───────────────────────────────────────────────

    def describe(self) -> str:
        """One line for the startup log / support bundle."""
        client = self._client
        host = getattr(client, "host", "?")
        port = getattr(client, "base_port", 0) + 1
        session = getattr(client, "session_id", 0)
        return (f"SCU-LAN10 CAT tunnel {host}:{port} "
                f"(session 0x{session:02X}, attached={self._open})")

    def __repr__(self) -> str:                              # pragma: no cover
        return f"<ScuLanCatTransport open={self._open} waiting={self.in_waiting}>"
