"""Per-model Yaesu facts (spec 2026-09-12 §5).

Single source of truth for everything that differs between the ASCII-CAT
Yaesu radios this project supports.  Nothing here performs I/O and nothing
here talks to a radio: the tables are data, and every table records the
offline source it was transcribed from in ``YaesuModelProfile.provenance``.

None of the four models has been verified against hardware (spec §2 D2).
Fields that no offline source could supply carry a ``TODO(hw-verify)``
marker in ``provenance`` and are never presented as verified: ``id_answer``
is empty for the three models whose answer is unknown (log-only), meter
curves ship in ``unverified_meters``, and ``tx_gated`` keeps transmit off
until the operator sets ``MRRC_ALLOW_UNVERIFIED_TX=1``.

Two contracts shape the mode tables and are deliberately not "simplified":

- The register is an ``int`` — ``RadioBackend.set_mode(mode_num: int)``,
  ``RadioState.mode: int`` and ``MODE_NUM_TO_NAME: dict[int, str]`` all
  speak integers.
- The CAT character is a separate lookup (``mode_codes``) because the
  FTX-1 uses ``H`` and ``I`` for its C4FM variants, and those are not hex
  digits: formatting the register would emit ``MD011`` instead of ``MD0I``
  (design review finding, 2026-09-12).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Tuple

# ── Meter calibration ───────────────────────────────────────────────

@dataclass(frozen=True)
class MeterCal:
    """Piecewise-linear raw -> value table (raw 0..255).

    ``points`` is an ascending sequence of ``(raw, value)`` samples; values
    between samples are linearly interpolated and out-of-range raws clamp to
    the end points, which is what the radios' own metering does at the ends
    of the scale.
    """

    points: Tuple[Tuple[int, float], ...]
    unit: str = "dBm"

    def value(self, raw: int) -> float:
        pts = self.points
        if raw <= pts[0][0]:
            return float(pts[0][1])
        if raw >= pts[-1][0]:
            return float(pts[-1][1])
        for (r0, v0), (r1, v1) in zip(pts, pts[1:]):
            if r0 <= raw <= r1:
                span = r1 - r0
                if span == 0:
                    return float(v1)
                frac = (raw - r0) / span
                return float(v0) + (float(v1) - float(v0)) * frac
        return float(pts[-1][1])            # unreachable, keeps the type honest

    def s_unit(self, raw: int) -> str:
        """Format a raw reading as an S-unit string.

        The family convention (all four models) is S9 = 0 dBm with 6 dB per
        S-unit; values above S9 are shown as ``S9+NN``.
        """
        dbm = self.value(raw)
        if dbm >= 0:
            return f"S9+{int(round(dbm))}"
        s_units = 9 + dbm / 6.0
        if s_units <= 1:
            return "S1"
        return f"S{int(s_units)}"


# ── Profile ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class YaesuModelProfile:
    """Everything the shared Yaesu core needs to know about one model."""

    model_key: str
    display_name: str
    default_baud: int

    # Identity
    id_answer: str                       # expected "ID;" answer, "" = log only

    # Modes / filters
    mode_numbers: Dict[str, int]         # UI mode name -> mode register (int)
    mode_codes: Dict[int, str]           # mode register -> MD P2 character
    ui_modes: Tuple[str, ...]            # order of the UI mode cycle button
    filter_widths: Dict[str, Tuple[Tuple[int, int], ...]]   # mode -> ((idx, Hz), ...)

    # Bands / RF
    bands: Tuple[Tuple[str, int, int], ...]   # (label, low_hz, high_hz)
    att_steps: Tuple[int, ...]
    preamp_labels: Dict[int, str]
    power_format: str                    # "PC1" | "PC2" | "auto"
    power_max_w: int
    has_atu: bool
    has_vd_id_meters: bool
    vfo_b_direct: bool
    tune_via: str                        # "tx2" | "atu"
    mode_set_leaves_memory: bool = False  # FTX-1: force leaving memory first

    # Meters
    s_meter_cal: MeterCal = field(
        default_factory=lambda: MeterCal(points=((0, -54.0), (255, 60.0))))

    # Audio
    audio_rx_rate: int = 44100
    audio_tx_rate: int = 44100
    audio_name_hints: Tuple[str, ...] = ()
    audio_gain_boost: float = 1.0

    # Verification boundary (spec §6)
    verified: bool = False
    tx_gated: bool = True
    dual_rx: bool = False                # recorded, not implemented (phase 2)
    unverified_meters: Tuple[str, ...] = ()
    provenance: Dict[str, str] = field(default_factory=dict)


# ── Shared tables ───────────────────────────────────────────────────
# Hamlib 4.7.2 rigs/yaesu/newcat.c:12043 newcat_mode_conv[] — the ASCII-CAT
# family mode characters.  Registers are ints because that is the contract
# (`RadioBackend.set_mode(mode_num: int)`, `RadioState.mode: int`,
# `MODE_NUM_TO_NAME: dict[int, str]`).  The CAT layer needs the character,
# and H/I are NOT hex digits, so the character is looked up in `mode_codes`
# rather than formatted as hex: `f"{0x11:X}"` would send "MD011" on an FTX-1
# C4FM-VW change instead of "MD0I" (design review finding, 2026-09-12).
_FAMILY_MODE_NUMBERS = {
    "LSB": 0x1, "USB": 0x2, "CW-U": 0x3, "FM": 0x4, "AM": 0x5,
    "RTTY-L": 0x6, "CW-L": 0x7, "DATA-L": 0x8, "RTTY-U": 0x9,
    "DATA-FM": 0xA, "FM-N": 0xB, "DATA-U": 0xC, "AM-N": 0xD,
    "C4FM": 0xE, "DATA-FM-N": 0xF,
}

_FAMILY_MODE_CODES = {
    0x1: "1", 0x2: "2", 0x3: "3", 0x4: "4", 0x5: "5", 0x6: "6",
    0x7: "7", 0x8: "8", 0x9: "9", 0xA: "A", 0xB: "B", 0xC: "C",
    0xD: "D", 0xE: "E", 0xF: "F",
}

_FAMILY_UI_MODES = ("LSB", "USB", "CW-U", "CW-L", "AM", "FM", "DATA-U", "RTTY-U")

# Hamlib ftdx10.c:256 /.filters and ftdx101.c:299 /.filters are identical for
# these two models; index 1 is "wide" because the radios label the widest
# slot 1 in their own menus.
_FAMILY_FILTER_WIDTHS = {
    "CW-U": ((1, 2400), (2, 600), (3, 300), (4, 1200)),
    "CW-L": ((1, 2400), (2, 600), (3, 300), (4, 1200)),
    "RTTY-U": ((1, 2400), (2, 600), (3, 300), (4, 1200)),
    "DATA-U": ((1, 2400), (2, 600), (3, 300), (4, 1200)),
    "SSB": ((1, 3000), (2, 2400), (3, 1800)),
    "USB": ((1, 3000), (2, 2400), (3, 1800)),
    "LSB": ((1, 3000), (2, 2400), (3, 1800)),
    "AM": ((1, 9000), (2, 6000)),
    "FM": ((1, 16000), (2, 9000)),
}

# Hamlib newcat.c:339 yaesu_default_str_cal (used by every family model that
# does not define its own curve — the FTDX10 among them).
_DEFAULT_S_CAL = MeterCal(points=(
    (0, -54.0), (26, -42.0), (51, -30.0), (81, -18.0), (105, -9.0),
    (130, 0.0), (157, 12.0), (186, 25.0), (203, 35.0), (237, 50.0),
    (255, 60.0),
))

# Hamlib ftdx101.h:164 FTDX101D_STR_CAL — 12 points, S9 at raw 160.
_FTDX101_S_CAL = MeterCal(points=(
    (0, -60.0), (17, -54.0), (25, -48.0), (34, -42.0), (51, -36.0),
    (68, -30.0), (85, -24.0), (102, -18.0), (119, -12.0), (136, -6.0),
    (160, 0.0), (255, 60.0),
))

# Hamlib ftx1/ftx1.h:105 FTX1_STR_CAL — 16 points, one per S-unit up to S9.
_FTX1_S_CAL = MeterCal(points=(
    (0, -54.0), (12, -48.0), (27, -42.0), (40, -36.0), (55, -30.0),
    (65, -24.0), (80, -18.0), (95, -12.0), (112, -6.0), (130, 0.0),
    (150, 10.0), (172, 20.0), (190, 30.0), (220, 40.0), (240, 50.0),
    (255, 60.0),
))

_HF_BANDS = (
    ("160m", 1_800_000, 2_000_000),
    ("80m", 3_500_000, 4_000_000),
    ("60m", 5_167_500, 5_406_500),
    ("40m", 7_000_000, 7_300_000),
    ("30m", 10_100_000, 10_150_000),
    ("20m", 14_000_000, 14_350_000),
    ("17m", 18_068_000, 18_168_000),
    ("15m", 21_000_000, 21_450_000),
    ("12m", 24_890_000, 24_990_000),
    ("10m", 28_000_000, 29_700_000),
    ("6m", 50_000_000, 54_000_000),
)

_FTX1_BANDS = _HF_BANDS + (
    ("2m", 144_000_000, 148_000_000),
    ("70cm", 430_000_000, 450_000_000),
)

_HF_ATT_STEPS = (0, 6, 12, 18)
_FAMILY_PREAMP_LABELS = {0: "OFF", 1: "AMP1", 2: "AMP2"}

# The FT-710 family's USB audio device naming, which the FT-710 backend
# already uses successfully (`audio_handler` matches by substring).
_YAESU_AUDIO_HINTS = ("USB Audio CODEC", "YAESU", "USB Audio Device")


def _provenance(overrides: Dict[str, str]) -> Dict[str, str]:
    """Base provenance for the shared tables, with per-model overrides."""
    base = {
        "mode_numbers": "Hamlib 4.7.2 rigs/yaesu/newcat.c:12043 newcat_mode_conv[]",
        "filter_widths": "Hamlib 4.7.2 rigs/yaesu/ftdx10.c:256 and ftdx101.c:299 .filters",
        "s_meter_cal": "Hamlib 4.7.2 rigs/yaesu/newcat.c:339 yaesu_default_str_cal",
        "bands": "Hamlib 4.7.2 rigs/yaesu/ftx1/ftx1.c:1096 rx_range_list1 + ITU HF band plan",
        "power_format": "TODO(hw-verify): power format not yet read back from hardware",
        "model_overall": "TODO(hw-verify): no hardware for this model (spec 2026-09-12 §2 D2)",
        "audio_rates": "TODO(hw-verify): USB audio rate assumption, not documented per model",
    }
    base.update(overrides)
    return base


# ── Profiles ────────────────────────────────────────────────────────

FTDX10 = YaesuModelProfile(
    model_key="ftdx10",
    display_name="Yaesu FTDX10",
    default_baud=38400,
    id_answer="",                        # TODO(hw-verify)
    mode_numbers=dict(_FAMILY_MODE_NUMBERS),
    mode_codes=dict(_FAMILY_MODE_CODES),
    ui_modes=_FAMILY_UI_MODES,
    filter_widths=dict(_FAMILY_FILTER_WIDTHS),
    bands=_HF_BANDS,
    att_steps=_HF_ATT_STEPS,
    preamp_labels=dict(_FAMILY_PREAMP_LABELS),
    power_format="PC1",
    power_max_w=100,
    has_atu=True,
    has_vd_id_meters=False,
    vfo_b_direct=True,
    tune_via="tx2",
    s_meter_cal=_DEFAULT_S_CAL,
    audio_name_hints=_YAESU_AUDIO_HINTS,
    dual_rx=False,
    unverified_meters=("s_meter", "power", "swr", "alc", "comp"),
    provenance=_provenance({
        "s_meter_cal": "Hamlib 4.7.2 rigs/yaesu/newcat.c:339 yaesu_default_str_cal"
                       " (FTDX10 defines no curve of its own)",
    }),
)

FTDX101D = YaesuModelProfile(
    model_key="ftdx101d",
    display_name="Yaesu FTDX101D",
    default_baud=38400,
    id_answer="",                        # TODO(hw-verify)
    mode_numbers=dict(_FAMILY_MODE_NUMBERS),
    mode_codes=dict(_FAMILY_MODE_CODES),
    ui_modes=_FAMILY_UI_MODES,
    filter_widths=dict(_FAMILY_FILTER_WIDTHS),
    bands=_HF_BANDS,
    att_steps=(0, 3, 6, 9, 12, 15, 18, 21, 24),
    preamp_labels=dict(_FAMILY_PREAMP_LABELS),
    power_format="PC1",
    power_max_w=100,
    has_atu=True,
    has_vd_id_meters=True,
    vfo_b_direct=True,
    tune_via="tx2",
    s_meter_cal=_FTDX101_S_CAL,
    audio_name_hints=_YAESU_AUDIO_HINTS,
    dual_rx=True,
    unverified_meters=("s_meter", "power", "swr", "alc", "comp", "vd", "id"),
    provenance=_provenance({
        "s_meter_cal": "Hamlib 4.7.2 rigs/yaesu/ftdx101.h:164 FTDX101D_STR_CAL",
        "att_steps": "TODO(hw-verify): 3 dB family steps assumed",
    }),
)

FTDX101MP = YaesuModelProfile(
    model_key="ftdx101mp",
    display_name="Yaesu FTDX101MP",
    default_baud=38400,
    id_answer="",                        # TODO(hw-verify)
    mode_numbers=dict(_FAMILY_MODE_NUMBERS),
    mode_codes=dict(_FAMILY_MODE_CODES),
    ui_modes=_FAMILY_UI_MODES,
    filter_widths=dict(_FAMILY_FILTER_WIDTHS),
    bands=_HF_BANDS,
    att_steps=(0, 3, 6, 9, 12, 15, 18, 21, 24),
    preamp_labels=dict(_FAMILY_PREAMP_LABELS),
    power_format="PC1",
    power_max_w=200,
    has_atu=True,
    has_vd_id_meters=True,
    vfo_b_direct=True,
    tune_via="tx2",
    s_meter_cal=_FTDX101_S_CAL,
    audio_name_hints=_YAESU_AUDIO_HINTS,
    dual_rx=True,
    unverified_meters=("s_meter", "power", "swr", "alc", "comp", "vd", "id"),
    provenance=_provenance({
        "s_meter_cal": "Hamlib 4.7.2 rigs/yaesu/ftdx101.h:164 FTDX101D_STR_CAL"
                       " (ftdx101mp.c:142 uses the same table)",
        "power_format": "Hamlib 4.7.2 rigs/yaesu/ftdx101mp.c (200 W class)",
    }),
)

FTX1 = YaesuModelProfile(
    model_key="ftx1",
    display_name="Yaesu FTX-1F",
    default_baud=38400,
    id_answer="0840",                    # ftx1/ftx1_readme.txt: all configs
    mode_numbers={
        "LSB": 0x1, "USB": 0x2, "CW-U": 0x3, "FM": 0x4, "AM": 0x5,
        "RTTY-L": 0x6, "CW-L": 0x7, "DATA-L": 0x8, "RTTY-U": 0x9,
        "DATA-FM": 0xA, "FM-N": 0xB, "DATA-U": 0xC, "AM-N": 0xD,
        "PSK": 0xE, "DATA-FM-N": 0xF, "C4FM-DN": 0x10, "C4FM-VW": 0x11,
    },
    mode_codes={**_FAMILY_MODE_CODES, 0x10: "H", 0x11: "I"},
    ui_modes=("LSB", "USB", "CW-U", "CW-L", "AM", "FM", "DATA-U", "PSK"),
    filter_widths=dict(_FAMILY_FILTER_WIDTHS),
    bands=_FTX1_BANDS,
    att_steps=(0, 3, 6, 9, 12, 15, 18, 21, 24),
    preamp_labels=dict(_FAMILY_PREAMP_LABELS),
    power_format="auto",                 # PC1xxx (6/10 W) or PC2xxx (100 W)
    power_max_w=100,
    has_atu=True,
    has_vd_id_meters=False,
    vfo_b_direct=True,
    tune_via="tx2",
    mode_set_leaves_memory=True,         # ftx1/ftx1_mode.c: memory MD is transient
    s_meter_cal=_FTX1_S_CAL,
    audio_name_hints=_YAESU_AUDIO_HINTS,
    dual_rx=True,
    unverified_meters=("s_meter", "power", "swr", "alc", "comp"),
    provenance=_provenance({
        "mode_numbers": "Hamlib 4.7.2 rigs/yaesu/ftx1/ftx1_mode.c (E=PSK, H=I=C4FM)",
        "s_meter_cal": "Hamlib 4.7.2 rigs/yaesu/ftx1/ftx1.h:105 FTX1_STR_CAL",
        "bands": "Hamlib 4.7.2 rigs/yaesu/ftx1/ftx1.c:1096 rx_range_list1"
                 " (30 kHz-56 MHz, 118-164 MHz AM/FM, 430-470 MHz)",
        "power_format": "Hamlib 4.7.2 rigs/yaesu/ftx1/ftx1_readme.txt"
                        " (Field head PC1xxx 6/10 W, SPA-1 PC2xxx 5-100 W)",
        "filter_widths": "TODO(hw-verify): family widths assumed;"
                         " ftx1_filter.c confirms SH is read/write",
    }),
)

PROFILES: Dict[str, YaesuModelProfile] = {
    p.model_key: p for p in (FTDX10, FTDX101D, FTDX101MP, FTX1)
}


def get_profile(model: str) -> YaesuModelProfile:
    """Return the profile for ``model`` (raises KeyError when unknown)."""
    key = (model or "").strip().lower()
    return PROFILES[key]


def known_models() -> Tuple[str, ...]:
    """Registered Yaesu model keys, in registry order."""
    return tuple(PROFILES)
