# IC-7300 / IC-7300MK2 CI-V Conformance Hardening Implementation Plan

> **For AI agent workers:** Required sub-skill: use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Track progress with the checkboxes below.

**Goal:** Make the existing IC-7300 family backend match Icom's documented scope, Transceive, power, ALC, and scope-speed behavior without changing FT-710 or audio processing.

**Architecture:** Keep framing and scope interpretation in the pure `civ_codec` layer, transport semantics in `CivController`, and model differences in `IC7300Backend`/`IC7300MK2Backend`. Extend the existing capability and state-table injection surfaces rather than adding model checks to shared server code. Validate every change first with a failing byte-level or source-contract regression.

**Tech stack:** Python 3.13, `unittest`/`IsolatedAsyncioTestCase`, `unittest.mock`, asyncio, pyserial abstraction, FastAPI backend interfaces, vanilla JavaScript, SDD Guardian.

**Execution status:** Tasks 1–7 complete. Full suite passes: 633 tests across 31 modules; syntax, SDD Guardian, and diff checks are clean.

---

## File Structure

- Modify `backends/ic7300/civ_codec.py`: interpret SCROLL-C information fields as lower/upper edges.
- Modify `backends/ic7300/civ_controller.py`: make Transceive item explicit and emit documented power-on preambles.
- Modify `backends/ic7300/backend.py`: select model-specific Transceive items, send the complete scope-init sequence, use a documented liveness query, expose ALC/speed metadata.
- Modify `backends/ic7300/config_ic7300.py`: provide IC ALC percentage conversion.
- Modify `backends/base.py`: add the backend-neutral `scope_speeds` capability.
- Modify `radio_state.py`: inject backend-specific ALC percentage conversion while preserving the FT-710 default.
- Modify `_diag_ic7300_scope.py`: use the complete official scope-enable sequence.
- Modify `static/ft710_ui.js`: rebuild the CI-V speed selector from capabilities.
- Modify `static/index.html` and `static/sw.js`: bump UI cache versions.
- Modify `tests/test_civ_codec.py`: official Center/SCROLL-C/Fixed metadata vectors.
- Modify `tests/test_civ_controller.py`: model/address separation and power-frame vectors.
- Modify `tests/test_ic7300_runtime_reliability.py`: scope-init, diagnostic, liveness, ALC, and capabilities regressions.
- Modify `tests/test_server_ws_protocol.py`: frontend speed-selector and cache-version contracts.
- Modify `IC-7300MK2_CI-V_Knowledge_Base.md`, `IC-7300_硬件验收清单.md`, `README.md`, `docs/OPERATION_GUIDE.md`, `AGENTS.md`, `tests/README.md`, `SDD/09-architecture-overview.md`, `SDD/12-operational-model.md`, `SDD/13-feasibility-assessment.md`, `SDD/14-version-history.md`, and `SDD/README.md`: synchronize protocol facts, operator prerequisites, verification limits, and exact test totals.

### Task 1: Correct scope metadata parsing

**Files:**
- Modify: `tests/test_civ_codec.py:240-281`
- Modify: `backends/ic7300/civ_codec.py:304-348`

- [ ] **Step 1: Add failing Center and SCROLL-C vectors**

Add tests which construct sequence-1 information chunks with `encode_freq_bcd()`:

```python
def test_center_info_uses_center_and_half_span(self):
    data = (bytes((0x00, 0x00, 0x01, 0x11, SCOPE_MODE_CENTER))
            + encode_freq_bcd(14_200_000)
            + encode_freq_bcd(100_000)
            + b"\x00")
    seg = self._parse(data)
    self.assertEqual(seg.center_freq_hz, 14_200_000)
    self.assertEqual(seg.span_hz, 100_000)
    self.assertIsNone(seg.low_edge_hz)
    self.assertIsNone(seg.high_edge_hz)


def test_scroll_c_info_uses_low_and_high_edges(self):
    data = (bytes((0x00, 0x00, 0x01, 0x11, SCOPE_MODE_SCROLL_C))
            + encode_freq_bcd(14_100_000)
            + encode_freq_bcd(14_300_000)
            + b"\x00")
    seg = self._parse(data)
    self.assertEqual(seg.low_edge_hz, 14_100_000)
    self.assertEqual(seg.high_edge_hz, 14_300_000)
    self.assertIsNone(seg.center_freq_hz)
    self.assertIsNone(seg.span_hz)
```

Import `SCOPE_MODE_CENTER`, `SCOPE_MODE_SCROLL_C`, and `encode_freq_bcd` from `civ_codec`.

- [ ] **Step 2: Run the focused tests and confirm the SCROLL-C assertion fails**

Run:

```bash
../../venv/bin/python -m unittest tests.test_civ_codec.ScopeSegmentTests -v
```

Expected: Center passes; SCROLL-C fails because the current parser populates `center_freq_hz`/`span_hz`.

- [ ] **Step 3: Apply the smallest parser correction**

Change the sequence-1 branch so only `SCOPE_MODE_CENTER` uses center/span:

```python
if seg.scope_mode == SCOPE_MODE_CENTER:
    seg.center_freq_hz = decode_freq_bcd(d[5:10])
    seg.span_hz = decode_freq_bcd(d[10:15])
else:
    seg.low_edge_hz = decode_freq_bcd(d[5:10])
    seg.high_edge_hz = decode_freq_bcd(d[10:15])
```

Update the docstring to say Center has center/half-span and Fixed/SCROLL-C/SCROLL-F have lower/upper edges.

- [ ] **Step 4: Run codec tests**

```bash
../../venv/bin/python -m unittest tests.test_civ_codec -v
```

Expected: all codec tests pass.

- [ ] **Step 5: Commit**

```bash
git add backends/ic7300/civ_codec.py tests/test_civ_codec.py
git commit -m "fix: decode IC-7300 SCROLL-C scope edges"
```

### Task 2: Send the complete scope activation sequence

**Files:**
- Modify: `tests/test_ic7300_runtime_reliability.py`
- Modify: `backends/ic7300/backend.py:237-264`
- Modify: `_diag_ic7300_scope.py:52-240`

- [ ] **Step 1: Add failing backend and diagnostic tests**

Create a recording scope fake and assert exact call order:

```python
class _RecordingScopeCiv:
    connected = True

    def __init__(self):
        self.calls = []

    async def set_scope_on(self, on):
        self.calls.append(("display", on))

    async def set_scope_mode(self, mode):
        self.calls.append(("mode", mode))

    async def set_scope_span(self, span):
        self.calls.append(("span", span))

    async def set_scope_data_output(self, on):
        self.calls.append(("data", on))


class ScopeActivationTests(unittest.IsolatedAsyncioTestCase):
    async def test_backend_enables_display_before_data_output(self):
        backend = IC7300Backend("/dev/null")
        fake = _RecordingScopeCiv()
        backend._civ = fake
        await backend.init_scope()
        self.assertEqual(fake.calls, [
            ("display", True),
            ("mode", 0),
            ("span", DEFAULT_SCOPE_SPAN),
            ("data", True),
        ])

    def test_diagnostic_scope_frames_match_official_order(self):
        frames = diag.scope_enable_frames(0x94)
        self.assertEqual(frames, [
            build_frame(0x27, b"\x10\x01", to=0x94),
            build_frame(0x27, b"\x14\x00", to=0x94),
            build_frame(0x27, b"\x15\x05", to=0x94),
            build_frame(0x27, b"\x11\x01", to=0x94),
        ])
```

Use the diagnostic's default ±100 kHz span code (`0x05`).

- [ ] **Step 2: Run and confirm failures**

```bash
../../venv/bin/python -m unittest tests.test_ic7300_runtime_reliability.ScopeActivationTests -v
```

Expected: backend order lacks `display`; diagnostic helper does not exist.

- [ ] **Step 3: Implement scope activation**

In `IC7300Backend.init_scope()`, add `("display on", civ.set_scope_on(True))` before mode/span/data. Rewrite the docstring to state both `27 10 01` and `27 11 01` are required.

In `_diag_ic7300_scope.py`, add:

```python
def scope_enable_frames(civ_to: int) -> list[bytes]:
    return [
        build_frame(0x27, b"\x10\x01", to=civ_to),
        build_frame(0x27, b"\x14\x00", to=civ_to),
        build_frame(0x27, b"\x15\x05", to=civ_to),
        build_frame(0x27, b"\x11\x01", to=civ_to),
    ]
```

Write each returned frame before `flush()`. Keep the existing `27 11 00` best-effort shutdown.

- [ ] **Step 4: Run focused tests**

```bash
../../venv/bin/python -m unittest tests.test_ic7300_runtime_reliability -v
```

Expected: all runtime reliability tests pass.

- [ ] **Step 5: Commit**

```bash
git add _diag_ic7300_scope.py backends/ic7300/backend.py tests/test_ic7300_runtime_reliability.py
git commit -m "fix: enable both IC-7300 scope switches"
```

### Task 3: Correct model-specific Transceive and power semantics

**Files:**
- Modify: `tests/test_civ_controller.py`
- Modify: `tests/test_ic7300_runtime_reliability.py`
- Modify: `backends/ic7300/civ_controller.py:150-192,819-821`
- Modify: `backends/ic7300/backend.py:20-35,63-64,212-217,513-526`

- [ ] **Step 1: Add failing Transceive tests**

Change controller construction to take an explicit `transceive_cmd` and test address independence:

```python
async def test_custom_mk2_address_keeps_mk2_transceive_item(self):
    ctl = CivController("/dev/fake", civ_addr=0xA2,
                        transceive_cmd=SETMODE_CIV_TRANSCEIVE_MK2,
                        query_timeout=0.05)
    # connect with FakeSerial, then assert 00 89 01 is present and 00 71 is absent


async def test_regular_model_does_not_infer_mk2_from_address(self):
    ctl = CivController("/dev/fake", civ_addr=MK2_CIV_ADDR,
                        transceive_cmd=SETMODE_CIV_TRANSCEIVE_ON,
                        query_timeout=0.05)
    # connect, then assert 00 71 01 is present
```

Also assert `IC7300MK2Backend(...)._civ._transceive_cmd == SETMODE_CIV_TRANSCEIVE_MK2`.

- [ ] **Step 2: Add failing power tests**

Test a pure frame builder and backend liveness:

```python
def test_power_on_preamble_counts_match_icom_table(self):
    for baud, count in {115200: 150, 57600: 75, 38400: 50,
                        19200: 25, 9600: 13, 4800: 7}.items():
        raw = build_power_on_frame(baud, 0x94)
        self.assertEqual(len(raw) - len(raw.lstrip(b"\xfe")), count)
        self.assertTrue(raw.endswith(bytes.fromhex("94 E0 18 01 FD")))


def test_unknown_baud_uses_standard_frame(self):
    self.assertEqual(build_power_on_frame(230400, 0x94),
                     build_frame(0x18, b"\x01", to=0x94))


async def test_power_health_uses_frequency_query(self):
    backend = IC7300Backend("/dev/null")
    backend._civ.get_frequency = AsyncMock(return_value=14_200_000)
    backend._civ._query_data = AsyncMock()
    self.assertIs(await backend._get_power_on(timeout=0.4), True)
    backend._civ.get_frequency.assert_awaited_once_with(timeout=0.4)
    backend._civ._query_data.assert_not_awaited()
```

- [ ] **Step 3: Run and confirm failures**

```bash
../../venv/bin/python -m unittest tests.test_civ_controller tests.test_ic7300_runtime_reliability -v
```

Expected: explicit constructor argument and power builder are absent; `_get_power_on()` calls bare `18`.

- [ ] **Step 4: Implement explicit Transceive selection**

Add `transceive_cmd: bytes = SETMODE_CIV_TRANSCEIVE_ON` to `CivController.__init__()` and assign it directly. Import `CIV_ADDR` and both Transceive constants in `backend.py`; construct the regular controller with item `0071` and MK2 with item `0089`, regardless of address.

- [ ] **Step 5: Implement documented power behavior**

Add:

```python
POWER_ON_FE_COUNTS = {
    115200: 150, 57600: 75, 38400: 50,
    19200: 25, 9600: 13, 4800: 7,
}


def build_power_on_frame(baudrate: int, civ_addr: int) -> bytes:
    standard = build_frame(CMD_POWER, b"\x01", to=civ_addr)
    count = POWER_ON_FE_COUNTS.get(baudrate)
    if count is None:
        logger.warning("No documented CI-V power-on preamble for %d baud", baudrate)
        return standard
    return bytes((0xFE,)) * (count - 2) + standard
```

Have `set_power(True)` write this raw frame under the existing lock and error policy. Keep `set_power(False)` delegated to `send_set_command()`.

Change `_get_power_on()` to:

```python
freq = await self._civ.get_frequency(timeout=timeout)
return True if freq is not None else None
```

- [ ] **Step 6: Run focused tests**

```bash
../../venv/bin/python -m unittest tests.test_civ_controller tests.test_ic7300_runtime_reliability tests.test_power_switch -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add backends/ic7300/civ_controller.py backends/ic7300/backend.py tests/test_civ_controller.py tests/test_ic7300_runtime_reliability.py
git commit -m "fix: align IC-7300 model and power commands"
```

### Task 4: Inject the IC ALC scale without changing FT-710

**Files:**
- Modify: `tests/test_radio_state.py`
- Modify: `backends/ic7300/config_ic7300.py:180-190`
- Modify: `backends/ic7300/backend.py:20-35,129-143`
- Modify: `radio_state.py:15-55,149-221`

- [ ] **Step 1: Add failing calibration tests**

```python
def test_ft710_default_alc_scale_is_unchanged(self):
    state = RadioState(alc_meter=120)
    self.assertAlmostEqual(state.alc_pct, 120 / 255 * 100)


def test_ic7300_alc_uses_official_full_scale(self):
    state = RadioState()
    state.configure(**IC7300Backend("/dev/null").state_tables())
    state.alc_meter = 60
    self.assertEqual(state.alc_pct, 50.0)
    state.alc_meter = 120
    self.assertEqual(state.alc_pct, 100.0)
    state.alc_meter = 241
    self.assertEqual(state.alc_pct, 100.0)
```

- [ ] **Step 2: Run and confirm the IC assertions fail**

```bash
../../venv/bin/python -m unittest tests.test_radio_state.RadioStateConfigureTests -v
```

Expected: IC raw 120 still maps to approximately 47.06%.

- [ ] **Step 3: Implement backend-injected ALC conversion**

Add a default converter preserving the existing FT result:

```python
def _default_alc_pct(raw: int) -> float:
    return max(0.0, min(100.0, raw / 255.0 * 100.0))
```

Store it as `raw_to_alc_pct` in `_default_tables()` and use it in `alc_pct`. In `config_ic7300.py`, add:

```python
def raw_to_alc_pct(raw: int) -> float:
    return max(0.0, min(100.0, raw_to_alc(raw) * 100.0))
```

Inject that function from `IC7300Backend.state_tables()` and update `RadioState.configure()` documentation.

- [ ] **Step 4: Run state and config tests**

```bash
../../venv/bin/python -m unittest tests.test_radio_state tests.test_config_ic7300 -v
```

Expected: all pass and FT default assertions remain unchanged.

- [ ] **Step 5: Commit**

```bash
git add radio_state.py backends/ic7300/config_ic7300.py backends/ic7300/backend.py tests/test_radio_state.py
git commit -m "fix: calibrate IC-7300 ALC percentage"
```

### Task 5: Constrain CI-V scope speed in the browser

**Files:**
- Modify: `tests/test_ic7300_runtime_reliability.py`
- Modify: `tests/test_server_ws_protocol.py:176-220`
- Modify: `backends/base.py:31-57`
- Modify: `backends/ic7300/backend.py:67-86`
- Modify: `static/ft710_ui.js:1080-1110`
- Modify: `static/index.html:510`
- Modify: `static/sw.js:1-10`

- [ ] **Step 1: Add failing capability and source-contract tests**

```python
def test_ic_scope_speeds_are_only_fast_mid_slow(self):
    caps = IC7300Backend("/dev/null").capabilities.to_dict()
    self.assertEqual(caps["scope_speeds"], ["FAST", "MID", "SLOW"])


def test_civ_scope_speed_selector_uses_capabilities(self):
    source = Path("static/ft710_ui.js").read_text()
    self.assertIn("c.scope_speeds", source)
    self.assertIn("speedSel.innerHTML = ''", source)
```

Update cache assertions from UI `v=26`/cache `mrrc-v26` to `v=27`/`mrrc-v27` before implementation so the cache test also fails red.

- [ ] **Step 2: Run and confirm failures**

```bash
../../venv/bin/python -m unittest tests.test_ic7300_runtime_reliability tests.test_server_ws_protocol.StateBroadcastLogicTests -v
```

Expected: `scope_speeds` is absent, source contract is absent, and cache versions are still 26.

- [ ] **Step 3: Add capability and selector rebuilding**

Add `scope_speeds: tuple = ()` to `RadioCapabilities` and `scope_speeds=("FAST", "MID", "SLOW")` to the IC capabilities.

Extend `_rebuildSpanSelect(c)` after rebuilding CI-V spans:

```javascript
const speedSel = document.getElementById('scope-speed-select');
if (speedSel && Array.isArray(c.scope_speeds) && c.scope_speeds.length) {
    speedSel.innerHTML = '';
    c.scope_speeds.forEach(function(name, idx) {
        const opt = document.createElement('option');
        opt.value = String(idx);
        opt.textContent = name;
        speedSel.appendChild(opt);
    });
    speedSel.value = String(radioState.scope_speed);
}
```

Keep static five-speed options as the FT-710 fallback.

- [ ] **Step 4: Bump cache versions**

Change `/ft710_ui.js?v=26` to `v=27` in `static/index.html` and `static/sw.js`, and change `CACHE = 'mrrc-v26'` to `mrrc-v27`.

- [ ] **Step 5: Run focused tests**

```bash
../../venv/bin/python -m unittest tests.test_ic7300_runtime_reliability tests.test_server_ws_protocol -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add backends/base.py backends/ic7300/backend.py static/ft710_ui.js static/index.html static/sw.js tests/test_ic7300_runtime_reliability.py tests/test_server_ws_protocol.py
git commit -m "fix: constrain IC-7300 scope speeds"
```

### Task 6: Synchronize protocol and operational documentation

**Files:**
- Modify: `IC-7300MK2_CI-V_Knowledge_Base.md`
- Modify: `IC-7300_硬件验收清单.md`
- Modify: `README.md`
- Modify: `docs/OPERATION_GUIDE.md`
- Modify: `AGENTS.md`
- Modify: `tests/README.md`
- Modify: `SDD/09-architecture-overview.md`
- Modify: `SDD/12-operational-model.md`
- Modify: `SDD/13-feasibility-assessment.md`
- Modify: `SDD/14-version-history.md`
- Modify: `SDD/README.md`

- [ ] **Step 1: Update protocol facts**

Record that waveform output requires `27 10 01` and `27 11 01`, SCROLL-C carries lower/upper edges, model—not address—selects item `0071`/`0089`, bare `18` is not used as a read, and IC ALC full scale is raw 120. Replace any knowledge-base statement that power-health command `18` remains unresolved with the documented `03` liveness policy.

- [ ] **Step 2: Update operator prerequisites and acceptance limits**

State exactly:

```text
CI-V USB Port = Unlink from [REMOTE]
CI-V USB Baud Rate = 115200 (explicitly selected; not Auto)
IC-7300 address = 0x94 by default
IC-7300MK2 address = 0xB6 by default
```

Keep USB driver, ACK timing, waveform cadence, RF/tuner/power-cycle behavior, and RX/TX audio quality marked as requiring physical hardware.

- [ ] **Step 3: Update SDD version and exact test totals**

Run the complete suite after code Tasks 1–5, capture its exact `Ran N tests` line, and count the discovered `test_*.py` modules. Add SDD V2.25 dated 2026-08-26 and use those observed totals consistently in `SDD/14-version-history.md`, `SDD/README.md`, `README.md`, `AGENTS.md`, and `tests/README.md`; do not estimate.

- [ ] **Step 4: Run documentation checks**

```bash
rg -n "CI-V USB Port|CI-V USB Baud Rate|27 10 01|27 11 01|SCROLL-C|V2.25" \
  IC-7300MK2_CI-V_Knowledge_Base.md IC-7300_硬件验收清单.md README.md \
  docs/OPERATION_GUIDE.md SDD tests/README.md
git diff --check
```

Expected: all required facts are present and `git diff --check` is silent.

- [ ] **Step 5: Commit**

```bash
git add IC-7300MK2_CI-V_Knowledge_Base.md IC-7300_硬件验收清单.md README.md \
  docs/OPERATION_GUIDE.md AGENTS.md tests/README.md SDD
git commit -m "docs: document IC-7300 CI-V conformance"
```

### Task 7: Final verification and integration

**Files:**
- Verify all changed files.

- [ ] **Step 1: Run proactive diagnostics**

Run primary LSP diagnostics on all changed Python and JavaScript files. Fix only diagnostics introduced by this branch; list pre-existing project findings separately.

- [ ] **Step 2: Run syntax checks**

```bash
../../venv/bin/python -m py_compile \
  _diag_ic7300_scope.py radio_state.py backends/base.py \
  backends/ic7300/backend.py backends/ic7300/civ_codec.py \
  backends/ic7300/civ_controller.py backends/ic7300/config_ic7300.py
```

Expected: exit 0 and no output.

- [ ] **Step 3: Run the complete suite**

```bash
../../venv/bin/python -m unittest discover -s tests -v
```

Expected: all tests pass. Capture the exact `Ran N tests` line and update every documentation count from Task 6 if necessary, then rerun this command.

- [ ] **Step 4: Run repository guards**

```bash
git diff --check
../../venv/bin/python .agents/skills/sdd-guardian/harness/sdd_context.py check \
  _diag_ic7300_scope.py radio_state.py backends/base.py backends/ic7300 \
  static/ft710_ui.js static/index.html static/sw.js tests README.md AGENTS.md \
  IC-7300MK2_CI-V_Knowledge_Base.md IC-7300_硬件验收清单.md docs/OPERATION_GUIDE.md SDD
```

Expected: no whitespace errors and `SDD-GUARDIAN: clean`.

- [ ] **Step 5: Review the complete branch diff**

```bash
git status --short --branch
git diff main...HEAD --stat
git diff main...HEAD --check
```

Confirm the diff contains no audio-rate changes, no FT-710 behavior changes, no credentials/device paths, and no claim of completed hardware verification.

- [ ] **Step 6: Commit any final count-only documentation correction**

```bash
git add README.md AGENTS.md tests/README.md SDD
git commit -m "docs: sync IC-7300 conformance verification"
```

Skip this commit when the tree is already clean.

- [ ] **Step 7: Merge locally after verification**

Fast-forward `fix/ic7300-civ-conformance` into local `main`, rerun the complete suite from `main`, and remove the worktree/branch only after the merged verification succeeds. Do not push unless separately requested.
