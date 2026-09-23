# ATR-1000: High-SWR Auto Full Tune + QRP-Friendly Learning

**Date:** 2026-09-23

**Status:** Approved for implementation (guard = client-autonomous, trigger gated on measured power, no self-keying; write-back only when improved and ≤1.8)

**Scope:** Port the sibling project's high-SWR auto-retune guard into this project's optional
ATR-1000 client, and bring the learning gate up to the sibling's latest behaviour. The guard
watches the tuner's own METER stream **while the operator is already transmitting** and, when SWR
stays above the threshold, sends one ATR-1000 full-tune command (`mode=2`). It never keys the
radio, never touches CAT/PTT, and adds no task, no WebSocket endpoint and no protocol change.

Reference: the sibling project `mrrc` V5.8.0 (`check_swr_retune()` in `atr1000_proxy.py`,
`docs/superpowers/plans/2026-08-09-atr1000-swr-autotune.md`, CHANGELOG "ATR-1000 SWR>2 自动完整调谐")
and V5.8.5 ("ATR-1000 天调自动学习修复": learning entry switched from the front-end TX signal to
measured power; `LEARN_MIN_POWER` 5 W → 3 W).

Authoritative in-repo references:

- `atr1000_client.py` — `_handle_meter()` (METER decode + tuning auto-clear + stable-window
  learning), `_handle_relay()` (`_relay_changed_at`, tuning stability tracking), `_clear_tuning()`
  (all four clear paths), `_poll_loop()` (1 s tick, 45 s tuning hard timeout, `_pending_freq`
  worker-side consumption via `_wake`), `start_tune(mode)`, `LearningBuffer`, `_maybe_learn()`,
  `_get_storage()`.
- `atr1000_tuner.py` — `TunerStorage.learn(..., force_update=False)`, `get_tune_params()`
  (learn gate SWR 1.0–1.8, 1 kHz keys, ±5 kHz nearest, atomic writes).
- `server.py` — `_atr_tune_assist()` + `ATR_TUNE_*` (manual TUNE button path: TX2 carrier →
  skip if SWR ≤ 1.6 → `start_tune(mode=2)` → keep+learn if improved ≥ 0.02 else rollback),
  `_start_atr_tune_assist()` preconditions, `_on_atr_change()` → `_broadcast_atr()`,
  the lifespan wiring (`atr = ATR1000Client(...)`), `/WSatr1000`.
- `static/modules/atr1000.js` — `onTuneResult()` toast switch, `tuneInProgress`, TUNE button
  progress state.
- SDD §9.8 (ATR1000 linkage), SDD ch15 (PTT safety; the tune-assist carrier `finally` rule).

## 1. Objective

An operator transmitting into a badly matched antenna must not have to press anything: if the
ATR-1000 reports SWR above 2.0 for 1.5 s of real transmit power, the server asks the tuner for a
full tune, tells the operator why, and — when the tune actually improved the match and landed
inside the learn gate — writes the resulting relays into the learned store so the next visit to
that frequency starts from the tuned values. All of it works with no browser open (panel PTT or
external software transmitting included).

## 2. Decisions taken during design review (2026-09-23)

| # | Decision | Rationale |
| --- | --- | --- |
| 1 | **Trigger only during the operator's own transmission** — measured power ≥ 5 W (option A) | Sibling parity; the strongest safety boundary available: the guard can never key the radio, so it cannot create an unattended carrier. Cost accepted: 1–3 s of speech is consumed by the tuner scan. |
| 2 | **Explicit post-tune write-back** — improved ≥ 0.02 **and** final SWR ≤ 1.8 → `learn(force_update=True)` (option B) | Answers "更新参数" deterministically even when the 4-sample window does not fill or the SWR would fall outside the learn gate; the ≤ 1.8 ceiling keeps junk out of a store whose values are auto-applied later. |
| 3 | **No rollback** — never restore the pre-tune relays (contrast with the manual assist) | Sibling parity; rolling back mid-QSO to a known-bad match is worse than the tuner's own best effort. |
| 4 | **Guard lives in `atr1000_client.py`** with instance state (approach 1) | Event-driven on the device's own METER push, zero new tasks, works without a browser, and structurally avoids the sibling's V5.8.5 bug class (module-level globals + `global` declarations across threads). |
| 5 | **No enable/disable environment variable** (option A) | The feature is already opt-in as a whole (`MRRC_ATR1000_HOST`); threshold + debounce + cooldown + 3-fail cap are four layers of protection. YAGNI on extra config surface. |
| 6 | **All sibling parameter values taken verbatim** (option A) | Field-tuned in the sibling; both projects behaving identically means one set of numbers to remember when debugging. |

## 3. Parameters (sibling parity, verbatim)

New constants in `atr1000_client.py`, immediately after the existing `LEARN_*` block:

```python
# ── High-SWR auto-retune guard (sibling mrrc V5.8.0 / V5.8.5 parity) ──
SWR_RETUNE_THRESHOLD      = 2.0    # strictly greater → too high
SWR_RETUNE_MIN_POWER      = 5      # W measured power proving "really transmitting"
SWR_RETUNE_DEBOUNCE       = 1.5    # s continuously above the threshold
SWR_RETUNE_COOLDOWN       = 30.0   # s between two auto tunes
SWR_RETUNE_MAX_FAILS      = 3      # consecutive no-improvement tunes per frequency
SWR_RETUNE_COMPARE_SETTLE = 0.8    # s after tuning clears before comparing SWR
SWR_RETUNE_IMPROVED       = 0.02   # minimum improvement required to write back
```

Changed constant:

| Constant | Was | Now | Why |
| --- | --- | --- | --- |
| `LEARN_MIN_POWER` | 5 W | **3 W** | QRP-friendly (sibling V5.8.5). Idle METER leakage reads ~1–2 W, so 3 W still cannot mis-learn. |

Unchanged by design: `LEARN_SWR_MAX` (1.8), `LEARN_WINDOW_SIZE` (4), `LEARN_SWR_STABILITY`
(0.08), `LEARN_IGNORE_WINDOW` (1.0), `LEARN_DEDUP_COOLDOWN` (5.0), `LEARN_FREQ_STEP` (1000),
`ATR_TUNE_SWR_SKIP` (1.6) and every other manual-assist constant in `server.py`.

## 4. Guard: state and data flow (`atr1000_client.py`)

**New instance state** (added to `__init__`, next to the existing `_pending_freq` field — no
module-level globals, no locks; every read and write happens on the worker task):

| Field | Meaning |
| --- | --- |
| `_swr_high_since: float` | monotonic start of the current high-SWR run (0.0 = not in one) |
| `_swr_high_freq: int` | frequency the current run belongs to (freq change resets the run) |
| `_last_retune_at: float` | monotonic timestamp of the last auto tune (cooldown) |
| `_retune_fail_count: dict` | `freq_khz → consecutive no-improvement count` |
| `_pending_tune_mode: Optional[int]` | worker-side request, consumed by `_poll_loop` |
| `_auto_tune: Optional[dict]` | pending comparison snapshot `{freq, swr_before, relays, count, reason}` — `relays` is diagnostic only (there is no rollback) |
| `_auto_tune_compare_at: float` | monotonic deadline for the post-tune comparison (0.0 = none) |
| `on_tune_event: Optional[Callable[[dict], None]]` | sync callback for UI-visible auto-tune phases |

**Flow** (no new task, no new endpoint):

1. `_handle_meter()` — after the tuning auto-clear and `_emit_change()`, before the learning
   branch — calls the new sync `_check_swr_retune(swr, power)`.
2. `_check_swr_retune()` decision order:
   - `_tuning` is true, **or** `power < SWR_RETUNE_MIN_POWER`, **or** `_freq <= 0` → clear the run
     (the tuner is busy; a manual assist or an external scan is in progress).
   - `swr <= SWR_RETUNE_THRESHOLD` → clear the run **and** delete this frequency's failure count
     (SWR recovered → the frequency is eligible again).
   - frequency moved more than `LEARN_FREQ_STEP` since the run started → clear the run, start a new
     one at the new frequency (failure counts are per frequency and survive).
   - inside `LEARN_IGNORE_WINDOW` after a relay change → clear the run (readings not settled).
   - failure count already `>= SWR_RETUNE_MAX_FAILS` → announce `auto_giveup` **once per run**
     (guard against log/toast spam by re-arming `_swr_high_since`), return.
   - otherwise accumulate the run; fire only when `now - _swr_high_since >= SWR_RETUNE_DEBOUNCE`
     **and** `now - _last_retune_at >= SWR_RETUNE_COOLDOWN`.
3. On fire: increment the frequency's failure count, stamp `_last_retune_at`, clear the run, store
   the `_auto_tune` snapshot (pre-tune SWR, current relays, frequency, attempt number), set
   `_pending_tune_mode = 2` and `self._wake.set()`, and emit `auto_start` through `on_tune_event`.
4. `_poll_loop()` consumes `_pending_tune_mode` on its next tick and `await self.start_tune(2)` —
   the **same mechanism as the existing `_pending_freq`/`_apply_pending_freq()` path**, so all
   device I/O stays inside the worker (`_wake` makes the 1 s tick immediate; latency is
   milliseconds, not seconds).
5. `start_tune()` is unchanged: it sends `[FF 04 01 02]`, sets `_tuning`, and therefore suppresses
   both re-triggering (step 2's first bullet) and stable-window learning while the tuner works.

**Safety invariant:** the guard sends exactly one frame and holds no reference to CAT, PTT, the
audio path or `radio_state`. Self-keying is structurally impossible, not merely avoided by
convention; a source-guard test asserts that the guard does not mention `cat`/`ptt`/`set_tune`.

## 5. Learning gate update (sibling V5.8.5)

`_handle_meter()`'s learning branch currently requires the server to know about the transmission:

```python
if not (self._tx and not self._tuning and power > 0 and self._freq > 0):
    return
```

becomes

```python
if not (not self._tuning and power >= LEARN_MIN_POWER and self._freq > 0):
    return
```

Consequences:

- A transmission started at the **radio panel, its own mic PTT, or by external software** now
  learns — the server-side `notify_tx()` signal is no longer a precondition for learning (it still
  drives SYNC suppression, `_tx_started_at` and tuning reset, all unchanged).
- The power gate replaces `power > 0`, using the new 3 W `LEARN_MIN_POWER`.
- Existing ignore windows are preserved: the TX-start window when `_tx_started_at` is known, and
  the 1 s post-relay-change window. The 4-sample stability window, the tiered dedup and the
  storage learn gate are untouched.

## 6. Post-tune write-back

1. `_clear_tuning(reason)` — reached from any of the four existing paths (relay stable > 5 s,
   same-relay confirm > 1.5 s, TX end, 45 s hard timeout) — additionally records the reason in the
   pending `_auto_tune` snapshot and, if no comparison is scheduled yet, sets
   `_auto_tune_compare_at = now + SWR_RETUNE_COMPARE_SETTLE` (0.8 s, so the METER stream has a
   fresh reading for the relays the tuner landed on). No other `_clear_tuning()` behaviour changes.
2. `_poll_loop()` compares once the deadline passes (single-shot, then state is cleared):
   - `reason == "45s hard timeout"` → `auto_timeout`.
   - `swr_before - _swr >= SWR_RETUNE_IMPROVED` **and** `0 < _swr <= LEARN_SWR_MAX` →
     `auto_success`; then `storage.learn(freq, sw, ind, cap, swr, force_update=True)` with the
     **current** relay values (what the tuner landed on), skipping silently when no store is
     available. Failures inside `learn()` are logged and contained, exactly like `_maybe_learn()`.
   - anything else → `auto_no_improve`. **Relays are never restored** (decision 3).
   - Every branch emits through `on_tune_event` (and therefore reaches the UI) and clears
     `_auto_tune` / `_auto_tune_compare_at`.

`_retune_fail_count` needs no eviction policy: an entry is only created when a retune actually
fires, and entries are removed when that frequency's SWR recovers, so the dict is bounded by the
number of frequencies that fired an auto tune in one process lifetime.
3. Independent of this path, the normal stable-window learning resumes as soon as `_tuning`
   clears, so a long transmission also refreshes the store through the usual route.

## 7. Server and frontend

**Server (`server.py`)** — two small additions, no protocol change:

- `_on_atr_tune_event(ev)` mirrors `_on_atr_change()`: schedule
  `_broadcast_atr({"type": "atrTuneResult", "auto": True, **ev})` on the running loop, exceptions
  contained. Wired in the lifespan right after `atr.on_change = _on_atr_change`.
- `_start_atr_tune_assist()` gains one precondition before launching: `atr.read_state().get("tuning")`
  → reply `{"type":"error","message":"Tuner is already tuning"}` and return. This closes the ≤500 ms
  window in which polled `radio.is_transmitting` has not yet caught up with an in-flight auto tune.
  The reverse direction needs nothing new: while the manual assist runs, its `start_tune()` has set
  `_tuning`, which is the guard's first skip condition.

**Frontend (`static/modules/atr1000.js`)** — `onTuneResult()` learns five auto phases (the manual
phases are unchanged):

| Phase | Toast | Button |
| --- | --- | --- |
| `auto_start` | `ATR: SWR x.x 自动调谐中…` | `···`, click ignored |
| `auto_success` | `ATR 自动调谐完成: SWR a → b` | back to `TUNE` |
| `auto_no_improve` | `ATR 自动调谐无改善 (SWR a → b)` | back to `TUNE` |
| `auto_timeout` | `ATR 自动调谐超时 (SWR a)` | back to `TUNE` |
| `auto_giveup` | `ATR 连续 3 次无改善，已放弃该频点自动调谐` | back to `TUNE` |

No HTML or CSS change; `sw.js` gets the `atr1000.js` cache-version bump so the new switch ships.

## 8. Tests

`tests/test_atr1000_client.py` (guard, all hardware-free — drive `_handle_meter` directly and use
`unittest.mock` for the clock where a test needs to advance time):

1. Debounce: SWR 2.4 at 9 W fires nothing before 1.5 s and queues `_pending_tune_mode = 2` after.
2. Cooldown: a second run crossing the threshold within 30 s does not fire; after 30 s it does.
3. Low power: SWR 3.0 at 2 W never fires (idle leakage / tuner scan).
4. Tuning suppression: with `_tuning` true, no trigger and the run is cleared.
5. Frequency reset: crossing 1 kHz resets the run (no trigger from two half-debounce segments).
6. Recovery: SWR ≤ 2.0 clears the failure count, so the next high-SWR episode can fire again.
7. Fail cap: three no-improvement tunes → `auto_giveup` announced once, no fourth fire; a frequency
   change re-arms it.
8. `_poll_loop` consumption: `_pending_tune_mode` is turned into one `start_tune(2)` call.
9. Learning regression (V5.8.5): with `_tx` false and 4 W measured power, a full stable window
   still reaches `storage.learn()`; 2 W does not.
10. Write-back success: improved ≥ 0.02 and final SWR ≤ 1.8 → `learn(force_update=True)` with the
    tuned relays.
11. Write-back refusal: no improvement, or final SWR > 1.8 → no `learn()` call **and** relays
    unchanged (`set_relay` never called).
12. Timeout path: `_clear_tuning("45s hard timeout")` still produces `auto_timeout` after settle.
13. Source guards: the guard never references CAT/PTT (`cat`/`set_tune`/`set_ptt` absent from the
    added code region), and `atr1000_client.py` still imports nothing from the audio path.

`tests/test_atr1000_server.py`:

14. `_on_atr_tune_event` broadcasts `atrTuneResult` with `auto: true` and the phase payload.
15. `_start_atr_tune_assist()` refuses with the new "Tuner is already tuning" error when
    `read_state()["tuning"]` is true.
16. Frontend switch guard: `atr1000.js` handles all five auto phases.

`tests/README.md` module/test counts updated.

## 9. Documentation sync (sdd-guardian Phase 5)

| Document | Change |
| --- | --- |
| `SDD/09-architecture-overview.md` §9.8 | New linkage behaviour 4 (auto full tune + write-back) and the parameter table; note that auto tune never keys the radio |
| `SDD/15-ptt-safety-architecture.md` | One line: the auto guard keys nothing — it only acts while the operator transmits (measured power ≥ 5 W), and only sends a tuner frame |
| `SDD/14-version-history.md` | New version entry describing the change, the tests and the sibling reference |
| `SDD/README.md` | Quick Facts version bump |
| `AGENTS.md` | `atr1000_client.py` module-table row (auto-retune guard + power-based learning) |
| `README.md` | ATR behaviour sentence in the features/env area (only if that section already describes ATR linkage) |
| `CHANGELOG.md` | New top entry under the next version |
| `tests/README.md` | Module/test counts |

## 10. Out of scope

- No enable/disable environment variable (decision 5), no settings-panel toggle.
- No change to `ATR_TUNE_*` / `_atr_tune_assist()` behaviour beyond the new precondition; the manual
  TUNE button keeps its TX2 carrier, ≤ 1.6 skip gate, ≥ 0.02 improvement test and rollback.
- No change to `atr1000_tuner.py` semantics (learn gate 1.0–1.8, 1 kHz keys, ±5 kHz nearest, atomic
  writes), to `/WSatr1000`'s message schema, or to the audio/CAT/spectrum paths.
- No auto-tune when no transmission is in progress — ever (decision 1).
