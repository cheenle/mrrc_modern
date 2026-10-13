# Rust Production Replacement Acceptance Matrix

This matrix is the go/no-go checklist before the Rust draft can replace the production Python runtime.

## Protocol Compatibility

| Gate | Required Evidence | Status |
|------|-------------------|--------|
| Same WS endpoints | `/WSradio`, `/WSspectrum`, `/WSaudioRX`, `/WSaudioTX`, optional `/WSatr1000` constants tested | Draft core tested |
| Same audio tags | `0x00` PCM and `0x01` Opus tested | Draft core tested |
| Same spectrum payload sizes | 851-byte v1 and 1701-byte v2 tested | Draft core tested |
| Same radio JSON shapes | serde models parse current `set`, `get`, memory, ping messages | Draft core partial |
| Same auth behavior | HTTP cookie plus WS token required | Runtime pending |

## Radio Hardware

| Gate | Required Evidence | Status |
|------|-------------------|--------|
| FT-710 CAT control | Frequency, mode, PTT, filter, tuner, compressor command traces match production | Core builders tested; hardware pending |
| FT-710 forbidden commands absent | No `DN;`, `PR02;`, `AC010;`, `AC011;` generated | Draft core tested |
| IC-7300 CI-V codec | BCD, frame parse, scope segments match real captures | Draft core partial |
| Scope reinitialization | Startup and reconnect re-enable scope | Runtime pending |
| Power control guards | Maintenance-only behavior preserved | Runtime pending |

## Audio

| Gate | Required Evidence | Status |
|------|-------------------|--------|
| 48 kHz codec domain | Tests prove 960-sample 20 ms codec frame | Draft core tested |
| FT-710 bridge | 960 to 882 and 882 to 960 frame mapping | Draft core tested |
| IC-7300 passthrough | 48 kHz device path has no SRC | Runtime pending |
| TX queue statistics | prebuffer, max cap, oldest drops counted | Draft core tested |
| Windows TX to RX recovery | RX restart after TX release on affected hosts | Runtime pending |

## PTT Safety

| Gate | Required Evidence | Status |
|------|-------------------|--------|
| Normal release | `ptt=false` writes release immediately | Runtime pending |
| Disconnect dead-man | Active owner disconnect triggers release | Draft state machine tested |
| Max TX watchdog | Continuous TX exceeds limit and releases | Draft state machine tested |
| No blocking release verify | Code review confirms no post-release 3x200 ms loop | Draft policy documented |
| TX owner handoff | PTT client's token claims audio uplink | Draft session tested |

## Operations

| Gate | Required Evidence | Status |
|------|-------------------|--------|
| Static path containment | traversal rejected | Draft core tested |
| Memory schema validation | invalid slots rejected before persistence | Draft core tested |
| Atomic memory write | temp file then rename | Draft core tested |
| SDD production update | root `SDD/` amended after approval | Not started |
| Rollback plan | Python runtime remains available until Rust passes soak | Not started |
