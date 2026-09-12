# ICOM SDR Model Support: IC-705, IC-7610, IC-7760

**Date:** 2026-09-12

**Status:** Approved for implementation

**Scope:** Add three Icom CI-V radios to the pluggable backend architecture by turning the
existing IC-7300 CI-V implementation into a profile-driven shared core. No physical unit of
any of the three new models is available for verification; the design therefore carries an
explicit, machine-readable "unverified" boundary (TX gating, capability flags, diagnostic
script) instead of implying verification it does not have.

Authoritative references (all offline, all already on the development machine):

- wfview rig data, secondary evidence only:
  `~/HAM/ref/wfview/rigs/IC-705.rig`, `IC-7610.rig`, `IC-7760.rig`, `IC-7300.rig`, `IC-7300MK2.rig`
  (same source family the existing `backends/ic7300/config_ic7300.py` already cites).
- Icom *IC-7300MK2 CI-V Reference Guide*, A7841-8EX (October 2025) — repository file
  `IC-7300MK2_ENG_CI-V_0.pdf`, via `IC-7300MK2_CI-V_Knowledge_Base.md`, for the CI-V frames
  shared by the whole generation.
- Icom *IC-7300 Full Manual* §19 for the framing/BCD/scope conventions already implemented.

No vendor CI-V Reference PDF for IC-705 / IC-7610 / IC-7760 was available offline and the
environment has no working download path, so **every field that the local rig data cannot
supply is either inherited from the verified IC-7300 tables and marked `TODO(hw-verify)`, or
omitted from the runtime mode**. Guessing is not allowed to masquerade as data.

## 1. Objective

An operator with an IC-705, IC-7610 or IC-7760 selects the model in the connection dialog (or
`MRRC_RADIO_MODEL`) and gets the same experience as today's IC-7300: CAT control, CI-V `27`
spectrum with S-meter fallback, 48 kHz USB audio RX/TX, PTT/TUNE with the existing layered
safety model — with honest capability reporting about what has and has not been verified.
One deliberate difference from an IC-7300 install: transmit on the three new models stays
disabled until the operator opts in (see §6.1), so a first connection is receive-only.

## 2. Decisions taken during design review (2026-09-12)

| # | Question | Decision |
| --- | --- | --- |
| D1 | Target models | **IC-705 + IC-7610 + IC-7760**. IC-7300/MK2 already ship; IC-9700/IC-905 are not shortwave and were excluded; the IC-7760 is the only Icom HF SDR released inside the last five years that is not already supported. |
| D2 | Hardware availability | **None.** No unit of any of the three models is available. |
| D3 | Documentation baseline | **Local wfview rig data only** (no vendor PDFs). Each profile field records its provenance; unverifiable fields are inherited-and-flagged, never invented. |
| D4 | Dual receiver | **MAIN only.** `dual_rx` is advertised as unsupported. The cmd `29` prefix is not sent (rig data shows frequency/mode are `Command29=false`, i.e. they act on the selected band, so the existing single-receiver command path is correct). |
| D5 | Release policy | **Full feature parity + explicit unverified boundary**: `verified:false`, TX gated by default behind `MRRC_ALLOW_UNVERIFIED_TX`, per-model diagnostic script, SDD §13 risk/assumption entries. |
| D6 | Architecture | **Shared CI-V core + `CivModelProfile` data table.** Package path stays `backends/ic7300/` for this change (naming debt documented); a pure rename is a separate future commit. |

## 3. Data provenance

| Field | Source | Confidence |
| --- | --- | --- |
| CI-V address (`0x94/0xB6/0xA4/0x98/0xB2`) | rig `CIVAddress` | High (factory defaults, user-changeable) |
| Transceive set-mode item (`0071/0089/0112/0112/0150`) | rig `Type=CIV Transceive` → `1a 05 <item> 00/01` | High |
| Scope bins / amplitude ceiling / segment max (`475/160/11`, `689/200/15`) | rig `SpectrumLenMax` / `SpectrumAmpMax` / `SpectrumSeqMax` | Medium — **the two 689-bin values are the least certain facts in this design**; both are measured by the diagnostic script in one run |
| Band edges and per-band rated power | rig `Bands\N\Start/End/Power` | High |
| Mode registers and per-mode filter-width ranges | rig `Modes\N\Reg/Min/Max` | High |
| Attenuator steps (`0/20` vs `0..45` in 3 dB) | rig `Attenuators\N` | High |
| Command surface (preamp `16 02`, AGC `16 12`, NB `16 22`, NR `16 40`, AN `16 41`, comp `14 0E`, filter width `1a 03`, squelch `14 03`, RF power `14 0A`, PTT `1C 00`, tune `1C 01`) | rig `Commands\N` | High — identical across all five models |
| Meter sub-codes (`15 02/11/12/13/14/15/16`) | rig `Commands\N` | High — identical across all five models |
| Scope spans (8 values) and scope sub-codes (`27 10/11/14/15/1A`) | rig `Spans`, `Commands` | High |
| **Power / Vd / Id meter calibration curves** | **Inherited from the verified IC-7300 tables, rescaled by rated power** | **Low — `TODO(hw-verify)`, listed in `unverified_meters`** |
| **USB audio sample rate (48 kHz) and device name hints** | **Inherited from the IC-7300 profile (same USB audio class)** | **Low — `TODO(hw-verify)`** |
| **`19 00` model-ID byte expectations** | **Deliberately not populated** | `model_id_bytes = None` until a real unit reports them (see §6.2) |
| **Per-mode FIL1/2/3 default widths** | Inherited from `FIL_DEFAULT_WIDTHS_HZ` (IC-7300 manual) | Low — cosmetic default only, `TODO(hw-verify)` |

## 4. Architecture

### 4.1 New module: `backends/ic7300/civ_profiles.py`

Single source of truth for per-model CI-V facts. The module docstring states explicitly that
the package directory name is historical (the shared Icom CI-V core lives here) and that a
rename to `backends/icom/` is a separate change.

```python
@dataclass(frozen=True)
class MeterCal:
    s_meter: list[tuple[int, float]]        # raw -> dB relative to S9
    power_frac: list[tuple[int, float]]     # raw -> fraction of rated power
    rated_power_w: float                    # 10 / 100 / 200
    swr: list[tuple[int, float]]            # raw -> SWR ratio
    alc: list[tuple[int, float]]            # raw -> ALC zone (1.0 = redline)
    comp: list[tuple[int, float]]           # raw -> dB compression
    voltage: list[tuple[int, float]]        # raw -> volts   (unverified on new models)
    current: list[tuple[int, float]]        # raw -> amps    (unverified on new models)

@dataclass(frozen=True)
class CivModelProfile:
    model_key: str                  # "ic705"
    display_name: str               # "Icom IC-705"
    source: str                     # provenance string for this profile's data
    civ_addr: int                   # 0xA4
    transceive_item: bytes          # b"\x01\x12"  ("1a 05 <item> 00/01")
    scope_bins: int                 # 475 / 689
    scope_amp_max: int              # 160 / 200
    scope_seq_max: int              # 11  / 15
    scope_queue_segments: int       # 4 * scope_seq_max
    scope_spans: dict[int, dict]    # UI index -> {"name", "freq": half-span Hz}
    bands: list[dict]               # {"name","start","end","default_freq","power_w"}
    mode_num_to_name: dict[int, str]
    mode_name_to_num: dict[str, int]
    fil_default_widths_hz: dict[str, list[int]]
    fil_width_range_hz: dict[str, tuple[int, int] | None]
    preamp_labels: dict[int, str]
    att_steps: tuple[int, ...]
    meter_cal: MeterCal
    audio_rx_rate: int
    audio_tx_rate: int
    audio_name_hints: tuple[str, ...]
    audio_gain_boost: float         # 1.0 for Icom, 10.0 for FT-710
    has_atu: bool
    filter_model: str               # "fil123"
    tune_via: str                   # "atu"
    dual_rx: bool                   # True for ic7610/ic7760, unsupported in this change
    mainsub_vfo: bool               # True for ic7610/ic7760 (A/B is MAIN/SUB)
    verified: bool                  # False for the three new models
    model_id_bytes: tuple[int, ...] | None   # None = do not compare (see §6.2)

PROFILES: dict[str, CivModelProfile]      # ic7300, ic7300mk2, ic705, ic7610, ic7760
def get_profile(model_key: str) -> CivModelProfile
def known_models() -> tuple[str, ...]      # registry order for server/UI validation
```

Profile field values are assembled from the tables in §5; the two verified profiles are
byte-for-byte equivalent to the current hardcoded IC-7300 tables (asserted by test).

### 4.2 Parameterization of the existing core

Every change below defaults to the current constant, so the verified IC-7300/MK2 path keeps
its present behavior and the existing call sites/tests need no edit.

| Module | Current | Change |
| --- | --- | --- |
| `civ_codec.ScopeAssembler` | `_seq_max` from wire, `SCOPE_MAX_SEGMENTS=11` only as module constant | `ScopeAssembler(seq_max=SCOPE_MAX_SEGMENTS, expected_bins=SCOPE_WAVEFORM_LEN)`. Bin-count mismatch → one WARNING with observed length, then adopt the observed length (adaptive, never freezes the waterfall). `SCOPE_WAVEFORM_LEN`/`SCOPE_AMPLITUDE_MAX`/`SCOPE_MAX_SEGMENTS` stay as module-level defaults. |
| `civ_scope.CivScopeProducer` | `scale_scope_bins(bins, SCOPE_AMPLITUDE_MAX, 255)` | `CivScopeProducer(controller, scope=None, on_frame=None, amp_max=SCOPE_AMPLITUDE_MAX)`; assembler built with the profile's `seq_max`/`bins` |
| `civ_controller.CivController` | `civ_addr=CIV_ADDR`, `transceive_cmd=SETMODE_CIV_TRANSCEIVE_ON`, queue `maxsize=SCOPE_QUEUE_MAX_SEGMENTS`, `self._model="IC-7300"` | new optional `profile: CivModelProfile \| None = None`; when supplied it fills address/transceive item/queue capacity/model label. **Explicit keyword arguments always win**, so existing callers and tests are unaffected. `SETMODE_CIV_TRANSCEIVE_ON/_MK2` stay as legacy constants. |
| `IC7300Backend` | tables hardcoded in `backend.py` | class attribute `_profile = PROFILES["ic7300"]`; `bands`, `ui_modes`, `mode_name_to_num`, `filter_tables`, `state_tables`, `capabilities` and the `CivController` construction all derive from `_profile` |
| `backends/base.RadioCapabilities` | — | new fields `verified: bool = True`, `tx_gated: bool = False`, `dual_rx: bool = False`, `unverified_meters: tuple = ()`, `audio_gain_boost: float = 1.0` (all included in `to_dict()`) |

### 4.3 New backend classes

`IC705Backend`, `IC7610Backend`, `IC7760Backend` — each ~10 lines, subclass `IC7300Backend`,
override `_profile` and `_display_name` only. Registered in `backends/__init__.py`:

```python
"ic705":     ("backends.ic7300.backend", "IC705Backend"),
"ic7610":    ("backends.ic7300.backend", "IC7610Backend"),
"ic7760":    ("backends.ic7300.backend", "IC7760Backend"),
```

## 5. Per-model data

### 5.1 Summary

| Item | IC-7300 | IC-7300MK2 | IC-705 | IC-7610 | IC-7760 |
| --- | --- | --- | --- | --- | --- |
| CI-V address | `0x94` | `0xB6` | `0xA4` | `0x98` | `0xB2` |
| Transceive item | `00 71` | `00 89` | `01 12` | `01 12` | `01 50` |
| Scope bins / amp / seq | 475 / 160 / 11 | same | 475 / 160 / 11 | **689 / 200 / 15** | **689 / 200 / 15** |
| Scope queue segments | 44 | 44 | 44 | 60 | 60 |
| Bands / rated power | 160m–6m / 100 W | same | 160m–6m + **2m/70cm** / **10 W** | 160m–6m / 100 W | 160m–6m / **200 W** |
| Modes (CI-V reg, decimal) | LSB 0, USB 1, AM 2, CW 3, RTTY 4, FM 5, CW-R 7, RTTY-R 8 | same | same + **WFM 6, DV 17** | same + **PSK-U 12, PSK-L 13** | same as IC-7610 |
| Attenuator steps | 0/20 dB | 0/20 | 0/20 | **0…45 dB, 3 dB steps** | **0…45 dB, 3 dB steps** |
| Preamp | OFF/PRE1/PRE2 | same | OFF/PRE1/PRE2 | OFF/PRE1/PRE2 | OFF/PRE1/PRE2 |
| VFO model | A/B | A/B | A/B | **MAIN/SUB** (`07 B0` swap) | **MAIN/SUB** |
| Dual receiver | no | no | no | **yes — not enabled** | **yes — not enabled** |
| `verified` | true | true | **false** | **false** | **false** |
| TX policy | allowed | allowed | **gated** | **gated** | **gated** |

Not included in the runtime tables (non-goals, §12): receive-only bands listed by the rig data
(Air, WFM, 630m, 2200m, Gen), IC-705 DV and the IC-7610/7760 PSK modes in the UI mode cycle,
and the IC-7610/IC-7760 SUB receiver.

### 5.2 Band tables

Built from the rig `Bands` entries, restricted to TX bands and normalised to the existing
`config_ic7300.BANDS` shape plus `power_w`:

- IC-705: 160m, 80m (3.500–4.000), 60m, 40m (7.000–7.300), 30m, 20m, 17m, 15m, 12m, 10m, 6m, **2m (144–148 MHz)**, **70cm (430–440 MHz)** — all 10 W.
- IC-7610: the IC-7300 list, all 100 W.
- IC-7760: the IC-7300 list, all 200 W.

The rig files carry regional variants (40m 7.0–7.2 vs 7.0–7.3, three 80m ranges, three 70cm/2m
ranges). The widest ITU-Region-3 set already used by the IC-7300 table is taken; per-region
override is out of scope.

### 5.3 Mode tables

`MODE_NUM_TO_NAME` reuses the existing UI strings (`CW` → `CW-U`, `CW-R` → `CW-L`, `RTTY` →
`RTTY-L`, `RTTY-R` → `RTTY-U`), keeping the shared UI mode vocabulary. New registers are added
to `MODE_NAME_TO_NUM` so that a radio-initiated mode change (`04` response) is always
nameable: `WFM` 6 and `DV` 17 for IC-705, `PSK-U` 12 and `PSK-L` 13 for IC-7610/IC-7760. They
are absent from `ui_modes`, so the browser cycle button cannot select them, but no mode reading
is ever mislabelled.

`ui_modes` is the IC-7300 list `["LSB", "USB", "CW-U", "AM", "FM", "RTTY-L"]` on all three new
models. `scope_spans` is the shared 8-entry table (§3) and `DEFAULT_SCOPE_SPAN` stays `5`
(±100 kHz) for all five profiles. `audio_name_hints` is the IC-7300 tuple
(`("usb audio codec", "usb audio device")`, per NFR-065) plus the model's own name
(`"ic-705"` / `"ic-7610"` / `"ic-7760"`) so the model-specific hint is tried first.

### 5.4 Meter calibration

`MeterCal.power_frac` is the verified IC-7300 `POWER_CAL` divided by its 100 W full-scale
value, i.e. normalised to "fraction of rated power"; `raw_to_power(raw) = frac(raw) *
rated_power_w`. This yields 10 W / 100 W / 200 W radios from one curve, keeps IC-7300 output
bit-identical to today, and confines the assumption to one multiply. New models list
`unverified_meters = ("power", "voltage", "current")`.

SWR, ALC and COMP keep the IC-7300 tables: the rig data shows identical raw ranges and
sub-codes on all five models, and ALC's raw-120 full scale is already documented in the
repository knowledge base.

### 5.5 Serial bandwidth check (NFR-003 / NFR-006)

The 689-bin waveform is ~1.45× the 475-bin payload (689 bin bytes + 15 segment headers vs
475 + 11). At 115200 8N1 that is roughly 11 waveforms/s of theoretical headroom against the
IC-7300's ~16/s, and the radio's own scope sweep rates are slower than both, so the CI-V link
remains the constraint-free path it is today — but the 689-bin models leave less margin. If a
field report shows scope starvation (segment drops, falling frame rate while CAT stays
responsive), the documented remedy is lowering the scope sweep speed, not raising the baud:
the CI-V USB baud is fixed at 115200 by the radio menu contract (A1, NFR-032). This is recorded
so the first hardware report is diagnosed from evidence rather than by guessing.

## 6. Safety

Safety here means: an unverified radio must not be able to end up in a state the operator did
not ask for, and an unverified implementation must not be presented as verified. SDD Chapter 15
(layered PTT release) is unchanged and is not weakened.

### 6.1 TX gate (unverified models default to transmit-disabled)

- `config.py` gains `ALLOW_UNVERIFIED_TX = _env_bool("MRRC_ALLOW_UNVERIFIED_TX", False)` and
  `capabilities.tx_gated = (not profile.verified) and not ALLOW_UNVERIFIED_TX`.
- `IC7300Backend._tx_allowed()` returns `profile.verified or config.ALLOW_UNVERIFIED_TX`.
  `set_ptt()` and `set_tune()` return `False` **before touching the serial port** when the gate
  is closed, with a single WARNING per process (no log flood).
- The guard lives in the backend, not only in the WebSocket handler, so every caller is covered:
  browser, iOS app, Android app, ATR1000 tune assist, and any REST path.
- `server.py` additionally checks `capabilities.tx_gated` in the PTT/TUNE handler and emits an
  actionable `error` message ("…not hardware-verified; set MRRC_ALLOW_UNVERIFIED_TX=1 to
  enable transmit"), so a refused key-up is never silent.
- Not gated: power-on/off, and every read/set that does not radiate (frequency, mode, filters,
  gains, scope). RX-only operation is fully usable without the environment variable.
- Diagnostics never bypass the gate implicitly: `_diag_civ.py --tx-check` requires both
  `--tx-check` and `--allow-tx` (see §7).

### 6.2 First-connection model identity

- `model_id_bytes` is `None` for **all five** profiles in this change. The repository has no
  verified `19 00` byte for any Icom model, and inventing one would be worse than not checking:
  a false mismatch warning on a working setup is a regression.
- Behaviour with `model_id_bytes is None`: query `19 00`, log the observed bytes at INFO
  ("radio reports model ID 0x…; profile has no expectation recorded"), continue. No warning, no
  blocking.
- Behaviour once a diagnostic report populates a profile: bytes differ → WARNING naming both
  values, `radioState.model_mismatch = True` → same banner path the frontend already uses for
  error toasts; never blocks a command. No answer (older radios / LAN-unlinked port) → silent
  skip.

### 6.3 Scope degradation

- The assembler is constructed from the profile (`seq_max`, `bins`) but is self-correcting:
  a waveform whose assembled length differs from `expected_bins` logs one WARNING with the
  observed length and is rendered at the observed length. A profile error therefore shows up
  as one log line and a working waterfall, not as a frozen spectrum or a crash.
- `spectrum_rx2` stays all-zeros on every model in this change (single-receiver presentation),
  matching the current IC-7300 behaviour and the existing frontend, which renders only `rx1`.

## 7. Diagnostic script `_diag_civ.py`

Generalises `_diag_ic7300_scope.py` (which stays as-is: it is referenced by
`IC-7300_硬件验收清单.md`). Pure helpers (sample summarisation, report formatting, mismatch
evaluation) are importable and unit-tested with synthetic frames; the serial path is not
unit-tested, exactly like the existing script.

```
python _diag_civ.py --model ic705 --port /dev/cu.usbmodemXXXX [--scan] [--tx-check --allow-tx]
```

| Step | Action | Output |
| --- | --- | --- |
| 1 | Port enumeration; optional read-only address scan | candidate ports / which CI-V addresses answer |
| 2 | `19 00` model ID; compare against the profile when populated | observed bytes, match/unknown verdict |
| 3 | Read-only probes: `03` freq, `04` mode, `15 02` S-meter, `14 0A` RF power, `16 02` preamp, `11` attenuator, `1a 05 <item>` transceive item | value or explicit "no answer" (never a fabricated default) |
| 4 | Spectrum: `27 10 01` → `27 14 00` → `27 15 <half-span BCD>` → `27 11 01`, capture 30 waveforms | **observed bin count, amplitude min/max/histogram, observed `seq_max`, frame rate, span round-trip** |
| 5 | Static meter sample: `15 11/12/13/14/15/16` | raw values (no RF) |
| 6 | Optional TX check (`--tx-check --allow-tx` only): `1C 00 01` → 200 ms → `1C 00 00`, then **read back `1C 00`** | PASS only if the read-back shows RX; on failure a loud warning plus manual recovery steps (power off / unplug) |
| 7 | `finally`: restore state (waveform output off, scope display restored to its original value, PTT released) and write `diag_civ_<model>_<timestamp>.md` | paste-ready report |

Step 4 is the highest-value output: it either confirms `475/160/11` / `689/200/15` or replaces
them with measured values in one run, which is the only way those two facts can stop being
assumptions.

## 8. Wiring changes

| File | Change |
| --- | --- |
| `backends/__init__.py` | register `ic705`/`ic7610`/`ic7760`; add `known_models()` derived from the registry |
| `backends/ic7300/civ_profiles.py` | new (§4.1) |
| `backends/ic7300/{backend,civ_codec,civ_controller,civ_scope}.py` | profile parameterisation (§4.2), three new backend classes (§4.3) |
| `backends/base.py` | five new `RadioCapabilities` fields (§4.2) |
| `config.py` | `_DEFAULT_BAUD_BY_MODEL` + three entries at 115200; `ALLOW_UNVERIFIED_TX` |
| `server.py` | hardcoded `("ft710","ic7300","ic7300mk2")` whitelist → `known_models()`; TX-gate error message in the PTT/TUNE handler; startup log line with model, `verified`, gate state |
| `radio_state.py` | new dirty-tracked field `model_mismatch: bool = False` (kept out of every poll path — set once at connect time by the backend's identity check, cleared on reconnect) |
| `backends/ft710/backend.py` | `audio_gain_boost=10.0` in its capabilities (same value the frontend already applies by string comparison — no behavior change) |
| `static/index.html` | three new connection-dialog options, each labelled as experimental |
| `static/ft710_main.js` | `AUDIO_GAIN_BOOST` from `capabilities.audio_gain_boost`, legacy expression kept as fallback |
| `static/ft710_ui.js` | "experimental / TX disabled" banner driven by `capabilities.verified` + `tx_gated` |
| `packaging/pyinstaller/mrrc_modern_server.spec` | hiddenimport `backends.ic7300.civ_profiles` |

`fullState` gains `radioModel`, existing `capabilities` plus the new fields; no new WebSocket
endpoint is introduced, so the auth surface is unchanged.

## 9. Test plan

| Test module | Coverage |
| --- | --- |
| `tests/test_civ_profiles.py` (new) | five profiles present; IC-7300/MK2 profiles equal today's hardcoded tables value-for-value; `scope_queue_segments == 4 * scope_seq_max`; mode tables are bijective; band tables ordered/contiguous; every TX band's `power_w` equals `meter_cal.rated_power_w`; `verified is False` on exactly the three new profiles; `model_id_bytes is None` on every profile |
| `tests/test_backend_factory.py` (extend) | the three new keys construct; capabilities carry the right address/scope/meter fields; unknown key still raises |
| `tests/test_civ_codec.py` (extend) | 689-bin / 15-segment reassembly; `scale_scope_bins(in_max=200)`; single-segment (LAN-style) waveform; mismatched assembled length is adopted with one warning |
| `tests/test_civ_scope.py` (extend) | 200-ceiling frame renders to 850 bins in range; `spectrum_rx2` stays zero; no crash on unexpected bin count |
| `tests/test_unverified_tx_gate.py` (new) | with `MRRC_ALLOW_UNVERIFIED_TX` unset, a backend on an unverified profile reports `capabilities.tx_gated is True` and `set_ptt`/`set_tune` return False while the mock serial `write()` is never called; with the variable set to `1` both are allowed; an IC-7300/MK2 backend is ungated and TX-capable in both settings |
| `tests/test_model_mismatch.py` (new) | `model_id_bytes is None` ⇒ INFO log only; populated mismatch ⇒ warning + `model_mismatch`; no answer ⇒ silent skip and no state change |
| `tests/test_sdd_harness.py` (extend if it asserts doc counts) | documentation-sync assertions stay green |

The existing 724 tests must stay green; the IC-7300/MK2 paths run on default arguments and must
behave identically.

## 10. Documentation synchronization

- `SDD/14-version-history.md` — new entry (V2.41) describing the profile-driven CI-V core, the
  three new models, the TX gate and the explicit unverified boundary.
- `SDD/README.md` — Quick Facts version bump and status line.
- `SDD/08-architecture-decisions.md` — AD-016 consequences extended (shared CI-V core, profile
  data table, TX gate); no new AD.
- `SDD/09`, `SDD/11` — backend/component tables gain the three models and `civ_profiles.py`.
- `SDD/05` — NFR-032/NFR-041 verification notes cover the new model keys; NFR-060/NFR-065 note
  the assumed 48 kHz Icom profile.
- `SDD/13.2/13.3` — new risk **R9** (unverified 689-bin amplitude ceiling and meter curves;
  mitigation: diagnostic script + `unverified_meters` + TX gate) and new assumption **A7**
  (the three models share the IC-7300 CI-V command surface, per local rig data).
- `SDD/15-ptt-safety-architecture.md` — note the new outermost "unverified model TX gate".
- `AGENTS.md` — module table (`civ_profiles.py`), environment variables
  (`MRRC_ALLOW_UNVERIFIED_TX`, new model keys), backend description.
- `README.md` — supported-radio table with the experimental label.
- `tests/README.md` — test/module counts.
- `IC-7300_硬件验收清单.md` — pointer to `_diag_civ.py` as the multi-model successor tool.

## 11. Explicit hardware acceptance boundary

Passing the software suite proves table correctness against the rig data, framing/parsing,
689-bin reassembly, gate behaviour, and wiring. It cannot prove, and no completion claim will
describe as proven:

- the actual `19 00` model-ID bytes of any model;
- the real bin count / amplitude ceiling / segment count of the IC-7610 and IC-7760 scope stream
  (rig data is the only evidence; measured in one diagnostic run);
- absolute wattage, drain voltage and drain current on the IC-705/7610/7760 meters;
- USB audio rate, device naming and audio level on the three models;
- front-panel CI-V menu prerequisites (USB CI-V baud, CI-V address, scope output enable) per
  model;
- MAIN/SUB interaction, since the SUB receiver is deliberately not implemented.

Each of these is reported by `_diag_civ.py` and resolved by pasting its report back into the
repository, which is also how a profile's `verified` flag is allowed to flip to `true`.

## 12. Non-goals

- No LAN / RS-BA1 / network transport. CAT and audio stay on USB (CI-V + USB audio), which is
  also how the IC-7760's control unit is driven from a PC in this deployment.
- No SUB-receiver support: no cmd-29 prefix, no second waterfall, no MAIN/SUB UI.
- No new CI-V features beyond what the existing IC-7300 backend already exposes (no memories,
  no VOX, no DV/PSK support in the UI, no dual-watch).
- No change to Opus, PyAudio, the resampler, the spectrum scheduler, or the FT-710 backend.
- No package rename in this change (`backends/ic7300/` keeps its name; recorded as naming debt).

## 13. SDD traceability

| Reference | Relationship |
| --- | --- |
| AD-016 | Extended — pluggable backends gain a profile-driven shared CI-V core and three new model keys; no contradiction, no new AD |
| AD-006 / AD-011 | Unchanged in mechanism — CI-V `27` scope and per-backend audio rates are reused; the new profiles assume 48 kHz like the IC-7300 |
| NFR-032 | Directly serves — protocol compatibility for the selected backend now covers five model keys |
| NFR-003 / NFR-006 | Affected — the 689-bin CI-V payload is a larger share of the 115200 baud link (§5.5); no scheduler or framing change |
| NFR-020 | Unchanged — no new WebSocket endpoint or REST route; the new capability fields travel in the existing authenticated `fullState` |
| AD-003 | Reused — `model_mismatch` is written through `radio.update(...)` so it is dirty-tracked like every other state field |
| AD-013 | Extended, not replaced — the piecewise-linear calibration approach is kept and its power table is normalised to "fraction of rated power" so one curve serves 10 W / 100 W / 200 W radios; the FT-710 tables are untouched |
| §7.2 / §9.7 | Updated by this change — the state entity table and the `fullState`/capability description gain `model_mismatch` and the five new capability fields |
| §10.4 | Updated by this change — the CI-V command/profile table gains the three model keys, their addresses, transceive items and scope geometry |
| NFR-041 | Directly serves — `MRRC_RADIO_MODEL`, new `MRRC_ALLOW_UNVERIFIED_TX` |
| NFR-050 | Directly serves — one profile module, one shared core, no per-model copies |
| NFR-060 / NFR-065 | Affected — IC-705/7610/7760 join the 48 kHz no-resample path with generic USB-audio name hints (assumed, flagged) |
| NFR-012 / Chapter 15 | Preserved and extended — the TX gate is a new outermost refusal layer; no blocking verify loop is introduced |
| R1 / A1 | Unchanged — serial port selection and radio menu prerequisites still apply per model |
| R9 / A7 | Added by this change (§10) |
| I6 | Unchanged and not addressed — no multi-client arbitration is added |
| UC-001 / UC-003 / UC-004 | Main flows unchanged for a new model key; spectrum and PTT behave as for the IC-7300 |
