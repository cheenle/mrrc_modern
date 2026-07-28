# 8. Architecture Decisions (ART 0513)

## AD-001: Use FastAPI/Uvicorn for MRRC FT-710 Server

| Attribute | Value |
|-----------|-------|
| Type | Architectural |
| Status | Implemented |
| Decision | Use FastAPI with native WebSocket routes and Uvicorn runtime |

**Problem**: The server needs static file serving, 4 WebSocket endpoints, async serial CAT I/O, scope subprocess management, audio streaming, and auth — all in one process.

**Rationale**: FastAPI/Uvicorn provides direct async integration, lifespan management, middleware, and a small code surface. No Tornado, Flask, or Django needed.

**Consequences**: All server logic lives in `server.py` with modular imports from sibling modules. Lifecycle managed via `@asynccontextmanager lifespan`.

## AD-002: Direct Serial CAT — No Hamlib/Rigctld

| Attribute | Value |
|-----------|-------|
| Type | Architectural |
| Status | Implemented |
| Decision | Use `pyserial` (sync API) with `asyncio.to_thread()` for serial I/O |

**Problem**: Hamlib adds a large dependency and another process to manage. FT-710 CAT protocol is well-documented (Yaesu standard) and straightforward.

**Rationale**: A dedicated `CatController` class with an `asyncio.Lock` for serialized access and thread-pool offloading is simpler, more debuggable, and has fewer failure modes than Hamlib/rigctld.

**Consequences**: The codebase is FT-710 specific. Adding another radio model requires a new commander class.

## AD-003: Dirty-Field State Broadcasting

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | `RadioState` dataclass tracks changed fields via `_dirty_fields` set; broadcast only changed fields |

**Problem**: Full state broadcasts on every poll would be wasteful (~44 fields at 10Hz).

**Rationale**: `RadioState.update()` records which fields changed; `to_dirty_dict()` sends only those fields. Clients merge partial updates into their local state.

**Consequences**: `stateUpdate` messages are compact; clients must maintain local state mirror.

## AD-004: Tagged Dual-Codec Audio Transport (Opus + Int16 PCM)

| Attribute | Value |
|-----------|-------|
| Type | Architectural |
| Status | Implemented |
| Decision | Both `/WSaudioRX` and `/WSaudioTX` carry a 1-byte codec tag per frame: `0x00` = Int16 PCM, `0x01` = Opus. RX: 48kHz @ 64kbps (fullband, transparent for broadcast music). TX: 48kHz @ 64kbps CBR (voice with fidelity priority: complexity=5, SIGNAL=VOICE, VBR/FEC/DTX disabled). Default Opus; falls back to PCM. |

**Problem**: (RX) Int16 PCM at 48kHz mono costs ~768kbps — heavy on mobile/WiFi. Opus at 64kbps cuts that 12×. (TX) Browser mic Opus encoding saves uplink bandwidth. A per-frame tag removes negotiation races — receiver inspects tag and decodes accordingly.

**Rationale**: `opus_rx.py` (copied from sunmrrc) provides direct ctypes libopus bindings. Uses `max_data_bytes` cap on `opus_encode()` to control bitrate — avoids arm64 variadic `opus_encoder_ctl` issues. Browser uses WASM `OpusDecoder`/`OpusEncoder`. TX encoder configured for voice with fidelity priority (`static/modules/opus_codec.js`): complexity=5, 64kbps CBR (stable packet size), FEC=OFF (WebSocket TCP is reliable), DTX=OFF (no priming gaps), fullband, SIGNAL=VOICE, LSB depth 16. (Raised from the original 28kbps/complexity=3 for transmit-fidelity headroom.)

**Consequences**: Adds libopus dependency (optional — degrades gracefully to PCM). Codec is user-switchable. `AUDIO_TAG_PCM` / `AUDIO_TAG_OPUS` are constants in both Python and JavaScript.

## AD-005: scope_pipe as Standalone Subprocess

| Attribute | Value |
|-----------|-------|
| Type | Architectural |
| Status | Implemented |
| Decision | FT4222 SPI I/O runs in a separate Python process (`scope_pipe.py`), communicating with the server via stdout/stderr pipes |

**Problem**: FT4222 ctypes calls are blocking and can hang. Running them in the asyncio event loop would stall the entire server. Threading is fragile with FTDI D2XX driver state.

**Rationale**: A subprocess isolates the FTDI driver. If scope_pipe crashes, the server continues (falls back to S-meter). Frame format: 4-byte BE uint32 length + payload. stderr carries machine-parseable `STATUS:` lines for diagnostics. Heartbeat frames (len=0) keep pipe alive when idle.

**Consequences**: scope_pipe is independently restartable. Server handles pipe exit gracefully (marks scope disconnected, switches to fallback). Two process lifecycle to manage.

**Amended (V2.7)**: the pipe protocol gains a stdin control channel — the server pushes `TX:1`/`TX:0` on every `tx_status` transition (`_notify_scope_pipe_tx`). While TX is active the pipe pauses SPI reads and freezes all sync/stall recovery counters (the FT-710 garbles its scope stream during TX; reading it previously churned the pipe into `fatal:too_many_reinits` after every PTT), and runs one clean re-sync on TX→RX. stdin EOF (parent died) also stops the pipe. Windows teardown uses `taskkill /PID <pid> /T /F` because `terminate()` only reaches the onefile bootloader and orphans the real worker (which then holds the FT4222: FT_DEVICE_NOT_FOUND for the next pipe).

## AD-006: Dual-Mode Spectrum (FT4222 + S-Meter Fallback)

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | When FT4222 is available, broadcast real 850-point FFT data. When unavailable, generate synthetic multi-peak Gaussian spectrum from CAT S-meter readings |

**Problem**: FT4222 requires specific libraries, D2XX driver config, and exclusive device access. It's not always available.

**Rationale**: The S-meter fallback provides useful visual context (shows band activity) even without hardware scope. The binary frame format is identical in both modes — clients don't care about the source.

**Consequences**: `ScopeHandler` has two code paths: `update_from_scope_frame()` (real data) and `update_from_radio_state()` (synthetic). `scope._connected` flag determines which is active.

## AD-007: PTT Release as Safety-Critical Flow

| Attribute | Value |
|-----------|-------|
| Type | Safety |
| Status | Implemented |
| Decision | Multiple independent release paths: normal WebSocket command (fire-and-forget TX0), PTT watchdog, dead-man switch on WS disconnect, beforeunload beacon, pagehide handler |

**Problem**: A lost or unprocessed PTT release command can leave the radio transmitting indefinitely — a serious safety and regulatory issue.

**Rationale**: Release is more safety-critical than keying. Each layer catches a different failure mode: lost WS message, half-open socket, browser crash, tab close, app switch. See Chapter 15 for detailed PTT Safety Architecture.

**Consequences**: Frontend PTT logic is more complex; polling skip-on-PTT ensures state consistency. (V1.2 removed the 3×200ms post-release verify loop — it added ~600ms to every release; stuck-keyup detection now relies on the 500ms TX-status poll plus the browser watchdog.)

## AD-008: PyAudio Auto-Detection of FT-710 USB Audio

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | Multi-layer device selection: (1) explicit `FT710_AUDIO_RX_DEVICE`/`FT710_AUDIO_TX_DEVICE` env var (index or name substring), (2) name match for "FT-710"/"FT710"/"YAESU", (3) name match for "USB Audio CODEC" / "USB Audio Device" (the FT-710's built-in sound card enumerates under one of these generic names on Windows; first match wins, multi-match warns), (4) mono-channel heuristic (FT-710 USB audio has 1 input channel vs typical stereo USB mics), (5) full-duplex heuristic for TX (device with both input + output), (6) system default fallback |

**Problem**: The FT-710 USB audio device name varies by OS and driver version. Hardcoding a device index is fragile. Previous version only searched by name substring and fell back to first input device — could select webcam mic instead of FT-710. On Windows the card carries no "FT-710"/"YAESU" string at all ("USB Audio CODEC" or "USB Audio Device", possibly localized/prefixed), so without tier (3) the heuristics grabbed a laptop mic (RX) or PC speakers (TX) — V2.6 field report.

**Rationale**: Name-based matching is more robust than index-based. The mono-channel heuristic is reliable: FT-710 provides exactly 1 input channel (mono RX), while webcams and USB mics typically offer 2 (stereo). Full-duplex preference for TX ensures the same device is used for both RX and TX paths. Logs all available devices at startup for debugging. The generic USB-audio names rank below the FT-710-specific names but above the channel heuristics; duplicates from per-host-API enumeration (MME/DirectSound/WASAPI) open the same hardware, and a genuine multi-device setup is warned about with a pointer to the env-var lock (`USB Audio` is the common substring covering both enumeration forms).

**Consequences**: Audio may still use wrong device if multiple mono USB audio devices are present. Configurable device override via env vars is the recommended approach for such setups.

**Amended (V2.8)**: Windows full-duplex wedge — on Windows (MME/DirectSound), opening the TX playback stream on the FT-710's C-Media codec silently wedges the RX capture stream (stays open, error-free, delivers silence; field symptom: RX audio perfect after server restart, gone after one PTT). `AudioHandler.restart_rx()` reopens the capture stream on every TX→RX transition (hooked in `_broadcast_state` on `tx_status`), Windows-only; macOS CoreAudio is unaffected and pays no reopen cost.

## AD-009: 7-Task Adaptive Polling with Bounded Lock Time

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | Background CAT polling split into 7 cooperative tasks (IF, VFO, TX status, TX meters, settings, slow telemetry, connection watchdog), with skip-on-command and short per-query timeout |

**Problem**: Polling too fast floods the serial port; too slow makes the UI feel unresponsive. Some fields (S-meter) change rapidly; others (filter width) rarely.

**Rationale**: Fast path keeps only `FA/MD0/SM0` at 100ms. `VS/FB` run separately at 500ms, so active-VFO tracking does not bloat the IF loop. TX status and TX meters are independent 500ms tasks; TX meter polling includes `RM3/RM4/RM5/RM6` and is TX-only. Settings (2s) include `RG0`, `MS`, and tuner state; slow telemetry (5s) includes `RM7/RM8`, `PR`, `AO`, and `RI0`. Poll query timeout is 0.25s to cap lock occupancy.

**Consequences**: `PollScheduler` owns task-level cadence and backpressure controls (`skip_next_poll()`, short pause after user command, and `_cancel_polls` awareness). CAT errors remain per-command and non-fatal. Since V1.7, poll loops re-check skip state AFTER each in-flight query response and discard stale reads (a mid-flight `SH0;` response previously overwrote a just-set filter width); the filter set path additionally verifies with a 150 ms `SH0;` read-back.

## AD-010: Memory Channels as Server-Side JSON

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | Memory channels stored server-side in `mem_channels.json`; API: GET/POST `/api/mem_channels`; auto-broadcast to all clients on change |

**Problem**: Client-side-only storage loses channels across devices/browsers. Server-side persistence ensures all clients see the same channels.

**Rationale**: Simple JSON file is adequate for 6-99 channel slots. No database needed. Auto-broadcast keeps all clients in sync.

**Consequences**: Channels survive server restarts. File is human-editable. No per-user channel isolation (single shared-password model).

## AD-011: 48kHz Codec Domain with 44.1kHz Device Bridge

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | TX audio chain runs at 48 kHz in the codec domain (browser capture → Opus encode → server decode) and plays to the radio at 44.1 kHz — the FT-710 USB audio native rate — via a frame-aligned resample bridge (`audio_resample.py`, numpy linear interp; 960↔882 samples = exactly 20 ms, ratio 160:147). RX uses the inverse bridge (44.1k capture → 48k Opus encode). |

**Problem**: V1.0 captured mic audio at 16 kHz (320 samples/20ms frame) but PyAudio played back at 48 kHz (expecting 960 samples/20ms). The 3:1 rate mismatch caused the output stream to underrun — every 20ms Opus frame produced 320 samples that filled only 1/3 of the 960-sample playback buffer. The remaining 2/3 was stale/residual buffer data, producing audible crackling ("咔咔咔") on transmitted audio. The first fix unified everything at 48 kHz — but later measurement showed the FT-710 USB audio interface natively runs at **44.1 kHz**, so 48 kHz PCM still could not be written straight to the device stream.

**Rationale**: Opus mandates 48 kHz; the FT-710 mandates 44.1 kHz. A stateless numpy linear-interp resampler bridges the two domains per 20 ms frame with exact integer alignment (960→882), costing ~µs per call with zero phase drift. Browser capture at 48 kHz works on all modern platforms (iOS 15+, Chrome, Firefox).

**Consequences**: Each direction has exactly one SRC step at the server boundary, owned by `audio_resample.py`. Browser and codec stay at 48 kHz; both PyAudio streams run at 44.1 kHz. The v1.0 underrun class of bug is impossible in both domains.

**Amended (V2.9)**: the device-domain rate is host-API-dependent, not universally 44.1 kHz. On macOS CoreAudio the codec runs natively at 44.1 kHz (bridge required, unchanged). On Windows the same C-Media codec's native audio-engine mix rate is 48 kHz, and its MME 44.1 kHz playback path paces ~1.4× slow (measured on the Win11 KVM rig: 50×20 ms writes block 1.36–1.42 s) — the TX drain falls behind, the 400 ms cap drops 24–34 % of voice frames, TX audio crackles. Windows TX therefore opens the same-name WASAPI entry at its native rate (48 kHz) and `feed_tx_audio` passes 48 kHz PCM through unchanged; the 44.1 kHz bridge applies only when the stream rate is actually 44.1 kHz. RX capture stays at 44.1 kHz MME on Windows (paces correctly).

## AD-012: Active-VFO-Aware Frequency Model

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | Poll `VS` + `FB` at 0.5s and treat `freq` set command as "apply to currently active VFO" |

**Problem**: Using `freq` as VFO-A-only could update the wrong oscillator when VFO-B is active.

**Rationale**: Active-VFO tracking keeps UI and CAT semantics aligned with front-panel behavior.

**Consequences**: State now carries `active_vfo`, `vfo_a_freq`, and `vfo_b_freq` continuously.

## AD-013: FT-710 Meter Calibration Tables

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | Convert raw RM meter values (0–255) into engineering units via piecewise-linear calibration tables in `config.py` |

**Problem**: Raw meter values are not user-meaningful and are non-linear.

**Rationale**: Calibration points from FT-710 rig data provide practical watt/SWR/volt/amp displays without firmware changes.

**Consequences**: UI meters show engineering units; calibration can be tuned independently of polling logic.

## AD-014: FT-710 CAT Errata Handling

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | Apply FT-710-specific command corrections: treat `DN` as step-down (never poll), use `PR00/PR01` for compressor, and map tuner control to `AC000/AC001/AC003` |

**Problem**: Yaesu CAT documentation ambiguities can cause unintended RF behavior (frequency drift, invalid tuner command forms).

**Rationale**: Runtime behavior is grounded in observed FT-710 responses and cross-command consistency.

**Consequences**: Safer default control path; ambiguous commands are either corrected or intentionally omitted.

## AD-015: Priority CAT Command Preemption

| Attribute | Value |
|-----------|-------|
| Type | Safety / Responsiveness |
| Status | Implemented |
| Decision | Introduce `send_priority_set_command()` and `_cancel_polls` cooperative abort so latency-sensitive commands (PTT/TUNE) preempt poll queries |

**Problem**: User TX/RX transitions can stall behind in-flight poll cycles if every query holds the serial lock to timeout.

**Rationale**: Priority commands set a cancel flag observed by poll loops, queued query waiters, and `_read_until()` threads, reducing worst-case handoff delay.

**Consequences**: Poll loops must be cancel-aware; UX is significantly more responsive during fast PTT/tune transitions.

## 8.16 Decision Summary

| ID | Topic | Status |
|----|-------|--------|
| AD-001 | FastAPI/Uvicorn backend | Implemented |
| AD-002 | Direct serial CAT (no Hamlib) | Implemented |
| AD-003 | Dirty-field state broadcasting | Implemented |
| AD-004 | Tagged dual-codec audio (Opus + PCM) | Implemented |
| AD-005 | scope_pipe standalone subprocess | Implemented |
| AD-006 | Dual-mode spectrum (FT4222 + fallback) | Implemented |
| AD-007 | PTT release safety flow | Implemented |
| AD-008 | PyAudio FT-710 auto-detection | Implemented |
| AD-009 | 7-task adaptive polling with bounded lock time | Implemented |
| AD-010 | Memory channels as server-side JSON | Implemented |
| AD-011 | Unified 48kHz TX audio pipeline | Implemented |
| AD-012 | Active-VFO-aware frequency model | Implemented |
| AD-013 | FT-710 meter calibration tables | Implemented |
| AD-014 | FT-710 CAT errata handling | Implemented |
| AD-015 | Priority CAT command preemption | Implemented |
