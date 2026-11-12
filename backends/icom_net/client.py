"""
Icom network-radio client — control handshake + audio stream
============================================================
The state machine behind "get RX audio off the radio's LAN port".  It is
model-agnostic (the protocol is Icom's), and it deliberately does **one** thing:
bring up the control channel, ask for an audio stream, and hand 16-bit PCM at
the negotiated rate to a callback.  No decoding of CI-V, no transmit.

Sequence (``docs/superpowers/specs/2026-11-12-icom-network-audio-design.md`` §3)::

    are-you-there (0x03, 100 ms)  ──►   ◄── i-am-here (0x04)
                                        ◄── i-am-ready (0x06)
    login (tok_request = random)   ──►   ◄── login reply (same tok_request)
    token (0x02)                   ──►   ◄── token accepted
                                        ◄── capabilities / conninfo (busy?)
    stream request (our ports)     ──►   ◄── status reply (radio's ports)
    ping 500 ms · idle 100 ms · token renewal 60 s; audio arrives meanwhile

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
from typing import Awaitable, Callable, Optional

from backends.icom_net import protocol as proto

logger = logging.getLogger("icom.net")

#: Everything the caller may want to know about a live link.
AUDIO_CALLBACK = Callable[[bytes, int], None]      # (pcm, sample_rate)


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
    logins: int = 0
    stream_requests: int = 0
    last_audio_at: float = 0.0

    @property
    def audio_age_s(self) -> float:
        return 0.0 if not self.last_audio_at else time.monotonic() - self.last_audio_at


class RadioBusy(RuntimeError):
    """Another PC holds the radio (the radio says so in its conninfo packet)."""


class LoginFailed(RuntimeError):
    """The radio rejected the network username/password."""


class _ControlProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "IcomNetworkClient"):
        self._owner = owner

    def datagram_received(self, data: bytes, addr) -> None:
        self._owner._on_control(data, addr)

    def error_received(self, exc: Exception) -> None:      # pragma: no cover
        logger.debug("icom net: control socket error: %s", exc)


class _AudioProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "IcomNetworkClient"):
        self._owner = owner

    def datagram_received(self, data: bytes, addr) -> None:
        self._owner._on_audio(data, addr)

    def error_received(self, exc: Exception) -> None:      # pragma: no cover
        logger.debug("icom net: audio socket error: %s", exc)


class IcomNetworkClient:
    """Bring up Icom's network link and stream RX audio to ``on_audio``."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        on_audio: Optional[AUDIO_CALLBACK] = None,
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
        self.rx_codec = int(rx_codec)
        self.rx_sample = int(rx_sample)
        self.on_audio = on_audio
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
        self.send_seq = 0
        self.idle_seq = 0

        self._control: Optional[asyncio.DatagramTransport] = None
        self._audio: Optional[asyncio.DatagramTransport] = None
        self._audio_port = 0
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._have_radio = False
        self._have_token = False
        self._stream_asked = False
        self._last_heard = 0.0
        self._login_sent_at = 0.0
        # CI-V tunnel (port 50002): opened after the stream comes up, and only
        # ever used when a caller asks for it (the audio path does not need it).
        self.on_civ: Optional[Callable[[bytes], None]] = None
        self.civ_port = 0
        self.radio_audio_port = 0
        self._civ_seq = 0
        self._civ_send_seq = 0
        self._civ_opened_at = 0.0
        self._civ_saw_data = False
        self.civ_packets = 0

    # ── lifecycle ──────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        if not self.host:
            raise ValueError("Icom network client needs a host")
        self._running = True
        loop = asyncio.get_running_loop()
        control, _ = await loop.create_datagram_endpoint(
            lambda: _ControlProtocol(self), local_addr=("0.0.0.0", 0))
        self._control = control
        audio, _ = await loop.create_datagram_endpoint(
            lambda: _AudioProtocol(self), local_addr=("0.0.0.0", 0))
        self._audio = audio
        self._audio_port = int(audio.get_extra_info("sockname")[1])
        self.stats.state = "discovering"
        self._tasks = [
            asyncio.create_task(self._poll_loop(), name="icom-net-poll"),
            asyncio.create_task(self._watchdog_loop(), name="icom-net-watchdog"),
        ]
        logger.info("Icom network: probing %s:%d (control from %s, audio on %d)",
                    self.host, self.control_port,
                    self._control.get_extra_info("sockname"),
                    self._audio_port)

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
        for transport in (self._control, self._audio):
            if transport is not None:
                try:
                    transport.close()
                except Exception:                              # noqa: BLE001
                    pass
        self._control = self._audio = None
        self.stats.state = "stopped"
        logger.info("Icom network: stopped (%d audio packets, %d bytes)",
                    self.stats.audio_packets, self.stats.audio_bytes)

    @property
    def connected(self) -> bool:
        return self._stream_asked and self.stats.state == "streaming"

    @property
    def audio_stalled(self) -> bool:
        return (self.stats.audio_packets == 0
                or self.stats.audio_age_s > self.audio_stall_timeout)

    # ── timers ─────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Are-you-there → ping/idle, plus the token renewal clock."""
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
                    self._civ_open_tick()
            except Exception as exc:                           # noqa: BLE001
                logger.debug("Icom network: poll error: %s", exc)
            await asyncio.sleep(self.are_you_there_period if not self._have_radio
                                else self.idle_period)

    async def _watchdog_loop(self) -> None:
        """Restart the handshake when the radio stops answering."""
        while self._running:
            await asyncio.sleep(1.0)
            if not self._have_radio:
                continue
            silence = time.monotonic() - (self._last_heard or time.monotonic())
            if silence > self.audio_stall_timeout and self._stream_asked:
                lost = self.stats.audio_packets == 0 or self.stats.audio_age_s > self.audio_stall_timeout
                if lost:
                    logger.warning(
                        "Icom network: no audio for %.0f s (control silent %.0f s) "
                        "— re-running the handshake", self.stats.audio_age_s, silence)
                    self._reset_link()

    def _reset_link(self) -> None:
        self._have_radio = False
        self._have_token = False
        self._stream_asked = False
        self.remote_id = 0
        self.token = 0
        self.stats.state = "discovering"

    # ── packet senders ─────────────────────────────────────────────

    def _send(self, payload: bytes) -> None:
        if self._control is not None:
            self._control.sendto(payload, (self.host, self.control_port))

    def _send_control(self, ptype: int) -> None:
        self.idle_seq = (self.idle_seq + 1) & 0xFFFF
        self._send(proto.build_control(ptype, seq=self.idle_seq,
                                       sent_id=self.my_id, rcvd_id=self.remote_id))

    def _send_idle(self) -> None:
        self._send_control(proto.T_CONTROL)

    def _send_ping(self) -> None:
        self.send_seq = (self.send_seq + 1) & 0xFFFF
        self._send(proto.build_ping(seq=self.send_seq, sent_id=self.my_id,
                                    rcvd_id=self.remote_id,
                                    time_ms=self.send_seq))

    def _send_login(self) -> None:
        self.tok_request = proto.random_token_request()
        self.auth_seq += 1
        self._login_sent_at = time.monotonic()
        self.stats.logins += 1
        self.stats.state = "logging-in"
        self._send(proto.build_login(
            sent_id=self.my_id, rcvd_id=self.remote_id,
            inner_seq=self.auth_seq, tok_request=self.tok_request,
            token=self.token, username=self.username, password=self.password,
            client_name=self.client_name))
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
            civ_port=self._audio_port, audio_port=self._audio_port))
        logger.info("Icom network: stream requested (audio on local %d)",
                    self._audio_port)

    # ── CI-V tunnel (port 50002) ───────────────────────────────────

    @property
    def civ_ready(self) -> bool:
        return bool(self.civ_port) and bool(self.on_civ is not None)

    def _next_civ_seq(self) -> int:
        self._civ_seq = (self._civ_seq + 1) & 0xFFFF
        return self._civ_seq

    async def open_civ(self) -> bool:
        """Ask the radio to start relaying CI-V on its tunnel port.

        The stream reply tells us the port; the radio then needs an explicit
        "open data" packet, and it repeats it until data actually arrives (the
        published implementation re-sends every 100 ms).  Returns False when the
        radio never told us a port.
        """
        if not self.civ_port:
            logger.info("Icom network: the radio announced no CI-V port — "
                        "the CAT link stays on the serial port")
            return False
        self._civ_opened_at = time.monotonic()
        self._civ_saw_data = False
        self._send_civ_openclose(close=False)
        return True

    def _send_civ_openclose(self, *, close: bool) -> None:
        if self._audio is None or not self.civ_port:
            return
        try:
            self._audio.sendto(proto.build_civ_openclose(
                seq=self._next_civ_seq(), sent_id=self.my_id,
                rcvd_id=self.remote_id, send_seq=self._civ_send_seq,
                close=close), (self.host, self.civ_port))
        except Exception as exc:                                # noqa: BLE001
            logger.debug("Icom network: CI-V open/close failed: %s", exc)

    def send_civ(self, frame: bytes) -> bool:
        """Ship one raw CI-V frame through the tunnel (fire-and-forget)."""
        if self._audio is None or not self.civ_port:
            return False
        try:
            self._audio.sendto(proto.build_civ_data(
                frame, seq=self._next_civ_seq(), sent_id=self.my_id,
                rcvd_id=self.remote_id, send_seq=self._civ_send_seq),
                (self.host, self.civ_port))
        except Exception as exc:                                # noqa: BLE001
            logger.debug("Icom network: CI-V send failed: %s", exc)
            return False
        self._civ_send_seq = (self._civ_send_seq + 1) & 0xFFFF
        return True

    # ── receive ────────────────────────────────────────────────────

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
                logger.info("Icom network: radio answered (id 0x%08X)", self.remote_id)
            elif header.type == proto.T_I_AM_READY and not self._have_token:
                # The radio offers itself: log in unless a login is in flight.
                if (not self._login_sent_at
                        or time.monotonic() - self._login_sent_at > self.login_timeout):
                    self._send_login()
            return

        if header.length == proto.PING_SIZE:
            return                                    # liveness only

        if header.length == proto.LOGIN_RESPONSE_SIZE:
            reply = proto.parse_login_reply(data)
            if reply is None or reply.tok_request != self.tok_request:
                return
            if reply.bad_credentials:
                raise LoginFailed(
                    "the radio rejected the network username/password "
                    f"({self.username!r})")
            self.token = reply.token
            self.remote_id = reply.sent_id
            logger.info("Icom network: logged in (connection %r)",
                        reply.connection_type)
            self._send_token(proto.REQ_TOKEN)
            return

        if header.length == proto.TOKEN_SIZE:
            reply = proto.parse_token_reply(data)
            if reply is None:
                return
            if reply.request_type == proto.REQ_TOKEN and reply.accepted:
                self._have_token = True
                self.stats.state = "ready"
                logger.info("Icom network: token accepted")
            elif reply.request_type == proto.REQ_TOKEN_RENEW:
                if reply.rejected:
                    logger.info("Icom network: token rejected, logging in again")
                    self._have_token = False
                    self._send_login()
            return

        if header.length == proto.CONNINFO_SIZE:
            info = proto.parse_conninfo(data)
            if info is None:
                return
            if info.busy and info.computer and info.computer != self.client_name:
                raise RadioBusy(
                    f"{self.stats.radio_name or 'the radio'} is in use by "
                    f"{info.computer} ({info.ip_address})")
            if self._have_token and not self._stream_asked:
                self._send_stream_request()
            return

        if header.length == proto.STATUS_SIZE:
            status = proto.parse_stream_status(data)
            if status is None:
                return
            if status.ok:
                self._stream_asked = True
                self.stats.state = "streaming"
                try:
                    self.civ_port = int(status.civ_port or 0)
                except (TypeError, ValueError):
                    self.civ_port = 0
                try:
                    self.radio_audio_port = int(status.audio_port or 0)
                except (TypeError, ValueError):
                    self.radio_audio_port = 0
                logger.info(
                    "Icom network: stream open — radio CI-V port %d, audio port %d",
                    status.civ_port, status.audio_port)
                if self.civ_port and self.on_civ is not None:
                    self._civ_opened_at = time.monotonic()
                    self._civ_saw_data = False
                    self._send_civ_openclose(close=False)
            elif status.disconnected:
                logger.warning("Icom network: radio says it disconnected us")
                self._reset_link()
            return

        if header.length == proto.CAPABILITIES_HEADER_SIZE + proto.RADIO_CAP_SIZE * 1 \
                or header.length >= proto.CAPABILITIES_HEADER_SIZE:
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

    def _on_audio(self, data: bytes, addr) -> None:
        # Both streams are bound to this one local socket, so a CI-V datagram is
        # demultiplexed here before the audio parser ever sees it.
        if proto.looks_like_civ(data):
            frame = proto.parse_civ_data(data)
            if frame:
                self._on_civ_data(frame)
                return
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

    def _on_civ_data(self, frame: bytes) -> None:
        self.civ_packets += 1
        self._civ_saw_data = True
        if self.on_civ is not None:
            try:
                self.on_civ(frame)
            except Exception as exc:                           # noqa: BLE001
                logger.warning("Icom network: CI-V callback failed: %s", exc)

    def _civ_open_tick(self) -> None:
        """Re-send the open packet while the tunnel is silent (called by the loop)."""
        if not self.civ_port or self._civ_saw_data or not self._civ_opened_at:
            return
        if time.monotonic() - self._civ_opened_at < 3.0:
            self._send_civ_openclose(close=False)
            return
        if int((time.monotonic() - self._civ_opened_at) * 10) % 300 == 0:
            logger.debug("Icom network: CI-V tunnel silent after open "
                         "(port %d)", self.civ_port)
