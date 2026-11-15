"""FlexRadio SmartSDR backend (model key ``flex``).

One backend key for the whole FlexRadio family: the radio describes itself
(``info`` / ``radio`` status: model, serial, software version) and the protocol
is identical across FLEX-6300/6400/6500/6600/6700 and the current 8400/8600
Signature line.  Nothing here branches on a model name — that is deliberate,
because a hardcoded capacity table only ever lies to the UI.

Layout (dependencies point downward, never sideways):

* ``vita``     — VITA-49 wire codec: header, FFT segments, RX audio, meters,
                 the client→radio Opus packet.  Pure stdlib + numpy.
* ``protocol`` — SmartSDR text codec: ``C``/``R``/``S`` lines, multi-word
                 object names, the ``meter`` ``#`` quirk, ``info``,
                 ``slice list``, the discovery payload.  Pure stdlib.
* ``session``  — TCP 4992 + UDP: handshake, sequence/reply matching,
                 subscriptions, port registration, ``client_handle`` filtering,
                 the interlock state machine, reconnect backoff.
* ``engine``   — the only owner of the sockets, the receive ring, the transmit
                 pacer, the meter table and the ``info`` cache.
* ``audio`` / ``scope`` — adapters onto the existing ``AudioHandler`` and
                 ``ScopeProducer`` contracts (48 kHz int16 mono both ways,
                 850-bin spectrum), so server/client code does not change.
* ``backend``  — ``RadioBackend`` + the CAT translation layer.

Protocol evidence and the GPL boundary: ``docs/flexradio-smartsdr-analysis.md``.
Design record: ``docs/superpowers/specs/2026-11-15-flexradio-backend-design.md``.

Hardware status: **not verified**.  ``MRRC_ALLOW_UNVERIFIED_TX`` is the only
thing that can lift the transmit gate (AD-019 / NFR-067).
"""

from __future__ import annotations

MODEL_KEY = "flex"
DISPLAY_NAME = "FlexRadio (SmartSDR)"
#: No hardware acceptance yet: PTT/TUNE stay behind AD-019 until a seed user
#: walks the checklist in the design record §11.
VERIFIED = False
DEFAULT_TCP_PORT = 4992
DEFAULT_BAUD_RATE = 115200      # registry table entry only — there is no serial port

# Submodules are re-exported so ``from backends.flex import protocol, vita``
# resolves; neither imports this package, so there is no cycle.
from backends.flex import protocol, session, vita  # noqa: E402,F401

__all__ = ["MODEL_KEY", "DISPLAY_NAME", "VERIFIED",
           "protocol", "session", "vita"]
