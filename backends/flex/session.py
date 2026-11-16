"""SmartSDR control session: TCP 4992 command channel + the client's UDP port.

One ``SmartSDRSession`` owns the TCP socket, the UDP socket and the command/
reply bookkeeping.  It knows nothing about radios, audio or spectra: status
lines and VITA packets leave through callbacks, so ``engine.py`` stays the only
place that builds a model (design §4.3).

The handshake is the fragile part and is spelled out in one place:

1. TCP connect → radio sends ``V<version>`` then ``H<handle>``.
2. ``sub <topic> all`` for every topic we care about.
3. ``client gui`` / ``client program`` / ``client station``.
4. Bind a UDP socket and send **one 0x00 byte** from it to ``<radio>:4992`` —
   that packet *is* the port registration.  ``client udpport <port>`` follows
   and answers ``0x50001000`` on v1.4.0.0; that answer is expected, not a
   failure (analysis §2.2).
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Set

from backends.flex import protocol
from backends.flex.protocol import Response, Status

logger = logging.getLogger(__name__)

__all__ = [
    "SessionError", "SessionTimeout", "SessionClosed",
    "INTERLOCK_STATES", "RECONNECT_LIMIT_S", "next_backoff",
    "SUBSCRIPTIONS", "DEFAULT_PORT", "DEFAULT_TIMEOUT_S", "XMIT_CONFIRM_S",
    "STATUS_STATES",
    "SmartSDRSession",
]

DEFAULT_PORT = 4992
DEFAULT_TIMEOUT_S = 2.0
#: How long after an accepted ``xmit`` the interlock has to reach
#: ``TRANSMITTING`` before the key-up counts as failed (design §7.2).
XMIT_CONFIRM_S = 1.5
#: Reconnect backoff: 1, 2, 4, 8 … capped.  Pure function so it is testable.
RECONNECT_LIMIT_S = 30.0

SUBSCRIPTIONS = ("slice", "pan", "tx", "atu", "meter", "audio")

INTERLOCK_STATES = frozenset({
    "NOT_READY", "PTT_REQUESTED", "READY", "RECEIVE",
    "TIMEOUT", "TRANSMITTING", "TX_FAULT", "UNKEY_REQUESTED",
})

#: Interlock states that mean "the radio is not on the air".
STATUS_STATES = INTERLOCK_STATES


class SessionError(RuntimeError):
    """The session is unusable (socket gone, handshake failed, ...)."""


class SessionTimeout(SessionError):
    """A command got no reply in time."""


class SessionClosed(SessionError):
    """The session was closed while a command was outstanding."""


def next_backoff(attempt: int, *, limit: float = RECONNECT_LIMIT_S) -> float:
    """Seconds to wait before reconnect attempt ``attempt`` (1, 2, 4, 8 …)."""
    budget = 1.0
    try:
        steps = int(attempt)
    except (TypeError, ValueError):
        steps = 0
    for _ in range(max(0, min(steps, 8))):
        budget *= 2.0
    return min(budget, limit)


@dataclass(frozen=True)
class InterlockTransition:
    state: str
    previous: str


class SmartSDRSession:
    """Async SmartSDR control session.

    ``on_status`` receives every parsed ``S`` line; ``on_packet`` receives every
    UDP datagram (VITA bytes) untouched.  Both run on the event loop.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        program: str = "MRRC",
        station: str = "MRRC",
        on_status: Optional[Callable[[Status], None]] = None,
        on_packet: Optional[Callable[[bytes], None]] = None,
        on_interlock: Optional[Callable[[InterlockTransition], None]] = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self.host = str(host or "").strip()
        try:
            self.port = int(port)
            self.timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise SessionError(f"bad port/timeout: {exc}") from exc
        self.program = program
        self.station = station
        self._on_status = on_status
        self._on_packet = on_packet
        self._on_interlock = on_interlock

        self.version: str = ""
        self.handle: int = 0
        self.local_udp_port: int = 0
        self.interlock_state: str = ""
        self.udp_registered: bool = False
        self.commands_sent: int = 0
        self.packets_received: int = 0
        self.status_received: int = 0
        self.last_error: str = ""

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._udp: Optional[socket.socket] = None
        self._tasks: List[asyncio.Task] = []
        self._pending: Dict[int, asyncio.Future] = {}
        self._seq = 0
        self._closing = False
        self._connected = False
        self._seen_objects: Set[str] = set()

    # ── lifecycle ────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected and self._writer is not None

    async def connect(self) -> bool:
        """Connect, read the prologue and run the handshake.  False on failure."""
        if not self.host:
            self.last_error = "no radio host configured"
            return False
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=max(2.0, self.timeout))
            await self._read_prologue()
            self._connected = True
            self._open_udp()
            self._start_tasks()
            await self._handshake()
            return True
        except (OSError, asyncio.TimeoutError, SessionError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("SmartSDR connect to %s:%d failed — %s",
                           self.host, self.port, self.last_error)
            await self.close()
            return False

    async def close(self) -> None:
        self._closing = True
        self._connected = False
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(SessionClosed("session closed"))
        self._pending.clear()
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except (OSError, RuntimeError):
                pass
            self._writer = None
        self._reader = None
        if self._udp is not None:
            try:
                self._udp.close()
            except OSError:
                pass
            self._udp = None
        self.local_udp_port = 0
        self.udp_registered = False

    # ── prologue and handshake ───────────────────────────────────

    async def _read_prologue(self) -> None:
        assert self._reader is not None
        for _ in range(4):
            raw = await asyncio.wait_for(self._reader.readline(),
                                         timeout=max(2.0, self.timeout))
            if not raw:
                raise SessionClosed("radio closed the connection during prologue")
            line = raw.decode("ascii", "replace").strip()
            if not line:
                continue
            kind, value = protocol.parse_hello(line)
            if kind == "version":
                self.version = value
            else:
                try:
                    self.handle = int(value, 16)
                except ValueError as exc:
                    raise SessionError(f"bad handle {value!r}") from exc
            if self.version and self.handle:
                return
        raise SessionError("prologue did not deliver both version and handle")

    def _open_udp(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.bind(("", 0))
        # Deliberately **not** connected to the radio, and measured, not assumed: a
        # FLEX-6600 (v4.3.16) sends its VITA-49 stream from UDP source port
        # **4993**, while the registration byte and everything else go to 4992.
        # A connected socket filters on the peer port, so every datagram was
        # dropped by the kernel: the connection looked perfect (panadapter and
        # streams created, `client udpport` answered) and not one packet arrived
        # (field probe 2026-11-16: 0 packets connected, 3094 in 8 s unconnected,
        # all from 192.168.124.16:4993).
        self._udp = sock
        self.local_udp_port = sock.getsockname()[1]

    def _start_tasks(self) -> None:
        loop = asyncio.get_running_loop()
        self._tasks = [
            asyncio.create_task(self._read_loop(), name="flex-tcp-read"),
            asyncio.create_task(self._udp_loop(), name="flex-udp-read"),
        ]
        # ``loop`` is only used to keep the reference explicit for readers of
        # this module: the UDP task below owns sock_recvfrom.
        del loop

    async def _handshake(self) -> None:
        for topic in SUBSCRIPTIONS:
            await self.send(f"sub {topic} all")
        await self.send("client gui")
        await self.send(f"client program {self.program}")
        await self.send(f"client station {self.station}")
        await self._register_udp()
        # Expected to answer 0x50001000 on v1.4.0.0; the byte above is what
        # actually registered the port, so the answer is logged, never fatal.
        try:
            reply = await self.send(f"client udpport {self.local_udp_port}")
        except SessionTimeout:
            return
        if not reply.ok:
            logger.debug("client udpport answered %s (expected on v1.4.0.0)",
                         protocol.error_name(reply.code))

    async def _register_udp(self) -> None:
        if self._udp is None:
            raise SessionError("UDP socket missing")
        try:
            # The raw socket method, not ``loop.sock_sendto``: uvloop advertises
            # the loop method and raises NotImplementedError when it is awaited
            # (the 2026-11-16 crash loop), and the address has to be passed
            # explicitly anyway because the socket is not connected (see
            # ``_open_udp``).  One byte on a non-blocking socket never blocks.
            self._udp.sendto(b"\x00", (self.host, self.port))
        except OSError as exc:
            raise SessionError(f"UDP port registration failed: {exc}") from exc
        self.udp_registered = True

    # ── command channel ──────────────────────────────────────────

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return self._seq

    async def send(self, command: str, timeout: Optional[float] = None) -> Response:
        """Send one command and wait for its reply (exactly one per command)."""
        if self._writer is None:
            raise SessionClosed("not connected")
        seq = self._next_seq()
        # Build before registering: a rejected command must not leave a pending
        # future behind to time out two seconds later.
        payload = protocol.build_command(seq, command)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[seq] = future
        try:
            self._writer.write(payload)
            await self._writer.drain()
        except (OSError, RuntimeError) as exc:
            self._pending.pop(seq, None)
            raise SessionClosed(f"write failed: {exc}") from exc
        self.commands_sent += 1
        try:
            budget = self.timeout if timeout is None else float(timeout)
        except (TypeError, ValueError):
            budget = self.timeout
        try:
            return await asyncio.wait_for(future, timeout=budget)
        except asyncio.TimeoutError as exc:
            self._pending.pop(seq, None)
            raise SessionTimeout(
                f"no reply to {command!r} within {budget:.2f}s") from exc

    async def send_ok(self, command: str, timeout: Optional[float] = None) -> bool:
        """Send a set-style command; True when the radio accepted it."""
        try:
            reply = await self.send(command, timeout=timeout)
        except SessionError:
            return False
        if not reply.ok:
            logger.debug("command %r rejected: %s", command,
                         protocol.error_name(reply.code))
        return reply.ok

    # ── readers ──────────────────────────────────────────────────

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while not self._closing:
                raw = await self._reader.readline()
                if not raw:
                    break
                self._dispatch_line(raw.decode("ascii", "replace").strip())
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            self._connected = False
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(SessionClosed("connection lost"))
            self._pending.clear()

    async def _udp_loop(self) -> None:
        if self._udp is None:
            return
        loop = asyncio.get_running_loop()
        try:
            while not self._closing:
                try:
                    data = await loop.sock_recv(self._udp, 65535)
                except ConnectionRefusedError:
                    # A connected UDP socket reports the peer's ICMP "port
                    # unreachable" on the next recv.  That is not a reason to stop
                    # listening: the radio answers again as soon as it is done
                    # rebooting, and the stream must not go silently dead.
                    continue
                self.packets_received += 1
                if self._on_packet is not None:
                    self._on_packet(data)
        except (OSError, asyncio.CancelledError) as exc:
            # Silence here used to be the only symptom of a dead waterfall, so
            # say why (the check is for a shutdown racing this task).
            if not self._closing:
                logger.warning("SmartSDR UDP stream stopped: %s: %s",
                               type(exc).__name__, exc)

    def _dispatch_line(self, line: str) -> None:
        if not line:
            return
        prefix = line[0]
        if prefix == "R":
            try:
                reply = protocol.parse_response(line)
            except protocol.ProtocolError as exc:
                logger.debug("unparseable response %r: %s", line, exc)
                return
            future = self._pending.pop(reply.seq, None)
            if future is not None and not future.done():
                future.set_result(reply)
            return
        if prefix == "S":
            try:
                status = protocol.parse_status(line)
            except protocol.ProtocolError as exc:
                logger.debug("unparseable status %r: %s", line, exc)
                return
            self.status_received += 1
            self._track_interlock(status)
            if self._on_status is not None:
                self._on_status(status)
            return
        if prefix == "M":
            logger.debug("radio message: %s", line[1:])
            return
        logger.debug("ignoring prologue-era line %r", line)

    def _track_interlock(self, status: Status) -> None:
        if not status.obj.startswith("interlock"):
            return
        state = status.get("state", "")
        if not state or state == self.interlock_state:
            return
        previous = self.interlock_state
        self.interlock_state = state
        if state not in INTERLOCK_STATES:
            logger.debug("unknown interlock state %r", state)
        if self._on_interlock is not None:
            self._on_interlock(InterlockTransition(state=state, previous=previous))

    # ── helpers used by the engine ───────────────────────────────

    def owns(self, status: Status) -> bool:
        """True when a status belongs to this client (or is system-wide)."""
        if self.handle == 0:
            return True
        owner = status.get("client_handle")
        if owner is None:
            return True
        try:
            return int(owner, 16) == self.handle
        except ValueError:
            return True

    async def wait_interlock(self, wanted: Set[str],
                             timeout: float = XMIT_CONFIRM_S) -> bool:
        """Wait until the interlock reaches one of ``wanted``.

        Polled at the status cadence rather than event-driven on purpose: the
        caller is a state machine that must survive a missed push, and 20 ms is
        well inside the 1.5 s confirmation budget.
        """
        try:
            budget = float(timeout)
        except (TypeError, ValueError):
            budget = XMIT_CONFIRM_S
        deadline = asyncio.get_running_loop().time() + budget
        while asyncio.get_running_loop().time() < deadline:
            if self.interlock_state in wanted:
                return True
            await asyncio.sleep(0.02)
        return self.interlock_state in wanted

    def seen_objects(self) -> Set[str]:
        """Object names observed so far (diagnostics)."""
        return set(self._seen_objects)


AwaitableStatus = Awaitable[Status]
