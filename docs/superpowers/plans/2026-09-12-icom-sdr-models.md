# ICOM SDR Model Support (IC-705 / IC-7610 / IC-7760) Implementation Plan

> **For AI agent workers:** Required sub-skill: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Track progress with the checkboxes below.

**Goal:** Add the Icom IC-705, IC-7610 and IC-7760 to the pluggable backend architecture by turning the existing IC-7300 CI-V implementation into a profile-driven shared core, with an explicit unverified-model boundary (capability flags, TX gate, diagnostic script) because no physical unit of the three models is available.

**Architecture:** `backends/ic7300/civ_profiles.py` becomes the single source of truth for per-model CI-V facts (address, Transceive item, scope geometry, bands, modes, attenuator steps, meter calibration). `civ_codec`/`civ_controller`/`civ_scope` gain optional parameters whose defaults reproduce today's IC-7300 constants exactly, so the verified path is unchanged; `IC7300Backend` derives every table from its profile and the three new backends are ~10-line subclasses. The server validates models from the registry instead of a hardcoded tuple, refuses PTT/TUNE on unverified profiles unless `MRRC_ALLOW_UNVERIFIED_TX=1`, and reports model identity from a read-only `19 00` query.

**Tech stack:** Python 3.13, `unittest` / `IsolatedAsyncioTestCase`, `unittest.mock`, asyncio, pyserial via `CivController`, FastAPI, vanilla JavaScript, SDD Guardian.

**Design authority:** `docs/superpowers/specs/2026-09-12-icom-sdr-models-design.md` (committed `13c6a0a`).

---

## File Structure

- **Create** `backends/ic7300/civ_profiles.py`: `MeterCal`, `CivModelProfile`, `PROFILES`, `get_profile()`, `known_models()`. One responsibility: per-model CI-V facts, no I/O.
- **Create** `_diag_civ.py`: model-agnostic CI-V self-check driving the production `CivController` (no direct serial I/O), emits a paste-ready report.
- **Create** `tests/test_civ_profiles.py`, `tests/test_unverified_tx_gate.py`, `tests/test_model_mismatch.py`, `tests/test_diag_civ.py`.
- **Modify** `backends/ic7300/civ_codec.py`: `ScopeAssembler(seq_max=…, expected_bins=…)` with warn-once drift detection; module constants stay as defaults.
- **Modify** `backends/ic7300/civ_scope.py`: `CivScopeProducer(..., amp_max=…, expected_bins=…, seq_max=…)`.
- **Modify** `backends/ic7300/civ_controller.py`: optional `profile`/`att_steps`, profile-derived queue capacity and model label, new `get_model_id()`.
- **Modify** `backends/ic7300/backend.py`: profile-driven tables/capabilities, TX gate, identity check, three new backend classes.
- **Modify** `backends/base.py`: five new `RadioCapabilities` fields.
- **Modify** `backends/__init__.py`: three registry entries + `known_models()`.
- **Modify** `radio_state.py`: `model_mismatch` field.
- **Modify** `config.py`: `_env_bool`, three baud entries, `ALLOW_UNVERIFIED_TX`.
- **Modify** `server.py`: registry-driven model validation, capability-driven attenuator bound, TX-gate error message, unverified-model startup warning.
- **Modify** `static/index.html`, `static/ft710_main.js`, `static/ft710_ui.js`, `static/sw.js`: three dialog options, capability-driven audio boost, experimental/TX-disabled badge, cache-version bump.
- **Modify** `packaging/pyinstaller/mrrc_modern_server.spec`: hiddenimport for the new module.
- **Modify tests**: `test_backend_factory.py`, `test_civ_codec.py`, `test_civ_controller.py`, `test_civ_scope.py`, `test_server_ws_protocol.py`, `test_audio.py`.
- **Modify docs**: spec (one factual correction, Task 1), `SDD/05`, `SDD/08`, `SDD/09`, `SDD/11`, `SDD/13`, `SDD/14`, `SDD/15`, `SDD/README.md`, `AGENTS.md`, `README.md`, `tests/README.md`, `IC-7300_硬件验收清单.md`.

---

### 任务 1：Profile data module

**文件：**

- 创建：`backends/ic7300/civ_profiles.py`
- 创建：`tests/test_civ_profiles.py`
- 修改：`docs/superpowers/specs/2026-09-12-icom-sdr-models-design.md`（§5.2 / §9 的一处事实更正，见步骤 6）

- [x] **步骤 1：编写失败的测试**

`tests/test_civ_profiles.py`:

```python
"""Tests for the per-model CI-V profiles (spec 2026-09-12 §4.1/§5).

Every assertion here is hardware-independent: it checks that the
profile data matches the offline evidence (wfview rig data + the
verified IC-7300 tables), not that a radio behaves as documented.
"""
import unittest

from backends.ic7300 import config_ic7300, civ_profiles
from backends.ic7300.civ_codec import (
    SCOPE_AMPLITUDE_MAX, SCOPE_MAX_SEGMENTS, SCOPE_WAVEFORM_LEN,
)
from backends.ic7300.civ_controller import (
    SETMODE_CIV_TRANSCEIVE_MK2, SETMODE_CIV_TRANSCEIVE_ON,
)
from backends.ic7300.civ_profiles import PROFILES, get_profile, known_models

ICOM_KEYS = ("ic7300", "ic7300mk2", "ic705", "ic7610", "ic7760")
NEW_KEYS = ("ic705", "ic7610", "ic7760")


class ProfileRegistryTests(unittest.TestCase):
    def test_all_icom_models_registered(self):
        self.assertEqual(known_models(), ICOM_KEYS)
        self.assertEqual(tuple(PROFILES), ICOM_KEYS)

    def test_get_profile_normalizes_key(self):
        self.assertIs(get_profile("  IC-705 \n"), PROFILES["ic705"])

    def test_get_profile_unknown_raises(self):
        with self.assertRaises(ValueError):
            get_profile("ic9999")


class VerifiedProfileFidelityTests(unittest.TestCase):
    """The IC-7300/MK2 profiles must equal today's hardcoded tables."""

    def test_ic7300_address_and_transceive_command(self):
        p = PROFILES["ic7300"]
        self.assertEqual(p.civ_addr, config_ic7300.CIV_ADDR)
        self.assertEqual(p.civ_addr, 0x94)
        self.assertEqual(p.transceive_cmd, SETMODE_CIV_TRANSCEIVE_ON)

    def test_ic7300mk2_address_and_transceive_command(self):
        p = PROFILES["ic7300mk2"]
        self.assertEqual(p.civ_addr, config_ic7300.MK2_CIV_ADDR)
        self.assertEqual(p.civ_addr, 0xB6)
        self.assertEqual(p.transceive_cmd, SETMODE_CIV_TRANSCEIVE_MK2)

    def test_ic7300_tables_equal_legacy_tables(self):
        p = PROFILES["ic7300"]
        self.assertEqual(p.mode_num_to_name, config_ic7300.MODE_NUM_TO_NAME)
        self.assertEqual(p.mode_name_to_num, config_ic7300.MODE_NAME_TO_NUM)
        self.assertEqual(p.scope_spans, config_ic7300.SCOPE_SPANS)
        self.assertEqual(list(p.bands), config_ic7300.BANDS)
        self.assertEqual(p.fil_default_widths_hz,
                         config_ic7300.FIL_DEFAULT_WIDTHS_HZ)
        self.assertEqual(p.preamp_labels, config_ic7300.PREAMP_LABELS)
        self.assertEqual(p.att_steps, (0, 20))

    def test_ic7300_meter_conversions_match_legacy_functions(self):
        cal = PROFILES["ic7300"].meter_cal
        for raw in (0, 10, 60, 120, 200, 241, 255):
            self.assertAlmostEqual(cal.raw_to_dbm(raw),
                                   config_ic7300.raw_to_dbm(raw), places=9)
            self.assertEqual(cal.raw_to_s_unit(raw),
                             config_ic7300.raw_to_s_unit(raw))
            self.assertAlmostEqual(cal.raw_to_power(raw),
                                   config_ic7300.raw_to_power(raw), places=9)
            self.assertAlmostEqual(cal.raw_to_swr(raw),
                                   config_ic7300.raw_to_swr(raw), places=9)
            self.assertAlmostEqual(cal.raw_to_alc_pct(raw),
                                   config_ic7300.raw_to_alc_pct(raw), places=9)
            self.assertAlmostEqual(cal.raw_to_voltage(raw),
                                   config_ic7300.raw_to_voltage(raw), places=9)
            self.assertAlmostEqual(cal.raw_to_current(raw),
                                   config_ic7300.raw_to_current(raw), places=9)

    def test_ic7300_filter_hz_matches_legacy_table(self):
        # Compared against the table itself (not the backend helper), so
        # task 6 can delete the now-redundant _ic7300_filter_hz.
        p = PROFILES["ic7300"]
        for mode in ("USB", "CW-U", "AM", "FM"):
            for fil in (1, 2, 3):
                self.assertEqual(p.filter_hz(mode, fil),
                                 config_ic7300.FIL_DEFAULT_WIDTHS_HZ[mode][fil - 1])
        self.assertIsNone(p.filter_hz("USB", 0))
        self.assertIsNone(p.filter_hz("USB", 4))
        self.assertIsNone(p.filter_hz("NO-SUCH-MODE", 1))


class NewModelProfileTests(unittest.TestCase):
    def test_ic705(self):
        p = PROFILES["ic705"]
        self.assertEqual(p.civ_addr, 0xA4)
        self.assertEqual(p.transceive_item, b"\x01\x12")
        self.assertEqual((p.scope_bins, p.scope_amp_max, p.scope_seq_max),
                         (475, 160, 11))
        self.assertEqual(p.att_steps, (0, 20))
        self.assertEqual(p.meter_cal.rated_power_w, 10.0)
        self.assertEqual(p.mode_num_to_name[6], "WFM")
        self.assertEqual(p.mode_num_to_name[17], "DV")
        names = [b["name"] for b in p.bands]
        self.assertIn("2m", names)
        self.assertIn("70cm", names)
        self.assertEqual([b for b in p.bands if b["name"] == "70cm"][0]["start"],
                         430_000_000)

    def test_ic7610(self):
        p = PROFILES["ic7610"]
        self.assertEqual(p.civ_addr, 0x98)
        self.assertEqual(p.transceive_item, b"\x01\x12")
        self.assertEqual((p.scope_bins, p.scope_amp_max, p.scope_seq_max),
                         (689, 200, 15))
        self.assertEqual(len(p.att_steps), 16)
        self.assertEqual(p.att_steps[:3], (0, 3, 6))
        self.assertEqual(p.att_steps[-1], 45)
        self.assertEqual(p.meter_cal.rated_power_w, 100.0)
        self.assertTrue(p.dual_rx)
        self.assertTrue(p.mainsub_vfo)
        self.assertEqual(p.mode_num_to_name[12], "PSK-U")
        self.assertEqual(p.mode_num_to_name[13], "PSK-L")

    def test_ic7760(self):
        p = PROFILES["ic7760"]
        self.assertEqual(p.civ_addr, 0xB2)
        self.assertEqual(p.transceive_item, b"\x01\x50")
        self.assertEqual((p.scope_bins, p.scope_amp_max, p.scope_seq_max),
                         (689, 200, 15))
        self.assertEqual(p.meter_cal.rated_power_w, 200.0)

    def test_scope_queue_segments_is_four_waveforms(self):
        for key, p in PROFILES.items():
            with self.subTest(model=key):
                self.assertEqual(p.scope_queue_segments, 4 * p.scope_seq_max)

    def test_new_models_are_unverified_and_carry_no_model_id(self):
        for key in NEW_KEYS:
            with self.subTest(model=key):
                p = PROFILES[key]
                self.assertFalse(p.verified)
                self.assertEqual(p.unverified_meters,
                                 ("power", "voltage", "current"))
                self.assertIsNone(p.model_id_bytes)

    def test_verified_models_have_no_unverified_meters(self):
        for key in ("ic7300", "ic7300mk2"):
            with self.subTest(model=key):
                self.assertTrue(PROFILES[key].verified)
                self.assertEqual(PROFILES[key].unverified_meters, ())

    def test_scope_geometry_defaults_match_module_constants(self):
        p = PROFILES["ic7300"]
        self.assertEqual(p.scope_bins, SCOPE_WAVEFORM_LEN)
        self.assertEqual(p.scope_amp_max, SCOPE_AMPLITUDE_MAX)
        self.assertEqual(p.scope_seq_max, SCOPE_MAX_SEGMENTS)


class ProfileInvariantTests(unittest.TestCase):
    def test_mode_tables_are_bijective(self):
        for key, p in PROFILES.items():
            with self.subTest(model=key):
                self.assertEqual(len(p.mode_num_to_name), len(p.mode_name_to_num))
                for num, name in p.mode_num_to_name.items():
                    self.assertEqual(p.mode_name_to_num[name], num)

    def test_bands_are_ordered_and_non_overlapping(self):
        for key, p in PROFILES.items():
            with self.subTest(model=key):
                last_end = 0
                for band in p.bands:
                    self.assertLess(band["start"], band["end"])
                    self.assertGreaterEqual(band["start"], last_end)
                    self.assertTrue(band["start"] <= band["default_freq"] <= band["end"])
                    last_end = band["end"]

    def test_band_lookup_uses_profile_bands(self):
        p = PROFILES["ic705"]
        self.assertEqual(p.get_band_for_frequency(145_500_000)["name"], "2m")
        self.assertIsNone(p.get_band_for_frequency(100_000_000))

    def test_narrow_modes_intersect_profile_mode_names(self):
        self.assertEqual(PROFILES["ic7300"].narrow_modes(),
                         ["CW-L", "CW-U", "RTTY-L", "RTTY-U"])

    def test_attenuator_labels_from_steps(self):
        self.assertEqual(PROFILES["ic7300"].attenuator_labels(),
                         {0: "OFF", 1: "20dB"})
        labels = PROFILES["ic7760"].attenuator_labels()
        self.assertEqual(labels[0], "OFF")
        self.assertEqual(labels[15], "45dB")
        self.assertEqual(len(labels), 16)

    def test_transceive_command_layout(self):
        # 1a 05 <item> 01 — the item is a per-model set-mode number.
        self.assertEqual(PROFILES["ic7760"].transceive_cmd,
                         bytes((0x1A, 0x05, 0x01, 0x50, 0x01)))

    def test_profiles_are_frozen(self):
        p = PROFILES["ic705"]
        with self.assertRaises(Exception):
            p.civ_addr = 0x00


if __name__ == "__main__":
    unittest.main()
```

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_civ_profiles.py" 2>&1 | tail -5`
预期：FAIL，`ModuleNotFoundError: No module named 'backends.ic7300.civ_profiles'`

- [x] **步骤 3：编写最少实现代码**

`backends/ic7300/civ_profiles.py`:

```python
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


def known_models() -> tuple[str, ...]:
    """Icom model keys that have a profile, in registry order."""
    return tuple(PROFILES)


def get_profile(model_key: str) -> CivModelProfile:
    """Look up a profile by model key (case/whitespace tolerant)."""
    key = (model_key or "").strip().lower()
    profile = PROFILES.get(key)
    if profile is None:
        raise ValueError(
            f"unknown Icom model {model_key!r} (known: {', '.join(PROFILES)})")
    return profile
```

- [x] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_civ_profiles.py" 2>&1 | tail -5`
预期：`Ran 20 tests` … `OK`

- [x] **步骤 5：验证没有破坏现有测试**

运行：`.venv/bin/python -m unittest discover -s tests 2>&1 | tail -3`
预期：`OK`（与改动前的通过数一致；本任务只新增模块，不改运行时）

- [x] **步骤 6：修正规格中的一处事实错误**

设计规格 §5.2/§9 原计划给每条 band 记录加 `power_w`。实现时发现这会破坏已验证的回归断言
`IC7300Backend.bands == config_ic7300.BANDS`（`test_backend_factory.py:163`），而五台机型
**没有任何一个波段的额定功率与整机不同**（IC-705 全 10 W、IC-7610 全 100 W、IC-7760 全 200 W），
因此逐波段功率是冗余的。改为：功率唯一来源是 `MeterCal.rated_power_w`。编辑规格：

```
# §5.2 最后一段替换为：
Rated power is a single per-profile scalar (`MeterCal.rated_power_w`: 10 W / 100 W / 200 W).
Per-band `power_w` was dropped during implementation: the rig data shows no band whose
rating differs from the radio's, and adding it would have changed the shape of the
verified IC-7300 band table for no runtime benefit.

# §9 test_civ_profiles.py 行中删除 "band tables … carry power_w"，改为：
… band tables are ordered and non-overlapping and stay value-identical to the verified
IC-7300 table …
```

- [x] **步骤 7：Commit**

```bash
git add backends/ic7300/civ_profiles.py tests/test_civ_profiles.py docs/superpowers/specs/2026-09-12-icom-sdr-models-design.md
git commit -m "feat(icom): per-model CI-V profiles (IC-705/IC-7610/IC-7760 data)"
```

---

### 任务 2：ScopeAssembler profile hints

**文件：**

- 修改：`backends/ic7300/civ_codec.py:340-390`
- 测试：`tests/test_civ_codec.py`

- [x] **步骤 1：编写失败的测试**

追加到 `tests/test_civ_codec.py`（文件顶部已 import `unittest`；若缺 `logging` 断言所需的模块则一并加上）：

```python
class ScopeAssemblerProfileHintTests(unittest.TestCase):
    """Profile-supplied scope geometry: warn once, never drop data."""

    def _segments(self, seq_max: int, bin_total: int) -> list:
        per = 50
        segs = [ScopeSegment(sequence=1, sequence_max=seq_max, bins=b"",
                             is_division_start=True,
                             scope_mode=SCOPE_MODE_CENTER,
                             center_freq_hz=14_074_000, span_hz=100_000)]
        remaining = bin_total
        for seq in range(2, seq_max + 1):
            n = per if remaining > per else remaining
            n = max(n, 0)
            segs.append(ScopeSegment(sequence=seq, sequence_max=seq_max,
                                     bins=bytes([160] * n)))
            remaining -= n
        return segs

    def test_689_bin_waveform_across_15_segments(self):
        asm = ScopeAssembler(seq_max=15, expected_bins=689)
        bins = None
        for seg in self._segments(15, 689):
            bins = asm.feed(seg)
        self.assertIsNotNone(bins)
        self.assertEqual(len(bins), 689)

    def test_mismatched_bin_count_warns_once_and_is_adopted(self):
        asm = ScopeAssembler(seq_max=15, expected_bins=475)
        with self.assertLogs("ic7300.codec", level="WARNING") as cap:
            for seg in self._segments(15, 689):
                bins = asm.feed(seg)
            for seg in self._segments(15, 689):
                bins = asm.feed(seg)
        self.assertEqual(len(bins), 689)             # data still rendered
        self.assertEqual(len(cap.output), 1)         # warning only once
        self.assertIn("475", cap.output[0])
        self.assertIn("689", cap.output[0])

    def test_mismatched_segment_count_warns_once(self):
        asm = ScopeAssembler(seq_max=11, expected_bins=689)
        with self.assertLogs("ic7300.codec", level="WARNING") as cap:
            for seg in self._segments(15, 689):
                asm.feed(seg)
        self.assertEqual(len(cap.output), 1)
        self.assertIn("15", cap.output[0])

    def test_defaults_do_not_warn(self):
        asm = ScopeAssembler()                       # 11 segments / 475 bins
        for seg in self._segments(11, 475):
            bins = asm.feed(seg)
        self.assertEqual(len(bins), 475)

    def test_single_segment_lan_waveform_still_supported(self):
        asm = ScopeAssembler(seq_max=15, expected_bins=689)
        seg = ScopeSegment(sequence=1, sequence_max=1,
                           bins=bytes([100] * 689))
        self.assertEqual(len(asm.feed(seg)), 689)
```

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_civ_codec.py" 2>&1 | tail -6`
预期：FAIL，`TypeError: ScopeAssembler.__init__() got an unexpected keyword argument 'seq_max'`

- [x] **步骤 3：编写最少实现代码**

在 `backends/ic7300/civ_codec.py` 顶部 import 区加入 logger（文件当前没有 logging）：

```python
import logging
...
logger = logging.getLogger("ic7300.codec")
```

替换 `ScopeAssembler` 的 `__init__`/`_reset`/完成分支：

```python
class ScopeAssembler:
    """Reassemble a complete scope waveform from scope segments.

    Feed segments in arrival order; returns the full bin list when the
    final segment arrives, else None.  A sequence gap, duplicate, or
    mismatched sequence_max drops the whole in-progress waveform (reset).

    ``seq_max`` / ``expected_bins`` come from the active model profile
    (11/475 for the IC-7300 family, 15/689 for the IC-7610/IC-7760).
    They are *hints*: a disagreement is logged once as a WARNING and the
    observed geometry is adopted, so a wrong profile degrades to one log
    line instead of a frozen or dropped waterfall.
    """

    def __init__(self, seq_max: Optional[int] = None,
                 expected_bins: Optional[int] = None):
        self._seq_max_hint = seq_max
        self._expected_bins = expected_bins
        self._drift_warned = False
        self._reset()

    def _reset(self) -> None:
        self._seq_max: Optional[int] = None
        self._expected = 0
        self._bins = bytearray()

    def _warn_once(self, message: str, *args) -> None:
        if self._drift_warned:
            return
        self._drift_warned = True
        logger.warning(message, *args)

    def feed(self, segment: ScopeSegment) -> Optional[list[int]]:
        if segment.sequence == 1:
            # Division start: begin a fresh waveform.
            self._reset()
            if (self._seq_max_hint is not None
                    and segment.sequence_max != self._seq_max_hint):
                self._warn_once(
                    "scope segment count %d != profile expectation %d — the "
                    "profile scope_seq_max may be wrong; update it from a "
                    "_diag_civ.py report", segment.sequence_max,
                    self._seq_max_hint)
            if segment.sequence_max == 1:
                # LAN-style single segment carries the whole waveform.
                return list(segment.bins) if segment.bins else None
            self._seq_max = segment.sequence_max
            self._expected = 2
            self._bins.extend(segment.bins)  # normally empty
            return None
        if self._seq_max is None:
            return None  # mid-waveform segment without a start: drop
        if segment.sequence != self._expected or segment.sequence_max != self._seq_max:
            self._reset()  # gap / duplicate / discontinuity: drop waveform
            return None
        self._bins.extend(segment.bins)
        if segment.sequence == self._seq_max:
            complete = list(self._bins)
            if (self._expected_bins is not None
                    and len(complete) != self._expected_bins):
                self._warn_once(
                    "scope waveform length %d != profile expectation %d — the "
                    "profile scope_bins may be wrong; update it from a "
                    "_diag_civ.py report", len(complete), self._expected_bins)
            self._reset()
            return complete
        self._expected += 1
        return None
```

- [x] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_civ_codec.py" 2>&1 | tail -5`
预期：`OK`（既有用例 + 5 个新用例）

- [x] **步骤 5：Commit**

```bash
git add backends/ic7300/civ_codec.py tests/test_civ_codec.py
git commit -m "feat(icom): profile-supplied scope geometry in ScopeAssembler (warn-once drift)"
```

---

### 任务 3：CivScopeProducer amplitude ceiling

**文件：**

- 修改：`backends/ic7300/civ_scope.py:63-110`、`_handle_waveform`
- 测试：`tests/test_civ_scope.py`

- [x] **步骤 1：编写失败的测试**

追加到 `tests/test_civ_scope.py`：

```python
def make_waveform_segments_n(seg_max: int, bin_total: int, bin_value: int,
                             seq_max_override: int | None = None) -> list[ScopeSegment]:
    """One complete waveform with profile-supplied geometry."""
    segs = [ScopeSegment(
        sequence=1, sequence_max=seq_max_override or seg_max, bins=b"",
        is_division_start=True, scope_mode=SCOPE_MODE_CENTER,
        center_freq_hz=14_074_000, span_hz=100_000)]
    remaining = bin_total
    for seq in range(2, (seq_max_override or seg_max) + 1):
        n = 50 if remaining > 50 else max(remaining, 0)
        segs.append(ScopeSegment(sequence=seq,
                                 sequence_max=seq_max_override or seg_max,
                                 bins=bytes([bin_value] * n)))
        remaining -= n
    return segs


class CivScopeProducerProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.civ = CivController("/dev/null")
        self.scope = ScopeHandler()
        self.frames = []
        self._frame_event = asyncio.Event()

        async def on_frame(scope):
            self.frames.append(scope)
            self._frame_event.set()

        self.on_frame = on_frame

    async def _run(self, producer):
        await producer.start()
        for seg in producer._segments_to_feed:
            self.civ.scope_queue.put_nowait(seg)
        await asyncio.wait_for(self._frame_event.wait(), timeout=5)
        await producer.stop()

    async def test_200_amp_ceiling_reaches_full_scale(self):
        # 689 bins, amplitude ceiling 200 -> a 200 bin value must map to 255.
        producer = CivScopeProducer(self.civ, self.scope, self.on_frame,
                                    amp_max=200, expected_bins=689, seq_max=15)
        producer._segments_to_feed = make_waveform_segments_n(15, 689, 200)
        await self._run(producer)
        self.assertEqual(len(self.scope.spectrum_rx1), 850)
        self.assertEqual(max(self.scope.spectrum_rx1), 255)
        self.assertEqual(set(self.scope.spectrum_rx2), {0})

    async def test_160_amp_ceiling_still_used_by_default(self):
        producer = CivScopeProducer(self.civ, self.scope, self.on_frame)
        producer._segments_to_feed = make_waveform_segments(bin_value=160)
        await self._run(producer)
        self.assertEqual(max(self.scope.spectrum_rx1), 255)

    async def test_unexpected_bin_count_still_renders(self):
        # Profile says 475 bins, the radio sends 689: adopt the measurement.
        producer = CivScopeProducer(self.civ, self.scope, self.on_frame,
                                    amp_max=200, expected_bins=475, seq_max=15)
        producer._segments_to_feed = make_waveform_segments_n(15, 689, 200)
        await self._run(producer)
        self.assertEqual(len(self.scope.spectrum_rx1), 850)
        self.assertEqual(self.scope._connected, True)
```

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_civ_scope.py" 2>&1 | tail -6`
预期：FAIL，`TypeError: CivScopeProducer.__init__() got an unexpected keyword argument 'amp_max'`

- [x] **步骤 3：编写最少实现代码**

`backends/ic7300/civ_scope.py` 的 `__init__` 与组装：

```python
    def __init__(
        self,
        controller: "CivController",
        scope: Optional["ScopeHandler"] = None,
        on_frame: Optional[OnFrameCallback] = None,
        amp_max: int = SCOPE_AMPLITUDE_MAX,
        expected_bins: Optional[int] = None,
        seq_max: Optional[int] = None,
    ):
        ...
        self._amp_max = amp_max
        self._assembler = ScopeAssembler(seq_max=seq_max,
                                        expected_bins=expected_bins)
```

`_handle_waveform` 中的缩放改为：

```python
        scope.spectrum_rx1 = upsample_bins(
            scale_scope_bins(bins, self._amp_max, 255), WF_SIZE)
```

（其余不变；`SCOPE_AMPLITUDE_MAX` 的 import 保留为默认值来源。）

- [x] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_civ_scope.py" 2>&1 | tail -5`
预期：`OK`

- [x] **步骤 5：Commit**

```bash
git add backends/ic7300/civ_scope.py tests/test_civ_scope.py
git commit -m "feat(icom): configurable scope amplitude ceiling in CivScopeProducer"
```

---

### 任务 4：CivController profile plumbing + model ID query

**文件：**

- 修改：`backends/ic7300/civ_controller.py:174-215`（`__init__`）、`:804-808`（`set_attenuator`）、`:1058-1063`（`_get_attenuator`）、新增 `get_model_id()`
- 测试：`tests/test_civ_controller.py`

- [x] **步骤 1：编写失败的测试**

追加到 `tests/test_civ_controller.py`：

```python
class CivControllerProfileTests(unittest.IsolatedAsyncioTestCase):
    def test_default_construction_matches_legacy_behaviour(self):
        civ = CivController("/dev/null")
        self.assertEqual(civ.civ_addr, 0x94)
        self.assertEqual(civ._att_steps, (0, 20))
        self.assertEqual(civ.scope_queue.maxsize, 44)   # 4 x 11 segments

    def test_profile_supplies_address_transceive_and_queue(self):
        from backends.ic7300.civ_profiles import PROFILES
        profile = PROFILES["ic7760"]
        civ = CivController("/dev/null", 115200, civ_addr=profile.civ_addr,
                            transceive_cmd=profile.transceive_cmd,
                            profile=profile, att_steps=profile.att_steps)
        self.assertEqual(civ.civ_addr, 0xB2)
        self.assertEqual(civ.scope_queue.maxsize, 60)   # 4 x 15 segments
        self.assertEqual(civ._att_steps[-1], 45)
        self.assertIn("IC-7760", civ._model)

    def test_explicit_kwargs_beat_profile(self):
        from backends.ic7300.civ_profiles import PROFILES
        civ = CivController("/dev/null", 115200, civ_addr=0x99,
                            profile=PROFILES["ic7300"])
        self.assertEqual(civ.civ_addr, 0x99)

    async def test_set_attenuator_writes_db_from_profile_steps(self):
        from backends.ic7300.civ_profiles import PROFILES
        civ = CivController("/dev/null", 115200,
                            att_steps=PROFILES["ic7760"].att_steps)
        sent = []

        async def fake_send(cmd):
            sent.append(cmd)
            return True

        civ.send_set_command = fake_send
        self.assertTrue(await civ.set_attenuator(0))
        self.assertTrue(await civ.set_attenuator(5))
        self.assertFalse(await civ.set_attenuator(16))
        # Wire bytes are packed BCD: 0 dB -> 0x00, 15 dB -> 0x15.
        self.assertEqual(sent, [bytes((0x11, 0x00)), bytes((0x11, 0x15))])

    async def test_get_attenuator_returns_index_for_3db_steps(self):
        from backends.ic7300.civ_profiles import PROFILES
        civ = CivController("/dev/null", 115200,
                            att_steps=PROFILES["ic7760"].att_steps)

        async def fake_query(command, sub=None, timeout=None):
            return bytes((0x15,))          # BCD 15 dB

        civ._query_data = fake_query
        self.assertEqual(await civ._get_attenuator(), 5)   # 15 dB -> index 5

    async def test_get_model_id_reads_19_00(self):
        civ = CivController("/dev/null")
        seen = []

        async def fake_query(command, sub=None, timeout=None):
            seen.append((command, sub))
            return bytes((0xB2,))

        civ._query_data = fake_query
        self.assertEqual(await civ.get_model_id(), bytes((0xB2,)))
        self.assertEqual(seen, [(0x19, 0x00)])

    async def test_get_model_id_none_when_radio_does_not_answer(self):
        civ = CivController("/dev/null")

        async def fake_query(command, sub=None, timeout=None):
            return None

        civ._query_data = fake_query
        self.assertIsNone(await civ.get_model_id())
```

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_civ_controller.py" 2>&1 | tail -6`
预期：FAIL，`AttributeError: 'CivController' object has no attribute '_att_steps'`

- [x] **步骤 3：编写最少实现代码**

`__init__` 签名与赋值（新增两个关键字参数，默认值 = 现状）：

```python
    def __init__(
        self,
        port: str,
        baudrate: int = CIV_BAUD_RATE,
        civ_addr: int = CIV_ADDR,
        query_timeout: float = _DEFAULT_QUERY_TIMEOUT,
        transceive_cmd: bytes = SETMODE_CIV_TRANSCEIVE_ON,
        profile: "CivModelProfile | None" = None,
        att_steps: tuple = (0, 20),
    ):
```

在 `self._transceive_cmd = transceive_cmd` 之后插入：

```python
        # Model profile (backends.ic7300.civ_profiles).  Explicit keyword
        # arguments always win over profile values, so existing callers
        # and tests keep their behaviour unchanged.
        self._model = profile.display_name if profile is not None else "IC-7300"
        self._att_steps = tuple(att_steps) if att_steps else (0, 20)
        queue_segments = (profile.scope_queue_segments if profile is not None
                          else SCOPE_QUEUE_MAX_SEGMENTS)
```

并把队列构造改为：

```python
        self.scope_queue: asyncio.Queue = asyncio.Queue(maxsize=queue_segments)
```

删除原先硬编码的 `self._model = "IC-7300"` 行（已被上面替换）。

`set_attenuator` / `_get_attenuator`（**注意：线值是 packed BCD**——20 dB 发 `0x20`，15 dB 发 `0x15`；依据 `IC-7300MK2_CI-V_Knowledge_Base.md` "00=OFF, 20=ON（⚠️ ON 是 0x20，不是 01）" 与 wfview `icomcommander.cpp` 的 `bcdEncodeChar`/`bcdHexToUChar`）：

```python
    async def set_attenuator(self, value: int) -> bool:
        """Set the attenuator by UI index (profile att_steps).

        Wire data is the attenuation in dB: 0/20 on the IC-7300/IC-705,
        0..45 in 3 dB steps on the IC-7610/IC-7760.
        """
        if not 0 <= value < len(self._att_steps):
            return False
        return await self.send_set_command(
            bytes((CMD_ATT, self._att_steps[value])))
```

```python
    async def _get_attenuator(self) -> Optional[int]:
        """ATT query -> UI index (position in the profile's att_steps)."""
        data = await self._query_data(CMD_ATT)
        if data is None or not data:
            return None
        try:
            return self._att_steps.index(data[0])
        except ValueError:
            return 0
```

新增 model-ID 查询（放在 `boot_verify` 或 `get_radio_info` 附近）：

```python
    async def get_model_id(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """Read the transceiver ID (19 00) — the fixed model identity.

        Distinct from the user-configurable CI-V address.  Returns the raw
        ID bytes, or None when the radio does not answer (older radios and
        LAN-unlinked ports do not implement 19 00).
        """
        return await self._query_data(0x19, 0x00, timeout=timeout)
```

若 `_query_data` 返回空 `b""`（无数据帧）也应视为无应答：

```python
        data = await self._query_data(0x19, 0x00, timeout=timeout)
        return data if data else None
```

- [x] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_civ_controller.py" 2>&1 | tail -5`
预期：`OK`

- [x] **步骤 5：运行全部 CI-V 相关测试**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_civ_*.py" 2>&1 | tail -3`
预期：`OK`

- [x] **步骤 6：Commit**

```bash
git add backends/ic7300/civ_controller.py tests/test_civ_controller.py
git commit -m "feat(icom): profile plumbing (address/queue/attenuator steps) + 19 00 model ID query"
```

---

### 任务 5：Capability and state fields

**文件：**

- 修改：`backends/base.py`（`RadioCapabilities` 数据类 + `to_dict()`）
- 修改：`radio_state.py`（新增 `model_mismatch`）
- 测试：`tests/test_backend_factory.py`

- [x] **步骤 1：编写失败的测试**

追加到 `tests/test_backend_factory.py`：

```python
class CapabilityVerificationFieldsTests(unittest.TestCase):
    def test_defaults_are_conservative(self):
        caps = RadioCapabilities(
            model_name="x", display_name="X", default_baud=38400,
            audio_rx_rate=44100, audio_tx_rate=44100)
        self.assertTrue(caps.verified)
        self.assertFalse(caps.tx_gated)
        self.assertFalse(caps.dual_rx)
        self.assertEqual(caps.unverified_meters, ())
        self.assertEqual(caps.audio_gain_boost, 1.0)

    def test_to_dict_serializes_new_fields(self):
        caps = RadioCapabilities(
            model_name="x", display_name="X", default_baud=38400,
            audio_rx_rate=48000, audio_tx_rate=48000,
            verified=False, tx_gated=True,
            unverified_meters=("power", "voltage"), audio_gain_boost=1.0)
        data = caps.to_dict()
        self.assertFalse(data["verified"])
        self.assertTrue(data["tx_gated"])
        self.assertEqual(data["unverified_meters"], ["power", "voltage"])
        self.assertEqual(data["audio_gain_boost"], 1.0)


class RadioStateModelMismatchTests(unittest.TestCase):
    def test_default_is_false_and_field_is_tracked(self):
        from radio_state import RadioState
        state = RadioState()
        self.assertFalse(state.model_mismatch)
        state.update(model_mismatch=True)
        self.assertIn("model_mismatch", state._dirty_fields)
        self.assertTrue(state.snapshot()["model_mismatch"])
```

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_backend_factory.py" 2>&1 | tail -6`
预期：FAIL，`TypeError: RadioCapabilities.__init__() got an unexpected keyword argument 'verified'`

- [x] **步骤 3：编写最少实现代码**

`backends/base.py` 的 `RadioCapabilities` 追加字段（放在 `tune_via` 之后）：

```python
    # ── Verification boundary (spec 2026-09-12 §6) ─────────────────
    verified: bool = True                 # False = no hardware evidence yet
    tx_gated: bool = False                # True = PTT/TUNE refused by the backend
    dual_rx: bool = False                 # radio has a second receiver
    unverified_meters: tuple = ()         # meter names not hardware-verified
    audio_gain_boost: float = 1.0         # browser RX playback gain multiplier
```

`to_dict()` 增加一行的元组转换：

```python
        data["unverified_meters"] = list(self.unverified_meters)
```

`radio_state.py`：在 `serial_connected` 之后加入字段（并在 `snapshot()` 的 Connection 段写入）：

```python
    serial_connected: bool = False
    model_mismatch: bool = False         # 19 00 identity disagrees with the profile
```

```python
            "serial_connected": self.serial_connected,
            "model_mismatch": self.model_mismatch,
```

- [x] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_backend_factory.py" 2>&1 | tail -5`
预期：`OK`

- [x] **步骤 5：Commit**

```bash
git add backends/base.py radio_state.py tests/test_backend_factory.py
git commit -m "feat(backend): verification-boundary capability fields + model_mismatch state"
```

---

### 任务 6：Profile-driven IC7300 backend, TX gate, identity check, new models

**文件：**

- 修改：`backends/ic7300/backend.py`
- 修改：`backends/__init__.py`
- 测试：`tests/test_unverified_tx_gate.py`（新建）、`tests/test_model_mismatch.py`（新建）、`tests/test_backend_factory.py`（扩展）

- [x] **步骤 1：编写失败的测试**

`tests/test_unverified_tx_gate.py`:

```python
"""Unverified-model transmit gate (spec 2026-09-12 §6.1).

The gate must keep an unverified radio from transmitting, must never
block a PTT release, and must not touch the serial port when it refuses.
"""
import unittest
from unittest import mock

import config
from backends import create_backend
from backends.ic7300.civ_profiles import PROFILES


class UnverifiedTxGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The gate warns once per process — reset it so the log-count
        # assertion cannot depend on test execution order.
        import backends.ic7300.backend as icom_backend
        icom_backend._TX_GATE_WARNED = False

    async def test_ic705_is_gated_by_default(self):
        backend = create_backend("ic705", port="/dev/null")
        self.assertTrue(backend.capabilities.tx_gated)
        self.assertFalse(backend.capabilities.verified)

    @mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False, create=True)
    async def test_refused_ptt_does_not_write_to_serial(self):
        backend = create_backend("ic705", port="/dev/null")
        backend._civ.send_set_command = mock.AsyncMock(return_value=True)
        self.assertFalse(await backend.set_ptt(True))
        backend._civ.send_set_command.assert_not_called()

    @mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False, create=True)
    async def test_refused_tune_does_not_write_to_serial(self):
        backend = create_backend("ic7760", port="/dev/null")
        backend._civ.send_set_command = mock.AsyncMock(return_value=True)
        self.assertFalse(await backend.set_tune(True))
        backend._civ.send_set_command.assert_not_called()

    @mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False, create=True)
    async def test_release_is_never_blocked(self):
        backend = create_backend("ic705", port="/dev/null")
        backend._civ.set_ptt = mock.AsyncMock(return_value=True)
        self.assertTrue(await backend.set_ptt(False))
        backend._civ.set_ptt.assert_awaited_once_with(False)
        backend._civ.set_tune = mock.AsyncMock(return_value=True)
        self.assertTrue(await backend.set_tune(False))
        backend._civ.set_tune.assert_awaited_once_with(False)

    @mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True, create=True)
    async def test_env_var_allows_transmit(self):
        backend = create_backend("ic705", port="/dev/null")
        self.assertFalse(backend.capabilities.tx_gated)
        backend._civ.set_ptt = mock.AsyncMock(return_value=True)
        self.assertTrue(await backend.set_ptt(True))

    @mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False, create=True)
    async def test_verified_models_are_unaffected(self):
        for model in ("ic7300", "ic7300mk2"):
            backend = create_backend(model, port="/dev/null")
            self.assertFalse(backend.capabilities.tx_gated)
            backend._civ.set_ptt = mock.AsyncMock(return_value=True)
            self.assertTrue(await backend.set_ptt(True))

    @mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False, create=True)
    async def test_gate_logs_once(self):
        backend = create_backend("ic7610", port="/dev/null")
        backend._civ.send_set_command = mock.AsyncMock(return_value=True)
        with self.assertLogs("ic7300.backend", level="WARNING") as cap:
            await backend.set_ptt(True)
            await backend.set_ptt(True)
        self.assertEqual(len(cap.output), 1)


class NewModelCapabilityTests(unittest.TestCase):
    def test_capabilities_are_profile_derived(self):
        ic705 = create_backend("ic705", port="/dev/null").capabilities
        self.assertEqual(ic705.model_name, "ic705")
        self.assertEqual(ic705.display_name, "Icom IC-705")
        self.assertEqual(ic705.att_steps, (0, 20))
        self.assertEqual(ic705.audio_gain_boost, 1.0)
        self.assertEqual(ic705.unverified_meters,
                         ("power", "voltage", "current"))
        self.assertEqual(ic705.scope_type, "civ27")

        ic7760 = create_backend("ic7760", port="/dev/null").capabilities
        self.assertEqual(len(ic7760.att_steps), 16)
        self.assertTrue(ic7760.dual_rx)
```

`tests/test_model_mismatch.py`:

```python
"""19 00 model-identity handling (spec 2026-09-12 §6.2).

No profile records an expected model ID yet, so the runtime must log the
observed bytes and never raise a mismatch verdict from a guess.
"""
import unittest
from unittest import mock

import dataclasses

from backends.ic7300.backend import IC7300Backend, IC705Backend


class ModelIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_expectation_logs_observed_bytes_only(self):
        backend = IC705Backend(port="/dev/null")
        backend._civ.get_model_id = mock.AsyncMock(return_value=bytes((0xA4,)))
        with self.assertLogs("ic7300.backend", level="INFO") as cap:
            verdict = await backend._check_model_identity()
        self.assertIsNone(verdict)
        self.assertTrue(any("A4" in line.upper() for line in cap.output))

    async def test_silent_radio_is_skipped(self):
        backend = IC705Backend(port="/dev/null")
        backend._civ.get_model_id = mock.AsyncMock(return_value=None)
        self.assertIsNone(await backend._check_model_identity())

    async def test_expected_bytes_match_returns_false(self):
        backend = IC705Backend(port="/dev/null")
        backend._civ.get_model_id = mock.AsyncMock(return_value=bytes((0xA4,)))
        # The profile is a frozen dataclass and lives on the class
        # attribute, so inject an expectation by patching the class.
        patched = dataclasses.replace(IC705Backend._profile,
                                      model_id_bytes=(0xA4,))
        with mock.patch.object(IC705Backend, "_profile", patched):
            self.assertIs(await backend._check_model_identity(), False)

    async def test_expected_bytes_mismatch_returns_true_and_warns(self):
        backend = IC7300Backend(port="/dev/null")
        backend._civ.get_model_id = mock.AsyncMock(return_value=bytes((0xB2,)))
        patched = dataclasses.replace(IC7300Backend._profile,
                                      model_id_bytes=(0x94,))
        with mock.patch.object(IC7300Backend, "_profile", patched):
            with self.assertLogs("ic7300.backend", level="WARNING") as cap:
                verdict = await backend._check_model_identity()
        self.assertIs(verdict, True)
        self.assertIn("mismatch", cap.output[0].lower())

    async def test_sync_result_carries_model_mismatch(self):
        backend = IC705Backend(port="/dev/null")
        backend._civ.initial_state_sync = mock.AsyncMock(return_value={"mode": 1})
        backend._check_model_identity = mock.AsyncMock(return_value=True)
        data = await backend.initial_state_sync()
        self.assertIs(data["model_mismatch"], True)
```

追加到 `tests/test_backend_factory.py` 的注册测试：

```python
    def test_new_icom_models_construct(self):
        from backends.ic7300.backend import (
            IC705Backend, IC7610Backend, IC7760Backend)
        self.assertIsInstance(create_backend("ic705", port="/dev/null"),
                              IC705Backend)
        self.assertIsInstance(create_backend("ic7610", port="/dev/null"),
                              IC7610Backend)
        self.assertIsInstance(create_backend("ic7760", port="/dev/null"),
                              IC7760Backend)

    def test_known_models_covers_every_backend(self):
        from backends import known_models
        self.assertEqual(known_models(),
                         ("ft710", "ic7300", "ic7300mk2", "ic705",
                          "ic7610", "ic7760"))
```

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_unverified_tx_gate.py" 2>&1 | tail -5`
预期：FAIL，`ValueError: unknown radio model 'ic705' (registered: ft710, ic7300, ic7300mk2)`

- [x] **步骤 3：编写最少实现代码**

`backends/__init__.py`：

```python
_BACKENDS = {
    "ft710": ("backends.ft710.backend", "FT710Backend"),
    "ic7300": ("backends.ic7300.backend", "IC7300Backend"),
    "ic7300mk2": ("backends.ic7300.backend", "IC7300MK2Backend"),
    "ic705": ("backends.ic7300.backend", "IC705Backend"),
    "ic7610": ("backends.ic7300.backend", "IC7610Backend"),
    "ic7760": ("backends.ic7300.backend", "IC7760Backend"),
}


def known_models() -> tuple[str, ...]:
    """Registered model keys — the server's validation source of truth."""
    return tuple(_BACKENDS)
```

`backends/ic7300/backend.py`：

1. imports 改为 profile 来源（保留 `_ic7300_filter_hz` 供既有测试用）：

```python
import config
from backends.ic7300.civ_profiles import PROFILES, CivModelProfile
```

1. 类头与构造：

```python
class IC7300Backend(RadioBackend):
    """RadioBackend for Icom CI-V radios (profile-driven).

    Every model difference (address, Transceive item, scope geometry,
    bands, modes, attenuator steps, meter curves, verification status)
    comes from ``_profile``; subclasses only override that attribute.
    """

    _profile: CivModelProfile = PROFILES["ic7300"]
    _display_name = "Icom IC-7300"

    def __init__(self, port: str, baud_rate: int = 115200):
        p = self._profile
        self._civ = CivController(
            port,
            baud_rate,
            civ_addr=p.civ_addr,
            transceive_cmd=p.transceive_cmd,
            profile=p,
            att_steps=p.att_steps,
        )
```

1. capabilities 从 profile 派生：

```python
    @property
    def capabilities(self) -> RadioCapabilities:
        p = self._profile
        return RadioCapabilities(
            model_name=p.model_key,
            display_name=self._display_name,
            default_baud=115200,
            audio_rx_rate=p.audio_rx_rate,
            audio_tx_rate=p.audio_tx_rate,
            audio_name_hints=p.audio_name_hints,
            has_atu=p.has_atu,
            has_auto_notch=False,
            has_vd_id_meters=False,
            vfo_b_direct=False,
            filter_model=p.filter_model,
            att_steps=p.att_steps,
            preamp_steps=tuple(p.preamp_labels.values()),
            scope_type="civ27",
            scope_spans=p.scope_spans,
            scope_speeds=("FAST", "MID", "SLOW"),
            tune_via=p.tune_via,
            verified=p.verified,
            tx_gated=not self._tx_allowed(),
            dual_rx=p.dual_rx,
            unverified_meters=p.unverified_meters,
            audio_gain_boost=p.audio_gain_boost,
        )
```

1. TX gate:

```python
    # ── Unverified-model transmit gate (spec §6.1) ─────────────────

    def _tx_allowed(self) -> bool:
        """A verified profile, or an explicit operator opt-in."""
        return self._profile.verified or bool(config.ALLOW_UNVERIFIED_TX)

    async def set_ptt(self, tx: bool) -> bool:
        # A release is never gated: refusing TX0 would strand the carrier
        # (Chapter 15 layering) — only keying is refused.
        if tx and not self._tx_allowed():
            _log_tx_gate_once(self._profile)
            return False
        return await self._civ.set_ptt(tx)

    async def set_tune(self, tune: bool) -> bool:
        if tune and not self._tx_allowed():
            _log_tx_gate_once(self._profile)
            return False
        return await self._civ.set_tune(tune)
```

模块级（放在 `_ic7300_filter_hz` 附近）：

```python
# One warning per process is enough: the gate is a configuration state,
# not a per-keystroke event (a log flood would bury real failures).
_TX_GATE_WARNED = False


def _log_tx_gate_once(profile: CivModelProfile) -> None:
    global _TX_GATE_WARNED
    if _TX_GATE_WARNED:
        return
    _TX_GATE_WARNED = True
    logger.warning(
        "%s is not hardware-verified — transmit refused. Set "
        "MRRC_ALLOW_UNVERIFIED_TX=1 and restart to enable TX after "
        "checking the radio with _diag_civ.py.", profile.display_name)
```

1. identity check + sync override:

```python
    async def _check_model_identity(self) -> Optional[bool]:
        """Compare the radio's 19 00 ID with the profile expectation.

        True = confirmed mismatch, False = confirmed match, None = no
        answer or no recorded expectation (every profile today, so a
        guess can never raise a false alarm).
        """
        p = self._profile
        try:
            observed = await self._civ.get_model_id(timeout=0.5)
        except Exception:
            return None
        if observed is None:
            return None
        if p.model_id_bytes is None:
            logger.info(
                "Radio reports model ID %s; profile %s records no expectation "
                "(fill it from a _diag_civ.py report to enable the check)",
                observed.hex(" ").upper(), p.model_key)
            return None
        if tuple(observed) == tuple(p.model_id_bytes):
            return False
        logger.warning(
            "Model mismatch: radio reports model ID %s but profile %s expects "
            "%s — check MRRC_RADIO_MODEL / the connection dialog",
            observed.hex(" ").upper(), p.model_key,
            bytes(p.model_id_bytes).hex(" ").upper())
        return True

    async def initial_state_sync(self) -> dict:
        data = await self._civ.initial_state_sync()
        mismatch = await self._check_model_identity()
        if mismatch is not None:
            data["model_mismatch"] = mismatch
        return data
```

1. 表属性改为 profile 来源（`bands`/`mode_name_to_num`/`filter_tables`/`state_tables`）与 scope producer：

```python
    @property
    def bands(self) -> list:
        return list(self._profile.bands)

    @property
    def mode_name_to_num(self) -> dict:
        return self._profile.mode_name_to_num

    def filter_tables(self) -> dict:
        p = self._profile
        return {
            "voice": [(i + 1, w) for i, w in enumerate(p.fil_default_widths_hz["USB"])],
            "narrow": [(i + 1, w) for i, w in enumerate(p.fil_default_widths_hz["CW-U"])],
            "narrowModes": p.narrow_modes(),
            "model": p.filter_model,
            "filDefaults": p.fil_default_widths_hz,
        }

    def state_tables(self) -> dict:
        p = self._profile
        cal = p.meter_cal
        return {
            "mode_num_to_name": p.mode_num_to_name,
            "preamp_labels": p.preamp_labels,
            "attenuator_labels": p.attenuator_labels(),
            "get_band_for_frequency": p.get_band_for_frequency,
            "get_filter_hz": p.filter_hz,
            "raw_to_dbm": cal.raw_to_dbm,
            "raw_to_s_unit": cal.raw_to_s_unit,
            "raw_to_power": cal.raw_to_power,
            "raw_to_swr": cal.raw_to_swr,
            "raw_to_alc_pct": cal.raw_to_alc_pct,
            "raw_to_voltage": cal.raw_to_voltage,
            "raw_to_current": cal.raw_to_current,
        }

    def create_scope_producer(self, scope_handler=None, on_frame=None):
        p = self._profile
        return CivScopeProducer(
            self._civ, scope_handler, on_frame,
            amp_max=p.scope_amp_max,
            expected_bins=p.scope_bins,
            seq_max=p.scope_seq_max,
        )
```

1. 新机型类（文件末尾替换 MK2 的 `__init__`，并追加三个类）：

```python
class IC7300MK2Backend(IC7300Backend):
    """IC-7300MK2 — same CI-V surface, 0xB6 and Transceive item 0089."""

    _profile = PROFILES["ic7300mk2"]
    _display_name = "Icom IC-7300MK2"


class IC705Backend(IC7300Backend):
    """IC-705 — 10 W portable, 475-bin scope, HF + 2m/70cm (unverified)."""

    _profile = PROFILES["ic705"]
    _display_name = "Icom IC-705"


class IC7610Backend(IC7300Backend):
    """IC-7610 — dual receiver, 689-bin scope, 3 dB attenuator steps.

    MAIN receiver only: ``dual_rx`` is advertised but the cmd 29 prefix
    is deliberately not sent (spec §2 D4).
    """

    _profile = PROFILES["ic7610"]
    _display_name = "Icom IC-7610"


class IC7760Backend(IC7300Backend):
    """IC-7760 — 200 W dual receiver, 689-bin scope (unverified)."""

    _profile = PROFILES["ic7760"]
    _display_name = "Icom IC-7760"
```

清理被 profile 取代的死代码：`grep -rn "_ATTENUATOR_INDEX_LABELS\|_ic7300_filter_hz" --include=*.py .` 必须零命中（测试也不引用它们——任务 1 的测试已改为直接对照 `FIL_DEFAULT_WIDTHS_HZ`）。

- [x] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_unverified_tx_gate.py" 2>&1 | tail -5`
运行：`.venv/bin/python -m unittest discover -s tests -p "test_model_mismatch.py" 2>&1 | tail -5`
运行：`.venv/bin/python -m unittest discover -s tests -p "test_backend_factory.py" 2>&1 | tail -5`
预期：三者均 `OK`

- [x] **步骤 5：回归全量测试**

运行：`.venv/bin/python -m unittest discover -s tests 2>&1 | tail -3`
预期：`OK`，通过数 = 任务 5 后的通过数 + 新增用例数（不得出现新的 FAIL/ERROR）

- [x] **步骤 6：Commit**

```bash
git add backends/ic7300/backend.py backends/__init__.py tests/test_unverified_tx_gate.py tests/test_model_mismatch.py tests/test_backend_factory.py
git commit -m "feat(icom): profile-driven Icom backend, TX gate, model identity check, IC-705/7610/7760"
```

---

### 任务 7：Configuration wiring

**文件：**

- 修改：`config.py:44-66`
- 测试：`tests/test_config.py`

- [x] **步骤 1：编写失败的测试**

追加到 `tests/test_config.py`：

```python
class IcomModelBaudTests(unittest.TestCase):
    def test_new_models_default_to_115200(self):
        for model in ("ic705", "ic7610", "ic7760"):
            with self.subTest(model=model):
                self.assertEqual(config.default_baud_for(model), 115200)

    def test_ft710_stays_at_38400_and_unknown_falls_back(self):
        self.assertEqual(config.default_baud_for("ft710"), 38400)
        self.assertEqual(config.default_baud_for("no-such-radio"), 38400)

    def test_model_keys_match_backend_registry(self):
        from backends import known_models
        for model in known_models():
            self.assertIn(model, config._DEFAULT_BAUD_BY_MODEL)


class UnverifiedTxGateConfigTests(unittest.TestCase):
    def test_env_bool_parsing(self):
        for raw, expected in (("1", True), ("true", True), ("YES", True),
                              ("on", True), ("0", False), ("no", False),
                              ("", False), ("banana", False)):
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ,
                                     {"MRRC_ALLOW_UNVERIFIED_TX": raw}):
                    import importlib
                    importlib.reload(config)
                    self.assertIs(config.ALLOW_UNVERIFIED_TX, expected)
        import importlib
        importlib.reload(config)

    def test_legacy_ft710_alias_is_honored(self):
        with mock.patch.dict(os.environ, {"FT710_ALLOW_UNVERIFIED_TX": "1"}):
            import importlib
            importlib.reload(config)
            self.assertTrue(config.ALLOW_UNVERIFIED_TX)
        import importlib
        importlib.reload(config)
```

（`tests/test_config.py` 顶部若无 `import os, mock` 则补上。）

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_config.py" 2>&1 | tail -6`
预期：FAIL，`AttributeError: module 'config' has no attribute 'ALLOW_UNVERIFIED_TX'`

- [x] **步骤 3：编写最少实现代码**

`config.py`：在 `_env_float` 之后加 helper：

```python
def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean env var: 1/true/yes/on are true, everything else false."""
    val = _env(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")
```

`_DEFAULT_BAUD_BY_MODEL` 补三行：

```python
_DEFAULT_BAUD_BY_MODEL = {
    "ft710": 38400,
    "ic7300": 115200,
    "ic7300mk2": 115200,
    "ic705": 115200,
    "ic7610": 115200,
    "ic7760": 115200,
}
```

在 Radio Model Selection 段之后新增：

```python
# ── Unverified-Model Transmit Gate ──────────────────────────────────
# Radios whose profile has no hardware evidence refuse PTT/TUNE until the
# operator opts in (spec 2026-09-12 §6.1).  Read at import time, like the
# other safety switches (MRRC_PTT_MAX_TX_SECONDS), so the state is visible
# in the startup log.
ALLOW_UNVERIFIED_TX = _env_bool("MRRC_ALLOW_UNVERIFIED_TX", False)
```

- [x] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_config.py" 2>&1 | tail -5`
预期：`OK`

- [x] **步骤 5：Commit**

```bash
git add config.py tests/test_config.py
git commit -m "feat(config): ICOM model baud defaults + MRRC_ALLOW_UNVERIFIED_TX gate"
```

---

### 任务 8：Server wiring (registry validation, attenuator bound, gate message, startup warning)

**文件：**

- 修改：`server.py:42-48`（import）、`:1242-1247`（att 边界）、`:1113-1125`（ptt 门控）、`:1180-1200`（tune 门控）、`:1623-1631`（启动告警）、`:1922-1929`（模型白名单）
- 测试：`tests/test_server_ws_protocol.py`

- [x] **步骤 1：编写失败的测试**

追加到 `tests/test_server_ws_protocol.py`：

```python
class ServerModelRegistryTests(unittest.TestCase):
    """The model whitelist must come from the backend registry, not a tuple."""

    def test_setup_accepts_every_registered_model(self):
        import inspect
        from backends import known_models
        source = inspect.getsource(server.api_setup_save)
        self.assertIn("known_models()", source)
        self.assertNotIn('("ft710", "ic7300", "ic7300mk2")', source)
        self.assertEqual(len(known_models()), 6)

    def test_attenuator_bound_comes_from_capabilities(self):
        import inspect
        source = inspect.getsource(server)
        self.assertNotIn("if v in (0, 1, 2, 3):", source)
        self.assertIn("len(_att_steps)", source)

    def test_tx_gate_message_present(self):
        import inspect
        source = inspect.getsource(server)
        self.assertIn("MRRC_ALLOW_UNVERIFIED_TX", source)
```

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_server_ws_protocol.py" 2>&1 | tail -6`
预期：FAIL，`AssertionError: 'known_models()' not found`

- [x] **步骤 3：编写最少实现代码**

import：`from backends import create_backend, known_models`

attenuator 边界（把硬编码的 `(0,1,2,3)` 换成 capability 驱动）：

```python
        elif field == "att" or field == "attenuator":
            v = int(value)
            _att_steps = (backend.capabilities.att_steps
                          if backend is not None else (0, 6, 12, 18))
            if 0 <= v < len(_att_steps):
                await cat.set_attenuator(v)
                radio.update(attenuator=v)
                scheduler and scheduler.skip_next_poll("attenuator", 3.0)
```

PTT 门控（`elif field == "ptt":` 分支开头）：

```python
        elif field == "ptt":
            tx = value is True or str(value).lower() == "true"
            if tx and backend is not None and backend.capabilities.tx_gated:
                # Refused before any CAT write: an unverified model must not
                # transmit.  Backend guard stays as defense in depth.
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": ("This radio model is not hardware-verified — "
                                "transmit is disabled. Set "
                                "MRRC_ALLOW_UNVERIFIED_TX=1 and restart to "
                                "enable TX."),
                }))
                return
            if tx:
```

TUNE 门控（`elif field == "tune":` 分支，`on` 解析之后）：

```python
            if on and backend is not None and backend.capabilities.tx_gated:
                await ws.send_text(json.dumps({
                    "type": "error",
                    "message": ("This radio model is not hardware-verified — "
                                "transmit is disabled. Set "
                                "MRRC_ALLOW_UNVERIFIED_TX=1 and restart to "
                                "enable TX."),
                }))
                return
```

启动告警（在 `_caps = backend.capabilities` 之后）：

```python
        if not _caps.verified:
            logger.warning(
                "Radio model %s is NOT hardware-verified — transmit is %s "
                "(profile verified=False; set MRRC_ALLOW_UNVERIFIED_TX=1 to "
                "enable TX once the radio has been checked with _diag_civ.py)",
                _caps.model_name,
                "DISABLED" if _caps.tx_gated else "ENABLED by env override")
```

模型白名单：

```python
    if radio_model not in known_models():
        return JSONResponse({"error": "invalid radio_model"}, status_code=400)
```

- [x] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_server_ws_protocol.py" 2>&1 | tail -5`
运行：`.venv/bin/python -m unittest discover -s tests -p "test_server_*.py" 2>&1 | tail -3`
预期：`OK`

- [x] **步骤 5：Commit**

```bash
git add server.py tests/test_server_ws_protocol.py
git commit -m "feat(server): registry-driven model validation, capability attenuator bound, TX-gate errors"
```

---

### 任务 9：Frontend (dialog options, boost from capabilities, experimental badge, cache bump)

**文件：**

- 修改：`static/index.html:548-556`、`:523-525`（cache 版本）
- 修改：`static/ft710_main.js:423-431`（boost）、`:515-525`（branding 调用）
- 修改：`static/ft710_ui.js`（新增 `applyCapabilityBadges`）
- 修改：`static/sw.js:2,17,39`
- 测试：`tests/test_server_ws_protocol.py:229-236`、`tests/test_audio.py:151`

- [x] **步骤 1：编写失败的测试**

修改 `tests/test_server_ws_protocol.py` 的缓存版本断言（v27→v28、v29→v30、mrrc-v30→v31）：

```python
        self.assertIn('/ft710_main.js?v=28', index_source)
        self.assertIn('/ft710_ui.js?v=30', index_source)
        self.assertIn("const CACHE = 'mrrc-v31'", sw_source)
        self.assertIn("'/ft710_main.js?v=28'", sw_source)
        self.assertIn("'/ft710_ui.js?v=30'", sw_source)
```

修改 `tests/test_audio.py:151`：`self.assertIn("mrrc-v31", sw_source)`

追加到 `tests/test_server_ws_protocol.py`：

```python
class FrontendNewModelContractTests(unittest.TestCase):
    def test_connection_dialog_lists_new_models(self):
        with open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8") as fh:
            html = fh.read()
        for key in ("ic705", "ic7610", "ic7760"):
            self.assertIn(f'<option value="{key}">', html)
        self.assertIn("实验性", html)

    def test_audio_boost_comes_from_capabilities(self):
        with open(os.path.join(ROOT, "static", "ft710_main.js"),
                  encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("audio_gain_boost", js)

    def test_experimental_badge_function_exists(self):
        with open(os.path.join(ROOT, "static", "ft710_ui.js"),
                  encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("function applyCapabilityBadges(", js)
        self.assertIn("tx_gated", js)
```

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_server_ws_protocol.py" 2>&1 | tail -6`
预期：FAIL，`AssertionError: '/ft710_main.js?v=28' not found`

- [x] **步骤 3：编写最少实现代码**

`static/index.html` 下拉：

```html
          <option value="ft710">Yaesu FT-710</option>
          <option value="ic7300">Icom IC-7300</option>
          <option value="ic7300mk2">Icom IC-7300MK2</option>
          <option value="ic705">Icom IC-705（实验性）</option>
          <option value="ic7610">Icom IC-7610（实验性）</option>
          <option value="ic7760">Icom IC-7760（实验性）</option>
```

`static/index.html` 资源版本：`/ft710_main.js?v=28`、`/ft710_ui.js?v=30`

`static/sw.js`：`const CACHE = 'mrrc-v31';` 及两处资源串同步为 v28/v30

`static/ft710_main.js`（替换 boost 行）：

```js
   // Per-radio RX playback boost comes from capabilities when the
   // server provides it (Icom USB audio is hotter than the FT-710's);
   // the legacy string check stays as the fallback for older servers.
   AUDIO_GAIN_BOOST =
    msg.capabilities && typeof msg.capabilities.audio_gain_boost === "number"
     ? msg.capabilities.audio_gain_boost
     : radioModel === "ic7300"
      ? 1.0
      : 10.0;
```

在 `_applyRadioBranding();` 之后追加：

```js
   if (typeof applyCapabilityBadges === "function") applyCapabilityBadges();
```

`static/ft710_ui.js`（放在 `_caps()` 定义之后的“Per-radio capabilities”段）：

```js
// ── Experimental-model badge (spec 2026-09-12 §6.1) ─────────────────
// A model whose profile has no hardware evidence is labelled in the UI,
// and a TX-disabled notice is surfaced when the gate is closed — an
// unverified radio must never look fully supported.
function applyCapabilityBadges() {
    const c = _caps();
    const existing = document.getElementById('ft710-exp-badge');
    if (!c || c.verified !== false) {
        if (existing) existing.remove();
        return;
    }
    let badge = existing;
    if (!badge) {
        badge = document.createElement('span');
        badge.id = 'ft710-exp-badge';
        badge.style.cssText =
            'margin-left:6px;padding:1px 6px;border-radius:8px;' +
            'background:#78350f;color:#fbbf24;font-size:11px;vertical-align:middle;';
        badge.textContent = '实验性';
        (document.getElementById('status-bar') || document.body).appendChild(badge);
    }
    badge.title = c.tx_gated
        ? '该机型未硬件实测 — 发射已禁用（设置 MRRC_ALLOW_UNVERIFIED_TX=1 后重启可启用）'
        : '该机型未硬件实测 — 发射已由环境变量放行';
    if (c.tx_gated) {
        showToast('未实测机型：发射已禁用。设置 MRRC_ALLOW_UNVERIFIED_TX=1 并重启后可用。', 8000);
    }
}
```

- [x] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_server_ws_protocol.py" 2>&1 | tail -5`
运行：`.venv/bin/python -m unittest discover -s tests -p "test_audio.py" 2>&1 | tail -5`
预期：`OK`

- [x] **步骤 5：Commit**

```bash
git add static/index.html static/ft710_main.js static/ft710_ui.js static/sw.js tests/test_server_ws_protocol.py tests/test_audio.py
git commit -m "feat(ui): IC-705/7610/7760 dialog options, capability audio boost, experimental badge"
```

---

### 任务 10：`_diag_civ.py` diagnostic script

**文件：**

- 创建：`_diag_civ.py`
- 创建：`tests/test_diag_civ.py`

- [x] **步骤 1：编写失败的测试**

`tests/test_diag_civ.py`:

```python
"""Pure-helper tests for the CI-V diagnostic script (spec §7).

The serial path itself is exercised on hardware only; everything that
decides what the report says is testable here.
"""
import unittest

from _diag_civ import (
    ScopeStats, evaluate_identity, summarize_scope, format_report,
)


class ScopeStatsTests(unittest.TestCase):
    def test_summarize_counts_lengths_amplitudes_and_segments(self):
        samples = [
            {"bins": [0, 160] * 40, "seq_max": 11},
            {"bins": [0, 200] * 40, "seq_max": 11},
            {"bins": [10] * 80, "seq_max": 11},
        ]
        stats = summarize_scope(samples)
        self.assertEqual(stats.waveforms, 3)
        self.assertEqual(stats.min_bins, 80)
        self.assertEqual(stats.max_bins, 80)
        self.assertEqual(stats.amp_min, 0)
        self.assertEqual(stats.amp_max, 200)
        self.assertEqual(stats.seq_max, 11)
        self.assertIn(200, stats.amplitude_histogram)

    def test_summarize_empty_is_explicit(self):
        stats = summarize_scope([])
        self.assertEqual(stats.waveforms, 0)
        self.assertIsNone(stats.max_bins)


class IdentityEvaluationTests(unittest.TestCase):
    def test_no_expectation_is_unknown(self):
        verdict, text = evaluate_identity(bytes((0xA4,)), None)
        self.assertEqual(verdict, "unknown")
        self.assertIn("A4", text)

    def test_match_and_mismatch(self):
        self.assertEqual(evaluate_identity(bytes((0xA4,)), (0xA4,))[0],
                         "match")
        verdict, text = evaluate_identity(bytes((0xB2,)), (0xA4,))
        self.assertEqual(verdict, "mismatch")
        self.assertIn("B2", text)

    def test_no_answer(self):
        self.assertEqual(evaluate_identity(None, (0xA4,))[0], "no-answer")


class ReportFormattingTests(unittest.TestCase):
    def test_report_contains_measured_facts_and_boundary(self):
        stats = summarize_scope([{"bins": [5] * 475, "seq_max": 11}])
        report = format_report(
            model_key="ic705", display_name="Icom IC-705", port="/dev/null",
            baud=115200, civ_addr=0xA4, identity_text="unknown (no answer)",
            probe_results=[("frequency", "14074000")],
            scope_stats=stats, meter_results=[("po", 0)],
            tx_check="skipped (needs --tx-check --allow-tx)",
            profile_bins=475, profile_amp_max=160, profile_seq_max=11,
        )
        self.assertIn("ic705", report)
        self.assertIn("475", report)
        self.assertIn("scope_bins", report)
        self.assertIn("TX check", report)
```

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_diag_civ.py" 2>&1 | tail -5`
预期：FAIL，`ModuleNotFoundError: No module named '_diag_civ'`

- [x] **步骤 3：编写最少实现代码**

`_diag_civ.py`（完整可运行脚本；所有串口 I/O 走生产 `CivController`，无裸 pyserial）：

```python
#!/usr/bin/env python3
"""
Generic CI-V self-check for the Icom backend (spec 2026-09-12 §7)
=================================================================
Steps: model ID (19 00) -> read-only probes -> scope measurement ->
static meters -> optional TX check -> state restore -> Markdown report.

Safe by default: the TX check runs only with BOTH ``--tx-check`` and
``--allow-tx``, reads PTT back after releasing, and prints manual
recovery steps if the radio does not report RX.

Usage:
    python _diag_civ.py --model ic705 --port /dev/cu.usbmodem1234
    python _diag_civ.py --model ic7760 --port COM5 --tx-check --allow-tx
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from backends.ic7300.civ_codec import ScopeAssembler
from backends.ic7300.civ_controller import CivController
from backends.ic7300.civ_profiles import PROFILES, get_profile

TX_SETTLE_S = 0.2
TX_READBACK_TIMEOUT_S = 3.0
SCOPE_CAPTURE_S = 6.0


@dataclass
class ScopeStats:
    waveforms: int = 0
    min_bins: Optional[int] = None
    max_bins: Optional[int] = None
    amp_min: Optional[int] = None
    amp_max: Optional[int] = None
    seq_max: Optional[int] = None
    amplitude_histogram: dict = field(default_factory=dict)
    fps: float = 0.0


def summarize_scope(samples: list) -> ScopeStats:
    """Aggregate captured waveforms into the facts the profile needs."""
    stats = ScopeStats()
    if not samples:
        return stats
    lengths = [len(s["bins"]) for s in samples if s.get("bins")]
    amps = [b for s in samples for b in (s.get("bins") or [])]
    if lengths:
        stats.min_bins, stats.max_bins = min(lengths), max(lengths)
    if amps:
        stats.amp_min, stats.amp_max = min(amps), max(amps)
        for value in amps:
            bucket = (value // 10) * 10
            stats.amplitude_histogram[bucket] = (
                stats.amplitude_histogram.get(bucket, 0) + 1)
    seqs = [s.get("seq_max") for s in samples if s.get("seq_max")]
    if seqs:
        stats.seq_max = max(seqs)
    stats.waveforms = len(samples)
    return stats


def evaluate_identity(observed: Optional[bytes],
                      expected: Optional[tuple]) -> tuple:
    """(verdict, human text) for the 19 00 model-ID comparison."""
    if observed is None:
        return "no-answer", "no answer to 19 00 (radio may not implement it)"
    hexed = observed.hex(" ").upper()
    if expected is None:
        return ("unknown",
                f"observed model ID {hexed}; the profile records no expectation "
                f"(paste this report to populate it)")
    if tuple(observed) == tuple(expected):
        return "match", f"model ID {hexed} matches the profile"
    return ("mismatch",
            f"model ID {hexed} != profile expectation "
            f"{bytes(expected).hex(' ').upper()}")


def format_report(**kw) -> str:
    """Render the paste-ready Markdown report."""
    stats: ScopeStats = kw["scope_stats"]
    lines = [
        f"# CI-V diagnostic report — {kw['display_name']} ({kw['model_key']})",
        "",
        f"- Date: {datetime.now().isoformat(timespec='seconds')}",
        f"- Port: {kw['port']} @ {kw['baud']} 8N1, CI-V address "
        f"0x{kw['civ_addr']:02X}",
        f"- Model identity (19 00): {kw['identity_text']}",
        "",
        "## Read-only probes",
    ]
    for name, value in kw["probe_results"]:
        lines.append(f"- {name}: {value}")
    lines += [
        "",
        "## Scope measurement",
        f"- waveforms captured: {stats.waveforms}",
        f"- bin count observed: {stats.min_bins}..{stats.max_bins} "
        f"(profile scope_bins={kw['profile_bins']})",
        f"- amplitude observed: {stats.amp_min}..{stats.amp_max} "
        f"(profile scope_amp_max={kw['profile_amp_max']})",
        f"- segment count observed: {stats.seq_max} "
        f"(profile scope_seq_max={kw['profile_seq_max']})",
        f"- amplitude histogram (10-wide buckets): {stats.amplitude_histogram}",
        "",
        "## Meters (static, no RF)",
    ]
    for name, value in kw["meter_results"]:
        lines.append(f"- {name}: {value}")
    lines += [
        "",
        "## TX check",
        f"- {kw['tx_check']}",
        "",
        "## What this report can change in the repository",
        "Any of the three profile numbers above that differs from the "
        "observed value is a profile bug: update "
        "`backends/ic7300/civ_profiles.py`, drop the corresponding "
        "`TODO(hw-verify)` note, and set `verified=True` only for the "
        "meters a hardware run actually confirmed.",
    ]
    return "\n".join(lines) + "\n"


async def run(args) -> int:
    profile = get_profile(args.model)
    civ = CivController(args.port, args.baud,
                        civ_addr=args.civ_addr or profile.civ_addr,
                        transceive_cmd=profile.transceive_cmd,
                        profile=profile, att_steps=profile.att_steps)
    probe_results: list = []
    meter_results: list = []
    samples: list = []
    identity_text = "not attempted"
    tx_check = "skipped (needs --tx-check --allow-tx)"
    scope_enabled = False
    try:
        if not await civ.connect():
            print(f"ERROR: cannot open {args.port}", file=sys.stderr)
            return 2

        observed = await civ.get_model_id(timeout=1.0)
        verdict, identity_text = evaluate_identity(observed,
                                                  profile.model_id_bytes)
        print(f"Model identity: {verdict} — {identity_text}")

        for name, coro in (
            ("frequency", civ._query_data(0x03)),
            ("mode", civ._query_data(0x04)),
            ("s_meter", civ.get_meter("s")),
            ("rf_power", civ._query_data(0x14, 0x0A)),
            ("preamp", civ._query_data(0x16, 0x02)),
            ("attenuator", civ._query_data(0x11)),
            ("transceive_item", civ._query_data(0x1A, 0x05)),
        ):
            value = await coro
            probe_results.append(
                (name, value.hex(" ").upper() if isinstance(value, bytes)
                 else str(value)))
            print(f"  {name}: {probe_results[-1][1]}")

        assembler = ScopeAssembler(seq_max=profile.scope_seq_max,
                                   expected_bins=profile.scope_bins)
        await civ.set_scope_on(True)
        scope_enabled = True
        await civ.set_scope_mode(0)
        await civ.set_scope_span(5)
        await civ.set_scope_data_output(True)
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            try:
                segment = await asyncio.wait_for(civ.scope_queue.get(),
                                                 timeout=1.0)
            except asyncio.TimeoutError:
                continue
            bins = assembler.feed(segment)
            if bins:
                samples.append({"bins": bins,
                                "seq_max": segment.sequence_max})
        stats = summarize_scope(samples)
        print(f"  scope: {stats.waveforms} waveforms, bins "
              f"{stats.min_bins}..{stats.max_bins}, amp "
              f"{stats.amp_min}..{stats.amp_max}")

        # get_meter() deliberately leaves vd/id unmapped on this backend
        # (has_vd_id_meters=False), so those two are read raw — the
        # diagnostic is exactly the place where they must still be visible.
        for meter in ("po", "swr", "alc", "comp"):
            meter_results.append((meter, await civ.get_meter(meter)))
        for name, sub in (("vd", 0x15), ("id", 0x16)):
            raw = await civ._query_data(0x15, sub)
            meter_results.append(
                (name, raw.hex(" ").upper() if raw else None))
        print(f"  meters: {meter_results}")

        if args.tx_check and args.allow_tx:
            print("TX check: keying for "
                  f"{TX_SETTLE_S * 1000:.0f} ms …")
            await civ.set_ptt(True)
            await asyncio.sleep(TX_SETTLE_S)
            await civ.set_ptt(False)
            released = False
            deadline = time.monotonic() + TX_READBACK_TIMEOUT_S
            while time.monotonic() < deadline:
                state = await civ.get_ptt()
                if state == 0:
                    released = True
                    break
                await asyncio.sleep(0.2)
            tx_check = "PASS (PTT released and read back as RX)" if released \
                else ("FAIL — radio still reports TX! Power the radio off / "
                      "unplug USB now, then re-check the 1C 00 setting with "
                      "Icom's documentation before retrying")
            print(f"TX check: {tx_check}")
        elif args.tx_check:
            print("TX check refused: --tx-check also needs --allow-tx")

        report = format_report(
            model_key=profile.model_key, display_name=profile.display_name,
            port=args.port, baud=args.baud, civ_addr=civ.civ_addr,
            identity_text=identity_text, probe_results=probe_results,
            scope_stats=stats, meter_results=meter_results,
            tx_check=tx_check, profile_bins=profile.scope_bins,
            profile_amp_max=profile.scope_amp_max,
            profile_seq_max=profile.scope_seq_max)
        path = f"diag_civ_{profile.model_key}_{datetime.now():%Y%m%d_%H%M%S}.md"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"Report written: {path}")
        return 0
    finally:
        # Restore the radio to the state we found it in.
        if scope_enabled:
            try:
                await civ.set_scope_data_output(False)
            except Exception:
                pass
        try:
            await civ.set_ptt(False)
        except Exception:
            pass
        await civ.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, choices=sorted(PROFILES))
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--civ-addr", type=lambda x: int(x, 0), default=None)
    parser.add_argument("--seconds", type=float, default=SCOPE_CAPTURE_S,
                        help="scope capture window (default 6 s)")
    parser.add_argument("--tx-check", action="store_true",
                        help="run the keyed TX self-check (still needs --allow-tx)")
    parser.add_argument("--allow-tx", action="store_true",
                        help="confirm the operator accepts RF being emitted")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [x] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_diag_civ.py" 2>&1 | tail -5`
运行：`.venv/bin/python _diag_civ.py --help 2>&1 | head -12`
预期：测试 `OK`；`--help` 打印参数列表（证明脚本可导入/可运行，无需硬件）

- [x] **步骤 5：Commit**

```bash
git add _diag_civ.py tests/test_diag_civ.py
git commit -m "feat(diag): generic _diag_civ.py self-check (model ID, scope geometry, meters, gated TX)"
```

---

### 任务 11：Packaging hiddenimport

**文件：**

- 修改：`packaging/pyinstaller/mrrc_modern_server.spec:89-93`
- 测试：`tests/test_windows_packaging_files.py`

- [x] **步骤 1：编写失败的测试**

追加到 `tests/test_windows_packaging_files.py`：

```python
class IcomProfileHiddenImportTests(unittest.TestCase):
    def test_civ_profiles_is_bundled(self):
        spec = (ROOT / "packaging" / "pyinstaller"
                / "mrrc_modern_server.spec").read_text(encoding="utf-8")
        self.assertIn("backends.ic7300.civ_profiles", spec)
```

（若该文件未定义 `ROOT`，使用该文件中既有的路径常量。）

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_windows_packaging_files.py" 2>&1 | tail -5`
预期：FAIL，`AssertionError: 'backends.ic7300.civ_profiles' not found`

- [x] **步骤 3：编写最少实现代码**

`packaging/pyinstaller/mrrc_modern_server.spec` 的 hiddenimports 列表（按字母序插入）：

```python
        "backends.ic7300.backend",
        "backends.ic7300.civ_codec",
        "backends.ic7300.civ_controller",
        "backends.ic7300.civ_profiles",
        "backends.ic7300.civ_scope",
        "backends.ic7300.config_ic7300",
```

- [x] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest discover -s tests -p "test_windows_packaging_files.py" 2>&1 | tail -5`
预期：`OK`

- [x] **步骤 5：Commit**

```bash
git add packaging/pyinstaller/mrrc_modern_server.spec tests/test_windows_packaging_files.py
git commit -m "build(pyinstaller): bundle backends.ic7300.civ_profiles"
```

---

### 任务 12：Documentation synchronization

**文件：**

- 修改：`SDD/14-version-history.md`、`SDD/README.md`、`SDD/08-architecture-decisions.md`、`SDD/05-non-functional-requirements.md`、`SDD/09-architecture-overview.md`、`SDD/11-component-model.md`、`SDD/13-feasibility-assessment.md`、`SDD/15-ptt-safety-architecture.md`
- 修改：`AGENTS.md`、`README.md`、`tests/README.md`、`IC-7300_硬件验收清单.md`

- [ ] **步骤 1：取得准确的测试计数**

运行：`.venv/bin/python -m unittest discover -s tests 2>&1 | tail -3`
记录 `Ran N tests` 与模块数：`ls tests/test_*.py | wc -l`

- [ ] **步骤 2：SDD/14 版本历史新条目（V2.41）**

在 `SDD/14-version-history.md` 的表格顶部（紧接表头）插入一行，内容必须包含：新增三机型与 profile 驱动核心；`backends/ic7300/civ_profiles.py`；`ScopeAssembler`/`CivScopeProducer`/`CivController` 的参数化（默认值 = 旧常量）；`RadioCapabilities.verified/tx_gated/dual_rx/unverified_meters/audio_gain_boost`；`RadioState.model_mismatch`；`MRRC_ALLOW_UNVERIFIED_TX` 门控（release 永不被拦）；`19 00` 只记录不判定；`_diag_civ.py`；新增测试模块与总数；明确写出"三台机型无真机验证"的边界。

- [ ] **步骤 3：SDD/README Quick Facts**

`SDD Version | V2.41`，状态行补一句：`IC-705/IC-7610/IC-7760 backend profiles added without hardware verification (TX gated by MRRC_ALLOW_UNVERIFIED_TX); diagnostic script _diag_civ.py ready for field reports;`

- [ ] **步骤 4：AD-016 扩充**

在 `SDD/08-architecture-decisions.md` AD-016 的 Consequences 段追加：

```
**Amended 2026-09-12 (V2.41)**: the Icom side of the backend layer became profile-driven —
`backends/ic7300/civ_profiles.py` holds one `CivModelProfile` per model (address, Transceive
item, scope geometry, bands, modes, attenuator steps, meter curves, verification flags) and the
shared codec/controller/scope producer take those values as optional parameters whose defaults
reproduce the previous IC-7300 constants. Adding IC-705/IC-7610/IC-7760 therefore required no
new protocol code and no `server.py` model branches; models without hardware evidence carry
`verified=False` and refuse PTT/TUNE until `MRRC_ALLOW_UNVERIFIED_TX=1`.
```

- [ ] **步骤 5：SDD/05、09、11、13、15**

- `SDD/05`：NFR-032 的 verification 列补 "five model keys (ft710, ic7300, ic7300mk2, ic705, ic7610, ic7760)"; NFR-041 补 `MRRC_ALLOW_UNVERIFIED_TX`；NFR-060/065 注明 IC-705/7610/7760 走 48 kHz 无重采样路径（assumed, `TODO(hw-verify)`）。
- `SDD/09`：backend 综述加一句 profile 驱动 + 三机型。
- `SDD/11`：component 表新增 `civ_profiles.py` 行与三个 backend 行。
- `SDD/13.2`：新增风险 **R9**：`Unverified model profiles (IC-705/IC-7610/IC-7760) ship without hardware evidence — the 689-bin amplitude ceiling, meter curves and USB audio properties are inherited assumptions` | Medium-High | Medium | `verified=False` + `unverified_meters` + `MRRC_ALLOW_UNVERIFIED_TX` TX gate + `_diag_civ.py` report round-trip`。
- `SDD/13.3`：新增假设 **A7**：`The three new models share the IC-7300 CI-V command surface (frequency/mode/preamp/AGC/NB/NR/comp/filter-width/squelch/RF power/PTT/tune, meter sub-codes) — evidenced by identical wfview rig command tables` | Medium | `_diag_civ.py` read-only probe step`。**注意**：不要把它写成 High——它是基于本地 rig 数据的推断。
- `SDD/15`：在分层释放模型之前加一层说明：unverified 机型的 PTT/TUNE 在 backend 边界即被拒绝（`MRRC_ALLOW_UNVERIFIED_TX` 放行），释放路径永不门控。

- [ ] **步骤 6：AGENTS.md / README.md / tests/README.md / 硬件验收清单**

- `AGENTS.md`：模块表 `backends/ic7300/` 行补 `civ_profiles.py`（"per-model CI-V profiles: address, Transceive item, scope geometry, bands, modes, attenuator steps, meter curves, verification flags — the single source of truth for every Icom model"），并在环境变量清单加入 `MRRC_ALLOW_UNVERIFIED_TX` 与三个新模型 key；模块表 `server.py` 行补"model validation from the backend registry"。
- `README.md`：支持机型表加三行，标注 `experimental — not hardware-verified (TX disabled until MRRC_ALLOW_UNVERIFIED_TX=1)`。
- `tests/README.md`：更新 Total/模块数（用步骤 1 的实测值）并新增四个测试模块的用途说明。
- `IC-7300_硬件验收清单.md`：末尾加一段指向 `_diag_civ.py`（多机型自检的继任工具）并说明其与本文档的关系。

- [ ] **步骤 7：Commit**

```bash
git add SDD/ AGENTS.md README.md tests/README.md IC-7300_硬件验收清单.md
git commit -m "docs(sdd): V2.41 — ICOM SDR model profiles, TX gate, unverified boundary (R9/A7)"
```

---

### 任务 13：Final verification

- [x] **步骤 1：全量测试**

运行：`.venv/bin/python -m unittest discover -s tests 2>&1 | tail -3`
预期：`OK`，无 FAIL/ERROR，计数与 `tests/README.md` 一致

- [x] **步骤 2：语法与静态检查**

运行：`python3 -m py_compile server.py config.py radio_state.py backends/base.py backends/ic7300/*.py _diag_civ.py && echo COMPILE_OK`
运行：`node --check static/ft710_main.js && node --check static/ft710_ui.js && node --check static/sw.js && echo JS_OK`
运行：`git diff --check && echo DIFF_CLEAN`
预期：`COMPILE_OK` / `JS_OK` / `DIFF_CLEAN`

- [x] **步骤 3：SDD Guardian 检查**

运行：`python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged`
若尚无暂存文件：`python3 .agents/skills/sdd-guardian/harness/sdd_context.py check backends/ic7300/civ_profiles.py server.py config.py radio_state.py backends/base.py _diag_civ.py`
预期：`SDD-GUARDIAN: clean` 或仅有可解释的 warn（例如 `env-hardcoded-device`）

- [x] **步骤 4：能力表人工核对**

运行：`.venv/bin/python -c "
from backends import create_backend
for m in ('ft710','ic7300','ic7300mk2','ic705','ic7610','ic7760'):
    c = create_backend(m, port='/dev/null').capabilities
    print(f\"{m:10s} verified={c.verified!s:5s} tx_gated={c.tx_gated!s:5s} \
scope={c.scope_type:6s} att={len(c.att_steps)} audio={c.audio_rx_rate} boost={c.audio_gain_boost}\")
"`
预期：`ft710`/`ic7300`/`ic7300mk2` → `tx_gated=False`；`ic705`/`ic7610`/`ic7760` → `verified=False tx_gated=True`

- [x] **步骤 5：LSP 诊断**

运行：`lens_diagnostics mode=all`（对所有已编辑文件）
预期：无新增 blocking error

- [x] **步骤 6：硬件边界声明**

在最终报告中明确写出：本变更的证据边界 = 离线 rig 数据 + 单元测试 + 能力表自查；**未**验证：`19 00` 机型 ID 字节、IC-7610/7760 的 689-bin/200 幅值/15 段、三台的表计绝对刻度、USB 音频速率与设备名、前面板 CI-V 菜单前置条件、SUB 接收。这些由 `_diag_civ.py` 在现场报告中解决，并据此翻转对应 profile 的 `verified` 标志。

- [x] **步骤 7：最终 Commit（若步骤 1-5 产生了修正）**

```bash
git add -A
git commit -m "test(icom): final verification adjustments for ICOM SDR profiles"
```

---

## 自检记录（编写计划后对照规格）

**1. 规格覆盖度：**

| 规格章节 | 覆盖任务 |
| --- | --- |
| §2 D1–D6（机型/无真机/rig 数据/MAIN-only/门控/架构） | 任务 1、6（D4 由 `dual_rx` 通告 + 不发 cmd 29 实现）、全局 |
| §3 数据来源表 | 任务 1（每条 profile 带 `source`，值与 rig 数据一致） |
| §4.1 profile 模块 | 任务 1 |
| §4.2 参数化（codec/scope/controller/base） | 任务 2、3、4、5 |
| §4.3 新 backend 类与注册 | 任务 6 |
| §5.1 汇总表 | 任务 1（测试逐项断言地址/item/几何/ATT/功率/mode 增量） |
| §5.2 波段表（含功率裁减） | 任务 1（含规格更正步骤 6） |
| §5.3 模式表 + ui_modes + scope span 5 | 任务 1（mode 表）、任务 6（`ui_modes` 沿用 `UI_MODES_IC7300`） |
| §5.4 表计标定（`power_frac` × 额定功率，`unverified_meters`） | 任务 1、5、6 |
| §5.5 串口带宽 | 文档性；无需代码（任务 12 记录在 SDD） |
| §6.1 TX 门控（backend 边界 + release 不拦 + server 提示 + 不门控电源） | 任务 6、7、8 |
| §6.2 机型身份（`model_id_bytes=None`、只记录、可告警、不阻塞） | 任务 4（查询）、6（判定与 state） |
| §6.3 频谱自适应降级 | 任务 2、3 |
| §7 诊断脚本（7 步 + finally 恢复 + 报告） | 任务 10 |
| §8 接线表（含 `radio_state.py` 与 FT-710 `audio_gain_boost`） | 任务 5、6、8、9、11 |
| §9 测试计划 | 任务 1–11 的测试 + 任务 13 |
| §10 文档同步 | 任务 12 |
| §11 硬件验收边界 | 任务 13 步骤 6 |
| §12 非目标 | 未实现任何 LAN/SUB/新 CI-V 功能 |
| §13 SDD 追溯 | 任务 12 |

**2. 占位符扫描：** 已完成——每个代码步骤都给出完整可粘贴实现；没有 "TODO"、"后续实现"、"类似任务 N"、"添加适当的错误处理"。唯一的 `TODO(hw-verify)` 是规格明文要求的硬件验证标记，且与 `verified=False` 绑定。

**3. 类型一致性：**

- `CivModelProfile`：任务 1 定义 `transceive_item`/`transceive_cmd`/`scope_queue_segments`/`attenuator_labels()`/`filter_hz()`/`get_band_for_frequency()`/`narrow_modes()`；任务 4、6 只使用这些名字。
- `MeterCal`：任务 1 定义 `raw_to_dbm/raw_to_s_unit/raw_to_power/raw_to_swr/raw_to_alc/raw_to_alc_pct/raw_to_comp/raw_to_voltage/raw_to_current` 与 `rated_power_w`；任务 6 的 `state_tables()` 与之逐项对应（`RadioState.configure` 的键名一致）。
- `ScopeAssembler(seq_max=…, expected_bins=…)`：任务 2 定义，任务 3 与任务 10 传参一致。
- `CivScopeProducer(controller, scope, on_frame, amp_max=…, expected_bins=…, seq_max=…)`：任务 3 定义，任务 6 调用一致。
- `CivController(..., profile=…, att_steps=…)` 与 `get_model_id()`：任务 4 定义，任务 6/10 调用一致。
- `RadioCapabilities` 新字段名（`verified`/`tx_gated`/`dual_rx`/`unverified_meters`/`audio_gain_boost`）：任务 5 定义，任务 6/8/9 使用一致。
- `RadioState.model_mismatch`：任务 5 定义，任务 6 通过 `initial_state_sync()` 返回键写入。
- `backends.known_models()`：任务 6 定义，任务 7/8 使用一致（`config._DEFAULT_BAUD_BY_MODEL` 的键与之对齐，由任务 7 的测试断言）。
