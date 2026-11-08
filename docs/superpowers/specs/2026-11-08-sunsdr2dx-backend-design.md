# SunSDR2 DX native backend — design (2026-11-08)

Add **Expert Electronics SunSDR2 DX** as MRRC Modern model `sunsdr2dx`.
This is a native backend, not a bridge to the old SunMRRC server. The sibling
repository `/Users/cheenle/HAM/sunsdr/` supplies the verified UDP protocol,
ExpertSDR3 packet captures and DSP reference; its server, authentication,
WebSocket protocol and frontend are not imported.

The radio is available for a complete hardware acceptance session, including
TX into a dummy load or a suitable antenna. Until that session passes, the
profile remains `verified=False` and PTT/TUNE stay behind AD-019 / NFR-067.

## 1. Requirements and existing contracts

The backend must preserve the standard MRRC browser, iOS and Android
contracts rather than adding a SunSDR-specific client protocol:

- 48 kHz, 20 ms, tagged Opus/Int16 audio (AD-004, AD-011, NFR-004/060/064).
- The existing 850-bin `ScopeHandler` and tiered `/WSspectrum` stream
  (AD-025, NFR-003, UC-004); no 512-bin SunMRRC frames reach a client.
- Existing RX taps for recording, FT8 and local speaker (AD-028), and existing
  TX mic gain and recording tap (AD-032).
- Existing multi-client PTT ownership, disconnect/heartbeat release and maximum
  TX duration (SDD chapter 15). UDP control does not weaken these guarantees.
- Cookie/Bearer authentication and prefix-safe standard endpoints (AD-024).
- RX audio below 500 ms end-to-end (NFR-001, SC3), real spectrum (SC6), and
  audible RF from browser microphone/FT8 (SC4).

Relevant legacy assumptions must be made explicit rather than leaked into the
UI: this radio has no serial CAT port and no USB sound card. Control, RX IQ,
TX IQ and telemetry all use the LAN.

## 2. Architecture

### 2.1 One device engine

`backends/sunsdr/engine.py` owns the only SunSDR2 DX device session:

- Bind control UDP 50001 and IQ UDP 50002 before sending the boot sequence.
- Send the captured START/HW_INIT/frequency/STREAM_CTRL sequence, then maintain
  0.5 s control heartbeat and stream keep-alive.
- Receive RX IQ, transmit paced TX IQ, and parse device telemetry.
- Own reconnect and stalled-stream recovery; consumers never boot or rebind the
  radio independently.
- Expose bounded RX-audio and TX-audio queues and counters. Slow consumers drop
  oldest data with a counter; they never block the event loop or UDP receive
  path. Keep-alive and PTT release must not depend on DSP throughput.

The engine starts with the backend connection, not only when a spectrum client
connects. RX audio, FT8 and recording therefore continue to work when no
waterfall is visible.

### 2.2 Pure protocol and DSP boundaries

`protocol.py` contains packet constants, pure builders/parsers and the captured
boot words. No sockets or asyncio objects appear here. Tests compare packets
byte-for-byte with the ExpertSDR3 captures, including:

- 14-byte header plus trailing word and magic `0xFF32`.
- RX DDS = VFO + 30,500 Hz, 0.1 Hz units; TX DDS = exact VFO.
- PTT `0x0006`, DRIVE `0x0017` in the trailing word with square-root taper,
  heartbeat `0x0018`, HW_INIT sample-rate word and STREAM_CTRL word[1] = 0.
- RX/TX 24-bit interleaved IQ and telemetry `0x1F00`/`0x1F01`.

`dsp.py` adapts the field-proven mathematics, not the old module wholesale:
RX IF shift, sideband-selective filtering, AM/FM/CW/SSB demodulation, S-meter
estimation, spectrum generation, TX DC blocking, 300 Hz high-pass, anti-alias
filtering, Hilbert SSB modulation, soft limiting and 24-bit packing. SciPy and
NumPy are already project dependencies. No debug WAV capture, hardcoded path,
global band-power table or server callback is copied.

WDSP is optional and is not a core acceptance dependency. NR/NB/ANF controls
are exposed only when a real implementation is available; otherwise the
backend reports them unsupported. The Python AGC and filters provide the
baseline receive path.

### 2.3 Standard backend and adapters

`backend.py` implements `RadioBackend` through a `SunSDR2DXController` (`cat`).
The controller preserves the existing async method surface used by the server
and poll scheduler, but transports commands over UDP. Unsupported operations
return `False`/`None` and are omitted from poll lists; no fake serial commands
are sent.

`audio.py` implements the existing `AudioHandler` surface over the engine:

- `start_rx`, `read_rx_chunk`, `restart_rx`, `note_rx_chunk`,
  `rx_silent_seconds`, `encode_rx_audio` and the small state attributes used by
  the server loop.
- `start_tx`, `feed_tx_audio`, `write_tx_chunk`, `stop_tx`, `tx_stats`,
  `has_pending_tx_audio`, `tx_queue_frames`, `device_rates` and `close`.
- Public RX reads and TX input are exactly 48 kHz Int16 mono. Device/codec rate
  conversion is continuous across blocks and stays inside this adapter/DSP;
  16 kHz never enters the MRRC audio contract.

Add a defaulted `RadioBackend.create_audio_handler()` hook. The default returns
`None`, and `server.py` then constructs today's PyAudio `AudioHandler`.
SunSDR2 DX returns its network adapter. Existing backends therefore remain
byte-for-byte on their current audio path.

`scope.py` adapts engine spectrum frames to `ScopeHandler`: 850 bins, empty RX2,
CENTER mode, current VFO and the selected IQ span. The engine's spectrum
subscription may sleep without clients, but IQ ingest and requested RX/TX audio
do not depend on that subscription.

## 3. Configuration and user-visible behaviour

New settings:

- `MRRC_SUNSDR_HOST` — radio address; empty means not configured.
- `MRRC_SUNSDR_LOCAL_HOST` — optional local bind address; empty selects the
  interface used to route to the radio, with `0.0.0.0` fallback only when that
  is safe for UDP reception.
- `MRRC_SUNSDR_CONTROL_PORT` / `MRRC_SUNSDR_IQ_PORT` — default 50001/50002,
  primarily for diagnostics and isolated tests.
- `MRRC_SUNSDR_SAMPLE_RATE` — `39062`, `78125`, `156250` or `312500`; default
  `78125`. A change while streaming performs one controlled engine restart and
  is refused while TX is active.

The model registry supplies network-connection metadata so setup UI can show
host/local-address fields and hide serial-port/baud controls without adding
radio-name branches to the main frontend. Existing serial models retain their
current form and validation. Startup and failure messages name the radio host
and UDP ports rather than printing an empty serial-port warning.

`RadioCapabilities` advertises `scope_type="lan_iq"`, the four actual spans,
48 kHz codec-domain rates, no USB audio hints, no ATU, no SWR/ALC meter and only
supported controls. The existing UI rebuilds its span table from capabilities.

Supported at initial acceptance: frequency, four IQ spans, SSB/CW/DIGI/AM/FM
receive and filtering, SSB voice TX, DRIVE, ATT/preamp, AGC, PTT, TUNE, CQ,
FT8, memories, recording, listen-only and standard desktop/mobile clients.
AM/FM/CW transmit remain disabled unless their modulator/keying implementation
and separate on-air checks pass. There is no invented SWR from the supply
voltage field and no claim of internal ATU support.

S-meter is derived from filtered RX IQ energy. It is labelled uncalibrated
until compared with known signals on the hardware; missing or stale IQ produces
no invented meter reading.

## 4. PTT and failure handling

PTT assert sends current DRIVE before `0x0006 trailing=1`; QSY also re-sends
DRIVE because the device restores per-band calibration. `tx_status` uses device
confirmation when available, while release remains fire-and-forget and never
waits for a confirmation loop.

Every existing release path ends at controller `set_ptt(False)`: key-owner
release, foreign-key rejection path, socket disconnect, TX heartbeat timeout,
maximum TX duration, server shutdown, audio-start failure and engine recovery.
Stopping TX audio never skips the shielded release. A failed release is logged
and retried by the engine's bounded safety recovery without delaying the normal
RX transition.

Network stale/stall detection, UDP bind conflicts, malformed datagrams and IQ
sequence gaps are visible in logs and diagnostic counters. Recovery is
rate-limited and never starts overlapping boot sequences. Port conflicts leave
the UI available with a clear disconnected state; they do not fall back to a
synthetic spectrum that looks like real RF.

During TX the RX stream is treated as unavailable rather than as proof of USB
audio failure. TX underruns emit silence IQ while keyed, count the event, and
never manufacture stale audio. Queue limits apply to both directions.

## 5. Files and integration

| File | Change |
| --- | --- |
| `backends/sunsdr/{__init__,protocol,engine,dsp,audio,scope,backend}.py` | New native backend and adapters |
| `backends/base.py` | Default audio-factory hook; connection metadata contract if required |
| `backends/__init__.py` | Register `sunsdr2dx` and verification metadata |
| `server.py` | Use backend audio factory; network-aware startup/setup diagnostics |
| `config.py` | SunSDR network settings and validation |
| `static/index.html` + connection module | Network-host fields driven by model metadata; no inline JS |
| `macos/first_run.py` | Do not serial-probe or overwrite a selected network model |
| `packaging/pyinstaller/mrrc_modern_server.spec` | Hidden imports for lazy backend modules |
| `packaging/common/profiles/sunsdr2dx.env` | Box profile with TX gate stated explicitly |
| `tests/test_sunsdr2dx*.py` | Protocol golden vectors, DSP, queues, backend and loopback tests |
| Existing registry/protocol/packaging guards | Update deliberate counts and supported-model assertions |
| `docs/` / README / AGENTS / SDD / tests/README / CHANGELOG | User guide and architecture/version/test sync |

Existing uncommitted G7250 work in the working tree is preserved. No old
SunMRRC runtime files are edited or deleted.

## 6. Verification

### Offline

Use `python -m unittest discover -s tests -v` and the SDD harness checks. New
tests cover:

- Every captured packet builder and malformed/truncated input.
- IQ sign/sense, 24-bit round trip, FFT peak and selected span.
- RX tone/sideband rejection, 48 kHz frame size and cross-block continuity.
- TX USB/LSB sideband, AM/FM gating, soft-limit ceiling, silence/underrun and
  no discontinuity at 20 ms input boundaries.
- One engine shared by audio and scope, no duplicate bind/boot, four-rate
  changes refused during TX, and bounded queue/drop counters.
- Full `RadioBackend` construction, unsupported poll removal, capabilities,
  model registry, setup validation and packaging imports.
- UDP loopback boot/heartbeat/IQ/telemetry, reconnect and forced PTT release.
- Existing audio-loop contract with the network adapter: recording, FT8 tap,
  CQ and TX session key failure all release correctly.

### Hardware

Record radio firmware, host OS, radio/local IPs and whether WDSP is present.
Acceptance includes frequency/readout, each IQ span, waterfall centre and
S-meter sanity, each supported RX mode, browser recording and FT8 decode.
TX into a dummy load verifies SSB voice with a second receiver, DRIVE sweep,
forward watts/volts/temperature, TUNE/CQ/FT8 RF and no TX queue drops. Test
foreign-client release, browser disconnect, network interruption, server
restart and maximum-duration timeout. Only after all applicable checks pass
does the profile become `verified=True`; unresolved items stay explicitly
gated and documented.

## 7. Documentation

Add a SunSDR2 DX user guide covering the exclusive UDP session, network setup,
four spans, uncalibrated S-meter and telemetry limits. Update SDD architecture
and use cases for network control plus network-IQ audio, chapter 14 version
history, SDD README quick facts, AGENTS backend table, README supported models,
test counts and release/package model lists. No claim of TCI, SunSDR2 PRO or
MB1 support is implied by this backend.
