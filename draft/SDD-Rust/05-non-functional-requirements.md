# 5. Non-Functional Requirements

## 5.1 Performance

| ID | Requirement | Rust Target | Verification |
|----|-------------|-------------|--------------|
| RNFR-001 | RX audio latency | Less than 500 ms end-to-end | Listening test and frame timing logs |
| RNFR-002 | Control response | UI command acknowledgement within 200 ms on LAN | WS round-trip test |
| RNFR-003 | PTT priority | PTT/TUNE writes bypass or cancel queued polls | actor tests and hardware trace |
| RNFR-004 | Poll serial load | Same 7-task cadence and 250 ms poll timeout | serial monitor and unit tests |
| RNFR-005 | Spectrum cadence | 30 Hz broadcast schedule, real frames only on frame advance | scope service tests |

## 5.2 Reliability

| ID | Requirement | Rust Target |
|----|-------------|-------------|
| RNFR-010 | Serial ownership | Exactly one actor owns a serial device handle |
| RNFR-011 | Blocking I/O isolation | Serial/audio/driver calls never block async reactor threads |
| RNFR-012 | Reconnect behavior | Backoff reconnect reinitializes radio scope setup |
| RNFR-013 | Memory persistence | JSON writes are validated and atomic |
| RNFR-014 | Scope freshness | Bounded newest-data queue for CI-V scope segments |

## 5.3 Security

| ID | Requirement | Rust Target |
|----|-------------|-------------|
| RNFR-020 | Auth gating | Every HTTP protected route and WS endpoint validates token/cookie |
| RNFR-021 | Password config | Password comes from environment, never hardcoded except documented insecure default warning |
| RNFR-022 | Constant-time compare | Password/token comparisons avoid early-exit semantics where applicable |
| RNFR-023 | Static containment | Static paths reject parent traversal and absolute path injection |
| RNFR-024 | Token lifetime | Tokens expire and are cleared on process restart |

## 5.4 Audio Quality

| ID | Requirement | Rust Target |
|----|-------------|-------------|
| RNFR-060 | Codec rate | Opus/PCM codec domain remains 48 kHz |
| RNFR-061 | FT-710 bridge | 960 samples at 48 kHz map to 882 samples at 44.1 kHz per 20 ms |
| RNFR-062 | IC-7300 native audio | 48 kHz passthrough for RX and TX |
| RNFR-063 | TX queue | 60 ms prebuffer, bounded latency, oldest-frame drops counted |
| RNFR-064 | Codec fallback | PCM fallback remains wire-compatible |

## 5.5 Safety

| ID | Requirement | Rust Target |
|----|-------------|-------------|
| RNFR-070 | PTT release | Release command remains fast, fire-and-forget at radio command layer |
| RNFR-071 | Dead-man switch | WS disconnect releases PTT |
| RNFR-072 | Watchdog | Client and server watchdogs prevent stuck key-up |
| RNFR-073 | TX owner | PTT client's authenticated audio channel owns uplink |
