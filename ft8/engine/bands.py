"""FT8 sub-band dial frequencies → ADIF band names.

Server-side mirror of the PWA band selector (``web/static/js/band.js``), so a
completed QSO's dial frequency becomes the ADIF ``BAND`` value.  ADIF band
names (``40m``/``20m``/``15m``/``10m``) follow the WSJT-X export convention.
"""

from __future__ import annotations

# Dial frequencies for the FT8 sub-band on each HF band (matches band.js).
FT8_BANDS: list[tuple[int, str]] = [
    (7_074_000, "40m"),
    (14_074_000, "20m"),
    (21_074_000, "15m"),
    (28_074_000, "10m"),
]

MATCH_HZ = 50_000  # within ±50 kHz counts as the same band (band.js)


def band_from_freq_hz(freq_hz: int) -> str:
    """ADIF band name for an FT8 dial frequency, or ``""`` when off-band."""

    for dial, band in FT8_BANDS:
        if abs(dial - freq_hz) < MATCH_HZ:
            return band
    return ""
