"""Shared Yaesu ASCII-CAT transport (spec 2026-09-12 §4.2).

The transport half of this file is a deliberate port of the field-proven
`backends/ft710/cat_controller.py` logic (framing, prefix filtering, priority
preemption, ENXIO classification, reconnect); everything model-specific is
read from the `YaesuModelProfile` passed to the constructor.  The FT-710
keeps its own copy: this core must not change the radio that is in daily use
until the phase-3 migration has been A/B tested against hardware.

The command layer that sits on top of this transport lives in the same
module (task 3 of the implementation plan).
"""
from __future__ import annotations

import asyncio
import errno
import logging
import time
from typing import Optional

import serial

from backends.yaesu.yaesu_profiles import YaesuModelProfile

logger = logging.getLogger(__name__)

# The FT-710 CAT processor needs ~20 ms between commands (matches Hamlib);
# the newer FTX-1 firmware is no slower, and the FT-710 value is the one
# proven against real hardware.
COMMAND_SETTLE_SECONDS = 0.02
DEFAULT_TIMEOUT = 1.0


class YaesuCatController:
    """Serial CAT transport for the Yaesu ASCII protocol (`CMD;`)."""

    def __init__(self, port: str, baudrate: Optional[int] = None,
                 profile: Optional[YaesuModelProfile] = None):
        if profile is None:
            raise ValueError("YaesuCatController requires a model profile")
        self._profile = profile
        self.port = port
        self.baudrate = baudrate or profile.default_baud
        self._ser: Optional[serial.Serial] = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._timeout = DEFAULT_TIMEOUT
        # Set by priority commands (PTT/TUNE) to make in-flight poll reads
        # release the serial lock immediately.
        self._cancel_polls = asyncio.Event()

    # ── Error classification (ported; see the FT-710 field notes) ────

    @staticmethod
    def _is_device_gone(exc: Exception) -> bool:
        """True when the USB serial bridge itself vanished.

        macOS reports this as errno 6 (ENXIO, "Device not configured") and the
        node then fails to open with ENOENT.  It is a physical-layer event
        (re-enumeration, marginal cable/hub/power), not a protocol error, so
        it is worth one actionable warning per outage.
        """
        errno_value = getattr(exc, "errno", None)
        if errno_value in (errno.ENXIO, errno.ENODEV, errno.ENOENT):
            return True
        text = str(exc).lower()
        return ("device not configured" in text
                or "no such file or directory" in text)

    @staticmethod
    def _is_device_fatal(exc: Exception) -> bool:
        """True only for device-level failures; False for transient I/O.

        A serial write/read error must not immediately mark the radio
        disconnected: under command contention (tuning, channel switches) a
        write can hit SerialTimeoutException or a short OSError burst while
        the device is perfectly fine — marking disconnected then flips
        radio.serial_connected=False and the UI flashes "radio not
        connected".  Only real device loss (unplug, port gone) should latch
        it.  (field finding 2026-08-15)
        """
        if isinstance(exc, serial.SerialTimeoutException):
            return False                      # transient, retry instead
        if isinstance(exc, serial.SerialException):
            return True                       # port not open / device-level
        if isinstance(exc, OSError):
            # errno 6 = ENXIO "Device not configured", 19 = ENODEV: real loss
            return exc.errno in (6, 19)
        return True

    # ── Low-level I/O ───────────────────────────────────────────────

    async def _write(self, data: bytes):
        """Write bytes to the serial port (threaded), then settle."""
        _serial = self._ser                 # narrow once: the closure below
        if _serial is None or not _serial.is_open:  # cannot re-check the attribute
            raise serial.SerialException("Port not open")

        def _w():
            _serial.reset_input_buffer()    # Clear stale input
            _serial.write(data)
            _serial.flush()

        await asyncio.to_thread(_w)
        await asyncio.sleep(COMMAND_SETTLE_SECONDS)

    async def _read_until(self, terminator: bytes = b";",
                          expected_prefix: str = "",
                          timeout: Optional[float] = None) -> Optional[bytes]:
        """Read until a message matching ``expected_prefix`` is complete.

        Messages that do not start with the prefix are Auto-Information (AI)
        frames — or stale answers to an aborted poll — and are discarded so
        they cannot be mistaken for the answer; parsing continues in whatever
        bytes remain buffered.  A stale frame of the *same* command is not
        distinguishable and is therefore accepted, which is why callers pass
        their own command as the prefix.
        """
        _serial = self._ser                 # narrow once for the closure
        if _serial is None or not _serial.is_open:
            raise serial.SerialException("Port not open")

        def _r():
            buf = bytearray()
            deadline = time.monotonic() + (timeout if timeout is not None
                                           else self._timeout)
            while time.monotonic() < deadline:
                # A priority command is waiting for the lock: bail out early.
                if self._cancel_polls.is_set():
                    return None
                waiting = _serial.in_waiting
                if waiting > 0:
                    buf.extend(_serial.read(waiting))
                    if terminator in buf:
                        idx = buf.find(terminator)
                        msg = bytes(buf[:idx + len(terminator)])
                        if expected_prefix:
                            text = msg.decode("ascii",
                                              errors="replace").rstrip(";")
                            if not text.startswith(expected_prefix):
                                logger.debug(
                                    "Skipping unexpected response %r "
                                    "(expected prefix %r)",
                                    text[:40], expected_prefix)
                                buf = bytearray(buf[idx + len(terminator):])
                                continue
                        return msg
                time.sleep(0.01)
            return bytes(buf) if buf else None

        return await asyncio.to_thread(_r)

    # ── Command interface ───────────────────────────────────────────

    async def send_command(self, cmd: str,
                           timeout: Optional[float] = None) -> Optional[str]:
        """Send ``cmd;`` and return the answer without its terminator.

        All serial I/O is serialised through ``self._lock``.  The command
        itself is used as the expected answer prefix, which is what makes AI
        frames harmless.  ``timeout`` lets pollers bound the lock occupancy
        so a non-responding query cannot block PTT.
        """
        # If a priority command (PTT/TUNE) has signalled polls to cancel,
        # don't even try to acquire the lock.
        if self._cancel_polls.is_set():
            return None

        await self._lock.acquire()
        try:
            # Double-check after acquiring the lock: a priority command may
            # have set the flag while this call was queued.
            if self._cancel_polls.is_set():
                return None
            if not self._connected or self._ser is None:
                return None

            try:
                await self._write((cmd + ";").encode("ascii"))
            except Exception as exc:
                logger.error("Serial write error for '%s': %s", cmd, exc)
                if self._is_device_fatal(exc):
                    self._connected = False
                return None

            try:
                raw = await self._read_until(b";", expected_prefix=cmd,
                                            timeout=timeout)
            except Exception as exc:
                logger.error("Serial read error for '%s': %s", cmd, exc)
                if self._is_device_fatal(exc):
                    self._connected = False
                return None

            if raw is None:
                logger.debug("Command timeout (no data): %s", cmd)
                return None
            text = raw.decode("ascii", errors="replace").rstrip(";")
            return text or None
        finally:
            self._lock.release()

    async def send_set_command(self, cmd: str) -> bool:
        """Write-only set command (fire and forget).

        Set commands on these radios often produce no answer (or a late one);
        waiting would hold the serial lock for the whole timeout on every UI
        action and make tuning stutter.  The lock is held for the write only
        (~1-2 ms).
        """
        async with self._lock:
            if not self._connected or self._ser is None:
                return False
            try:
                await self._write((cmd + ";").encode("ascii"))
                return True
            except Exception as exc:
                logger.error("Serial write error for '%s': %s", cmd, exc)
                if self._is_device_fatal(exc):
                    self._connected = False
                return False

    async def send_priority_set_command(self, cmd: str) -> bool:
        """Latency-critical set command (PTT/TUNE) that preempts poll reads.

        Sets ``_cancel_polls`` so in-flight ``_read_until`` threads and
        queued poll waiters abort, then takes the lock with minimal delay.
        Write-only for speed, like ``send_set_command``.
        """
        self._cancel_polls.set()
        try:
            # Let an in-flight _read_until thread observe the flag.
            await asyncio.sleep(0.005)
            async with self._lock:
                # Clear it now that we hold the lock; polls resume after
                # we release it.
                self._cancel_polls.clear()
                if not self._connected or self._ser is None:
                    return False
                try:
                    await self._write((cmd + ";").encode("ascii"))
                    return True
                except Exception as exc:
                    logger.error("Serial write error for '%s': %s", cmd, exc)
                    if self._is_device_fatal(exc):
                        self._connected = False
                    return False
        finally:
            self._cancel_polls.clear()

    async def query(self, cmd: str,
                    timeout: Optional[float] = None) -> Optional[str]:
        """Query form (prefix + ';'), e.g. ``query("FA")`` -> ``"FA014074000"``."""
        return await self.send_command(cmd, timeout=timeout)

    async def set(self, cmd: str) -> bool:
        """Set form (prefix + value), e.g. ``set("FA014074000")``."""
        return await self.send_set_command(cmd)

    # ── Connection management ───────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def model(self) -> str:
        return self._profile.model_key

    async def connect(self) -> bool:
        """Open the serial port.

        The retry/error-classification rhythm is copied from the FT-710
        controller rather than reinvented because it is the version that
        survives USB re-enumeration on real hardware.
        """
        if self._connected and self._ser is not None and self._ser.is_open:
            return True
        try:
            self._ser = await asyncio.to_thread(
                serial.Serial, self.port, self.baudrate,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE, timeout=0.1)
            self._connected = True
            logger.info("%s: CAT port %s open at %d baud",
                        self._profile.display_name, self.port, self.baudrate)
            return True
        except Exception as exc:
            self._connected = False
            self._ser = None
            if self._is_device_gone(exc):
                logger.warning("%s: CAT port %s is gone (%s) — check the USB "
                               "cable/hub or the port name",
                               self._profile.display_name, self.port, exc)
            else:
                logger.error("%s: cannot open CAT port %s: %s",
                             self._profile.display_name, self.port, exc)
            return False

    async def disconnect(self) -> None:
        self._connected = False
        if self._ser is not None:
            try:
                await asyncio.to_thread(self._ser.close)
            except Exception:
                pass
            self._ser = None

    async def reconnect_loop(self) -> bool:
        """Reconnect with the FT-710 rhythm (1 s, doubling to a 10 s cap).

        Returns True once the port is open again.  Callers (the server
        watchdog) re-run scope init / state sync afterwards; this method only
        restores the transport.
        """
        delay = 1.0
        while True:
            if await self.connect():
                logger.info("%s: CAT link restored on %s",
                            self._profile.display_name, self.port)
                return True
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)
            logger.debug("%s: CAT reconnect retry in %.0fs",
                         self._profile.display_name, delay)
