"""FT8 dial frequency → ADIF band name mapping (mirror of band.js)."""

from __future__ import annotations

from ft8.engine.bands import band_from_freq_hz


def test_known_ft8_bands() -> None:
    assert band_from_freq_hz(7_074_000) == "40m"
    assert band_from_freq_hz(14_074_000) == "20m"
    assert band_from_freq_hz(21_074_000) == "15m"
    assert band_from_freq_hz(28_074_000) == "10m"


def test_within_match_tolerance_counts_as_same_band() -> None:
    # band.js: Math.abs(dial - freq) < 50_000 → same band.
    assert band_from_freq_hz(14_074_000 - 49_999) == "20m"
    assert band_from_freq_hz(14_074_000 + 49_999) == "20m"


def test_exactly_at_match_tolerance_is_off_band() -> None:
    assert band_from_freq_hz(14_074_000 - 50_000) == ""
    assert band_from_freq_hz(14_074_000 + 50_000) == ""


def test_off_band_and_zero() -> None:
    assert band_from_freq_hz(1_000_000) == ""
    assert band_from_freq_hz(0) == ""
