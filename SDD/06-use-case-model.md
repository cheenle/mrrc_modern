# 6. Use Case Model (ART 0508)

## 6.1 Actors

The actors are the operator, constrained listener, SDR visitor, embedded terminal, maintainer, browser/native runtime, selected radio backend and optional SDR/NanoVNA/ATR devices defined in Chapter 4. Authorization is a server property; hiding a button is never the enforcement mechanism.

## 6.2 Core Use Cases

### UC-001: Start a Control Session

| Field | Description |
| --- | --- |
| Goal | Open the UI and establish authenticated control/media |
| Preconditions | Server reachable, valid operator/listen credentials and a configured backend endpoint |
| Basic Flow | Open HTTPS entry → login → persistent cookie → `/WSradio` → `fullState` with real capabilities/bands/modes → independent RX/TX/spectrum connections |
| Postconditions | UI renders only supported controls and displays the actual backend connection state |
| Exceptions | Auth expiry, missing endpoint, degraded controller and stale assets are reported; UI does not fabricate a connected radio |

### UC-002: Receive Audio

| Field | Description |
| --- | --- |
| Goal | Hear the selected receiver in a browser/native/terminal client |
| Basic Flow | USB/network source → backend conversion → 48 kHz PCM → recording/FT8/local taps → listener-only box gain → Opus48/PCM48 → client jitter buffer |
| PCM16 branch (AD-038) | A socket that declared `audioCaps` and was accepted receives `0x02` frames (16 kHz Int16 mono, 640 B payload / 20 ms) instead: one shared soxr LQ 48→16 conversion per tick for the whole PCM16 group, with no Opus encode when no legacy listener exists. The ESP32-C3 single-mode firmware plays the PCM directly without an Opus decoder or 48 kHz buffer |
| Postconditions | Continuous playback without unbounded stale backlog |
| Exceptions | Missing device/network data is explicit; Opus failure falls back to PCM48 on legacy clients; network capture recovery does not require a browser audio subscriber when FT8/recording/local speaker still consumes it |

### UC-003: Tune Frequency and Mode

| Field | Description |
| --- | --- |
| Goal | Change the intended VFO/slice/receiver frequency and mode |
| Basic Flow | UI sends `set` → backend translates through its profile/object model → radio accepts → poll/status read-back updates dirty state |
| Postconditions | Display, band and subsequent tuning follow the authoritative radio state |
| Notes | Yaesu dual receive follows `MRRC_RX_SOURCE`; Flex commands target only the owned slice with `slice set`; frequency bounds derive from backend bands |

### UC-004: Monitor Spectrum and S-Meter

| Field | Description |
| --- | --- |
| Goal | See trustworthy signal context |
| Basic Flow | FT4222/CI-V/SCU/SunSDR/Flex real source or S-meter fallback → `ScopeHandler` → AD-025 socket tier → waterfall and meter rendering |
| Postconditions | New real data advances the display; fallback does not repeatedly upload an unchanged curve |
| Exceptions | Invalid/truncated/foreign FFT data is dropped and counted; no-slice Flex pans and guessed frequency axes are not presented as live RF |

### UC-005: PTT and Tune Control

| Field | Description |
| --- | --- |
| Goal | Key and release safely |
| Preconditions | Operator role, connected backend and passed AD-019/backend-specific TX gate |
| Basic Flow | Press → owner/keyer arbitration → key → start media; release → immediate unkey + queue/stream cleanup + watchdog telemetry |
| Exceptions | SCU-LAN10 TX refuses because audio is unknown; unverified models require explicit opt-in; every release remains allowed |
| Safety | Chapters 15 and AD-007/020/033/039 cover disconnect, foreign release, heartbeat, CQ, restart and process-death limitations |

### UC-006: Adjust Radio Settings

| Field | Description |
| --- | --- |
| Goal | Change only settings the backend actually supports |
| Basic Flow | Capability-driven control → shared set executor → backend-specific command → read-back/status authority → broadcast |
| Postconditions | Controls and values agree with the selected receiver/object |
| Examples | Filter indices map to real Hz; Flex RF gain uses radio-reported SCU ranges; box audio gains never modify FT8/recording sources |

### UC-007: Manage Memory Channels

| Field | Description |
| --- | --- |
| Goal | Save, delete and recall frequency/mode channels |
| Basic Flow | Authenticated `memSave`/REST write persists JSON; recall uses the same atomic frequency/mode path as local controls |
| Postconditions | Memories survive restart and all clients receive updates |
| Exceptions | Listen role may recall but not mutate; request schema validation and backup remain open as I7 |

### UC-008: View Multi-Meter Telemetry

| Field | Description |
| --- | --- |
| Goal | Monitor S/power/ALC/SWR/current/voltage and related telemetry supported by the radio |
| Basic Flow | Backend poll or network meter stream → calibration/model mapping → dirty fields → meter UI |
| Postconditions | Only real values appear; unsupported/unverified meters are hidden or named |
| Notes | Flex S-meter resolves `SLC/LEVEL` by owned slice, not by the first slice on the radio |

### UC-009: Record a QSO or Send One CQ Call

| Field | Description |
| --- | --- |
| Goal | Preserve an RX/TX timeline or play one finite CQ asset |
| Recording | Authenticated start creates a per-session writer; blocks are queued to one incremental MP3 writer; stop finalizes and indexes the file; insufficient free space refuses the start |
| CQ | Validate asset and TX gate → key → feed finite frames under queue low-water control → drain/unkey; second press, disconnect or external release aborts |
| Safety | CQ/FT8 waveforms bypass microphone gain; every automated transmission has a definite end |

### UC-010: Operate FT8

| Field | Description |
| --- | --- |
| Goal | Decode and sequence FT8 while retaining common audio/CAT safety |
| Basic Flow | Switch work mode → feed 12 kHz UTC-aligned RX ring → per-slot fast decode → conditional deep cascade/merge → optional sequencer TX through the existing sink/controller |
| Postconditions | Logs/statistics expose slots, paths, latency and failures; SSB state restores on exit |
| Exceptions | Missing library/data/time data fails visibly; no deadline-risk escalation or silent reduction of decode results |

### UC-011: Use a Public Receive Role

| Field | Description |
| --- | --- |
| Goal | Share a radio or independent SDR without operator privileges |
| Listen | Separate password → listen token → `/listen`; server permits only freq/mode/memory recall and GETs, and rejects TX/tuner writes |
| SDR | Separate password/cookie → `/sdr` → `/WSsdr`; `sdr_pipe` owns the device and `sdr_hub` arbitrates tuning, pooled demodulators and listener/channel caps |

### UC-012: Configure and Operate a Box Locally

| Field | Description |
| --- | --- |
| Goal | Install and run without SSH-centric daily operation |
| Basic Flow | `/setup`/`/settings` → validated network/model/credential changes → read-back/restart; optional Bluetooth HID and local speaker operate beside the radio |
| Exceptions | One Bluetooth operation at a time, explicit paired/keys_work state, non-destructive absent-AP handling and fixed root-helper verbs |
| Safety | Unverified-TX uplift requires the exact confirmation token; physical power key does not power off the box |

### UC-013: Measure an Antenna with NanoVNA

| Field | Description |
| --- | --- |
| Goal | Scan and interpret S11 without risking the radio or instrument |
| Basic Flow | `/Nano` → guarded REST → off-loop `nano/vna.py` → exact-length binary frame → pure analysis → verdict/plots/export; restore device sweep afterwards |
| Exceptions | Any TX, concurrent operation, CAT-port collision or listen/SDR role is refused; partial frames are discarded, not plotted |

### UC-014: Connect a Single-Socket Terminal

| Field | Description |
| --- | --- |
| Goal | Provide full control/RX/TX/spectrum with one TLS/WS session |
| Basic Flow | Authenticate `/WSterminal` → receive `fullState` → reuse control JSON → send `spectrumCaps` only if scope is wanted → exchange channel-prefixed legacy media bodies |
| Backpressure | Media send failure increments terminal drop counters and preserves the control/PTT socket |
| Disconnect | Unregister every logical channel and force RX exactly once if the socket was keyer/uplink owner |
| Roles | Operator has full control; listen can receive/tune but has no TX registration; SDR credentials are rejected |

### UC-015: Diagnose, Update and Remotely Access an Instance

| Field | Description |
| --- | --- |
| Goal | Resolve field problems without exposing operator data or guessing state |
| Basic Flow | Inspect `/api/health`, `/api/perf`, session/cloud/help state → build redacted bundle → optional upload/answer workflow → apply the platform's verified update path |
| Postconditions | Real errors and versions are visible; configuration writes are read back; restart releases PTT |
| Boundaries | Cloud Hub registry/leases and the public support board are external authorities, not hidden runtime dependencies |
