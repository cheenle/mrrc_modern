# 5. Non-Functional Requirements (ART 0507)

## 5.1 Performance Requirements

| ID | Requirement | Target | Priority | Verification |
|----|-------------|--------|----------|-------------|
| NFR-001 | RX audio latency | < 500ms end-to-end (radio speaker → browser speaker) | Critical | Listening test |
| NFR-002 | Control response | UI command ack within 200ms on LAN | High | WebSocket round-trip observation |
| NFR-003 | Spectrum bandwidth | ~1701 bytes/frame at ~30fps (~51KB/s) real scope; ~851 bytes/frame fallback | Medium | WS frame size inspection |
| NFR-004 | Audio transport bandwidth | Opus ~48–64kbps at 48kHz mono; PCM ~768kbps fallback | Medium | Network monitor |
| NFR-005 | CPU stability | No sustained overload from serial polling + audio + scope | High | Activity Monitor/top observation |
| NFR-006 | Serial port throughput | < 300 bytes/sec polling at 38400 baud (FT-710) or 115200 baud (IC-7300), well under limit | High | Serial monitor |
| NFR-007 | Waterfall render quality | 120-row history, adaptive colormap, frequency scale alignment | Medium | Visual inspection |
| NFR-008 | PTT response | < 100ms from touch to TX command | Critical | Timing logs |

## 5.2 Availability Requirements

| ID | Requirement | Target | Priority | Verification |
|----|-------------|--------|----------|-------------|
| NFR-010 | Service restart | `start.sh` / `stop.sh` manage background service | High | Script execution |
| NFR-011 | WebSocket reconnect | Frontend auto-reconnects with exponential backoff (1s→30s) | High | Connection loss test |
| NFR-012 | PTT release safety | TX0; always sent on release, even if connection lost | Critical | Force-close browser during TX |

## 5.3 Security Requirements

| ID | Requirement | Target | Priority | Verification |
|----|-------------|--------|----------|-------------|
| NFR-020 | Session authentication | All routes and WS endpoints require valid auth token (`ft710_auth` cookie + `?token=` query param) | High | Unauth curl test |
| NFR-021 | Password configurable | `FT710_WEB_PASSWORD` env var; never hardcoded in repo | Critical | Config review |
| NFR-022 | Token lifetime | 30-day session tokens, cleared on server restart | Medium | Cookie inspection |
| NFR-023 | Open redirect prevention | Login redirect validates same-origin target | High | Code review of `_auth_middleware` and `/login` |

## 5.4 Compatibility Requirements

| ID | Requirement | Target | Priority | Verification |
|----|-------------|--------|----------|-------------|
| NFR-030 | iOS Safari | RX audio, touch controls, PWA support | High | iPhone test |
| NFR-031 | Desktop Chrome/Safari/Firefox | Full functionality | High | Desktop browser test |
| NFR-032 | Radio firmware | Protocol compatible with current FT-710 or IC-7300/MK2 firmware for the selected backend | Critical | Radio connect and control test |
| NFR-033 | macOS + Linux | Server runs on both platforms | High | Cross-platform build test |
| NFR-034 | opus_rx.py | Works on arm64 (Apple Silicon) and x86_64 | High | ctypes libopus loading test |

## 5.5 Operability Requirements

| ID | Requirement | Target | Priority | Verification |
|----|-------------|--------|----------|-------------|
| NFR-040 | Logging | Startup, CAT connect, scope status, audio device selection logged | High | `logs/` directory output |
| NFR-041 | Configuration | `MRRC_RADIO_MODEL`, `IC7300_CIV_ADDR`, `FT710_SERIAL_PORT`, `FT710_WEB_PORT`, `FT710_WEB_PASSWORD`, `FT710_WEB_HOST` env vars | Medium | Env var test |
| NFR-042 | PID file | `.ft710-server.pid` tracks running process | Medium | `start.sh` / `stop.sh` behavior |
| NFR-043 | Static cache safety | Service worker bypasses JS/HTML cache | High | `sw.js` review |

## 5.6 Maintainability Requirements

| ID | Requirement | Target | Priority | Verification |
|----|-------------|--------|----------|-------------|
| NFR-050 | Module boundaries | CAT, audio, scope, state, poll, config each in separate files | Medium | Code review |
| NFR-051 | Explicit gaps | Documented in SDD until implemented or removed | High | SDD review |
| NFR-052 | No external middleware | Zero Hamlib/rigctld/TCI dependency | High | Import review |

## 5.7 Audio Quality Requirements

| ID | Requirement | Target | Priority | Verification |
|----|-------------|--------|----------|-------------|
| NFR-060 | RX sample rate | FT-710: 44.1kHz native capture, resampled to 48kHz for Opus; IC-7300/MK2: 48kHz native capture, no resample | Critical | PyAudio stream config |
| NFR-061 | Opus bitrate | 64kbps default, 16-128kbps adjustable | Medium | Codec config |
| NFR-062 | TX audio quality | Clean mic audio reaches radio without distortion | High | On-air listening test |
| NFR-063 | AudioWorklet playback | Jitter buffer: 220ms prebuffer, 90ms recovery, 800ms max | High | Listening under network jitter |
| NFR-064 | PCM fallback | Automatic when libopus unavailable (server or browser) | High | Start without libopus |
| NFR-065 | PyAudio device selection | Auto-detect per-backend device hints (e.g., "FT-710"/"YAESU" for FT-710; generic "USB Audio CODEC"/"USB Audio Device"), then mono/full-duplex heuristics; fallback to system default | Medium | Device enumeration log |
