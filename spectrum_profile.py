"""Spectrum wire-frame profile tiering (SDD AD-025).

Pure stdlib, zero app imports, so it is unit-testable without a server and
patchable through the hot-fix channel (same shape as ``session_metrics``).

Two orthogonal factors make one profile:

* ``shape`` — ``full`` is today's ``0x01 + wf1(850) + wf2(850)`` (1701 B);
  ``wf1`` drops the 850 B second waterfall, which is all zeros on both the
  FT-710 and the IC-7300 path and which no client ever draws (Web reads it into
  ``window._lastWf2`` "for potential future use", iOS ignores it, Android parses
  it without rendering it).  That is exactly the ``v1 = 851 B`` frame SDD §9.2.4
  specified and which was never shipped — every server build so far sent the v1
  version byte with the v2 length.
* ``divider`` — how many of the ``SPECTRUM_BROADCAST_FPS`` (30 Hz) broadcast
  ticks actually reach the socket.

The divider is the only factor that helps a *browser* client: uvicorn 0.52.1
negotiates ``permessage-deflate`` with browsers, and measured on this payload
the 850 zero bytes of wf2 cost ~1.4 B/frame after compression (full frame
1701 B -> ~441 B on the wire, 3.9x; wf1 alone 851 B -> ~440 B, 1.9x).  Android
ships OkHttp 4.12, which does not offer the extension (its dex carries the
"Request header not permitted: 'Sec-WebSocket-Extensions'" guard), so on a phone
the shape factor is the whole saving and 1701 B really does leave the radio.
Both factors are therefore needed for "each tier halves the previous one" to
hold on either axis.

Backward compatibility (design §4 / D-3): a socket stays on ``high`` — full
1701 B, divider 1, byte-for-byte today's behaviour — until it declares
capability with a ``{"type":"spectrumCaps","profile":...}`` text frame.  The old
Android build rejects any frame whose length is not 1701
(``SpectrumFrame.kt:14-15``) and would drop short frames silently, so the server
must never volunteer them.  The full frame keeps its ``0x01`` version byte on
purpose: iOS guards ``version == 0x01`` and re-tagging it ``0x02`` would black
out already-installed iOS clients.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

WF1_BYTES = 850
WIRE_VERSION = 0x01
VERSION_BYTES = 1

FULL_FRAME_BYTES = VERSION_BYTES + 2 * WF1_BYTES   # 1701, today's frame
SHORT_FRAME_BYTES = VERSION_BYTES + WF1_BYTES      # 851, SDD §9.2.4 "v1"

SHAPE_FULL = "full"
SHAPE_WF1 = "wf1"


@dataclass(frozen=True)
class Profile:
    """One bandwidth tier: what to send, and how often."""

    name: str
    shape: str
    divider: int


PROFILES: dict[str, Profile] = {
    "high": Profile("high", SHAPE_FULL, 1),
    "mid": Profile("mid", SHAPE_WF1, 2),
    "low": Profile("low", SHAPE_WF1, 4),
    # Server-internal: the listener-password default, i.e. exactly the /3 full
    # frame rate listeners have had since v1.25.2.  Not client-selectable.
    "listen": Profile("listen", SHAPE_FULL, 3),
}

DEFAULT_PROFILE = "high"
LISTEN_PROFILE = "listen"
CLIENT_PROFILES = ("high", "mid", "low")


def divider_for(name: str) -> int:
    """Broadcast-tick divider for a profile name; unknown names get no throttle."""
    return PROFILES.get(name, PROFILES[DEFAULT_PROFILE]).divider


def shape_for(name: str) -> str:
    """Frame shape for a profile name; unknown names get the compatible full frame."""
    return PROFILES.get(name, PROFILES[DEFAULT_PROFILE]).shape


def frame_due(tick: int, divider: int) -> bool:
    """Is ``tick`` one of the ticks that may deliver a frame at this divider?

    Reproduces the pre-profile listener gate exactly (``tick % 3 == 0`` over
    ticks starting at 1 gives [3, 6, 9]) so no existing behaviour shifts.
    """
    if divider <= 1:
        return True
    return tick % divider == 0


def build_variants(full_frame: bytes) -> dict[str, bytes]:
    """Derive every sendable frame shape from one full frame.

    The short frame is a slice of the long one — same version byte, same wf1 —
    so ``scope_handler`` and the backends stay untouched and there is no second
    code path that can drift out of sync with the first.  A frame whose length
    is not the documented 1701 yields only the full variant; the fan-out then
    falls back to it rather than dropping data (see ``variant_for``).
    """
    variants = {SHAPE_FULL: full_frame}
    if len(full_frame) == FULL_FRAME_BYTES:
        variants[SHAPE_WF1] = full_frame[:SHORT_FRAME_BYTES]
    return variants


def variant_for(variants: dict[str, bytes], name: str) -> bytes:
    """The frame to send for ``name``; never ``None``.

    A client that asked for a shape we cannot build still gets data — silently
    dropping frames is precisely the failure mode this feature must not create.
    """
    frame = variants.get(shape_for(name))
    if frame is None:
        frame = variants.get(SHAPE_FULL, b"")
    return frame


def parse_caps(text: str | bytes) -> str | None:
    """Extract a client-selectable profile name from a ``spectrumCaps`` frame.

    Returns ``None`` for anything else — a keepalive, malformed JSON, an unknown
    name, or the server-internal ``listen`` tier.  Never raises: the
    ``/WSspectrum`` receive loop has always discarded its text frames, so a bad
    one must stay as harmless as silence.
    """
    if isinstance(text, (bytes, bytearray)):
        return None
    try:
        msg = json.loads(text)
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("type") != "spectrumCaps":
        return None
    name = msg.get("profile")
    return name if name in CLIENT_PROFILES else None
