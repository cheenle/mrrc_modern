# Windows TX 44.1 kHz Device-Domain Design

**Date:** 2026-07-28  
**Target release:** v1.7.8 test build  
**Status:** Approved design; implementation pending

## 1. Problem

The FT-710 USB audio device domain is fixed at 44.1 kHz. The browser and
Opus codec domain correctly operate at 48 kHz, but the current Windows path
treats a WASAPI endpoint's `defaultSampleRate` as the radio hardware rate.
When that value is 48 kHz, `AudioHandler` opens a 48 kHz output stream and
bypasses the explicit 48→44.1 kHz bridge.

That inference is unsafe: a WASAPI shared-mode mix rate is not proof of the
USB endpoint's hardware clock. It also contradicts the base AD-011 decision
and the FT-710's verified 44.1 kHz requirement. The V2.9 exception was based
on pacing measurements from a KVM USB isochronous-OUT environment that was
later found unsuitable for TX audio-quality validation.

## 2. Decision

Adopt one invariant on every platform:

```text
Browser microphone / Opus codec domain: 48 kHz mono, 960 samples / 20 ms
                         ↓ explicit server SRC
FT-710 USB device domain:              44.1 kHz mono, 882 samples / 20 ms
```

- `start_tx()` always opens the selected FT-710 output device at 44,100 Hz.
- The output stream uses 882 frames per buffer.
- `feed_tx_audio()` always converts decoded 48 kHz Int16 PCM to 44.1 kHz via
  `audio_resample.py`; there is no Windows 48 kHz pass-through.
- Windows may still enumerate MME, DirectSound, and WASAPI entries, but host
  API enumeration must not change the device-domain rate.
- Remove the `_wasapi_tx_variant()` rate-selection policy rather than replace
  it with another runtime heuristic.
- Preserve the Windows TX→RX capture reopen workaround and the v1.7.7
  PortAudio reinitialization/re-enumeration recovery.

This restores the original AD-011 boundary and satisfies the SDD Guardian
audio constraint: 48 kHz belongs to the codec domain; 44.1 kHz belongs to
the FT-710 PyAudio device domain.

## 3. Scope

### Production code

- `audio_handler.py`
  - remove WASAPI-native-rate selection;
  - keep `_tx_rate` fixed at `TX_SAMPLE_RATE == 44100`;
  - keep prebuffer, maximum-buffer, and graceful-drain byte budgets derived
    from 44.1 kHz;
  - always perform 48→44.1 kHz conversion before queueing TX PCM;
  - keep device re-resolution after PortAudio reinitialization without
    changing the selected rate.

No WebSocket, Opus, browser capture, PTT ordering, CAT, or RX codec behavior
changes are included.

### Packaging and release

- Set the Windows installer version to v1.7.8.
- Build the installer on the existing Win11 VM using the normal gated build.
- Install the resulting package on that VM for the operator's evening test.
- Copy the artifact back as a clearly identified v1.7.8 test installer.
- Do not publish it to the public website or replace the public v1.7.6
  download until the operator approves the hardware test.

## 4. Error Handling and Diagnostics

- A 44.1 kHz stream-open failure follows the existing three-attempt retry,
  one PortAudio reinitialization, device-name re-resolution, and second
  three-attempt retry sequence.
- Reinitialization never inherits or retains a previous 48 kHz rate.
- Existing TX session diagnostics remain authoritative for frame receipt,
  decode failures, writes, write errors, peak level, and owner drops.
- Startup logging must report `TX audio started: [...] @ 44100 Hz` for the
  Windows test path.

## 5. Test Strategy

Implementation follows red-green TDD.

1. Replace the current Windows-WASAPI expectation with a failing regression
   test: even when an identically named WASAPI entry advertises 48 kHz,
   `start_tx()` opens the selected device at 44.1 kHz with an 882-frame
   buffer.
2. Add/adjust a failing regression test proving every 960-sample 48 kHz TX
   frame becomes exactly 882 samples / 1764 bytes before queueing.
3. Cover the PortAudio-reinitialization path and prove the retry remains at
   44.1 kHz even if device enumeration changes.
4. Run the focused audio tests, then the full hardware-independent suite.
5. Run `py_compile` and the SDD constraint checker.
6. On Win11, run the complete build gate and inspect the packaged DLL/static
   assets before installing.

Hardware acceptance is intentionally separate:

- VM: installation, launch, Opus decoder readiness, `@ 44100 Hz` log,
  frames/fed/written counters, and absence of write errors.
- Operator evening test: actual RF speech intelligibility and noise quality.
- The KVM's USB isochronous OUT path is not treated as an authoritative TX
  audio-quality oracle.

## 6. Documentation Traceability

- **AD-011:** restore the 48 kHz codec-domain / 44.1 kHz device-domain bridge
  and withdraw the V2.9 Windows exception.
- **SDD §9.4:** retain the documented 960→882 TX chain on Windows.
- **NFR-061:** Opus remains 64 kbps at 48 kHz; unchanged.
- **NFR-065 / AD-008:** audio device selection remains name/index based;
  only the rate-selection policy changes.
- **SC4:** success remains audible RF output from browser microphone input.
- Update `CHANGELOG.md`, `SDD/14-version-history.md`, SDD quick facts,
  `AGENTS.md`, Windows installer documentation, and `tests/README.md` in the
  implementation change.

## 7. Acceptance Criteria

- Browser/Opus TX stays 48 kHz mono with 960-sample frames.
- Every server-decoded TX frame is converted to 882 Int16 samples.
- Windows PyAudio TX always opens at 44,100 Hz with 882 frames per buffer.
- No code path promotes a Windows WASAPI mix rate into the FT-710 device rate.
- PortAudio reinitialization preserves the 44.1 kHz invariant.
- Focused and full automated tests pass; SDD checks are clean.
- v1.7.8 test installer builds and installs on the Win11 VM.
- Public downloads remain unchanged until hardware acceptance.
