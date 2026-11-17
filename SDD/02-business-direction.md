# 2. Business Direction (BUS 411)

## 2.1 Vision

Make a radio station usable from a phone, desktop, native client or small embedded terminal without installing a desktop radio suite: open an authenticated URL, see trustworthy signal context, hear the receiver, operate the radio and always have a safe way back to RX. Extend the same station into a maintainable field appliance with public listening, independent SDR reception, recording, FT8 and antenna measurements.

## 2.2 Mission

Deliver pragmatic, browser-native remote operation with direct protocol integration, mobile ergonomics, bounded latency and field diagnosability. Preserve a small set of stable client contracts while allowing serial, LAN-tunnel and native network radios to evolve behind them. Never trade RF safety or verification honesty for a larger supported-model list.

## 2.3 Business Goals

| ID | Goal | Description |
| --- | --- | --- |
| G1 | Mobile RX confidence | Reliable audio, spectrum and signal telemetry across supported transports |
| G2 | Safe remote control | Predictable control with explicit model gates and layered PTT release |
| G3 | Bidirectional media | Browser/terminal microphone reaches supported TX paths; RX reaches every authorized listener |
| G4 | Deployable appliance | Desktop, W103D and Raspberry Pi installations are configurable and diagnosable without source-tree access |
| G5 | Design continuity | SDD distinguishes current behavior, field evidence, accepted designs and open gaps |
| G6 | Signal awareness | Real spectrum where available, truthful fallback elsewhere, meters and antenna analysis |
| G7 | Accessible reception | Listen-only and independent SDR roles share media without acquiring operator privileges |
| G8 | Sustainable support | Redacted diagnostics, observable performance and provable updates shorten field resolution |

## 2.4 Objectives

| ID | Objective | Current Status |
| --- | --- | --- |
| O1 | Backend-neutral radio control | Implemented for 15 registry keys; verification remains per model |
| O2 | Continuous RX audio | Implemented over USB and native/tunnelled network paths |
| O3 | TX audio uplink | Implemented only where the backend proves a TX path; SCU-LAN10 refuses it |
| O4 | Real-time spectrum | Implemented through FT4222, CI-V 0x27, SCU-LAN10, SunSDR and Flex adapters plus fallback |
| O5 | PTT release safety | Implemented; see Chapter 15 for ownership and hub liveness |
| O6 | Persistent operating data | Memories, audio levels, settings, FT8 logs and recordings persist with bounded writes |
| O7 | Field appliance operation | Box network, local remote, support, update and performance pages implemented |
| O8 | Embedded-client efficiency | Single-socket terminal implemented; PCM16 profile approved but not yet implemented |

## 2.5 Strategy

| ID | Strategy | Description |
| --- | --- | --- |
| S1 | Mobile-first, capability-driven UI | Controls appear only when the active backend reports them |
| S2 | Direct protocol ownership | No Hamlib/rigctld dependency; protocol families own their byte layer |
| S3 | One internal audio domain | 48 kHz PCM/Opus internally; conversion belongs at device or negotiated client boundaries |
| S4 | Stable client contracts | Legacy WS frames remain unchanged; cookie/Bearer auth and per-socket capabilities gate extensions |
| S5 | Shared engine per exclusive device | One owner for FT4222, FT8 DSP, SDRplay, SunSDR or Flex sockets; consumers share adapters |
| S6 | Document measured state | Offline conformance, field RX evidence and whole-model verification are different claims |

## 2.6 Tactics

| ID | Tactic | Implementation |
| --- | --- | --- |
| T1 | Release-first PTT | Normal release, browser watchdogs, per-socket ownership, dead-man switches, heartbeat and bounded automated TX |
| T2 | Worklet media | Time-based RX jitter buffer and 20 ms TX frames; server RX pacing follows the wall clock and sheds stale backlog |
| T3 | Bounded device I/O | Blocking serial/audio/system calls leave the event loop; queues and native parsers reject partial or foreign data |
| T4 | Backend adaptation | `RadioBackend`, profiles and `RadioCapabilities` isolate model differences from `server.py` |
| T5 | Operational evidence | Startup/read-back logs, `/api/health`, `/api/perf`, session metrics and redacted support bundles expose real state |
| T6 | Independent public roles | Listen and SDR passwords/cookies constrain authorization at the server, not merely in their pages |
