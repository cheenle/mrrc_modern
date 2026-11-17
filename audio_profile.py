"""Per-socket main-voice audio profiles (SDD AD-038).

Pure stdlib, zero app imports, so it is unit-testable without a server and
patchable through the hot-fix channel — the same shape as ``spectrum_profile``
(AD-025), whose ``{"type":"spectrumCaps",…}`` text frame this mirrors.

The 48 kHz Opus bus is unchanged. A client that never declares a profile keeps
exactly today's stream byte-for-byte; PCM16 is opt-in per audio socket::

    {"type":"audioCaps","profile":"pcm16"}   ->  tag 0x02, 16 kHz mono s16le

/WSterminal (the ESP32-class single-socket client, AD-039) has no Opus option
and may declare either raw-PCM profile; one declaration registers both
directions of that socket and is answered exactly once::

    {"type":"audioCaps","profile":"pcm48"}   ->  tag 0x00, 48 kHz mono s16le
    {"type":"audioCaps","profile":"pcm16"}   ->  tag 0x02, 16 kHz mono s16le

The dedicated /WSaudioRX and /WSaudioTX endpoints still accept only pcm16.

Frame geometry is therefore fixed by the wire, not negotiated: one PCM16 frame
is ``PCM16_FRAME_BYTES`` = 320 samples × 2 bytes = 20 ms of 16 kHz Int16 LE
mono — the same cadence as the legacy 960-sample 48 kHz Opus frame, so nothing
downstream has to learn a new tick length.

Rejected requests answer with the reason (``pcm16-tx-unsupported`` etc.) rather
than silently degrading: a device that asked for 16 kHz and got 48 kHz would
play back at 3× speed, which is far harder to diagnose from the field than one
explicit line.
"""
from __future__ import annotations

import json

#: The pre-AD-038 behaviour: Opus 48 kHz (tag 0x01) / raw PCM 48 kHz (tag 0x00).
PROFILE_LEGACY = "legacy"
#: 16 kHz mono Int16 little-endian, tag 0x02, no codec on the wire.
PROFILE_PCM16 = "pcm16"
#: 48 kHz mono Int16 little-endian, tag 0x00, no codec on the wire. The other
#: terminal-only profile (AD-039): an ESP32 that cannot afford Opus declares
#: this and is guaranteed raw PCM even when libopus is available server-side.
PROFILE_PCM48 = "pcm48"

PCM16_RATE = 16000
PCM16_CHANNELS = 1
PCM16_FORMAT = "s16le"
PCM16_FRAME_MS = 20
#: Samples in one PCM16 frame (320 @ 16 kHz / 20 ms).
PCM16_FRAME_SAMPLES = PCM16_RATE * PCM16_FRAME_MS // 1000
#: Bytes in one PCM16 frame payload, excluding the 1-byte tag (640).
PCM16_FRAME_BYTES = PCM16_FRAME_SAMPLES * 2

PCM48_RATE = 48000
PCM48_CHANNELS = 1
PCM48_FORMAT = "s16le"
PCM48_FRAME_MS = 20
#: Samples in one nominal PCM48 frame (960 @ 48 kHz / 20 ms). A catch-up tick
#: may carry several of these back to back, exactly like the legacy PCM
#: fallback — the wire is a sample stream, not a fixed-size packet bus.
PCM48_FRAME_SAMPLES = PCM48_RATE * PCM48_FRAME_MS // 1000
#: Bytes in one nominal PCM48 frame payload, excluding the 1-byte tag (1920).
PCM48_FRAME_BYTES = PCM48_FRAME_SAMPLES * 2

#: Wire description of each declarable profile, keyed by name. ``legacy`` is
#: deliberately absent: it is what a socket gets by saying nothing, never a
#: name a client may declare.
_PROFILE_WIRE = {
    PROFILE_PCM16: {"rate": PCM16_RATE, "channels": PCM16_CHANNELS,
                    "format": PCM16_FORMAT, "frameMs": PCM16_FRAME_MS},
    PROFILE_PCM48: {"rate": PCM48_RATE, "channels": PCM48_CHANNELS,
                    "format": PCM48_FORMAT, "frameMs": PCM48_FRAME_MS},
}

#: 1-byte codec tag for a PCM16 frame on /WSaudioRX and /WSaudioTX. Legacy tags
#: are 0x00 (raw PCM 48 kHz) and 0x01 (Opus) and live in ``opus_rx``; this one
#: is defined here with the rest of the profile's contract and re-exported
#: there, and 0x00/0x01 are **never** reinterpreted as 16 kHz (AD-038).
AUDIO_TAG_PCM16 = 0x02


def parse_caps(text: str | bytes | None) -> str | None:
    """Extract a client-selectable audio profile from an ``audioCaps`` frame.

    Returns ``None`` for anything else — a keepalive, a bytes frame, malformed
    JSON, an unknown name. Never raises: like ``spectrum_profile.parse_caps``,
    a text frame the server does not understand must stay as harmless as
    silence, so an old client can never break a new server.
    """
    if not isinstance(text, str):
        return None
    try:
        msg = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get("type") != "audioCaps" or msg.get("profile") != PROFILE_PCM16:
        return None
    return PROFILE_PCM16


def parse_terminal_caps(text: str | bytes | None) -> str | None:
    """The terminal's two-profile parser (AD-039): ``pcm48`` or ``pcm16``.

    /WSterminal serves an ESP32-class device with no Opus option, so its
    declaration may name either raw-PCM profile. The dedicated audio endpoints
    keep ``parse_caps`` (pcm16 only) — widening this parser must never widen
    theirs. Same silence rule as ``parse_caps`` for anything unrecognized.
    """
    if not isinstance(text, str):
        return None
    try:
        msg = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get("type") != "audioCaps":
        return None
    profile = msg.get("profile")
    if profile in (PROFILE_PCM48, PROFILE_PCM16):
        return profile
    return None


def accepted_caps(profile: str = PROFILE_PCM16) -> str:
    """The server's yes: the full wire description of the named profile."""
    return json.dumps({
        "type": "audioCaps", "profile": profile, "accepted": True,
        **_PROFILE_WIRE[profile],
    }, separators=(",", ":"))


def rejected_caps(reason: str, profile: str = PROFILE_PCM16) -> str:
    """The server's no, with the reason named instead of a silent downgrade."""
    return json.dumps({
        "type": "audioCaps", "profile": profile,
        "accepted": False, "reason": reason,
    }, separators=(",", ":"))
