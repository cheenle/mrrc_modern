# IC-7300 / IC-7300MK2 CI-V Conformance Hardening

**Date:** 2026-08-26

**Status:** Approved for implementation

**Scope:** Hardware-independent protocol correctness and browser behavior for the existing IC-7300 family backend

## 1. Objective

Align the existing IC-7300 and IC-7300MK2 implementation with Icom's published CI-V formats wherever correctness can be established without a radio. Preserve the FT-710 backend and the existing IC native-48-kHz audio path. Clearly separate byte-level software verification from checks that still require physical hardware.

Authoritative references:

- Icom *IC-7300 Full Manual*, section 19, "Remote control (CI-V) information."
- Icom *IC-7300MK2 CI-V Reference Guide*, A7841-8EX (October 2025), repository file `IC-7300MK2_ENG_CI-V_0.pdf`.

Third-party implementations may be used only as secondary evidence and must not override an explicit Icom definition.

## 2. Confirmed Protocol Corrections

### 2.1 Scope activation

Icom documents `27 00` waveform output as requiring both scope display ON and waveform-data output ON. Scope initialization and `_diag_ic7300_scope.py` will therefore send, in order:

1. `27 10 01` — scope display ON;
2. `27 14 00` — Center mode;
3. `27 15 <span>` — selected half-span;
4. `27 11 01` — waveform-data output ON.

The diagnostic script will continue to disable `27 11` in `finally`. It will not force the operator's scope display OFF on exit because the display may have been enabled before the diagnostic started.

### 2.2 SCROLL-C metadata

For `scope_mode == 2`, the two five-byte frequency fields are lower and upper display edges, exactly as in Fixed/SCROLL-F mode. They are not center frequency and half-span. `parse_scope_segment()` will decode mode 2 into `low_edge_hz` and `high_edge_hz`; downstream metadata will use those boundaries.

The production backend remains initialized in Center mode. This correction prevents incorrect metadata if the front panel or another CI-V client changes the radio to SCROLL-C.

### 2.3 Model-specific Transceive item

The IC-7300 uses set-mode item `1A 05 00 71`; the IC-7300MK2 uses `1A 05 00 89`. Model identity, not the current CI-V address, will select the item. This preserves correct behavior when either radio's address is customized.

### 2.4 Power state and power-on format

Bare command `18` is not documented as a readable power-state query. The periodic IC power-health getter will instead issue documented frequency query `03`; a valid response means the radio is on, while no response remains unknown/disconnected.

For maintenance-path `18 01`, the controller will prepend the Icom-documented number of `FE` bytes for the configured baud rate (150 at 115200, 75 at 57600, 50 at 38400, 25 at 19200, 13 at 9600, and 7 at 4800). `18 00` remains a normal CI-V frame. Existing server boot verification and safety guards remain unchanged.

### 2.5 ALC presentation

The IC-7300 family reports ALC full-scale at raw value 120 in the official meter table. `RadioState` will gain an injectable `raw_to_alc_pct` conversion. FT-710 keeps its existing raw/255 behavior; the IC backend maps raw 0..120 to 0..100% and clamps over-range readings at 100%.

### 2.6 Scope speed choices

Icom CI-V defines three scope speeds: FAST (`00`), MID (`01`), and SLOW (`02`). Backend capabilities will publish those choices. The browser will rebuild the speed selector for CI-V radios so it cannot send the FT-710-only values `03` or `04`.

## 3. Architecture and Data Flow

No new process or protocol layer is introduced.

- `civ_codec.py` remains the pure framing/BCD/scope parser.
- `civ_controller.py` remains the serialized CI-V transport and demultiplexer.
- `backend.py` supplies model identity, scope initialization, state conversions, and browser capabilities.
- `radio_state.py` remains the common state container with backend-injected calibration functions.
- `ft710_ui.js` consumes capability metadata to constrain controls.
- `_diag_ic7300_scope.py` continues to reuse production framing and parsing.

All model-specific differences stay at backend/controller construction boundaries. The FT-710 command, scope, calibration, and audio paths are unchanged.

## 4. Error Handling

- Scope-init writes retain existing boolean failure reporting and warning logs.
- The diagnostic script reports invalid baud/address arguments before opening serial and always attempts to disable waveform output before closing an opened port.
- A failed frequency health query returns `None`; it does not fabricate `power_on=False` from one timeout.
- Unsupported baud rates use the normal two-byte CI-V preamble for `18 01` and emit a warning rather than guessing a repetition count.
- Browser speed choices are constrained before a command is sent; controller range validation remains defense in depth.

## 5. Verification Strategy

Add hardware-independent regressions using recorded/constructed official wire vectors:

1. Scope initialization command order includes both `27 10 01` and `27 11 01`.
2. The diagnostic script emits checksum-free official frames and enables scope display before data output.
3. Center mode decodes center/span, while SCROLL-C and Fixed modes decode lower/upper edges.
4. IC-7300 and MK2 retain their correct Transceive items under non-default CI-V addresses.
5. Power health uses command `03`, never a bare `18` query.
6. Power-on preamble byte counts match every Icom table entry; power-off remains standard framed CI-V.
7. IC ALC raw 60/120 maps to 50/100%, while the FT-710 default remains raw/255.
8. CI-V browser capabilities expose only the three valid scope speeds and the rendered selector contains no invalid values.
9. Existing codec, controller, scope, server, frontend source-contract, and full Python suites remain green.
10. Run `py_compile`, primary LSP diagnostics, `git diff --check`, and SDD Guardian checks.

## 6. Documentation Synchronization

Update the CI-V knowledge base, operation guide, SDD architecture/operations/version history, `README.md`, `AGENTS.md`, and `tests/README.md`. Operator documentation must state that CI-V scope output requires:

- `CI-V USB Port = Unlink from [REMOTE]`;
- `CI-V USB Baud Rate = 115200` selected explicitly, not Auto;
- the correct CI-V address (`0x94` for IC-7300, `0xB6` for MK2 unless changed consistently on radio and server).

## 7. Explicit Hardware Acceptance Boundary

Passing the software suite proves frame construction, parsing, queue/state behavior, UI constraints, and documented command ordering. It cannot prove:

- USB serial-driver enumeration or radio-side menu configuration;
- radio echo and ACK timing on a physical unit;
- actual `27 00` waveform cadence or RF-state interactions;
- RF output, tuner behavior, or power-cycle behavior;
- USB audio endpoint selection, RX audibility, TX modulation, or RF audio quality;
- undocumented behavior such as AGC value `00`.

Those remain items for `IC-7300_硬件验收清单.md` when hardware is available. No completion claim will describe them as verified.

## 8. Non-goals

- No new CI-V features such as automatic notch, VOX, memories, or LAN scope transport.
- No change to Opus, PyAudio, or sample-rate conversion.
- No change to spectrum broadcast scheduling introduced by the preceding reliability patch.
- No broad controller acknowledgment-policy rewrite; fire-and-forget setters remain unchanged except for the documented power-on byte format.
- No FT-710 behavior change.
