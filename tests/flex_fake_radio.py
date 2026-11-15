"""A scripted SmartSDR radio for tests — TCP command channel + UDP streams.

Same idea as the SunSDR engine's ``FakeRadio`` and ``sdr_hub``'s fake pipe:
the protocol is exercised end to end over loopback sockets, so the session's
handshake order, reply matching, ownership filtering and interlock tracking are
tested without a radio and without monkeypatching the transport.

The radio binds one port number for **both** TCP and UDP (as the real one does
with 4992), so the client's one-byte UDP port registration lands where the
session sends it.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Dict, List, Optional, Tuple


class FakeRadio:
    """Scripted SmartSDR endpoint."""

    def __init__(self, *, handle: int = 0xDE300001, version: str = "1.4.0.0",
                 program_echo: bool = True) -> None:
        self.handle = handle
        self.version = version
        self.program_echo = program_echo

        self.commands: List[str] = []
        self.udp_from: Optional[Tuple[str, int]] = None
        self.udp_payloads: List[bytes] = []
        self.connected = False
        self.reply_delay = 0.0
        self.swallow: set[str] = set()          # command prefixes never answered
        self.replies: Dict[str, Tuple[int, str]] = {}
        self.client_connected = asyncio.Event()

        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("127.0.0.1", 0))
        udp.setblocking(False)
        self._udp = udp
        self.port = udp.getsockname()[1]

        self._server: Optional[asyncio.AbstractServer] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._tasks: List[asyncio.Task] = []

    # ── lifecycle ────────────────────────────────────────────────

    @classmethod
    async def create(cls, **kwargs) -> "FakeRadio":
        radio = cls(**kwargs)
        await radio.start()
        return radio

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, "127.0.0.1", self.port)
        self._tasks.append(asyncio.create_task(self._udp_loop(),
                                               name="fake-flex-udp"))

    def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self._writer is not None:
            try:
                self._writer.close()
            except (OSError, RuntimeError):
                pass
            self._writer = None
        if self._server is not None:
            self._server.close()
            self._server = None
        self._udp.close()
        self.connected = False

    # ── script ───────────────────────────────────────────────────

    def set_reply(self, prefix: str, *, code: int = 0, body: str = "") -> None:
        self.replies[prefix] = (code, body)

    def commands_matching(self, prefix: str) -> List[str]:
        return [c for c in self.commands if c.startswith(prefix)]

    async def wait_command(self, prefix: str, timeout: float = 2.0) -> str:
        """Wait for a command with this prefix and return it."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for command in self.commands_matching(prefix):
                return command
            await asyncio.sleep(0.005)
        raise AssertionError(f"no command starting with {prefix!r}; "
                             f"got {self.commands}")

    # ── pushes ───────────────────────────────────────────────────

    async def push_status(self, body: str, *, handle: Optional[int] = None) -> None:
        """Send one ``S<handle>|<body>`` line."""
        owner = self.handle if handle is None else handle
        await self._write(f"S{owner:08X}|{body}\n".encode("ascii"))

    async def push_raw(self, data: bytes) -> None:
        await self._write(data)

    async def push_packet(self, data: bytes) -> None:
        """Send a UDP datagram back to the registered client port."""
        if self.udp_from is None:
            raise AssertionError("no UDP registration received yet")
        loop = asyncio.get_running_loop()
        await loop.sock_sendto(self._udp, data, self.udp_from)

    # ── internals ────────────────────────────────────────────────

    async def _write(self, data: bytes) -> None:
        writer = self._writer
        if writer is None:
            raise AssertionError("client is not connected")
        writer.write(data)
        try:
            await writer.drain()
        except (OSError, RuntimeError):
            self.connected = False

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self.connected = True
        self.client_connected.set()
        writer.write(f"V{self.version}\r\nH{self.handle:X}\r\n".encode("ascii"))
        try:
            await writer.drain()
        except (OSError, RuntimeError):
            self.connected = False
            return
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                self._on_command(raw.decode("ascii", "replace").strip())
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            self.connected = False

    def _on_command(self, line: str) -> None:
        head, sep, command = line.partition("|")
        if not sep or not head.startswith("C"):
            return
        try:
            seq = int(head[1:])
        except ValueError:
            return
        command = command.strip()
        self.commands.append(command)
        if any(command.startswith(prefix) for prefix in self.swallow):
            return
        code, body = self._reply_for(command)
        text = f"R{seq}|{code:X}|{body}\n"
        asyncio.get_running_loop().create_task(self._send_reply(text))

    def _reply_for(self, command: str) -> Tuple[int, str]:
        for prefix, reply in self.replies.items():
            if command.startswith(prefix):
                return reply
        if command.startswith("client udpport"):
            # v1.4.0.0 always answers this even though the byte registered it.
            return 0x50001000, ""
        if command.startswith("slice list"):
            return 0, "0"
        return 0, ""

    async def _send_reply(self, text: str) -> None:
        if self.reply_delay:
            await asyncio.sleep(self.reply_delay)
        try:
            await self._write(text.encode("ascii"))
        except AssertionError:
            pass

    async def _udp_loop(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                data, addr = await loop.sock_recvfrom(self._udp, 65535)
                self.udp_payloads.append(data)
                if data == b"\x00":
                    self.udp_from = addr
        except (OSError, asyncio.CancelledError):
            pass
