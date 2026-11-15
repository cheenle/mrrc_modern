"""
Yaesu SCU-LAN10 wire format — framing, header, XOR, channel/message ids
=======================================================================
The transport an SCU-LAN10-equipped Yaesu radio speaks (FTDX101MP/D, FTDX10,
FT-710): the LAN unit bridges the radio's CAT, audio and spectrum scope onto
four UDP ports and obfuscates every datagram with a per-packet XOR key.

This module is **the byte layer and nothing else** — no sockets, no session
state, no timers — so every rule in it is provable offline against the five
hardcoded init packets the stock client always sends and against the gTable
published with the protocol's reverse-engineering.

Wire shape (lengths in bytes)::

    5A 5A | len u16 LE | 00 01 | header[8] | body[N]
    ^magic            ^type    ^plain       ^XOR'd with key ^ gTable

``len`` counts type + header + body (``N + 10``), so a datagram is ``N + 12``
bytes.  The header carries the per-packet key in ``b5``; every other header
byte is XOR'd with it, which is why a constant field still looks random on the
wire.  A header whose b1/b3/b6/b7 do not agree with that key is dropped by the
device silently — so `parse_wire` validates exactly those four bytes and
nothing more.

Provenance: reverse-engineered from ~76k captured packets between Yaesu's
FT-Control and a real SCU-LAN10, confirmed by an independently written client
against an FTDX10 (the `sculan10-protocol` project's SPEC.md).  Only *facts*
are taken from it — offsets, formulas, wire constants — never its code; the
same rule this repo applies to wfview's GPL implementation (see
docs/g7250-wfview-analysis.md §7).

One correction to that spec is asserted in the tests: its § Phase 1 describes
the init keepalives' decrypted payload as ``04 04 04 04``, but those packets
decode to ``00 00 00 00`` under the spec's own formulas (and its § Keepalives
says four zero bytes).  The bytes are the authority.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "MAGIC", "TYPE_FIELD", "HEADER_SIZE", "MIN_WIRE_SIZE",
    "CHANNEL_CTRL", "CHANNEL_CAT", "CHANNEL_AUDIO", "CHANNEL_SCOPE",
    "CHANNELS", "channel_name",
    "MSG_SETUP", "MSG_AUTH", "MSG_KEEPALIVE", "MSG_PORT_INIT", "MSG_DATA",
    "MSG_ACK_SETUP", "MSG_ACK_AUTH", "MSG_ACK_KEEPALIVE", "MSG_ACK_PORT_INIT",
    "MSG_ACK_DATA", "MSG_NAMES",
    "GTABLE", "gtable", "build_header", "frame", "parse_wire", "try_parse",
    "Packet", "MalformedPacket", "SequenceCounter",
    "INIT_PACKETS", "AUTH_KEY", "AUTH_SEQ", "build_auth",
    "port_init_body", "continuation_body", "cat_setup_body", "audio_setup_body",
    "cat_request_body", "cat_ascii",
]

MAGIC = b"\x5a\x5a"
TYPE_FIELD = b"\x00\x01"
HEADER_SIZE = 8
#: magic(2) + length(2) + type(2) + header(8); a zero-length body is legal.
MIN_WIRE_SIZE = 4 + 2 + HEADER_SIZE

# ── Channels (b2 ^ key) ────────────────────────────────────────────
CHANNEL_CTRL = 0xFC
CHANNEL_CAT = 0xFD
CHANNEL_AUDIO = 0xFE
CHANNEL_SCOPE = 0xFA

CHANNELS = (CHANNEL_CTRL, CHANNEL_CAT, CHANNEL_AUDIO, CHANNEL_SCOPE)
_CHANNEL_NAMES = {
    CHANNEL_CTRL: "ctrl",
    CHANNEL_CAT: "cat",
    CHANNEL_AUDIO: "audio",
    CHANNEL_SCOPE: "scope",
}

# ── Message types (b4 ^ key); bit 7 means "server → client" ─────────
MSG_SETUP = 0x01
MSG_AUTH = 0x03
MSG_KEEPALIVE = 0x05
MSG_PORT_INIT = 0x06
MSG_DATA = 0x07
MSG_ACK_SETUP = 0x81
MSG_ACK_AUTH = 0x83
MSG_ACK_KEEPALIVE = 0x85
MSG_ACK_PORT_INIT = 0x86
MSG_ACK_DATA = 0x87

MSG_NAMES = {
    MSG_SETUP: "setup",
    MSG_AUTH: "auth",
    MSG_KEEPALIVE: "keepalive",
    MSG_PORT_INIT: "port-init",
    MSG_DATA: "data",
    MSG_ACK_SETUP: "ack-setup",
    MSG_ACK_AUTH: "ack-auth",
    MSG_ACK_KEEPALIVE: "ack-keepalive",
    MSG_ACK_PORT_INIT: "ack-port-init",
    MSG_ACK_DATA: "ack-data",
}

#: Constant byte in every header (b3 = key ^ 0x03).
_HEADER_CONST = 0x03
#: b1 is keyed with the CTRL channel id for every direction.
_VERIFY_SALT = CHANNEL_CTRL
#: b7 additionally XORs this constant.
_HIGH_LEN_SALT = 0x07

#: The body scramble table: ``(4*(i//4) + 6 + (0, 0xFF, 0xFE, 5)[i % 4]) & 0xFF``.
_GTABLE_OFFSETS = (0, 0xFF, 0xFE, 5)

#: Auth keeps a fixed key and sequence in every observed session.
AUTH_SEQ = 0x06
AUTH_KEY = 0x3A

#: The five init packets the stock client sends before authenticating, byte for
#: byte, with fixed keys and sequences 1-5.  Their bodies decode to four zero
#: bytes (``keepalive``); the device answers nothing.
INIT_PACKETS = tuple(bytes.fromhex(h) for h in (
    "5a5a0e00000101a0a15e585d595a5b585956",
    "5a5a0e000001024042bdbbbebab9b8bbbab5",
    "5a5a0e00000103e0e31c1a1f1b18191a1b14",
    "5a5a0e0000010480847b7d787c7f7e7d7c73",
    "5a5a0e000001052025dadcd9dddedfdcddd2",
))


class MalformedPacket(ValueError):
    """A datagram that is not a well-formed SCU-LAN10 packet."""


def gtable(size: int = 8192) -> bytes:
    """The body scramble table (4096 covers the scope channel; 8192 is margin)."""
    offsets = _GTABLE_OFFSETS
    return bytes(((4 * (i // 4) + 6 + offsets[i % 4]) & 0xFF) for i in range(size))


GTABLE = gtable()


def channel_name(channel: int) -> str:
    """``"cat"`` for 0xFD — for log lines, never for routing."""
    return _CHANNEL_NAMES.get(channel, f"0x{channel:02X}")


def build_header(seq: int, channel: int, msg_type: int, body_len: int,
                 key: Optional[int] = None) -> bytes:
    """The 8-byte plain header for one packet.

    ``key`` defaults to a fresh random byte; the auth packet is the one caller
    that passes an explicit key (``AUTH_KEY``).
    """
    if key is None:
        import random
        key = random.randint(0, 255)
    return bytes((
        seq & 0xFF,
        key ^ (seq & 0xFF) ^ _VERIFY_SALT,
        key ^ channel,
        key ^ _HEADER_CONST,
        key ^ msg_type,
        key,
        key ^ (body_len & 0xFF),
        key ^ (_HIGH_LEN_SALT ^ ((body_len >> 8) & 0xFF)),
    ))


def frame(body: bytes, *, seq: int, channel: int, msg_type: int,
          key: Optional[int] = None) -> bytes:
    """Wrap ``body`` in header + framing, encrypting it.  One UDP datagram."""
    body = bytes(body)
    header = build_header(seq, channel, msg_type, len(body), key)
    table = GTABLE
    if len(body) > len(table):
        table = gtable(len(body))
        globals()["GTABLE"] = table        # grow once, never shrink
    encrypt_key = header[5]
    encrypted = bytes(b ^ encrypt_key ^ table[i] for i, b in enumerate(body))
    payload = header + encrypted
    length = len(payload) + 2              # +2 = the type field
    return MAGIC + struct.pack("<H", length) + TYPE_FIELD + payload


@dataclass(frozen=True)
class Packet:
    """One decoded datagram.  ``body`` is already decrypted."""

    seq: int
    channel: int
    msg_type: int
    key: int
    body: bytes

    @property
    def channel_name(self) -> str:
        return channel_name(self.channel)

    @property
    def body_len(self) -> int:
        return len(self.body)


def parse_wire(raw: bytes) -> Packet:
    """Decode one datagram; raise `MalformedPacket` if the header disagrees.

    Only the checks the device itself makes are performed (magic, declared
    length, b1/b3/b6/b7 consistency) — an authority check list longer than the
    device's would reject traffic the real thing accepts.
    """
    if len(raw) < MIN_WIRE_SIZE:
        raise MalformedPacket(f"short datagram: {len(raw)} bytes")
    if raw[0:2] != MAGIC:
        raise MalformedPacket(f"bad magic: {raw[0:2]!r}")
    declared = struct.unpack_from("<H", raw, 2)[0]
    if raw[4:6] != TYPE_FIELD:
        raise MalformedPacket(f"bad type field: {raw[4:6]!r}")
    body_len = declared - 10
    # declared counts type(2)+header(8)+body; the datagram adds magic(2)+len(2).
    expected = body_len + 14
    if body_len < 0:
        raise MalformedPacket(f"declared length {declared} < 10")
    if len(raw) != expected:
        raise MalformedPacket(
            f"declared {declared} wants {expected} bytes, got {len(raw)}")

    seq, b1, b2, b3, b4, key, b6, b7 = raw[6:14]
    if b1 != (key ^ seq ^ _VERIFY_SALT):
        raise MalformedPacket("b1 does not verify (seq/key mismatch)")
    if b3 != (key ^ _HEADER_CONST):
        raise MalformedPacket("b3 constant mismatch")
    if b6 != (key ^ (body_len & 0xFF)):
        raise MalformedPacket("b6 length mismatch")
    if b7 != (key ^ (_HIGH_LEN_SALT ^ ((body_len >> 8) & 0xFF))):
        raise MalformedPacket("b7 length mismatch")

    channel = b2 ^ key
    msg_type = b4 ^ key
    table = GTABLE
    if body_len > len(table):
        table = gtable(body_len)
        globals()["GTABLE"] = table
    body = bytes(raw[14 + i] ^ key ^ table[i] for i in range(body_len))
    return Packet(seq=seq, channel=channel, msg_type=msg_type, key=key, body=body)


def try_parse(raw: bytes) -> Optional[Packet]:
    """`parse_wire` for a receive path that must never raise (drop != crash)."""
    try:
        return parse_wire(raw)
    except MalformedPacket:
        return None


class SequenceCounter:
    """Per-channel, per-direction sequence number (wraps at 0xFF).

    The device validates b1 against this counter, so two writers on one channel
    would break verification — callers serialise per channel.
    """

    __slots__ = ("_value",)

    def __init__(self, start: int = 0x01) -> None:
        self._value = start & 0xFF

    def next(self) -> int:
        value = self._value
        self._value = (value + 1) & 0xFF
        return value

    def reset(self, start: int = 0x01) -> None:
        """Restart the counter — a rebuilt session begins at sequence 1."""
        self._value = start & 0xFF

    @property
    def value(self) -> int:
        """The value the next `next()` will return."""
        return self._value


# ── Session payload bodies (facts, kept next to the framing) ────────


def build_auth(username: str, password: str) -> bytes:
    """The auth datagram: 68-byte body of ``01 00`` + user[33] + pass[33].

    Credentials are ASCII, at most 32 characters, null-padded to 33 bytes.
    """
    user = (username or "").encode("ascii", "ignore")[:32].ljust(33, b"\x00")
    psk = (password or "").encode("ascii", "ignore")[:32].ljust(33, b"\x00")
    return frame(b"\x01\x00" + user + psk, seq=AUTH_SEQ,
                 channel=CHANNEL_CTRL, msg_type=MSG_AUTH, key=AUTH_KEY)


def port_init_body(session_id: int) -> bytes:
    """``[sid, 00, 00, 00]`` — "I am listening on this port" (msg 0x06)."""
    return bytes((session_id & 0xFF, 0, 0, 0))


def continuation_body(session_id: int) -> bytes:
    """Phase 4's ``[sid,0,0,0] + 51 C3 00 96 00 00`` (msg 0x01 on CAT)."""
    return bytes((session_id & 0xFF, 0, 0, 0, 0x51, 0xC3, 0x00, 0x96, 0, 0))


def cat_setup_body(session_id: int) -> bytes:
    """``'R'`` port identifier — registers the CAT channel (msg 0x01)."""
    return bytes((session_id & 0xFF, 0, 0, 0, 0x52, 0xC3))


def audio_setup_body(session_id: int) -> bytes:
    """``'S'`` port identifier — registers the audio channel (msg 0x01)."""
    return bytes((session_id & 0xFF, 0, 0, 0, 0x53, 0xC3))


def cat_request_body(session_id: int, cmd_seq: int, command: str) -> bytes:
    """``sid, 00 00 00, cmd_seq, 00 00 00`` + the ASCII CAT command.

    The radio dedupes by ``cmd_seq``, which is what makes "send every CAT twice"
    safe on a lossy UDP path.
    """
    ascii_bytes = command.encode("ascii", "ignore")
    return bytes((session_id & 0xFF, 0, 0, 0, cmd_seq & 0xFF, 0, 0, 0)) + ascii_bytes


def cat_ascii(body: bytes) -> str:
    """The CAT text of a server→client body (``resp_seq, 00 00 00`` + ASCII)."""
    if len(body) <= 4:
        return ""
    return body[4:].decode("ascii", "replace")
