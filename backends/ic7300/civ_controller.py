"""
Icom IC-7300 CI-V Controller
============================
Async CI-V protocol handler for the IC-7300 (and IC-7300MK2), the
Icom counterpart of ``backends/ft710/cat_controller.py``.

Unlike the Yaesu CAT port (one ASCII request → one ASCII response), the
IC-7300's single CI-V serial stream carries THREE kinds of traffic at
once:

1. responses to our commands (addressed to the controller, 0xE0),
2. transceive broadcasts (to==0x00: cmd 0x00 freq / cmd 0x01 mode),
3. the 0x27 0x00 scope-waveform segment stream,
4. plus bus echoes of our own transmissions.

Architecture: one daemon thread performs blocking ``ser.read()`` and
hands chunks to the asyncio loop via ``loop.call_soon_threadsafe``;
``_on_bytes`` feeds the incremental ``CivFrameParser`` and demuxes each
frame to the echo drop, the scope queue, the broadcast callback, or a
pending request future (keyed by command + sub-command byte).  All
writes are serialized through one asyncio.Lock; queries hold the lock
until their response arrives (timeout → one retry → CivTimeoutError),
set commands are fire-and-forget like the FT-710's ``send_set_command``.

CI-V sub-command bytes verified against wfview ``rigs/IC-7300.rig``
(github mirror eliggett/wfview) and hamlib ``rigs/icom/ic7300.c`` —
see the per-command comments.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from typing import Callable, Optional

import serial

from backends.ic7300.civ_codec import (
    CivFrame, CivFrameParser, build_frame,
    encode_freq_bcd, decode_freq_bcd,
    encode_level_bcd, decode_level_bcd,
    is_echo, parse_scope_segment,
    CONTROLLER_ADDR, SCOPE_CMD, SCOPE_SUB_DATA,
)
from backends.ic7300.config_ic7300 import (
    CIV_ADDR, CIV_BAUD_RATE, SCOPE_SPAN_HZ,
    METER_SUB_S, METER_SUB_PO, METER_SUB_SWR, METER_SUB_ALC,
    METER_SUB_COMP,
)
from config import RECONNECT_BASE_DELAY, RECONNECT_MAX_DELAY

logger = logging.getLogger("ic7300.civ")

# ── CI-V command / sub-command bytes (all verified — see class docstring) ──
CMD_FREQ_BCAST = 0x00       # transceive broadcast: operating frequency
CMD_MODE_BCAST = 0x01       # transceive broadcast: operating mode (+FIL)
CMD_READ_FREQ = 0x03
CMD_READ_MODE = 0x04        # response data: mode byte + FIL byte (1-3)
CMD_SET_FREQ = 0x05
CMD_SET_MODE = 0x06         # data: mode byte + FIL byte
CMD_VFO = 0x07              # 00=A 01=B A0=equalize B0=swap
CMD_SPLIT = 0x0F            # data 00/01
CMD_ATT = 0x11              # data = attenuation dB (00 off / 20 on)
CMD_LEVEL = 0x14            # levels; first data byte = level selector
CMD_METER = 0x15            # meters; first data byte = meter selector
CMD_SWITCH = 0x16           # on/off switches; first data byte = selector
CMD_POWER = 0x18            # data 01=on / 00=off
CMD_SET_MODE_ITEM = 0x1A    # set-mode area; sub 0x05 = parameter items
CMD_TX = 0x1C               # sub 0x00 PTT (00/01), sub 0x01 tuner (00/01/02)
CMD_UNSEL_FREQ = 0x25       # sub 0x00 selected / 0x01 unselected freq
CMD_SCOPE = 0x27            # scope commands (see SCOPE_SUB_* below)

# 0x14 level selectors (wfview IC-7300.rig Commands\20-35)
LVL_AF = 0x01
LVL_RF_GAIN = 0x02
LVL_SQL = 0x03
LVL_NR = 0x06               # NR level — NOT 0x14 (spec guess corrected)
LVL_RF_POWER = 0x0A
LVL_MIC = 0x0B
LVL_COMP = 0x0E             # compressor level
LVL_NB = 0x12

# 0x16 switch selectors (wfview IC-7300.rig Commands\47-61)
SW_PREAMP = 0x02            # data 0=OFF 1=AMP1 2=AMP2
SW_AGC = 0x12               # data 0=OFF 1=FAST 2=MID 3=SLOW (hamlib agc_levels)
SW_NB = 0x22
SW_NR = 0x40
SW_COMP = 0x44

# 0x1C sub-commands
SUB_TX_PTT = 0x00
SUB_TX_TUNER = 0x01         # data 00=off 01=on 02=start tuning

# 0x27 scope sub-commands (wfview IC-7300.rig Commands\167-181; Icom
# CI-V reference pp. 9-10).  NOTE: 0x10 is the scope DISPLAY on/off,
# 0x11 is the scope DATA OUTPUT on/off (Phase 2b spec had them merged).
SCOPE_SUB_DISPLAY = 0x10
SCOPE_SUB_DATA_OUT = 0x11
SCOPE_SUB_MODE = 0x14       # data 0=center 1=fixed 2=scroll-c 3=scroll-f
SCOPE_SUB_SPAN = 0x15       # data: 5-byte BCD half-span in Hz
SCOPE_SUB_SPEED = 0x1A      # data 0=fast 1=mid 2=slow

# Set-mode item for "CI-V Transceive" ON at connect:
# 1A 05 <item 00 71> <value 01>.  Item 0071 per hamlib ic7300.c
# (RIG_FUNC_TRANSCEIVE extcmd {0x00, 0x71}) and wfview IC-7300.rig
# Commands\95 — Phase 2a research said item 0048, which both sources
# contradict.  (IC-7300MK2 uses item 0089; we alias the MK2 to this
# backend, so its broadcast enable may need hardware verification.)
# TODO(hw-verify)
SETMODE_CIV_TRANSCEIVE_ON = bytes((CMD_SET_MODE_ITEM, 0x05, 0x00, 0x71, 0x01))

# Commands whose response is identified by cmd + first data byte.
_SUBKEYED_CMDS = frozenset((
    CMD_LEVEL, CMD_METER, CMD_SWITCH, CMD_SET_MODE_ITEM, CMD_TX,
    CMD_UNSEL_FREQ, CMD_SCOPE,
))

_DEFAULT_QUERY_TIMEOUT = 0.3    # per-attempt response timeout (seconds)
_READ_CHUNK = 256               # reader-thread ser.read() size


class CivTimeoutError(Exception):
    """No response to a CI-V query after the initial attempt + one retry."""


class CivNakError(Exception):
    """The radio answered a set command with NG (0xFA)."""


def _pending_key(command: int, data: bytes) -> tuple:
    """Pending-request key: (cmd, sub) for sub-keyed families, else (cmd,)."""
    if command in _SUBKEYED_CMDS and data:
        return (command, data[0])
    return (command,)


class CivController:
    """Asynchronous CI-V protocol handler for the Icom IC-7300.

    Same public semantics as the FT-710 ``CatController`` (three command
    priority tiers, ``connected`` flag, exponential-backoff reconnect),
    but with a single shared reader demuxing responses / transceive
    broadcasts / scope segments.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = CIV_BAUD_RATE,
        civ_addr: int = CIV_ADDR,
        query_timeout: float = _DEFAULT_QUERY_TIMEOUT,
    ):
        self.port = port
        self.baudrate = baudrate
        self.civ_addr = civ_addr
        self.query_timeout = query_timeout
        self._ser: Optional[serial.Serial] = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._model = "IC-7300"
        # Priority preemption flag — same semantics as CatController:
        # set by send_priority_set_command (PTT/tune) so queued/in-flight
        # poll queries yield the serial lock as soon as possible.
        self._cancel_polls: asyncio.Event = asyncio.Event()

        # ── Reader / demux state ────────────────────────────────────
        self._parser = CivFrameParser()
        self._last_sent: bytes = b""
        self._pending: dict[tuple, deque[asyncio.Future]] = {}
        self._pending_acks: deque[asyncio.Future] = deque()
        # Parsed 0x27 0x00 scope segments (consumed by the Phase 3
        # scope producer; unbounded — segments are tiny).
        self.scope_queue: asyncio.Queue = asyncio.Queue()
        self._broadcast_cb: Optional[Callable[[str, object], None]] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_running = False
        # Last FIL selection (1-3) seen in a mode response; mode-sets
        # re-send it so changing mode keeps the selected IF filter.
        self._fil = 1

    # ── Connection Management ──────────────────────────────────────

    @staticmethod
    def _is_device_fatal(exc: Exception) -> bool:
        """True only for device-level failures; False for transient I/O.

        Mirrors CatController._is_device_fatal: only real device loss
        (unplug, port gone) may latch connected=False — transient write
        contention must not flap the UI's "radio disconnected" state.
        """
        if isinstance(exc, serial.SerialTimeoutException):
            return False
        if isinstance(exc, serial.SerialException):
            return True
        if isinstance(exc, OSError):
            # errno 6 = ENXIO "Device not configured", 19 = ENODEV
            return exc.errno in (6, 19)
        return True

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def model(self) -> str:
        return self._model

    async def connect(self) -> bool:
        """Open the serial port, start the reader, enable CI-V transceive."""
        try:
            logger.info("Opening CI-V port %s at %d baud", self.port, self.baudrate)

            def _open():
                return serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0.1,        # reader-thread read quantum
                    write_timeout=1.0,
                    xonxoff=False,
                    rtscts=False,
                )

            self._ser = await asyncio.to_thread(_open)
            await asyncio.to_thread(self._ser.reset_input_buffer)
            self._parser = CivFrameParser()
            self._loop = asyncio.get_running_loop()
            self._connected = True
            self._start_reader()

            # Enable CI-V transceive so front-panel knob/mode changes are
            # broadcast (cmd 0x00/0x01) without polling.
            await self.send_set_command(SETMODE_CIV_TRANSCEIVE_ON)
            logger.info("Connected to IC-7300 (CI-V 0x%02X) on %s",
                        self.civ_addr, self.port)
            return True
        except Exception as e:
            logger.error("Failed to connect to %s: %s", self.port, e)
            await self._cleanup()
            return False

    async def disconnect(self) -> None:
        """Stop the reader and close the serial port."""
        logger.info("Disconnecting from %s", self.port)
        self._connected = False
        await self._cleanup()

    async def _cleanup(self):
        self._reader_running = False
        if self._reader_thread is not None:
            await asyncio.to_thread(self._reader_thread.join, 1.0)
            self._reader_thread = None
        if self._ser is not None:
            try:
                await asyncio.to_thread(self._ser.close)
            except Exception:
                pass
            self._ser = None
        self._fail_all_pending(CivTimeoutError("connection closed"))

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

    # ── Reader thread & frame demux ────────────────────────────────

    def _start_reader(self) -> None:
        self._reader_running = True
        self._reader_thread = threading.Thread(
            target=self._reader_main, name="civ-reader", daemon=True)
        self._reader_thread.start()

    def _reader_main(self) -> None:
        """Blocking serial reader; hands chunks to the asyncio loop."""
        while self._reader_running:
            ser = self._ser
            if ser is None:
                return
            try:
                chunk = ser.read(_READ_CHUNK)
            except Exception as e:
                if not self._reader_running:
                    return
                logger.error("CI-V serial read error: %s", e)
                if self._is_device_fatal(e) and self._loop is not None:
                    self._loop.call_soon_threadsafe(self._on_device_lost)
                return
            if chunk and self._loop is not None:
                try:
                    self._loop.call_soon_threadsafe(self._on_bytes, chunk)
                except RuntimeError:
                    return  # loop closed (shutdown)

    def _on_device_lost(self) -> None:
        """Handle a fatal device error (runs on the asyncio loop)."""
        self._connected = False
        self._fail_all_pending(CivTimeoutError("device lost"))

    def _fail_all_pending(self, exc: Exception) -> None:
        for dq in self._pending.values():
            while dq:
                fut = dq.popleft()
                if not fut.done():
                    fut.set_exception(exc)
        self._pending.clear()
        while self._pending_acks:
            fut = self._pending_acks.popleft()
            if not fut.done():
                fut.set_exception(exc)

    def _on_bytes(self, data: bytes) -> None:
        """Feed the parser and demux complete frames (asyncio loop)."""
        for frame in self._parser.feed(data):
            self._demux(frame)

    def _demux(self, frame: CivFrame) -> None:
        """Route one parsed CI-V frame to its consumer."""
        # 1. Our own traffic echoed on the bus (from == controller).
        if frame.from_addr == CONTROLLER_ADDR:
            if not is_echo(frame, self._last_sent):
                logger.debug("dropping own non-echo frame: %s", bytes(frame).hex())
            return
        # 2. Scope waveform segment stream.
        if frame.command == SCOPE_CMD and frame.data[:1] == bytes((SCOPE_SUB_DATA,)):
            seg = parse_scope_segment(frame)
            if seg is not None:
                self.scope_queue.put_nowait(seg)
            return
        # 3. Transceive broadcasts (to==0x00) and freq/mode broadcasts.
        if frame.to == 0x00 or frame.command in (CMD_FREQ_BCAST, CMD_MODE_BCAST):
            self._handle_broadcast(frame)
            return
        # 4. OK / NG acknowledgements resolve set-with-ack futures.
        if frame.command == 0xFB or frame.command == 0xFA:  # OK / NG
            if self._pending_acks:
                fut = self._pending_acks.popleft()
                if not fut.done():
                    if frame.command == 0xFB:
                        fut.set_result(True)
                    else:
                        fut.set_exception(CivNakError("radio rejected command (NG)"))
            else:
                logger.debug("unmatched %s", "OK" if frame.command == 0xFB else "NG")
            return
        # 5. Command responses: match against pending requests (FIFO).
        key = _pending_key(frame.command, frame.data)
        dq = self._pending.get(key)
        if dq:
            fut = dq.popleft()
            if not dq:
                del self._pending[key]
            if not fut.done():
                fut.set_result(frame)
            return
        logger.debug("unmatched CI-V frame dropped: %s", bytes(frame).hex())

    def _handle_broadcast(self, frame: CivFrame) -> None:
        """Parse a transceive broadcast and notify the state layer."""
        if self._broadcast_cb is None:
            return
        try:
            if frame.command == CMD_FREQ_BCAST and frame.data:
                self._broadcast_cb("vfo_a_freq", decode_freq_bcd(frame.data))
            elif frame.command == CMD_MODE_BCAST and len(frame.data) >= 1:
                if len(frame.data) >= 2 and 1 <= frame.data[1] <= 3:
                    self._fil = frame.data[1]
                    self._broadcast_cb("filter_width", frame.data[1])
                self._broadcast_cb("mode", frame.data[0])
        except ValueError as e:
            logger.debug("broadcast parse error: %s", e)

    def set_broadcast_callback(self, cb: Optional[Callable[[str, object], None]]) -> None:
        """Register cb(field, value) for transceive (0x00/0x01) frames."""
        self._broadcast_cb = cb

    # ── Low-level I/O ──────────────────────────────────────────────

    async def _write(self, data: bytes) -> None:
        """Write a raw frame (threaded) and remember it for echo drop."""
        if self._ser is None or not self._ser.is_open:
            raise serial.SerialException("Port not open")

        def _w():
            self._ser.write(data)
            self._ser.flush()

        await asyncio.to_thread(_w)
        self._last_sent = data
        # No Yaesu-style inter-command delay needed on CI-V.

    def _register_pending(self, key: tuple) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending.setdefault(key, deque()).append(fut)
        return fut

    def _unregister_pending(self, key: tuple, fut: asyncio.Future) -> None:
        dq = self._pending.get(key)
        if dq is None:
            return
        try:
            dq.remove(fut)
        except ValueError:
            pass
        if not dq:
            self._pending.pop(key, None)

    async def transact(
        self,
        command: int,
        data: bytes = b"",
        timeout: Optional[float] = None,
    ) -> CivFrame:
        """Send a query and wait for the matching response frame.

        Holds the serial lock until the response arrives.  On timeout
        the query is retried once, then CivTimeoutError is raised.
        Caller must hold self._lock (or be send_command).
        """
        key = _pending_key(command, data)
        raw = build_frame(command, data, to=self.civ_addr)
        attempt_timeout = timeout if timeout is not None else self.query_timeout
        for attempt in (0, 1):      # initial try + one retry
            fut = self._register_pending(key)
            try:
                await self._write(raw)
                return await asyncio.wait_for(fut, attempt_timeout)
            except asyncio.TimeoutError:
                if attempt == 0:
                    logger.debug("CI-V query %02X timeout — retrying once", command)
                    continue
                raise CivTimeoutError(
                    f"no response to CI-V cmd 0x{command:02X} "
                    f"(2 attempts x {attempt_timeout}s)")
            finally:
                self._unregister_pending(key, fut)
        raise CivTimeoutError(f"no response to CI-V cmd 0x{command:02X}")

    async def set_with_ack(
        self,
        command: int,
        data: bytes = b"",
        timeout: Optional[float] = None,
    ) -> bool:
        """Send a set command and wait for the OK (0xFB) acknowledgement.

        Raises CivNakError on NG (0xFA), CivTimeoutError after one retry.
        Caller must hold self._lock.
        """
        loop = asyncio.get_running_loop()
        raw = build_frame(command, data, to=self.civ_addr)
        attempt_timeout = timeout if timeout is not None else self.query_timeout
        for attempt in (0, 1):
            fut: asyncio.Future = loop.create_future()
            self._pending_acks.append(fut)
            try:
                await self._write(raw)
                return await asyncio.wait_for(fut, attempt_timeout)
            except asyncio.TimeoutError:
                if attempt == 0:
                    continue
                raise CivTimeoutError(
                    f"no OK/NG for CI-V cmd 0x{command:02X}")
            finally:
                try:
                    self._pending_acks.remove(fut)
                except ValueError:
                    pass
        raise CivTimeoutError(f"no OK/NG for CI-V cmd 0x{command:02X}")

    # ── Command Interface (three priority tiers, CatController parity) ──

    async def send_command(self, cmd, timeout: Optional[float] = None):
        """Send a CI-V query and return the response frame (or None).

        ``cmd`` is either an int command byte (no data) or a bytes
        payload whose first byte is the command byte (rest = data).
        Returns None on timeout/error instead of raising — poll-loop
        parity with the FT-710's send_command.
        """
        if self._cancel_polls.is_set():
            return None
        command, data = self._split_cmd(cmd)
        await self._lock.acquire()
        try:
            if self._cancel_polls.is_set():
                return None
            if not self._connected or self._ser is None:
                return None
            try:
                return await self.transact(command, data, timeout)
            except CivTimeoutError as e:
                logger.debug("send_command %02X: %s", command, e)
                return None
            except Exception as e:
                logger.error("CI-V exchange error for cmd %02X: %s", command, e)
                if self._is_device_fatal(e):
                    self._connected = False
                return None
        finally:
            self._lock.release()

    async def send_set_command(self, cmd) -> bool:
        """Fire-and-forget set command (write-only, lock held briefly).

        ``cmd`` is an int command byte, or a bytes payload whose first
        byte is the command byte (rest = data).
        """
        command, data = self._split_cmd(cmd)
        async with self._lock:
            if not self._connected or self._ser is None:
                return False
            try:
                await self._write(build_frame(command, data, to=self.civ_addr))
                return True
            except Exception as e:
                logger.error("CI-V write error for cmd %02X: %s", command, e)
                if self._is_device_fatal(e):
                    self._connected = False
                return False

    async def send_priority_set_command(self, cmd) -> bool:
        """High-priority set command that preempts poll queries.

        Same contract as CatController.send_priority_set_command: sets
        _cancel_polls so queued/in-flight polls yield the serial lock,
        then writes without waiting for a response.  Used for PTT/tune.
        """
        command, data = self._split_cmd(cmd)
        self._cancel_polls.set()
        try:
            # Brief yield so an in-flight poll can see the cancel flag.
            await asyncio.sleep(0.005)
            async with self._lock:
                self._cancel_polls.clear()
                if not self._connected or self._ser is None:
                    return False
                try:
                    await self._write(build_frame(command, data, to=self.civ_addr))
                    return True
                except Exception as e:
                    logger.error("CI-V priority write error cmd %02X: %s", command, e)
                    if self._is_device_fatal(e):
                        self._connected = False
                    return False
        finally:
            self._cancel_polls.clear()

    @staticmethod
    def _split_cmd(cmd) -> tuple[int, bytes]:
        """Normalize a command argument into (command byte, data)."""
        if isinstance(cmd, int):
            return cmd, b""
        if isinstance(cmd, (bytes, bytearray)):
            if not cmd:
                raise ValueError("empty CI-V command")
            return cmd[0], bytes(cmd[1:])
        # str: hex payload, e.g. "1C0001" -> cmd 0x1C data 00 01
        raw = bytes.fromhex(cmd)
        return raw[0], raw[1:]

    async def query(self, cmd, timeout: Optional[float] = None):
        """Alias for send_command (ABC parity with CatController.query)."""
        return await self.send_command(cmd, timeout=timeout)

    async def set(self, cmd) -> bool:
        """Alias for send_set_command (ABC parity with CatController.set)."""
        return await self.send_set_command(cmd)

    # ── Query/set helpers ──────────────────────────────────────────

    async def _query_data(self, command: int, sub: Optional[int] = None,
                          timeout: Optional[float] = None) -> Optional[bytes]:
        """Query cmd[/sub] and return the response data bytes (or None)."""
        data = bytes((sub,)) if sub is not None else b""
        frame = await self.send_command(bytes((command,)) + data, timeout=timeout)
        if frame is None:
            return None
        return frame.data

    async def _level_query(self, command: int, sub: int,
                           timeout: Optional[float] = None) -> Optional[int]:
        """Query a sub-keyed command and decode its trailing BCD level."""
        data = await self._query_data(command, sub, timeout=timeout)
        if data is None or len(data) < 2:
            return None
        try:
            return decode_level_bcd(data[1:])
        except ValueError:
            return None

    async def _switch_query(self, sub: int, timeout: Optional[float] = None) -> Optional[int]:
        """Query a 0x16 on/off (or small-enum) switch."""
        data = await self._query_data(CMD_SWITCH, sub, timeout=timeout)
        if data is None or len(data) < 2:
            return None
        return data[1]

    async def _level_set(self, sub: int, value: int) -> bool:
        return await self.send_set_command(
            bytes((CMD_LEVEL, sub)) + encode_level_bcd(value))

    async def _switch_set(self, sub: int, value: int) -> bool:
        return await self.send_set_command(bytes((CMD_SWITCH, sub, value)))

    @staticmethod
    def _pct_to_raw(value: int) -> int:
        """Map a 0-100 UI percent to the CI-V 0-255 level range."""
        return max(0, min(255, round(value * 255 / 100)))

    @staticmethod
    def _raw_to_pct(raw: int) -> int:
        """Map a CI-V 0-255 level back to the 0-100 UI percent range."""
        return max(0, min(100, round(raw * 100 / 255)))

    # ── High-Level Command Helpers ─────────────────────────────────

    async def set_frequency(self, freq_hz: int, vfo: str = "A") -> bool:
        if vfo.upper() != "A":
            # No direct VFO-B set (capabilities.vfo_b_direct=False);
            # swap-read-swap is unsafe while the user may be operating.
            return False
        return await self.send_set_command(
            bytes((CMD_SET_FREQ,)) + encode_freq_bcd(freq_hz))

    async def get_active_vfo(self, timeout: Optional[float] = None) -> Optional[str]:
        """CI-V has no selected-VFO query; transceive doesn't report it."""
        return None

    async def get_frequency(self, vfo: str = "A", timeout: Optional[float] = None) -> Optional[int]:
        if vfo.upper() == "A":
            data = await self._query_data(CMD_READ_FREQ, timeout=timeout)
        else:
            # Unselected (VFO-B) frequency: cmd 0x25 sub 0x01.  Supported
            # on the IC-7300 (hamlib ic7300.c: .x25x26_always = 1).
            data = await self._query_data(CMD_UNSEL_FREQ, 0x01, timeout=timeout)
            if data is not None and len(data) >= 6 and data[0] == 0x01:
                data = data[1:]
        if data is None or not data:
            return None
        try:
            return decode_freq_bcd(data[:5])
        except ValueError:
            return None

    async def set_mode(self, mode_num: int) -> bool:
        # Mode-set carries the FIL byte too; re-send the current filter
        # selection so a mode change doesn't silently reset it.
        return await self.send_set_command(
            bytes((CMD_SET_MODE, mode_num & 0xFF, self._fil)))

    async def get_mode(self, timeout: Optional[float] = None) -> Optional[int]:
        data = await self._query_data(CMD_READ_MODE, timeout=timeout)
        if data is None or not data:
            return None
        if len(data) >= 2 and 1 <= data[1] <= 3:
            self._fil = data[1]
        return data[0]

    async def set_ptt(self, tx: bool) -> bool:
        return await self.send_priority_set_command(
            bytes((CMD_TX, SUB_TX_PTT, 0x01 if tx else 0x00)))

    async def set_tune(self, tune: bool) -> bool:
        # IC-7300 internal-ATU tune: 1C 01 02 starts tuning (the radio
        # transmits a carrier and tunes itself), 1C 01 00 switches the
        # tuner off.  Per the Icom CI-V reference, cmd 1C sub 01.
        # TODO(hw-verify): confirm the rig keys its own carrier on 02.
        return await self.send_priority_set_command(
            bytes((CMD_TX, SUB_TX_TUNER, 0x02 if tune else 0x00)))

    async def get_ptt(self, timeout: Optional[float] = None) -> Optional[int]:
        data = await self._query_data(CMD_TX, SUB_TX_PTT, timeout=timeout)
        if data is None or len(data) < 2:
            return None
        return 1 if data[1] else 0

    async def get_s_meter(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._level_query(CMD_METER, METER_SUB_S, timeout=timeout)

    async def get_info(self) -> Optional[dict]:
        """Combined snapshot: freq + mode + S-meter (three queries)."""
        freq = await self.get_frequency()
        mode = await self.get_mode()
        smeter = await self.get_s_meter()
        result = {}
        if freq is not None:
            result["freq"] = freq
        if mode is not None:
            result["mode"] = mode
        if smeter is not None:
            result["s_meter"] = smeter
        return result if result else None

    async def get_meter(self, meter: str, timeout: Optional[float] = None) -> Optional[int]:
        """Read a raw 0-255 meter value by name (po/swr/alc/comp/s/vd/id)."""
        sub = {
            "s": METER_SUB_S, "po": METER_SUB_PO, "pwr": METER_SUB_PO,
            "swr": METER_SUB_SWR, "alc": METER_SUB_ALC, "comp": METER_SUB_COMP,
            # vd/id intentionally unmapped: capabilities.has_vd_id_meters
            # is False for this backend, so the scheduler never asks and
            # a direct call returns None (unsupported).
        }.get(str(meter).lower())
        if sub is None:
            return None
        return await self._level_query(CMD_METER, sub, timeout=timeout)

    async def set_filter_width(self, index: int) -> bool:
        """Select IF filter FIL1-FIL3 (index 1-3) keeping the mode."""
        if index not in (1, 2, 3):
            return False
        mode = await self.get_mode()
        if mode is None:
            return False
        self._fil = index
        return await self.send_set_command(bytes((CMD_SET_MODE, mode, index)))

    async def get_filter_width(self) -> Optional[int]:
        """Current FIL selection (1-3), from the mode query's FIL byte."""
        data = await self._query_data(CMD_READ_MODE)
        if data is None or len(data) < 2 or not 1 <= data[1] <= 3:
            return None
        self._fil = data[1]
        return data[1]

    async def set_af_gain(self, value: int) -> bool:
        return await self._level_set(LVL_AF, max(0, min(255, value)))

    async def set_rf_gain(self, value: int) -> bool:
        return await self._level_set(LVL_RF_GAIN, max(0, min(255, value)))

    async def set_rf_power(self, value: int) -> bool:
        # UI scale is 0-100 percent (FT-710 parity); CI-V wants 0-255.
        return await self._level_set(LVL_RF_POWER, self._pct_to_raw(value))

    async def set_preamp(self, value: int) -> bool:
        if value not in (0, 1, 2):
            return False
        return await self._switch_set(SW_PREAMP, value)

    async def set_attenuator(self, value: int) -> bool:
        # UI index 0/1 (matches FT-710's index semantics); wire data is
        # the attenuation in dB (00 off / 20 on).
        return await self.send_set_command(
            bytes((CMD_ATT, 0x20 if value else 0x00)))

    async def set_noise_blanker(self, on: bool) -> bool:
        return await self._switch_set(SW_NB, 0x01 if on else 0x00)

    async def set_noise_reduction(self, on: bool) -> bool:
        return await self._switch_set(SW_NR, 0x01 if on else 0x00)

    async def set_auto_notch(self, on: bool) -> bool:
        # The IC-7300 HAS an auto-notch (ANF, 0x16 0x41) but this backend
        # reports has_auto_notch=False (UI hides the toggle), so the
        # setter is deliberately inert — same pattern as FT-710 set_dnr.
        logger.debug("set_auto_notch ignored on IC-7300 backend")
        return False

    async def set_compressor(self, on: bool) -> bool:
        return await self._switch_set(SW_COMP, 0x01 if on else 0x00)

    async def set_tuner(self, value: int) -> bool:
        # 0=off, 1=on, 2=start tuning (1C 01 data 00/01/02).
        if value not in (0, 1, 2):
            return False
        return await self.send_set_command(bytes((CMD_TX, SUB_TX_TUNER, value)))

    async def set_vfo(self, vfo: str) -> bool:
        if vfo.upper() not in ("A", "B"):
            return False
        return await self.send_set_command(
            bytes((CMD_VFO, 0x00 if vfo.upper() == "A" else 0x01)))

    async def set_split(self, on: bool) -> bool:
        return await self.send_set_command(
            bytes((CMD_SPLIT, 0x01 if on else 0x00)))

    async def set_power(self, on: bool) -> bool:
        return await self.send_set_command(
            bytes((CMD_POWER, 0x01 if on else 0x00)))

    async def set_squelch(self, value: int) -> bool:
        # UI scale 0-100 (FT-710 parity) -> CI-V 0-255.
        return await self._level_set(LVL_SQL, self._pct_to_raw(value))

    async def set_mic_gain(self, value: int) -> bool:
        return await self._level_set(LVL_MIC, self._pct_to_raw(value))

    async def set_band_stack(self, bsr: int) -> bool:
        return False  # Icom selects bands by frequency; no BSR command

    async def set_antenna(self, ant: int) -> bool:
        return False  # IC-7300 has a single antenna port

    async def get_antenna(self) -> Optional[int]:
        return None

    async def set_agc(self, value: int) -> bool:
        # 0=OFF 1=FAST 2=MID 3=SLOW (hamlib ic7300.c agc_levels; OFF is
        # really the AGC time-constant menu but 00 is accepted).
        # TODO(hw-verify): confirm data 00 behaviour on the rig.
        if value not in (0, 1, 2, 3):
            return False
        return await self._switch_set(SW_AGC, value)

    async def get_agc(self) -> Optional[int]:
        return await self._switch_query(SW_AGC)

    async def set_dnr(self, value: int) -> bool:
        return False  # DNR level is set via set_nr_level on this radio

    async def get_dnr(self) -> Optional[int]:
        return None

    async def set_contour(self, value: int) -> bool:
        return False  # no Contour control on the IC-7300

    async def get_contour(self) -> Optional[int]:
        return None

    async def set_drive(self, value: int) -> bool:
        """Drive level maps to RF Power (same as the FT-710 backend)."""
        return await self.set_rf_power(value)

    # ── Meter & Radio Info Commands ─────────────────────────────────

    async def set_meter_display(self, meter: int) -> bool:
        return False  # front-panel meter selection is menu-only on Icom

    async def get_meter_display(self, timeout: Optional[float] = None) -> Optional[int]:
        return None

    async def set_amc_level(self, level: int) -> bool:
        return False  # no AMC on the IC-7300

    async def get_amc_level(self, timeout: Optional[float] = None) -> Optional[int]:
        return None

    async def get_radio_info(self, timeout: Optional[float] = None) -> Optional[dict]:
        return None  # no Yaesu RI; equivalent on the IC-7300

    # ── Scope/Spectrum Commands ────────────────────────────────────

    async def set_scope_on(self, on: bool) -> bool:
        """Scope display on/off (27 10; wfview Commands\168)."""
        return await self.send_set_command(
            bytes((CMD_SCOPE, SCOPE_SUB_DISPLAY, 0x01 if on else 0x00)))

    async def get_scope_on(self) -> Optional[int]:
        data = await self._query_data(CMD_SCOPE, SCOPE_SUB_DISPLAY)
        if data is None or len(data) < 2:
            return None
        return 1 if data[1] else 0

    async def set_scope_data_output(self, on: bool) -> bool:
        """Scope CI-V data output on/off (27 11; wfview Commands\169).

        This is the switch that actually starts the 0x27 0x00 waveform
        segment stream — set_scope_on only toggles the radio's display.
        """
        return await self.send_set_command(
            bytes((CMD_SCOPE, SCOPE_SUB_DATA_OUT, 0x01 if on else 0x00)))

    async def set_scope_span(self, span: int) -> bool:
        """Set center-mode span by UI index (0-7, see SCOPE_SPAN_HZ).

        On the wire the span is a 5-byte BCD half-span frequency
        (Icom CI-V reference p. 10), not the small index.
        """
        half_span = SCOPE_SPAN_HZ.get(span)
        if half_span is None:
            return False
        return await self.send_set_command(
            bytes((CMD_SCOPE, SCOPE_SUB_SPAN)) + encode_freq_bcd(half_span))

    async def set_scope_speed(self, speed: int) -> bool:
        """Scope sweep speed 0-2 (27 1A; wfview Commands\176)."""
        if speed not in (0, 1, 2):
            return False
        return await self.send_set_command(
            bytes((CMD_SCOPE, SCOPE_SUB_SPEED, speed)))

    async def set_scope_mode(self, mode: int) -> bool:
        """Scope mode 0=center 1=fixed 2=scroll-c 3=scroll-f (27 14)."""
        if mode not in (0, 1, 2, 3):
            return False
        return await self.send_set_command(
            bytes((CMD_SCOPE, SCOPE_SUB_MODE, mode)))

    # ── Misc Settings ──────────────────────────────────────────────

    async def set_nb_level(self, level: int) -> bool:
        """NB level: UI scale 0-10 (FT-710 parity) -> CI-V 0-255."""
        value = max(0, min(255, round(level * 255 / 10)))
        return await self._level_set(LVL_NB, value)

    async def set_nr_level(self, level: int) -> bool:
        """NR level: UI scale 1-15 (FT-710 parity) -> CI-V 0-255."""
        value = max(0, min(255, round(level * 255 / 15)))
        return await self._level_set(LVL_NR, value)

    async def set_compressor_level(self, level: int) -> bool:
        """Compressor level: UI scale 1-100 (FT-710 parity) -> 0-255."""
        return await self._level_set(LVL_COMP, self._pct_to_raw(level))

    async def set_monitor(self, on: bool) -> bool:
        return False  # TODO(phase-later): 0x16 0x45 if the UI needs it

    async def set_monitor_gain(self, value: int) -> bool:
        return False  # TODO(phase-later): 0x14 0x15

    async def set_vox(self, on: bool) -> bool:
        return False  # TODO(phase-later): 0x16 0x46

    async def set_break_in(self, on: bool) -> bool:
        return False  # TODO(phase-later): 0x16 0x47

    async def set_key_speed(self, speed: int) -> bool:
        return False  # TODO(phase-later): 0x14 0x0C (6-48 wpm)

    async def set_cw_pitch(self, pitch: int) -> bool:
        return False  # TODO(phase-later): 0x14 0x09 (300-900 Hz)

    async def set_rit(self, on: bool) -> bool:
        return False  # TODO(phase-later): 0x21 0x01

    async def set_rit_freq(self, freq: int) -> bool:
        return False  # TODO(phase-later): 0x21 0x00

    async def set_xit(self, on: bool) -> bool:
        return False  # TODO(phase-later): 0x21 0x02

    # ── Bulk State Query ──────────────────────────────────────────

    async def initial_state_sync(self) -> dict:
        """Full state read after connect; returns PARSED field→value.

        Field names match RadioState.  Individual query failures are
        logged and skipped (the radio may NAK queries while off).
        Values are in RadioState units (percents where the FT-710 uses
        percents, raw 0-255 where it uses raw).
        """
        state: dict = {}

        async def _try(field: str, getter, map_fn=lambda v: v):
            try:
                value = await getter()
            except Exception as e:
                logger.debug("initial sync %s failed: %s", field, e)
                return
            if value is not None:
                state[field] = map_fn(value)

        await _try("vfo_a_freq", self.get_frequency)
        await _try("mode", self.get_mode)
        if "mode" in state:
            # get_mode already refreshed self._fil from the FIL byte.
            state["filter_width"] = self._fil
        await _try("tx_status", self.get_ptt)
        await _try("s_meter", self.get_s_meter)
        await _try("af_gain", lambda: self._level_query(CMD_LEVEL, LVL_AF))
        await _try("rf_gain", lambda: self._level_query(CMD_LEVEL, LVL_RF_GAIN))
        await _try("squelch", lambda: self._level_query(CMD_LEVEL, LVL_SQL),
                   self._raw_to_pct)
        await _try("rf_power", lambda: self._level_query(CMD_LEVEL, LVL_RF_POWER),
                   self._raw_to_pct)
        await _try("mic_gain", lambda: self._level_query(CMD_LEVEL, LVL_MIC),
                   self._raw_to_pct)
        await _try("preamp", lambda: self._switch_query(SW_PREAMP))
        await _try("attenuator", self._get_attenuator)
        await _try("noise_blanker", lambda: self._switch_query(SW_NB), bool)
        await _try("noise_reduction", lambda: self._switch_query(SW_NR), bool)
        await _try("compressor", lambda: self._switch_query(SW_COMP), bool)
        await _try("agc", self.get_agc)
        await _try("tuner_status", self._get_tuner)
        await _try("split", self._get_split, bool)
        await _try("scope_on", self.get_scope_on, lambda v: v == 1)
        state["power_on"] = bool(state)  # any answer at all ⇒ radio is on
        return state

    async def _get_attenuator(self) -> Optional[int]:
        """ATT query -> UI index (0=off, 1=20dB)."""
        data = await self._query_data(CMD_ATT)
        if data is None or not data:
            return None
        return 1 if data[0] else 0

    async def _get_tuner(self) -> Optional[int]:
        """Tuner query -> RadioState.tuner_status (0=off 1=on 2=tuning)."""
        data = await self._query_data(CMD_TX, SUB_TX_TUNER)
        if data is None or len(data) < 2:
            return None
        return data[1] if data[1] in (0, 1, 2) else None

    async def _get_split(self) -> Optional[int]:
        data = await self._query_data(CMD_SPLIT)
        if data is None or not data:
            return None
        return 1 if data[0] else 0
