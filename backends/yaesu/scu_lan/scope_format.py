"""
SCU-LAN10 spectrum body → the server's 850-bin scope line
=========================================================
Each scope datagram carries a 4096-byte body: one spectrum sweep, one amplitude
byte per bin, **inverted** (0 = strongest, 255 = weakest), ≈25 bodies/second.

What is *not* the same as the FT-710 is the layout.  The FT-710 frame is a
fixed pair of 850-bin windows (`backends.ft710.scope_frame`); here the active
range is marked at run time:

* the sweep ends at the first bin ``<= 1`` when scanning from bin 500 (the
  reference block that follows averages ≈233, so a low value there is a
  boundary marker, not a signal);
* the visible bin count is ``marker // 2`` — an empirical rule from the
  protocol's reverse-engineering whose cause is unknown, verified only on an
  FTDX10 (see the design record §6.2);
* everything past the marker is undecoded by anyone we know of.

So this module's job is narrow: find that boundary, take the bins, invert them
into the server's convention (higher = stronger, the same convention
`parse_scope_frame` produces) and resample to the 850-bin width the rest of the
pipeline speaks.  Resampling reuses the IC-7300's ``upsample_bins`` on purpose:
a second interpolation implementation is a second thing to drift.

Frequency mapping is *not* here: the span and mode come from CAT (`SS;`), the
centre from `FA;`.  Guessing them from the body is how a waterfall ends up
plausible and wrong.
"""
from __future__ import annotations

from typing import Sequence

from backends.ft710.scope_frame import WF_SIZE
from backends.ic7300.civ_codec import upsample_bins

__all__ = [
    "SPECTRUM_BINS", "MARKER_SEARCH_START", "MARKER_SEARCH_END",
    "BOUNDARY_MAX", "REFERENCE_FLOOR",
    "find_spectrum_end", "usable_bin_count", "signal_bins", "to_scope_bins",
    "invert",
]

#: The pipeline's width — `ScopeHandler` / spectrum frames / the frontend.
SPECTRUM_BINS = WF_SIZE

#: Search window for the boundary marker.  The docstring in
#: `find_spectrum_end` explains why the scan cannot start at 0.
MARKER_SEARCH_START = 500
MARKER_SEARCH_END = 3000

#: A bin at or below this marks the end of the sweep.
BOUNDARY_MAX = 1

#: Average of a 64-bin block above this ⇒ the reference/calibration block.
REFERENCE_FLOOR = 225

_FALLBACK_BLOCK = 64
_FALLBACK_MARGIN = 100


def find_spectrum_end(body: Sequence[int]) -> int:
    """Index of the boundary marker (the first bin ``<= BOUNDARY_MAX``).

    Scanning starts at 500 because a genuinely strong signal near the centre
    can be 0 or 1 as well; the reference block that follows the sweep is
    uniformly high, so the first low value in the tail is the marker.

    Falls back to the first high-average block, then to a fixed cut at the
    smaller of the body length and 2000 — the three steps are the published
    heuristic, kept in the same order so a field report is comparable to it.
    """
    values = list(body)
    n = len(values)
    if n == 0:
        return 0
    limit = min(n, MARKER_SEARCH_END)
    for i in range(min(MARKER_SEARCH_START, limit), limit):
        if values[i] <= BOUNDARY_MAX:
            return i
    block = _FALLBACK_BLOCK
    start = MARKER_SEARCH_START
    while start + block <= limit:
        window = values[start:start + block]
        if sum(window) / block > REFERENCE_FLOOR:
            return max(_FALLBACK_MARGIN, start - _FALLBACK_MARGIN)
        start += block
    return min(n, 2000)


def usable_bin_count(body: Sequence[int]) -> int:
    """How many leading bins hold this sweep (the ``marker // 2`` rule)."""
    return find_spectrum_end(body) // 2


def signal_bins(body: Sequence[int]) -> list[int]:
    """Native-resolution, inverted bins: ``255 - raw`` (higher = stronger)."""
    count = usable_bin_count(body)
    return invert(body[:count])


def invert(values: Sequence[int]) -> list[int]:
    """``255 - v`` — the SCU-LAN10's low-is-strong into the server's convention."""
    return [0xFF - (v & 0xFF) for v in values]


def to_scope_bins(body: Sequence[int], width: int = SPECTRUM_BINS) -> list[int]:
    """One scope body → ``width`` bins in the server's 0..255 convention.

    An empty or unusable body yields an empty list rather than a flat line: a
    flat 850-bin "spectrum" at the noise floor is indistinguishable from a
    real quiet band, and the server already has an S-meter synthesiser for
    "no scope data" (scope_type="none").
    """
    native = signal_bins(body)
    if not native:
        return []
    if len(native) == width:
        return native
    return upsample_bins(native, width)
