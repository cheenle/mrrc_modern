"""
Icom network-radio client — control handshake, CI-V tunnel, audio stream
========================================================================
The state machine behind "run this radio over its LAN port".  Model-agnostic
(the protocol is Icom's), it brings up three streams and hands out what they
carry: 16-bit PCM to an audio callback, raw CI-V frames to a frame callback.

**Three streams, three sockets.**  Measured on a real G7250 (2026-11-12): the
control port answers ``are you there``, but audio and CI-V only start flowing
after *their own* local sockets exist and their port numbers have been announced
in the stream request.  Sharing one socket — which is what this client used to
do — gets a successful login and then silence.  So:

===========  =========================  ================================
socket       talks to                   carries
===========  =========================  ================================
``_control``  radio control port         are-you-there, login, token,
              (50001)                    capabilities, conninfo, status
``_civ``      radio CI-V port (50002)    CI-V frames both ways
``_audio``    radio audio port (50003)   audio packets (type 0x00)
===========  =========================  ================================

Sequence (``docs/superpowers/specs/2026-11-12-icom-network-audio-design.md``)::

    are-you-there (0x03, 100 ms)  ──►   ◄── i-am-here (0x04)
                                        ◄── i-am-ready (0x06)   ← then login
    login (tok_request = random)   ──►   ◄── login reply (same tok_request)
    token (0x02)                   ──►   ◄── token accepted
                                        ◄── capabilities / conninfo (busy?)
    stream request (our ports)     ──►   ◄── status reply (radio's ports)
    open CI-V tunnel (0x16, repeated until frames arrive)
    ping 500 ms · idle 100 ms · token renewal 60 s

Failure handling is the part that matters in the field: the radio is a UDP peer
that goes away without a handshake, so every wait here has a deadline and the
client escalates back to "are you there" rather than sitting in a half-open
state.  Nothing blocks the event loop.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from backends.icom_net import protocol as proto

logger = logging.getLogger("icom.net")

#: Everything the caller may want to know about a live link.
AUDIO_CALLBACK = Callable[[bytes, int], None]      # (pcm, sample_rate)
CIV_CALLBACK = Callable[[bytes], None]             # raw CI-V frame, FE FE .. FD

#: How often the tunnel's "open data" packet is re-sent while no frame arrives.
CIV_OPEN_RETRY_S = 0.10
CIV_OPEN_GIVE_UP_S = 3.0


@dataclass
class LinkStats:
    """Observable state, for the log line that answers "why is it silent?"."""

    state: str = "idle"
    radio_name: str = ""
    radio_civ_address: int = 0
    rx_sample_rate: int = 0
    rx_codec: int = 0
    audio_packets: int = 0
    audio_bytes: int = 0
    civ_frames: int = 0
    logins: int = 0
    stream_requests: int = 0
    last_audio_at: float = 0.0
    last_civ_at: float = 0.0

    @property
    def audio_age_s(self) -> float:
        return 0.0 if not self.last_audio_at else time.monotonic() - self.last_audio_at

    @property
    def civ_age_s(self) -> float:
        return 0.0 if not self.last_civ_at else time.monotonic() - self.last_civ_at


class RadioBusy(RuntimeError):
    """The radio is in use by another client (names it in the message)."""


class LoginFailed(RuntimeError):
    """Wrong network username/password."""


def _local_port(transport) -> int:
    try:
        return int(transport.get_extra_info("sockname")[1])
    except (TypeError, ValueError, IndexError, AttributeError):
        return 0


def _endpoint(owner: "IcomNetworkClient", kind: str):
    class _Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr) -> None:
            owner._dispatch(kind, data, addr)

        def error_received(self, exc: Exception) -> None:      # pragma: no cover
            logger.debug("Icom network %s: %s", kind, exc)

    return _Proto()


class IcomNetworkClient:
    """Bring up Icom's network link and stream audio/CI-V to callbacks."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        on_audio: Optional[AUDIO_CALLBACK] = None,
        on_civ: Optional[CIV_CALLBACK] = None,
        client_name: str = "mrrc",
        control_port: int = proto.CONTROL_PORT,
        rx_codec: int = proto.CODEC_PCM16,
        rx_sample: int = 16000,
        are_you_there_period: float = proto.ARE_YOU_THERE_PERIOD_MS / 1000.0,
        ping_period: float = proto.PING_PERIOD_MS / 1000.0,
        idle_period: float = proto.IDLE_PERIOD_MS / 1000.0,
        token_renewal: float = proto.TOKEN_RENEWAL_MS / 1000.0,
        login_timeout: float = 3.0,
        #: How long the radio may stay silent before we start over.
        audio_stall_timeout: float = 15.0,
    ) -> None:
        self.host = str(host or "").strip()
        self.username = str(username or "")
        self.password = str(password or "")
        self.client_name = str(client_name or "mrrc")[:16]
        try:
            self.control_port = int(control_port)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"bad control port: {control_port!r}") from exc
        try:
            self.rx_codec = int(rx_codec)
            self.rx_sample = int(rx_sample)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"bad rx codec/rate: {rx_codec!r}/{rx_sample!r}") from exc
        self.on_audio = on_audio
        self.on_civ = on_civ
        self.are_you_there_period = are_you_there_period
        self.ping_period = ping_period
        self.idle_period = idle_period
        self.token_renewal = token_renewal
        self.login_timeout = login_timeout
        self.audio_stall_timeout = audio_stall_timeout

        self.stats = LinkStats()
        self.my_id = proto.random_client_id()
        self.remote_id = 0
        self.token = 0
        self.tok_request = 0
        self.auth_seq = 0x30
        self.civ_port = 0
        self.audio_port = 0
        self.civ_local_port = 0
        self.audio_local_port = 0

        self._control: Optional[asyncio.DatagramTransport] = None
        self._civ: Optional[asyncio.DatagramTransport] = None
        self._audio: Optional[asyncio.DatagramTransport] = None
        self._tasks: list[asyncio.Task] = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._have_radio = False
        self._have_token = False
        self._stream_asked = False
        self._last_heard = 0.0
        self._login_sent_at = 0.0
        self._civ_seq = 0
        self._civ_send_seq = 0
        self._civ_open_at = 0.0
        self._civ_saw_frame = False
        self._audio_open_at = 0.0
        self._next_idle = 0.0

    # ── lifecycle ──────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        if not self.host:
            raise ValueError("Icom network client needs a host")
        self._running = True
        self._loop = asyncio.get_running_loop()
        self._control, _ = await self._loop.create_datagram_endpoint(
            lambda: _endpoint(self, "control"), local_addr=("0.0.0.0", 0))
        self._civ, _ = await self._loop.create_datagram_endpoint(
            lambda: _endpoint(self, "civ"), local_addr=("0.0.0.0", 0))
        self._audio, _ = await self._loop.create_datagram_endpoint(
            lambda: _endpoint(self, "audio"), local_addr=("0.0.0.0", 0))
        self.civ_local_port = _local_port(self._civ)
        self.audio_local_port = _local_port(self._audio)
        self.stats.state = "discovering"
        self._tasks = [
            asyncio.create_task(self._poll_loop(), name="icom-net-poll"),
            asyncio.create_task(self._watchdog_loop(), name="icom-net-watchdog"),
        ]
        logger.info(
            "Icom network: probing %s:%d (local ports: civ %d, audio %d)",
            self.host, self.control_port, self.civ_local_port, self.audio_local_port)

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):        # noqa: BLE001
                pass
        self._tasks.clear()
        for transport in (self._control, self._civ, self._audio):
            if transport is not None:
                try:
                    transport.close()
                except Exception:                              # noqa: BLE001
                    pass
        self._control = self._civ = self._audio = None
        self.stats.state = "stopped"
        logger.info("Icom network: stopped (%d audio packets, %d CI-V frames)",
                    self.stats.audio_packets, self.stats.civ_frames)

    @property
    def connected(self) -> bool:
        return self._stream_asked and self.stats.state in ("streaming", "civ-open")

    # ── loops ──────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Are-you-there → idle/ping, the token renewal clock, and the streams."""
        last_token = 0.0
        while self._running:
            now = time.monotonic()
            try:
                if not self._have_radio:
                    self._send_control(proto.T_ARE_YOU_THERE)
                else:
                    self._send_idle()
                    self._send_ping()
                    if (self._have_token and self.token
                            and now - last_token >= self.token_renewal):
                        last_token = now
                        self._send_token(proto.REQ_TOKEN_RENEW)
                    self._pump_streams(now)
            except Exception as exc:                           # noqa: BLE001
                logger.debug("Icom network: poll error: %s", exc)
            await asyncio.sleep(self.are_you_there_period if not self._have_radio
                                else self.idle_period)

    def _pump_streams(self, now: float) -> None:
        """Keep the sub-stream sessions alive and open the CI-V tunnel.

        Measured on a real radio: the audio and CI-V ports ignore an
        ``are you there``, but they do answer *data* once our socket is the one
        announced in the stream request.  Idle packets keep the session from
        ageing out, and the CI-V tunnel needs its open packet repeated until a
        frame actually arrives.
        """
        if not self._stream_asked or not self.remote_id:
            return
        if now >= self._next_idle:
            self._next_idle = now + self.idle_period
            for transport, port in ((self._civ, self.civ_port),
                                    (self._audio, self.audio_port)):
                if transport is not None and port:
                    try:
                        transport.sendto(
                            proto.build_control(proto.T_CONTROL, seq=0,
                                                sent_id=self.my_id,
                                                rcvd_id=self.remote_id),
                            (self.host, port))
                    except Exception:                          # noqa: BLE001
                        pass
        if self.civ_port and self._civ_open_at and not self._civ_saw_frame:
            if now - self._civ_open_at <= CIV_OPEN_GIVE_UP_S:
                if now - getattr(self, "_civ_last_open", 0.0) >= CIV_OPEN_RETRY_S:
                    self._civ_last_open = now
                    self._send_civ_open()

    # ── senders ────────────────────────────────────────────────────

    def _send(self, payload: bytes) -> None:
        if self._control is not None:
            self._control.sendto(payload, (self.host, self.control_port))

    def _send_control(self, ptype: int) -> None:
        self.idle_seq = (self.idle_seq + 1) & 0xFFFF if hasattr(self, "idle_seq") else 0
        self._send(proto.build_control(ptype, seq=0, sent_id=self.my_id,
                                       rcvd_id=self.remote_id))

    def _send_idle(self) -> None:
        self._send_control(proto.T_CONTROL)

    def _send_ping(self) -> None:
        self._send(proto.build_ping(seq=0, sent_id=self.my_id,
                                    rcvd_id=self.remote_id))

    def _send_login(self) -> None:
        self._login_sent_at = time.monotonic()
        self.tok_request = proto.random_token_request()
        self._send(proto.build_login(
            sent_id=self.my_id, rcvd_id=self.remote_id, inner_seq=self.auth_seq,
            tok_request=self.tok_request, token=0, username=self.username,
            password=self.password, client_name=self.client_name))
        self.auth_seq += 1
        logger.info("Icom network: login sent as %r", self.username)

    def _send_token(self, request_type: int) -> None:
        self.auth_seq += 1
        self._send(proto.build_token(
            sent_id=self.my_id, rcvd_id=self.remote_id, inner_seq=self.auth_seq,
            tok_request=self.tok_request, token=self.token,
            request_type=request_type))

    def _send_stream_request(self) -> None:
        self.auth_seq += 1
        self.stats.stream_requests += 1
        self.stats.state = "requesting-stream"
        self._send(proto.build_stream_request(
            sent_id=self.my_id, rcvd_id=self.remote_id, inner_seq=self.auth_seq,
            tok_request=self.tok_request, token=self.token,
            client_name=self.client_name, username=self.username,
            rx_codec=self.rx_codec, rx_sample=self.rx_sample,
            civ_port=self.civ_local_port, audio_port=self.audio_local_port))
        logger.info("Icom network: stream requested (civ on %d, audio on %d)",
                    self.civ_local_port, self.audio_local_port)

    # ── CI-V tunnel ────────────────────────────────────────────────

    @property
    def civ_ready(self) -> bool:
        return bool(self.civ_port) and self.on_civ is not None

    def _next_civ_seq(self) -> int:
        self._civ_seq = (self._civ_seq + 1) & 0xFFFF
        return self._civ_seq

    def _send_civ_open(self) -> None:
        if self._civ is None or not self.civ_port:
            return
        try:
            self._civ.sendto(proto.build_civ_openclose(
                seq=self._next_civ_seq(), sent_id=self.my_id,
                rcvd_id=self.remote_id, send_seq=self._civ_send_seq),
                (self.host, self.civ_port))
        except Exception as exc:                                # noqa: BLE001
            logger.debug("Icom network: CI-V open failed: %s", exc)

    def open_civ(self) -> bool:
        """Start nudging the tunnel open (idempotent, safe to call twice)."""
        if not self.civ_port:
            logger.info("Icom network: the radio announced no CI-V port — "
                        "the CAT link stays on the serial port")
            return False
        self._civ_open_at = time.monotonic()
        self._civ_last_open = 0.0
        self._civ_saw_frame = False
        self._send_civ_open()
        return True

    def send_civ(self, frame: bytes) -> bool:
        """Ship one raw CI-V frame through the tunnel (fire-and-forget)."""
        if self._civ is None or not self.civ_port:
            return False
        try:
            self._civ.sendto(proto.build_civ_data(
                frame, seq=self._next_civ_seq(), sent_id=self.my_id,
                rcvd_id=self.remote_id, send_seq=self._civ_send_seq),
                (self.host, self.civ_port))
        except Exception as exc:                                # noqa: BLE001
            logger.debug("Icom network: CI-V send failed: %s", exc)
            return False
        self._civ_send_seq = (self._civ_send_seq + 1) & 0xFFFF
        return True

    def send_civ_threadsafe(self, frame: bytes) -> None:
        """``send_civ`` from any thread (the CAT reader/writer runs in one)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self.send_civ, frame)
        except RuntimeError:                                   # loop closed
            pass

    # ── receive ────────────────────────────────────────────────────

    def _dispatch(self, kind: str, data: bytes, addr) -> None:
        if kind == "control":
            self._on_control(data, addr)
        elif kind == "civ":
            self._on_civ(data, addr)
        else:
            self._on_audio(data, addr)

    def _on_control(self, data: bytes, addr) -> None:
        self._last_heard = time.monotonic()
        header = proto.parse_header(data)
        if header is None:
            return

        if header.length == proto.CONTROL_SIZE:
            if header.type == proto.T_I_AM_HERE and not self._have_radio:
                self._have_radio = True
                self.remote_id = header.sent_id
                self.stats.state = "discovered"
                logger.info("Icom network: radio answered (id 0x%08X)",
                            self.remote_id)
            elif header.type == proto.T_I_AM_READY:
                # Measured: the radio sends its ready **after** it hears ours,
                # and ignores a login that arrives before that exchange.
                self._send_control(proto.T_I_AM_READY)
                if not self._login_sent_at:
                    self._send_login()
            return

        if header.length == proto.PING_SIZE:
            reply = proto.parse_ping(data)
            if reply is not None and reply.reply == 0x00:      # a request to us
                self._send(proto.build_ping(seq=0, sent_id=self.my_id,
                                            rcvd_id=self.remote_id,
                                            time_ms=reply.uptime_ms))
            return

        if header.length == proto.LOGIN_RESPONSE_SIZE:
            reply = proto.parse_login_reply(data)
            if reply is None:
                return
            self.stats.logins += 1
            if reply.error == proto.ERROR_BAD_CREDENTIALS:
                raise LoginFailed("radio rejected the network username/password")
            if reply.error not in (proto.ERROR_NONE,):
                raise LoginFailed(f"login failed (0x{reply.error:08X})")
            self.token = reply.token
            logger.info("Icom network: logged in (connection %r)", reply.connection)
            self._send_token(proto.REQ_TOKEN)
            return

        if header.length == proto.TOKEN_SIZE:
            reply = proto.parse_token_reply(data)
            if reply is None:
                return
            if reply.response == proto.ERROR_BAD_CREDENTIALS:
                raise LoginFailed("token rejected (bad credentials)")
            if reply.request_type in (proto.REQ_TOKEN, proto.REQ_TOKEN_RENEW):
                self._have_token = True
                self.stats.state = "token"
                logger.info("Icom network: token accepted")
            return

        if header.length == proto.CONNINFO_SIZE:
            status = proto.parse_conninfo(data)
            if status is None:
                return
            if status.busy:
                raise RadioBusy(
                    f"radio is in use by {status.computer or 'another client'}")
            if self._have_token and not self._stream_asked:
                self._send_stream_request()
            return

        if header.length == proto.STATUS_SIZE:
            status = proto.parse_stream_status(data)
            if status is None:
                return
            if status.ok:
                first = not self._stream_asked
                self._stream_asked = True
                self.stats.state = "streaming"
                try:
                    self.civ_port = int(status.civ_port or 0)
                    self.audio_port = int(status.audio_port or 0)
                except (TypeError, ValueError):
                    self.civ_port = self.audio_port = 0
                logger.info(
                    "Icom network: stream open — radio CI-V port %d, audio port %d",
                    self.civ_port, self.audio_port)
                if first:
                    self._audio_open_at = time.monotonic()
                    if self.civ_port and self.on_civ is not None:
                        self.open_civ()
            elif status.disconnected:
                logger.warning("Icom network: radio says it disconnected us")
                self._reset_link()
            return

        if header.length >= proto.CAPABILITIES_HEADER_SIZE:
            caps = proto.parse_capabilities(data)
            if caps:
                radio = caps[0]
                self.stats.radio_name = radio.name
                self.stats.radio_civ_address = radio.civ_address
                self.stats.rx_sample_rate = radio.rx_sample_rate or self.rx_sample
                self.stats.rx_codec = radio.enable_tx_codec or self.rx_codec
                logger.info(
                    "Icom network: radio %r (audio %r, CI-V 0x%02X, %d Hz)",
                    radio.name, radio.audio, radio.civ_address,
                    self.stats.rx_sample_rate)
            return

    def _on_civ(self, data: bytes, addr) -> None:
        frame = proto.parse_civ_data(data) if proto.looks_like_civ(data) else None
        if not frame:
            return
        self._civ_saw_frame = True
        self.stats.civ_frames += 1
        self.stats.last_civ_at = time.monotonic()
        if self.on_civ is not None:
            try:
                self.on_civ(frame)
            except Exception as exc:                           # noqa: BLE001
                logger.warning("Icom network: CI-V callback failed: %s", exc)

    def _on_audio(self, data: bytes, addr) -> None:
        pkt = proto.parse_audio(data)
        if pkt is None or not pkt.payload:
            return
        self.stats.audio_packets += 1
        self.stats.audio_bytes += len(pkt.payload)
        self.stats.last_audio_at = time.monotonic()
        if self.stats.state != "streaming":
            self.stats.state = "streaming"
        if self.on_audio is not None:
            try:
                self.on_audio(pkt.payload, self.stats.rx_sample_rate or self.rx_sample)
            except Exception as exc:                           # noqa: BLE001
                logger.warning("Icom network: audio callback failed: %s", exc)

    # ── watchdog ───────────────────────────────────────────────────

    async def _watchdog_loop(self) -> None:
        """Restart the handshake when the audio goes quiet for too long."""
        while self._running:
            await asyncio.sleep(1.0)
            if not self._stream_asked:
                continue
            age = self.stats.audio_age_s
            if age and age > self.audio_stall_timeout:
                logger.warning(
                    "Icom network: no audio for %.0f s — restarting the session",
                    age)
                self._reset_link()

    def _reset_link(self) -> None:
        self._have_radio = False
        self._have_token = False
        self._stream_asked = False
        self._civ_open_at = 0.0
        self._civ_saw_frame = False
        self.civ_port = self.audio_port = 0
        self.stats.state = "discovering"
