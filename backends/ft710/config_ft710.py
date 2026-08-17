"""
FT-710-specific configuration tables
=====================================
Scope port, span choices, mode/band/filter tables, and meter calibration
for the Yaesu FT-710 backend.  Protocol-neutral constants and shared
helpers (NARROW_MODES, MODE_DISPLAY_NAMES, UI_MODES, _interp) live in the
top-level ``config`` module.
"""
from __future__ import annotations

import os

from config import NARROW_MODES, _interp

# ── Scope/Spectrum Serial Port ─────────────────────────────────────
# FT-710 Standard COM Port for scope data (second USB serial interface)
SCOPE_SERIAL_PORT = os.environ.get("FT710_SCOPE_PORT", "")
SCOPE_BAUD_RATE = int(os.environ.get("FT710_SCOPE_BAUD", "115200"))

# Span choices (from FT-710.rig Spans section)
SCOPE_SPANS: dict[int, dict] = {
    0:  {"name": "1 kHz",   "freq": 1000},
    1:  {"name": "2 kHz",   "freq": 2000},
    2:  {"name": "5 kHz",   "freq": 5000},
    3:  {"name": "10 kHz",  "freq": 10000},
    4:  {"name": "20 kHz",  "freq": 20000},
    5:  {"name": "50 kHz",  "freq": 50000},
    6:  {"name": "100 kHz", "freq": 100000},
    7:  {"name": "200 kHz", "freq": 200000},
    8:  {"name": "500 kHz", "freq": 500000},
    9:  {"name": "1 MHz",   "freq": 1000000},
}

# ── Mode Tables ─────────────────────────────────────────────────────
# FT-710 mode register values (from FT-710.rig Modes section)
MODE_NUM_TO_NAME: dict[int, str] = {
    0x01: "LSB",
    0x02: "USB",
    0x03: "CW-U",
    0x04: "FM",
    0x05: "AM",
    0x06: "RTTY-L",
    0x07: "CW-L",
    0x08: "DATA-L",
    0x09: "RTTY-U",
    0x0A: "DATA-FM",
    0x0B: "FM-N",
    0x0C: "DATA-U",
    0x0D: "AM-N",
    0x0E: "PSK",
    0x0F: "DATA-FM-N",
}

MODE_NAME_TO_NUM: dict[str, int] = {v: k for k, v in MODE_NUM_TO_NAME.items()}

# ── Band Definitions ────────────────────────────────────────────────
# From FT-710.rig Bands section.  Each entry: (name, start_hz, end_hz, band_stack_reg, default_freq)
BANDS: list[dict] = [
    {"name": "160m",  "start": 1_800_000,  "end": 2_000_000,  "bsr": 0,  "default_freq": 1_845_500},
    {"name": "80m",   "start": 3_500_000,  "end": 4_000_000,  "bsr": 1,  "default_freq": 3_850_000},
    {"name": "60m",   "start": 5_250_000,  "end": 5_450_000,  "bsr": 2,  "default_freq": 5_350_000},
    {"name": "40m",   "start": 7_000_000,  "end": 7_300_000,  "bsr": 3,  "default_freq": 7_050_000},
    {"name": "30m",   "start": 10_100_000, "end": 10_150_000, "bsr": 4,  "default_freq": 10_140_000},
    {"name": "20m",   "start": 14_000_000, "end": 14_350_000, "bsr": 5,  "default_freq": 14_270_000},
    {"name": "17m",   "start": 18_068_000, "end": 18_168_000, "bsr": 6,  "default_freq": 18_132_500},
    {"name": "15m",   "start": 21_000_000, "end": 21_450_000, "bsr": 7,  "default_freq": 21_400_000},
    {"name": "12m",   "start": 24_890_000, "end": 24_990_000, "bsr": 8,  "default_freq": 24_952_500},
    {"name": "10m",   "start": 28_000_000, "end": 29_700_000, "bsr": 9,  "default_freq": 28_450_000},
    {"name": "6m",    "start": 50_000_000, "end": 54_000_000, "bsr": 10, "default_freq": 50_150_000},
    {"name": "4m",    "start": 70_000_000, "end": 70_500_000, "bsr": 11, "default_freq": 70_250_000},
]


def get_band_for_frequency(freq_hz: int) -> dict | None:
    """Return the band dict that contains freq_hz, or None."""
    for band in BANDS:
        if band["start"] <= freq_hz <= band["end"]:
            return band
    return None


# ── Filter Widths ───────────────────────────────────────────────────
# From FT-710.rig Widths section.  Two groups: voice (SSB/AM/FM) and narrow (CW/RTTY/DATA)
# Each entry: (index, hz)
FILTER_WIDTHS_VOICE: list[tuple[int, int]] = [
    (1, 300), (2, 400), (3, 600), (4, 850), (5, 1100),
    (6, 1200), (7, 1500), (8, 1650), (9, 1800), (10, 1950),
    (11, 2100), (12, 2250), (13, 2400), (14, 2450), (15, 2500),
    (16, 2600), (17, 2700), (18, 2800), (19, 2900), (20, 3000),
    (21, 3200), (22, 3500), (23, 4000),
]

FILTER_WIDTHS_NARROW: list[tuple[int, int]] = [
    (1, 50), (2, 100), (3, 150), (4, 200), (5, 250),
    (6, 300), (7, 350), (8, 400), (9, 450), (10, 500),
    (11, 600), (12, 800), (13, 1200), (14, 1400), (15, 1700),
    (16, 2000), (17, 2400), (18, 3000), (19, 3200), (20, 3500), (21, 4000),
]


def get_filter_widths_for_mode(mode_name: str) -> list[tuple[int, int]]:
    """Return the appropriate filter width table for the given mode."""
    if mode_name in NARROW_MODES:
        return FILTER_WIDTHS_NARROW
    return FILTER_WIDTHS_VOICE


def get_filter_hz(mode_name: str, index: int) -> int | None:
    """Return filter width in Hz for the given mode and index, or None."""
    widths = get_filter_widths_for_mode(mode_name)
    for idx, hz in widths:
        if idx == index:
            return hz
    return None


# ── S-Meter Calibration ─────────────────────────────────────────────
# From FT-710.rig Meters section (entries 25-40).
# Maps raw 0-255 S-meter values to dBm.
S_METER_CAL: list[tuple[int, float]] = [
    (0,   -54), (12,  -48), (27,  -42), (40,  -36),
    (55,  -30), (65,  -24), (80,  -18), (95,  -12),
    (112,  -6), (130,   0), (150,  10), (172,  20),
    (190,  30), (220,  40), (240,  50), (255,  60),
]

# S-unit display mapping for raw values
S_UNIT_LEVELS: list[tuple[int, str]] = [
    (0, "S0"), (12, "S1"), (27, "S2"), (40, "S3"),
    (55, "S4"), (65, "S5"), (80, "S6"), (95, "S7"),
    (112, "S8"), (130, "S9"), (150, "+10"), (172, "+20"),
    (190, "+30"), (220, "+40"), (240, "+50"), (255, "+60"),
]


def raw_to_dbm(raw: int) -> float:
    """Convert raw S-meter value (0-255) to dBm using lookup table with interpolation."""
    if raw <= 0:
        return -54.0
    if raw >= 255:
        return 60.0
    for i in range(len(S_METER_CAL) - 1):
        r1, d1 = S_METER_CAL[i]
        r2, d2 = S_METER_CAL[i + 1]
        if r1 <= raw <= r2:
            frac = (raw - r1) / (r2 - r1)
            return d1 + frac * (d2 - d1)
    return 0.0


def raw_to_s_unit(raw: int) -> str:
    """Convert raw S-meter value to S-unit display string."""
    for i in range(len(S_UNIT_LEVELS) - 1):
        r1, label = S_UNIT_LEVELS[i]
        r2, _ = S_UNIT_LEVELS[i + 1]
        if r1 <= raw < r2:
            return label
    return S_UNIT_LEVELS[-1][1]


# ── Attenuator & Preamp Labels ──────────────────────────────────────
ATTENUATOR_LABELS: dict[int, str] = {0: "OFF", 1: "6dB", 2: "12dB", 3: "18dB"}
PREAMP_LABELS: dict[int, str] = {0: "OFF", 1: "AMP1", 2: "AMP2"}


# ── TX Meter Calibration (from FT-710.rig Meters section) ───────────
# Raw 0-255 values returned by RM3..RM8 map non-linearly to actual
# engineering units.  These tables are piecewise-linear interpolations
# of the rig file's calibration points.
POWER_CAL: list[tuple[int, float]] = [        # RM5 -> watts (FT-710 100W HF)
    # FT-710 power meter is non-linear: very gradual at low power,
    # steeper at high power.  Points derived from FT-710.rig calibration
    # with the dead-zone (0-27→0W) removed so even low modulation
    # registers a small non-zero reading.
    (0, 0.0), (94, 25.0), (147, 50.0),
    (176, 75.0), (205, 100.0), (255, 110.0),
]
SWR_CAL: list[tuple[int, float]] = [          # RM6 -> SWR ratio
    (0, 1.0), (26, 1.2), (52, 1.5), (89, 2.0),
    (126, 3.0), (173, 4.0), (236, 5.0), (255, 9.9),
]
VOLTAGE_CAL: list[tuple[int, float]] = [      # RM8 -> volts (13.8V rail)
    (0, 0.0), (192, 13.8), (255, 15.0),
]
CURRENT_CAL: list[tuple[int, float]] = [      # RM7 -> amps
    (0, 0.0), (53, 5.0), (65, 6.0), (78, 7.0),
    (86, 8.0), (98, 9.0), (107, 10.0), (255, 26.0),
]


def raw_to_power(raw: int) -> float:
    """RM5 raw 0-255 -> watts (FT-710, 100W radio)."""
    return _interp(raw, POWER_CAL)


def raw_to_swr(raw: int) -> float:
    """RM6 raw 0-255 -> SWR ratio (1.0 .. ~9.9)."""
    return _interp(raw, SWR_CAL)


def raw_to_voltage(raw: int) -> float:
    """RM8 raw 0-255 -> drain/supply volts."""
    return _interp(raw, VOLTAGE_CAL)


def raw_to_current(raw: int) -> float:
    """RM7 raw 0-255 -> drain current amps."""
    return _interp(raw, CURRENT_CAL)
