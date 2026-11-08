# SunSDR2 DX Native Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a complete native SunSDR2 DX network backend with UDP control, shared IQ, 850-bin spectrum, 48 kHz RX/TX audio, MRRC setup/packaging and hardware-safe PTT.

**Architecture:** `backends/sunsdr/` owns one UDP device engine shared by controller, audio and scope adapters. Pure protocol/DSP modules are tested against captured bytes and synthetic IQ; a default backend audio-factory hook leaves all existing USB-audio radios unchanged.

**Tech Stack:** Python 3, asyncio DatagramProtocol, NumPy/SciPy, FastAPI, existing MRRC `RadioBackend`/`AudioHandler`/`ScopeHandler`, unittest.

**Design:** `docs/superpowers/specs/2026-11-08-sunsdr2dx-backend-design.md`

**Working-tree rule:** Existing G7250 edits are user work. Do not reset, overwrite or commit them. No git commit is part of this plan unless the user explicitly requests one.

---

### Task 1: UDP protocol golden vectors

**Files:**
- Create: `backends/sunsdr/__init__.py`
- Create: `backends/sunsdr/protocol.py`
- Create: `tests/test_sunsdr2dx_protocol.py`

- [ ] **Step 1: Write failing packet tests**

Use `unittest.TestCase` and assert exact bytes, not just lengths:

```python
from backends.sunsdr import protocol as p

class ProtocolTests(unittest.TestCase):
    def test_ptt_and_drive_layout(self):
        self.assertEqual(
            p.build_ptt(True).hex(),
            "32ff0600040000000000010000000000000001000000")
        self.assertEqual(p.build_drive(100)[-4:], b"\xff\0\0\0")
        self.assertEqual(p.build_drive(50)[-4:], b"\xb4\0\0\0")

    def test_frequency_uses_if_offset_only_for_rx(self):
        self.assertEqual(p.build_rx_frequency(7_074_000)[14:22],
                         struct.pack("<II", 0, 71_045_000))
        self.assertEqual(p.build_tx_frequency(7_074_000)[14:22],
                         struct.pack("<II", 0, 70_740_000))

    def test_stream_ctrl_keeps_verified_zero_word(self):
        packet = p.build_stream_ctrl()
        self.assertEqual(struct.unpack_from("<I", packet, 18)[0], 0)

    def test_iq_24bit_round_trip_and_truncation(self):
        samples = np.array([-1+0.5j, 0.25-0.75j], dtype=np.complex64)
        decoded = p.unpack_iq_24bit(p.pack_iq_24bit(samples))
        self.assertEqual(decoded.dtype, np.complex64)
        self.assertLess(np.max(np.abs(decoded-samples)), 2/8388608)
        with self.assertRaises(ValueError):
            p.unpack_iq_24bit(b"12345")
```

- [ ] **Step 2: Run tests and confirm the module is absent**

Run: `.venv/bin/python -m unittest tests.test_sunsdr2dx_protocol -v`
Expected: FAIL with `ModuleNotFoundError: backends.sunsdr`.

- [ ] **Step 3: Implement the pure protocol module**

Define `MAGIC=0xFF32`, `TRX_INDEX=0x00010000`, `IF_OFFSET_HZ=30_500`, command IDs, the four exact sample rates and captured boot lists. The common builder is:

```python
def build_packet(cmd_id: int, data: bytes = b"", trailing: int = 0) -> bytes:
    return (struct.pack("<HHIIH", MAGIC, cmd_id, len(data), TRX_INDEX, 0)
            + data + struct.pack("<I", trailing & 0xFFFFFFFF))

def build_ptt(tx: bool) -> bytes:
    return build_packet(CmdID.PTT, struct.pack("<I", 0), trailing=int(tx))

def build_drive(percent: int) -> bytes:
    value = int(round(255 * math.sqrt(max(0, min(100, percent)) / 100)))
    return build_packet(CmdID.DRIVE, struct.pack("<I", 0), trailing=value)

def build_rx_frequency(freq_hz: int) -> bytes:
    return build_packet(CmdID.RX_FREQ,
                        struct.pack("<II", 0, int((freq_hz + IF_OFFSET_HZ) * 10)))

def build_tx_frequency(freq_hz: int) -> bytes:
    return build_packet(CmdID.TX_FREQ,
                        struct.pack("<II", 0, int(freq_hz * 10)))
```

Implement `build_hw_init(rate)` and `build_stream_ctrl()` from the design's captured words. Vectorize 24-bit packing with an `(n,4)` byte view dropping every fourth byte; reject payload lengths not divisible by six. Add strict `parse_telemetry(data)` returning forward watts, average watts, volts, temperature and confirmed PTT only when the corresponding `0x1F00`/`0x1F01` packet has the required length.

- [ ] **Step 4: Run protocol tests and compile**

Run: `.venv/bin/python -m unittest tests.test_sunsdr2dx_protocol -v && .venv/bin/python -m py_compile backends/sunsdr/protocol.py`
Expected: all protocol tests PASS and compile exits 0.

### Task 2: Spectrum and 48 kHz DSP

**Files:**
- Create: `backends/sunsdr/dsp.py`
- Create: `tests/test_sunsdr2dx_dsp.py`

- [ ] **Step 1: Write failing synthetic-IQ tests**

```python
class DspTests(unittest.TestCase):
    def test_spectrum_has_850_bins_and_tracks_tone(self):
        rate = 78_125
        iq = np.exp(2j*np.pi*5_000*np.arange(4096)/rate)
        row = iq_to_spectrum(iq, rate)
        self.assertEqual(len(row), 850)
        self.assertGreater(int(np.argmax(row)), 425)

    def test_rx_output_is_48khz_int16(self):
        demod = AudioDemodulator(sample_rate=78_125, audio_rate=48_000)
        demod.set_mode("USB")
        pcm = demod.demodulate(_tone(5_000, 78_125, 4096))
        self.assertEqual(len(pcm) % 2, 0)
        self.assertEqual(demod.audio_rate, 48_000)

    def test_tx_sidebands_and_limit(self):
        tx = TXModulator(input_rate=48_000, iq_rate=39_062.5)
        tx.set_mode("USB")
        packets = tx.feed_audio((np.ones(960)*10000).astype("<i2").tobytes())
        self.assertTrue(all(len(packet) == 1200 for packet in packets))
        self.assertTrue(all(np.max(np.abs(p.unpack_iq_24bit(packet))) <= 1.0
                            for packet in packets))
```

Add cross-block phase-continuity tests using a sine split at a 20 ms boundary, LSB versus USB sideband-rejection tests, and empty/truncated input tests. Do not use exact float equality for DSP output.

- [ ] **Step 2: Run tests and confirm missing DSP**

Run: `.venv/bin/python -m unittest tests.test_sunsdr2dx_dsp -v`
Expected: FAIL importing `backends.sunsdr.dsp`.

- [ ] **Step 3: Implement spectrum and streaming RX/TX DSP**

Use a Hann FFT, `fftshift`, fixed dB window and linear display resampling to 850 bins. The RX demodulator must retain oscillator/filter/resampler state between calls and expose 48 kHz Int16 bytes. Use `scipy.signal.lfilter`/`sosfilt` for continuous filters and `scipy.signal.resample_poly` or a continuous rational resampler for fractional device rates; do not reset phase or filter state per datagram.

The TX constructor and public input contract are:

```python
class TXModulator:
    def __init__(self, input_rate: int = 48_000, iq_rate: float = 39_062.5):
        self.input_rate = input_rate
        self.iq_rate = iq_rate
        self.mode = "USB"
        self._audio_buffer = np.empty(0, dtype=np.float64)
        self._phase = 0.0

    def feed_audio(self, pcm: bytes) -> list[bytes]:
        """Return zero or more 200-sample, 1200-byte TX IQ payloads."""
```

Implement SSB through the old field-proven overlap-save Hilbert mathematics, continuous DC blocker, 300 Hz HPF, 3.6 kHz anti-alias LPF, fixed make-up gain and magnitude `tanh` limiter. Keep AM/FM/CW TX behind explicit `ValueError` until those paths have their own tests. Remove old debug-WAV writes and hardcoded directories.

- [ ] **Step 4: Run DSP tests**

Run: `.venv/bin/python -m unittest tests.test_sunsdr2dx_dsp -v`
Expected: all DSP tests PASS without a radio or WDSP.

### Task 3: Single asyncio device engine

**Files:**
- Create: `backends/sunsdr/engine.py`
- Create: `tests/test_sunsdr2dx_engine.py`

- [ ] **Step 1: Write fake-clock/UDP loopback tests**

Test one endpoint owns control and IQ, boot occurs only after both binds, heartbeat cadence is 0.5 s, frequency emits RX then TX then DRIVE, PTT assert emits DRIVE first, release emits PTT zero, and malformed datagrams do not stop receipt. Use ephemeral ports in tests:

```python
async def test_ptt_release_survives_audio_failure(self):
    engine = SunSDREngine("127.0.0.1", control_port=0, iq_port=0,
                          heartbeat_s=0.01)
    await engine.start()
    await engine.set_ptt(True)
    engine.audio.fail_next_write = True
    await engine.set_ptt(False)
    self.assertFalse(engine.ptt_requested)
    self.assertEqual(engine.sent_packets[-1], protocol.build_ptt(False))
    await engine.stop()
```

Also test a second `start()` is idempotent, concurrent recovery is single-flight, RX/TX queues have configured maxima and drop counters, sample-rate change while keyed returns `False`, and `stop()` always attempts release before closing sockets.

- [ ] **Step 2: Run engine tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_sunsdr2dx_engine -v`
Expected: FAIL importing `backends.sunsdr.engine`.

- [ ] **Step 3: Implement the engine with bounded ownership**

```python
class SunSDREngine:
    def __init__(self, host: str, *, local_host: str = "",
                 control_port: int = 50_001, iq_port: int = 50_002,
                 sample_rate: int = 78_125, heartbeat_s: float = 0.5):
        self.host = host.strip()
        self.local_host = local_host.strip()
        self.control_port = control_port
        self.iq_port = iq_port
        self.sample_rate = sample_rate
        self.heartbeat_s = heartbeat_s
        self.connected = False
        self.ptt_requested = False
```

Use `loop.create_datagram_endpoint()` for both directions. Bind IQ before `boot_sequence()`. Protect boot/recovery and state transitions with separate locks so UDP ingest never waits behind DSP. A dedicated async pacer sends 200-sample TX payloads at `200 / 39_062.5` seconds; underrun sends zero IQ and increments `tx_underruns`. `set_ptt(False)` clears requested state in `finally` after a send attempt and never performs a post-release read-back loop.

- [ ] **Step 4: Run engine and prior tests**

Run: `.venv/bin/python -m unittest tests.test_sunsdr2dx_protocol tests.test_sunsdr2dx_dsp tests.test_sunsdr2dx_engine -v`
Expected: all SunSDR tests PASS.

### Task 4: Backend, registry and network audio factory

**Files:**
- Create: `backends/sunsdr/backend.py`
- Create: `backends/sunsdr/audio.py`
- Create: `backends/sunsdr/scope.py`
- Modify: `backends/base.py`
- Modify: `backends/__init__.py`
- Modify: `server.py:3751-3807`
- Create: `tests/test_sunsdr2dx_backend.py`
- Modify: `tests/test_backend_factory.py`

- [ ] **Step 1: Write backend and default-factory tests**

Assert model registration, display name, bands/modes, 48 kHz rates, four spans, no USB hints/ATU, `verified=False`, TX gate both ways, no serial poll items, and default audio factory behaviour:

```python
def test_default_backend_does_not_replace_usb_audio(self):
    backend = FT710Backend(port="")
    self.assertIsNone(backend.create_audio_handler())

def test_sunsdr_uses_shared_engine_for_audio_and_scope(self):
    backend = SunSDR2DXBackend(host="127.0.0.1")
    audio = backend.create_audio_handler()
    scope = backend.create_scope_producer(None, None)
    self.assertIs(audio.engine, backend.engine)
    self.assertIs(scope.engine, backend.engine)
```

Add a controller test that key-up is allowed while key-down is gated, and unsupported setters return `False` without UDP traffic.

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_sunsdr2dx_backend tests.test_backend_factory -v`
Expected: FAIL because `sunsdr2dx` and the factory hook do not exist.

- [ ] **Step 3: Add the default hook and server selection**

```python
# RadioBackend
 def create_audio_handler(self):
     """Return a backend-owned audio handler, or None for the PyAudio default."""
     return None
```

In lifespan, call the hook first and construct `AudioHandler(rx_rate=..., tx_rate=..., name_hints=...)` only when it returns `None`. Keep the existing 8 s open timeout and all later audio-loop/tap code unchanged.

- [ ] **Step 4: Implement backend and adapters**

`SunSDR2DXBackend` owns exactly one `SunSDREngine`; its `cat` is a controller implementing all abstract methods with real UDP/DSP operations or explicit unsupported `False`/`None`. `create_audio_handler()` and `create_scope_producer()` are idempotent and return adapters sharing that engine. The audio adapter uses the existing `RxOpusEncoder`, exposes `_rx_running`, `_rx_dev_rate=48_000`, `_rx_last_peak`, bounded PCM queues and the full methods listed in the design. The scope adapter writes 850 bins, RX2 zeros, CENTER, current span/start frequency and frame counter into `ScopeHandler`.

Register the lazy import in `_BACKENDS`, add verification metadata and keep `create_backend(... port/baud_rate)` compatible by accepting but ignoring those serial-only arguments.

- [ ] **Step 5: Run backend and regression tests**

Run: `.venv/bin/python -m unittest tests.test_sunsdr2dx_backend tests.test_backend_factory tests.test_server_ws_protocol -v`
Expected: PASS with no existing protocol-contract change.

### Task 5: Network configuration and setup UI

**Files:**
- Modify: `config.py`
- Modify: `backends/base.py`
- Modify: `server.py` setup/startup helpers
- Modify: `static/index.html`
- Modify: `static/modules/` connection-settings module used by the page
- Modify: `macos/first_run.py`
- Create: `tests/test_sunsdr2dx_config.py`
- Modify: `tests/test_first_run.py`

- [ ] **Step 1: Write configuration tests**

Cover empty host refusal, valid IPv4/hostname, invalid sample rate/ports, default local-route selection and no serial probe for network models. Assert the model descriptor contains JSON-safe data:

```python
descriptor = model_connection("sunsdr2dx")
self.assertEqual(descriptor, {"transport": "network", "host_required": True})
self.assertEqual(model_connection("ft710")["transport"], "serial")
```

Assert an operator-selected `sunsdr2dx` is never overwritten by `probe_ft710()` and that empty serial port warnings are not emitted for this model.

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_sunsdr2dx_config tests.test_first_run -v`
Expected: FAIL on missing network descriptor/settings.

- [ ] **Step 3: Implement generic connection metadata and settings**

Add a registry-derived `model_connection(model)` descriptor with `transport="serial"` default and `transport="network", host_required=True` for SunSDR2 DX. Read these values through the existing `_env` compatibility path:

```python
SUNSDR_HOST = _env("MRRC_SUNSDR_HOST", "").strip()
SUNSDR_LOCAL_HOST = _env("MRRC_SUNSDR_LOCAL_HOST", "").strip()
SUNSDR_CONTROL_PORT = _bounded_int("MRRC_SUNSDR_CONTROL_PORT", 50_001, 1, 65_535)
SUNSDR_IQ_PORT = _bounded_int("MRRC_SUNSDR_IQ_PORT", 50_002, 1, 65_535)
SUNSDR_SAMPLE_RATE = _choice_int("MRRC_SUNSDR_SAMPLE_RATE", 78_125,
                                  (39_062, 78_125, 156_250, 312_500))
```

If the existing config module has no bounded-int helpers, add small private helpers with tests rather than silently accepting invalid values. Extend setup save/load to persist each key once and read it back before restart.

- [ ] **Step 4: Make the existing connection form metadata-driven**

Add a labeled host input and optional local-host input to the existing dialog, give them stable IDs, and put display/hide logic in the existing external connection module. On `transport="network"`, require host and hide serial/baud; on serial models preserve the current form. No inline JS and no `if model === "sunsdr2dx"` branch. Bump edited asset query versions.

- [ ] **Step 5: Run setup and path-prefix tests**

Run: `.venv/bin/python -m unittest tests.test_sunsdr2dx_config tests.test_first_run tests.test_server_box_pages tests.test_path_prefix -v`
Expected: PASS, with serial defaults unchanged.

### Task 6: Packaging, diagnostics and operator guide

**Files:**
- Modify: `packaging/pyinstaller/mrrc_modern_server.spec`
- Create: `packaging/common/profiles/sunsdr2dx.env`
- Create: `_diag_sunsdr.py`
- Create: `docs/SUNSDR2DX_USER_GUIDE.md`
- Modify: `tests/test_box_profiles.py`
- Create: `tests/test_diag_sunsdr.py`

- [ ] **Step 1: Write packaging and diagnostic tests**

Assert every lazy `backends.sunsdr` module is a hidden import, the box profile contains `MRRC_RADIO_MODEL=sunsdr2dx` and `MRRC_ALLOW_UNVERIFIED_TX=0`, and the diagnostic defaults to read-only. Diagnostic `--tx-check` must require both the flag and `--allow-tx`, and its `finally` must call `set_ptt(False)`.

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/python -m unittest tests.test_box_profiles tests.test_diag_sunsdr -v`
Expected: FAIL on the missing profile/diagnostic.

- [ ] **Step 3: Implement packaging and field diagnostic**

The diagnostic uses the production protocol/engine, prints host/local IP, bound ports, boot result, packet/IQ rate, actual sample rate, spectrum range, telemetry values and PTT confirmation. TX check is a bounded 200 ms dummy-load test and always releases in `finally`. Do not embed a deployment IP or password.

- [ ] **Step 4: Write the operator guide**

Document one-client-at-a-time device ownership, IP setup, four IQ spans, network audio, uncalibrated S-meter, DRIVE versus software mic gain, forward-power/volts/temperature, no SWR/ATU, TX gate and the exact diagnostic command. State explicitly that TCI/PRO/MB1 are not supported.

- [ ] **Step 5: Run packaging tests and import gate**

Run: `.venv/bin/python -m unittest tests.test_box_profiles tests.test_diag_sunsdr tests.test_ft8_packaging -v && .venv/bin/python -c "from backends import create_backend; b=create_backend('sunsdr2dx'); print(b.capabilities.display_name)"`
Expected: PASS and `Expert Electronics SunSDR2 DX`.

### Task 7: Full verification and documentation sync

**Files:**
- Modify: `AGENTS.md`, `README.md`, `tests/README.md`
- Modify: `SDD/04-system-context.md`, `SDD/05-non-functional-requirements.md`, `SDD/06-use-case-model.md`, `SDD/07-subject-area-model.md`, `SDD/08-architecture-decisions.md`, `SDD/09-architecture-overview.md`, `SDD/10-service-model.md`, `SDD/12-operational-model.md`, `SDD/13-feasibility-assessment.md`, `SDD/14-version-history.md`, `SDD/README.md`
- Modify: `CHANGELOG.md`
- Modify: affected website/SDD generated pages through the repository's standard generator

- [ ] **Step 1: Run all focused tests**

Run: `.venv/bin/python -m unittest discover -s tests -p 'test_sunsdr2dx*.py' -v`
Expected: all new SunSDR tests PASS.

- [ ] **Step 2: Run the full standard suite**

Run: `.venv/bin/python -m unittest discover -s tests -v`
Expected: PASS. If pre-existing G7250 work fails independently, record the exact failing tests and do not hide or revert that work.

- [ ] **Step 3: Run SDD and frontend guards**

Run: `python3 .agents/skills/sdd-guardian/harness/sdd_context.py check backends/sunsdr backends/base.py backends/__init__.py server.py config.py static/index.html macos/first_run.py`
Expected: no block violations. Run the repository's configured Biome check without `--write` on changed frontend modules; do not format guarded `static/ft710_main.js` or `static/ft710_ui.js`.

- [ ] **Step 4: Sync documentation from verified behaviour**

Add a new SDD architecture decision for the first pure-network backend and network-IQ audio adapter, amend USB-only use-case wording, add the model and settings, and update version history/Quick Facts/test counts. Keep `verified=False` until hardware acceptance. Do not claim AM/FM/CW TX or calibrated S-meter without the corresponding successful checks.

- [ ] **Step 5: Exercise the real server without RF first**

Start an isolated instance bound to `127.0.0.1` with a loopback fake device and exercise login, model selection, `fullState`, frequency/mode, `/WSspectrum`, `/WSaudioRX` and `/WSaudioTX`. Confirm no token appears in a URL and no serial-device prompt appears for the network model.

- [ ] **Step 6: Perform and record hardware acceptance**

Use `_diag_sunsdr.py` read-only first, then enable the explicit TX gate for the controlled session. Verify RX modes/spans/audio/recording/FT8, then SSB voice/TUNE/CQ/FT8 TX into a dummy load with a monitoring receiver. Test disconnect, heartbeat timeout, foreign PTT release, server shutdown and maximum TX duration. Record measured values and any unresolved items; only successful applicable items lose their unverified marker.
