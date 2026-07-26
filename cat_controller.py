"""
FT-710 Serial CAT Protocol Controller
=====================================
Manages the serial connection to the FT-710, formats CAT commands,
and parses responses.  All serial access is serialized through an
asyncio.Lock to prevent interleaved reads/writes.

Uses standard pyserial (synchronous) with asyncio.to_thread()
for I/O — no serial_asyncio dependency needed.
"""
import asyncio
import logging
import time
from typing import Optional

import serial
import serial.tools.list_ports

from config import SERIAL_TIMEOUT, RECONNECT_BASE_DELAY, RECONNECT_MAX_DELAY

logger = logging.getLogger("ft710.cat")


def _available_serial_ports_summary() -> str:
    try:
        ports = list(serial.tools.list_ports.comports())
    except Exception as e:
        return f"unable to list ports: {e}"
    if not ports:
        return "none"
    entries = []
    for port in ports:
        device = getattr(port, "device", "") or "?"
        description = getattr(port, "description", "") or ""
        hwid = getattr(port, "hwid", "") or ""
        details = " ".join(part for part in (description, hwid) if part)
        entries.append(f"{device} ({details})" if details else device)
    return "; ".join(entries)


class CatController:
    """Asynchronous serial CAT protocol handler for Yaesu FT-710.

    Uses pyserial (sync API) under the hood; all blocking I/O is
    offloaded to a thread pool via asyncio.to_thread().
    """

    def __init__(self, port: str, baudrate: int = 38400):
        self.port = port
        self.baudrate = baudrate
        self._ser: Optional[serial.Serial] = None
        self._lock = asyncio.Lock()
        self._timeout = SERIAL_TIMEOUT
        self._connected = False
        self._model = "Unknown"
        # Set by send_priority_set_command() to signal poll queries
        # (both in-progress _read_until threads and queued send_command
        # waiters) to abort so PTT/tune can grab the serial lock quickly.
        # Using asyncio.Event for thread-safe signaling
        self._cancel_polls: asyncio.Event = asyncio.Event()

    # ── Connection Management ──────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def model(self) -> str:
        return self._model

    async def connect(self) -> bool:
        """Open the serial port and verify the radio responds.

        Returns True if connection and radio verification succeeded.
        """
        try:
            logger.info("Opening serial port %s at %d baud", self.port, self.baudrate)

            def _open():
                s = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0.1,        # Short timeout for reads
                    write_timeout=1.0,
                    xonxoff=False,
                    rtscts=False,
                )
                return s

            self._ser = await asyncio.to_thread(_open)

            # Flush any stale data
            await asyncio.sleep(0.5)
            await asyncio.to_thread(self._ser.reset_input_buffer)

            # Mark connected early so send_set_command/query will work.
            self._connected = True

            # Disable AI (Auto Information) mode FIRST, before any query.
            # If a previous session left AI1 enabled, the radio streams IF
            # frames continuously, which makes every query (ID/FA/MD0/SM0)
            # time out waiting for a matching response.  Sending AI0 stops
            # the stream so queries work reliably.  We poll actively instead
            # of relying on AI frames.
            await self.send_set_command("AI0")
            # Give the radio a moment to stop streaming and drain any
            # in-flight IF frames from the input buffer.
            await asyncio.sleep(0.2)
            await asyncio.to_thread(self._ser.reset_input_buffer)
            logger.info("AI (Auto Information) mode disabled")

            # Verify the radio responds
            resp = await self.query("ID")
            if resp is not None and "ID" in resp:
                model_id = resp[2:] if len(resp) >= 6 else resp
                self._model = model_id
                logger.info("Connected to FT-710 (ID=%s) on %s", model_id, self.port)
            else:
                logger.warning("No ID response from radio on %s. Check connection.", self.port)
                # Stay connected — UI still works, polls will retry.
            return self._connected
        except Exception as e:
            logger.error("Failed to connect to %s: %s", self.port, e)
            logger.error("Available serial ports: %s", _available_serial_ports_summary())
            await self._cleanup()
            return False

    async def disconnect(self):
        """Close the serial port."""
        logger.info("Disconnecting from %s", self.port)
        self._connected = False
        await self._cleanup()

    async def _cleanup(self):
        if self._ser is not None:
            try:
                await asyncio.to_thread(self._ser.close)
            except Exception:
                pass
            self._ser = None

    # ── Low-level I/O ─────────────────────────────────────────────

    async def _write(self, data: bytes):
        """Write bytes to the serial port (threaded)."""
        if self._ser is None or not self._ser.is_open:
            raise serial.SerialException("Port not open")

        def _w():
            self._ser.reset_input_buffer()  # Clear stale input
            self._ser.write(data)
            self._ser.flush()

        await asyncio.to_thread(_w)
        # FT-710 CAT processor needs ~20ms between commands (matches Hamlib)
        await asyncio.sleep(0.02)

    async def _read_until(self, terminator: bytes = b";",
                          expected_prefix: str = "",
                          timeout: Optional[float] = None) -> bytes:
        """Read bytes from serial port until a matching response is found.

        If expected_prefix is given, skips any complete messages that don't
        start with the prefix (e.g. AI frames that arrived before the real
        response) and continues reading until a match or timeout.

        If timeout is given, overrides self._timeout for this call (used
        by pollers to bound lock occupancy so user commands like PTT
        aren't blocked behind a query that never responds).
        """
        if self._ser is None or not self._ser.is_open:
            raise serial.SerialException("Port not open")

        def _r():
            buf = bytearray()
            deadline = time.monotonic() + (timeout if timeout is not None else self._timeout)
            while time.monotonic() < deadline:
                # Priority command (PTT/tune) pending — abort early so
                # the serial lock is released for it as soon as possible.
                if self._cancel_polls.is_set():
                    return None
                waiting = self._ser.in_waiting
                if waiting > 0:
                    chunk = self._ser.read(waiting)
                    buf.extend(chunk)
                    if terminator in buf:
                        idx = buf.find(terminator)
                        msg = bytes(buf[:idx + len(terminator)])

                        if expected_prefix:
                            try:
                                msg_str = msg.decode(
                                    "ascii", errors="replace").rstrip(";")
                            except Exception:
                                msg_str = ""
                            if not msg_str.startswith(expected_prefix):
                                # AI frame or stale data — discard and
                                # keep looking in the remaining bytes.
                                logger.debug(
                                    "Skipping unexpected response %r "
                                    "(expected prefix %r)",
                                    msg_str[:40], expected_prefix)
                                buf = bytearray(
                                    buf[idx + len(terminator):])
                                # If there is already another complete
                                # message in buf, process it next iter.
                                continue

                        return msg
                # Only sleep if no data was waiting
                time.sleep(0.01)
            # Timeout — return whatever we have
            return bytes(buf) if buf else None

        return await asyncio.to_thread(_r)

    # ── Command Interface ──────────────────────────────────────────

    async def send_command(self, cmd: str, timeout: Optional[float] = None) -> Optional[str]:
        """Send a CAT command and return the response.

        All serial I/O is serialized through self._lock.
        Returns the response string (without trailing ';') or None on timeout/error.

        Uses the command as the expected response prefix so that AI
        (Auto Information) frames arriving ahead of the real response
        are skipped instead of being mistaken for the answer.

        Args:
            cmd: CAT command string WITHOUT trailing ';' (it's appended here).
            timeout: optional per-call read timeout (seconds).  Pollers
                pass a short value so a non-responding query releases the
                lock quickly instead of blocking user commands (PTT) for
                the full SERIAL_TIMEOUT.
        """
        # If a priority command (PTT/tune) has signalled polls to cancel,
        # don't even try to acquire the lock — let the priority command
        # grab it with minimal delay.
        if self._cancel_polls.is_set():
            return None

        await self._lock.acquire()
        result: Optional[str] = None
        try:
            # Double-check after acquiring the lock: a priority command
            # may have set _cancel_polls while we were queued waiting.
            if self._cancel_polls.is_set():
                return result

            if not self._connected or self._ser is None:
                return result

            raw = (cmd + ";").encode("ascii")
            try:
                await self._write(raw)
            except Exception as e:
                logger.error("Serial write error for '%s': %s", cmd, e)
                self._connected = False
                return result

            # Read response — skip any AI frames that don't match the
            # expected command prefix.
            try:
                response_bytes = await self._read_until(
                    b";", expected_prefix=cmd, timeout=timeout)
                if response_bytes is None:
                    logger.debug("Command timeout (no data): %s", cmd)
                    return result
                if not response_bytes:
                    logger.debug("Command timeout (empty): %s", cmd)
                    return result
                result = response_bytes.decode("ascii", errors="replace").rstrip(";")
            except Exception as e:
                logger.error("Serial read error for '%s': %s", cmd, e)
                self._connected = False
            return result
        finally:
            self._lock.release()

    async def send_set_command(self, cmd: str) -> bool:
        """Send a set command WITHOUT waiting for a response.

        FT-710 set commands often don't produce a response or produce
        a delayed response.  Waiting for one would block the serial lock
        for SERIAL_TIMEOUT (1.0s) on every user UI action, causing
        noticeable CAT stutter when tuning or adjusting settings.

        This is a write-only fire-and-forget — the serial lock is held
        only for the write duration (~1-2ms), keeping the UI responsive.
        """
        async with self._lock:
            if not self._connected or self._ser is None:
                return False

            raw = (cmd + ";").encode("ascii")
            try:
                await self._write(raw)
                return True
            except Exception as e:
                logger.error("Serial write error for '%s': %s", cmd, e)
                self._connected = False
                return False

    async def send_priority_set_command(self, cmd: str) -> bool:
        """Send a high-priority set command that preempts poll queries.

        Used for latency-sensitive commands like PTT and TUNE.  Sets
        _cancel_polls to signal in-progress poll reads (in _read_until
        threads) and queued poll send_command waiters to abort, so this
        command can grab the serial lock with minimal delay.

        Write-only (no response read) for speed — same fire-and-forget
        semantics as send_set_command.
        """
        self._cancel_polls.set()
        try:
            # Brief yield so any in-progress _read_until thread can see
            # the cancel flag and bail out, releasing the lock sooner.
            await asyncio.sleep(0.005)

            async with self._lock:
                # Clear the cancel flag now that we hold the lock.
                # Poll queries will resume once we release it.
                self._cancel_polls.clear()

                if not self._connected or self._ser is None:
                    return False

                raw = (cmd + ";").encode("ascii")
                try:
                    await self._write(raw)
                    return True
                except Exception as e:
                    logger.error("Serial write error for '%s': %s", cmd, e)
                    self._connected = False
                    return False
        finally:
            self._cancel_polls.clear()

    async def query(self, cmd: str, timeout: Optional[float] = None) -> Optional[str]:
        """Send a query command (no value, just prefix + ';').

        Example: query("FA") -> "FA014200000"
        """
        return await self.send_command(cmd, timeout=timeout)

    async def set(self, cmd: str) -> bool:
        """Send a set command (prefix + value + ';').

        Returns True if the write succeeded.  Uses send_set_command
        (write-only) for responsiveness; does NOT block on a response.

        Example: set("FA014200000") -> True
        """
        return await self.send_set_command(cmd)

    # ── High-Level Command Helpers ─────────────────────────────────

    async def set_frequency(self, freq_hz: int, vfo: str = "A") -> bool:
        prefix = "FA" if vfo.upper() == "A" else "FB"
        return await self.set(f"{prefix}{freq_hz:09d}")

    async def get_active_vfo(self, timeout: Optional[float] = None) -> Optional[str]:
        """Query which VFO is active.  Returns "A" or "B" (or None).

        FT-710 VS; response: "VS0" = VFO-A active, "VS1" = VFO-B active.
        """
        resp = await self.query("VS", timeout=timeout)
        if resp and len(resp) >= 3:
            return "B" if resp.endswith("1") else "A"
        return None

    async def get_frequency(self, vfo: str = "A", timeout: Optional[float] = None) -> Optional[int]:
        prefix = "FA" if vfo.upper() == "A" else "FB"
        resp = await self.query(prefix, timeout=timeout)
        if resp and len(resp) >= len(prefix) + 1:
            try:
                return int(resp[len(prefix):])
            except ValueError:
                return None
        return None

    async def set_mode(self, mode_num: int) -> bool:
        return await self.set(f"MD0{mode_num:X}")

    async def get_mode(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("MD0", timeout=timeout)
        if resp and len(resp) >= 4:
            try:
                return int(resp[3:], 16)
            except ValueError:
                return None
        return None

    async def set_ptt(self, tx: bool) -> bool:
        return await self.send_priority_set_command("TX1" if tx else "TX0")

    async def set_tune(self, tune: bool) -> bool:
        return await self.send_priority_set_command("TX2" if tune else "TX0")

    async def get_ptt(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("TX", timeout=timeout)
        if resp and len(resp) >= 3:
            try:
                return int(resp[2:])
            except ValueError:
                return None
        return None

    async def get_s_meter(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("SM0", timeout=timeout)
        if resp and len(resp) >= 4:
            try:
                return int(resp[3:])
            except ValueError:
                return None
        return None

    async def get_info(self) -> Optional[dict]:
        """Get combined info via IF; command."""
        resp = await self.query("IF")
        if not resp or len(resp) < 10:
            return None
        try:
            # Parse: IF + freq(9 digits or space-padded) + status + S-meter
            tail = resp[2:]  # Skip "IF"
            freq_part = ''
            i = 0
            # Frequency digits (may be space-padded)
            while i < len(tail) and (tail[i].isdigit() or tail[i] == ' '):
                freq_part += tail[i]
                i += 1
            freq_str = freq_part.strip()
            # FT-710 IF returns Hz directly (9 digits), NOT tens-of-Hz.
            # Older Yaesu models use 11-digit tens-of-Hz; FT-710 uses 9-digit Hz.
            freq_hz = int(freq_str) if freq_str else None

            # After frequency: mode byte + misc
            rest = tail[i:]
            mode_num = None
            if len(rest) > 0 and rest[0].isdigit():
                mode_num = int(rest[0], 16) if rest[0] in '0123456789ABCDEFabcdef' else None

            # S-meter: find 3 consecutive digits near the end
            smeter = None
            digits = ''.join(c for c in rest if c.isdigit())
            if len(digits) >= 3:
                smeter = int(digits[-3:])

            result = {}
            if freq_hz is not None:
                result["freq"] = freq_hz
            if mode_num is not None:
                result["mode"] = mode_num
            if smeter is not None:
                result["s_meter"] = smeter
            return result if result else None
        except (ValueError, IndexError) as e:
            logger.debug("IF parse error: %s (raw: %s)", e, resp)
            return None

    async def get_meter(self, meter: str, timeout: Optional[float] = None) -> Optional[int]:
        """Read a raw meter value (RM3..RM8).  Returns the raw 0-255 value.

        FT-710 RM response format is 9 chars: "RM" + meter_id + 6 digits,
        e.g. "RM5150000" for Power raw=150.  The meaningful raw value
        (0-255, per FT-710.rig Min=0/Max=255) is the FIRST 3 of those 6
        digits; the trailing 3 digits are zero padding.  (wfview reads
        Bytes=6 = prefix(3)+value(3) and ignores the extra padding.)
        Previously this used resp[4:] which dropped the high digit and
        returned garbage for any value whose top digit was non-zero.
        """
        resp = await self.query(meter, timeout=timeout)
        if resp and len(resp) >= 6:
            try:
                val = int(resp[3:6])
                logger.debug("get_meter(%s): raw=%r → val=%d", meter, resp, val)
                return val
            except ValueError:
                logger.warning("get_meter(%s): unparseable response %r", meter, resp)
                return None
        logger.debug("get_meter(%s): no/bad response %r", meter, resp)
        return None

    async def set_filter_width(self, index: int) -> bool:
        # FT-710 SH (WIDTH) command format per CAT manual:
        #   SH + P1(0) + P2(0) + 2-digit width index  =>  SH00NN
        # (e.g. SH0005 for filter 5, SH0023 for filter 23).
        cmd = f"SH00{index:02d}"
        return await self.set(cmd)

    async def get_filter_width(self) -> Optional[int]:
        resp = await self.query("SH0")
        # Response is "SH00NN" (P1=0, P2=0, P3=2-digit index); take last 2 digits.
        if resp and len(resp) >= 4:
            try:
                return int(resp[-2:])
            except ValueError:
                return None
        return None

    async def set_af_gain(self, value: int) -> bool:
        return await self.set(f"AG0{value:03d}")

    async def set_rf_gain(self, value: int) -> bool:
        return await self.set(f"RG0{value:03d}")

    async def set_rf_power(self, value: int) -> bool:
        return await self.set(f"PC{value:03d}")

    async def set_preamp(self, value: int) -> bool:
        return await self.set(f"PA0{value}")

    async def set_attenuator(self, value: int) -> bool:
        return await self.set(f"RA0{value}")

    async def set_noise_blanker(self, on: bool) -> bool:
        return await self.set(f"NB0{1 if on else 0}")

    async def set_noise_reduction(self, on: bool) -> bool:
        return await self.set(f"NR0{1 if on else 0}")

    async def set_auto_notch(self, on: bool) -> bool:
        return await self.set(f"BC0{1 if on else 0}")

    async def set_compressor(self, on: bool) -> bool:
        # FT-710 PR command: P1=0 (Speech Processor), P2=0=OFF, P2=1=ON.
        # NOTE: The Yaesu CAT PDF says P2=1=OFF/2=ON, but this contradicts
        # every other binary command (NB/NR/BC/NA/BI/VX all use 0=OFF/1=ON)
        # AND empirical testing confirms the radio uses 0=OFF, 1=ON.
        # The PDF is a known errata — sending PR02 kills TX audio.
        return await self.set(f"PR0{1 if on else 0}")

    async def set_tuner(self, value: int) -> bool:
        # AC P1P2P3; P1=0, P2=0(standard tuner), P3=0(OFF)/1(ON)/3(Tuning)
        # P2=1 is invalid for standard tuner per the CAT spec.
        tuner_map = {0: "000", 1: "001", 2: "003"}
        v = tuner_map.get(value, "000")
        return await self.set(f"AC{v}")

    async def set_vfo(self, vfo: str) -> bool:
        cmd = "VS0" if vfo.upper() == "A" else "VS1"
        return await self.set(cmd)

    async def set_split(self, on: bool) -> bool:
        return await self.set(f"ST{1 if on else 0}")

    async def set_power(self, on: bool) -> bool:
        return await self.set(f"PS{1 if on else 0}")

    async def set_squelch(self, value: int) -> bool:
        return await self.set(f"SQ0{value:03d}")

    async def set_mic_gain(self, value: int) -> bool:
        return await self.set(f"MG{value:03d}")

    async def set_band_stack(self, bsr: int) -> bool:
        return await self.set(f"BS{bsr:02d}")

    async def set_scope_on(self, on: bool) -> bool:
        """Set scope display on/off (SS01)."""
        return await self.set(f"SS01{1 if on else 0}")

    async def get_scope_on(self) -> Optional[int]:
        resp = await self.query("SS01")
        if resp and len(resp) >= 5:
            try:
                return int(resp[4:])
            except ValueError:
                return None
        return None

    async def set_antenna(self, ant: int) -> bool:
        """Set antenna select 1-3 (AN)."""
        return await self.set(f"AN{ant}")

    async def get_antenna(self) -> Optional[int]:
        resp = await self.query("AN")
        if resp and len(resp) >= 3:
            try:
                return int(resp[2:])
            except ValueError:
                return None
        return None

    async def set_agc(self, value: int) -> bool:
        """Set AGC time constant 0-3 (GT)."""
        return await self.set(f"GT{value:02d}")

    async def get_agc(self) -> Optional[int]:
        resp = await self.query("GT")
        if resp and len(resp) >= 4:
            try:
                return int(resp[2:])
            except ValueError:
                return None
        return None

    async def set_dnr(self, value: int) -> bool:
        """DNR level set is NOT supported on the FT-710.

        The "DN" command is the active-VFO step-DOWN command, not DNR.
        Sending "DN0XX;" is rejected by the radio (returns "?").  This
        method is a no-op kept only for API compatibility.
        """
        logger.warning("set_dnr ignored: 'DN' is step-down on FT-710, not DNR")
        return False

    async def get_dnr(self) -> Optional[int]:
        """DNR level query is NOT supported on the FT-710.

        NEVER send "DN;" to query — it steps the active VFO down ~20 Hz.
        """
        return None

    async def set_contour(self, value: int) -> bool:
        """Set Contour level 0-255 (CO)."""
        return await self.set(f"CO{value:03d}")

    async def get_contour(self) -> Optional[int]:
        resp = await self.query("CO")
        if resp and len(resp) >= 5:
            try:
                return int(resp[2:5])
            except ValueError:
                return None
        return None

    async def set_drive(self, value: int) -> bool:
        """Set drive level — maps to RF Power (PC) on FT-710. 5-100W."""
        return await self.set_rf_power(value)

    # ── Meter & Radio Info Commands ─────────────────────────────────

    async def set_meter_display(self, meter: int) -> bool:
        """Set which meter is displayed on the radio (MS command).

        Args:
            meter: 0=PO, 1=COMP, 2=ALC, 3=VDD, 4=ID, 5=SWR
        """
        if meter < 0 or meter > 5:
            return False
        return await self.set(f"MS{meter}0")

    async def get_meter_display(self, timeout: Optional[float] = None) -> Optional[int]:
        """Query which meter is displayed (MS;)."""
        resp = await self.query("MS", timeout=timeout)
        if resp and len(resp) >= 4:
            try:
                return int(resp[2])
            except ValueError:
                return None
        return None

    async def set_amc_level(self, level: int) -> bool:
        """Set AMC output level (AO command).  Range: 1-100."""
        if level < 1 or level > 100:
            return False
        return await self.set(f"AO{level:03d}")

    async def get_amc_level(self, timeout: Optional[float] = None) -> Optional[int]:
        """Query AMC output level (AO;)."""
        resp = await self.query("AO", timeout=timeout)
        if resp and len(resp) >= 5:
            try:
                return int(resp[2:5])
            except ValueError:
                return None
        return None

    async def get_radio_info(self, timeout: Optional[float] = None) -> Optional[dict]:
        """Read radio information via RI; command.

        Returns dict with keys: hi_swr, recording_status, rx_tx_status,
        tuner_tuning, scan_status, squelch_open.
        """
        resp = await self.query("RI0", timeout=timeout)
        if not resp or len(resp) < 10:
            return None
        try:
            # RI0 + P2 + P3 + P4 + P5 + P6 + P7 + P8 (each 1 char)
            # resp format: "RI0" + 7 single-char fields
            tail = resp[3:]  # Skip "RI0"
            if len(tail) < 7:
                return None
            return {
                "hi_swr": tail[0] == '1',          # P2
                "recording_status": int(tail[1]) if tail[1].isdigit() else 0,  # P3
                "rx_tx_status": int(tail[2]) if tail[2].isdigit() else 0,      # P4
                # P5 is fixed 0, skip
                "tuner_tuning": tail[4] == '1',     # P6
                "scan_status": int(tail[5]) if tail[5].isdigit() else 0,       # P7
                "squelch_open": tail[6] == '1',     # P8
            }
        except (ValueError, IndexError) as e:
            logger.debug("RI parse error: %s (raw: %s)", e, resp)
            return None

    # ── Scope/Spectrum Commands ────────────────────────────────────

    async def set_scope_span(self, span: int) -> bool:
        """Set scope span (0-9, see SCOPE_SPANS)."""
        return await self.set(f"SS05{span:02d}")

    async def set_scope_speed(self, speed: int) -> bool:
        """Set scope sweep speed (0-5)."""
        return await self.set(f"SS00{speed:02d}")

    async def set_scope_mode(self, mode: int) -> bool:
        """Set scope display mode (0-9)."""
        return await self.set(f"SS06{mode:02d}")

    async def set_nb_level(self, level: int) -> bool:
        """Set noise blanker level (0-10)."""
        return await self.set(f"NL0{level:02d}")

    async def set_nr_level(self, level: int) -> bool:
        """Set noise reduction level (1-15)."""
        return await self.set(f"RL{level:02d}")

    async def set_compressor_level(self, level: int) -> bool:
        """Set compressor level (1-100)."""
        return await self.set(f"PL{level:03d}")

    async def set_monitor(self, on: bool) -> bool:
        """Set monitor on/off."""
        return await self.set(f"ML0{1 if on else 0}")

    async def set_monitor_gain(self, value: int) -> bool:
        """Set monitor gain (0-100)."""
        return await self.set(f"ML1{value:03d}")

    async def set_vox(self, on: bool) -> bool:
        """Set VOX on/off."""
        return await self.set(f"VX{1 if on else 0}")

    async def set_break_in(self, on: bool) -> bool:
        """Set break-in on/off."""
        return await self.set(f"BI{1 if on else 0}")

    async def set_key_speed(self, speed: int) -> bool:
        """Set CW key speed (0-60)."""
        return await self.set(f"KS{speed:03d}")

    async def set_cw_pitch(self, pitch: int) -> bool:
        """Set CW pitch (0-75)."""
        return await self.set(f"KP{pitch:02d}")

    async def set_rit(self, on: bool) -> bool:
        """Set RIT on/off."""
        return await self.set(f"CF000{1 if on else 0:01d}")

    async def set_rit_freq(self, freq: int) -> bool:
        """Set RIT offset (-9999 to 9999 Hz)."""
        sign = '+' if freq >= 0 else '-'
        return await self.set(f"CF001{sign}{abs(freq):04d}")

    async def set_xit(self, on: bool) -> bool:
        """Set XIT on/off."""
        return await self.set(f"XT{1 if on else 0}")

    # ── Bulk State Query ──────────────────────────────────────────

    async def initial_state_sync(self) -> dict:
        """Perform a full state read after connection."""
        state = {}
        queries = [
            ("vfo_a_freq", "FA"),
            ("vfo_b_freq", "FB"),
            ("active_vfo", "VS"),
            ("mode", "MD0"),
            ("tx_status", "TX"),
            ("s_meter", "SM0"),
            ("filter_width", "SH0"),
            ("af_gain_raw", "AG"),
            ("rf_power", "PC"),
            ("preamp", "PA0"),
            ("attenuator", "RA0"),
            ("noise_blanker", "NB0"),
            ("noise_reduction", "NR0"),
            ("auto_notch", "BC"),
            ("tuner_status", "AC"),
            ("power_on", "PS"),
            ("scope_on", "SS01"),
            ("antenna", "AN"),
            ("agc", "GT"),
            # "DN" intentionally omitted — on the FT-710 "DN;" is the
            # active-VFO step-DOWN command, not a DNR query.
            ("contour_level", "CO"),
            ("meter_display", "MS"),
            ("amc_level", "AO"),
            ("ri", "RI0"),       # Radio Information (Hi-SWR, etc.)
            ("rf_gain", "RG0"),  # RF Gain (was missing from initial sync)
        ]
        for field, cmd in queries:
            resp = await self.query(cmd)
            if resp:
                state[field] = resp
            # Reduced sleep time from 50ms to 20ms for faster initial sync
            await asyncio.sleep(0.02)
        return state

    # ── Reconnect ──────────────────────────────────────────────────

    async def reconnect_loop(self) -> bool:
        """Attempt reconnection with exponential backoff."""
        delay = RECONNECT_BASE_DELAY
        while True:
            logger.info("Attempting reconnect to %s (delay=%.1fs)...", self.port, delay)
            if await self.connect():
                logger.info("Reconnected to %s", self.port)
                return True
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)
