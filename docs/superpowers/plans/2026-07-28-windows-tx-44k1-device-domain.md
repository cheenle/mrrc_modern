# Windows TX 44.1 kHz Device Domain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Windows TX keep the browser/Opus domain at 48 kHz while always converting and playing FT-710 device audio at 44.1 kHz, then produce a private v1.7.8 Win11 test installer.

**Architecture:** `AudioHandler` retains the existing 48 kHz WebSocket/Opus boundary and makes the FT-710 output boundary platform-independent: one 960-sample codec frame is always resampled to one 882-sample device frame. Windows device re-enumeration recovery and TX→RX capture reopen remain unchanged; only the WASAPI rate-promotion policy is removed. This restores AD-011 and SDD §9.4, serves UC-002/SC4, and preserves AD-004/NFR-061 Opus transport plus AD-008/NFR-065 device selection.

**Tech Stack:** Python 3.12, stdlib `unittest`, NumPy resampling, PyAudio/PortAudio, PowerShell/PyInstaller/Inno Setup, SDD Guardian.

---

## File map

- `tests/test_audio.py`: hardware-independent regression coverage for the fixed device-domain rate, 960→882 conversion, byte budgets, and PortAudio reinitialization.
- `audio_handler.py`: TX stream-open rate and codec-to-device conversion policy.
- `packaging/windows/MRRC-FT710.iss`: v1.7.8 installer metadata.
- `CHANGELOG.md`: operator-facing v1.7.8 fix and test status.
- `SDD/08-architecture-decisions.md`: withdraw the V2.9 Windows exception to AD-011.
- `SDD/09-architecture-overview.md`: make the 48 kHz codec / 44.1 kHz device invariant explicitly cross-platform.
- `SDD/14-version-history.md`, `SDD/README.md`: V2.14 behavior record and quick-facts bump.
- `AGENTS.md`, `README.md`: repository/runtime description synchronized with the implementation.
- `tests/README.md`: audio regression coverage and suite count.
- `docs/WINDOWS_INSTALLER_GUIDE.md`: v1.7.8 Windows audio behavior and setup guidance.

### Task 1: Lock the regression contract with failing tests

**Files:**
- Modify: `tests/test_audio.py:664-916`

- [ ] **Step 1: Replace obsolete WASAPI-policy tests with device-domain invariant tests**

Replace `WindowsWasapiTxTests` with:

```python
class TxDeviceDomainTests(unittest.TestCase):
    """SDD AD-011: Opus PCM is always converted from 48 kHz to the
    FT-710's fixed 44.1 kHz device domain (960→882 samples per 20 ms)."""

    def _feed_handler(self, rate=None):
        import threading
        from collections import deque
        from audio_handler import AudioHandler
        h = AudioHandler.__new__(AudioHandler)
        h._tx_stream = object()
        h._tx_queue = deque()
        h._tx_queued_bytes = 0
        h._tx_peak = 0
        h._tx_lock = threading.Lock()
        if rate is not None:
            h._tx_rate = rate
        return h

    def test_48k_frame_resamples_to_exact_44k1_frame(self):
        h = self._feed_handler()
        h.feed_tx_audio(b"\x01\x02" * 960)
        self.assertEqual(len(h._tx_queue[0]), 882 * 2)
        self.assertEqual(h._tx_queued_bytes, 1764)

    def test_stale_48k_rate_cannot_bypass_resampling(self):
        h = self._feed_handler(48000)
        frame = b"\x01\x02" * 960
        h.feed_tx_audio(frame)
        self.assertEqual(len(h._tx_queue[0]), 882 * 2)
        self.assertNotEqual(h._tx_queue[0], frame)

    def test_prebuffer_budget_is_derived_from_44100(self):
        from audio_handler import TX_PREBUFFER_BYTES, TX_PREBUFFER_MS
        self.assertEqual(TX_PREBUFFER_BYTES, 44100 * 2 * TX_PREBUFFER_MS // 1000)

    def test_max_buffer_budget_is_derived_from_44100(self):
        from audio_handler import TX_MAX_BUFFER_BYTES, TX_MAX_BUFFER_MS
        self.assertEqual(TX_MAX_BUFFER_BYTES, 44100 * 2 * TX_MAX_BUFFER_MS // 1000)
```

- [ ] **Step 2: Change the Windows end-to-end expectation to 44.1 kHz**

Rename the Windows test to `test_start_tx_keeps_selected_device_at_44100_on_win32` and assert:

```python
self.assertTrue(ok)
self.assertEqual(h._tx_rate, 44100)
self.assertEqual(h._pa.open_calls[0]["output_device_index"], 0)
self.assertEqual(h._pa.open_calls[0]["rate"], 44100)
self.assertEqual(h._pa.open_calls[0]["frames_per_buffer"], 882)
self.assertEqual(h._tx_prebuffer_bytes, 44100 * 2 * 60 // 1000)
self.assertEqual(h._tx_max_buffer_bytes, 44100 * 2 * 400 // 1000)
```

- [ ] **Step 3: Add a Windows reinitialization regression**

Add this test to `PortAudioReinitTests`:

```python
def test_start_tx_reinit_preserves_44100_when_enumeration_changes(self):
    from unittest.mock import patch
    self._patch_devices_empty()
    try:
        bad = _FailingOpenPyAudio(
            [_dev("USB Audio Device", inputs=1, outputs=2, rate=44100, host_api=0),
             _dev("USB Audio Device", inputs=1, outputs=2, rate=48000, host_api=1)],
            ["MME", "Windows WASAPI"],
        )
        good = _FakePyAudioOpen(
            [_dev("Built-in Speakers", outputs=2, rate=48000, host_api=0),
             _dev("USB Audio Device", inputs=1, outputs=2, rate=48000, host_api=1)],
            ["MME", "Windows WASAPI"],
        )
        h = self._make_handler(bad)

        def fake_reinit():
            h._pa = good

        h._reinit_pyaudio = fake_reinit
        with patch("sys.platform", "win32"):
            self.assertTrue(h.start_tx())
        self.assertEqual(len(bad.open_calls), 3)
        self.assertEqual(good.open_calls[0]["output_device_index"], 1)
        self.assertEqual(good.open_calls[0]["rate"], 44100)
        self.assertEqual(good.open_calls[0]["frames_per_buffer"], 882)
    finally:
        self._restore_devices()
```

- [ ] **Step 4: Run the focused tests and confirm the regression is red**

Run:

```bash
venv/bin/python -m unittest \
  tests.test_audio.TxDeviceDomainTests \
  tests.test_audio.StartTxWindowsTests \
  tests.test_audio.PortAudioReinitTests -v
```

Expected: failures show the current Windows path selecting device 1/rate 48000 and the current feed path retaining 1920 bytes when `_tx_rate == 48000`; unrelated tests pass.

- [ ] **Step 5: Commit the red regression tests**

```bash
git add tests/test_audio.py
git commit -m "test: lock Windows TX to 44.1 kHz device domain"
```

### Task 2: Remove Windows rate promotion and always bridge 48→44.1 kHz

**Files:**
- Modify: `audio_handler.py:12-22,514-753`

- [ ] **Step 1: Remove the WASAPI rate-selection helper and unused imports**

Delete `_wasapi_tx_variant()`, remove `import sys`, and change the resampler import to:

```python
from audio_resample import resample_441_to_48, resample_48_to_441
```

- [ ] **Step 2: Make `start_tx()` establish one fixed device-domain rate**

Replace the Windows rate-selection block with:

```python
self._tx_rate = TX_SAMPLE_RATE
self._tx_prebuffer_bytes = TX_PREBUFFER_BYTES
self._tx_max_buffer_bytes = TX_MAX_BUFFER_BYTES
```

Open the stream with:

```python
stream = self._pa.open(
    format=pyaudio.paInt16,
    channels=TX_CHANNELS,
    rate=TX_SAMPLE_RATE,
    output=True,
    output_device_index=dev,
    frames_per_buffer=RX_CHUNK_SIZE,
)
```

Log `TX_SAMPLE_RATE`, and after `_reinit_pyaudio()` only call `_find_tx_device()`; do not derive a rate or switch host-API entries.

- [ ] **Step 3: Make `feed_tx_audio()` unconditionally cross the SRC boundary**

Replace its conditional rate handling with:

```python
data = resample_48_to_441(pcm)
```

Keep queue locking, peak tracking, oldest-frame dropping, graceful drain, PortAudio recovery, and Windows `restart_rx()` unchanged.

- [ ] **Step 4: Run the focused tests and confirm green**

Run:

```bash
venv/bin/python -m unittest \
  tests.test_audio.TxDeviceDomainTests \
  tests.test_audio.StartTxWindowsTests \
  tests.test_audio.PortAudioReinitTests -v
```

Expected: 10 tests pass; Windows opens the selected FT-710 entry at 44100 Hz with 882 frames per buffer, including after re-enumeration.

- [ ] **Step 5: Run all audio tests**

Run: `venv/bin/python -m unittest tests.test_audio -v`

Expected: 73 audio tests pass.

- [ ] **Step 6: Commit the production fix**

```bash
git add audio_handler.py
git commit -m "fix: keep Windows TX device audio at 44.1 kHz"
```

### Task 3: Synchronize v1.7.8 release and architecture documentation

**Files:**
- Modify: `packaging/windows/MRRC-FT710.iss`
- Modify: `CHANGELOG.md`
- Modify: `SDD/08-architecture-decisions.md`
- Modify: `SDD/09-architecture-overview.md`
- Modify: `SDD/14-version-history.md`
- Modify: `SDD/README.md`
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `tests/README.md`
- Modify: `docs/WINDOWS_INSTALLER_GUIDE.md`

- [ ] **Step 1: Bump private test-package metadata to v1.7.8**

Change:

```iss
#define MyAppVersion "1.7.8"
```

Add a v1.7.8 changelog entry dated 2026-07-28 stating that Windows now always converts 48 kHz/960-sample Opus PCM to 44.1 kHz/882-sample FT-710 PCM, that the V2.9 WASAPI exception was withdrawn because the VM/KVM mix rate was not the radio hardware clock, that PortAudio recovery and TX→RX reopen remain, and that RF audio quality awaits the operator's hardware test.

- [ ] **Step 2: Restore AD-011 as a cross-platform invariant**

Append an amendment after the V2.9 paragraph in `SDD/08-architecture-decisions.md`:

```markdown
**Amended (V2.14, supersedes V2.9)**: Windows follows the same fixed boundary as every other platform: browser/Opus remains 48 kHz and the FT-710 PyAudio device stream remains 44.1 kHz. The prior WASAPI `defaultSampleRate` was a Windows shared-mode mix rate, not evidence of the radio's USB hardware clock; the KVM pacing result is retained as incident evidence but withdrawn as a device-rate policy. `feed_tx_audio()` therefore always performs 960→882 SRC. PortAudio reinitialization and the Windows TX→RX capture reopen workaround remain unchanged.
```

In `SDD/09-architecture-overview.md` add “on all platforms, including Windows” to the §9.4 boundary explanation.

- [ ] **Step 3: Record SDD V2.14 and synchronize repository guidance**

Add a top `SDD V2.14` row describing the fixed-rate Windows TX correction, regression coverage, v1.7.8 test build, and deferred RF acceptance. Set `SDD/README.md` Quick Facts to `V2.14`, baseline date `2026-07-28`, and test-build status. Remove the Windows WASAPI 48 kHz exception from `AGENTS.md`; keep the 44.1↔48 kHz bridge and recovery behavior. Confirm `README.md` already describes 48→44.1 and add only a concise Windows invariant where needed.

- [ ] **Step 4: Synchronize test and Windows installer documentation**

Keep `tests/README.md` at 435 total/73 audio tests, rename `WindowsWasapiTxTests` to `TxDeviceDomainTests` with four invariant tests, update `StartTxWindowsTests` to selected-device 44.1 kHz coverage, and update `PortAudioReinitTests` to four tests. Change the installer guide to v1.7.8, remove “native WASAPI 48 kHz TX path”, and state that any selected FT-710 output entry is opened at 16-bit/44.1 kHz while codec transport remains 48 kHz.

- [ ] **Step 5: Trace the plan and run documentation-focused checks**

Run:

```bash
python3 .agents/skills/sdd-guardian/harness/sdd_context.py trace \
  docs/superpowers/plans/2026-07-28-windows-tx-44k1-device-domain.md
rg -n "WASAPI 48|WASAPI@48|passes 48|pass.*48|device-domain rate is host" \
  audio_handler.py AGENTS.md README.md tests/README.md docs/WINDOWS_INSTALLER_GUIDE.md \
  SDD/08-architecture-decisions.md SDD/09-architecture-overview.md
```

Expected: trace resolves AD-008, AD-011, NFR-065 and SC4; remaining WASAPI-48 text appears only in clearly superseded V2.9 history/incident context.

- [ ] **Step 6: Commit release documentation**

```bash
git add packaging/windows/MRRC-FT710.iss CHANGELOG.md \
  SDD/08-architecture-decisions.md SDD/09-architecture-overview.md \
  SDD/14-version-history.md SDD/README.md AGENTS.md README.md \
  tests/README.md docs/WINDOWS_INSTALLER_GUIDE.md
git commit -m "docs: prepare v1.7.8 Windows TX test build"
```

### Task 4: Verify and build the private Win11 test installer

**Files:**
- Verify: all staged/committed source and documentation
- Create outside Git: `dist/windows/MRRC-FT710-v1.7.8-TX-44k1-test.exe`

- [ ] **Step 1: Run syntax and full hardware-independent verification**

Run:

```bash
venv/bin/python -m py_compile *.py
venv/bin/python -m unittest discover -s tests -v
```

Expected: compilation succeeds and 435 tests pass with zero failures/errors.

- [ ] **Step 2: Run SDD Guardian gates**

Run:

```bash
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check \
  audio_handler.py tests/test_audio.py packaging/windows/MRRC-FT710.iss \
  CHANGELOG.md SDD/08-architecture-decisions.md SDD/09-architecture-overview.md \
  SDD/14-version-history.md SDD/README.md AGENTS.md README.md tests/README.md \
  docs/WINDOWS_INSTALLER_GUIDE.md
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
```

Expected: both checks print `clean` (informational doc-sync notices are acceptable; block/warn violations are not).

- [ ] **Step 3: Build on the existing Win11 VM**

Sync the committed tree to the existing `ham.vlsc.net` Win11 build workspace without copying `atr1000_tuner.json`, then run:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
```

Expected: the script passes `py_compile`, all 435 tests, all three PyInstaller builds, and Inno Setup; `dist\windows\MRRC-FT710-Setup.exe` is produced.

- [ ] **Step 4: Inspect and retrieve the artifact**

On Windows, verify the assembled application contains `vendor\opus\windows\bin\x64\opus.dll` and the installer reports version 1.7.8. Copy the installer back as:

```text
dist/windows/MRRC-FT710-v1.7.8-TX-44k1-test.exe
```

Record SHA-256 and size. Do not alter `website/` or any public download target.

- [ ] **Step 5: Install and perform VM smoke checks**

Install the v1.7.8 package over the current Win11 installation and launch it. Device presence is not a gate; when the FT-710 endpoint is available, confirm the log contains `TX Opus decoder ready: 48000 Hz mono` and `TX audio started: [...] @ 44100 Hz`, with no `opus_decode failed` or TX write errors.

- [ ] **Step 6: Preserve the operator handoff boundary**

Report the installer path, SHA-256, VM build/install result, and the exact evening hardware checks: intelligible RF speech, absence of all-noise output, `fed/written/decode_fail/write_err` counters, and the `@ 44100 Hz` startup line. Public publishing remains blocked until the operator approves the RF test.
