"""Per-model Yaesu facts (spec 2026-09-12 §5).

Single source of truth for everything that differs between the ASCII-CAT
Yaesu radios this project supports.  Nothing here performs I/O and nothing
here talks to a radio: the tables are data, and every table records the
offline source it was transcribed from in ``YaesuModelProfile.provenance``.

None of the five models has been verified against hardware (spec §2 D2; the
FT-891 by design 2026-10-05 D2).
Fields that no offline source could supply carry a ``TODO(hw-verify)``
marker in ``provenance`` and are never presented as verified: ``id_answer``
is empty for the four models whose answer is unknown (log-only), meter
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
            return pts[0][1]
        if raw >= pts[-1][0]:
            return pts[-1][1]
        for (r0, v0), (r1, v1) in zip(pts, pts[1:]):
            if r0 <= raw <= r1:
                span = r1 - r0
                if span == 0:
                    return v1
                frac = (raw - r0) / span
                return v0 + (v1 - v0) * frac
        return pts[-1][1]                   # unreachable, keeps the type honest

    def s_unit(self, raw: int) -> str:
        """Format a raw reading as an S-unit string.

        The family convention (all four models) is S9 = 0 dBm with 6 dB per
        S-unit; values above S9 are shown as ``S9+NN``.
        """
        dbm = self.value(raw)
        if dbm >= 0:
            return f"S9+{round(dbm)}"
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
    # Filter-width SET prefix = "SH" + P1 (0 = main VFO) + P2 (bandwidth on).
    # The family is "SH00"; the FT-891 needs P2=1 (Hamlib 4.7.2
    # rigs/yaesu/newcat.c:9658-9663, `int on = is_ft891`).  Never hardcode the
    # 3-character "SH0NN" form — the radio silently ignores it.
    filter_width_prefix: str = "SH00"

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

# Hamlib ft891.h:88 FT891_STR_CAL — 16 points, S9 at raw 130. Hamlib marks it
# "/* TBC */"; it is point-for-point identical to ftx1/ftx1.h:105 FTX1_STR_CAL,
# so the same table is reused rather than transcribed twice.
_FT891_S_CAL = _FTX1_S_CAL

# Hamlib newcat.c:10099-10153 (get side) — the FT-891's CW/RTTY/PKT slots.
# Slot 0 is excluded on purpose: its width depends on the NA (narrow) flag
# (CW narrow?500:2400, RTTY/PKT narrow?300:500) and this UI never sends it.
_FT891_NARROW_FILTERS = (
    (1, 50), (2, 100), (3, 150), (4, 200), (5, 250), (6, 300), (7, 350),
    (8, 400), (9, 450), (10, 500), (11, 800), (12, 1200), (13, 1400),
    (14, 1700), (15, 2000), (16, 2400), (17, 3000),
)

# Hamlib newcat.c:10162-10208 (get side) — the FT-891's SSB slots. Slot 21 is
# 3200 Hz here; the set side (newcat.c:8919-8920) comments the same slot as
# 3000 Hz, and ft891.c:245-290 .filters lists a 2250 Hz width that has no slot
# at all. The get side wins: it answers "what does this slot mean".
_FT891_VOICE_FILTERS = (
    (1, 200), (2, 400), (3, 600), (4, 850), (5, 1100), (6, 1350), (7, 1500),
    (8, 1650), (9, 1800), (10, 1950), (11, 2100), (12, 2200), (13, 2300),
    (14, 2400), (15, 2500), (16, 2600), (17, 2700), (18, 2800), (19, 2900),
    (20, 3000), (21, 3200),
)

# AM/FM are absent on purpose: newcat.c:8924-8936 sets their width through NA
# only and never writes an SH index, and 10217-10228 returns fixed values
# (AM 9000, AM-N 6000, FM/PKT-FM 16000). Giving them slot numbers would be
# invented data. Known consequence (design 2026-10-05 §12): in AM/FM the UI
# falls back to the voice list, so those labels do not match the radio's fixed
# width.
_FT891_FILTER_WIDTHS = {
    "CW-U": _FT891_NARROW_FILTERS, "CW-L": _FT891_NARROW_FILTERS,
    "RTTY-U": _FT891_NARROW_FILTERS, "RTTY-L": _FT891_NARROW_FILTERS,
    "DATA-U": _FT891_NARROW_FILTERS, "DATA-L": _FT891_NARROW_FILTERS,
    "SSB": _FT891_VOICE_FILTERS, "USB": _FT891_VOICE_FILTERS,
    "LSB": _FT891_VOICE_FILTERS,
}

# Hamlib newcat.c:12043 newcat_mode_conv[] minus the four registers the FT-891
# does not have: ft891.h:35-36 FT891_ALL_RX_MODES carries no C4FM, no PKT-FM
# and no AM-N (DATA-FM 0xA, AM-N 0xD, C4FM 0xE, DATA-FM-N 0xF all dropped).
_FT891_MODE_NUMBERS = {
    "LSB": 0x1, "USB": 0x2, "CW-U": 0x3, "FM": 0x4, "AM": 0x5,
    "RTTY-L": 0x6, "CW-L": 0x7, "DATA-L": 0x8, "RTTY-U": 0x9,
    "FM-N": 0xB, "DATA-U": 0xC,
}

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

FT891 = YaesuModelProfile(
    model_key="ft891",
    display_name="Yaesu FT-891",
    default_baud=38400,
    id_answer="",                        # D6: derivable as "0135", not recorded
    mode_numbers=dict(_FT891_MODE_NUMBERS),
    mode_codes=dict(_FAMILY_MODE_CODES),
    ui_modes=_FAMILY_UI_MODES,
    filter_widths=dict(_FT891_FILTER_WIDTHS),
    bands=_HF_BANDS,
    att_steps=(0, 12),
    preamp_labels={0: "OFF", 1: "AMP1"},
    power_format="PC1",
    power_max_w=100,
    has_atu=False,                       # no internal ATU (D7)
    has_vd_id_meters=False,              # ID_METER present, VD absent
    vfo_b_direct=True,
    tune_via="tx2",
    filter_width_prefix="SH01",          # Hamlib newcat.c:9661 `int on = is_ft891`
    s_meter_cal=_FT891_S_CAL,
    audio_rx_rate=48000,                 # no USB codec: external interface (D3)
    audio_tx_rate=48000,
    audio_name_hints=(),                 # D3: nothing to auto-match
    dual_rx=False,
    unverified_meters=("s_meter", "power", "swr", "alc", "comp"),
    provenance=_provenance({
        "mode_numbers": "Hamlib 4.7.2 rigs/yaesu/newcat.c:12043 newcat_mode_conv[]"
                        " minus the registers ft891.h:35-36 FT891_ALL_RX_MODES"
                        " does not carry (C4FM, DATA-FM, AM-N, DATA-FM-N)",
        "filter_widths": "Hamlib 4.7.2 rigs/yaesu/newcat.c:10099-10153"
                         " (CW/RTTY/PKT slots 1-17) and 10162-10208 (SSB slots"
                         " 1-21). AM/FM omitted: 8924-8936 sets them via NA only"
                         " and 10217-10228 returns fixed widths. Hamlib disagrees"
                         " with itself at slot 21 (set side comments 3000, get"
                         " side returns 3200) and ft891.c:245-290 .filters lists"
                         " an SSB 2250 with no slot. TODO(hw-verify)",
        "s_meter_cal": "Hamlib 4.7.2 rigs/yaesu/ft891.h:88 FT891_STR_CAL, marked"
                       " /* TBC */ there and point-identical to ftx1/ftx1.h:105."
                       " Field report hub/mrrc/dist/support_answers/"
                       "20260917-073700-14ef.digest.md: readings ran 1-1.5 S above"
                       " the radio's own display. Curve deliberately not adjusted"
                       " from another product's observation. TODO(hw-verify)",
        "bands": "Hamlib 4.7.2 rigs/yaesu/ft891.c:202-204 rx_range_list1"
                 " (30 kHz-470 MHz) and 207-213 tx_range_list1 (HF + 6 m only)",
        "power_format": "Hamlib 4.7.2 rigs/yaesu/ft891.c:207-213: 5-100 W for"
                        " SSB/CW/FM, 2-25 W for AM. The AM ceiling is not modelled"
                        " (power_max_w is a single value, as for the whole family)."
                        " TODO(hw-verify)",
        "att_steps": "Hamlib 4.7.2 rigs/yaesu/ft891.c:181 .attenuator = { 12 }",
        "preamp_labels": "Hamlib 4.7.2 rigs/yaesu/ft891.c:180 .preamp = { 10 },"
                         " marked /* TBC */ in the source. TODO(hw-verify)",
        "filter_width_prefix": "Hamlib 4.7.2 rigs/yaesu/newcat.c:9658-9663:"
                               " `int on = is_ft891;` then \"SH%c%d%02d;\", so"
                               " P2=1 for the FT-891 and P2=0 for FTDX101D/MP."
                               " TODO(hw-verify)",
        "has_atu": "No internal ATU. Hamlib marks AC valid (newcat.c:470) and"
                   " ft891.h:66 FT891_FUNCS carries RIG_FUNC_TUNER for an external"
                   " tuner; has_atu=False so the UI hides a control we cannot"
                   " verify (static/ft710_ui.js:1183). TUNE stays a TX2 carrier,"
                   " which is what an external ATU keys on. TODO(hw-verify)",
        "has_vd_id_meters": "Hamlib 4.7.2 rigs/yaesu/ft891.h:51-61 FT891_LEVELS"
                            " carries ID_METER but no VD; the combined flag is"
                            " False, so the Id meter stays hidden."
                            " TODO(hw-verify)",
        "vfo_b_direct": "Hamlib 4.7.2 rigs/yaesu/ft891.h:31 FT891_VFO_ALL includes"
                        " RIG_VFO_B; ft891.c:189 targetable_vfo is"
                        " RIG_TARGETABLE_FREQ. TODO(hw-verify)",
        "id_answer": "Left empty on purpose (design 2026-10-05 D6). A derivation"
                     " exists (newcat.c:58 NC_RIGID_FT891=135, 11212-11227 atoi of"
                     " the ID; answer, 51 `ID 0310 == 310`, cross-checked by the"
                     " documented FTX-1 0840 == NC_RIGID_FTX1), but an inferred"
                     " value is not recorded as an expectation. TODO(hw-verify)",
        "serial": "Field evidence, not Hamlib: the FT-891 report"
                  " hub/mrrc/dist/support_answers/20260917-073700-14ef.digest.md"
                  " ran rig_model=1036 at 38400 8N1 (data_bits=8, stop_bits=1,"
                  " parity=None), pinned by"
                  " hub/mrrc/tests/test_rigctld_supervisor.py:82. Hamlib"
                  " ft891.c:143 serial_stop_bits=2 is self-marked `Assumed since"
                  " manual makes no mention` and is not followed; ft891.c:140"
                  " hints the manual's default rate may be 4800.",
        "audio_rates": "The FT-891 has no USB sound card (the USB port is CAT"
                       " only), so there is no radio-side rate: the field report's"
                       " audio devices were the motherboard's `Realtek High"
                       " Definition` pair, not a Yaesu codec. 48 kHz is the normal"
                       " rate for an external interface on the DATA/ACC port and is"
                       " already a proven path here (the IC-7300 family runs 48 kHz"
                       " end to end). Operator knowledge plus field report; no"
                       " offline manual. TODO(hw-verify)",
        "model_overall": "No hardware for this model (design 2026-10-05 D2). Every"
                         " table above is offline-derived; TX stays gated behind"
                         " MRRC_ALLOW_UNVERIFIED_TX. TODO(hw-verify)",
    }),
)

PROFILES: Dict[str, YaesuModelProfile] = {
    p.model_key: p for p in (FTDX10, FTDX101D, FTDX101MP, FTX1, FT891)
}


def get_profile(model: str) -> YaesuModelProfile:
    """Return the profile for ``model`` (raises KeyError when unknown)."""
    key = (model or "").strip().lower()
    return PROFILES[key]


def known_models() -> Tuple[str, ...]:
    """Registered Yaesu model keys, in registry order."""
    return tuple(PROFILES)
