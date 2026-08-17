# MRRC FT-710 Test Suite

## Overview

Automated test suite covering the core backend modules. All tests run **without hardware** — no radio, no serial port, no USB audio device needed. 592 tests across 29 test modules.

```bash
python -m unittest discover -s tests -v
```

## Test Results Summary

| Metric | Value |
|--------|-------|
| Total tests | 592 |
| Passed | 592 (with all optional dependencies installed) |
| Skipped | 4 certificate tests when `cryptography` is unavailable |
| Failed | 0 |
| Execution time | ~13s (harness tests spawn CLI subprocesses) |

## Test Modules

### 1. test_radio_state.py — RadioState (33 tests)

SDD coverage: §7.2, AD-003, §9.7

| Class | Tests | Covers |
|-------|-------|--------|
| `RadioStateFieldMutationTests` | 8 | Field updates, dirty tracking, unknown field handling, batch mutation |
| `RadioStateDerivedPropertiesTests` | 13 | active_freq, mode_name, band_name, is_transmitting, s_meter_dbm, s_unit, preamp_label, attenuator_label |
| `RadioStateSerializationTests` | 6 | to_dict (core + derived), to_dirty_dict, value accuracy |
| `RadioStateFromSyncResultTests` | 6 | CAT response parsing, empty data, malformed data, booleans, preamp/att, tuner |

### 2. test_cat_controller.py — CAT Protocol (30 tests)

SDD coverage: AD-002, §9.6, §10.4

| Class | Tests | Covers |
|-------|-------|--------|
| `CatCommandFormattingTests` | 15 | FA, FB, MD0, TX, SM0, SH00, AG, PC, PA0, RA0, NB0, NR0, BC, PR, PS, ST, VS, SS, AC, BS — all command formats |
| `CatResponseParsingTests` | 7 | Frequency parse, S-meter parse, mode parse, PTT parse, IF response parse, filter width parse, error detection |
| `CatControllerMockedTests` | 8 | Command terminator (;), query vs set, ASCII encoding, SH two-digit width format, write-only set, PTT verify sequence, available-port diagnostics on connect failure |

### 3. test_config.py — Configuration Tables (28 tests)

SDD coverage: §7.2, §10.4, NFRs

| Class | Tests | Covers |
|-------|-------|--------|
| `ModeTableTests` | 5 | Mode name↔num mapping, bidirectional lookup, display names, UI_MODES |
| `BandTableTests` | 8 | Band list structure, get_band_for_frequency (20m/40m/80m/10m/edge cases) |
| `FilterTableTests` | 6 | Filter widths by mode (SSB, CW, FM), get_filter_hz |
| `SMeterCalibrationTests` | 4 | raw_to_dbm monotonic, raw_to_s_unit labels (S0–S9, +10–+60) |
| `ConfigConstantsTests` | 5 | PREAMP_LABELS, ATTENUATOR_LABELS, SCOPE_SPANS, MEM_CHANNEL_COUNT, AUTH_CONFIG |

### 4. test_audio.py — Audio Handler + Opus Codec (75 tests)

SDD coverage: AD-004, NFR-060–NFR-065

| Class | Tests | Covers |
|-------|-------|--------|
| `CodecTagTests` | 4 | AUDIO_TAG_PCM (0x00), AUDIO_TAG_OPUS (0x01), tag distinctness, 1-byte fit |
| `OpusConstantsTests` | 7 | RX_RATE=48000, FRAME_SAMPLES=960, DEFAULT_BITRATE=64000, MIN=8000, MAX=128000, Windows packaged opus.dll search paths |
| `TxFrontendContractTests` | 12 | TX worklet/worker contract: 48kHz, frame sizes, packet format, mutable intentional-close cleanup flag |
| `RxRecordingFrontendTests` | 5 | RX recording (MP3/lamejs) frontend contract |
| `TXBufferTests` | 9 | TX jitter buffer pre-buffer/cap behavior and oldest-frame drop diagnostics |
| `TXReleaseOrderTests` | 3 | PTT release ordering: audio drain before TX0 |
| `RXBackpressureTests` | 3 | RX broadcast backpressure handling |
| `AudioFrameFormatTests` | 6 | Tagged PCM/Opus frame format, Int16 range, 768kbps PCM bandwidth, multi-frame tags |
| `AudioDeviceDetectionTests` | 2 | FT-710 name pattern matching, non-FT-710 rejection |
| `USBCodecDeviceSelectionTests` | 10 | Generic USB-audio tier ("USB Audio CODEC"/"USB Audio Device"): wins over mono/full-duplex heuristics (RX+TX), per-host-API duplicates, explicit name lock, "USB Audio" common-prefix lock |
| `RestartRxTests` | 4 | Windows full-duplex wedge workaround: `restart_rx()` stop→start order, non-Windows no-op, RX-not-running guard, failed-reopen path |
| `TxDeviceDomainTests` | 4 | Fixed 44.1kHz TX device domain: exact 960→882/1764-byte conversion, stale 48k rate cannot bypass SRC, prebuffer/cap budgets use 44100 |
| `StartTxWindowsTests` | 2 | `start_tx` end-to-end: Windows keeps the selected device at 44.1kHz/882 frames; macOS stays 44.1kHz |
| `PortAudioReinitTests` | 4 | RX/TX PortAudio reinit recovery, bounded give-up, and Windows re-enumeration preserving 44.1kHz/882-frame TX |

### 5. test_server_ws_protocol.py — WebSocket Protocol (51 tests)

SDD coverage: §9.2, §9.6, §10.4, §15

| Class | Tests | Covers |
|-------|-------|--------|
| `WSMessageFormatTests` | 11 | fullState, stateUpdate, set, get, ping/pong, error, memChannels, memSave, value, legacy colon format |
| `WSAuthTests` | 4 | Token format (64 hex chars), valid/invalid token check, WS close code 4001 |
| `PTTSafetyLogicTests` | 10 | TX1/TX0 commands, dead-man switch (3 conditions), watchdog retry count, sendBeacon format, tx audio stop signal, m: settings format |
| `StateBroadcastLogicTests` | 5 | Meter logging, atomic band commands, frontend band fallback, partial-field rendering, cache-busted assets |
| `TXUplinkOwnershipTests` | 7 | Owner-disconnect promotion, PTT-client token claim, same-token replacement takeover, cross-token isolation, per-socket token tracking |
| `CookieSettingsPersistenceTests` | 14 | Cookie persistence plus dirty-state, stale-poll, band/filter, and lazy-scope source contracts |

### 6. test_poll_scheduler.py — Poll Scheduler (17 tests)

SDD coverage: AD-009, §9.6

| Class | Tests | Covers |
|-------|-------|--------|
| `PollTierStructureTests` | 6 | Tier intervals (100ms/500ms/2s/5s), tier commands, throughput limit |
| `PollSkipLogicTests` | 4 | Skip field accumulation, expiry, multi-field skip, duration types |
| `PollingOrderTests` | 3 | User command priority over poll, polling pause after user command, resume after skip expiry |
| `TXMeterPollingPreemptionTests` | 1 | TX-meter cycle yields between RM queries for priority commands |
| `WatchdogReconnectTests` | 2 | `on_reconnected` hook fires after watchdog reconnect (scope re-init), hook failure is non-fatal |
| `IFPollRecoveryTests` | 1 | `serial_connected` recovers to True on the next successful poll after a failure streak; first-poll freq logging must not raise (None-guard regression) |

### 7. test_scope_frame.py — Scope Frame Parsing (7 tests)

SDD coverage: AD-005, AD-006, §9.5

| Class | Tests | Covers |
|-------|-------|--------|
| `ScopeFrameTests` | 3 | Parse validation (sync + metadata), sync rejection, pipe payload round-trip |
| `FrameQualityTests` | 4 | All-zero spectrum, all-ones spectrum, normal spectrum metrics (nonzero_pct, dynamic_range) |

### 8. test_radio_state_scope.py — Scope Fields in State (1 test)

SDD coverage: §7.2 (ScopeFrame entity)

### 9. test_scope_handler_fallback.py — S-Meter Fallback (1 test)

SDD coverage: AD-006, §9.5.2

### 10. test_scope_runtime_config.py — SPI Clock Config (2 tests)

SDD coverage: AD-005, §12.2

### 11. test_server_scope_init.py — Scope CAT Init (2 tests)

SDD coverage: AD-005, §9.5.1

Requires `fastapi` (installed in the project venv).

### 12. test_memory_recall.py — Memory Channel Recall (3 tests)

SDD coverage: §10.4 (memory recall applies stored frequency + mode)

| Class | Tests | Covers |
|-------|-------|--------|
| `MemoryRecallTests` | 2 | Recall applies frequency and mode via CAT |
| `MemoryButtonSourceTests` | 1 | Frontend memory button contract |

### 13. test_quiet_logging.py — Logging Noise Control (4 tests)

| Class | Tests | Covers |
|-------|-------|--------|
| `QuietLoggingSourceTests` | 3 | High-frequency polls stay at DEBUG, no per-frame INFO spam |
| `TXOnlyMeterResetTests` | 1 | TX-only meters zeroed on TX→RX transition |

### 14. test_scope_pipe_restart.py — Scope Pipe Restart (3 tests)

SDD coverage: AD-005 (pipe subprocess lifecycle)

| Class | Tests | Covers |
|-------|-------|--------|
| `ScopePipeRestartTests` | 1 | Exited pipe can restart while the previous reader task finishes |
| `ScopePipeHeartbeatTests` | 2 | len=0 stdout heartbeat accepted silently by the server reader; scope_pipe emits the heartbeat (dead-parent EPIPE detection) |

### 15. test_windows_launcher.py — Windows Launcher (13 tests)

SDD coverage: §12.2 (Windows packaging)

| Class | Tests | Covers |
|-------|-------|--------|
| `WindowsLauncherTests` | 7 | Local browser URL selection for wildcard binds; FTDI dir absolutized; mem_channels seeding incl. PyInstaller 6 `_internal` fallback; frozen launcher never falls back to re-spawning itself |

### 16. test_windows_packaging_files.py — Windows Packaging Files (3 tests)

SDD coverage: §12.2 (Windows packaging)

### 17. test_windows_packaging_paths.py — Windows Packaging Paths (8 tests)

SDD coverage: §12.2 (Windows packaging)

| Class | Tests | Covers |
|-------|-------|--------|
| `WindowsPackagingPathTests` | 4 | Frozen-runtime resource path resolution |
| `ScopePipeCommandTests` | 2 | scope_pipe command construction under frozen runtime |
| `ResourceDirTests` | 2 | `_resource_dir()` prefers `_MEIPASS` when frozen (PyInstaller 6 `_internal` layout), falls back to SCRIPT_DIR |

### 18. test_sdd_harness.py — SDD-Guardian Context Harness (27 tests)

SDD coverage: NFR-051 (explicit gaps documented), §14 (doc-sync discipline)

| Class | Tests | Covers |
|-------|-------|--------|
| `ConstraintRegistryTests` | 5 | constraints.json well-formed: required fields, unique ids, valid severities, regexes compile, SDD traceability, core-module coverage |
| `HarnessCliTests` | 10 | prime digest, context routing, check blocks DN/SH0NN (exit 2), clean passes, hook blocks/allows/fail-open, core files stay clean |
| `KnowledgeIndexTests` | 4 | index.json: chapter files exist, every topic ref resolves to live SDD text, topics reachable + routed, core-area coverage |
| `KnowledgeCliTests` | 8 | Live extraction of AD/NFR/UC/issue/section, brief includes decisions + requirements + risks, Chinese keyword routing |

### 19. test_atr1000_tuner.py — TunerStorage LC-Learning (36 tests)

SDD coverage: §9.8, §11.1 (TunerStorage)

| Class | Tests | Covers |
|-------|-------|--------|
| `LearnGateTests` | 6 | Learn gate SWR 1.0–1.8 acceptance/rejection |
| `NeedsVerifyTests` | 2 | Verify-needed detection for learned entries |
| `OverwritePolicyTests` | 4 | When a new learn overwrites an existing entry |
| `FindBestTests` | 7 | 1kHz keys, ±5kHz nearest-match lookup |
| `TuneParamsTests` | 3 | LC parameter derivation/validation |
| `PersistenceTests` | 5 | JSON save/load round-trip, atomic writes |
| `DeleteClearStatsTests` | 7 | Entry delete, clear, statistics |
| `SingletonTests` | 2 | Shared store instance behavior |

### 20. test_atr1000_client.py — ATR1000 WS Client (50 tests)

SDD coverage: §9.8, §11.1 (ATR1000Client)

| Class | Tests | Covers |
|-------|-------|--------|
| `FrameEncodeTests` | 5 | Binary frame encoding [0xFF,CMD,LEN,DATA] |
| `FrameParseTests` | 13 | Binary frame parsing |
| `LearningBufferTests` | 13 | 4-sample stability-window learning |
| `MeterLearningFlowTests` | 6 | Learning flow from meter pushes |
| `RelayThrottleTests` | 5 | 5s relay-write throttle |
| `StateCallbackTests` | 3 | `on_change` callback |
| `TuningHeuristicTests` | 5 | Tuning-clear heuristics |

### 21. test_atr1000_server.py — Server Linkage + Tune Assist (13 tests)

SDD coverage: §9.8, §15 (tune-assist carrier safety)

| Class | Tests | Covers |
|-------|-------|--------|
| `TuneAssistSkippedTests` | 1 | Tune skipped when SWR≤1.6 |
| `TuneAssistSuccessTests` | 1 | SWR improved ≥0.02 → keep + learn |
| `TuneAssistRollbackTests` | 1 | No improvement → rollback relays |
| `TuneAssistNoMeterTests` | 1 | Meter-wait timeout path |
| `LinkageHookTests` | 3 | Freq-dirty → notify_freq, TX → notify_tx hooks |
| `SourceGuardTests` | 6 | Disabled/default guard: hooks short-circuit, no client task, frozen-store env override |

### 22. test_scope_pipe_tx.py — Scope Pipe TX Pause (13 tests)

SDD coverage: AD-005 (V2.7 amendment — stdin control channel + Windows tree kill), §9.5.1

| Class | Tests | Covers |
|-------|-------|--------|
| `ApplyControlLineTests` | 6 | `TX:1`/`TX:0` parsing: activate, one-shot resync arm, idempotence, unknown-line ignore, whitespace/case tolerance |
| `NotifyScopePipeTxTests` | 5 | Server → pipe stdin notify: TX/RX transitions, no-write on unchanged state, force resend, dead-pipe guard |
| `TerminateProcessTreeTests` | 2 | Windows `taskkill /PID /T /F` vs POSIX SIGTERM selection |

### 23. test_ssl_bootstrap.py — Self-Signed TLS Bootstrap (6 tests)

SDD coverage: V2.10 (HTTPS-by-default launcher bootstrap)

| Class | Tests | Covers |
|-------|-------|--------|
| `SelfSignedCertTests` | 4 | First-run generation, PEM validity, SAN coverage (localhost/hostname/IPs), self-issued 10-year server cert, idempotent reuse |
| `CryptoMissingTests` | 1 | Graceful None when cryptography is unavailable |
| `LanIpTests` | 1 | LAN IP detection excludes loopback, IPv4-parseable |

### 24. test_power_switch.py — Radio Power Command Guards (9 tests)

SDD coverage: V2.11 (header power switch), V2.12 (boot-window guard + PS1 verify, 2026-07-27 CAT-MCU wedge incident)

| Class | Tests | Covers |
|-------|-------|--------|
| `PowerOnRadioTests` | 4 | PS1 verify-first-attempt, retry-until-answer, give-up after N attempts, boot window armed |
| `PowerSetCommandGuardTests` | 5 | PS0 rejected in boot window, PS0 refused while TX, PS0 double-send, PS1 failure error message, PS1 success state update |

### 25. test_config_ic7300.py — IC-7300 Config Tables (22 tests)

SDD coverage: AD-016, §7.2

| Class | Tests | Covers |
|-------|-------|--------|
| `ConnectionTests` | 1 | CI-V defaults (115200 8N1, address 0x94) |
| `ModeTableTests` | 3 | CI-V mode codes, inverse map consistency, UI mode names reused |
| `BandTests` | 4 | Band shape (FT-710 minus `bsr`), radio coverage, expected bands, get_band_for_frequency |
| `FilterTableTests` | 2 | FIL1-3 filter model, defaults cover all UI modes |
| `MeterCalTests` | 7 | Monotonic tables, S-meter/power/SWR reference points, interpolation, meter sub-codes |
| `ScopeTableTests` | 3 | Span codes, span Hz consistency, fixed-mode edges |
| `PreampAttTests` | 2 | Preamp labels, attenuator steps |

### 26. test_civ_codec.py — CI-V Codec (45 tests)

SDD coverage: AD-016, §9.6

| Class | Tests | Covers |
|-------|-------|--------|
| `FramingTests` | 4 | CI-V frame build/parse (0xFE 0xFE … 0xFD) |
| `ParserTests` | 11 | Stream parser: partial frames, garbage resync, multi-frame |
| `EchoTests` | 3 | Echo frame detection/drop |
| `FreqBcdTests` | 5 | Frequency BCD encode/decode round-trip |
| `LevelBcdTests` | 5 | Level BCD encode/decode |
| `ScopeSegmentTests` | 4 | 0x27 scope segment parsing |
| `ScopeAssemblerTests` | 7 | Waveform assembly from segments |
| `ScaleUpsampleTests` | 6 | 475 bins → scale 160→255 → upsample 850 |

### 27. test_civ_controller.py — CI-V Controller (16 tests)

SDD coverage: AD-016, §9.6

| Class | Tests | Covers |
|-------|-------|--------|
| `ConnectTests` | 1 | Connect enables CI-V transceive |
| `FrequencyTests` | 3 | get_frequency with echo + broadcast interleaved, set frame format, VFO-B rejection |
| `ModeTests` | 2 | Mode+FIL decode, set_mode resends current FIL |
| `AckTests` | 2 | NG raises CivNak, OK resolves set |
| `TimeoutTests` | 2 | Retry once then raise, send_command returns None on timeout |
| `ScopeDemuxTests` | 1 | 0x27 segment demuxed between request and response |
| `PendingFifoTests` | 1 | Same-key pending futures resolve in order |
| `FatalErrorTests` | 2 | Fatal write/read error flips connected False |
| `PriorityTests` | 2 | PTT uses priority path, send_command yields when polls cancelled |

### 28. test_civ_scope.py — CI-V Scope Producer (12 tests)

SDD coverage: AD-016, §9.5

| Class | Tests | Covers |
|-------|-------|--------|
| `CivScopeProducerTests` | 11 | Full waveform → ScopeHandler, amplitude scaling, center/fixed metadata, on_frame once per waveform, sequence gap drops waveform, stop drains + disconnects, notify_tx no-op, callback replacement, stall watchdog warns once, idempotent start |
| `BackendFactoryScopeProducerTests` | 1 | create_scope_producer returns the CI-V producer |

### 29. test_backend_factory.py — Backend Factory + Capabilities (20 tests)

SDD coverage: AD-016

| Class | Tests | Covers |
|-------|-------|--------|
| `CreateBackendTests` | 6 | ft710/ic7300 keys, ic7300mk2 alias, key normalization, unknown model ValueError, MRRC_RADIO_MODEL env default |
| `CapabilitiesTests` | 3 | Capability keys/values, to_dict JSON-serializable, dataclass round-trip |
| `BackendUiTableTests` | 4 | FT-710 bands/ui_modes/filter tables match config, scope producer created |
| `IC7300CapabilitiesTests` | 4 | IC-7300 capability values, scope producer, UI tables, CAT surface |
| `FullStateCapabilitiesTests` | 3 | fullState includes radioModel/radioDisplayName/capabilities, tables come from backend, fallback without backend |

## Test Coverage by SDD Requirement

| SDD Section | Test Module(s) | Status |
|-------------|---------------|--------|
| AD-001 FastAPI/Uvicorn | test_server_scope_init | 2 tests |
| AD-002 Direct Serial CAT | test_cat_controller | 29 tests |
| AD-003 Dirty-Field Broadcasting | test_radio_state, test_server_ws_protocol | 33+ tests |
| AD-004 Dual-Codec Audio | test_audio | 48 tests |
| AD-005 scope_pipe Subprocess | test_scope_frame, test_scope_runtime_config, test_server_scope_init, test_scope_pipe_restart, test_scope_pipe_tx | 27 tests |
| AD-006 Dual-Mode Spectrum | test_scope_frame, test_scope_handler_fallback | 8 tests |
| AD-007 PTT Safety | test_server_ws_protocol (PTTSafetyLogicTests) | 10 tests |
| AD-008 PyAudio Detection | test_audio (AudioDeviceDetectionTests) | 2 tests |
| AD-009 7-Task Polling | test_poll_scheduler | 17 tests |
| AD-010 Memory Channels | test_server_ws_protocol (mem messages), test_memory_recall | 6 tests |
| §7.2 RadioState Entity | test_radio_state | 33 tests |
| §7.2 Config Tables | test_config | 28 tests |
| §9.2 WS Protocol | test_server_ws_protocol (WSMessageFormatTests) | 11 tests |
| §9.6 Polling (incl. stale-read guard) | test_poll_scheduler, test_server_ws_protocol | 17+ tests |
| §15 PTT Safety | test_server_ws_protocol (PTTSafetyLogicTests) | 10 tests |
| NFR-060–065 Audio Quality | test_audio | 48 tests |
| NFR-020–023 Auth/Security | test_server_ws_protocol (WSAuthTests) | 4 tests |
| NFR-051 Doc-sync / SDD-Guardian harness | test_sdd_harness | 27 tests |
| §9.8 ATR1000 Tuner Linkage | test_atr1000_tuner, test_atr1000_client, test_atr1000_server | 99 tests |
| AD-016 Pluggable Backends (FT-710 + IC-7300) | test_backend_factory, test_config_ic7300, test_civ_codec, test_civ_controller, test_civ_scope | 115 tests |

## Running Specific Tests

```bash
# All tests
python -m unittest discover -s tests -v

# Single module
python -m unittest tests.test_radio_state -v

# Single test class
python -m unittest tests.test_radio_state.RadioStateFieldMutationTests -v

# Single test method
python -m unittest tests.test_config.ModeTableTests.test_bidirectional_mode_mapping -v
```

## Design Principles

1. **No hardware required**: All tests use mocked serial, no FT-710, no USB audio, no SPI.
2. **Fast execution**: ~223 tests in ~0.8s — can run on every commit.
3. **Coverage by SDD**: Each test references the SDD requirement it validates.
4. **Isolation**: Each test is self-contained; no shared mutable state.
5. **Readable failures**: Assertion messages clearly state expected vs actual.
