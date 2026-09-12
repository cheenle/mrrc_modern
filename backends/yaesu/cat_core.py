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
        # (power class, max watts) once detected; None until first use.
        self._detected_power: Optional[tuple] = None

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

    # ── Identity ────────────────────────────────────────────────────

    async def get_model_id(self, timeout: Optional[float] = None) -> Optional[str]:
        """Read the model ID (`ID;`), e.g. "0840" on every FTX-1 configuration.

        Read-only: the caller logs the observed value and only warns when the
        profile records an expectation (spec §6.2).  Never a hard failure.
        """
        resp = await self.query("ID", timeout=timeout)
        if resp and len(resp) > 2:
            return resp[2:]
        return None

    # ── Frequency / VFO ─────────────────────────────────────────────

    async def set_frequency(self, freq_hz: int, vfo: str = "A") -> bool:
        prefix = "FA" if vfo.upper() == "A" else "FB"
        return await self.set(f"{prefix}{freq_hz:09d}")

    async def get_frequency(self, vfo: str = "A",
                            timeout: Optional[float] = None) -> Optional[int]:
        prefix = "FA" if vfo.upper() == "A" else "FB"
        resp = await self.query(prefix, timeout=timeout)
        if resp and len(resp) >= len(prefix) + 1:
            try:
                return int(resp[len(prefix):])
            except ValueError:
                return None
        return None

    async def get_active_vfo(self, timeout: Optional[float] = None) -> Optional[str]:
        """`VS;` -> "VS0" (VFO-A active) or "VS1" (VFO-B active)."""
        resp = await self.query("VS", timeout=timeout)
        if resp and len(resp) >= 3:
            return "B" if resp.endswith("1") else "A"
        return None

    async def set_vfo(self, vfo: str) -> bool:
        return await self.set("VS0" if vfo.upper() == "A" else "VS1")

    # ── Mode ────────────────────────────────────────────────────────

    async def _leave_memory_mode_if_needed(self) -> None:
        """FTX-1 only: exit memory mode, whose MD sets do not persist.

        Mirrors Hamlib `ftx1/ftx1.c:881-899`, which sends `VM000;` for the
        same reason (the firmware accepts a memory-mode MAIN `MD` but treats
        it as a transient tune overlay).
        """
        if not self._profile.mode_set_leaves_memory:
            return
        resp = await self.query("VM")
        if resp and resp[2:3] == "1":
            logger.debug("%s: leaving memory mode before the mode set",
                         self._profile.display_name)
            await self.set("VM000")

    async def set_mode(self, mode_num: int) -> bool:
        """Set the operating mode from its numeric register.

        ``mode_num`` is the profile register — the same int `RadioState.mode`
        and the UI carry.  The CAT character comes from `profile.mode_codes`
        rather than from `f"{mode_num:X}"`, because the FTX-1 uses H and I
        for its C4FM variants and those are not hex digits: formatting the
        register would send `MD011` instead of `MD0I`.
        """
        code = self._profile.mode_codes.get(int(mode_num))
        if code is None:
            logger.warning("%s: mode register 0x%X is not in the profile — "
                           "no MD sent", self._profile.display_name, mode_num)
            return False
        await self._leave_memory_mode_if_needed()
        return await self.set(f"MD0{code}")

    async def get_mode(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("MD0", timeout=timeout)
        if resp and len(resp) >= 4:
            char = resp[3].upper()
            for num, code in self._profile.mode_codes.items():
                if code == char:
                    return num
            logger.debug("%s: mode character %r is not in the profile",
                         self._profile.display_name, char)
        return None

    # ── Filter width (slot index, not Hz) ───────────────────────────

    async def set_filter_width(self, index: int) -> bool:
        """`SH00NN;` with the radio's own filter slot number (2 digits)."""
        return await self.set(f"SH00{index:02d}")

    async def get_filter_width(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("SH0", timeout=timeout)
        if resp and len(resp) >= 4:
            try:
                return int(resp[-2:])
            except ValueError:
                return None
        return None

    # ── Transmit (latency-critical: priority path) ──────────────────

    async def set_ptt(self, tx: bool) -> bool:
        return await self.send_priority_set_command("TX1" if tx else "TX0")

    async def set_tune(self, tune: bool) -> bool:
        if self._profile.tune_via == "atu":
            return await self.send_priority_set_command("AC1" if tune else "AC0")
        return await self.send_priority_set_command("TX2" if tune else "TX0")

    async def get_ptt(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("TX", timeout=timeout)
        if resp and len(resp) >= 3:
            try:
                return int(resp[2:])
            except ValueError:
                return None
        return None

    # ── Meters ──────────────────────────────────────────────────────

    async def get_s_meter(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("SM0", timeout=timeout)
        if resp and len(resp) >= 4:
            try:
                return int(resp[3:])
            except ValueError:
                return None
        return None

    async def get_meter(self, meter: str, timeout: Optional[float] = None) -> Optional[int]:
        """Read a raw meter value (`RM3`..`RM8`, `RM0` for the S-meter).

        The answer is "RM" + meter-id + 6 digits; the meaningful 0..255 raw
        value is the FIRST three of those six, as the FT-710 implementation
        documents (the rest is zero padding).
        """
        resp = await self.query(meter, timeout=timeout)
        if resp and len(resp) >= 6:
            try:
                return int(resp[3:6])
            except ValueError:
                return None
        return None

    # ── Gains / DSP / RF controls ───────────────────────────────────

    async def _get_int(self, cmd: str, offset: int,
                       timeout: Optional[float] = None) -> Optional[int]:
        """Read a numeric answer field starting at ``offset``.

        Shared by the polled readers: the poll tiers and
        `RadioState.from_sync_result` require PARSED values, so a raw answer
        string must never be returned from here.
        """
        resp = await self.query(cmd, timeout=timeout)
        if resp and len(resp) > offset:
            try:
                return int(resp[offset:])
            except ValueError:
                return None
        return None

    async def get_af_gain(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._get_int("AG0", 3, timeout)

    async def get_rf_gain(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._get_int("RG0", 3, timeout)

    async def get_rf_power(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._get_int("PC", 2, timeout)

    async def set_af_gain(self, value: int) -> bool:
        return await self.set(f"AG0{value:03d}")

    async def set_rf_gain(self, value: int) -> bool:
        return await self.set(f"RG0{value:03d}")

    async def set_squelch(self, value: int) -> bool:
        return await self.set(f"SQ0{value:03d}")

    async def set_mic_gain(self, value: int) -> bool:
        # Family form is "MG P1 P2 P2 P2" with P1=0 — the same shape
        # AG/RG/SQ use. (The verified FT-710 path sends "MG{value:03d}"
        # without the selector; that discrepancy is flagged for the
        # field diagnostic and not changed here.)
        return await self.set(f"MG0{value:03d}")

    async def set_preamp(self, value: int) -> bool:
        return await self.set(f"PA0{value}")

    async def set_attenuator(self, value: int) -> bool:
        return await self.set(f"RA0{value}")

    async def set_noise_blanker(self, on: bool) -> bool:
        return await self.set(f"NB0{'1' if on else '0'}")

    async def set_nb_level(self, level: int) -> bool:
        return await self.set(f"NL0{level:03d}")

    async def set_noise_reduction(self, on: bool) -> bool:
        return await self.set(f"NR0{'1' if on else '0'}")

    async def set_nr_level(self, level: int) -> bool:
        return await self.set(f"RL0{level:03d}")

    async def set_auto_notch(self, on: bool) -> bool:
        return await self.set(f"BC0{'1' if on else '0'}")

    async def set_compressor(self, on: bool) -> bool:
        return await self.set(f"PR0{'1' if on else '0'}")

    async def set_compressor_level(self, level: int) -> bool:
        return await self.set(f"PL0{level:03d}")

    async def set_agc(self, value: int) -> bool:
        return await self.set(f"GT0{value}")

    async def get_agc(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("GT0", timeout=timeout)
        return int(resp[3]) if resp and len(resp) >= 4 and resp[3].isdigit() else None

    async def set_vox(self, on: bool) -> bool:
        return await self.set(f"VX0{'1' if on else '0'}")

    async def set_break_in(self, on: bool) -> bool:
        return await self.set(f"BI0{'1' if on else '0'}")

    async def set_key_speed(self, speed: int) -> bool:
        return await self.set(f"KS{speed:03d}")

    async def set_cw_pitch(self, pitch: int) -> bool:
        return await self.set(f"KP{pitch:03d}")

    async def set_rit(self, on: bool) -> bool:
        return await self.set(f"RT0{'1' if on else '0'}")

    async def set_rit_freq(self, value: int) -> bool:
        sign = "+" if value >= 0 else "-"
        return await self.set(f"RU{sign}{abs(value):04d}")

    async def set_xit(self, on: bool) -> bool:
        return await self.set(f"XT0{'1' if on else '0'}")

    async def set_split(self, on: bool) -> bool:
        return await self.set(f"ST0{'1' if on else '0'}")

    async def set_power(self, on: bool) -> bool:
        return await self.set(f"PS{'1' if on else '0'}")

    async def set_antenna(self, ant: int) -> bool:
        return await self.set(f"AN{ant}")

    async def get_antenna(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("AN", timeout=timeout)
        return int(resp[2]) if resp and len(resp) >= 3 and resp[2].isdigit() else None

    async def set_tuner(self, value: int) -> bool:
        """0 = tuner off, 1 = tune cycle (`AC` = antenna tuner control)."""
        return await self.set(f"AC{value}")

    async def set_amc_level(self, level: int) -> bool:
        return await self.set(f"AO0{level:03d}")

    async def get_amc_level(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("AO0", timeout=timeout)
        if resp and len(resp) >= 4:
            try:
                return int(resp[3:])
            except ValueError:
                return None
        return None

    async def set_monitor(self, on: bool) -> bool:
        return await self.set(f"ML0{'1' if on else '0'}")

    async def set_monitor_gain(self, value: int) -> bool:
        return await self.set(f"ML0{value:03d}")

    # ── Power (family PC format; FTX-1 self-detects its configuration) ──

    async def detect_power_config(self, timeout: Optional[float] = None) -> tuple:
        """Read `PC;` and classify the FTX-1 head/amplifier configuration.

        `ftx1/ftx1_readme.txt`: Field head (battery 0.5-6 W, 12 V 0.5-10 W)
        answers in the `PC1xxx` shape, the SPA-1/Optima 100 W configuration in
        `PC2xxx`.  Models with a fixed configuration skip the probe and report
        their profile value.  TODO(hw-verify): the exact answer strings have
        not been observed on hardware yet (spec §10).
        """
        if self._profile.power_format != "auto":
            # Fixed configuration: no probe, but record it so
            # effective_power_max()/set_rf_power() have a value.
            self._detected_power = (self._profile.power_format,
                                    self._profile.power_max_w)
            return self._detected_power
        resp = await self.query("PC", timeout=timeout)
        if resp and len(resp) >= 3:
            try:
                raw = int(resp[2:])
            except ValueError:
                raw = None
            if raw is not None:
                # A four-digit answer (PC1xxx / PC2xxx) carries the class.
                if len(resp) >= 4 and resp[2] in "12" and len(resp[2:]) >= 4:
                    cfg = "PC" + resp[2]
                    self._detected_power = (cfg, 100 if cfg == "PC2" else 10)
                else:
                    self._detected_power = ("PC1", 10)
                logger.info("%s: power configuration %s (max %d W)",
                            self._profile.display_name, *self._detected_power)
                return self._detected_power
        self._detected_power = (self._profile.power_format if
                                self._profile.power_format != "auto" else "PC1",
                                self._profile.power_max_w)
        logger.info("%s: power configuration unknown, using the profile "
                    "maximum %d W", self._profile.display_name,
                    self._detected_power[1])
        return self._detected_power

    async def effective_power_max(self) -> int:
        """Maximum settable watts, from the detected (or profile) config."""
        if self._detected_power is None:
            await self.detect_power_config()
        return self._detected_power[1]

    async def set_rf_power(self, watts: int) -> bool:
        limit = await self.effective_power_max()
        value = max(0, min(int(watts), limit))
        if value != int(watts):
            logger.debug("%s: RF power %d W clamped to the %s maximum %d W",
                         self._profile.display_name, watts,
                         self._detected_power[0], limit)
        return await self.set(f"PC{value:03d}")

    # ── Bulk state ──────────────────────────────────────────────────

    async def initial_state_sync(self) -> dict:
        """Parsed RadioState fields for the first state push.

        Contract (`RadioState.from_sync_result`): plain
        ``{RadioState field: parsed value}`` pairs.  Raw CAT answers are not
        accepted here, and the names must be the dataclass fields —
        `vfo_a_freq`, not `frequency`.
        """
        state: dict = {}
        for field, getter in (
            ("vfo_a_freq", lambda: self.get_frequency("A")),
            ("vfo_b_freq", lambda: self.get_frequency("B")),
            ("active_vfo", lambda: self.get_active_vfo()),
            ("mode", lambda: self.get_mode()),
            ("filter_width", lambda: self.get_filter_width()),
            ("tx_status", lambda: self.get_ptt()),
            ("s_meter", lambda: self.get_s_meter()),
            ("af_gain", lambda: self.get_af_gain()),
            ("rf_gain", lambda: self.get_rf_gain()),
            ("rf_power", lambda: self.get_rf_power()),
            ("preamp", lambda: self.get_preamp()),
            ("attenuator", lambda: self.get_attenuator()),
            ("agc", lambda: self.get_agc()),
        ):
            value = await getter()
            if value is not None:
                state[field] = value
        return state
