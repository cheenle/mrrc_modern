# 3. Project Definition (ENG 343)

## 3.1 Project Attributes

| Attribute | Value |
| --- | --- |
| Project Name | MRRC Modern / `mrrc_modern` |
| Project Type | Remote radio control, media, receive sharing and field-appliance service |
| Primary Users | Radio operators, listeners, embedded-terminal clients and system maintainers |
| Registry Keys | `ft710`, `ic7300`, `ic7300mk2`, `ic705`, `ic7610`, `ic7760`, `ftdx10`, `ftdx101d`, `ftdx101mp`, `ftx1`, `ft891`, `ft991a`, `g7250`, `sunsdr2dx`, `flex` |
| Whole-model Verified | FT-710, IC-7300, IC-7300MK2, FT-991A; all other registry entries remain TX-gated |
| Server Platform | Windows, macOS and Linux; Python 3.12+ for source/desktop deployments, image-managed Python on boxes |
| Client Platform | Modern browsers; iOS/Android native clients; constrained WS clients through AD-039 |
| Runtime Framework | FastAPI + Uvicorn, asyncio, NumPy/SciPy and backend-specific native libraries |
| Frontend Stack | HTML/CSS/vanilla JavaScript, Canvas, Web Audio/AudioWorklet and Web Workers |
| Radio Interfaces | Yaesu ASCII CAT, Icom/Xiegu CI-V, Icom network tunnel, SCU-LAN10 UDP, SunSDR native UDP, Flex SmartSDR TCP/VITA-49 UDP |
| Audio Interfaces | PyAudio USB/external sound cards or backend-owned network audio; libopus for the main client path |
| Spectrum Interfaces | FT4222 SPI, CI-V 0x27, SCU-LAN10 scope, SunSDR IQ FFT, Flex panadapter FFT, S-meter synthesis |

## 3.2 In Scope

- Mobile SPA and independent `/listen`, `/sdr`, `/terminal`, `/Perf` and `/Nano` pages.
- Authenticated `/WSradio`, `/WSaudioRX`, `/WSaudioTX`, `/WSspectrum` and `/WSterminal`; optional `/WSatr1000`, `/WSsdr` and FT8 event stream.
- Dirty-field radio state, seven-tier polling, priority commands, memories, meters and capability-driven controls.
- Tagged Opus48/PCM48 media, 48 kHz internal audio bus, recording/local-speaker/FT8 taps and bounded TX queues.
- Real and synthetic spectrum with the common AD-025 850-bin wire contract and per-socket bandwidth tiers.
- Server-side MP3 recordings, finite CQ calls, FT8 fast/deep cascade and native DSP packaging.
- Shared-password operator/listen sessions, separate SDR credentials, path-prefix-safe Cloud Hub deployment and session metrics.
- Desktop and image deployment, box onboarding/network/Bluetooth/local controls, remote assistance, diagnostics and update channels.
- NanoVNA antenna measurement and guarded optional firmware upgrade.

## 3.3 Out of Scope

- Hamlib/rigctld as runtime middleware; the implementation owns its protocol codecs and transports.
- Multi-tenant per-user account administration; operator access remains a shared station password, with separate constrained roles.
- Cloud Hub registry, leases, portal and gateway internals; their authority is `mrrc_hub/SDD/`.
- ATR-1000 firmware and tuning algorithm internals; only the network linkage and learned relay store belong here.
- SDRplay vendor driver internals and closed-source SDR applications; only the device shim, channelizer and sharing service belong here.
- Full SmartSDR, ExpertSDR or WFView feature parity, SmartLink, multi-pan/multi-slice operator UI and arbitrary third-party radio middleware.
- TS-940S modernization and ACC adapter hardware until their separate designs are implemented and registered.
- Native mobile application internals; their own guides own UI/toolchain details, while this SDD owns the server contracts they consume.

## 3.4 Success Criteria

| ID | Criterion | Verification |
| --- | --- | --- |
| SC1 | Selected backend connects | Log and `/api/health` identify the real serial/network endpoint and connected controller |
| SC2 | Advertised controls work | Capability-backed commands reach the intended receiver/object and state read-back agrees |
| SC3 | RX audio reaches clients | Correct-rate tagged frames arrive without growing backlog; recordings/FT8 retain the unmodified capture |
| SC4 | Supported TX reaches the radio | Explicitly enabled backend carries modulation, with no silent-carrier fallback and zero unsafe release regression |
| SC5 | Waterfall renders honestly | Real frames advance only on new data; fallback is identifiable and identical synthetic frames are suppressed |
| SC6 | Real scope is validated per source | SPI/CI-V/SCU/SunSDR/Flex framing, geometry and frequency axis pass source-specific tests and available field checks |
| SC7 | Roles remain constrained | Listen/SDR cannot invoke operator mutations, TX, host internals or instruments |
| SC8 | PTT cannot be left keyed by a covered failure | Normal release, foreign-release arbitration, disconnect, heartbeat and shutdown tests pass; hardware TOT remains the last resort |
| SC9 | Operational data survives restart | Memories, tokens, settings and recordings persist; update/config writes are read back |
| SC10 | Field failures are diagnosable | Support bundle, tunnel error, event-loop lag and device/audio evidence identify the failing layer without secrets |

## 3.5 Major Milestones

| Milestone | Date | Deliverable |
| --- | --- | --- |
| M1–M8 | 2026-06…08 | FT-710 control, SPI spectrum, meters, PTT, memories, Opus audio, worklets and SDD baseline |
| M9 | 2026-08 | Pluggable Icom CI-V backend and native mobile client work |
| M10 | 2026-09 | Persistent sessions, remote-access hardening, recording/CQ and support chain |
| M11 | 2026-10 | Box images, Cloud Hub integration, local controls, SDR receiver, NanoVNA, FT8 and performance instrumentation |
| M12 | 2026-11-01…12 | FT-991A field verification, SunSDR2 DX and G7250 native/network backends |
| M13 | 2026-11-14…16 | Local-remote hardening, SCU-LAN10 RX path and FlexRadio SmartSDR RX/antenna field evidence |
| M14 | 2026-11-17 | AD-039 single-socket terminal in current source; AD-038 PCM16 accepted for implementation |
