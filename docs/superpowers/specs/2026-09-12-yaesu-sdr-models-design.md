# Yaesu SDR Model Support: FTDX10, FTDX101D, FTDX101MP, FTX-1F

**Date:** 2026-09-12

**Status:** Approved for implementation

**Scope:** Add four Yaesu ASCII-CAT radios (FTDX10, FTDX101D, FTDX101MP, FTX-1F) on a new
profile-driven Yaesu core `backends/yaesu/`, leaving the hardware-verified FT-710 path
(`backends/ft710/`) untouched. No physical unit of any of the four models is available, so the
design carries the same explicit, machine-readable "unverified" boundary the Icom models use
(§6): transmit stays gated, spectrum falls back to the existing S-meter synthesiser, and every
table records where it came from. Dual receive — a real capability of the FTDX101D/MP and the
FTX-1F — is deliberately **phase 2** (§12).

Authoritative references (all offline, all already on the development machine):

- Hamlib 4.7.2 source tree at `~/hamlib/Hamlib-4.7.2/`:
  - `rigs/yaesu/newcat.c` — 13,048-line shared Yaesu ASCII CAT core. The architectural
    precedent for this design: `newcat.c` is the core, `ftdx10.c` (334 lines), `ftdx101.c`
    (381), `ftdx101mp.c` (281) and `ft710.c` (321) are per-model table files.
  - `rigs/yaesu/ftx1/` — 18 modules, **90/90 CAT commands implemented**, reference
    *FTX-1 CAT Operation Reference Manual* `FTX-1_CAT_OM_ENG_2508-C.pdf`, firmware tested
    November 2025; ships its own command list (`ftx1-cat-commands.txt`) and readme.
  - `simulators/simftdx101.c`, `simulators/simyaesu.c` — protocol simulators usable as a
    hardware-free test peer.
- Yaesu *FT-710 CAT Operation Reference* — repository file `FT-710_CAT.md`, already the
  reference for the verified backend, for the ASCII framing (`CMD;`, `P1P2;` parameters,
  `;`-terminated answers) that the family shares.
- Repository fact (checked 2026-09-12): the Yaesu scope **waveform is not documented in any
  CAT reference**. `SS` ("SPECTRUM SCOPE") is a settings command only — set/read span, speed,
  edge — confirmed in Hamlib's `newcat.c` command table and `ftx1/ftx1_ext.c`. Hamlib's entire
  Yaesu family contains zero scope-data code. A waterfall for these models therefore cannot be
  written from documentation and requires field captures (§12, phase 4).

Fields that no local source can supply are marked `TODO(hw-verify)` in code and stay out of any
verified claim. Guessing is never presented as data.

## 1. Objective

An operator with an FTDX10, FTDX101D, FTDX101MP or FTX-1F selects the model in the connection
dialog (or `MRRC_RADIO_MODEL`) and gets the FT-710 experience minus the waterfall: CAT control
(frequency, mode, filter, PTT/TUNE, gains, DSP, memory channels), per-model meter calibration,
USB audio RX/TX, PTT/TUNE under the existing layered safety model — with honest capability
reporting about what has and has not been verified. Transmit on all four models stays disabled
until the operator opts in (§6.1), so a first connection is receive-only.

## 2. Decisions taken during design review (2026-09-12)

| # | Question | Decision |
| --- | --- | --- |
| D1 | Target models | **FTDX10 + FTDX101D + FTDX101MP + FTX-1F.** The FT-710 is already supported; the FTDX101D/MP (2019) are included at the operator's explicit request even though they sit just outside a literal five-year window. VHF/UHF-only SDR mobiles and handhelds (FT-5D, FTM-500D/510D) are out of scope: no HF, no waterfall, different product. |
| D2 | Hardware availability | **None.** No unit of any of the four models is reachable; all four ship marked unverified with the TX gate on. |
| D3 | Dual receive | **Phase 2.** Both the FTDX101D/MP and the FTX-1F have a second receiver (FTX-1F: `ftx1_vfo.c` carries 46 MAIN/SUB references under Hamlib), but phase 1 is single-receiver — `dual_rx=False`, SUB neither controlled nor shown. Rationale: protocol tables are unverified; multiplying that by a doubled state model, WebSocket surface and UI in the same release stacks two independent risks. Phase 2 gets its own spec once field feedback exists. |
| D4 | Spectrum | **S-meter fallback, no fabricated waterfall.** No CAT waveform exists (above), so `scope_type="none"` and `create_scope_producer()` returns `None` — a contract `backends/base.py` already documents ("the server's S-meter fallback covers that"). The existing `ScopeHandler` keeps broadcasting the S-meter-derived trace; the frontend needs no change. A real waterfall waits for field captures (phase 4). |
| D5 | Architecture | **New shared core + per-model profiles in `backends/yaesu/`** (the two alternatives were rejected: per-model subclasses of `FT710Backend` would drag the daily-driver FT-710 class hierarchy into three unverified models' needs and leave `cat_controller.py` as an accidental shared core; migrating FT-710 in the same release has the largest blast radius). |
| D6 | FT-710 migration | **Not in this work.** `backends/ft710/` is untouched. Migrating it onto the shared core is phase 3, done against real hardware with per-command A/B comparison and `MRRC_RADIO_MODEL=ft710` as the rollback. |
| D7 | FTX-1 key naming | **One key `ftx1`**, display name "Yaesu FTX-1F". `ID;` answers `0840` in all three hardware configurations, so the radio is one profile; the configuration (Field head on battery 0.5–6 W / Field head on 12 V 0.5–10 W / SPA-1 5–100 W) is detected at runtime from the `PC` power-format answer instead of being split into separate model keys. |
| D8 | FTDX101 variants | **Two keys** — `ftdx101d` (100 W) and `ftdx101mp` (200 W, Vd/Id meters) — because capability flags differ, exactly as `ic7300`/`ic7300mk2` are separate keys today. |
| D9 | Unverified discipline | Same three mechanisms as the Icom models: `verified=False` + `tx_gated=True` in `RadioCapabilities`; a read-only `ID;` identity check that logs observed bytes and only warns; provenance recorded per table. |

## 3. Data provenance

| Data | Source | Status |
| --- | --- | --- |
| ASCII framing, `;` terminator, `P1P2;` parameters | `FT-710_CAT.md` (in-repo), implemented and field-proven in `backends/ft710/cat_controller.py` | verified on FT-710, carried over |
| Mode code tables (`MD` P2) | Hamlib `newcat.c` + `ftdx10.c`/`ftdx101.c` tables; FTX-1 codes enumerated in `ftx1/ftx1_mode.c` (1=LSB … C=DATA-U, D=AM-N, E=PSK, F=DATA-FM-N, H=C4FM-DN, I=C4FM-VW) | documented, per model |
| Filter width tables (`SH`) | Hamlib per-model filter tables (`ftdx10.c` `{RIG_MODE_SSB, Hz(2400)}` …) | documented |
| Menu/`EX` item map | Hamlib `ftdx10_ext_levels[]`, `ftx1/ftx1_menu.c` + `ftx1_menu.h` | documented |
| FTX-1 memory-mode quirk | Hamlib `ftx1/ftx1_mode.c`: memory-mode `MD` sets on MAIN are accepted by firmware but act as a transient tune overlay that does not persist → the model must leave memory mode first | documented, encode as a profile flag |
| Power-format split (`PC1xxx` 6/10 W vs `PC2xxx` 100 W) | Hamlib `ftx1/ftx1_readme.txt` (three configurations, `ID;` = 0840 for all) | documented |
| `ID;` answer strings for FTDX10/FTDX101D/MP | cross-check official manuals against Hamlib `nc_rigid_t` at implementation time | `TODO(hw-verify)` — empty expectation means INFO-only logging |
| Meter curves (raw → S/dBm/power/SWR/ALC/Vd/Id) | no local source for the new models; shape inherited from the FT-710 curves | **assumed**, listed in `unverified_meters` |
| USB audio sample rates and device names | not in Hamlib and not documented per model | **assumed 44.1 kHz** (the family value), `TODO(hw-verify)` |

## 4. Architecture

### 4.1 New package `backends/yaesu/`

```
backends/yaesu/
  __init__.py          # exports PROFILES, YaesuBackend, get_profile()
  cat_core.py          # YaesuCatController — transport, profile-parameterized
  yaesu_profiles.py    # YaesuModelProfile dataclass + PROFILES + provenance
  backend.py           # YaesuBackend(RadioBackend) + FTDX10Backend,
                       # FTDX101DBackend, FTDX101MPBackend, FTX1Backend
```

`backends/ft710/` is not modified by this work.

### 4.2 `YaesuCatController` (transport layer)

Carries over the parts of the FT-710 `CatController` that are proven on hardware and are
model-independent: `_read_until(b";")` framing with `expected_prefix`, `send_priority_set_command`
priority writes, `_is_device_gone()` ENXIO/USB-re-enumeration classification, `reconnect_loop`
cadence, and the connect/cleanup sequence. Everything model-specific is **removed** from the
core and read from the profile instead: `EX` menu items, meter-selection numbers, mode numbers,
filter widths, attenuator/preamp step values, VFO-B direct addressing, `PC` power formatting,
and the FTX-1 memory-mode pre-step.

Public surface mirrors `RadioBackend`'s abstract command list exactly (the ~60 helpers in
`backends/ft710/cat_controller.py` are the de-facto interface contract), so `YaesuBackend` is a
thin profile-driven wrapper in the same way `IC7300Backend` wraps `CivController`.

### 4.3 `YaesuBackend` and the four subclasses

`YaesuBackend` derives `capabilities`, `bands`, `ui_modes`, `mode_name_to_num`,
`filter_tables()`, `state_tables()`, poll items and meter items from `self._profile`, copying the
structure of `backends/ic7300/backend.py` (including `_log_tx_gate_once`, `_tx_allowed`,
`_check_model_identity`). The four subclasses override only `_profile` and `_display_name`.

### 4.4 Wiring

- `backends/__init__.py` `_BACKENDS` gains `ftdx10`, `ftdx101d`, `ftdx101mp`, `ftx1`
  (lazy import, same pattern as today).
- `config._DEFAULT_BAUD_BY_MODEL` gains the same four keys at 38400 (this dict is hand-written,
  not registry-derived — verified 2026-09-12 — and is what `default_baud_for()` serves to the
  connection dialog and first-run probing).
- No WebSocket, `radio_state.py`, `poll_scheduler.py` or frontend change: single receiver, no
  scope frames. The connection dialog picks the new models up from `known_models()`.

## 5. `YaesuModelProfile`

Field groups (dataclass style follows `CivModelProfile`):

**(a) Static capability** — `model_key`, `display_name`, `default_baud`, mode number ↔ name map,
per-mode filter width tables, attenuator steps, preamp labels, band table, memory-channel
support, `has_atu`, `has_vd_id_meters`, `vfo_b_direct`, audio RX/TX rates, `audio_name_hints`,
`audio_gain_boost`.

**(b) Protocol differences** — `power_format` (`"PC1"` / `"PC2"` / detected), `id_answer`
(may be empty = log-only), `power_on_via` (`PS`), `tune_via`, `mode_set_leaves_memory` (FTX-1
quirk), scope setting commands (`SS`/`EX` items) for a future phase.

**(c) Meter calibration** — raw → dBm / S-unit / power / SWR / ALC / Vd / Id functions, plus
`unverified_meters`.

**(d) Verification and provenance** — `verified=False`, `tx_gated=True`, `dual_rx=False`
(phase 1), `provenance` string per table (manual name + section, or Hamlib file + symbol).

Per-model summary to be filled at implementation:

| Key | Display | Year | Power | `ID;` | Power format | Dual RX (phase 2) |
| --- | --- | --- | --- | --- | --- | --- |
| `ftdx10` | Yaesu FTDX10 | 2020 | 100 W | TODO(hw-verify) | `PC1` | no |
| `ftdx101d` | Yaesu FTDX101D | 2019 | 100 W | TODO(hw-verify) | `PC1` | yes |
| `ftdx101mp` | Yaesu FTDX101MP | 2019 | 200 W | TODO(hw-verify) | `PC1` | yes |
| `ftx1` | Yaesu FTX-1F | 2025 | 6/10/100 W | `0840` | detected (`PC1`/`PC2`) | yes |

## 6. Safety

### 6.1 TX gate (unverified models transmit only on explicit opt-in)

`RadioCapabilities.tx_gated = not _tx_allowed()`; `_tx_allowed()` is True only when
`MRRC_ALLOW_UNVERIFIED_TX=1`. `set_ptt(True)` and `set_tune(True)` return False and log **once**
per process (`_log_tx_gate_once`). PTT release is never gated. The existing server-side PTT
watchdog, TX-audio gating and same-session replacement rules are unchanged.

### 6.2 Identity check on connect

A read-only `ID;` query (the Yaesu family's model read-back) logs the observed bytes at INFO.
When the profile records an expected answer and it does not match, a single WARNING names both
values and the radio continues to work — same discipline as the Icom `19 00` check: never a
release, never a hard failure.

### 6.3 Degradation

No scope producer → the server's existing S-meter synthesiser keeps the waterfall area alive.
No meter curve → that meter is omitted from polling and listed in `unverified_meters` rather
than shown with invented numbers.

## 7. Diagnostic script `_diag_yaesu.py`

A read-only field tool modelled on `_diag_civ.py`: open the serial port, `ID;`, then probe the
read-only commands the profile claims (frequency, mode, filter, S-meter, power/SWR/ALC/Vd/Id
where present), print a paste-ready Markdown report with observed answers, and list which
profile fields the observed bytes contradict. Optional `--tx-check --allow-tx` performs a
sub-second key-up with PTT read-back and always releases PTT in a `finally` block. This is the
instrument that closes the `TODO(hw-verify)` gaps once a field owner has one of the radios.

## 8. Test plan

All hardware-free, following existing patterns:

- `tests/test_yaesu_profiles.py` (mirrors `tests/test_civ_profiles.py`): every profile has
  provenance; every table is non-empty; mode numbers unique and answers round-trip; attenuator
  and preamp steps strictly ordered; meter curves monotonic; all four models carry
  `verified=False` and `tx_gated=True`; `dual_rx` False in phase 1.
- `tests/test_yaesu_cat_core.py` (mirrors `tests/test_cat_controller.py`, `AsyncMock`/`patch`,
  no serial hardware): framing and `;` handling, `expected_prefix` filtering, timeout, priority
  writes, ENXIO classification and reconnect, both `PC` power formats, the FTX-1 memory-mode
  pre-step, and gate refusal on PTT/TUNE.
- Hamlib simulator protocol test: run `simftdx101` (and `simyaesu` where applicable) as a fake
  radio over a pty/TCP and drive frequency/mode/PTT round-trips for `ftdx101d` — the closest
  thing to an end-to-end check available without hardware.
- Update `tests/test_backend_factory.py::test_known_models_covers_every_backend` (exact tuple)
  and `tests/test_config.py`'s baud assertions.
- Full suite must stay green (909 tests today).

## 9. Documentation synchronization

- `README.md` — model table, `MRRC_RADIO_MODEL` values, unverified-model warning, Quick Facts.
- `AGENTS.md` — backend table rows for `backends/yaesu/`.
- `DEPENDENCIES.md` — new model rows (USB serial, audio).
- `SDD/` — new AD (proposed **AD-018: profile-driven Yaesu core; the verified FT-710 path stays
  separate until hardware A/B migration**), chapter 2 (models), 5 (NFR: no hardware claims),
  10/11 (service/component), 12 (operational: gating), 13 (risks: unverified tables, assumed
  audio rates, missing scope), 14 (version history).
- `tests/README.md` — new test modules.
- `docs/` operator guides — model list and the receive-only first-connection behaviour.

## 10. Explicit hardware acceptance boundary

Everything in this work is documentation-derived. The following stay open until a field owner
runs `_diag_yaesu.py` on real hardware:

| Item | How it closes |
| --- | --- |
| `ID;` answers for FTDX10/FTDX101D/MP | diagnostic report, observed bytes |
| USB audio sample rate and device names | device enumeration in the report; a wrong rate is audible as pitch shift |
| Meter curves (S/dBm/power/SWR/ALC/Vd/Id) | compare reported values against the radio's own display at known power levels |
| Mode/filter coverage per model | set each mode/filter and read it back |
| PTT/TUNE timing and the TX audio path | opt-in TX check, then a real QSO |
| Scope presence | confirms D4 (no documented waveform) on real hardware |

## 11. Non-goals

- Dual receive (phase 2), waterfall/scope (phase 4), RF/audio-quality verification.
- The SCU-LAN10 hardware path, and the FTDX101's external-display scope output.
- Hamlib `newcat.c` reuse: it is C, copyleft-licensed (GPL-2 tools / LGPL library, see
  `COPYING` + `COPYING.LIB`) and structured for `rigctl`, so it is source evidence, not a
  dependency.
- Migrating the FT-710 backend (phase 3).
- Digital-voice (C4FM/WIRES-X) control.

## 12. Phases

| Phase | Content | Gate |
| --- | --- | --- |
| **1 (this spec)** | `backends/yaesu/` core + four profiles, single receiver, gated TX, S-meter fallback, tests, docs | 909+ tests green, no hardware claims |
| 2 | Dual receive for `ftdx101d`/`ftdx101mp`/`ftx1` (state model + WS + UI) | own spec; ideally after phase-1 field feedback |
| 3 | Migrate FT-710 onto the shared core as a fifth profile | real-hardware A/B per command; rollback = old path |
| 4 | Real spectrum for these models | requires field captures of the scope stream; none documented today |

## 13. SDD traceability

- Architecture decisions: **AD-018** (new, proposed): profile-driven Yaesu core with the
  verified FT-710 path kept separate; **AD-019** (new, proposed): unverified models ship
  receive-only with a read-only identity check and per-table provenance.
- Inherited constraints: no hardware claim without evidence (Icom model precedent, spec
  2026-09-12), TX safety layering unchanged, one writer per CAT port, per-backend audio rate
  and name hints, poll-tier coverage of all state fields.
- Risks: unverified CAT tables (R: mitigated by provenance + diagnostics), assumed audio rate,
  absent spectrum, the FT-710 daily path staying on an older code shape until phase 3.
- Verification boundary: §10 above; SDD §13 and the README carry the same list.


## Implementation outcome (2026-09-12)

Shipped as planned in `docs/superpowers/plans/2026-09-12-yaesu-sdr-models.md`
(9 tasks / 42 steps, commits `76e3664` … `f22e17c`). The deviations below were found while executing
the plan and are recorded in the plan's review tables; each one changed this design.

| Finding | Effect on this design |
| --- | --- |
| R1 — `__getattr__` does not satisfy `ABCMeta`, so the four backend classes would have stayed abstract and failed to instantiate | §4.3: the abstract surface is satisfied by delegates bound onto the class at import time, with the frozen abstract set cleared and a test that fails if the ABC grows a method |
| R2 — mode must be an **int** register (`RadioBackend.set_mode(mode_num: int)`, `RadioState.mode: int`), and the FTX-1's `H`/`I` C4FM codes are not hex digits (`f"{0x11:X}"` would have sent an invalid `MD011`) | §5 gained `mode_codes`: registers for the state/UI contract, a separate explicit CAT-character table for the wire, plus reverse lookup with unknown-character rejection |
| R3 — poll items and `initial_state_sync` must return **parsed** values under real `RadioState` field names (`vfo_a_freq`, not `frequency`) | §4.3: the core gained `get_af_gain`/`get_rf_gain`/`get_rf_power` + a shared `_get_int` helper; the sync keys are the dataclass fields |
| Task-9 boot smoke test — `server.py` reads `backend.cat` unconditionally, so a backend without it aborts application startup | §4.3/§6: `backends/base.py` now declares `cat` and `set_broadcast_callback` (documented contract, no-op default), and a regression test asserts every server-visible attribute exists on every registered backend |
| The unverified-model startup warning told Yaesu operators to run `_diag_civ.py` | §7: the warning now names the matching tool per capability family |
| The verified FT-710 path sends `MG{value:03d}` without the family `P1` selector | §5: the new core sends the documented `MG0xxx` form; the discrepancy is flagged for the field diagnostic rather than changed on the verified path |

Verification actually achieved against §10's boundary: **1034 tests** (1 opt-in Hamlib-simulator test
skipped), authoritative pyright clean, backend smoke test for the four models, hardware-free server
boot with `MRRC_RADIO_MODEL=ftx1` (ENXIO classification and the TX-gate warning observed, no
traceback). The hardware boundary itself is unchanged: no unit of any of the four models has been
tested, so `id_answer` stays empty for three of them, the audio rate stays an assumption, the meter
curves stay in `unverified_meters`, and there is still no waterfall. Phases 2-4 (§12) are unstarted.
