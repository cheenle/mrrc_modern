# 13. Feasibility Assessment (ART 0530)

## 13.1 Feasibility Summary

| Dimension | Assessment | Explanation |
| --- | --- | --- |
| Serial CAT/CI-V | High | FT-710 and FT-991A field paths plus shared profile/controller tests; Icom experimental models remain individually gated |
| Network radio control | Medium-High | G7250 RS-BA1, SCU-LAN10 and Flex have real RX-path evidence; SunSDR has basic acceptance. Whole-model verification remains separate |
| Spectrum | High architecturally, variable by model | Common 850-bin contract is stable; each producer has source-specific framing/axis risks and fallback remains truthful |
| RX audio | High | USB and several network adapters converge on 48 kHz; wall-clock pacing/backlog control addresses measured box behavior |
| TX audio | Backend-dependent | USB/SunSDR/Flex adapters exist only behind their gates; SCU-LAN10 TX is infeasible until its frame is characterized |
| Mobile/embedded | High / Medium | Browser/native contracts are established. AD-039 removes embedded multi-TLS pressure; AD-038 PCM16 is still design work |
| FT8 | High | Fast path + adaptive deep cascade is field-measured on W103D; native packaging remains platform-specific |
| Field appliance | High | Box configuration, support, metrics and updates exist; hardware/OS variation still requires clean-room and field checks |
| Product completeness | High for core, explicitly uneven at edges | Core control/media/record/FT8/roles are implemented; experimental models and future hardening are not presented as complete |

## 13.2 Risks

| ID | Risk | Probability | Impact | Mitigation |
| --- | --- | --- | --- | --- |
| R1 | Wrong/missing serial endpoint or baud | Medium | High | Registry/profile defaults, port logs, auto rate ladder where implemented, read-back setup |
| R2 | Real scope unavailable or malformed | Medium | Medium | Exact framing/foreign-stream rejection, reconnect and honest S-meter fallback |
| R3 | Wrong/silent audio device | Medium | Medium | Backend hints, loopback exclusion, explicit-choice warnings, actual-rate/level logs |
| R4 | TX release lost or process dies | Low | Critical | Chapter 15 layers, heartbeat/max-TX options, release-before-restart; radio TOT is the only process-death survivor |
| R5 | Opus/native codec unavailable | Low-Medium | Medium | Legacy PCM48 fallback; packaged library smoke tests; future PCM16 must fail explicitly, not mislabel PCM |
| R6 | Audio contention/queue starvation | Medium | High | Exclusive streams, bounded queues, dedicated TX executor, wall-clock RX pacing and drop evidence |
| R7 | FT4222 subprocess failure | Low-Medium | Low | Process isolation/restart and fallback |
| R8 | Stale frontend assets | Low-Medium | Medium | Revalidation, manual query bump, SW content-hash pins and contract tests |
| R9 | Unverified Icom profiles are wrong | Medium | Medium-High | AD-019 gate, provenance/unverified meters and `_diag_civ.py` measurement loop |
| R10 | Recording disk growth | Medium | Low-Medium | Usage display, free-space refusal and session cap; deliberate no-auto-delete policy |
| R11 | USB serial re-enumeration | Medium | Low-Medium | Fast bounded reconnect, tiered state refill, ENXIO classification and cabling/power guidance |
| R12 | Yaesu profile/filter/receiver assumptions differ by hardware | Medium | Medium-High | Profile command shapes, per-mode Hz mapping, read-back, dual-RX tests and field diagnostics; unverified TX gate |
| R13 | Diagnostic privacy or unauthenticated upload ingestion abuse | Medium | High | Allow-list+secret redaction, negative tests, receiver rate limits/unguessable ids/Basic-auth reads |
| R14 | FT8 cascade escalates too often or misses deadline | Medium | Medium | Measured policy knobs, direct counters and budget guard that skips escalation rather than returns late |
| R15 | SunSDR UDP loss/exclusive session | Medium | High for TX | Single engine, bound ports/queues and epoch-guarded bounded release retry |
| R16 | G7250 network handshake/substream differences produce successful login with silence | Medium | Medium | Three sockets, per-stream handshake/open retries, byte-level golden/fake-radio tests and serial fallback with named cause |
| R17 | SCU-LAN10 sequence/direction/session quirks silently drop setup/data | Medium | Medium-High | Per-port sequence counters, accept data `0x07/0x87`, full seven-phase reconnect and field packet counters |
| R18 | Flex MultiFlex object/stream confusion operates or displays another client's resources | Medium | High | Own handle/pan/slice/stream filtering, no `min(slices)` fallback, `slice set` form, live antenna/axis/aperture read-back |
| R19 | uvloop/native I/O differences bypass otherwise correct network logic | Medium | High | Raw socket send where required, unconnected VITA UDP, real-uvloop guards and common-controller contract tests |
| R20 | Blocking system/audio work stalls event loop and media/PTT | Medium | High | Off-loop D-Bus/PortAudio/device operations, lifecycle locks, lag probe and `/Perf` attribution |
| R21 | Terminal media backpressure disconnects its control/PTT channel | Medium | Critical | AD-039 drop-frame-not-socket policy, one-shot disconnect cleanup and legacy-socket regression tests |

## 13.3 Assumptions

| ID | Assumption | Confidence | Validation |
| --- | --- | --- | --- |
| A1 | Serial radios use the configured port/profile parameters | High for verified models | Identity/read-back and physical checklist |
| A2 | USB-audio radios expose an OS-recognizable codec; FT-891 instead uses an external interface | High/Medium per model | Device enumeration and actual stream rate |
| A3 | libopus is present or legacy PCM48 is acceptable | High | ctypes load and package smoke test |
| A4 | FTDI libraries match host architecture/version | Medium-High | scope_pipe startup/frame test |
| A5 | Clients support their documented WS/audio/canvas features | High | Browser/native/embedded acceptance |
| A6 | FT4222 library provenance remains compatible | Medium | SPI real-frame test |
| A7 | Experimental Icom profiles share enough CI-V surface for receive control | Medium | `_diag_civ.py`; no TX assumption |
| A8 | `lameenc` wheels/native packaging cover target platforms | High after package checks | Short packaged recording on each platform |
| A9 | Offline Yaesu tables are useful but not a substitute for a radio | Verified as a design principle | Provenance, read-back and field reports |
| A10 | FT-891/FT-991A factory 4800 and FT-891 external-audio facts are correctly recorded | High for cited field units | Profile evidence and repeat connection tests |
| A11 | Network radios' field-observed byte quirks are stable for the tested firmware | Medium-High | Golden tests plus firmware-specific revalidation after upgrades |
| A12 | soxr is available where high-quality streaming network conversion is required | High in packaged images | Import/rate/continuity checks; explicit degradation where documented |

## 13.4 Current Issues

| ID | Issue | Status | Resolution Path |
| --- | --- | --- | --- |
| I1 | Browser microphone needs a secure context | Open platform constraint | HTTPS entry and user permission flow |
| I2 | Audio device index/name configuration | Resolved | `MRRC_AUDIO_RX_DEVICE` / `_TX_DEVICE` |
| I3 | Per-band TX power not modeled | Open/N/A by backend | Hardware/global power model; do not invent per-band state |
| I4 | ATR-1000 unsupported | Resolved | AD/linkage in §9.8 and optional `/WSatr1000` |
| I5 | Digital modes/FT8 unsupported | Resolved for FT8 | §9.9/9.10; other modes remain out of scope |
| I6 | multi-client arbitration | PTT and TX-uplink ownership resolved; general freq/mode/set remains last-writer-wins | Define remaining control-lock/role policy without weakening release |
| I7 | Memory POST schema/backup | **Open**: writes slice/pad the payload but have no full schema validation or `.bak` recovery copy | Add strict payload validation, atomic write and backup recovery |
| I8 | Static path traversal | Resolved | Resolve+containment and security regressions |
| I9 | Password timing/default/first-login policy | Constant-time compare and default warning resolved; forced first-login change/shorter TTL remain | Authenticated password rotation/onboarding policy |
| I10 | Media subchannel reconnect | Resolved for legacy web clients | Independent backoff; terminal reconnect covered separately |
| I11 | iOS app PTT watchdog/scenePhase | Gesture release fixed; full watchdog/background layers still need device verification | Native client PTTManager completion |
| I12 | Connected-but-hung client stuck TX | Resolved as opt-in layers | Heartbeat + maximum continuous TX watchdog |
| I13 | Windows Proactor `stop()` can lose final SunSDR PTT0 | Open, observed while POSIX passes | Reproduce with packaged Windows loop and add a release-drain/lifecycle fix without blocking normal release |
| I14 | ESP32-C3 PCM16 resource profile | Implemented offline; firmware/hardware acceptance pending | AD-038 server implementation complete; firmware map/heap/network acceptance remains open |
| I15 | Flex native waterfall/DAX and stronger generation/reorder handling | Approved hardening design, not current behavior | Implement the 2026-11-16 design separately; keep current FFT/speaker semantics until then |
| I16 | AD-039 terminal endpoint | Implemented and released as box 1.28.86 · FP73; real embedded firmware and on-air modulation not yet accepted | Run reference-page end-to-end checks and later ESP32 fallback/safety acceptance |
| I17 | Whole-model verification after partial network RX success | Open for G7250/SCU-supported Yaesu/SunSDR/Flex | Per-model TX/audio/meter/reconnect checklist; do not flip flags from RX evidence alone |

## 13.5 Dependencies

| Dependency | Type | Status / degradation |
| --- | --- | --- |
| Python 3.12+ (source/desktop), FastAPI, Uvicorn | Runtime | Required |
| pyserial | Serial transport | Required for serial/instrument paths |
| NumPy/SciPy | DSP/media | Required by active DSP/network/FT8 paths; corrupted installs must be detected, not hidden |
| PyAudio/PortAudio | USB audio | Required for sound-card backends; network backends may replace it |
| soxr | Streaming network resampling | Required for designed quality in applicable adapters; documented fallback only where implemented |
| libopus | Main audio codec | Optional with PCM48 legacy fallback |
| lameenc | Recording | Required for MP3 recording |
| FTDI FT4222/D2XX | FT-710 scope | Optional, fallback to synthetic spectrum |
| SDRplay API + native shim/channelizer | Public SDR | Optional box feature |
| FT8 Fortran/C/FFTW/gomp runtime | FT8 | Built and shipped per platform |
| NetworkManager/bluez/ALSA/evdev | Box operations | Optional by platform; pages degrade rather than invent success |
| paramiko | Remote help | Optional support feature |
| Browser Web Audio/WASM/worklets | Client | Required for full browser media |

## 13.6 Feasibility Conclusion

The architecture is feasible and already supports substantially more than the original two serial radios. Its success comes from stable server/client contracts, one exclusive owner per device/session and explicit adaptation at backend boundaries. The main remaining risk is not basic feasibility but **evidence quality**: a green parser test, a logged-in network session or audible noise must not be promoted to verified RF operation. AD-019, per-source diagnostics and the open-issue list remain part of the product until their hardware and end-to-end acceptance closes.
