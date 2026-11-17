# 5. Non-Functional Requirements (ART 0507)

## 5.1 Performance Requirements

| ID | Requirement | Target | Priority | Verification |
| --- | --- | --- | --- | --- |
| NFR-001 | RX audio latency | <500 ms end-to-end on a healthy LAN; stale queued audio is dropped rather than replayed late | Critical | Listening test, queue/pace counters and W103D field runs |
| NFR-002 | Control response | UI acknowledgement within 200 ms on LAN when the device link is healthy | High | WS round-trip and command/read-back observation |
| NFR-003 | Spectrum bandwidth | AD-025 high/mid/low = 1701 B full or 851 B short frame × divider; legacy sockets default high. Measured FT-710 real scope is 11.1 fps (about 151 kbps high payload before compression; mid/low divide it). Real frames are sent only when new; identical synthetic fallback frames are suppressed. Wire compression is client-dependent | Medium | `spectrum_profile` tests, frame probes and session byte metrics |
| NFR-004 | Audio transport bandwidth | Opus48 normally 48–64 kbps, PCM48 about 768 kbps. Negotiated AD-038 PCM16 costs 256 kbps/direction (641 B/20 ms including tag) and is implemented for RX/TX | Medium | Network metrics and codec/profile tests |
| NFR-005 | CPU stability | No sustained overload from polling, media, DSP and scope; per-channel SDR limits derive from measured cores | High | `/Perf`, host measurements and load tests |
| NFR-006 | Serial pacing | Query cost and baud determine tier pacing; nominal poll duty target 45%, with TX/settings throttles and priority preemption | High | Scheduler tests and 4800–115200 baud field logs |
| NFR-007 | Waterfall quality | 120-row history, source-correct frequency/intensity axes, no fabricated partial frames or duplicate stale rows | Medium | Visual and parser/axis tests |
| NFR-008 | PTT response | Key command normally <100 ms; release is fire-and-forget and never waits for repeated verification | Critical | Timing and safety tests |

## 5.2 Availability Requirements

| ID | Requirement | Target | Priority | Verification |
| --- | --- | --- | --- | --- |
| NFR-010 | Service lifecycle | Launchers/systemd/start-stop scripts own a clean restart; config-save restart releases PTT first | High | Lifecycle and restart tests |
| NFR-011 | WS recovery | Control and media subchannels reconnect independently with bounded backoff; terminal reconnect re-registers all logical channels | High | Connection-loss and contract tests |
| NFR-012 | Release availability | Every release path remains accepted regardless of model gate, profile or media negotiation | Critical | Chapter 15 fault-injection tests |
| NFR-013 | Media degradation | A slow terminal media path drops frames, not the control/PTT socket; pure legacy media sockets retain their eviction behavior | High | AD-039 backpressure tests |

## 5.3 Security Requirements

| ID | Requirement | Target | Priority | Verification |
| --- | --- | --- | --- | --- |
| NFR-020 | Authentication and roles | Protected routes/WS require a valid session; public/login paths are explicit; operator, listen and SDR domains stay separate | High | Unauthenticated and cross-role tests |
| NFR-021 | Secret handling | Passwords/keys are configurable, compared with `hmac.compare_digest`, never hardcoded for a deployment or written to logs/URLs | Critical | Config, auth and bundle review |
| NFR-022 | Token lifetime | 30-day random tokens persist atomically in `data/auth-tokens.json` (0600); transport is Bearer → cookie → deprecated query | Medium | Restart, cookie/header and AD-024 tests |
| NFR-023 | Redirect and path safety | Login targets remain same-origin; static paths resolve and stay under `STATIC_DIR`; missing extension paths do not fall through to the SPA | High | Security regression tests |
| NFR-024 | Login throttling | Per-IP limit defaults to 5 attempts/300 s; `MRRC_LOGIN_MAX_ATTEMPTS=0` explicitly disables it and startup says so | High | Limiter/configuration tests |

## 5.4 Compatibility Requirements

| ID | Requirement | Target | Priority | Verification |
| --- | --- | --- | --- | --- |
| NFR-030 | iOS Safari | RX, touch controls, PWA behavior and secure microphone context | High | Device test |
| NFR-031 | Desktop browsers | Current Chrome/Safari/Firefox operate the main UI | High | Browser test |
| NFR-032 | Backend transport | Registry-derived serial/network metadata; hardware-unverified models remain gated even after partial RX field success | Critical | Factory/profile/setup and field tests |
| NFR-033 | Host platforms | Windows, macOS and Linux packages; W103D and Raspberry Pi images share the service layer | High | Packaging/clean-room tests |
| NFR-034 | Native codec portability | libopus loads on arm64/x86_64 or media explicitly degrades to PCM48 | High | ctypes and packaged smoke tests |
| NFR-035 | Legacy protocol stability | Existing browser/iOS/Android endpoints retain byte contracts; new spectrum/audio/terminal features require explicit socket behavior | Critical | WS protocol and native parser tests |

## 5.5 Operability Requirements

| ID | Requirement | Target | Priority | Verification |
| --- | --- | --- | --- | --- |
| NFR-040 | Logging and evidence | Startup, real endpoints, audio rates, scope source, TX ownership/drops, reconnects and tunnel failures are visible without secret leakage | High | Logs and support bundle |
| NFR-041 | Configuration | Registry-derived model/connection metadata plus environment-backed radio, network, auth, recording, SDR and safety settings; writes are read back | Medium | Setup/read-back tests |
| NFR-042 | Process ownership | PID files where scripts use them; systemd/launchers supervise installed instances | Medium | Lifecycle tests |
| NFR-043 | Static cache safety | HTML/JSON/service worker revalidate; fingerprinted assets and content-hash pins prevent new HTML with stale JS/CSS | High | Cache-busting and conditional-request tests |
| NFR-044 | Event-loop health | Blocking PortAudio, D-Bus, serial, instruments and native calls leave the loop; lag is measured and surfaced at `/Perf` | High | `test_event_loop_blocking.py`, lag probe and field traces |

## 5.6 Maintainability Requirements

| ID | Requirement | Target | Priority | Verification |
| --- | --- | --- | --- | --- |
| NFR-050 | Module boundaries | Each protocol/device owner has one boundary; pure framing/math/sharing logic is hardware-free testable | Medium | Code and dependency review |
| NFR-051 | Explicit gaps | Unimplemented, unverified and degraded behavior remains named in SDD/UI | High | SDD and capability review |
| NFR-052 | No external radio middleware | Zero Hamlib/rigctld/TCI runtime dependency | High | Import review |
| NFR-053 | Common controller surface | Every backend controller exposes the server's minimum contract (`connected`, connect/disconnect, PTT/frequency/mode); missing attributes degrade safely rather than cancel restart | Critical | Backend controller contract tests |

## 5.7 Audio Quality and Data Requirements

| ID | Requirement | Target | Priority | Verification |
| --- | --- | --- | --- | --- |
| NFR-060 | RX sample rate | Main bus is 48 kHz mono. FT-710/Yaesu USB bridges 44.1 kHz; Icom uses 48 kHz; network adapters convert explicitly. AD-038 does not change this bus | Important | DSP, rate and continuity tests |
| NFR-061 | Opus bitrate | 64 kbps default with supported adjustment; no unannounced global codec/rate change | Medium | Codec configuration |
| NFR-062 | TX audio quality | Clean modulation without distortion, silent-carrier fallback or cross-source gain leakage | High | On-air test and queue/peak counters |
| NFR-063 | AudioWorklet playback | Time-based jitter buffer (220 ms prebuffer, 90 ms recovery, 800 ms cap) and server wall-clock pacing | High | Jitter and long-run tests |
| NFR-064 | PCM fallback | Legacy clients use PCM48 when Opus is unavailable; a PCM16-only client must fail explicitly if negotiation fails, and negotiated `0x02` PCM16 remains 256 kbps/direction | High | Codec/profile failure tests |
| NFR-065 | Device selection | Backend hints precede generic USB and heuristics; automatic tiers never select computer loopbacks, explicit choices warn, FT-891 uses an external interface | Medium | Enumeration and loopback tests |
| NFR-066 | QSO recording | Incremental 16 kHz mono MP3 ≤64 kbps, ±50 ms timeline tolerance, free-space refusal and visible usage; no automatic deletion | Medium | Recorder/API/range/free-space tests |
| NFR-067 | Verification honesty | Unverified models report false, gate key-up, name unverified meters and retain table provenance; partial RX acceptance does not imply TX verification | Critical | Profile/capability/UI/docs tests |
| NFR-068 | Data ownership | Only redacted operator-initiated diagnostics leave the machine; secrets, certificates, recordings, memories and tuner learning stay out | Critical | Bundle/API negative tests and manifest hit count |
| NFR-069 | Factory credential disclosure | Preflash credentials and their batch-image trade-off are documented and changeable; non-preflash builds do not invent fixed secrets | High | Preflash/docs tests |
