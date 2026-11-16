"""FlexRadio must run on **uvloop**, not just on the default selector loop.

Field incident, 2026-11-16 (box MRRC-Modern, v1.28.67, FlexRadio on the LAN):
the service crash-looped 36 times with

    File "backends/flex/session.py", line 258, in _register_udp
      await loop.sock_sendto(self._udp, b"\\x00", (self.host, self.port))
    File "uvloop/loop.pyx", line 2658, in sock_sendto
    NotImplementedError

``uvloop``'s loop class **advertises** ``sock_sendto`` and ``sock_recvfrom``
(``hasattr`` is True) and raises ``NotImplementedError`` only when they are
awaited — so nothing about the code reads as wrong, and the whole flex suite
passed because ``IsolatedAsyncioTestCase`` runs on the default selector loop
while the product runs under uvicorn's ``loop="auto"`` (which picks uvloop
whenever it is importable — it ships in ``uvicorn[standard]`` on Linux and
macOS).

Two guards, because they catch different mistakes:

* ``LoopCompatibilityTests`` — the UDP registration and discovery really work on
  a loop that lacks those two methods (uvloop when installed; otherwise an
  instance patched to raise exactly as uvloop does, so Windows CI is covered).
* ``NoUnsupportedDatagramLoopApiTests`` — an AST scan that fails the build when
  a *new* production call site appears. Works without uvloop installed.
"""

from __future__ import annotations

import ast
import asyncio
import os
import socket
import struct
import unittest
from pathlib import Path

from backends.flex import vita
from backends.flex.engine import discover_radios
from backends.flex.session import SmartSDRSession

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_like_uvloop(coro):
    """Run ``coro`` on a loop that has uvloop's datagram-helper gaps.

    With uvloop installed this is the real thing. Without it we patch the
    instance, because the *only* property that matters here is "these two
    methods raise NotImplementedError when awaited".
    """
    try:
        import uvloop                      # noqa: PLC0415 — optional dependency
    except ImportError:
        uvloop = None
    if uvloop is not None:
        return uvloop.run(coro)

    loop = asyncio.new_event_loop()

    def _not_implemented(*_args, **_kwargs):
        raise NotImplementedError

    loop.sock_sendto = _not_implemented       # type: ignore[method-assign]
    loop.sock_recvfrom = _not_implemented     # type: ignore[method-assign]
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _free_udp_port() -> int:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


def _discovery_packet(text: bytes) -> bytes:
    """A VITA ext-data broadcast exactly as ``tests/test_flex_engine.py`` builds it."""
    payload = text + b"\x00" * 4
    word0 = ((3 << 28) | (1 << 27) | (3 << 22) | (1 << 20)
             | ((28 + len(payload) + 3) // 4))
    head = struct.pack(">IIIIIII", word0, vita.DISCOVERY_STREAM_ID,
                       vita.OPUS_OUI, (vita.ICC << 16) | 0xFFFF, 0, 0, 0)
    return head + payload


class UdpRegistrationTests(unittest.TestCase):
    """``_register_udp`` — the exact line that crashed the box."""

    def test_the_registration_byte_reaches_the_radio(self):
        radio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        radio.bind(("127.0.0.1", 0))
        radio.setblocking(False)
        session = SmartSDRSession("127.0.0.1", radio.getsockname()[1])

        async def flow():
            session._open_udp()
            try:
                await asyncio.wait_for(session._register_udp(), 2.0)
                return await asyncio.wait_for(
                    asyncio.get_running_loop().sock_recv(radio, 64), 2.0)
            finally:
                await session.close()

        try:
            self.assertEqual(run_like_uvloop(flow()), b"\x00")
        finally:
            radio.close()
        self.assertEqual(session.local_udp_port, 0)      # closed with the session

    def test_the_downlink_stream_is_received_on_the_same_socket(self):
        """The VITA stream comes back on the client's UDP port; the connected
        socket must still deliver it (the port registration is the only thing
        the radio knows about us)."""
        radio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        radio.bind(("127.0.0.1", 0))
        radio.setblocking(False)
        session = SmartSDRSession("127.0.0.1", radio.getsockname()[1])
        seen = []
        session._on_packet = seen.append

        async def flow():
            session._open_udp()
            try:
                await asyncio.wait_for(session._register_udp(), 2.0)
                task = asyncio.create_task(session._udp_loop())
                await asyncio.sleep(0.05)
                radio.sendto(b"VITA-49-PAYLOAD",
                             ("127.0.0.1", session.local_udp_port))
                for _ in range(40):
                    if seen:
                        break
                    await asyncio.sleep(0.02)
                task.cancel()
                return session.packets_received
            finally:
                await session.close()

        try:
            self.assertGreaterEqual(run_like_uvloop(flow()), 1)
        finally:
            radio.close()
        self.assertEqual(seen[0], b"VITA-49-PAYLOAD")


class DiscoveryTests(unittest.TestCase):
    """``discover_radios`` — it needs the sender's address, which ``sock_recv``
    (the only datagram helper uvloop *does* implement) throws away."""

    def test_a_broadcast_is_discovered_without_sock_recvfrom(self):
        port = _free_udp_port()
        # No ``ip=`` in the payload: the address can then only come from the
        # datagram's source, which is what the old sock_recvfrom supplied.
        packet = _discovery_packet(b"model=FLEX-6400 serial=1234-5678 "
                                   b"version=4.1.5 name=Shack_FLEX callsign=BG7XXX")

        async def flow():
            task = asyncio.create_task(discover_radios(1.0, port=port))
            await asyncio.sleep(0.1)
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sender.sendto(packet, ("127.0.0.1", port))
            finally:
                sender.close()
            return await asyncio.wait_for(task, 3.0)

        radios = run_like_uvloop(flow())
        self.assertEqual(len(radios), 1)
        self.assertEqual(radios[0]["model"], "FLEX-6400")
        self.assertEqual(radios[0]["ip"], "127.0.0.1")

    def test_timeout_still_returns_an_empty_list(self):
        self.assertEqual(
            run_like_uvloop(discover_radios(0.1, port=_free_udp_port())), [])


class NoUnsupportedDatagramLoopApiTests(unittest.TestCase):
    """uvloop advertises these two and raises once awaited — a call site here is
    a box that cannot start, and only on the deployed platform."""

    FORBIDDEN = {"sock_sendto", "sock_recvfrom"}
    # Runtime trees only (the server, its backends, the DSP worker, the box-side
    # scripts). tests/ keeps using them on purpose: the fake radios drive the
    # default selector loop. The scan is an include list rather than a skip list
    # because this repo carries ~2200 .py files, most of them vendored
    # references under ref/ that would cost seconds to parse for nothing.
    SCAN_ROOTS = ("backends", "ft8", "nano", "tools", "linux", "dev_tools")

    def _production_files(self):
        yield from sorted(REPO_ROOT.glob("*.py"))
        for name in self.SCAN_ROOTS:
            base = REPO_ROOT / name
            if not base.is_dir():
                continue
            for root, dirs, files in os.walk(base):
                dirs[:] = sorted(d for d in dirs if d != "__pycache__")
                for filename in sorted(files):
                    if filename.endswith(".py"):
                        yield Path(root) / filename

    def test_no_production_call_site(self):
        offenders = []
        for path in self._production_files():
            text = path.read_text("utf-8", "replace")
            # Cheap gate first: an ast.parse of every runtime file is seconds
            # (server.py alone is 9k lines) and this test runs in the full suite.
            if not any(token in text for token in self.FORBIDDEN):
                continue
            tree = ast.parse(text, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in self.FORBIDDEN:
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} {func.attr}()")
        self.assertEqual(
            offenders, [],
            "uvloop raises NotImplementedError for these; use a connected "
            "socket + sock_sendall, or create_datagram_endpoint:\n  "
            + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
