"""
Per-model CI-V profiles (shared Icom backend core)
==================================================
The directory name ``backends/ic7300`` is HISTORICAL, not a model
boundary: this package hosts the shared Icom CI-V core (codec,
controller, scope producer) that serves every supported Icom model.
Renaming it to ``backends/icom`` is tracked as a separate change; until
then this docstring is the authoritative note.

Every value below carries provenance.  Values that the offline evidence
does not support are inherited from the hardware-verified IC-7300 tables
and marked ``TODO(hw-verify)``; such models also set ``verified=False``
and list the affected meters in ``unverified_meters`` so the runtime can
refuse to present them as measured facts.

Sources (spec §3):
- wfview rig data: ~/HAM/ref/wfview/rigs/{IC-7300,IC-7300MK2,IC-705,
  IC-7610,IC-7760}.rig — CIVAddress, Commands\\N (Transceive item, meter
  sub-codes, command surface), Spans, Attenuators, Modes, Bands,
  SpectrumLenMax / SpectrumAmpMax / SpectrumSeqMax.
- Icom IC-7300MK2 CI-V Reference Guide A7841-8EX (repository file
  IC-7300MK2_ENG_CI-V_0.pdf) — inherited framing/scope conventions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from config import NARROW_MODES, _interp

from backends.ic7300.civ_codec import (
    SCOPE_AMPLITUDE_MAX, SCOPE_MAX_SEGMENTS, SCOPE_WAVEFORM_LEN,
)
from backends.ic7300.config_ic7300 import (
    ALC_CAL, BANDS as IC7300_BANDS, COMP_CAL, CURRENT_CAL,
    FIL_DEFAULT_WIDTHS_HZ, FIL_WIDTH_RANGE_HZ, MODE_NAME_TO_NUM,
    MODE_NUM_TO_NAME, POWER_CAL, PREAMP_LABELS, SCOPE_SPANS,
    S_METER_CAL, SWR_CAL, VOLTAGE_CAL,
)

# ── Shared satellite tables ─────────────────────────────────────────
# The rig data shows identical meter sub-codes and raw ranges
# (15 02/11/12/13/14/15/16) on all five models, so S-meter, SWR, ALC,
# COMP, Vd and Id reuse the verified IC-7300 tables.  Only the power
# curve is rescaled per radio class (10 W / 100 W / 200 W).
# TODO(hw-verify) on the three unverified models.
_S_METER_TABLE = tuple(S_METER_CAL)
_SWR_TABLE = tuple(SWR_CAL)
_ALC_TABLE = tuple(ALC_CAL)
_COMP_TABLE = tuple(COMP_CAL)
_VOLTAGE_TABLE = tuple(VOLTAGE_CAL)
_CURRENT_TABLE = tuple(CURRENT_CAL)
# raw -> fraction of rated power (the verified curve is a 100 W curve).
_POWER_FRAC = tuple((raw, watts / 100.0) for raw, watts in POWER_CAL)


@dataclass(frozen=True)
class MeterCal:
    """Piecewise-linear meter tables plus the radio's rated power."""

    s_meter: tuple = _S_METER_TABLE
    power_frac: tuple = _POWER_FRAC
    rated_power_w: float = 100.0
    swr: tuple = _SWR_TABLE
    alc: tuple = _ALC_TABLE
    comp: tuple = _COMP_TABLE
    voltage: tuple = _VOLTAGE_TABLE
    current: tuple = _CURRENT_TABLE

    def raw_to_dbm(self, raw: int) -> float:
        return _interp(raw, list(self.s_meter))

    def raw_to_s_unit(self, raw: int) -> str:
        db = self.raw_to_dbm(raw)
        if db < 0:
            unit = max(0, min(9, round((db + 54.0) / 6.0)))
            return f"S{unit}"
        over = int(round(min(60.0, db) / 10.0) * 10)
        return f"+{over}" if over else "S9"

    def raw_to_power(self, raw: int) -> float:
        return _interp(raw, list(self.power_frac)) * self.rated_power_w

    def raw_to_swr(self, raw: int) -> float:
        return _interp(raw, list(self.swr))

    def raw_to_alc(self, raw: int) -> float:
        return _interp(raw, list(self.alc))

    def raw_to_alc_pct(self, raw: int) -> float:
        return max(0.0, min(100.0, self.raw_to_alc(raw) * 100.0))

    def raw_to_comp(self, raw: int) -> float:
        return _interp(raw, list(self.comp))

    def raw_to_voltage(self, raw: int) -> float:
        return _interp(raw, list(self.voltage))

    def raw_to_current(self, raw: int) -> float:
        return _interp(raw, list(self.current))


@dataclass(frozen=True)
class CivModelProfile:
    """Static, JSON-independent description of one Icom CI-V radio."""

    model_key: str
    display_name: str
    source: str
    civ_addr: int
    transceive_item: bytes          # 1a 05 <item> 00/01
    scope_bins: int = SCOPE_WAVEFORM_LEN
    scope_amp_max: int = SCOPE_AMPLITUDE_MAX
    scope_seq_max: int = SCOPE_MAX_SEGMENTS
    scope_spans: dict = field(default_factory=lambda: dict(SCOPE_SPANS))
    bands: tuple = ()
    mode_num_to_name: dict = field(default_factory=dict)
    mode_name_to_num: dict = field(default_factory=dict)
    fil_default_widths_hz: dict = field(default_factory=dict)
    fil_width_range_hz: dict = field(default_factory=dict)
    preamp_labels: dict = field(default_factory=lambda: dict(PREAMP_LABELS))
    att_steps: tuple = (0, 20)
    meter_cal: MeterCal = field(default_factory=MeterCal)
    audio_rx_rate: int = 48000
    audio_tx_rate: int = 48000
    audio_name_hints: tuple = ("usb audio codec", "usb audio device")
    audio_gain_boost: float = 1.0
    has_atu: bool = True
    filter_model: str = "fil123"
    tune_via: str = "atu"
    dual_rx: bool = False
    mainsub_vfo: bool = False
    verified: bool = False
    unverified_meters: tuple = ()
    model_id_bytes: Optional[tuple] = None

    # ── Derived helpers (kept derived so they cannot drift) ────────

    @property
    def scope_queue_segments(self) -> int:
        """Four complete USB waveforms of segment queue capacity."""
        return 4 * self.scope_seq_max

    @property
    def transceive_cmd(self) -> bytes:
        """Full CI-V 'CI-V Transceive ON' command for this model."""
        return bytes((0x1A, 0x05)) + bytes(self.transceive_item) + b"\x01"

    def attenuator_labels(self) -> dict:
        return {i: ("OFF" if db == 0 else f"{db}dB")
                for i, db in enumerate(self.att_steps)}

    def get_band_for_frequency(self, freq_hz: int) -> Optional[dict]:
        for band in self.bands:
            if band["start"] <= freq_hz <= band["end"]:
                return band
        return None

    def filter_hz(self, mode_name: str, fil_index: int) -> Optional[int]:
        """FIL1-3 default width (Hz) for a mode — fil123 model."""
        widths = self.fil_default_widths_hz.get(mode_name)
        if widths is None or not 1 <= fil_index <= len(widths):
            return None
        return widths[fil_index - 1]

    def narrow_modes(self) -> list:
        return sorted(NARROW_MODES & set(self.mode_num_to_name.values()))


# ── Profile construction helpers ────────────────────────────────────

def _bands(*extra: dict) -> tuple:
    """IC-7300 band set (160m-6m) plus optional extra TX bands."""
    return tuple([dict(b) for b in IC7300_BANDS] + [dict(b) for b in extra])


def _modes(*extra: tuple) -> tuple:
    """IC-7300 mode table plus (register, UI name) additions."""
    num_to_name = dict(MODE_NUM_TO_NAME)
    for reg, name in extra:
        num_to_name[reg] = name
    return num_to_name, {v: k for k, v in num_to_name.items()}


_2M_70CM = (
    {"name": "2m", "start": 144_000_000, "end": 148_000_000,
     "default_freq": 145_500_000},
    {"name": "70cm", "start": 430_000_000, "end": 440_000_000,
     "default_freq": 433_500_000},
)

_IC705_MODES = _modes((6, "WFM"), (17, "DV"))
_7610_MODES = _modes((12, "PSK-U"), (13, "PSK-L"))

_ATT_3DB_STEPS = tuple(range(0, 46, 3))     # 0,3,...,45 — 16 steps

_WFVIEW = "wfview rigs/{name}.rig (local, offline)"
_UNVERIFIED = ("power", "voltage", "current")
_INHERITED_NOTE = (
    "meter curves inherited from the verified IC-7300 tables and rescaled "
    "by rated power; USB audio rate/name hints inherited from the IC-7300 "
    "profile — TODO(hw-verify) on a real unit (_diag_civ.py)"
)


PROFILES: dict[str, CivModelProfile] = {
    "ic7300": CivModelProfile(
        model_key="ic7300",
        display_name="Icom IC-7300",
        source="config_ic7300.py tables (hardware-verified in the field)",
        civ_addr=0x94,
        transceive_item=b"\x00\x71",
        bands=_bands(),
        mode_num_to_name=dict(MODE_NUM_TO_NAME),
        mode_name_to_num=dict(MODE_NAME_TO_NUM),
        fil_default_widths_hz=dict(FIL_DEFAULT_WIDTHS_HZ),
        fil_width_range_hz=dict(FIL_WIDTH_RANGE_HZ),
        att_steps=(0, 20),
        meter_cal=MeterCal(rated_power_w=100.0),
        verified=True,
    ),
    "ic7300mk2": CivModelProfile(
        model_key="ic7300mk2",
        display_name="Icom IC-7300MK2",
        source="config_ic7300.py tables + IC-7300MK2 CI-V reference",
        civ_addr=0xB6,
        transceive_item=b"\x00\x89",
        bands=_bands(),
        mode_num_to_name=dict(MODE_NUM_TO_NAME),
        mode_name_to_num=dict(MODE_NAME_TO_NUM),
        fil_default_widths_hz=dict(FIL_DEFAULT_WIDTHS_HZ),
        fil_width_range_hz=dict(FIL_WIDTH_RANGE_HZ),
        att_steps=(0, 20),
        meter_cal=MeterCal(rated_power_w=100.0),
        verified=True,
    ),
    "ic705": CivModelProfile(
        model_key="ic705",
        display_name="Icom IC-705",
        source=_WFVIEW.format(name="IC-705") + "; " + _INHERITED_NOTE,
        civ_addr=0xA4,
        transceive_item=b"\x01\x12",
        bands=_bands(*_2M_70CM),
        mode_num_to_name=_IC705_MODES[0],
        mode_name_to_num=_IC705_MODES[1],
        fil_default_widths_hz=dict(FIL_DEFAULT_WIDTHS_HZ),
        fil_width_range_hz=dict(FIL_WIDTH_RANGE_HZ),
        att_steps=(0, 20),
        meter_cal=MeterCal(rated_power_w=10.0),
        audio_name_hints=("ic-705", "usb audio codec", "usb audio device"),
        verified=False,
        unverified_meters=_UNVERIFIED,
    ),
    "ic7610": CivModelProfile(
        model_key="ic7610",
        display_name="Icom IC-7610",
        source=_WFVIEW.format(name="IC-7610") + "; " + _INHERITED_NOTE,
        civ_addr=0x98,
        transceive_item=b"\x01\x12",
        scope_bins=689,
        scope_amp_max=200,
        scope_seq_max=15,
        bands=_bands(),
        mode_num_to_name=_7610_MODES[0],
        mode_name_to_num=_7610_MODES[1],
        fil_default_widths_hz=dict(FIL_DEFAULT_WIDTHS_HZ),
        fil_width_range_hz=dict(FIL_WIDTH_RANGE_HZ),
        att_steps=_ATT_3DB_STEPS,
        meter_cal=MeterCal(rated_power_w=100.0),
        audio_name_hints=("ic-7610", "usb audio codec", "usb audio device"),
        dual_rx=True,
        mainsub_vfo=True,
        verified=False,
        unverified_meters=_UNVERIFIED,
    ),
    "ic7760": CivModelProfile(
        model_key="ic7760",
        display_name="Icom IC-7760",
        source=_WFVIEW.format(name="IC-7760") + "; " + _INHERITED_NOTE,
        civ_addr=0xB2,
        transceive_item=b"\x01\x50",
        scope_bins=689,
        scope_amp_max=200,
        scope_seq_max=15,
        bands=_bands(),
        mode_num_to_name=_7610_MODES[0],
        mode_name_to_num=_7610_MODES[1],
        fil_default_widths_hz=dict(FIL_DEFAULT_WIDTHS_HZ),
        fil_width_range_hz=dict(FIL_WIDTH_RANGE_HZ),
        att_steps=_ATT_3DB_STEPS,
        meter_cal=MeterCal(rated_power_w=200.0),
        audio_name_hints=("ic-7760", "usb audio codec", "usb audio device"),
        dual_rx=True,
        mainsub_vfo=True,
        verified=False,
        unverified_meters=_UNVERIFIED,
    ),
}


def known_models() -> tuple:
    """Icom model keys that have a profile, in registry order."""
    return tuple(PROFILES)


def get_profile(model_key: str) -> CivModelProfile:
    """Look up a profile by model key.

    Tolerant of case, surrounding whitespace and separators so that the
    human-readable spelling (``IC-705``) works in the diagnostic CLI
    alongside the registry key (``ic705``).
    """
    key = (model_key or "").strip().lower().replace("-", "").replace("_", "")
    profile = PROFILES.get(key)
    if profile is None:
        raise ValueError(
            f"unknown Icom model {model_key!r} (known: {', '.join(PROFILES)})")
    return profile
