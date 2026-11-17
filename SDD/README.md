# MRRC Modern / `mrrc_modern` SDD — Software Design Description

> Remote radio control, media, receive sharing and field-appliance design record
> IBM Team Solution Design (TeamSD) v2.3.2 aligned documentation set

## Purpose

This SDD is the canonical design record for the `mrrc_modern` codebase: a Python FastAPI service connecting browsers, native clients and embedded terminals to serial and network radios through pluggable backends. It records requirements, architecture, decisions, service/component boundaries, operational behavior, verification status, known gaps and evolution history.

Runtime facts are derived from the current `server.py`, `backends/*`, audio/state/polling/scope modules, `ft8/`, SDR/Nano/box operations modules and `static/`. Dated design documents under `docs/superpowers/` are not treated as implemented merely because they exist; Chapter 8 and the issue list distinguish **accepted**, **implemented**, **field-exercised** and **whole-model verified**.

## Document Index

| # | Chapter | ART Code | File |
| --- | --- | --- | --- |
| 1 | Executive Summary | - | [01-executive-summary.md](01-executive-summary.md) |
| 2 | Business Direction | BUS 411 | [02-business-direction.md](02-business-direction.md) |
| 3 | Project Definition | ENG 343 | [03-project-definition.md](03-project-definition.md) |
| 4 | System Context | APP 011 | [04-system-context.md](04-system-context.md) |
| 5 | Non-Functional Requirements | ART 0507 | [05-non-functional-requirements.md](05-non-functional-requirements.md) |
| 6 | Use Case Model | ART 0508 | [06-use-case-model.md](06-use-case-model.md) |
| 7 | Subject Area Model | APP 408 | [07-subject-area-model.md](07-subject-area-model.md) |
| 8 | Architecture Decisions | ART 0513 | [08-architecture-decisions.md](08-architecture-decisions.md) |
| 9 | Architecture Overview | ART 0512 | [09-architecture-overview.md](09-architecture-overview.md) |
| 10 | Service Model | ART 0582 | [10-service-model.md](10-service-model.md) |
| 11 | Component Model | ART 0515 | [11-component-model.md](11-component-model.md) |
| 12 | Operational Model | ART 0522 | [12-operational-model.md](12-operational-model.md) |
| 13 | Feasibility Assessment | ART 0530 | [13-feasibility-assessment.md](13-feasibility-assessment.md) |
| 14 | Version History | - | [14-version-history.md](14-version-history.md) |
| 15 | PTT Safety Architecture | ART 0535 | [15-ptt-safety-architecture.md](15-ptt-safety-architecture.md) |

## Related Design Records

| Record | Authority |
| --- | --- |
| [MRRC Cloud Hub SDD](../../mrrc_hub/SDD/README.md) | Public remote access: tunnel/gateway/registry/tickets/leases/certificates/portal/OTA/audit |
| [Fleet Hub design review](../../mrrc_hub/docs/2026-09-30-fleet-hub-design-review.md) | Code-evidence review of the instance-side remote-access boundary |
| `docs/superpowers/specs/` | Dated feature designs; implementation status must be checked against code and this SDD |
| `docs/legacy/sdd/original-sdd/` | Legacy predecessor architecture |

**Boundary:** radio control/media, public SDR/Nano/box operations and instance-side safety are owned here. Cloud access/registry/lease internals are owned by `mrrc_hub/SDD/`. Hub IDs use the `-H` infix to avoid collisions.

## Quick Facts

| Attribute | Value |
| --- | --- |
| Document ID | SDD-MRRC-MODERN-2026-001 |
| SDD Version | V3.26 |
| Baseline Date | 2026-11-17 |
| Status | Comprehensive current-source resync: 15 registry keys, native/tunnelled network backends, AD-038 PCM16 implemented offline and AD-039 `/WSterminal` released in box 1.28.86 · FP73. PCM16/terminal/terminal-audio merge result: unittest **4396** cases / **200** modules |
| Verified Models | Yaesu FT-710 / FT-991A; Icom IC-7300 / IC-7300MK2 |
| TX-gated Models | Icom IC-705 / IC-7610 / IC-7760; Yaesu FTDX10 / FTDX101D / FTDX101MP / FTX-1F / FT-891; Xiegu G7250; SunSDR2 DX; FlexRadio |
| Backend Selection | `MRRC_RADIO_MODEL=ft710\|ic7300\|ic7300mk2\|ic705\|ic7610\|ic7760\|ftdx10\|ftdx101d\|ftdx101mp\|ftx1\|ft891\|ft991a\|g7250\|sunsdr2dx\|flex` |
| Runtime | Python 3.12+ source/desktop, FastAPI/Uvicorn, asyncio, NumPy/SciPy; PyAudio only for sound-card backends |
| Client Surfaces | Mobile SPA, `/listen`, `/sdr`, `/terminal`, iOS/Android and embedded WS clients |
| Core Transport | `/WSradio`, `/WSaudioRX`, `/WSaudioTX`, `/WSspectrum`, `/WSterminal`; optional `/WSatr1000`, `/WSsdr`, `/WSft8` |
| Main Audio | 48 kHz internal mono bus, Opus48/PCM48 legacy client tags plus negotiated AD-038 `0x02` PCM16; recording storage 16 kHz MP3 |
| Default Entry | `https://localhost:8888` with first-run self-signed or enrolled certificate |
| Current Released Box Source | v1.28.86 · FP73 (AD-039 single-socket terminal) |

## System at a Glance

```text
Browser / native client / embedded terminal
  | HTTPS + authenticated WS (legacy channels or AD-039 single socket)
  v
FastAPI/Uvicorn MRRC server
  | Dirty RadioState + seven-tier polling + media/scope fan-out
  | Recording / CQ / FT8 / listen / SDR / Nano / box operations
  v
Active backend selected by MRRC_RADIO_MODEL
  ├─ FT-710: ASCII CAT + FT4222 scope_pipe + USB audio
  ├─ Icom/Xiegu: shared CI-V core + 0x27 scope + USB audio
  │    └─ G7250 optional RS-BA1 three-socket LAN transport
  ├─ Yaesu: profile-driven ASCII CAT + USB/external audio
  │    └─ optional SCU-LAN10 CAT + scope + dual-RX audio, TX refused
  ├─ SunSDR2 DX: native UDP engine + DSP audio/FFT
  └─ FlexRadio: SmartSDR TCP + VITA UDP + owned pan/slice audio/FFT
```

## Capability Summary

| Area | Status | Notes |
| --- | --- | --- |
| Radio control | Implemented | Backend-specific commands, capability-driven UI and read-back/status authority |
| Spectrum waterfall | Implemented | SPI/CI-V/SCU/SunSDR/Flex real sources and S-meter fallback; AD-025 socket tiers |
| RX/TX audio | Backend-dependent | 48 kHz main bus, bounded queues and network adapters; SCU-LAN10 TX refused |
| Metering/memories | Implemented | Real backend values only; persistent JSON memories |
| Recording/CQ/FT8 | Implemented | Incremental MP3, finite CQ, native FT8 worker and fast/deep cascade |
| Authentication/roles | Implemented | Persistent operator/listen tokens, independent SDR role, cookie/Bearer transport |
| Terminal multiplex | Implemented | AD-039 channel prefix + legacy body, shared PTT/TX ownership and one-shot disconnect release |
| PCM16 audio profile | Implemented offline | AD-038 `audioCaps`/`0x02` implemented for RX/TX; ESP32 and radio acceptance pending |
| Box operations | Implemented on supported boxes | Setup/settings, Wi-Fi/Bluetooth/local control/speaker, performance and remote help |
| Public SDR/NanoVNA | Implemented with device guards | Independent device processes/ports and authorization boundaries |
| Support/update/Cloud Hub | Implemented with platform scope | Redacted bundles, desktop manifest checks, box apply/state, explicit tunnel errors |
| PTT safety | Implemented | Layered release and ownership; process death still requires radio hardware TOT |
