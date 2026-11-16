"""
SCU-LAN10 session client — four UDP streams, one 7-phase handshake
==================================================================
The state machine an SCU-LAN10 wants before it will carry anything: five
hardcoded init packets, an auth packet with fixed key and sequence, the
session id it answers with, a continuation, three channel setups (CAT / audio
/ scope), a keepalive on each of the four ports, then steady state — CAT
round-trips on port+1, 16 kHz audio on port+2, 4096-byte scope bodies on
port+3, all four kept alive at 1 Hz.

Two properties of this device shape the whole module:

* **The session is exclusive and unforgiving.**  Missing keepalives for a few
  seconds ends it, and there is no resumption — a reconnect is the full
  seven phases again.  So the UDP sockets are created once and the *session*
  is what is rebuilt; and the session being taken over by another client
  (Yaesu's own FT-Control, say) is a logged, named event rather than a silent
  stall — a box that quietly stops moving is the worst field report there is.
* **CAT is fire-and-forget on the wire.**  Every command goes out twice with
  the same ``cmd_seq`` (the radio dedupes); replies come back identified by the
  command mnemonic, not by a request id, and the radio also pushes unsolicited
  ``RM1xxx;`` meter frames.  So this layer never correlates: it hands every
  inbound frame to ``on_cat`` and lets the serial-shaped transport above it
  (which knows what it wrote) do the matching.  One correlator, not two.

Sequence counting is **per port, not per channel field** (2026-11-16, field).
The header's ``b2`` names the channel being *configured* by a setup packet —
which is deliberately the *next* one — while the counter the unit validates
belongs to the port the datagram arrives on.  Mixing the two made the two
setup packets sent over CTRL consume the CAT/AUDIO/SCOPE counters, so all
three port-inits went out as ``seq 2`` instead of ``1``; the unit dropped all
three silently, the handshake still "succeeded" (auth and keepalive acks keep
the link alive) and the session then carried no CAT, no audio and no sweeps.
The rule the SPEC states is what this module now implements: post-auth CTRL
packets continue at ``07`` (continuation, both setups, then keepalives) and
each data port's first packet — its port-init — is that port's ``01``.

Nothing here knows about radios: frequencies, modes and meters are the CAT
core's business (`backends/yaesu/cat_core.py`, unchanged) and the spectrum
body's interpretation is `scope_format.py`.  See
docs/superpowers/specs/2026-11-15-scu-lan-backend-design.md.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from backends.yaesu.scu_lan import protocol as proto

#: The four UDP streams, in the order the handshake sets them up.  This is the
#: *sending port* — the thing a header sequence counter belongs to.
_SOCKET_KINDS = ("ctrl", "cat", "audio", "scope")

logger = logging.getLogger("yaesu.scu_lan")

#: Capability bytes the unit reports in the auth answer.  Constant across every
#: observed session and firmware; meaning unknown, so we only echo it back for
#: logs and the support bundle.
CAPABILITY_BYTES = bytes((0x01, 0x25, 0x01, 0x3F, 0x51))

#: Pause between the channel-setup steps — hammering them makes the unit drop
#: them silently (published observation).
SETUP_STEP_S = 0.18

OnScope = Callable[[bytes], None]
OnAudio = Callable[[bytes], None]
OnCat = Callable[[str], None]


class ScuLanError(RuntimeError):
    """Base class for every SCU-LAN10 link failure."""


class AuthenticationFailed(ScuLanError):
    """The unit never answered the auth packet (a bad credential is silent)."""


class SessionLost(ScuLanError):
    """Keepalives or data stopped — the session has to be rebuilt."""


@dataclass
class LinkStats:
    """Counters for ``/api/state``, the log and the support bundle."""

    session_id: int = 0
    handshakes: int = 0
    reconnects: int = 0
    scope_packets: int = 0
    audio_packets: int = 0
    cat_commands: int = 0
    cat_frames: int = 0
    malformed: int = 0
    foreign_packets: int = 0
    last_scope_at: float = 0.0
    last_audio_at: float = 0.0
    last_cat_at: float = 0.0
    last_keepalive_at: float = 0.0
    capability: bytes = CAPABILITY_BYTES

    def scope_age_s(self) -> float:
        return 0.0 if not self.last_scope_at else time.monotonic() - self.last_scope_at

    def audio_age_s(self) -> float:
        return 0.0 if not self.last_audio_at else time.monotonic() - self.last_audio_at

    def as_dict(self) -> dict:
        data = {k: v for k, v in self.__dict__.items()}
        data["capability"] = self.capability.hex()
        data["scope_age_s"] = round(self.scope_age_s(), 2)
        data["audio_age_s"] = round(self.audio_age_s(), 2)
        return data


class _Stream(asyncio.DatagramProtocol):
    """One UDP socket, one channel kind.  Never raises into the loop."""

    def __init__(self, client: "ScuLanClient", kind: str) -> None:
        self._client = client
        self._kind = kind
        self.transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport) -> None:            # noqa: D102
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # noqa: D102
        self._client._dispatch(self._kind, data, addr)

    def error_received(self, exc: Exception) -> None:        # pragma: no cover
        logger.debug("SCU-LAN10 %s socket error: %s", self._kind, exc)

    def connection_lost(self, exc) -> None:                  # pragma: no cover
        if exc:
            logger.debug("SCU-LAN10 %s socket closed: %s", self._kind, exc)


@dataclass
class _Sockets:
    ctrl: Optional[asyncio.DatagramTransport] = None
    cat: Optional[asyncio.DatagramTransport] = None
    audio: Optional[asyncio.DatagramTransport] = None
    scope: Optional[asyncio.DatagramTransport] = None
    targets: dict = field(default_factory=dict)

    def all(self) -> dict:
        return {"ctrl": self.ctrl, "cat": self.cat,
                "audio": self.audio, "scope": self.scope}

    def close(self) -> None:
        for transport in self.all().values():
            if transport is not None:
                try:
                    transport.close()
                except Exception:                            # noqa: BLE001
                    pass
        self.ctrl = self.cat = self.audio = self.scope = None


class ScuLanClient:
    """An SCU-LAN10 session: handshake, keepalives, CAT, scope, audio.

    The callbacks run **on the event loop**; a slow one delays keepalives, and
    a late keepalive is an ended session, so they must only enqueue.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        base_port: int = 50000,
        on_scope: Optional[OnScope] = None,
        on_audio: Optional[OnAudio] = None,
        on_cat: Optional[OnCat] = None,
        on_state: Optional[Callable[[dict], None]] = None,
        auth_timeout: float = 5.0,
        keepalive_interval: float = 0.5,
        link_timeout: float = 12.0,
        setup_step_s: float = SETUP_STEP_S,
        want_scope: bool = True,
        want_audio: bool = True,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        try:
            self.base_port = int(base_port)
        except (TypeError, ValueError):
            raise ScuLanError(
                f"base_port must be an integer, got {base_port!r}") from None
        self.on_scope = on_scope
        self.on_audio = on_audio
        self.on_cat = on_cat
        self.on_state = on_state
        self.auth_timeout = auth_timeout
        self.keepalive_interval = keepalive_interval
        self.link_timeout = link_timeout
        self.setup_step_s = setup_step_s
        self.want_scope = want_scope
        self.want_audio = want_audio

        self.stats = LinkStats()
        self._sockets = _Sockets()
        #: One counter per **sending port** (see the module docstring): the
        #: channel byte of a packet names the channel it sets up, not the
        #: counter it advances.
        self._sequence = {kind: proto.SequenceCounter(0x01)
                          for kind in _SOCKET_KINDS}
        self._cat_seq = proto.SequenceCounter(0x01)
        self._session_id = 0
        #: Sample rate the unit declares in the audio inner header (bytes 4-7,
        #: LE u32).  The reverse-engineered captures were all 16 kHz, but the
        #: official client offers "PCM(48kHz)" in its connection setup, so the
        #: rate is read rather than assumed: a 48 kHz stream run through a
        #: 16 kHz resampler is a 3x pitch/speed error, not a subtle bug.
        self.audio_rate = 0
        #: IP the unit speaks from, learned from its first packet.  The protocol
        #: carries no authentication, so a packet from anywhere else is not the
        #: radio — dropping it keeps a stray device on the LAN from moving this
        #: state machine (a forged ACK_AUTH would complete the "handshake").
        self._peer_ip = None
        self._foreign_logged = 0
        self._connected = False
        self._auth_failed = False
        self._command_done = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tasks: list[asyncio.Task] = []
        self._stop = False
        self._last_inbound = 0.0
        self._last_logged_loss = 0.0
        #: True while `start()` is between its first await and its task list.
        self._starting = False

    # ── lifecycle ──────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def session_id(self) -> int:
        return self._session_id

    @property
    def auth_failed(self) -> bool:
        """True after a mute unit — the only symptom of a wrong credential."""
        return self._auth_failed

    async def start(self) -> None:
        """Bind the four sockets and begin the session loop (never blocks).

        Idempotent **and** re-entrancy-safe: the CAT transport and the RX audio
        adapter can both reach this within the same startup, and the first
        call awaits four `create_datagram_endpoint` calls before it has any
        `_tasks` to show for it.  Without the `_starting` latch the second
        caller sees an empty task list, binds a second set of sockets and
        clobbers `self._sockets` — one session, two socket sets, and the
        targets the first one announced point at a closed transport.
        """
        if self._tasks or self._starting:
            return
        self._starting = True
        try:
            loop = asyncio.get_running_loop()
            self._loop = loop
            self._stop = False
            for kind in _SOCKET_KINDS:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda kind=kind: _Stream(self, kind),
                    local_addr=("0.0.0.0", 0), family=socket.AF_INET)
                setattr(self._sockets, kind, transport)
            self._sockets.targets = {
                "ctrl": (self.host, self.base_port + 0),
                "cat": (self.host, self.base_port + 1),
                "audio": (self.host, self.base_port + 2),
                "scope": (self.host, self.base_port + 3),
            }
            self._tasks = [
                loop.create_task(self._session_loop(), name="scu-lan-session"),
            ]
        finally:
            self._starting = False

    async def stop(self) -> None:
        """Stop the session and close the sockets."""
        self._stop = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):      # noqa: BLE001
                pass
        self._tasks = []
        self._sockets.close()
        self._connected = False

    async def wait_until_connected(self, timeout: float = 10.0) -> bool:
        """Block until the handshake completes (tests and the diag script)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._connected:
                return True
            await asyncio.sleep(0.02)
        return self._connected

    # ── public API ─────────────────────────────────────────────────

    def send_cat(self, command: str) -> bool:
        """Queue one CAT command (sent twice, as the device expects).

        Each call takes a **fresh** `cmd_seq`, which is what makes a repeat call
        a *retry*: the unit dedupes by that field, so a byte-identical resend is
        silently discarded.  A release (`TX0;`) that has to be re-sent must
        therefore come back through this function, never as a raw re-write.
        """
        if not self._connected or not self._sockets.cat:
            return False
        command = (command or "").strip()
        if not command:
            return False
        if not command.endswith(";"):
            command += ";"
        cmd_seq = self._cat_seq.next()
        body = proto.cat_request_body(self._session_id, cmd_seq, command)
        # Two datagrams with **independent wire sequence numbers** and the same
        # `cmd_seq` (the radio dedupes on that field).  Sending the same bytes
        # twice would put an id on the wire that the unit has already seen.
        targets = self._sockets.targets["cat"]
        for _ in range(2):
            raw = self._frame(body, "cat", proto.MSG_DATA,
                              channel=proto.CHANNEL_CAT)
            self._sockets.cat.sendto(raw, targets)
        self.stats.cat_commands += 1
        return True

    def send_cat_threadsafe(self, command: str) -> bool:
        """`send_cat` for a caller on another thread (the serial-shaped seam).

        ``asyncio`` datagram transports belong to the loop, while
        ``YaesuCatController._write`` runs through ``asyncio.to_thread`` — so
        every write from the transport lands here.  Returns False when there is
        no loop yet or the session is down; the caller turns that into a
        transient serial error instead of a silent drop.
        """
        loop = self._loop
        if loop is None or loop.is_closed() or not self._connected:
            return False
        loop.call_soon_threadsafe(self.send_cat, command)
        return True

    def snapshot(self) -> dict:
        """JSON-safe link state for the server log / support bundle."""
        return {
            "host": self.host,
            "port": self.base_port,
            "connected": self._connected,
            "session_id": self._session_id,
            **self.stats.as_dict(),
        }

    # ── session ────────────────────────────────────────────────────

    async def _session_loop(self) -> None:
        """Rebuild the session forever; cancellation propagates untouched."""
        backoff = 1.0
        while not self._stop:
            try:
                await self._handshake()
                self._connected = True
                self.stats.handshakes += 1
                self.stats.session_id = self._session_id
                backoff = 1.0
                self._publish_state()
                logger.info(
                    "SCU-LAN10 session 0x%02X up on %s:%d (handshake %d)",
                    self._session_id, self.host, self.base_port,
                    self.stats.handshakes)
                await self._serve()
            except AuthenticationFailed as exc:
                self._connected = False
                # A silent unit is the *only* signal for a bad credential, so
                # this must not retry in a tight loop — and must not be silent
                # to the operator either.
                logger.error("SCU-LAN10 auth failed on %s:%d — %s "
                             "(user %r; the unit answers nothing on bad "
                             "credentials)",
                             self.host, self.base_port, exc, self.username)
                self._auth_failed = True
                self._publish_state()
                await asyncio.sleep(10.0)
            except (SessionLost, OSError) as exc:
                self._connected = False
                self.stats.reconnects += 1
                self._publish_state()
                logger.warning("SCU-LAN10 session down (%s); re-handshake in %.0fs",
                               exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    async def _handshake(self) -> None:
        """Phases 1-7.  Raises `AuthenticationFailed` when the unit stays mute."""
        self._auth_failed = False
        self._session_id = 0
        # The data ports each start at 1 with their port-init; the CTRL port's
        # counter is resumed at 0x07 after the auth answer (see Phase 4).
        for kind in ("cat", "audio", "scope"):
            self._sequence[kind].reset()

        ctrl = self._sockets.ctrl
        target = self._sockets.targets["ctrl"]
        assert ctrl is not None                            # start() ran

        # Phase 1 — the five fixed init packets, byte for byte.
        for raw in proto.INIT_PACKETS:
            ctrl.sendto(raw, target)

        # Phase 2 — auth.
        auth = proto.build_auth(self.username, self.password)
        self._command_done.clear()
        ctrl.sendto(auth, target)

        # Phase 3 — the answer carries the session id.  Polled rather than
        # wait_for() so the failure reads as a protocol outcome (a mute unit)
        # instead of an asyncio timeout.
        deadline = time.monotonic() + self.auth_timeout
        while not self._command_done.is_set():
            if time.monotonic() > deadline:
                raise AuthenticationFailed(
                    f"no auth answer within {self.auth_timeout:.1f}s")
            await asyncio.sleep(0.01)

        # Phase 4 — continuation, sent on the CTRL socket with the CAT channel
        # id: a wire quirk that is consistent across captures.
        #
        # The wire keeps ONE CTRL counter across the init packets (01-05), the
        # auth packet (06) and everything after it, so post-auth CTRL traffic
        # continues at 07.  Restarting at 01 would replay sequence ids the unit
        # has already seen — and it drops a packet whose id it has seen before,
        # which reads as "the unit ignored my setup" (upstream SPEC §Sequence
        # Counters, 2026-11-15 audit).
        self._sequence["ctrl"].reset(0x07)
        self._send(proto.continuation_body(self._session_id), "ctrl",
                   proto.MSG_SETUP, channel=proto.CHANNEL_CAT)
        await asyncio.sleep(self.setup_step_s)

        # Phase 5 — three channels: port-init on the data port, setup on ctrl.
        # The `channel` of a setup packet names the channel being set up NEXT
        # (another quirk), so each entry carries its own b2 value.
        sid = self._session_id
        steps: list[tuple[str, int, Optional[bytes]]] = [
            ("cat", proto.CHANNEL_CAT, None),
            ("ctrl", proto.CHANNEL_AUDIO, proto.cat_setup_body(sid)),
        ]
        if self.want_audio:
            steps.append(("audio", proto.CHANNEL_AUDIO, None))
            steps.append(("ctrl", proto.CHANNEL_SCOPE, proto.audio_setup_body(sid)))
        if self.want_scope:
            steps.append(("scope", proto.CHANNEL_SCOPE, None))
        for kind, channel, body in steps:
            if body is None:
                self._send(proto.port_init_body(sid), kind,
                           proto.MSG_PORT_INIT, channel=channel)
            else:
                self._send(body, "ctrl", proto.MSG_SETUP, channel=channel)
            await asyncio.sleep(self.setup_step_s)

        # Phase 6 — one keepalive per channel.
        self._send_keepalives()

    async def _serve(self) -> None:
        """Steady state: keepalives at 1 Hz, fail when the link goes quiet."""
        last_keepalive = 0.0
        self._last_inbound = time.monotonic()
        while not self._stop and self._connected:
            now = time.monotonic()
            if now - last_keepalive >= self.keepalive_interval:
                self._send_keepalives()
                last_keepalive = now
                self.stats.last_keepalive_at = now
                if now - self._last_inbound > self.link_timeout:
                    raise SessionLost(
                        f"no datagram for {now - self._last_inbound:.0f}s")
            await asyncio.sleep(0.05)

    def _send_keepalives(self) -> None:
        for kind, channel in (("ctrl", proto.CHANNEL_CTRL),
                              ("cat", proto.CHANNEL_CAT),
                              ("audio", proto.CHANNEL_AUDIO),
                              ("scope", proto.CHANNEL_SCOPE)):
            transport = getattr(self._sockets, kind)
            if transport is None:
                continue
            transport.sendto(self._frame(b"\x00\x00\x00\x00", kind,
                                         proto.MSG_KEEPALIVE, channel=channel),
                             self._sockets.targets[kind])

    # ── inbound ────────────────────────────────────────────────────

    def _dispatch(self, kind: str, data: bytes, addr=None) -> None:
        if not self._from_peer(addr):
            return
        packet = proto.try_parse(data)
        if packet is None:
            self.stats.malformed += 1
            return
        self._last_inbound = time.monotonic()
        # The unit sets bit 7 on every message it sends (direction), so its
        # data frames arrive as ``MSG_ACK_DATA`` (0x87), not ``MSG_DATA``
        # (0x07).  Comparing against the client's own constant dropped every
        # CAT reply, every audio body and every sweep into `_on_ack` — the
        # second half of the 2026-11-16 field defect, invisible because an
        # ack it cannot use is just a debug line.
        if packet.msg_type not in (proto.MSG_DATA, proto.MSG_ACK_DATA):
            # Acks (port-init 0x86, keepalive 0x85, setup 0x81) share the data
            # channels' ports; treating one as a 4-byte scope body is how a
            # waterfall ends up drawn from an acknowledgement.
            self._on_ack(packet)
            return
        if packet.channel == proto.CHANNEL_CAT:
            self._on_cat(packet)
        elif packet.channel == proto.CHANNEL_AUDIO:
            self._on_audio(packet)
        elif packet.channel == proto.CHANNEL_SCOPE:
            self._on_scope(packet)
        else:
            logger.debug("SCU-LAN10 unexpected data channel 0x%02X on %s",
                         packet.channel, kind)

    def _from_peer(self, addr) -> bool:
        """True when this datagram comes from the unit we are talking to.

        The first packet from anywhere is adopted as the peer (so a hostname
        configured by the operator resolves to whatever it answers from); after
        that, anything from another address is counted and dropped.
        """
        if addr is None:                       # in-process tests call _dispatch
            return True
        ip = addr[0] if isinstance(addr, (tuple, list)) and addr else str(addr)
        if self._peer_ip is None:
            self._peer_ip = ip
            logger.debug("SCU-LAN10 peer is %s", ip)
            return True
        if ip == self._peer_ip:
            return True
        self.stats.foreign_packets += 1
        if self.stats.foreign_packets % 100 == 1:
            logger.warning("SCU-LAN10: ignoring packet from %s (the unit is %s) "
                           "— %d so far", ip, self._peer_ip,
                           self.stats.foreign_packets)
        return False

    def _on_ack(self, packet: proto.Packet) -> None:
        if packet.channel == proto.CHANNEL_CTRL and packet.msg_type == proto.MSG_ACK_AUTH:
            self._on_auth_answer(packet)
            return
        logger.debug("SCU-LAN10 %s ack on %s",
                     proto.MSG_NAMES.get(packet.msg_type, hex(packet.msg_type)),
                     packet.channel_name)

    def _on_auth_answer(self, packet: proto.Packet) -> None:
        if self._command_done.is_set():
            return
        self._session_id = packet.body[0] if packet.body else 0
        if len(packet.body) >= 14:
            self.stats.capability = bytes(packet.body[9:14])
        self._command_done.set()

    def _on_cat(self, packet: proto.Packet) -> None:
        text = proto.cat_ascii(packet.body)
        if not text:
            return
        self.stats.cat_frames += 1
        self.stats.last_cat_at = time.monotonic()
        if self.on_cat is not None:
            try:
                self.on_cat(text)
            except Exception:                              # noqa: BLE001
                logger.exception("SCU-LAN10 CAT callback failed")

    def _on_audio(self, packet: proto.Packet) -> None:
        payload = self._audio_payload(packet.body)
        if not payload:
            return
        rate = self._declared_rate(packet.body)
        if rate and rate != self.audio_rate:
            if self.audio_rate:
                logger.info("SCU-LAN10 audio rate changed %d -> %d Hz",
                            self.audio_rate, rate)
            self.audio_rate = rate
        self.stats.audio_packets += 1
        self.stats.last_audio_at = time.monotonic()
        if self.on_audio is not None:
            try:
                self.on_audio(payload)
            except Exception:                              # noqa: BLE001
                logger.exception("SCU-LAN10 audio callback failed")

    @staticmethod
    def _declared_rate(body: bytes) -> int:
        """Sample rate from the audio inner header (0 when it is not there)."""
        if len(body) < 8:
            return 0
        rate = int.from_bytes(body[4:8], "little")
        # Anything outside a sane audio range is a misread, not a sample rate.
        return rate if 4000 <= rate <= 192000 else 0

    @staticmethod
    def _audio_payload(body: bytes) -> bytes:
        """Strip the 24-byte inner header; a short body is not audio."""
        if len(body) <= 24:
            return b""
        declared = body[22] | (body[23] << 8) if len(body) >= 24 else 0
        payload = body[24:]
        if 0 < declared <= len(payload):
            return payload[:declared]
        return payload

    def _on_scope(self, packet: proto.Packet) -> None:
        if not packet.body:
            return
        self.stats.scope_packets += 1
        self.stats.last_scope_at = time.monotonic()
        if self.on_scope is not None:
            try:
                self.on_scope(packet.body)
            except Exception:                              # noqa: BLE001
                logger.exception("SCU-LAN10 scope callback failed")

    # ── helpers ────────────────────────────────────────────────────

    def _frame(self, body: bytes, kind: str, msg_type: int,
               channel: Optional[int] = None) -> bytes:
        channel = proto.CHANNEL_CTRL if channel is None else channel
        seq = self._sequence[kind].next()
        return proto.frame(body, seq=seq, channel=channel, msg_type=msg_type)

    def _send(self, body: bytes, kind: str, msg_type: int,
              channel: Optional[int] = None) -> None:
        transport = getattr(self._sockets, kind)
        if transport is None:
            return
        transport.sendto(self._frame(body, kind, msg_type, channel),
                         self._sockets.targets[kind])

    def _publish_state(self) -> None:
        if self.on_state is None:
            return
        try:
            self.on_state(self.snapshot())
        except Exception:                                  # noqa: BLE001
            logger.exception("SCU-LAN10 state callback failed")
