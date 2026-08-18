"""
IC-7300-specific configuration tables
=====================================
CI-V address/baud, mode/band/filter tables, meter calibration and
scope spans for the Icom IC-7300 backend.  Mirrors the structure of
``backends/ft710/config_ft710.py`` so server code stays generic.
Protocol framing/parsing facts live in ``backends/ic7300/civ_codec.py``.

Sources:
- wfview ``rigs/IC-7300.rig`` (github mirror eliggett/wfview): CI-V
  address 0x94, mode registers, band edges, preamp/attenuator commands,
  meter calibration points, spectrum facts (475 bins, 0..160, seq max 11).
- Icom "IC-7300 INFORMATION / CI-V Reference" (IC-7300_ENG_Info_V140_0.pdf),
  p. 10: scope span is sent on the wire as a 5-byte BCD *frequency*
  (cmd 27 15) — the small integer codes below are UI indices only.
- IC-7300 Full Manual, p. 4-5: factory default IF filter widths.
"""
from __future__ import annotations

import os

from config import _interp

# ── CI-V Connection ──────────────────────────────────────────────────
# IC-7300 default CI-V address is 0x94 (wfview rig: CIVAddress=148).
CIV_ADDR = int(os.environ.get("IC7300_CIV_ADDR", "0x94"), 16)
# IC-7300MK2 factory default CI-V address is 0xB6 (IC-7300MK2 CI-V
# Reference frame diagram; hamlib ic7300.c).  The IC-7300 is 0x94.
MK2_CIV_ADDR = int(os.environ.get("IC7300MK2_CIV_ADDR", "0xB6"), 16)
# IC-7300 USB serial defaults to 115200 (menu: CI-V USB Baud Rate).
CIV_BAUD_RATE = int(os.environ.get("IC7300_CIV_BAUD", "115200"))

# ── Mode Tables ─────────────────────────────────────────────────────
# CI-V mode codes (cmd 0x04/0x06 data byte 0; wfview rig Modes\N\Reg).
# Names reuse the shared UI mode strings from config.py where they
# exist: Icom CW (normal, USB-side) -> "CW-U", CW-R -> "CW-L",
# RTTY (normal, LSB-side) -> "RTTY-L", RTTY-R -> "RTTY-U".
# WFM is receive-only on the IC-7300 and has no shared UI equivalent.
MODE_NUM_TO_NAME: dict[int, str] = {
    0x00: "LSB",
    0x01: "USB",
    0x02: "AM",
    0x03: "CW-U",
    0x04: "RTTY-L",
    0x05: "FM",
    0x06: "WFM",        # receive only
    0x07: "CW-L",
    0x08: "RTTY-U",
}

MODE_NAME_TO_NUM: dict[str, int] = {v: k for k, v in MODE_NUM_TO_NAME.items()}

# ── Band Definitions ────────────────────────────────────────────────
# IC-7300: HF/50/70 MHz (TX 160m-6m, +4m in regions with 70 MHz).
# Same shape as the FT-710 table minus "bsr" (Icom selects bands by
# frequency; there is no band-stack register command).
BANDS: list[dict] = [
    {"name": "160m",  "start": 1_800_000,  "end": 2_000_000,  "default_freq": 1_845_500},
    {"name": "80m",   "start": 3_500_000,  "end": 4_000_000,  "default_freq": 3_850_000},
    {"name": "60m",   "start": 5_250_000,  "end": 5_450_000,  "default_freq": 5_350_000},
    {"name": "40m",   "start": 7_000_000,  "end": 7_300_000,  "default_freq": 7_050_000},
    {"name": "30m",   "start": 10_100_000, "end": 10_150_000, "default_freq": 10_140_000},
    {"name": "20m",   "start": 14_000_000, "end": 14_350_000, "default_freq": 14_270_000},
    {"name": "17m",   "start": 18_068_000, "end": 18_168_000, "default_freq": 18_132_500},
    {"name": "15m",   "start": 21_000_000, "end": 21_450_000, "default_freq": 21_400_000},
    {"name": "12m",   "start": 24_890_000, "end": 24_990_000, "default_freq": 24_952_500},
    {"name": "10m",   "start": 28_000_000, "end": 29_700_000, "default_freq": 28_450_000},
    {"name": "6m",    "start": 50_000_000, "end": 54_000_000, "default_freq": 50_150_000},
    {"name": "4m",    "start": 70_000_000, "end": 70_500_000, "default_freq": 70_250_000},
]


def get_band_for_frequency(freq_hz: int) -> dict | None:
    """Return the band dict that contains freq_hz, or None."""
    for band in BANDS:
        if band["start"] <= freq_hz <= band["end"]:
            return band
    return None


# ── Filter Model ────────────────────────────────────────────────────
# The IC-7300 has three per-mode IF filters (FIL1/FIL2/FIL3) whose
# widths are freely adjustable; CI-V selects the filter (cmd 0x03 /
# mode-set byte 1 = 01..03) instead of picking from a fixed width
# table.  capabilities.filter_model = "fil123".
FILTER_MODEL = "fil123"

# Factory default FIL1/FIL2/FIL3 widths per mode (IC-7300 Full Manual
# p. 4-5, "Selecting the IF filter").  FM widths are fixed (cannot be
# changed on the radio).
FIL_DEFAULT_WIDTHS_HZ: dict[str, list[int]] = {
    "LSB":    [3000, 2400, 1800],
    "USB":    [3000, 2400, 1800],
    "CW-U":   [1200, 500, 250],
    "CW-L":   [1200, 500, 250],
    "RTTY-L": [2400, 500, 250],
    "RTTY-U": [2400, 500, 250],
    "AM":     [9000, 6000, 3000],
    "FM":     [15000, 10000, 7000],
}

# Adjustable width ranges per mode group (manual p. 4-5); None = fixed.
FIL_WIDTH_RANGE_HZ: dict[str, tuple[int, int] | None] = {
    "SSB": (50, 3600),    # 50-500 Hz in 50 Hz steps, 600-3600 in 100 Hz
    "CW":  (50, 3600),
    "RTTY": (50, 2700),
    "AM":  (200, 10000),  # 200 Hz steps
    "FM":  None,          # fixed
}

# ── S-Meter Calibration ─────────────────────────────────────────────
# Raw 0-255 (cmd 0x15 sub 0x02) -> dB relative to S9 (same convention
# as the FT-710 table: S9 = 0).  Points from the Icom meter-reading
# documentation as encoded in wfview rigs/IC-7300.rig:
# S1=raw 10, S3=raw 30, S5=raw 60, S7=raw 90, S9=raw 120.
# NOTE: wfview's rig file lists raw 241 as +64; Icom documentation says
# S9+60 dB = raw 241.  Follow Icom.  # TODO(hw-verify)
S_METER_CAL: list[tuple[int, float]] = [
    (0, -54), (10, -48), (30, -36), (60, -24),
    (90, -12), (120, 0), (241, 60),
]


def raw_to_dbm(raw: int) -> float:
    """Raw S-meter value (0-255) -> dB relative to S9 (interpolated)."""
    return _interp(raw, S_METER_CAL)


def raw_to_s_unit(raw: int) -> str:
    """Raw S-meter value -> display string ("S7", "S9", "+20")."""
    db = raw_to_dbm(raw)
    if db < 0:
        unit = max(0, min(9, round((db + 54.0) / 6.0)))
        return f"S{unit}"
    over = int(round(min(60.0, db) / 10.0) * 10)
    return f"+{over}" if over else "S9"


# ── Meter Read Command Sub-codes (cmd 0x15) ─────────────────────────
# From wfview rigs/IC-7300.rig Commands section.
METER_SUB_S = 0x02
METER_SUB_PO = 0x11
METER_SUB_SWR = 0x12
METER_SUB_ALC = 0x13
METER_SUB_COMP = 0x14
METER_SUB_VD = 0x15
METER_SUB_ID = 0x16

# ── TX Meter Calibration ────────────────────────────────────────────
# Raw 0-255 -> engineering units.  Points from wfview rigs/IC-7300.rig
# Meters section (which encode the Icom-documented meter readings).
POWER_CAL: list[tuple[int, float]] = [        # Po meter -> watts (100W radio)
    (0, 0.0), (21, 5.0), (43, 10.0), (65, 15.0), (83, 20.0),
    (95, 25.0), (105, 30.0), (114, 35.0), (124, 40.0),
    (143, 50.0), (183, 75.0), (213, 100.0), (255, 120.0),
]
SWR_CAL: list[tuple[int, float]] = [          # SWR meter -> ratio
    (0, 1.0), (48, 1.5), (80, 2.0), (120, 3.0), (241, 6.0),
    # wfview's rig file shows the last raw value as "2410" — an obvious
    # typo for 241 (raw max); corrected here.  # TODO(hw-verify)
]
ALC_CAL: list[tuple[int, float]] = [          # ALC meter -> zone (redline at 1)
    (0, 0.0), (120, 1.0), (255, 2.0),
]
COMP_CAL: list[tuple[int, float]] = [         # COMP meter -> dB compression
    (0, 0.0), (130, 15.0), (210, 30.0),       # 30 dB = BCD 02 10 (per MK2 reference)
]
VOLTAGE_CAL: list[tuple[int, float]] = [      # Vd meter -> volts
    (0, 0.0), (19, 10.0), (185, 13.8), (241, 16.0),   # 10 V = BCD 00 13 (per MK2 reference)
]
CURRENT_CAL: list[tuple[int, float]] = [      # Id meter -> amps
    (0, 0.0), (97, 10.0), (146, 15.0), (241, 25.0),
]


def raw_to_power(raw: int) -> float:
    """Po raw 0-255 -> watts (IC-7300, 100W radio)."""
    return _interp(raw, POWER_CAL)


def raw_to_swr(raw: int) -> float:
    """SWR raw 0-255 -> SWR ratio."""
    return _interp(raw, SWR_CAL)


def raw_to_alc(raw: int) -> float:
    """ALC raw 0-255 -> ALC zone (0..2, 1 = redline)."""
    return _interp(raw, ALC_CAL)


def raw_to_comp(raw: int) -> float:
    """COMP raw 0-255 -> compression dB."""
    return _interp(raw, COMP_CAL)


def raw_to_voltage(raw: int) -> float:
    """Vd raw 0-255 -> drain volts."""
    return _interp(raw, VOLTAGE_CAL)


def raw_to_current(raw: int) -> float:
    """Id raw 0-255 -> drain current amps."""
    return _interp(raw, CURRENT_CAL)


# ── Scope Spans ─────────────────────────────────────────────────────
# Center-mode span choices (wfview rig Spans section; values match the
# Icom CI-V reference p. 10, cmd 27 15).  "freq" is the HALF-span in Hz
# (±2.5 kHz = 5 kHz total).  On the wire the span travels as a 5-byte
# BCD frequency, not as these indices — the indices are UI keys only.
SCOPE_SPANS: dict[int, dict] = {
    0: {"name": "±2.5 kHz",  "freq": 2500},
    1: {"name": "±5 kHz",    "freq": 5000},
    2: {"name": "±10 kHz",   "freq": 10000},
    3: {"name": "±25 kHz",   "freq": 25000},
    4: {"name": "±50 kHz",   "freq": 50000},
    5: {"name": "±100 kHz",  "freq": 100000},
    6: {"name": "±250 kHz",  "freq": 250000},
    7: {"name": "±500 kHz",  "freq": 500000},
}

# UI index -> half-span Hz (same numbers as SCOPE_SPANS[n]["freq"]).
SCOPE_SPAN_HZ: dict[int, int] = {k: v["freq"] for k, v in SCOPE_SPANS.items()}

# Fixed-mode edge pairs (cmd 27 16 selects edge 01-04, cmd 27 1E sets
# the frequencies).  Reasonable HF defaults covering the main bands.
# TODO(hw-verify): confirm against the radio's factory band-scope edges.
SCOPE_FIXED_EDGES: list[tuple[int, int]] = [
    (3_500_000, 4_000_000),     # Edge 1: 80m
    (7_000_000, 7_300_000),     # Edge 2: 40m
    (14_000_000, 14_350_000),   # Edge 3: 20m
    (28_000_000, 29_700_000),   # Edge 4: 10m
]

# ── Attenuator & Preamp ─────────────────────────────────────────────
# Preamp: cmd 0x16 sub 0x02, data 00/01/02 (wfview rig Commands\47).
# Attenuator: cmd 0x11, data = attenuation in dB (00 = off, 20 = on;
# wfview rig Commands\18, Attenuators 0 dB / -20 dB).  NOT 0x16 0x12 —
# that sub-code is the AGC setting on this radio (rig Commands\48).
PREAMP_LABELS: dict[int, str] = {0: "OFF", 1: "AMP1", 2: "AMP2"}
ATTENUATOR_LABELS: dict[int, str] = {0: "OFF", 20: "20dB"}
ATT_STEPS_DB: tuple[int, ...] = (0, 20)
