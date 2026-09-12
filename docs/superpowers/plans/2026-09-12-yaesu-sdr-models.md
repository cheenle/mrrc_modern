# Yaesu SDR Model Support (FTDX10 / FTDX101D / FTDX101MP / FTX-1F) Implementation Plan

> **For AI agent workers:** Required sub-skill: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Track progress with the checkboxes below.

**Goal:** Add four Yaesu ASCII-CAT radios (FTDX10, FTDX101D, FTDX101MP, FTX-1F) on a new profile-driven Yaesu core in `backends/yaesu/`, with the hardware-verified FT-710 path left completely untouched, and with the same explicit unverified-model boundary the Icom models use (capability flags, TX gate behind `MRRC_ALLOW_UNVERIFIED_TX`, read-only `ID;` identity check, per-table provenance).

**Architecture:** `backends/yaesu/yaesu_profiles.py` is the single source of truth for per-model Yaesu facts (mode code table, filter widths, bands, attenuator/preamp steps, power format, S-meter calibration, verification status, provenance). `backends/yaesu/cat_core.py` carries over the transport logic that is already field-proven in `backends/ft710/cat_controller.py` (frame reading, priority writes, ENXIO classification, reconnect) and reads every model difference from the profile. `backends/yaesu/backend.py` derives `RadioCapabilities`, UI tables, poll items and meter items from the profile, exactly as `IC7300Backend` does; the four model backends are subclasses that override `_profile`/`_display_name` only. No WebSocket, `radio_state.py`, `poll_scheduler.py` or waterfall change: single receiver, no scope stream (`scope_type="none"` → the existing S-meter synthesiser covers the UI).

**Tech stack:** Python 3.13, `unittest` / `IsolatedAsyncioTestCase`, `unittest.mock`, asyncio, pyserial, `pty` (test-only fake radio), FastAPI (untouched), vanilla JavaScript (one dropdown edit), SDD Guardian.

**Design authority:** `docs/superpowers/specs/2026-09-12-yaesu-sdr-models-design.md` (committed `18baba9`).

---

## File Structure

- **Create** `backends/yaesu/__init__.py`: package exports (`YaesuBackend`, `PROFILES`, `get_profile`, `known_models`).
- **Create** `backends/yaesu/yaesu_profiles.py`: `MeterCal`, `YaesuModelProfile`, `PROFILES`, `get_profile()`, `known_models()`. One responsibility: per-model Yaesu facts as data, no I/O, no `asyncio`.
- **Create** `backends/yaesu/cat_core.py`: `YaesuCatController`. Transport (framing, priority writes, ENXIO classification, reconnect) plus the profile-driven command layer (frequency, mode, filter, PTT/TUNE, meters, gains, DSP, memory, `ID;`). No UI tables, no capabilities.
- **Create** `backends/yaesu/backend.py`: `YaesuBackend(RadioBackend)` + `FTDX10Backend`, `FTDX101DBackend`, `FTDX101MPBackend`, `FTX1Backend` (each overriding `_profile` + `_display_name`).
- **Create** `_diag_yaesu.py`: read-only field self-check driving the production `YaesuCatController`, emitting a paste-ready Markdown report (modelled on `_diag_civ.py`).
- **Create** tests: `tests/test_yaesu_profiles.py`, `tests/test_yaesu_cat_core.py`, `tests/test_yaesu_backend.py`, `tests/test_yaesu_fake_radio.py`, `tests/test_diag_yaesu.py`.
- **Modify** `backends/__init__.py`: four registry entries (lazy import).
- **Modify** `config.py`: four `_DEFAULT_BAUD_BY_MODEL` entries at 38400.
- **Modify** `static/index.html`: four dialog options (the model list is a hardcoded `<select>`, lines ~552–559).
- **Modify** `packaging/pyinstaller/mrrc_modern_server.spec`: hiddenimports for `backends.yaesu.*`.
- **Modify** tests: `tests/test_backend_factory.py` (exact model tuple), `tests/test_config.py` (baud assertions).
- **Modify** docs: `README.md`, `AGENTS.md`, `DEPENDENCIES.md`, `tests/README.md`, `SDD/02`, `SDD/05`, `SDD/08` (AD-018/AD-019), `SDD/09`, `SDD/10`, `SDD/11`, `SDD/12`, `SDD/13`, `SDD/14`, `SDD/15`, `SDD/README.md`.

### 任务 1：Profile data module

**文件：**

- 创建：`backends/yaesu/yaesu_profiles.py`
- 创建：`backends/yaesu/__init__.py`
- 创建：`tests/test_yaesu_profiles.py`

- [x] **步骤 1：编写失败的测试**

`tests/test_yaesu_profiles.py`:

```python
"""Tests for the per-model Yaesu profiles (spec 2026-09-12 §5).

Every assertion is hardware-independent: it checks that the profile data is
internally consistent and matches the offline evidence recorded in
`provenance`, not that a radio behaves as documented.
"""
import unittest

from backends.yaesu import yaesu_profiles as yp

# Evidence: Hamlib 4.7.2, ~/hamlib/Hamlib-4.7.2/rigs/yaesu/
#   newcat.c:12043 newcat_mode_conv[]   (family mode characters)
#   ftx1/ftx1_mode.c header comment      (FTX-1 mode characters, E=PSK, H/I=C4FM)
#   ftdx10.c:256 /.filters               (FTDX10 filter widths)
#   ftdx101.c:299 /.filters              (FTDX101 filter widths)
#   newcat.c:339 yaesu_default_str_cal   (FTDX10 S-meter curve, 11 points)
#   ftdx101.h:164 FTDX101D_STR_CAL       (12 points)
#   ftx1/ftx1.h:105 FTX1_STR_CAL         (16 points)
EXPECTED_KEYS = ("ftdx10", "ftdx101d", "ftdx101mp", "ftx1")


class ProfileRegistryTests(unittest.TestCase):
    def test_known_models(self):
        self.assertEqual(yp.known_models(), EXPECTED_KEYS)

    def test_get_profile_round_trip(self):
        for key in EXPECTED_KEYS:
            self.assertIs(yp.get_profile(key), yp.PROFILES[key])
        with self.assertRaises(KeyError):
            yp.get_profile("ft710")      # FT-710 is a separate, verified path


class ProfileInvariantTests(unittest.TestCase):
    def test_all_models_are_unverified_and_tx_gated(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertFalse(p.verified, key)
            self.assertTrue(p.tx_gated, key)

    def test_provenance_is_recorded_for_every_table(self):
        required = {"mode_numbers", "filter_widths", "s_meter_cal",
                    "bands", "power_format", "model_overall"}
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertEqual(required - set(p.provenance), set(), key)
            for table, source in p.provenance.items():
                self.assertTrue(source.strip(), f"{key}/{table}")
                # A provenance string must name a file the plan cites.
                self.assertRegex(source, r"\.(c|h|md|txt)|manual|FTX-1_CAT")

    def test_ui_modes_are_defined_in_mode_numbers(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertTrue(p.ui_modes, key)
            for name in p.ui_modes:
                self.assertIn(name, p.mode_numbers, f"{key}: {name}")

    def test_mode_registers_are_unique_and_have_a_cat_code(self):
        """Registers are ints (RadioState contract); H/I are not hex digits."""
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            registers = list(p.mode_numbers.values())
            self.assertEqual(len(registers), len(set(registers)), key)
            for name, num in p.mode_numbers.items():
                self.assertIsInstance(num, int, f"{key}/{name}")
                self.assertIn(num, p.mode_codes, f"{key}/{name}")
            self.assertEqual(len(set(p.mode_codes)), len(p.mode_codes), key)

    def test_filter_widths_are_ordered_and_positive(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertTrue(p.filter_widths, key)
            for mode, widths in p.filter_widths.items():
                self.assertTrue(widths, f"{key}/{mode}")
                indexes = [i for i, _ in widths]
                self.assertEqual(indexes, sorted(indexes), f"{key}/{mode}")
                self.assertEqual(len(indexes), len(set(indexes)), f"{key}/{mode}")
                for _, hz in widths:
                    self.assertGreater(hz, 0, f"{key}/{mode}")

    def test_attenuator_and_preamp_steps(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertEqual(p.att_steps[0], 0, key)          # 0 dB = off
            self.assertEqual(list(p.att_steps), sorted(p.att_steps), key)
            self.assertEqual(len(p.att_steps), len(set(p.att_steps)), key)
            self.assertTrue(p.preamp_labels, key)

    def test_s_meter_curve_is_monotonic_and_reaches_s9(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            points = p.s_meter_cal.points
            raws = [r for r, _ in points]
            dbms = [d for _, d in points]
            self.assertEqual(raws, sorted(raws), key)
            self.assertEqual(dbms, sorted(dbms), key)         # monotonic
            self.assertEqual(points[0][0], 0, key)            # raw 0 = S0
            self.assertEqual(points[-1][0], 255, key)         # full scale
            self.assertEqual(p.s_meter_cal.value(0), points[0][1], key)
            self.assertEqual(p.s_meter_cal.value(255), points[-1][1], key)
            self.assertEqual(p.s_meter_cal.value(999), points[-1][1], key)

    def test_bands_are_ascending_and_inside_the_models_range(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            lows = [low for _, low, _ in p.bands]
            self.assertEqual(lows, sorted(lows), key)
            for label, low, high in p.bands:
                self.assertLess(low, high, f"{key}/{label}")

    def test_power_format_and_limits(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertIn(p.power_format, ("PC1", "PC2", "auto"), key)
            self.assertGreater(p.power_max_w, 0, key)

    def test_audio_hints_are_present(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertGreater(p.audio_rx_rate, 0, key)
            self.assertTrue(p.audio_name_hints, key)
            self.assertGreater(p.audio_gain_boost, 0, key)

    def test_unverified_meters_named_but_not_invented(self):
        for key in EXPECTED_KEYS:
            p = yp.get_profile(key)
            self.assertIn("s_meter", p.unverified_meters, key)
            self.assertIn("power", p.unverified_meters, key)


class ModelSpecificDataTests(unittest.TestCase):
    def test_ftdx10_uses_the_documented_family_mode_table(self):
        p = yp.get_profile("ftdx10")
        self.assertEqual(p.mode_numbers["LSB"], 0x1)
        self.assertEqual(p.mode_numbers["USB"], 0x2)
        self.assertEqual(p.mode_numbers["CW-U"], 0x3)
        self.assertEqual(p.mode_numbers["DATA-U"], 0xC)
        self.assertEqual(p.mode_numbers["AM-N"], 0xD)
        self.assertEqual(p.mode_numbers["C4FM"], 0xE)
        self.assertEqual(p.mode_codes[0xE], "E")

    def test_ftx1_mode_codes_are_not_hex_formatted(self):
        """ftx1/ftx1_mode.c: E=PSK, H=C4FM-DN, I=C4FM-VW.

        H and I are not hex digits, so the CAT layer must look the character
        up instead of formatting the register as hex — `f"{0x11:X}"` would
        send `MD011`, which is not a mode at all.
        """
        p = yp.get_profile("ftx1")
        self.assertEqual(p.mode_numbers["PSK"], 0xE)
        self.assertEqual(p.mode_numbers["C4FM-DN"], 0x10)
        self.assertEqual(p.mode_numbers["C4FM-VW"], 0x11)
        self.assertEqual(p.mode_codes[0xE], "E")
        self.assertEqual(p.mode_codes[0x10], "H")
        self.assertEqual(p.mode_codes[0x11], "I")
        self.assertNotIn("C4FM", p.mode_numbers)

    def test_ftdx10_filter_table_matches_hamlib(self):
        p = yp.get_profile("ftdx10")
        self.assertEqual([hz for _, hz in p.filter_widths["CW-U"]],
                         [2400, 600, 300, 1200])
        self.assertEqual([hz for _, hz in p.filter_widths["USB"]],
                         [3000, 2400, 1800])

    def test_ftdx101mp_has_greater_power_than_ftdx101d(self):
        self.assertEqual(yp.get_profile("ftdx101d").power_max_w, 100)
        self.assertEqual(yp.get_profile("ftdx101mp").power_max_w, 200)

    def test_ftx1_is_the_only_multi_band_and_auto_power_model(self):
        self.assertEqual(yp.get_profile("ftx1").power_format, "auto")
        for key in ("ftdx10", "ftdx101d", "ftdx101mp"):
            self.assertEqual(yp.get_profile(key).power_format, "PC1")
        labels = [b[0] for b in yp.get_profile("ftx1").bands]
        self.assertIn("2m", labels)
        self.assertIn("70cm", labels)

    def test_only_explicitly_configured_models_declare_an_id(self):
        self.assertEqual(yp.get_profile("ftx1").id_answer, "0840")
        for key in ("ftdx10", "ftdx101d", "ftdx101mp"):
            # Unknown until real hardware answers (spec §3, §10).
            self.assertEqual(yp.get_profile(key).id_answer, "")

    def test_dual_rx_is_recorded_but_not_implemented(self):
        self.assertTrue(yp.get_profile("ftdx101d").dual_rx)
        self.assertTrue(yp.get_profile("ftdx101mp").dual_rx)
        self.assertTrue(yp.get_profile("ftx1").dual_rx)
        self.assertFalse(yp.get_profile("ftdx10").dual_rx)


class MeterCalTests(unittest.TestCase):
    def test_interpolation_between_points(self):
        cal = yp.MeterCal(points=((0, -60.0), (100, 0.0), (255, 60.0)))
        self.assertAlmostEqual(cal.value(50), -30.0, places=3)
        self.assertAlmostEqual(cal.value(100), 0.0, places=3)

    def test_clamping(self):
        cal = yp.MeterCal(points=((0, -60.0), (255, 60.0)))
        self.assertEqual(cal.value(-5), -60.0)
        self.assertEqual(cal.value(300), 60.0)

    def test_s_unit_mapping(self):
        cal = yp.MeterCal(points=((0, -54.0), (130, 0.0), (255, 60.0)))
        # S9 = 0 dBm by the family convention (-54 dBm = S0).
        self.assertEqual(cal.s_unit(130), "S9+0")
        self.assertTrue(cal.s_unit(200).startswith("S9+"))
        self.assertTrue(cal.s_unit(60).startswith("S"))
```

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_yaesu_profiles -v`
预期：FAIL — `ModuleNotFoundError: No module named 'backends.yaesu'`

- [x] **步骤 3：编写最少实现代码**

`backends/yaesu/__init__.py`:

```python
"""Yaesu ASCII-CAT backend family (FTDX10, FTDX101D, FTDX101MP, FTX-1F).

Profile-driven core: every model difference lives in
``backends.yaesu.yaesu_profiles``.  The hardware-verified FT-710 keeps its
own, older code path in ``backends/ft710/`` (spec 2026-09-12 §2 D5/D6).
"""
from backends.yaesu.backend import (  # noqa: F401  re-exported for the factory
    FTX1Backend,
    FTDX10Backend,
    FTDX101DBackend,
    FTDX101MPBackend,
    YaesuBackend,
)
from backends.yaesu.yaesu_profiles import (  # noqa: F401
    PROFILES,
    MeterCal,
    YaesuModelProfile,
    get_profile,
    known_models,
)

__all__ = ["YaesuBackend", "FTDX10Backend", "FTDX101DBackend",
           "FTDX101MPBackend", "FTX1Backend", "YaesuModelProfile",
           "MeterCal", "PROFILES", "get_profile", "known_models"]
```

`backends/yaesu/yaesu_profiles.py`:

```python
"""Per-model Yaesu facts (spec 2026-09-12 §5).

Single source of truth for everything that differs between the ASCII-CAT
Yaesu radios this project supports.  Nothing here performs I/O and nothing
here talks to a radio: the tables are data, and every table records the
offline source it was transcribed from in ``YaesuModelProfile.provenance``.

None of the four models has been verified against hardware (spec §2 D2).
Fields that no offline source could supply are left empty or carry a
``TODO(hw-verify)`` marker in ``provenance`` and are never presented as
verified: ``id_answer`` is empty (log-only), meter curves ship in
``unverified_meters``, and ``tx_gated`` keeps transmit off until the
operator sets ``MRRC_ALLOW_UNVERIFIED_TX=1``.
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
    s_meter_cal: MeterCal = field(default_factory=lambda: MeterCal(points=((0, -54.0), (255, 60.0))))

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
    provenance=_provenance({"s_meter_cal":
                            "Hamlib 4.7.2 rigs/yaesu/newcat.c:339 yaesu_default_str_cal"
                            " (FTDX10 defines no curve of its own)"}),
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
```

- [x] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest tests.test_yaesu_profiles -v`
预期：PASS（约 20 个测试）。LSP 与 `interpreter-check` 无阻塞：`_YN` 之外没有引用未定义的符号。

- [x] **步骤 5：Commit**

```bash
git add backends/yaesu/yaesu_profiles.py backends/yaesu/__init__.py tests/test_yaesu_profiles.py
git commit -m "feat(yaesu): per-model profiles for FTDX10/FTDX101D/MP/FTX-1F

Data-only module: mode code tables, filter widths, bands, attenuator and
preamp steps, power format, S-meter curves and the unverified-model flags,
each table carrying the offline source it came from (Hamlib 4.7.2 rigs and
the FTX-1 CAT reference). No I/O, no hardware claims."
```

---

### 任务 2：Transport core `YaesuCatController`

**文件：**

- 创建：`backends/yaesu/cat_core.py`
- 创建：`tests/test_yaesu_cat_core.py`

This task ports the transport half of `backends/ft710/cat_controller.py` — the part that is
model-independent and field-proven — and parameterises it. Read the source before porting:
`_write` (lines 205–217), `_read_until` (219–276), `send_command` (280–351),
`send_set_command` (353–376), `send_priority_set_command` (378–413), `query`/`set` (415–430),
`_is_device_gone`/`_is_device_fatal` (76–110), `connect`/`disconnect`/`_cleanup`
(120–201), `reconnect_loop` (880–915).

- [x] **步骤 1：编写失败的测试**

`tests/test_yaesu_cat_core.py`:

```python
"""Transport tests for the shared Yaesu core (spec 2026-09-12 §4.2).

No serial hardware: `_FakeSerial` mimics the pyserial surface the core uses
(`is_open`, `in_waiting`, `read`, `write`, `flush`, `reset_input_buffer`) and
serves scripted responses, so framing, prefix skipping, priority preemption
and the error classification are all exercised deterministically.
"""
import asyncio
import errno
import serial
import unittest
from unittest.mock import patch

from backends.yaesu.cat_core import YaesuCatController
from backends.yaesu.yaesu_profiles import get_profile


class _FakeSerial:
    """Minimal pyserial stand-in with scripted answers."""

    def __init__(self, responses=(), fail_on_write=None):
        self._rx = bytearray()
        for r in responses:
            self._rx.extend(r.encode("ascii"))
        self.writes = []
        self.is_open = True
        self.fail_on_write = fail_on_write

    @property
    def in_waiting(self):
        return len(self._rx)

    def read(self, n):
        chunk = bytes(self._rx[:n])
        del self._rx[:n]
        return chunk

    def write(self, data):
        if self.fail_on_write is not None:
            raise self.fail_on_write
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def close(self):
        self.is_open = False


def _controller(responses=(), model="ftdx10", **kwargs):
    ctrl = YaesuCatController("/dev/null", baudrate=38400,
                              profile=get_profile(model), **kwargs)
    ctrl._ser = _FakeSerial(responses)
    ctrl._connected = True
    return ctrl


class FramingTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_returns_answer_without_terminator(self):
        ctrl = _controller(["FA014074000;"])
        self.assertEqual(await ctrl.query("FA"), "FA014074000")
        self.assertEqual(ctrl._ser.writes, [b"FA;"])   # ';' appended here

    async def test_query_skips_ai_frames_ahead_of_the_answer(self):
        """Auto-Information frames must not be mistaken for the answer."""
        ctrl = _controller(["FA014074000;",       # stale AI frame
                            "FA014100000;"])      # real answer
        self.assertEqual(await ctrl.query("FA"), "FA014100000")

    async def test_subsequent_message_stays_in_the_buffer(self):
        ctrl = _controller(["FA014074000;TX0;"])
        self.assertEqual(await ctrl.query("FA"), "FA014074000")
        self.assertEqual(await ctrl.query("TX"), "TX0")

    async def test_timeout_returns_none_after_the_configured_budget(self):
        ctrl = _controller([])
        ctrl._timeout = 0.05
        self.assertIsNone(await ctrl.query("FA", timeout=0.05))


class SetCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_is_write_only(self):
        ctrl = _controller(["FA014074000;"])     # answer present, must be ignored
        self.assertTrue(await ctrl.set("FA014074000"))
        self.assertEqual(ctrl._ser.writes, [b"FA014074000;"])

    async def test_priority_set_preempts_a_pending_poll(self):
        ctrl = _controller(["FA014074000;"])
        ctrl._timeout = 0.2
        poll = asyncio.create_task(ctrl.query("FA"))
        await asyncio.sleep(0.01)
        self.assertTrue(await ctrl.send_priority_set_command("TX1"))
        await poll                                    # poll aborts, no hang
        self.assertIn(b"TX1;", ctrl._ser.writes)
        self.assertFalse(ctrl._cancel_polls.is_set())  # cleared in finally


class ErrorClassificationTests(unittest.TestCase):
    def test_enxio_is_device_gone(self):
        self.assertTrue(YaesuCatController._is_device_gone(
            OSError(errno.ENXIO, "Device not configured")))
        self.assertTrue(YaesuCatController._is_device_gone(
            OSError(errno.ENOENT, "No such file or directory")))

    def test_protocol_error_is_not_device_gone(self):
        self.assertFalse(YaesuCatController._is_device_gone(ValueError("bad frame")))

    def test_serial_timeout_is_not_fatal(self):
        """A marginal write under contention must not latch 'disconnected'."""
        self.assertFalse(YaesuCatController._is_device_fatal(
            serial.SerialTimeoutException("write timeout")))

    def test_serial_exception_is_fatal(self):
        self.assertTrue(YaesuCatController._is_device_fatal(
            serial.SerialException("port not open")))


class WriteFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_fatal_write_clears_connectivity(self):
        ctrl = _controller([], )
        ctrl._ser = _FakeSerial([], fail_on_write=serial.SerialException("gone"))
        self.assertFalse(await ctrl.set("FA014074000"))
        self.assertFalse(ctrl._connected)

    async def test_timeout_write_keeps_connectivity(self):
        ctrl = _controller([])
        ctrl._ser = _FakeSerial([], fail_on_write=serial.SerialTimeoutException("busy"))
        self.assertFalse(await ctrl.set("FA014074000"))
        self.assertTrue(ctrl._connected)
```

- [x] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_yaesu_cat_core -v`
预期：FAIL — `ModuleNotFoundError: No module named 'backends.yaesu.cat_core'`

- [x] **步骤 3：编写最少实现代码**

`backends/yaesu/cat_core.py` — port the transport methods verbatim from the FT-710 controller,
with these four deliberate differences: (1) the constructor takes a `YaesuModelProfile`, so
`self._timeout`, the inter-command delay and the baud default come from the profile/model
rather than constants; (2) `_write`'s 20 ms settle delay is a named module constant
(`COMMAND_SETTLE_SECONDS`) because the FTX-1 is the model whose CAT firmware is newest; (3) no
FT-710-only helpers (`EX` items, FT-710 meter selection) are carried over; (4) the model ID
query is `ID;` (see 任务 3).

```python
"""Shared Yaesu ASCII-CAT transport (spec 2026-09-12 §4.2).

The transport half of this file is a deliberate port of the field-proven
`backends/ft710/cat_controller.py` logic (framing, prefix filtering, priority
preemption, ENXIO classification, reconnect); everything model-specific is
read from the `YaesuModelProfile` passed to the constructor.  The FT-710
keeps its own copy: this core must not change the radio that is in daily use
until the phase-3 migration has been A/B tested against hardware.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import time
from typing import Optional

import serial

from backends.yaesu.yaesu_profiles import YaesuModelProfile

logger = logging.getLogger(__name__)

# The FT-710 CAT processor needs ~20 ms between commands (matches Hamlib);
# the newer FTX-1 firmware is no slower, and the FT-710 value is the one
# proven against real hardware.
COMMAND_SETTLE_SECONDS = 0.02
DEFAULT_TIMEOUT = 1.0


class YaesuCatController:
    """Serial CAT transport for the Yaesu ASCII protocol (`CMD;`)."""

    def __init__(self, port: str, baudrate: Optional[int] = None,
                 profile: Optional[YaesuModelProfile] = None):
        if profile is None:
            raise ValueError("YaesuCatController requires a model profile")
        self._profile = profile
        self.port = port
        self.baudrate = baudrate or profile.default_baud
        self._ser: Optional[serial.Serial] = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._timeout = DEFAULT_TIMEOUT
        # Set by priority commands (PTT/TUNE) to make in-flight poll reads
        # release the serial lock immediately.
        self._cancel_polls = asyncio.Event()

    # ── Error classification (ported verbatim; see FT-710 field notes) ──

    @staticmethod
    def _is_device_gone(exc: Exception) -> bool:
        """True when the USB serial bridge itself vanished (ENXIO/ENODEV/ENOENT)."""
        errno_value = getattr(exc, "errno", None)
        if errno_value in (errno.ENXIO, errno.ENODEV, errno.ENOENT):
            return True
        text = str(exc).lower()
        return ("device not configured" in text
                or "no such file or directory" in text)

    @staticmethod
    def _is_device_fatal(exc: Exception) -> bool:
        """True only for device-level failures; False for transient I/O.

        A write can hit SerialTimeoutException under command contention
        while the device is fine; latching 'disconnected' there makes the UI
        flash 'radio not connected' (field finding 2026-08-15).
        """
        if isinstance(exc, serial.SerialTimeoutException):
            return False
        if isinstance(exc, serial.SerialException):
            return True
        if isinstance(exc, OSError):
            return exc.errno in (6, 19)
        return True

    # ── Low-level I/O ───────────────────────────────────────────────

    async def _write(self, data: bytes):
        """Write bytes to the serial port (threaded), then settle."""
        if self._ser is None or not self._ser.is_open:
            raise serial.SerialException("Port not open")

        def _w():
            self._ser.reset_input_buffer()      # Clear stale input
            self._ser.write(data)
            self._ser.flush()

        await asyncio.to_thread(_w)
        await asyncio.sleep(COMMAND_SETTLE_SECONDS)

    async def _read_until(self, terminator: bytes = b";",
                          expected_prefix: str = "",
                          timeout: Optional[float] = None) -> Optional[bytes]:
        """Read until a message matching ``expected_prefix`` is complete.

        Messages that do not start with the prefix are Auto-Information (AI)
        frames — or stale answers to an aborted poll — and are discarded so
        they cannot be mistaken for the answer; parsing continues in whatever
        bytes remain buffered.
        """
        if self._ser is None or not self._ser.is_open:
            raise serial.SerialException("Port not open")

        def _r():
            buf = bytearray()
            deadline = time.monotonic() + (timeout if timeout is not None else self._timeout)
            while time.monotonic() < deadline:
                # A priority command is waiting for the lock: bail out early.
                if self._cancel_polls.is_set():
                    return None
                waiting = self._ser.in_waiting
                if waiting > 0:
                    buf.extend(self._ser.read(waiting))
                    if terminator in buf:
                        idx = buf.find(terminator)
                        msg = bytes(buf[:idx + len(terminator)])
                        if expected_prefix:
                            text = msg.decode("ascii", errors="replace").rstrip(";")
                            if not text.startswith(expected_prefix):
                                logger.debug("Skipping unexpected response %r (expected %r)",
                                             text[:40], expected_prefix)
                                buf = bytearray(buf[idx + len(terminator):])
                                continue
                        return msg
                time.sleep(0.01)
            return bytes(buf) if buf else None

        return await asyncio.to_thread(_r)

    # ── Command interface ───────────────────────────────────────────

    async def send_command(self, cmd: str, timeout: Optional[float] = None) -> Optional[str]:
        """Send ``cmd;`` and return the answer without its terminator.

        Serialised through `self._lock`; the command itself is used as the
        expected answer prefix, which is what makes AI frames harmless.
        """
        if self._cancel_polls.is_set():
            return None

        await self._lock.acquire()
        try:
            if self._cancel_polls.is_set():
                return None
            if not self._connected or self._ser is None:
                return None

            try:
                await self._write((cmd + ";").encode("ascii"))
            except Exception as exc:
                logger.error("Serial write error for '%s': %s", cmd, exc)
                if self._is_device_fatal(exc):
                    self._connected = False
                return None

            try:
                raw = await self._read_until(b";", expected_prefix=cmd, timeout=timeout)
            except Exception as exc:
                logger.error("Serial read error for '%s': %s", cmd, exc)
                if self._is_device_fatal(exc):
                    self._connected = False
                return None

            if raw is None:
                logger.debug("Command timeout (no data): %s", cmd)
                return None
            text = raw.decode("ascii", errors="replace").rstrip(";")
            return text or None
        finally:
            self._lock.release()

    async def send_set_command(self, cmd: str) -> bool:
        """Write-only set command (fire and forget).

        Set commands on these radios often produce no answer (or a late one);
        waiting would hold the serial lock for the whole timeout on every UI
        action and make tuning stutter.
        """
        async with self._lock:
            if not self._connected or self._ser is None:
                return False
            try:
                await self._write((cmd + ";").encode("ascii"))
                return True
            except Exception as exc:
                logger.error("Serial write error for '%s': %s", cmd, exc)
                if self._is_device_fatal(exc):
                    self._connected = False
                return False

    async def send_priority_set_command(self, cmd: str) -> bool:
        """Latency-critical set command (PTT/TUNE) that preempts poll reads."""
        self._cancel_polls.set()
        try:
            # Let an in-flight _read_until thread observe the flag.
            await asyncio.sleep(0.005)
            async with self._lock:
                self._cancel_polls.clear()
                if not self._connected or self._ser is None:
                    return False
                try:
                    await self._write((cmd + ";").encode("ascii"))
                    return True
                except Exception as exc:
                    logger.error("Serial write error for '%s': %s", cmd, exc)
                    if self._is_device_fatal(exc):
                        self._connected = False
                    return False
        finally:
            self._cancel_polls.clear()

    async def query(self, cmd: str, timeout: Optional[float] = None) -> Optional[str]:
        """Query form (prefix + ';'), e.g. ``query("FA")`` -> ``"FA014074000"``."""
        return await self.send_command(cmd, timeout=timeout)

    async def set(self, cmd: str) -> bool:
        """Set form (prefix + value), e.g. ``set("FA014074000")``."""
        return await self.send_set_command(cmd)

    # ── Connection management ───────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def model(self) -> str:
        return self._profile.model_key

    async def connect(self) -> bool:
        """Open the serial port.  Ported from the FT-710 controller.

        The retry/lock rhythm is copied rather than reinvented because it is
        the version that survives USB re-enumeration on real hardware.
        """
        if self._connected and self._ser is not None and self._ser.is_open:
            return True
        try:
            self._ser = await asyncio.to_thread(
                serial.Serial, self.port, self.baudrate,
                bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE, timeout=0.1)
            self._connected = True
            logger.info("%s: CAT port %s open at %d baud",
                        self._profile.display_name, self.port, self.baudrate)
            return True
        except Exception as exc:
            self._connected = False
            self._ser = None
            if self._is_device_gone(exc):
                logger.warning("%s: CAT port %s is gone (%s) — check the USB "
                               "cable/hub or the port name",
                               self._profile.display_name, self.port, exc)
            else:
                logger.error("%s: cannot open CAT port %s: %s",
                             self._profile.display_name, self.port, exc)
            return False

    async def disconnect(self) -> None:
        self._connected = False
        if self._ser is not None:
            try:
                await asyncio.to_thread(self._ser.close)
            except Exception:
                pass
            self._ser = None

    async def reconnect_loop(self) -> bool:
        """Reconnect with the FT-710 rhythm (1 s, capped at 10 s).

        Returns True once the port is open again.  Callers (the server
        watchdog) re-run `init_scope()`/state sync afterwards; this method
        only restores the transport.
        """
        delay = 1.0
        while True:
            if await self.connect():
                logger.info("%s: CAT link restored on %s",
                            self._profile.display_name, self.port)
                return True
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)
            logger.debug("%s: CAT reconnect retry in %.0fs",
                         self._profile.display_name, delay)
```

- [x] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest tests.test_yaesu_cat_core -v`
预期：PASS（12 个测试）。

- [x] **步骤 5：Commit**

```bash
git add backends/yaesu/cat_core.py tests/test_yaesu_cat_core.py
git commit -m "feat(yaesu): shared ASCII-CAT transport core

Ports the field-proven transport logic from the FT-710 controller (framing,
AI-frame filtering, write-only sets, priority preemption for PTT/TUNE, ENXIO
vs transient-failure classification, reconnect cadence) behind a profile
parameter, so the FT-710 keeps its verified path untouched. Hardware-free
tests drive it through a scripted fake serial port."
```

---

### 任务 3：Profile-driven command layer

**文件：**

- 修改：`backends/yaesu/cat_core.py`（在 `YaesuCatController` 里增加命令方法）
- 修改：`tests/test_yaesu_cat_core.py`（追加测试类）

Evidence that fixes the three shapes this task implements:

- **Leave memory mode**: Hamlib `ftx1/ftx1.c:881-899` `ftx1_ensure_vfo_mode()` sends **`VM000;`**
  (Yaesu `VM` = VFO/memory select) when the radio is in memory mode, because a memory-mode
  `MD` set on MAIN is accepted but does not persist (`ftx1/ftx1_mode.c` header).
- **Filter widths are slot indexes**: `server.py:1510-1525` forwards the UI's `filter` value
  straight to `cat.set_filter_width(idx)` and the FT-710 implements it as `SH00{index:02d}`
  (`cat_controller.py:560-565`); the UI picks a slot from `filter_tables()`'s `(index, Hz)`
  pairs (`ft710_ui.js:1320 sendCommand('filter', nextIdx)`).
- **Power**: the family set format is `PC%03d` (Hamlib `newcat.c:4119`); the FTX-1 answers a
  `PC` read in one of two shapes depending on the head/amplifier configuration
  (`ftx1/ftx1_readme.txt`).

- [ ] **步骤 1：编写失败的测试**

Append to `tests/test_yaesu_cat_core.py`:

```python
class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_id_is_reported_verbatim(self):
        ctrl = _controller(["ID0840;"])
        self.assertEqual(await ctrl.get_model_id(), "0840")

    async def test_model_id_none_when_silent(self):
        ctrl = _controller([])
        ctrl._timeout = 0.05
        self.assertIsNone(await ctrl.get_model_id(timeout=0.05))


class FrequencyAndVfoTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_frequency_uses_fa_for_a_and_fb_for_b(self):
        ctrl = _controller([])
        await ctrl.set_frequency(7_074_000, vfo="A")
        await ctrl.set_frequency(7_074_000, vfo="B")
        self.assertEqual(ctrl._ser.writes, [b"FA007074000;", b"FB007074000;"])

    async def test_get_frequency_parses_nine_digit_hz(self):
        ctrl = _controller(["FA007074000;"])
        self.assertEqual(await ctrl.get_frequency("A"), 7_074_000)

    async def test_get_active_vfo_reads_vs(self):
        for answer, expected in (("VS0;", "A"), ("VS1;", "B")):
            ctrl = _controller([answer])
            self.assertEqual(await ctrl.get_active_vfo(), expected)


class ModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_mode_uses_the_profile_register(self):
        """The register is an int (RadioBackend.set_mode(mode_num) contract)."""
        ctrl = _controller([])
        await ctrl.set_mode(0xC)                     # FTDX10 DATA-U
        self.assertEqual(ctrl._ser.writes, [b"MD0C;"])

    async def test_set_mode_round_trip_for_c4fm(self):
        ctrl = _controller([])
        await ctrl.set_mode(0xE)
        self.assertEqual(ctrl._ser.writes, [b"MD0E;"])

    async def test_ftx1_c4fm_codes_are_not_hex_formatted(self):
        """0x11 must go out as 'I', never as "MD011" (review finding)."""
        ctrl = _controller([], model="ftx1")
        await ctrl.set_mode(0x11)
        self.assertEqual(ctrl._ser.writes, [b"MD0I;"])

    async def test_unknown_register_is_refused_without_a_write(self):
        ctrl = _controller([])
        self.assertFalse(await ctrl.set_mode(0x99))
        self.assertEqual(ctrl._ser.writes, [])

    async def test_ftx1_leaves_memory_mode_before_setting_mode(self):
        """A memory-mode MD set does not persist (ftx1_mode.c)."""
        ctrl = _controller(["VM1;", "MD02;"])       # radio is in memory mode
        self.assertTrue(await ctrl.set_mode(0x2))
        self.assertEqual(ctrl._ser.writes, [b"VM;", b"VM000;", b"MD02;"])

    async def test_ftx1_skips_the_leave_step_when_already_in_vfo_mode(self):
        ctrl = _controller(["VM0;"])
        await ctrl.set_mode(0x2)
        self.assertEqual(ctrl._ser.writes, [b"VM;", b"MD02;"])

    async def test_ftdx10_does_not_query_memory_mode(self):
        ctrl = _controller([])
        await ctrl.set_mode(0xC)
        self.assertEqual(ctrl._ser.writes, [b"MD0C;"])

    async def test_get_mode_reads_the_register_after_md0(self):
        ctrl = _controller(["MD0C;"])
        self.assertEqual(await ctrl.get_mode(), 0xC)

    async def test_ftx1_get_mode_maps_i_back_to_0x11(self):
        """The reverse lookup must handle the non-hex characters too."""
        ctrl = _controller(["MD0I;"], model="ftx1")
        self.assertEqual(await ctrl.get_mode(), 0x11)

    async def test_unknown_mode_character_is_not_guessed(self):
        ctrl = _controller(["MD0Z;"])
        self.assertIsNone(await ctrl.get_mode())


class FilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_filter_width_uses_the_two_digit_slot(self):
        ctrl = _controller([])
        await ctrl.set_filter_width(3)
        self.assertEqual(ctrl._ser.writes, [b"SH0003;"])

    async def test_get_filter_width_reads_the_slot(self):
        ctrl = _controller(["SH0003;"])
        self.assertEqual(await ctrl.get_filter_width(), 3)


class TransmitTests(unittest.IsolatedAsyncioTestCase):
    async def test_ptt_uses_the_priority_path(self):
        ctrl = _controller([])
        self.assertTrue(await ctrl.set_ptt(True))
        self.assertEqual(ctrl._ser.writes, [b"TX1;"])
        await ctrl.set_ptt(False)
        self.assertEqual(ctrl._ser.writes[-1], b"TX0;")

    async def test_tune_is_tx2(self):
        ctrl = _controller([])
        await ctrl.set_tune(True)
        self.assertEqual(ctrl._ser.writes, [b"TX2;"])

    async def test_get_ptt(self):
        ctrl = _controller(["TX1;"])
        self.assertEqual(await ctrl.get_ptt(), 1)


class MeterAndGainTests(unittest.IsolatedAsyncioTestCase):
    async def test_s_meter_reads_sm0(self):
        ctrl = _controller(["SM0123;"])
        self.assertEqual(await ctrl.get_s_meter(), 123)

    async def test_get_meter_uses_the_first_three_digits(self):
        """RM answers are 'RM' + meter + 6 digits; the raw value is the first 3."""
        ctrl = _controller(["RM5150000;"])
        self.assertEqual(await ctrl.get_meter("RM5"), 150)

    async def test_gain_commands(self):
        ctrl = _controller([])
        await ctrl.set_af_gain(120)
        await ctrl.set_rf_gain(80)
        await ctrl.set_squelch(15)
        await ctrl.set_mic_gain(50)
        self.assertEqual(ctrl._ser.writes, [b"AG0120;", b"RG0080;",
                                            b"SQ0015;", b"MG0050;"])


class PowerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_models_send_three_digit_watts(self):
        ctrl = _controller([], model="ftdx10")
        await ctrl.set_rf_power(75)
        self.assertEqual(ctrl._ser.writes, [b"PC075;"])

    async def test_ftx1_detects_the_100w_configuration(self):
        """PC2xxx answer = SPA-1/Optima (5-100 W)."""
        ctrl = _controller(["PC2100;"], model="ftx1")
        self.assertEqual(await ctrl.detect_power_config(), ("PC2", 100))
        self.assertEqual(await ctrl.effective_power_max(), 100)

    async def test_ftx1_detects_the_field_head_configuration(self):
        """PC1xxx answer = Field head (6 W on battery, 10 W on 12 V)."""
        ctrl = _controller(["PC1010;"], model="ftx1")
        self.assertEqual(await ctrl.detect_power_config(), ("PC1", 10))
        self.assertEqual(await ctrl.effective_power_max(), 10)

    async def test_ftx1_clamps_a_request_above_the_detected_maximum(self):
        ctrl = _controller(["PC1010;"])
        await ctrl.set_rf_power(100)
        self.assertEqual(ctrl._ser.writes[-1], b"PC010;")   # clamped to 10 W

    async def test_ftx1_unknown_configuration_falls_back_to_the_profile_max(self):
        ctrl = _controller([])                              # no PC answer
        ctrl._timeout = 0.05
        await ctrl.detect_power_config()
        self.assertEqual(await ctrl.effective_power_max(), 100)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_yaesu_cat_core -v`
预期：FAIL — `AttributeError: 'YaesuCatController' object has no attribute 'get_model_id'`

- [ ] **步骤 3：编写实现代码**

Append to `backends/yaesu/cat_core.py` (inside `YaesuCatController`):

```python
    # ── Identity ────────────────────────────────────────────────────

    async def get_model_id(self, timeout: Optional[float] = None) -> Optional[str]:
        """Read the model ID (`ID;`), e.g. "0840" on every FTX-1 configuration.

        Read-only: the caller logs the observed value and only warns when the
        profile records an expectation (spec §6.2).  Never a hard failure.
        """
        resp = await self.query("ID", timeout=timeout)
        if resp and len(resp) > 2:
            return resp[2:]
        return None

    # ── Frequency / VFO ─────────────────────────────────────────────

    async def set_frequency(self, freq_hz: int, vfo: str = "A") -> bool:
        prefix = "FA" if vfo.upper() == "A" else "FB"
        return await self.set(f"{prefix}{freq_hz:09d}")

    async def get_frequency(self, vfo: str = "A",
                            timeout: Optional[float] = None) -> Optional[int]:
        prefix = "FA" if vfo.upper() == "A" else "FB"
        resp = await self.query(prefix, timeout=timeout)
        if resp and len(resp) >= len(prefix) + 1:
            try:
                return int(resp[len(prefix):])
            except ValueError:
                return None
        return None

    async def get_active_vfo(self, timeout: Optional[float] = None) -> Optional[str]:
        """`VS;` -> "VS0" (VFO-A active) or "VS1" (VFO-B active)."""
        resp = await self.query("VS", timeout=timeout)
        if resp and len(resp) >= 3:
            return "B" if resp.endswith("1") else "A"
        return None

    async def set_vfo(self, vfo: str) -> bool:
        return await self.set("VS0" if vfo.upper() == "A" else "VS1")

    # ── Mode ────────────────────────────────────────────────────────

    async def _leave_memory_mode_if_needed(self) -> None:
        """FTX-1 only: exit memory mode, whose MD sets do not persist.

        Mirrors Hamlib `ftx1/ftx1.c:881-899`, which sends `VM000;` for the
        same reason (the firmware accepts a memory-mode MAIN `MD` but treats
        it as a transient tune overlay).
        """
        if not self._profile.mode_set_leaves_memory:
            return
        resp = await self.query("VM")
        if resp and resp[2:3] == "1":
            logger.debug("%s: leaving memory mode before the mode set",
                         self._profile.display_name)
            await self.set("VM000")

    async def set_mode(self, mode_num: int) -> bool:
        """Set the operating mode from its numeric register.

        ``mode_num`` is the profile register — the same int `RadioState.mode`
        and the UI carry.  The CAT character comes from `profile.mode_codes`
        rather than from `f"{mode_num:X}"`, because the FTX-1 uses H and I
        for its C4FM variants and those are not hex digits: formatting the
        register would send `MD011` instead of `MD0I`.
        """
        code = self._profile.mode_codes.get(int(mode_num))
        if code is None:
            logger.warning("%s: mode register 0x%X is not in the profile — "
                           "no MD sent", self._profile.display_name, mode_num)
            return False
        await self._leave_memory_mode_if_needed()
        return await self.set(f"MD0{code}")

    async def get_mode(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("MD0", timeout=timeout)
        if resp and len(resp) >= 4:
            char = resp[3].upper()
            for num, code in self._profile.mode_codes.items():
                if code == char:
                    return num
            logger.debug("%s: mode character %r is not in the profile",
                         self._profile.display_name, char)
        return None

    # ── Filter width (slot index, not Hz) ───────────────────────────

    async def set_filter_width(self, index: int) -> bool:
        """`SH00NN;` with the radio's own filter slot number (2 digits)."""
        return await self.set(f"SH00{index:02d}")

    async def get_filter_width(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("SH0", timeout=timeout)
        if resp and len(resp) >= 4:
            try:
                return int(resp[-2:])
            except ValueError:
                return None
        return None

    # ── Transmit (latency-critical: priority path) ──────────────────

    async def set_ptt(self, tx: bool) -> bool:
        return await self.send_priority_set_command("TX1" if tx else "TX0")

    async def set_tune(self, tune: bool) -> bool:
        if self._profile.tune_via == "atu":
            return await self.send_priority_set_command("AC1" if tune else "AC0")
        return await self.send_priority_set_command("TX2" if tune else "TX0")

    async def get_ptt(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("TX", timeout=timeout)
        if resp and len(resp) >= 3:
            try:
                return int(resp[2:])
            except ValueError:
                return None
        return None

    # ── Meters ──────────────────────────────────────────────────────

    async def get_s_meter(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("SM0", timeout=timeout)
        if resp and len(resp) >= 4:
            try:
                return int(resp[3:])
            except ValueError:
                return None
        return None

    async def get_meter(self, meter: str, timeout: Optional[float] = None) -> Optional[int]:
        """Read a raw meter value (`RM3`..`RM8`, `RM0` for the S-meter).

        The answer is "RM" + meter-id + 6 digits; the meaningful 0..255 raw
        value is the FIRST three of those six, as the FT-710 implementation
        documents (the rest is zero padding).
        """
        resp = await self.query(meter, timeout=timeout)
        if resp and len(resp) >= 6:
            try:
                return int(resp[3:6])
            except ValueError:
                return None
        return None

    # ── Gains / DSP / RF controls ───────────────────────────────────

    async def _get_int(self, cmd: str, offset: int,
                       timeout: Optional[float] = None) -> Optional[int]:
        """Read a numeric answer field starting at ``offset``.

        Shared by the polled readers: the poll tiers and
        `RadioState.from_sync_result` require PARSED values, so a raw answer
        string must never be returned from here.
        """
        resp = await self.query(cmd, timeout=timeout)
        if resp and len(resp) > offset:
            try:
                return int(resp[offset:])
            except ValueError:
                return None
        return None

    async def get_af_gain(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._get_int("AG0", 3, timeout)

    async def get_rf_gain(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._get_int("RG0", 3, timeout)

    async def get_rf_power(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._get_int("PC", 2, timeout)

    async def set_af_gain(self, value: int) -> bool:
        return await self.set(f"AG0{value:03d}")

    async def set_rf_gain(self, value: int) -> bool:
        return await self.set(f"RG0{value:03d}")

    async def set_squelch(self, value: int) -> bool:
        return await self.set(f"SQ0{value:03d}")

    async def set_mic_gain(self, value: int) -> bool:
        return await self.set(f"MG{value:03d}")

    async def set_preamp(self, value: int) -> bool:
        return await self.set(f"PA0{value}")

    async def set_attenuator(self, value: int) -> bool:
        return await self.set(f"RA0{value}")

    async def set_noise_blanker(self, on: bool) -> bool:
        return await self.set(f"NB0{'1' if on else '0'}")

    async def set_nb_level(self, level: int) -> bool:
        return await self.set(f"NL0{level:03d}")

    async def set_noise_reduction(self, on: bool) -> bool:
        return await self.set(f"NR0{'1' if on else '0'}")

    async def set_nr_level(self, level: int) -> bool:
        return await self.set(f"RL0{level:03d}")

    async def set_auto_notch(self, on: bool) -> bool:
        return await self.set(f"BC0{'1' if on else '0'}")

    async def set_compressor(self, on: bool) -> bool:
        return await self.set(f"PR0{'1' if on else '0'}")

    async def set_compressor_level(self, level: int) -> bool:
        return await self.set(f"PL0{level:03d}")

    async def set_agc(self, value: int) -> bool:
        return await self.set(f"GT0{value}")

    async def get_agc(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("GT0", timeout=timeout)
        return int(resp[3]) if resp and len(resp) >= 4 and resp[3].isdigit() else None

    async def set_vox(self, on: bool) -> bool:
        return await self.set(f"VX0{'1' if on else '0'}")

    async def set_break_in(self, on: bool) -> bool:
        return await self.set(f"BI0{'1' if on else '0'}")

    async def set_key_speed(self, speed: int) -> bool:
        return await self.set(f"KS{speed:03d}")

    async def set_cw_pitch(self, pitch: int) -> bool:
        return await self.set(f"KP{pitch:03d}")

    async def set_rit(self, on: bool) -> bool:
        return await self.set(f"RT0{'1' if on else '0'}")

    async def set_rit_freq(self, value: int) -> bool:
        sign = "+" if value >= 0 else "-"
        return await self.set(f"RU{sign}{abs(value):04d}")

    async def set_xit(self, on: bool) -> bool:
        return await self.set(f"XT0{'1' if on else '0'}")

    async def set_split(self, on: bool) -> bool:
        return await self.set(f"ST0{'1' if on else '0'}")

    async def set_power(self, on: bool) -> bool:
        return await self.set(f"PS{'1' if on else '0'}")

    async def set_antenna(self, ant: int) -> bool:
        return await self.set(f"AN{ant}")

    async def get_antenna(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("AN", timeout=timeout)
        return int(resp[2]) if resp and len(resp) >= 3 and resp[2].isdigit() else None

    async def set_tuner(self, value: int) -> bool:
        """0 = tuner off, 1 = tune cycle (`AC` = antenna tuner control)."""
        return await self.set(f"AC{value}")

    async def set_amc_level(self, level: int) -> bool:
        return await self.set(f"AO0{level:03d}")

    async def get_amc_level(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("AO0", timeout=timeout)
        if resp and len(resp) >= 4:
            try:
                return int(resp[3:])
            except ValueError:
                return None
        return None

    async def set_monitor(self, on: bool) -> bool:
        return await self.set(f"ML0{'1' if on else '0'}")

    async def set_monitor_gain(self, value: int) -> bool:
        return await self.set(f"ML0{value:03d}")

    # ── Power (family PC format; FTX-1 self-detects its configuration) ──

    async def detect_power_config(self, timeout: Optional[float] = None) -> tuple:
        """Read `PC;` and classify the FTX-1 head/amplifier configuration.

        `ftx1/ftx1_readme.txt`: Field head (battery 0.5-6 W, 12 V 0.5-10 W)
        answers in the `PC1xxx` shape, the SPA-1/Optima 100 W configuration in
        `PC2xxx`.  Models with a fixed configuration skip the probe and report
        their profile value.  TODO(hw-verify): the exact answer strings have
        not been observed on hardware yet (spec §10).
        """
        if self._profile.power_format != "auto":
            return (self._profile.power_format, self._profile.power_max_w)
        resp = await self.query("PC", timeout=timeout)
        if resp and len(resp) >= 3:
            try:
                raw = int(resp[2:])
            except ValueError:
                raw = None
            if raw is not None:
                # A four-digit answer (PC1xxx / PC2xxx) carries the class.
                if len(resp) >= 4 and resp[2] in "12" and len(resp[2:]) >= 4:
                    cfg = "PC" + resp[2]
                    self._detected_power = (cfg, 100 if cfg == "PC2" else 10)
                else:
                    self._detected_power = ("PC1", 10)
                logger.info("%s: power configuration %s (max %d W)",
                            self._profile.display_name, *self._detected_power)
                return self._detected_power
        self._detected_power = (self._profile.power_format if
                                self._profile.power_format != "auto" else "PC1",
                                self._profile.power_max_w)
        logger.info("%s: power configuration unknown, using the profile "
                    "maximum %d W", self._profile.display_name,
                    self._detected_power[1])
        return self._detected_power

    async def effective_power_max(self) -> int:
        """Maximum settable watts, from the detected (or profile) config."""
        if getattr(self, "_detected_power", None) is None:
            await self.detect_power_config()
        return self._detected_power[1]

    async def set_rf_power(self, watts: int) -> bool:
        limit = await self.effective_power_max()
        value = max(0, min(int(watts), limit))
        if value != int(watts):
            logger.debug("%s: RF power %d W clamped to the %s maximum %d W",
                         self._profile.display_name, watts,
                         self._detected_power[0], limit)
        return await self.set(f"PC{value:03d}")

    # ── Bulk state ──────────────────────────────────────────────────

    async def initial_state_sync(self) -> dict:
        """Parsed RadioState fields for the first state push.

        Contract (`RadioState.from_sync_result`): plain
        ``{RadioState field: parsed value}`` pairs.  Raw CAT answers are not
        accepted here, and the names must be the dataclass fields —
        `vfo_a_freq`, not `frequency`.
        """
        state: dict = {}
        for field, getter in (
            ("vfo_a_freq", lambda: self.get_frequency("A")),
            ("vfo_b_freq", lambda: self.get_frequency("B")),
            ("active_vfo", lambda: self.get_active_vfo()),
            ("mode", lambda: self.get_mode()),
            ("filter_width", lambda: self.get_filter_width()),
            ("tx_status", lambda: self.get_ptt()),
            ("s_meter", lambda: self.get_s_meter()),
            ("af_gain", lambda: self.get_af_gain()),
            ("rf_gain", lambda: self.get_rf_gain()),
            ("rf_power", lambda: self.get_rf_power()),
            ("preamp", lambda: self.get_preamp()),
            ("attenuator", lambda: self.get_attenuator()),
            ("agc", lambda: self.get_agc()),
        ):
            value = await getter()
            if value is not None:
                state[field] = value
        return state
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest tests.test_yaesu_cat_core -v`
预期：PASS（约 42 个测试）。

- [ ] **步骤 5：Commit**

```bash
git add backends/yaesu/cat_core.py tests/test_yaesu_cat_core.py
git commit -m "feat(yaesu): profile-driven CAT command layer

Frequency/VFO, mode (with the FTX-1 leave-memory step from Hamlib
ftx1.c:881), slot-based filter width, priority PTT/TUNE, meters, gains, DSP
and the family PC power format with FTX-1 configuration detection."
```

---

### 任务 4：`YaesuBackend` and the four model backends

**文件：**

- 创建：`backends/yaesu/backend.py`
- 创建：`tests/test_yaesu_backend.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_yaesu_backend.py`:

```python
"""Backend-surface tests for the Yaesu family (spec 2026-09-12 §4.3/§6)."""
import unittest
from unittest.mock import AsyncMock

from backends.base import RadioCapabilities
from backends.yaesu import backend as yb
from backends.yaesu.yaesu_profiles import get_profile


class CapabilityTests(unittest.TestCase):
    def test_every_model_reports_unverified_and_scope_free(self):
        for model in ("ftdx10", "ftdx101d", "ftdx101mp", "ftx1"):
            b = yb.make_backend(model)("/dev/null")
            self.assertIsInstance(b.capabilities, RadioCapabilities)
            self.assertFalse(b.capabilities.verified, model)
            self.assertTrue(b.capabilities.tx_gated, model)
            self.assertEqual(b.capabilities.scope_type, "none", model)

    def test_vd_id_meters_follow_the_profile(self):
        self.assertTrue(yb.FTDX101DBackend("/dev/null").capabilities.has_vd_id_meters)
        self.assertTrue(yb.FTDX101MPBackend("/dev/null").capabilities.has_vd_id_meters)
        self.assertFalse(yb.FTDX10Backend("/dev/null").capabilities.has_vd_id_meters)
        self.assertFalse(yb.FTX1Backend("/dev/null").capabilities.has_vd_id_meters)

    def test_capabilities_are_json_serialisable(self):
        for cls in (yb.FTDX10Backend, yb.FTDX101DBackend,
                    yb.FTDX101MPBackend, yb.FTX1Backend):
            data = cls("/dev/null").capabilities.to_dict()
            self.assertEqual(data["model_name"], cls._profile.model_key)
            self.assertIn("audio_name_hints", data)
            self.assertIsInstance(data["att_steps"], list)

    def test_ftdx101mp_reports_vd_id_meters(self):
        self.assertTrue(yb.FTDX101MPBackend("/dev/null").capabilities.has_vd_id_meters)

    def test_dual_rx_is_advertised_without_being_implemented(self):
        """Phase 2 owns dual receive; the flag is for the UI badge only."""
        self.assertTrue(yb.FTDX101DBackend("/dev/null").capabilities.dual_rx)
        self.assertFalse(yb.FTDX10Backend("/dev/null").capabilities.dual_rx)

    def test_no_scope_producer(self):
        self.assertIsNone(yb.FTDX10Backend("/dev/null").create_scope_producer())
        self.assertIsNone(yb.FTX1Backend("/dev/null").create_scope_producer())


class TableTests(unittest.TestCase):
    def test_ui_tables_come_from_the_profile(self):
        b = yb.FTX1Backend("/dev/null")
        p = get_profile("ftx1")
        self.assertEqual(b.ui_modes, list(p.ui_modes))
        self.assertEqual(b.bands, [list(x) if isinstance(x, tuple) else x
                                   for x in p.bands])
        self.assertEqual(set(b.mode_name_to_num), set(p.mode_numbers))

    def test_filter_tables_expose_index_hz_pairs(self):
        b = yb.FTDX10Backend("/dev/null")
        tables = b.filter_tables()
        self.assertIn("voice", tables)
        self.assertIn("narrow", tables)
        for idx, hz in tables["voice"]:
            self.assertIsInstance(idx, int)
            self.assertGreater(hz, 0)

    def test_state_tables_cover_every_radio_state_hook(self):
        tables = yb.FTDX101DBackend("/dev/null").state_tables()
        for key in ("mode_num_to_name", "preamp_labels", "attenuator_labels",
                    "get_band_for_frequency", "get_filter_hz", "raw_to_dbm",
                    "raw_to_s_unit", "raw_to_power"):
            self.assertIn(key, tables)
        self.assertEqual(tables["mode_num_to_name"][0x1], "LSB")
        self.assertEqual(tables["get_filter_hz"]("USB", 2), 2400)

    def test_poll_items_are_callables_of_the_expected_shape(self):
        b = yb.FTX1Backend("/dev/null")
        for field, getter in b.settings_poll_items():
            self.assertIsInstance(field, str)
            self.assertTrue(callable(getter))
        for label, field, getter in b.tx_meter_items():
            self.assertIsInstance(label, str)
            self.assertIsInstance(field, str)
            self.assertTrue(callable(getter))


class DelegationTests(unittest.TestCase):
    def test_every_abstract_method_is_implemented(self):
        """A missing delegate would leave the class abstract and unbuildable."""
        from backends.base import RadioBackend
        for name in RadioBackend.__abstractmethods__:
            self.assertTrue(hasattr(yb.YaesuBackend, name), name)

    def test_the_backend_class_is_concrete(self):
        self.assertEqual(yb.YaesuBackend.__abstractmethods__, frozenset())
        yb.FTX1Backend("/dev/null")          # TypeError if still abstract

    def test_gated_methods_are_wired_through_the_delegate(self):
        b = yb.FTDX10Backend("/dev/null")
        b._cat.set_ptt = AsyncMock(return_value=True)
        self.assertFalse(b._cat.connected)   # never touched: the gate runs first
        self.assertTrue(hasattr(yb.YaesuBackend, "set_frequency"))


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_ptt_is_refused_while_unverified(self):
        b = yb.FTDX10Backend("/dev/null")
        b._cat.set_ptt = AsyncMock(return_value=True)
        self.assertFalse(await b.set_ptt(True))
        b._cat.set_ptt.assert_not_awaited()

    async def test_tune_is_refused_while_unverified(self):
        b = yb.FTX1Backend("/dev/null")
        b._cat.set_tune = AsyncMock(return_value=True)
        self.assertFalse(await b.set_tune(True))
        b._cat.set_tune.assert_not_awaited()

    async def test_ptt_release_is_never_gated(self):
        b = yb.FTDX10Backend("/dev/null")
        b._cat.set_ptt = AsyncMock(return_value=True)
        self.assertTrue(await b.set_ptt(False))
        b._cat.set_ptt.assert_awaited_once_with(False)

    async def test_gate_warning_is_logged_once(self):
        from backends.yaesu import backend as mod
        mod._TX_GATE_WARNED = False
        with self.assertLogs("backends.yaesu.backend", level="WARNING") as cm:
            yb.FTDX10Backend("/dev/null")._tx_allowed()
            yb.FTDX10Backend("/dev/null")._tx_allowed()
        self.assertEqual(len([m for m in cm.output if "not hardware-verified" in m]), 1)

    async def test_gate_opens_with_the_environment_switch(self):
        import config
        from unittest.mock import patch
        with patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            b = yb.FTDX10Backend("/dev/null")
            self.assertTrue(b._tx_allowed())
            self.assertFalse(b.capabilities.tx_gated)


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_records_the_observed_value(self):
        b = yb.FTX1Backend("/dev/null")
        b._cat.get_model_id = AsyncMock(return_value="0840")
        with self.assertLogs("backends.yaesu.backend", level="INFO") as cm:
            await b._check_model_identity()
        self.assertIn("0840", "".join(cm.output))

    async def test_identity_mismatch_warns_but_does_not_fail(self):
        b = yb.FTX1Backend("/dev/null")
        b._cat.get_model_id = AsyncMock(return_value="0841")
        with self.assertLogs("backends.yaesu.backend", level="WARNING") as cm:
            observed = await b._check_model_identity()
        self.assertEqual(observed, "0841")
        self.assertIn("0841", "".join(cm.output))

    async def test_model_without_a_recorded_expectation_never_warns(self):
        b = yb.FTDX10Backend("/dev/null")
        b._cat.get_model_id = AsyncMock(return_value="0810")
        with self.assertLogs("backends.yaesu.backend", level="INFO") as cm:
            await b._check_model_identity()
        self.assertFalse([m for m in cm.output if m.startswith("WARNING")])
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_yaesu_backend -v`
预期：FAIL — `ModuleNotFoundError: No module named 'backends.yaesu.backend'`

- [ ] **步骤 3：编写实现代码**

`backends/yaesu/backend.py`:

```python
"""Profile-driven RadioBackend for the Yaesu ASCII-CAT family.

Structure follows `backends/ic7300/backend.py`: every table, capability and
poll item is derived from ``self._profile``, and each model class overrides
only two attributes.  Two mechanisms implement the unverified-model boundary
of spec 2026-09-12 §6: `_tx_allowed()` (PTT/TUNE refused until the operator
sets MRRC_ALLOW_UNVERIFIED_TX=1) and `_check_model_identity()` (read-only
`ID;` logging, warn-only on mismatch).
"""
from __future__ import annotations

import logging
from typing import Optional

import config

from backends.base import RadioBackend, RadioCapabilities
from backends.yaesu.cat_core import YaesuCatController
from backends.yaesu.yaesu_profiles import YaesuModelProfile, get_profile

logger = logging.getLogger(__name__)

# One warning per process: the gate is a configuration state, not a
# per-keystroke event (a log flood would bury real failures).
_TX_GATE_WARNED = False


def _log_tx_gate_once(profile: YaesuModelProfile) -> None:
    global _TX_GATE_WARNED
    if _TX_GATE_WARNED:
        return
    _TX_GATE_WARNED = True
    logger.warning(
        "%s is not hardware-verified — transmit refused. Set "
        "MRRC_ALLOW_UNVERIFIED_TX=1 and restart to enable TX after checking "
        "the radio with _diag_yaesu.py.", profile.display_name)


class YaesuBackend(RadioBackend):
    """RadioBackend for Yaesu ASCII-CAT radios (profile-driven)."""

    _profile: YaesuModelProfile = get_profile("ftdx10")
    _display_name = "Yaesu"

    def __init__(self, port: str, baud_rate: Optional[int] = None):
        self._cat = YaesuCatController(port, baud_rate, profile=self._profile)

    # ── Capabilities ────────────────────────────────────────────────

    @property
    def capabilities(self) -> RadioCapabilities:
        p = self._profile
        return RadioCapabilities(
            model_name=p.model_key,
            display_name=self._display_name,
            default_baud=p.default_baud,
            audio_rx_rate=p.audio_rx_rate,
            audio_tx_rate=p.audio_tx_rate,
            audio_name_hints=p.audio_name_hints,
            has_atu=p.has_atu,
            has_auto_notch=True,
            has_vd_id_meters=p.has_vd_id_meters,
            vfo_b_direct=p.vfo_b_direct,
            filter_model="width_table",
            att_steps=p.att_steps,
            preamp_steps=tuple(p.preamp_labels.values()),
            scope_type="none",           # no documented CAT waveform (spec §2 D4)
            scope_spans={},
            scope_speeds=(),
            tune_via=p.tune_via,
            verified=p.verified,
            tx_gated=not self._tx_allowed(),
            dual_rx=p.dual_rx,
            unverified_meters=p.unverified_meters,
            audio_gain_boost=p.audio_gain_boost,
        )

    def _tx_allowed(self) -> bool:
        if config.ALLOW_UNVERIFIED_TX:
            return True
        if not self._profile.verified:
            _log_tx_gate_once(self._profile)
            return False
        return True

    # ── UI tables (from the profile) ────────────────────────────────

    @property
    def bands(self) -> list:
        return [[label, low, high] for label, low, high in self._profile.bands]

    @property
    def ui_modes(self) -> list:
        return list(self._profile.ui_modes)

    @property
    def mode_name_to_num(self) -> dict:
        return dict(self._profile.mode_numbers)

    def filter_tables(self) -> dict:
        p = self._profile
        voice, narrow = [], []
        narrow_modes = ("CW-U", "CW-L", "RTTY-U", "RTTY-L", "DATA-U", "DATA-L")
        for mode, widths in p.filter_widths.items():
            (narrow if mode in narrow_modes else voice).extend(
                w for w in widths if w not in (narrow if mode in narrow_modes else voice))
        return {"voice": voice, "narrow": narrow,
                "narrowModes": sorted(narrow_modes)}

    def state_tables(self) -> dict:
        """Profile tables for RadioState.configure()."""
        p = self._profile
        cal = p.s_meter_cal
        return {
            "mode_num_to_name": {v: k for k, v in p.mode_numbers.items()},
            "preamp_labels": {str(k): v for k, v in p.preamp_labels.items()},
            "attenuator_labels": {i: (f"{db} dB" if db else "OFF")
                                  for i, db in enumerate(p.att_steps)},
            "get_band_for_frequency": self._band_for_frequency,
            "get_filter_hz": self._filter_hz,
            "raw_to_dbm": cal.value,
            "raw_to_s_unit": cal.s_unit,
            "raw_to_power": RAW_TO_METER_TABLES["power"](p.power_max_w),
            "raw_to_swr": RAW_TO_METER_TABLES["swr"](),
            "raw_to_voltage": RAW_TO_METER_TABLES["voltage"](),
            "raw_to_current": RAW_TO_METER_TABLES["current"](),
        }

    def _band_for_frequency(self, freq_hz: int) -> Optional[dict]:
        for label, low, high in self._profile.bands:
            if low <= freq_hz <= high:
                return {"name": label, "low": low, "high": high}
        return None

    def _filter_hz(self, mode_name: str, index: int) -> Optional[int]:
        for idx, hz in self._profile.filter_widths.get(mode_name, ()):
            if idx == index:
                return hz
        return None

    # ── Poll items ──────────────────────────────────────────────────

    def settings_poll_items(self) -> list:
        """2s-tier items; getters return PARSED RadioState values (never raw)."""
        cat = self._cat
        return [
            ("af_gain", lambda t=None: cat.get_af_gain(timeout=t)),
            ("rf_gain", lambda t=None: cat.get_rf_gain(timeout=t)),
            ("rf_power", lambda t=None: cat.get_rf_power(timeout=t)),
            ("filter_width", lambda t=None: cat.get_filter_width(timeout=t)),
            ("preamp", lambda t=None: cat.get_preamp(timeout=t)),
            ("attenuator", lambda t=None: cat.get_attenuator(timeout=t)),
            ("agc", lambda t=None: cat.get_agc(timeout=t)),
        ]

    def slow_poll_items(self) -> list:
        cat = self._cat

        async def _freq_mode(timeout=None):
            values: dict = {}
            if (f := await cat.get_frequency("A", timeout=timeout)) is not None:
                values["vfo_a_freq"] = f
            if (m := await cat.get_mode(timeout=timeout)) is not None:
                values["mode"] = m
            return values

        return [("freq_mode", _freq_mode)]

    def tx_meter_items(self) -> list:
        cat = self._cat
        return [
            ("Power", "power_meter", lambda t=None: cat.get_meter("RM5", timeout=t)),
            ("SWR", "swr_meter", lambda t=None: cat.get_meter("RM6", timeout=t)),
            ("ALC", "alc_meter", lambda t=None: cat.get_meter("RM4", timeout=t)),
            ("COMP", "comp_meter", lambda t=None: cat.get_meter("RM3", timeout=t)),
        ]

    def always_meter_items(self) -> list:
        cat = self._cat
        return [("S", "s_meter", lambda t=None: cat.get_s_meter(timeout=t))]

    # ── Connection / command surface (delegating) ───────────────────

    @property
    def connected(self) -> bool:
        return self._cat.connected

    @property
    def model(self) -> str:
        return self._profile.model_key

    async def connect(self) -> bool:
        ok = await self._cat.connect()
        if ok:
            await self._check_model_identity()
        return ok

    async def disconnect(self) -> None:
        await self._cat.disconnect()

    async def reconnect_loop(self) -> bool:
        return await self._cat.reconnect_loop()

    async def boot_verify(self, cat=None, timeout: float = 0.4) -> bool:
        """The Yaesu family answers a frequency read-back (`FA;`)."""
        return bool(await self._cat.query("FA", timeout=timeout))

    # ── Identity (spec §6.2) ────────────────────────────────────────

    async def _check_model_identity(self) -> Optional[str]:
        """Log the `ID;` answer; warn only when the profile expects another.

        Never a release: a mismatch means the profile (or the operator's
        model selection) is wrong, not that the radio must be refused.
        """
        observed = await self._cat.get_model_id()
        p = self._profile
        if observed is None:
            logger.debug("%s: no answer to ID; (optional command)", p.display_name)
            return None
        if not p.id_answer:
            logger.info("%s: model ID observed: %s (no expectation recorded "
                        "for this model yet)", p.display_name, observed)
            return observed
        if observed.upper() == p.id_answer.upper():
            logger.info("%s: model ID %s matches the profile",
                        p.display_name, observed)
        else:
            logger.warning("%s: model ID mismatch — radio answered %s, profile "
                           "expects %s. Check the selected model in the "
                           "connection dialog; control continues.",
                           p.display_name, observed, p.id_answer)
        return observed

    async def initial_state_sync(self) -> dict:
        return await self._cat.initial_state_sync()


def make_backend(model: str) -> type:
    """Return the backend class for a model key (used by tests and the factory)."""
    return _CLASSES[model]


# ── Mechanical delegates for the rest of the abstract surface ───────
#
# Design review finding (2026-09-12): `RadioBackend` is an ABC and
# `__getattr__` does NOT satisfy ABCMeta — the class would have stayed
# abstract and `FTDX10Backend("/dev/null")` would raise TypeError.  The
# delegates are therefore bound onto the class at import time, and the
# abstract set is cleared afterwards because ABCMeta froze it at class
# creation.  `test_every_abstract_method_is_implemented` keeps this honest
# if the ABC grows a method later.
_GATED_METHODS = {"set_ptt", "set_tune"}


def _make_delegate(name: str):
    async def _delegate(self, *args, **kwargs):
        if name in _GATED_METHODS and args and args[0] and not self._tx_allowed():
            return False
        if name in _GATED_METHODS and kwargs.get("tx") and not self._tx_allowed():
            return False
        return await getattr(self._cat, name)(*args, **kwargs)

    _delegate.__name__ = name
    _delegate.__qualname__ = f"YaesuBackend.{name}"
    return _delegate


# Everything the class implements itself (including the gated PTT/TUNE pair).
_EXPLICIT = {
    "capabilities", "create_scope_producer", "bands", "ui_modes",
    "mode_name_to_num", "filter_tables", "state_tables",
    "settings_poll_items", "slow_poll_items", "tx_meter_items",
    "always_meter_items", "connected", "model", "connect", "disconnect",
    "reconnect_loop", "boot_verify", "initial_state_sync",
    "_tx_allowed", "_check_model_identity",
}

for _name in sorted(RadioBackend.__abstractmethods__ - _EXPLICIT):
    setattr(YaesuBackend, _name, _make_delegate(_name))

YaesuBackend.__abstractmethods__ = frozenset()


# ── Model classes (profile + label only, like the Icom family) ──────

class FTDX10Backend(YaesuBackend):
    _profile = get_profile("ftdx10")
    _display_name = "Yaesu FTDX10"


class FTDX101DBackend(YaesuBackend):
    _profile = get_profile("ftdx101d")
    _display_name = "Yaesu FTDX101D"


class FTDX101MPBackend(YaesuBackend):
    _profile = get_profile("ftdx101mp")
    _display_name = "Yaesu FTDX101MP"


class FTX1Backend(YaesuBackend):
    _profile = get_profile("ftx1")
    _display_name = "Yaesu FTX-1F"


_CLASSES = {
    "ftdx10": FTDX10Backend,
    "ftdx101d": FTDX101DBackend,
    "ftdx101mp": FTDX101MPBackend,
    "ftx1": FTX1Backend,
}
```

Two things this task must also add, because the backend references them:

1. `backends/yaesu/cat_core.py` — `get_preamp()`/`get_attenuator()` readers used by the poll tier:

```python
    async def get_preamp(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("PA0", timeout=timeout)
        return int(resp[3]) if resp and len(resp) >= 4 and resp[3].isdigit() else None

    async def get_attenuator(self, timeout: Optional[float] = None) -> Optional[int]:
        resp = await self.query("RA0", timeout=timeout)
        return int(resp[3]) if resp and len(resp) >= 4 and resp[3].isdigit() else None
```

1. `config.py` — `ALLOW_UNVERIFIED_TX` already exists (`config.py:56`, added by the Icom work).
   Add the shared TX meter curve helpers next to it; the shapes are the FT-710 ones
   (`config_ft710.py:166-208`), and only the rated power differs per model, so the profile
   passes its own maximum. (A per-curve profile field was considered and rejected: the shapes
   are family-wide — YAGNI.)

```python
# ── Shared TX meter curves ──────────────────────────────────────────
# Same shapes as the FT-710 tables (config_ft710.py:166-208); only the
# rated power differs per model, so the profile passes its own maximum.
def _make_raw_to_power(rated_w: int):
    def _raw_to_power(raw: int) -> float:
        return round(max(0, min(raw, 255)) / 255 * rated_w, 1)
    return _raw_to_power


def _raw_to_swr(raw: int) -> float:
    return round(1.0 + max(0, min(raw, 255)) / 255 * 9.0, 2)


def _raw_to_voltage(raw: int) -> float:
    return round(max(0, min(raw, 255)) / 255 * 16.0, 1)


def _raw_to_current(raw: int) -> float:
    return round(max(0, min(raw, 255)) / 255 * 25.0, 1)


RAW_TO_METER_TABLES = {
    "power": _make_raw_to_power,
    "swr": lambda: _raw_to_swr,
    "voltage": lambda: _raw_to_voltage,
    "current": lambda: _raw_to_current,
}
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest tests.test_yaesu_backend -v`
预期：PASS（约 20 个测试）。另外运行 `.venv/bin/python -m py_compile backends/yaesu/*.py` 确认无语法错误。

- [ ] **步骤 5：Commit**

```bash
git add backends/yaesu/backend.py backends/yaesu/cat_core.py tests/test_yaesu_backend.py
git commit -m "feat(yaesu): profile-driven backend with TX gate and ID check

Capabilities, UI tables, state tables and poll items derive from the model
profile; PTT/TUNE refuse to key an unverified model until
MRRC_ALLOW_UNVERIFIED_TX=1, and the read-only ID; check logs the observed
model ID and warns (never blocks) on a mismatch."
```

---

### 任务 5：Registry, config, frontend and packaging wiring

**文件：**

- 修改：`backends/__init__.py:14-22`（注册表）
- 修改：`config.py:70-77`（波特率表）
- 修改：`static/index.html:552-559`（机型下拉）
- 修改：`packaging/pyinstaller/mrrc_modern_server.spec`
- 修改：`tests/test_backend_factory.py:235-240`
- 修改：`tests/test_config.py:230-240`
- 创建：`tests/test_yaesu_wiring.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_yaesu_wiring.py`:

```python
"""Wiring tests: registry, baud table, factory and packaging (spec §4.4)."""
import unittest

from backends import create_backend, known_models
from config import default_baud_for

YAESU_KEYS = ("ftdx10", "ftdx101d", "ftdx101mp", "ftx1")


class RegistryTests(unittest.TestCase):
    def test_keys_are_registered(self):
        for key in YAESU_KEYS:
            self.assertIn(key, known_models())

    def test_factory_returns_the_model_backend(self):
        for key in YAESU_KEYS:
            backend = create_backend(key, "/dev/null")
            self.assertEqual(backend.model, key)
            self.assertEqual(backend.capabilities.model_name, key)

    def test_ft710_is_untouched(self):
        backend = create_backend("ft710", "/dev/null")
        self.assertEqual(backend.model, "ft710")
        self.assertTrue(backend.capabilities.verified)

    def test_unknown_model_still_raises(self):
        with self.assertRaises(ValueError):
            create_backend("ftdx9999", "/dev/null")


class BaudTests(unittest.TestCase):
    def test_yaesu_models_default_to_38400(self):
        for key in YAESU_KEYS:
            self.assertEqual(default_baud_for(key), 38400)

    def test_every_registered_model_has_a_baud_entry(self):
        for key in known_models():
            self.assertIn(default_baud_for(key), (38400, 115200))
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_yaesu_wiring -v`
预期：FAIL — `test_keys_are_registered` 失败（`ftdx10` 不在 `known_models()` 中）

- [ ] **步骤 3：修改实现**

`backends/__init__.py` — extend the registry (keep the lazy-import comment):

```python
_BACKENDS = {
    "ft710": ("backends.ft710.backend", "FT710Backend"),
    "ic7300": ("backends.ic7300.backend", "IC7300Backend"),
    "ic7300mk2": ("backends.ic7300.backend", "IC7300MK2Backend"),
    "ic705": ("backends.ic7300.backend", "IC705Backend"),
    "ic7610": ("backends.ic7300.backend", "IC7610Backend"),
    "ic7760": ("backends.ic7300.backend", "IC7760Backend"),
    # Yaesu ASCII-CAT family (spec 2026-09-12): same core, four profiles.
    # Unverified models: TX gated behind MRRC_ALLOW_UNVERIFIED_TX.
    "ftdx10": ("backends.yaesu.backend", "FTDX10Backend"),
    "ftdx101d": ("backends.yaesu.backend", "FTDX101DBackend"),
    "ftdx101mp": ("backends.yaesu.backend", "FTDX101MPBackend"),
    "ftx1": ("backends.yaesu.backend", "FTX1Backend"),
}
```

`config.py` — extend `_DEFAULT_BAUD_BY_MODEL` (the dict is hand-written, not registry-derived):

```python
_DEFAULT_BAUD_BY_MODEL = {
    "ft710": 38400,
    "ic7300": 115200,
    "ic7300mk2": 115200,
    "ic705": 115200,
    "ic7610": 115200,
    "ic7760": 115200,
    # Yaesu ASCII-CAT family: Yaesu's documented default is 38400 8N1
    # (Hamlib ftx1/ftx1_readme.txt confirms the FTX-1's default).
    "ftdx10": 38400,
    "ftdx101d": 38400,
    "ftdx101mp": 38400,
    "ftx1": 38400,
}
```

`config.py` — the shared `RAW_TO_METER_TABLES` helpers were added in 任务 4 step 3 (item 2);
no change needed here beyond the baud table above.

`static/index.html` — extend the model dropdown (after the IC-7760 option):

```html
          <option value="ftdx10">Yaesu FTDX10（实验性，仅接收）</option>
          <option value="ftdx101d">Yaesu FTDX101D（实验性，仅接收）</option>
          <option value="ftdx101mp">Yaesu FTDX101MP（实验性，仅接收）</option>
          <option value="ftx1">Yaesu FTX-1F（实验性，仅接收）</option>
```

`packaging/pyinstaller/mrrc_modern_server.spec` — add the three new modules to `hiddenimports`
next to the existing `backends.*` entries:

```python
        "backends.yaesu.backend",
        "backends.yaesu.cat_core",
        "backends.yaesu.yaesu_profiles",
```

`tests/test_backend_factory.py` — the exact-tuple assertion must list eleven keys:

```python
    def test_known_models_covers_every_backend(self):
        from backends import known_models
        self.assertEqual(known_models(), (
            "ft710", "ic7300", "ic7300mk2", "ic705", "ic7610", "ic7760",
            "ftdx10", "ftdx101d", "ftdx101mp", "ftx1"))
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest tests.test_yaesu_wiring tests.test_backend_factory tests.test_config -v`
预期：PASS。然后跑全量：`.venv/bin/python -m unittest discover -s tests 2>&1 | tail -3`
预期：`OK`（909 + 新增测试）。

- [ ] **步骤 5：Commit**

```bash
git add backends/__init__.py config.py static/index.html \
        packaging/pyinstaller/mrrc_modern_server.spec \
        tests/test_yaesu_wiring.py tests/test_backend_factory.py tests/test_config.py
git commit -m "feat(yaesu): register the four models and wire config/UI/packaging

Registry keys, hand-written baud table, connection-dialog options labelled
receive-only while unverified, PyInstaller hidden imports, and the shared
TX meter curve helpers."
```

---

### 任务 6：Hardware-free fake radio (pty) and the optional Hamlib simulator peer

**文件：**

- 创建：`tests/test_yaesu_fake_radio.py`

Two independent peers. The **pty fake radio** is dependency-free and always runs; the **Hamlib
simulator** test is skipped unless a built `simftdx101` and `socat` are present, because the
simulators are not built by default (`simulators/` in the Hamlib tree contains only sources
today — verified 2026-09-12) and their intended wiring is `socat` pty pairs
(`simulators/simftdx101.c:1`: "can run this using rigctl/rigctld and socat pty devices").

- [ ] **步骤 1：编写失败的测试**

`tests/test_yaesu_fake_radio.py`:

```python
"""End-to-end CAT round trips against a fake Yaesu radio on a pty.

This is the closest thing to a live radio available to this project (spec §8):
the production `YaesuCatController` opens a real device path with pyserial and
talks to a peer that answers the documented ASCII protocol.  Nothing here is
mocked — the framing, the serial round trip and the profile tables are all
exercised together.
"""
import asyncio
import os
import pty
import shutil
import subprocess
import unittest

from backends.yaesu.cat_core import YaesuCatController
from backends.yaesu.yaesu_profiles import get_profile


class FakeYaesuRadio:
    """Minimal ASCII-CAT peer on the master side of a pty pair."""

    def __init__(self, model="ftdx10", freq=14_074_000, mode="2"):
        self.model = model
        self.freq = freq
        self.mode = mode
        self.filter_slot = 2
        self.vfo = "A"
        self.tx = 0
        self.power_w = 25
        self.commands = []
        self._master, self._slave = pty.openpty()
        self.port = os.ttyname(self._slave)
        self._task = None

    async def _serve(self):
        loop = asyncio.get_running_loop()
        buf = bytearray()
        while True:
            try:
                chunk = await loop.run_in_executor(None, os.read, self._master, 256)
            except OSError:
                return
            if not chunk:
                return
            buf.extend(chunk)
            while b";" in buf:
                idx = buf.index(b";")
                cmd = bytes(buf[:idx]).decode("ascii", "replace")
                del buf[:idx + 1]
                answer = self._handle(cmd)
                if answer:
                    os.write(self._master, answer.encode("ascii"))

    def _handle(self, cmd: str) -> str:
        self.commands.append(cmd)
        p = get_profile(self.model)
        if cmd == "ID":
            return f"ID{p.id_answer or '0000'};"
        if cmd == "FA":
            return f"FA{self.freq:09d};"
        if cmd == "FB":
            return f"FB{self.freq - 1000:09d};"
        if cmd == "VS":
            return f"VS{'0' if self.vfo == 'A' else '1'};"
        if cmd == "MD0":
            return f"MD0{self.mode};"
        if cmd.startswith("MD0"):
            self.mode = cmd[3]
            return ""
        if cmd == "SH0":
            return f"SH00{self.filter_slot:02d};"
        if cmd.startswith("SH00"):
            self.filter_slot = int(cmd[4:])
            return ""
        if cmd == "TX":
            return f"TX{self.tx};"
        if cmd.startswith("TX"):
            self.tx = int(cmd[2])
            return ""
        if cmd == "SM0":
            return "SM0128;"
        if cmd == "PC":
            return f"PC{self.power_w:03d};" if self.model != "ftx1" else f"PC2{self.power_w:03d};"
        if cmd.startswith("PC"):
            self.power_w = int(cmd[2:])
            return ""
        if cmd == "VM":
            return "VM0;"                      # never in memory mode here
        return ""

    async def __aenter__(self):
        self._task = asyncio.create_task(self._serve())
        return self

    async def __aexit__(self, *exc):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        os.close(self._master)
        os.close(self._slave)


class FakeRadioRoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.radio = await FakeYaesuRadio(model="ftdx10").__aenter__()
        self.ctrl = YaesuCatController(self.radio.port, profile=get_profile("ftdx10"))
        self.assertTrue(await self.ctrl.connect(),
                        "pyserial must be able to open the pty device path")

    async def asyncTearDown(self):
        await self.ctrl.disconnect()
        await self.radio.__aexit__(None, None, None)

    async def test_frequency_round_trip(self):
        self.assertEqual(await self.ctrl.get_frequency("A"), 14_074_000)
        self.assertTrue(await self.ctrl.set_frequency(7_050_000, vfo="A"))
        self.assertEqual(await self.ctrl.get_frequency("A"), 7_050_000)

    async def test_mode_round_trip(self):
        self.assertEqual(await self.ctrl.get_mode(), 0x2)
        await self.ctrl.set_mode(0xC)
        self.assertEqual(await self.ctrl.get_mode(), 0xC)

    async def test_filter_slot_round_trip(self):
        self.assertEqual(await self.ctrl.get_filter_width(), 2)
        await self.ctrl.set_filter_width(3)
        self.assertEqual(await self.ctrl.get_filter_width(), 3)

    async def test_ptt_round_trip(self):
        await self.ctrl.set_ptt(True)
        self.assertEqual(await self.ctrl.get_ptt(), 1)
        await self.ctrl.set_ptt(False)
        self.assertEqual(await self.ctrl.get_ptt(), 0)

    async def test_model_id(self):
        self.assertEqual(await self.ctrl.get_model_id(), "0000")   # no expectation


class FakeFTX1RoundTripTests(unittest.IsolatedAsyncioTestCase):
    async def test_ftx1_power_detection_from_the_answer_shape(self):
        async with FakeYaesuRadio(model="ftx1", freq=50_313_000) as radio:
            ctrl = YaesuCatController(radio.port, profile=get_profile("ftx1"))
            self.assertTrue(await ctrl.connect())
            self.assertEqual(await ctrl.detect_power_config(), ("PC2", 100))
            await ctrl.disconnect()

    async def test_ftx1_identity_is_the_documented_value(self):
        async with FakeYaesuRadio(model="ftx1") as radio:
            ctrl = YaesuCatController(radio.port, profile=get_profile("ftx1"))
            await ctrl.connect()
            self.assertEqual(await ctrl.get_model_id(), "0840")
            await ctrl.disconnect()


HAMLIB_SIM = os.path.expanduser("~/hamlib/Hamlib-4.7.2/simulators/simftdx101")


@unittest.skipUnless(os.access(HAMLIB_SIM, os.X_OK) and shutil.which("socat"),
                     "optional: build ~/hamlib/Hamlib-4.7.2/simulators and install socat "
                     "(see tests/README.md) to run the third-party protocol check")
class HamlibSimulatorPeerTests(unittest.IsolatedAsyncioTestCase):
    """Cross-implementation check: our core against Hamlib's FTDX101 simulator.

    The simulator emulates the radio side of the same ASCII protocol, so this
    catches table mistakes that a self-written fake peer would repeat.
    """

    async def test_frequency_and_mode_against_the_simulator(self):
        proc = subprocess.Popen(
            ["socat", "-d", "-d", "pty,raw,echo=0,link=/tmp/mrrc_sim_rig",
             "pty,raw,echo=0,link=/tmp/mrrc_sim_radio"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            await asyncio.sleep(0.5)
            sim = subprocess.Popen([HAMLIB_SIM, "/tmp/mrrc_sim_radio"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                await asyncio.sleep(0.5)
                ctrl = YaesuCatController("/tmp/mrrc_sim_rig",
                                          profile=get_profile("ftdx101d"))
                self.assertTrue(await ctrl.connect())
                # The simulator boots at 14074000 Hz in mode 0xc (DATA-U).
                self.assertEqual(await ctrl.get_frequency("A"), 14_074_000)
                self.assertEqual(await ctrl.get_mode(), 0xC)   # simulator boots in DATA-U
                await ctrl.set_frequency(21_074_000, vfo="A")
                self.assertEqual(await ctrl.get_frequency("A"), 21_074_000)
                await ctrl.disconnect()
            finally:
                sim.terminate()
        finally:
            proc.terminate()
```

- [ ] **步骤 2：运行测试验证失败/通过**

运行：`.venv/bin/python -m unittest tests.test_yaesu_fake_radio -v`
预期：pty 测试 PASS；`HamlibSimulatorPeerTests` SKIP（附注原因）。若 pty 打开失败（例如受限沙箱），测试必须在 `asyncSetUp` 内 `self.skipTest(...)` 而不是报错——把
`self.assertTrue(await self.ctrl.connect(), ...)` 换成显式检查：

```python
        if not await self.ctrl.connect():
            self.skipTest("pyserial cannot open a pty device path in this environment")
```

- [ ] **步骤 3：可选设置（写入 `tests/README.md`，非 CI 必需）**

```bash
cd ~/hamlib/Hamlib-4.7.2/simulators && make            # builds simftdx101 et al.
brew install socat                                     # macOS; apt install socat on Linux
```

- [ ] **步骤 4：Commit**

```bash
git add tests/test_yaesu_fake_radio.py tests/README.md
git commit -m "test(yaesu): pty fake-radio round trips + optional Hamlib simulator peer

The pty harness drives the production transport against a real device path
with no external dependency; the Hamlib FTDX101 simulator test is opt-in and
skipped unless the simulator is built and socat is installed."
```

---

### 任务 7：`_diag_yaesu.py` field tool

**文件：**

- 创建：`_diag_yaesu.py`
- 创建：`tests/test_diag_yaesu.py`

- [ ] **步骤 1：编写失败的测试**

`tests/test_diag_yaesu.py`:

```python
"""Tests for the read-only Yaesu field diagnostic (spec §7)."""
import unittest

import _diag_yaesu as diag


class IdentityEvaluationTests(unittest.TestCase):
    def test_match(self):
        ok, note = diag.evaluate_identity("0840", "0840", "Yaesu FTX-1F")
        self.assertTrue(ok)
        self.assertIn("matches", note)

    def test_mismatch_names_both_values(self):
        ok, note = diag.evaluate_identity("0810", "0840", "Yaesu FTX-1F")
        self.assertFalse(ok)
        self.assertIn("0810", note)
        self.assertIn("0840", note)

    def test_no_expectation_is_not_a_failure(self):
        ok, note = diag.evaluate_identity("0810", "", "Yaesu FTDX10")
        self.assertTrue(ok)
        self.assertIn("no expectation", note)

    def test_silent_radio_is_reported(self):
        ok, note = diag.evaluate_identity(None, "0840", "Yaesu FTX-1F")
        self.assertFalse(ok)
        self.assertIn("no answer", note)


class TxCheckTests(unittest.TestCase):
    def test_tx_check_requires_the_opt_in(self):
        self.assertFalse(diag.tx_check_permitted(True, False))
        self.assertFalse(diag.tx_check_permitted(False, True))
        self.assertTrue(diag.tx_check_permitted(True, True))


class ReportTests(unittest.TestCase):
    def test_report_contains_the_model_and_observed_values(self):
        report = diag.format_report(
            model="ftx1", display_name="Yaesu FTX-1F", port="/dev/ttyUSB0",
            baud=38400, model_id="0840", identity_ok=True,
            identity_note="model ID matches the profile",
            probes=[("FA", "FA14074000", True), ("MD0", "MD02", True)],
            tx_check="skipped (needs --tx-check --allow-tx)",
            notes=["audio rate assumption: TODO(hw-verify)"])
        self.assertIn("Yaesu FTX-1F", report)
        self.assertIn("0840", report)
        self.assertIn("FA14074000", report)
        self.assertIn("| command | answer | ok |", report)

    def test_report_flags_failed_probes(self):
        report = diag.format_report(
            model="ftdx10", display_name="Yaesu FTDX10", port="/dev/null",
            baud=38400, model_id=None, identity_ok=False,
            identity_note="no answer to ID;", probes=[("FA", None, False)],
            tx_check="not requested", notes=[])
        self.assertIn("FAIL", report)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`.venv/bin/python -m unittest tests.test_diag_yaesu -v`
预期：FAIL — `ModuleNotFoundError: No module named '_diag_yaesu'`

- [ ] **步骤 3：编写实现代码**

`_diag_yaesu.py` — read-only by default; the only write path is
`--tx-check --allow-tx`, which is bounded by a sub-second key-up with PTT read-back and always
releases PTT in a `finally` block. Structure mirrors `_diag_civ.py`
(`evaluate_identity` / `tx_check_permitted` / `format_report` / `run` / `build_parser`), with no
scope section because these models have no documented waveform (spec §2 D4):

```python
#!/usr/bin/env python3
"""Read-only field self-check for the Yaesu ASCII-CAT models.

Purpose (spec 2026-09-12 §7): a field owner of an FTDX10 / FTDX101D / FTDX101MP / FTX-1F
runs this against the radio and pastes the report back.  It closes the
`TODO(hw-verify)` gaps the design could not: the `ID;` answer, whether the
documented commands answer at all, and — with an explicit opt-in — whether PTT
and the TX path work.

Nothing is written to the radio unless --tx-check --allow-tx is given, and PTT
is released in a finally block even then.

Usage:
    python3 _diag_yaesu.py --model ftx1 --port /dev/ttyUSB0
    python3 _diag_yaesu.py --model ftdx10 --port COM5 --tx-check --allow-tx
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import List, Optional, Tuple

from backends.yaesu.cat_core import YaesuCatController
from backends.yaesu.yaesu_profiles import get_profile

# (command, timeout) — read-only queries only.
PROBES: Tuple[Tuple[str, float], ...] = (
    ("ID", 0.6), ("FA", 0.6), ("FB", 0.6), ("VS", 0.6), ("MD0", 0.6),
    ("SH0", 0.6), ("TX", 0.6), ("SM0", 0.6), ("PC", 0.6), ("AG0", 0.6),
    ("RG0", 0.6), ("SQ0", 0.6), ("PA0", 0.6), ("RA0", 0.6), ("GT0", 0.6),
)


def evaluate_identity(observed: Optional[str], expected: str,
                      display_name: str) -> Tuple[bool, str]:
    """Compare the observed `ID;` answer with the profile's expectation."""
    if observed is None:
        return False, f"no answer to ID; from {display_name}"
    if not expected:
        return True, (f"model ID observed: {observed} (no expectation recorded "
                      f"for {display_name} yet — please report this value)")
    if observed.upper() == expected.upper():
        return True, f"model ID matches the profile ({observed})"
    return False, (f"model ID mismatch: radio answered {observed}, profile "
                   f"expects {expected} — report this and check the selected model")


def tx_check_permitted(tx_check: bool, allow_tx: bool) -> bool:
    """A key-up needs both switches; either one alone is not enough."""
    return bool(tx_check and allow_tx)


def format_report(**kw) -> str:
    """Paste-ready Markdown report."""
    lines: List[str] = []
    ok = "PASS" if kw["identity_ok"] else "FAIL"
    lines.append(f"# Yaesu field report — {kw['display_name']} (`{kw['model']}`)")
    lines.append("")
    lines.append(f"- port: `{kw['port']}` @ {kw['baud']} baud")
    lines.append(f"- `ID;` observed: **{kw.get('model_id') or 'no answer'}** — {ok}")
    lines.append(f"- identity: {kw['identity_note']}")
    lines.append(f"- TX check: {kw['tx_check']}")
    lines.append("")
    lines.append("| command | answer | ok |")
    lines.append("| --- | --- | --- |")
    for cmd, answer, good in kw["probes"]:
        lines.append(f"| `{cmd}` | `{answer or ''}` | {'yes' if good else '**FAIL**'} |")
    if kw.get("notes"):
        lines.append("")
        lines.append("## Notes")
        for note in kw["notes"]:
            lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


async def run(args) -> int:
    profile = get_profile(args.model)
    ctrl = YaesuCatController(args.port, args.baud, profile=profile)
    notes: List[str] = []
    if not await ctrl.connect():
        print(f"cannot open {args.port}", file=sys.stderr)
        return 2
    try:
        model_id = await ctrl.get_model_id()
        identity_ok, identity_note = evaluate_identity(
            model_id, profile.id_answer, profile.display_name)

        probes: List[Tuple[str, Optional[str], bool]] = []
        for cmd, timeout in PROBES:
            answer = await ctrl.query(cmd, timeout=timeout)
            probes.append((cmd, answer, answer is not None))

        tx_check = "not requested"
        if tx_check_permitted(args.tx_check, args.allow_tx):
            try:
                await ctrl.set_ptt(True)
                await asyncio.sleep(0.2)
                readback = await ctrl.get_ptt(timeout=0.6)
                tx_check = (f"keyed 0.2 s, TX; read back {readback}"
                            if readback == 1 else
                            f"PTT did not report TX (read back {readback})")
            finally:
                await ctrl.set_ptt(False)
                await asyncio.sleep(0.1)
                released = await ctrl.get_ptt(timeout=0.6)
                tx_check += f"; released (TX; = {released})"
        elif args.tx_check:
            tx_check = "refused: --tx-check also needs --allow-tx"

        for meter in profile.unverified_meters:
            notes.append(f"{meter}: no hardware-verified curve yet — compare with "
                         f"the radio's display and report")
        notes.append("audio rate assumption is TODO(hw-verify): the design assumed "
                     "44.1 kHz for this model family")
        print(format_report(model=profile.model_key,
                            display_name=profile.display_name,
                            port=args.port, baud=ctrl.baudrate,
                            model_id=model_id, identity_ok=identity_ok,
                            identity_note=identity_note, probes=probes,
                            tx_check=tx_check, notes=notes))
        return 0 if identity_ok else 1
    finally:
        await ctrl.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", required=True,
                        choices=("ftdx10", "ftdx101d", "ftdx101mp", "ftx1"))
    parser.add_argument("--port", required=True, help="CAT serial port")
    parser.add_argument("--baud", type=int, default=None,
                        help="defaults to the model profile (38400)")
    parser.add_argument("--tx-check", action="store_true",
                        help="key the transmitter briefly (needs --allow-tx)")
    parser.add_argument("--allow-tx", action="store_true",
                        help="explicit consent for the TX check")
    return parser


def main() -> int:
    return asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **步骤 4：运行测试验证通过**

运行：`.venv/bin/python -m unittest tests.test_diag_yaesu -v`
预期：PASS（6 个测试）。再手工确认帮助文本：`.venv/bin/python _diag_yaesu.py --help`

- [ ] **步骤 5：Commit**

```bash
git add _diag_yaesu.py tests/test_diag_yaesu.py
git commit -m "feat(yaesu): read-only _diag_yaesu.py field diagnostic

Probes the commands the profiles claim, compares ID; with the recorded
expectation, and emits a paste-ready report — the instrument that closes the
TODO(hw-verify) gaps once a field owner has one of the four radios. TX check
requires --tx-check --allow-tx and always releases PTT."
```

---

### 任务 8：Documentation synchronization

**文件：**

- 修改：`README.md`、`AGENTS.md`、`DEPENDENCIES.md`、`tests/README.md`
- 修改：`SDD/02`、`SDD/05`、`SDD/08`、`SDD/09`、`SDD/10`、`SDD/11`、`SDD/12`、`SDD/13`、`SDD/14`、`SDD/15`、`SDD/README.md`

- [ ] **步骤 1：README 与 AGENTS**

`README.md` — 机型表加入四行（Yaesu 段），并写明未验证边界：

```markdown
| `ftdx10` | Yaesu FTDX10 | 38400 | ⚠️ 未验证（无真机）· 仅接收 · 无频谱 |
| `ftdx101d` | Yaesu FTDX101D | 38400 | ⚠️ 未验证（无真机）· 仅接收 · 无频谱 |
| `ftdx101mp` | Yaesu FTDX101MP | 38400 | ⚠️ 未验证（无真机）· 仅接收 · 无频谱 |
| `ftx1` | Yaesu FTX-1F | 38400 | ⚠️ 未验证（无真机）· 仅接收 · 无频谱 |
```

`AGENTS.md` — 后端表新增 `backends/yaesu/` 一行，说明"共享 ASCII 核心 + 每机型 profile；
FT-710 保持独立路径，迁移是三期"。

- [ ] **步骤 2：DEPENDENCIES 与 tests/README**

`DEPENDENCIES.md` — 四行（USB 串口 CAT、无频谱通路、UAC 音频假设 44.1 kHz 标 TODO）。
`tests/README.md` — 五个新测试模块；并记录 Hamlib 模拟器的可选设置命令（任务 6 步骤 3）。

- [ ] **步骤 3：SDD 各章**

- `SDD/02`（业务方向/机型清单）：四机型 + 未验证标注。
- `SDD/05`（NFR）：新增一条"无硬件证据的机型不得声称已验证"的一致性要求，引用本规格 §6。
- `SDD/08`（架构决策）新增 **AD-018**（profile 驱动的 Yaesu 核心；FT-710 验证路径保持独立直到三期真机 A/B）与 **AD-019**（未验证机型仅接收：TX 门禁 + 只读身份校验 + 逐表溯源）。
- `SDD/09`/`SDD/10`/`SDD/11`：`backends/yaesu/` 的组件与职责、服务边界（单接收）。
- `SDD/12`（运行模型）：首连仅接收、`MRRC_ALLOW_UNVERIFIED_TX` 开启方式、无频谱回退说明。
- `SDD/13`（可行性）新增风险：未验证 CAT 表 / 假设的音频采样率 / 频谱缺失 / FT-710 双路径维护成本（含各自缓解措施与三期计划）。
- `SDD/14` 版本历史新行；`SDD/15` 附录（机型表）；`SDD/README.md` 状态行更新。

- [ ] **步骤 4：Commit**

```bash
git add README.md AGENTS.md DEPENDENCIES.md tests/README.md SDD/
git commit -m "docs: SDD and guides for the Yaesu SDR models (AD-018/AD-019)

Documents the four unverified models, the receive-only first connection, the
shared profile-driven core, the missing spectrum path and the phased plan
(dual RX, FT-710 migration, field captures)."
```

---

### 任务 9：Final verification

- [ ] **步骤 1：全量测试与静态检查**

```bash
.venv/bin/python -m unittest discover -s tests 2>&1 | tail -4
.venv/bin/python -m py_compile backends/yaesu/*.py _diag_yaesu.py
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
```

预期：`OK`（≥ 960 tests），编译无输出，guardian 无阻塞违规。

- [ ] **步骤 2：端到端烟测（无硬件）**

```bash
.venv/bin/python - <<'PY'
from backends import create_backend, known_models
for key in ("ftdx10", "ftdx101d", "ftdx101mp", "ftx1"):
    b = create_backend(key, "/dev/null")
    caps = b.capabilities.to_dict()
    print(f"{key:10s} verified={caps['verified']} tx_gated={caps['tx_gated']} "
          f"scope={caps['scope_type']} dual_rx={caps['dual_rx']} "
          f"bands={len(b.bands)} modes={len(b.ui_modes)}")
assert "ft710" in known_models()          # the verified path is still registered
PY
```

预期输出（四行，全部 `verified=False tx_gated=True scope=none`）。

- [ ] **步骤 3：服务器启动烟测（无硬件，串口不存在）**

```bash
MRRC_RADIO_MODEL=ftx1 MRRC_SERIAL_PORT=/dev/does-not-exist MRRC_WEB_PORT=8898 \
  timeout 12 .venv/bin/python server.py 2>&1 | grep -E "Yaesu FTX-1F|not hardware-verified" | head -3
```

预期：出现 `Yaesu FTX-1F` 相关日志与一次 TX 门禁警告，进程在超时后结束（无崩溃）。

- [ ] **步骤 4：最终 commit 与推送**

```bash
git add -A && git commit -m "chore(yaesu): final wiring and verification pass

Full suite green, guardian clean, backend smoke test and a hardware-free
server boot for every new model." && git push origin main
```

---

## 自检记录（编写计划后对照规格）

**1. 规格覆盖度**

| 规格章节 | 实现任务 |
| --- | --- |
| §2 D1/D7/D8 机型、键名、FTX-1 单键 | 任务 1（profiles）、任务 5（注册表） |
| §2 D2/D9 无真机、未验证纪律 | 任务 1（`verified`/`tx_gated`）、任务 4（门禁 + 身份校验） |
| §2 D3 双接收二期 | 任务 1（`dual_rx` 仅记录）、任务 4（capabilities 透出，未实现） |
| §2 D4 频谱回退 | 任务 4（`scope_type="none"`、无 producer） |
| §2 D5/D6 架构与 FT-710 不迁移 | 任务 2–4（新包），任务 4 测试 `test_ft710_is_untouched` |
| §3 溯源 | 任务 1（`provenance` + 测试强制齐备） |
| §4.1 包结构 | 任务 1–4 |
| §4.2 传输层 | 任务 2 |
| §4.3 后端与四个子类 | 任务 4 |
| §4.4 接线 | 任务 5 |
| §5 profile 字段 | 任务 1（四组字段） |
| §6.1 TX 门禁 | 任务 4 |
| §6.2 身份校验 | 任务 4（`_check_model_identity`） |
| §6.3 降级 | 任务 4（无 scope producer、`unverified_meters`） |
| §7 诊断脚本 | 任务 7 |
| §8 测试计划 | 任务 1–6（含 pty 与模拟器） |
| §9 文档同步 | 任务 8 |
| §10 硬件验收边界 | 任务 7（诊断报告）+ 任务 8（SDD/README 同理记录） |
| §11 非目标 | 未实现（未添加 scope/双接收/SCU-LAN10/Hamlib 依赖） |
| §12 分期 | 任务 1（`dual_rx` 标志）、任务 8（SDD 记录三期计划） |

**2. 占位符扫描**：无"待定/TODO 实现"式步骤；出现的 `TODO(hw-verify)` 全部是**规格要求的数据诚实标记**（寄存器期望值、音频速率、部分衰减步进、FTX-1 滤波宽度），并有任务 7 的现场工具负责闭合。

**2b. 执行前设计审查发现（2026-09-12，执行者复核计划时提出并已内联修正）**

| # | 发现 | 影响 | 修正 |
| --- | --- | --- | --- |
| R1 | 计划用 `__getattr__` 委托 `RadioBackend` 的其余抽象方法 | `RadioBackend` 是 ABC，`__getattr__` **不满足** ABCMeta → 四个后端类仍是抽象类，实例化直接 `TypeError`，任务 4/5/6 的测试全部无法运行 | 改为在导入时把委托方法绑到类上，随后清空 `YaesuBackend.__abstractmethods__`（ABCMeta 在创建类时已冻结该集合）；新增 `test_every_abstract_method_is_implemented` 防止 ABC 以后新增方法时静默漏接 |
| R2 | 计划里 `set_mode`/`get_mode` 走 CAT **字符**（`"C"`、`"H"`） | 违反契约：`RadioBackend.set_mode(mode_num: int)`、`RadioState.mode: int`（`radio_state.py:77`）、`MODE_NUM_TO_NAME: dict[int, str]`；且 `f"MD0{mode_num:X}"` 对 FTX-1 的 `H`/`I` 会发出**无效命令 `MD011`**（H/I 不是十六进制字符） | 改为整数寄存器 + `profile.mode_codes` 显式字符查找（`set_mode(0x11)` → `MD0I`），`get_mode` 反查并拒绝未知字符；新增两条测试固定该行为 |
| R3 | 计划的 poll 项与 `initial_state_sync` 返回**原始应答字符串**，且键名用 `frequency` | 违反 `RadioState.from_sync_result` 契约（"`sync_data` is ALREADY PARSED"、键必须是 dataclass 字段名）→ 状态里会写入 `"AG0120"` 这样的字符串，`radio_state.py:263` 之类的比较全部失效 | poll 项改用解析后的 getter（新增 `get_af_gain`/`get_rf_gain`/`get_rf_power` 与 `_get_int` 共享助手），键名改为 `vfo_a_freq`/`vfo_b_freq`/`active_vfo`/`mode`/`filter_width`/`tx_status`/`s_meter`/… |

**3. 类型一致性**：`YaesuModelProfile.mode_numbers` 全程是 `dict[str, str]`（模式名 → `MD` 字符），`filter_widths` 全程是 `dict[str, tuple[tuple[int, int], ...]]`（(槽位, Hz)），`set_mode` 接受字符而 `mode_name_to_num` 提供字符，`get_mode` 返回字符（FT-710 的整数约定**不**沿用，任务 3/4 的测试固定这一点）；`initial_state_sync` 返回 `dict` 与 `RadioBackend` 契约一致；`_detected_power` 在 `detect_power_config`/`effective_power_max`/`set_rf_power` 三处同名同形。
