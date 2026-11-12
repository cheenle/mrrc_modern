"""
Icom network-radio protocol — packet codecs (pure)
==================================================
The G7250's LAN port speaks **Icom's network-radio protocol** (the one wfview
drives and the Icom RS-BA1 software speaks): three UDP streams — control,
CI-V tunnel, audio — with a login/token handshake in front.

This module is the byte layer and nothing else: no sockets, no asyncio, no
timers.  Every layout below was read off wfview's public implementation
(GPL-3.0) and re-expressed here from the protocol facts — field offsets, type
codes and handshake order are interface facts; no wfview code was copied (this
repository has no GPL declaration, see the design record:
``docs/superpowers/specs/2026-11-12-icom-network-audio-design.md``).

Conventions, learned the hard way from the C structs:

* The first 16 bytes are **little-endian** everywhere: ``len:u32 @0``,
  ``type:u16 @4``, ``seq:u16 @6``, ``sentid:u32 @8``, ``rcvdid:u32 @12``.
* Fields the original marks ``qToBigEndian`` are **big-endian** and say ``BE``
  in the layouts below.  Mixing them up produces a packet the radio silently
  ignores — the failure mode this whole module exists to avoid.
* ``sentid`` is *our* random 32-bit id; ``rcvdid`` is the radio's, learned from
  ``sentid`` of its first reply.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Optional

# ── Sizes (the original's *_SIZE constants) ────────────────────────
CONTROL_SIZE = 0x10
PING_SIZE = 0x15
TOKEN_SIZE = 0x40
STATUS_SIZE = 0x50
LOGIN_RESPONSE_SIZE = 0x60
LOGIN_SIZE = 0x80
CONNINFO_SIZE = 0x90
CAPABILITIES_HEADER_SIZE = 0x42
RADIO_CAP_SIZE = 0x64
AUDIO_HEADER_SIZE = 0x18

#: The radio's control-channel port (the CI-V and audio ports are told to us
#: in the stream-status reply).
CONTROL_PORT = 50001

# ── Packet types ───────────────────────────────────────────────────
T_CONTROL = 0x00            # idle / keep-alive
T_LOGIN = 0x01              # also token, capabilities, conninfo, status, audio
T_ARE_YOU_THERE = 0x03
T_I_AM_HERE = 0x04
T_I_AM_READY = 0x06
T_PING = 0x07
T_DISCONNECT = 0x05

#: Control-packet ``type`` values that carry a meaning (all on the control port).
CONTROL_TYPES = {
    T_CONTROL: "idle",
    T_ARE_YOU_THERE: "are-you-there",
    T_I_AM_HERE: "i-am-here",
    T_I_AM_READY: "i-am-ready",
    T_DISCONNECT: "disconnect",
}

#: ``requesttype`` in login / token / stream-request packets.
REQ_LOGIN = 0x00
REQ_TOKEN = 0x02
REQ_TOKEN_RENEW = 0x05
REQ_STREAM = 0x03

#: Audio codec numbers (the radio's own vocabulary).
CODEC_PCM8 = 0x01           # 8-bit linear
CODEC_PCM16 = 0x04          # 16-bit linear — what the G7250 offers at 16 kHz
CODEC_ULAW = 0x08
CODEC_OPUS = 0x40

#: Login failure marker in the login response / status ``error`` field.
ERROR_BAD_CREDENTIALS = 0xFEFFFFFF
ERROR_FAILED = 0xFFFFFFFF
ERROR_NONE = 0x00000000

#: Timings from the original (milliseconds).
ARE_YOU_THERE_PERIOD_MS = 100
IDLE_PERIOD_MS = 100
PING_PERIOD_MS = 500
TOKEN_RENEWAL_MS = 60_000

#: Substitution table for the username/password obfuscation.  A protocol
#: constant: printable ASCII maps through it after a per-character offset.
_PASSCODE = bytes([
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x47, 0x5D, 0x4C, 0x42, 0x66, 0x20, 0x23, 0x46,
    0x4E, 0x57, 0x45, 0x3D, 0x67, 0x76, 0x60, 0x41,
    0x62, 0x39, 0x59, 0x2D, 0x68, 0x7E, 0x7C, 0x65,
    0x7D, 0x49, 0x29, 0x72, 0x73, 0x78, 0x21, 0x6E,
    0x5A, 0x5E, 0x4A, 0x3E, 0x71, 0x2C, 0x2A, 0x54,
    0x3C, 0x3A, 0x63, 0x4F, 0x43, 0x75, 0x27, 0x79,
    0x5B, 0x35, 0x70, 0x48, 0x6B, 0x56, 0x6F, 0x34,
    0x32, 0x6C, 0x30, 0x61, 0x6D, 0x7B, 0x2F, 0x4B,
    0x64, 0x38, 0x2B, 0x2E, 0x50, 0x40, 0x3F, 0x55,
    0x33, 0x37, 0x25, 0x77, 0x24, 0x26, 0x74, 0x6A,
    0x28, 0x53, 0x4D, 0x69, 0x22, 0x5C, 0x44, 0x31,
    0x36, 0x58, 0x3B, 0x7A, 0x51, 0x5F, 0x52,
])


def passcode(text: str, *, length: int = 16) -> bytes:
    """Obfuscate a username/password for the login and stream-request packets.

    Not encryption — a fixed substitution after a per-character offset, which is
    exactly what the radio expects.  Truncated to ``length`` bytes like the
    original (the fields are fixed-width).
    """
    out = bytearray()
    raw = str(text or "").encode("utf-8")
    for i, ch in enumerate(raw[:length]):
        p = ch + i
        if p > 126:
            p = 32 + p % 127
        out.append(_PASSCODE[p] if p < len(_PASSCODE) else 0)
    return bytes(out)


def _fixed(text, size: int) -> bytes:
    """A NUL-padded fixed-width field (str is UTF-8 encoded, bytes pass through)."""
    raw = text if isinstance(text, (bytes, bytearray)) else str(text or "").encode("utf-8")
    raw = bytes(raw)[:size]
    return raw + b"\x00" * (size - len(raw))


def random_client_id() -> int:
    """Our 32-bit ``sentid`` — any value the radio will echo back to us."""
    return struct.unpack("<I", os.urandom(4))[0] or 0x1234


def random_token_request() -> int:
    """The 16-bit ``tokrequest`` that ties a login to its answer."""
    return struct.unpack("<H", os.urandom(2))[0] or 0x4242


@dataclass(frozen=True)
class Header:
    """The 16-byte header every packet starts with."""

    length: int
    type: int
    seq: int
    sent_id: int
    rcvd_id: int


def parse_header(data: bytes) -> Optional[Header]:
    """Read the common header, or None when the datagram is too short."""
    if len(data) < CONTROL_SIZE:
        return None
    length, ptype, seq, sent, rcvd = struct.unpack("<IHHII", data[:CONTROL_SIZE])
    return Header(length, ptype, seq, sent, rcvd)


# ── Control plane ──────────────────────────────────────────────────

def build_control(ptype: int, *, seq: int, sent_id: int,
                  rcvd_id: int) -> bytes:
    """Idle / are-you-there / i-am-here / i-am-ready are all 16-byte packets."""
    return struct.pack("<IHHII", CONTROL_SIZE, ptype, seq, sent_id, rcvd_id)


def build_ping(*, seq: int, sent_id: int, rcvd_id: int,
               time_ms: int = 0) -> bytes:
    """The 500 ms liveness ping (type 0x07)."""
    # len, type, seq, sentid, rcvdid, reply(0x00 = request), time
    return struct.pack("<IHHIIBI", PING_SIZE, T_PING, seq, sent_id, rcvd_id,
                       0x00, time_ms & 0xFFFFFFFF)


@dataclass(frozen=True)
class PingReply:
    sent_id: int
    rcvd_id: int
    seq: int
    reply: int
    uptime_ms: int


def parse_ping(data: bytes) -> Optional[PingReply]:
    if len(data) < PING_SIZE:
        return None
    _len, ptype, seq, sent, rcvd = struct.unpack("<IHHII", data[:CONTROL_SIZE])
    if ptype != T_PING:
        return None
    reply = data[0x10]
    (uptime,) = struct.unpack("<I", data[0x11:0x15])
    return PingReply(sent, rcvd, seq, reply, uptime)


# ── Login ──────────────────────────────────────────────────────────

def build_login(*, sent_id: int, rcvd_id: int, inner_seq: int,
                tok_request: int, token: int, username: str, password: str,
                client_name: str) -> bytes:
    """The 0x80-byte login packet (requesttype 0x00)."""
    buf = bytearray(LOGIN_SIZE)
    struct.pack_into("<IHHII", buf, 0, LOGIN_SIZE, T_LOGIN, 0, sent_id, rcvd_id)
    struct.pack_into(">I", buf, 0x10, LOGIN_SIZE - 0x10)   # payloadsize (BE)
    buf[0x14] = 0x01                                       # requestreply
    buf[0x15] = REQ_LOGIN                                  # requesttype
    struct.pack_into(">H", buf, 0x16, inner_seq & 0xFFFF)  # innerseq (BE)
    struct.pack_into("<H", buf, 0x1A, tok_request & 0xFFFF)
    struct.pack_into("<I", buf, 0x1C, token & 0xFFFFFFFF)
    buf[0x40:0x50] = _fixed(passcode(username), 16)
    buf[0x50:0x60] = _fixed(passcode(password), 16)
    buf[0x60:0x70] = _fixed(client_name, 16)
    return bytes(buf)


@dataclass(frozen=True)
class LoginReply:
    sent_id: int
    rcvd_id: int
    tok_request: int
    token: int
    error: int
    connection: str

    @property
    def bad_credentials(self) -> bool:
        return self.error == ERROR_BAD_CREDENTIALS

    @property
    def ok(self) -> bool:
        return self.error in (0x00000000, ERROR_BAD_CREDENTIALS)

    @property
    def connection_type(self) -> str:
        return self.connection


def parse_login_reply(data: bytes) -> Optional[LoginReply]:
    if len(data) < LOGIN_RESPONSE_SIZE:
        return None
    _len, ptype, _seq, sent, rcvd = struct.unpack("<IHHII", data[:CONTROL_SIZE])
    if ptype != T_LOGIN:
        return None
    (tok_req,) = struct.unpack("<H", data[0x1A:0x1C])
    (token,) = struct.unpack("<I", data[0x1C:0x20])
    (error,) = struct.unpack("<i", data[0x30:0x34])
    conn = data[0x40:0x50].split(b"\x00", 1)[0].decode("ascii", "replace")
    return LoginReply(sent, rcvd, tok_req, token, error & 0xFFFFFFFF, conn)


# ── Token ──────────────────────────────────────────────────────────

def build_token(*, sent_id: int, rcvd_id: int, inner_seq: int,
                tok_request: int, token: int, request_type: int = REQ_TOKEN,
                reset_cap: int = 0x0798) -> bytes:
    """Token request (0x02 first time, 0x05 renewal)."""
    buf = bytearray(TOKEN_SIZE)
    struct.pack_into("<IHHII", buf, 0, TOKEN_SIZE, T_LOGIN, 0, sent_id, rcvd_id)
    struct.pack_into(">I", buf, 0x10, TOKEN_SIZE - 0x10)
    buf[0x14] = 0x01
    buf[0x15] = request_type & 0xFF
    struct.pack_into(">H", buf, 0x16, inner_seq & 0xFFFF)
    struct.pack_into("<H", buf, 0x1A, tok_request & 0xFFFF)
    struct.pack_into("<I", buf, 0x1C, token & 0xFFFFFFFF)
    struct.pack_into(">H", buf, 0x24, reset_cap & 0xFFFF)
    return bytes(buf)


@dataclass(frozen=True)
class TokenReply:
    sent_id: int
    rcvd_id: int
    request_type: int
    reply: int
    response: int

    @property
    def accepted(self) -> bool:
        return self.response == 0x00000000

    @property
    def rejected(self) -> bool:
        return self.response == 0xFFFFFFFF


def parse_token_reply(data: bytes) -> Optional[TokenReply]:
    if len(data) < TOKEN_SIZE:
        return None
    _len, _ptype, _seq, sent, rcvd = struct.unpack("<IHHII", data[:CONTROL_SIZE])
    (resp,) = struct.unpack("<i", data[0x30:0x34])
    return TokenReply(sent, rcvd, data[0x15], data[0x14], resp & 0xFFFFFFFF)


# ── Capabilities ───────────────────────────────────────────────────

@dataclass(frozen=True)
class RadioCapability:
    name: str
    audio: str
    connection_type: int
    civ_address: int
    rx_sample_rate: int
    tx_sample_rate: int
    enable_rx: int
    enable_tx: int
    enable_tx_codec: int
    baud_rate: int
    capf: int
    guid: bytes = b""


def parse_capabilities(data: bytes) -> list[RadioCapability]:
    """Parse the radio list (each entry 0x64 bytes after the 0x42 header)."""
    if len(data) < CAPABILITIES_HEADER_SIZE:
        return []
    (count,) = struct.unpack(">H", data[0x40:0x42])
    out: list[RadioCapability] = []
    base = CAPABILITIES_HEADER_SIZE
    for i in range(count):
        off = base + i * RADIO_CAP_SIZE
        chunk = data[off:off + RADIO_CAP_SIZE]
        if len(chunk) < RADIO_CAP_SIZE:
            break

        def text(field_off: int, size: int) -> str:
            raw = chunk[field_off:field_off + size]
            return raw.split(b"\x00", 1)[0].decode("ascii", "replace")

        guid = chunk[0x00:0x10]
        (common_cap,) = struct.unpack(">H", chunk[0x07:0x09])
        (conn_type,) = struct.unpack(">H", chunk[0x50:0x52])
        (rxsample,) = struct.unpack(">H", chunk[0x53:0x55])
        (txsample,) = struct.unpack(">H", chunk[0x55:0x57])
        (baud,) = struct.unpack(">I", chunk[0x5A:0x5E])
        (capf,) = struct.unpack(">H", chunk[0x5E:0x60])
        out.append(RadioCapability(
            name=text(0x10, 32),
            audio=text(0x30, 32),
            connection_type=conn_type,
            civ_address=chunk[0x52],
            rx_sample_rate=rxsample,
            tx_sample_rate=txsample,
            enable_rx=chunk[0x57],
            enable_tx=chunk[0x58],
            enable_tx_codec=chunk[0x59],
            baud_rate=baud,
            capf=capf,
            guid=guid + struct.pack(">H", common_cap),
        ))
    return out


# ── Connection status / stream request ─────────────────────────────

@dataclass(frozen=True)
class ConnectionStatus:
    sent_id: int
    rcvd_id: int
    seq: int
    busy: int
    computer: str
    ip_address: str


def parse_conninfo(data: bytes) -> Optional[ConnectionStatus]:
    """The radio's "who has this radio" packet (0x90, receive direction)."""
    if len(data) < CONNINFO_SIZE:
        return None
    _len, ptype, seq, sent, rcvd = struct.unpack("<IHHII", data[:CONTROL_SIZE])
    if ptype != T_LOGIN:
        return None
    (busy,) = struct.unpack("<I", data[0x60:0x64])
    computer = data[0x64:0x74].split(b"\x00", 1)[0].decode("ascii", "replace")
    (ip,) = struct.unpack("<I", data[0x84:0x88])
    ip_text = ".".join(str((ip >> s) & 0xFF) for s in (0, 8, 16, 24))
    return ConnectionStatus(sent, rcvd, seq, busy, computer, ip_text)


def build_stream_request(*, sent_id: int, rcvd_id: int, inner_seq: int,
                         tok_request: int, token: int, client_name: str,
                         username: str, rx_codec: int, rx_sample: int,
                         civ_port: int, audio_port: int,
                         tx_codec: int = 0, tx_sample: int = 0,
                         tx_buffer: int = 0,
                         common_cap: int = 0x8010,
                         mac_address: bytes = b"\x00" * 6) -> bytes:
    """Ask for the CI-V + audio streams, announcing our local ports."""
    buf = bytearray(CONNINFO_SIZE)
    struct.pack_into("<IHHII", buf, 0, CONNINFO_SIZE, T_LOGIN, 0, sent_id, rcvd_id)
    struct.pack_into(">I", buf, 0x10, CONNINFO_SIZE - 0x10)
    buf[0x14] = 0x01
    buf[0x15] = REQ_STREAM
    struct.pack_into(">H", buf, 0x16, inner_seq & 0xFFFF)
    struct.pack_into("<H", buf, 0x1A, tok_request & 0xFFFF)
    struct.pack_into("<I", buf, 0x1C, token & 0xFFFFFFFF)
    struct.pack_into(">H", buf, 0x27, common_cap & 0xFFFF)
    buf[0x2A:0x30] = (mac_address or b"\x00" * 6)[:6]
    buf[0x40:0x60] = _fixed(client_name, 32)
    buf[0x60:0x70] = _fixed(passcode(username), 16)
    buf[0x70] = 0x01                                    # rxenable
    if tx_codec:
        buf[0x71] = 0x01                                # txenable
    buf[0x72] = rx_codec & 0xFF
    buf[0x73] = tx_codec & 0xFF
    struct.pack_into(">I", buf, 0x74, rx_sample & 0xFFFFFFFF)
    struct.pack_into(">I", buf, 0x78, tx_sample & 0xFFFFFFFF)
    struct.pack_into(">I", buf, 0x7C, civ_port & 0xFFFFFFFF)
    struct.pack_into(">I", buf, 0x80, audio_port & 0xFFFFFFFF)
    struct.pack_into(">I", buf, 0x84, tx_buffer & 0xFFFFFFFF)
    buf[0x88] = 0x01                                    # convert
    return bytes(buf)


@dataclass(frozen=True)
class StreamStatus:
    """The radio's answer: which ports it opened for us."""

    sent_id: int
    rcvd_id: int
    error: int
    disconnected: int
    civ_port: int
    audio_port: int

    @property
    def ok(self) -> bool:
        return self.error == ERROR_NONE and not self.disconnected


def parse_stream_status(data: bytes) -> Optional[StreamStatus]:
    if len(data) < STATUS_SIZE:
        return None
    _len, _ptype, _seq, sent, rcvd = struct.unpack("<IHHII", data[:CONTROL_SIZE])
    (error,) = struct.unpack("<i", data[0x30:0x34])
    disc = data[0x40]
    (civ,) = struct.unpack(">I", data[0x42:0x46])
    (audio,) = struct.unpack(">I", data[0x46:0x4A])
    return StreamStatus(sent, rcvd, error & 0xFFFFFFFF, disc, civ, audio)


# ── Audio ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AudioPacket:
    """One received audio datagram: 0x18-byte header + PCM payload."""

    sent_id: int
    rcvd_id: int
    seq: int
    ident: int
    send_seq: int
    declared_len: int
    payload: bytes

    @property
    def missing_tail(self) -> int:
        """How many payload bytes the header promised but did not deliver."""
        return max(0, self.declared_len - len(self.payload))


def parse_audio(data: bytes) -> Optional[AudioPacket]:
    """Split an audio datagram (control ``type != 0x01`` ones are not audio)."""
    if len(data) < AUDIO_HEADER_SIZE:
        return None
    _len, ptype, seq, sent, rcvd = struct.unpack("<IHHII", data[:CONTROL_SIZE])
    if ptype == 0x01:            # a control/status packet, not audio
        return None
    (ident,) = struct.unpack("<H", data[0x10:0x12])
    (send_seq,) = struct.unpack("<H", data[0x12:0x14])
    (datalen,) = struct.unpack(">H", data[0x16:0x18])
    return AudioPacket(sent, rcvd, seq, ident, send_seq, datalen,
                       data[AUDIO_HEADER_SIZE:])


def pcm16_from_audio(pkt: AudioPacket) -> bytes:
    """The PCM payload as-is (16-bit little-endian mono at the negotiated rate)."""
    return pkt.payload
