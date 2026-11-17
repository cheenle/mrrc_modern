# 11. Component Model (ART 0515)

## 11.1 Component Inventory

### 11.1.1 Application and shared core

| Component | File | Responsibility |
| --- | --- | --- |
| FastAPIApp | `server.py` | Lifespan, middleware, static/REST/WS routes, fan-out, safety ownership and service composition |
| RadioBackendFactory | `backends/__init__.py` | 15 lazy registry entries, `known_models()`, connection metadata and verification table |
| RadioBackend | `backends/base.py` | ABC, `RadioCapabilities`, `ScopeProducer`, default hooks and optional network audio factory |
| RadioState | `radio_state.py` | Dirty-field state, live antenna/AGC fields, derived labels and serialization |
| PollScheduler | `poll_scheduler.py` | Seven adaptive tasks, baud-cost pacing, skip/cancel semantics, reconnect and controller-surface tolerance |
| Config | `config.py` | Environment parsing, protocol-neutral defaults, auth/audio/network/safety settings and shared tables |
| AudioHandler | `audio_handler.py` | PyAudio device selection, lifecycle-locked RX/TX streams, loopback avoidance and recovery |
| AudioResample | `audio_resample.py` | Frame-aligned legacy 44.1↔48 kHz conversion plus stateful soxr LQ PCM16 conversion and complete 641 B `0x02` framing (AD-038) |
| OpusCodec | `opus_rx.py` | ctypes libopus encoder/decoder, including configurable rate/channels for network backends |
| AudioLevels | `audio_levels.py` | Box mic/listener gains, atomic persistence, validation and clipping evidence |
| Recorder | `recorder.py` | 16 kHz monotonic timeline, FIR decimation and incremental MP3/index helpers |
| CQPlayer | `cq_player.py` | WAV normalization, finite 20 ms feed and low-water queue behavior |
| ScopeHandler | `scope_handler.py` | Common 850-bin real/synthetic container and legacy binary encoding |
| SpectrumProfile | `spectrum_profile.py` | AD-025 shape/divider presets and strict caps parsing |
| SessionMetrics | `session_metrics.py` | Per-role/kind sessions/sockets, peaks and audio/spectrum bytes without token disclosure |
| HostPerf | `host_perf.py` | Delta-based host/process sampling, absent-field semantics and one verdict authority |

### 11.1.2 Radio protocol families

| Component | File | Responsibility |
| --- | --- | --- |
| FT710Backend / CatController | `backends/ft710/` | Verified FT-710 CAT, capabilities and model tables |
| FT4222Scope | `backends/ft710/{scope_pipe,scope_producer,scope_frame,scope_libraries}.py` | Exclusive subprocess, framing, restart, TX pause and FTDI discovery |
| CivProfiles / Codec | `backends/ic7300/{civ_profiles,civ_codec}.py` | Icom/Xiegu profile facts, BCD/framing and geometry-adopting scope assembly |
| CivController | `backends/ic7300/civ_controller.py` | Reader/demux, echo/pending/transceive routing, priorities and reconnect over serial-like transport |
| CivScope | `backends/ic7300/civ_scope.py` | 0x27 waveform to common 850-bin scope |
| XieguBackend | `backends/xiegu/` | G7250 profile/address discovery, capabilities and serial/network selection |
| IcomNetwork | `backends/icom_net/` | RS-BA1 byte/session layer, three sockets, CI-V serial facade and 16→48 kHz audio |
| YaesuProfiles | `backends/yaesu/yaesu_profiles.py` | Six profiles, CAT command shapes, filter Hz, dual RX, rates, verification and provenance |
| YaesuCatCore / Backend | `backends/yaesu/{cat_core,backend}.py` | Shared transport/controller, profile-derived backend, read-back and RX-source behavior |
| ScuLan | `backends/yaesu/scu_lan/` | UDP cable/session/serial facade, 850-bin scope and adaptive-rate dual-receiver audio; TX refused |
| SunSDR | `backends/sunsdr/` | Captured UDP protocol, exclusive engine, DSP, optional WDSP boundary and backend/audio/scope adapters |
| FlexVita / Protocol | `backends/flex/{vita,protocol}.py` | Pure VITA-49 and SmartSDR text/discovery codecs, stream/FFT/meter/audio framing |
| FlexSession / Engine | `backends/flex/{session,engine}.py` | TCP/UDP ownership, uvloop-safe I/O, own pan/slice/stream state, meters and paced TX |
| FlexAdapters | `backends/flex/{backend,audio,scope}.py` | Backend-neutral control, 24 kHz float stereo↔48 kHz PCM and owned-pan FFT scope |

### 11.1.3 Work modes, devices and operations

| Component | File | Responsibility |
| --- | --- | --- |
| FT8Core / Engine | `ft8/{core,engine}/` | Large-stack native worker, ABI/library boundary, UTC orchestration, cascade, sequencer and repository |
| FT8Platform / Web | `ft8/{platform,web}/`, `ft8/mode.py` | RX/TX/CAT/settings adapters, SSB snapshot/restore, REST and event stream |
| SDRPipe / Frames | `sdr_pipe.py`, `sdr_frames.py` | Exclusive SDRplay process and the sole P1/S1/A1/C1 framing contract |
| SDRHub / DSP / Shim | `sdr_hub.py`, `sdr_dsp.c`, `sdr_api_shim.c` | Pure sharing rules, native channelizer/demodulator and flat vendor API boundary |
| LocalControl | `local_ctl.py` | HID-usage decoding, numeric/band/channel/CQ/QSY mapping and PTT edges |
| LocalSpeaker | `local_speaker.py` | Bounded ALSA/Bluetooth speaker tap from native RX |
| Network / BoxOps | `net_wifi.py`, `box_ops.py`, `linux/box_helper.py` | nmcli ownership, pure orchestration/reporting and fixed peer-credentialed root verbs |
| Bluetooth | `net_bt.py` | Serialized scan/pair/forget, paired/keys_work truth and controller recovery requests |
| NanoVNA | `nano/{vna,analysis,flash}.py` | Sole instrument serial owner, pure S11 math and guarded DFU state machine |
| ATR1000 | `atr1000_{client,tuner}.py` | Optional tuner protocol, learning and atomic LC store |
| TLS / Bootstrap | `net_tls.py`, `ssl_bootstrap.py` | Shipped-CA outbound trust and idempotent instance certificate enrollment |
| HelpSession | `help_session.py` | Loopback-only, time-bounded SSH with POSIX pty or Windows pipe shell |
| UpgradeCore | `upgrade_core.py` | Manifest validation, version comparison and update proof/state |
| Support | `support_bundle.py`, `support_answers.py`, `support_board.py` | Redacted diagnostics, answer contract/rendering and external board classification |
| LauncherSupport | `launcher_net.py`, `launcher_log.py` | Nonfatal HTTP/HTTPS probing and bounded startup stdout/stderr tee |

### 11.1.4 Frontend components

| Component | File | Responsibility |
| --- | --- | --- |
| MobileSPA | `static/index.html`, `ft710.css` | Capability-driven operator shell, menus, meters, waterfall and PTT |
| MainJS / UIJS | `static/ft710_main.js`, `ft710_ui.js` | Legacy WS/audio/state and all main rendering; protected cross-file globals/formatting |
| TerminalPage | `static/terminal.html`, `terminal.js` | Independent AD-039 reference client; no guarded monolith imports |
| Listen / SDR | `static/{listen,sdr}.{html,js}` | Self-contained role-specific receive pages with prefix-safe URLs |
| Box / Perf / Nano pages | `static/{setup,settings,perf,nano}*` | Operational interfaces over the corresponding authenticated APIs |
| PTTManager | `static/modules/ptt_manager.js` | Client state machine, watchdog, heartbeat and release hooks |
| SettingsManager | `static/modules/settings_manager.js` | Cookie-backed preferences and memory/settings persistence |
| OpusWASM / Workers | `static/modules/opus_*`, `tx_*` | Browser codec, mic capture and 48 kHz 20 ms TX encoding |
| RXWorklet | `static/rx_worklet_processor.js` | Time-based jitter-buffered playback shared by applicable pages |
| ServiceWorker | `static/sw.js` | Content-hashed precache/version contract; independent pages need not be precached |

## 11.2 Backend Component Collaboration (Startup)

```text
FastAPI lifespan
  1. Load configuration and select create_backend(MRRC_RADIO_MODEL)
  2. Resolve serial/network metadata; construct the backend's stable controller surface
  3. Connect controller/engine and perform read-only identity/capability discovery
  4. Initialize RadioState from backend sync; start PollScheduler
  5. Initialize scope and backend ScopeProducer (subprocess, CI-V, network or none)
  6. Create backend audio handler (PyAudio default or network adapter), then start RX/TX tasks
  7. Start wall-clock RX loop, TX drain, spectrum broadcaster and event-loop lag probe
  8. Compose recording/CQ/FT8, optional SDR/ATR/local-control/help/cloud services
  9. Advertise only actual capabilities; unverified/backend-specific TX gates remain active
```

A reconnect re-runs only necessary identity/scope initialization and lets polling tiers refill state, rather than holding a serial lock through a full startup sync. Missing optional native/device components degrade their own service and leave control/safety running.

## 11.3 Frontend Component Collaboration (Page Load)

```text
Operator SPA:
  HTTPS login → mrrc_auth cookie
  → /WSradio → fullState/capabilities
  → independent /WSaudioRX, /WSaudioTX, /WSspectrum connections
  → spectrumCaps selects a tier; audio/worklet and UI initialize from real state

Terminal reference/client:
  authenticated /WSterminal → fullState
  → optional spectrumCaps on the same socket
  → text uses control JSON; binary strips/adds the AD-039 channel byte
  → media backpressure never intentionally closes the control socket

All clients:
  cookie is implicit on same-origin WS; native clients may use Bearer
  → no frontend constructs a ?token= URL
```

## 11.4 Frontend Component Collaboration (PTT)

```text
Press:
  PTTManager → sendCommand('ptt', true)
  → server AD-019/backend gate + control keyer arbitration
  → backend key command → start TX media
  → /WSaudioTX or terminal channel 0x01 enters _feed_tx_uplink_frame
  → owner check → decode → mic gain/record → backend queue/modulator

Release (every path):
  PTTManager / watchdog / pagehide / disconnect / heartbeat / automated-TX end
  → backend set_ptt(False), fire-and-forget
  → stop/flush TX media and clear keyer/owner as appropriate
  → authoritative TX state poll/status and client watchdog

Terminal disconnect while keyer/owner:
  one cleanup path removes all channel registrations and forces RX exactly once
```
