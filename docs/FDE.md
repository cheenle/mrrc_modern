# FDE: Forward Deployed Engineering — MRRC FT-710

> **Echo → Delta → Product** in the FT-710 software SCU-LAN10 replacement project.
>
> 版本 1.0 · 2026-07-24 · 基于 SDD V2.1 + git 历史 + 日志分析

---

## The FDE Formula

```
FDE = Echo + Delta + Product
```

| Phase | Question | ~% Time | Output |
| ------- | ---------- | --------- | -------- |
| **Echo** | "What does the user really need?" | ~20% | Validated demo definition |
| **Delta** | "What's the fastest thing that works?" | ~55% | Working prototype |
| **Product** | "How to serve the next 10 users?" | ~25% | Reusable assets + updated SDD |

---

## Part 1: Echo — Demand Discovery

### 1.1 The Field Signal

The FT-710 project started from a concrete, real-world gap:

> Yaesu sells the SCU-LAN10 (~$300) for remote control. But the FT-710 already exposes everything
> over a single USB cable: CAT serial, FT4222 SPI spectrum data, USB audio. Why buy extra hardware?

**Echo sources:**

| Source | Insight |
| -------- | --------- |
| Personal daily operation | FT-710 user operating from multiple devices (browser, phone, tablet) |
| F4HTB upstream evaluation | Existing open-source FT-710 remote — evaluated, found gaps (no SPI scope, no bidirectional Opus audio, no mobile UI) |
| MRRC ecosystem experience | 15-cycle MRRC project already solved PTT safety, WDSP, multi-instance, Glassmorphism — these are reusable patterns |
| Hamlib gap analysis | Documented in `SDD/FT-710-Hamlib-Gap-Analysis.md` — confirmed direct CAT via pyserial is simpler and more reliable |

### 1.2 The Echo Output

**Concrete demo definition:**

> A browser-based FT-710 remote control that replaces SCU-LAN10 with zero additional hardware.
> One USB cable. Real-time waterfall via FT4222 SPI. Bidirectional 48kHz Opus audio.
> Mobile-first UI. 4 WebSocket channels (control, audio RX, audio TX, spectrum).

This demo definition drove the initial SDD V1.0 baseline (all 15 chapters, written in a single burst).

---

## Part 2: Delta — 11 Rapid Prototyping Cycles

Each SDD version is a FDE Delta cycle: Echo → ship demo → Product abstraction.

### 2.1 Cycle Map (Git-Verified)

| Cycle | SDD | Date | Echo Input | Delta Challenge | Product Output |
| ------- | ----- | ------ | ------------ | ----------------- | ---------------- |
| 1 | V1.0 | Jul 6 | F4HTB evaluation + MRRC patterns | Full baseline: CAT, audio, scope, polling, PTT safety | 15-chapter SDD, 7 core modules |
| 2 | V1.1 | Jul 6 | "16kHz TX crackling" | Unify TX pipeline to 48kHz | AD-011: 48kHz codec + 44.1kHz bridge |
| 3 | V1.2 | Jul 8 | "Freq drifts ~20Hz, TX meters blank" | VS/FB active VFO tracking, RM calibration | AD-012/013: VFO + meter tables |
| 4 | V1.3 | Jul 8 | "TUNE doesn't work, PTT latency" | Priority CAT preemption, TX2+AC003 tune sequence | AD-015: `_cancel_polls` + `send_priority_set_command` |
| 5 | V1.4 | Jul 12 | "Waterfall alone is hard to read" | FFT spectrum line plot, install.sh, FAQ | FFT overlay, dev tooling |
| 6 | V1.5 | Jul 18 | "SDD docs stale vs. runtime" | Doc sync: PTT layers, polling naming, scope clarification | V1.5 docs + open issues I6/I7 |
| 7 | V1.6 | Jul 18 | "SSB filter change — can't hear difference" | RX zero-audio watchdog, TX Opus truth sync, auth redirect | Dead-carrier fix, WS 403 flow |
| 8 | V1.7 | Jul 19 | "Filter switch works 50% of the time" | Stale-read discard after each poll query, 150ms SH0 read-back | 3 regression tests |
| 9 | V1.8 | Jul 21 | "PTT watchdog dead code, TUNE accidental tap" | PTTManager routing, TUNE press-and-hold, keyboard guards | Space-bar PTT, mobile layout |
| 10 | V1.9 | Jul 21 | "USB reconnect → waterfall frozen forever" | `on_reconnected` hook → re-run scope-init CAT after reconnect | 2 regression tests |
| 11 | V2.0 | Jul 21 | "spurious 'radio disconnected' during operation" | NoneType guard in freq-logging + recovery path | 1 regression test |
| 12 | V2.1 | Jul 22 | "PTT keys but no voice / no RF power" | TX uplink ownership promote + claim, per-session TX counters | 5 regression tests + TX session logging |

### 2.2 Delta Patterns

**Pattern 1: The 14-month operational gap proxy**
The FT-710 project inherited 15 cycles of MRRC operational learning. The foundational assets — PTT safety (ACK×3, watchdog), WDSP integration, multi-instance architecture, Glassmorphism design tokens — were already Product-phase abstractions. FT-710 didn't start from zero; it started from leverage.

**Pattern 2: Each cycle addresses exactly 1 real field signal**
No speculative features. Every Delta cycle was triggered by a concrete Echo event: a log line, a user report, an observed misbehavior. The `_tx_session_frames` UnboundLocalError that was found and fixed in the current session (2026-07-24) is the most recent example.

**Pattern 3: Documentation is concurrent, not retrospective**
SDD chapters were updated after each Delta phase, not at the end. The version history table in `14-version-history.md` IS the FDE cycle log.

**Pattern 4: Prototype validates the riskiest assumption first**

- V1.0: Can we do direct CAT without Hamlib? → Yes, AD-002.
- V1.1: Is 48kHz TX the right pipeline? → Yes, AD-011.
- V1.9: Does the watchdog need scope-init re-run? → Yes, `on_reconnected`.
- V2.1: Does multi-client TX need ownership promotion? → Yes, `_promote_tx_owner()`.

---

## Part 3: Product — Abstraction & Generalization

### 3.1 Reusable Assets Inherited

| Asset | From | Used In FT-710 |
| ------- | ------ | ---------------- |
| PTT safety (7-layer model) | MRRC | Ch15, `server.py` PTT handler |
| AudioWorklet + Opus WASM | MRRC | `rx_worklet_processor.js`, `tx_opus_worker.js` |
| Glassmorphism design system | MRRC | `css/octen.css` |
| Multi-instance architecture | MRRC | `server.py` lifespan management |
| SDD template (IBM TeamSD) | MRRC/SunMRRC | All 15 SDD chapters |
| Website deploy pattern | MRRC/SunMRRC | `website/deploy.sh` + `build_sdd.py` |

### 3.2 Reusable Assets Created

| Asset | Gravel Road (Delta hack) | Highway (Product abstraction) | Reusable For |
| ------- | -------------------------- | ------------------------------- | ------------- |
| `scope_pipe.py` | FT4222 ctypes in asyncio crashed | Standalone subprocess with length-prefixed stdout pipe | Any FTDI SPI project |
| `audio_resample.py` | 44.1kHz↔48kHz mismatch crackles | Frame-aligned linear SRC (960↔882) | Any 44.1kHz→48kHz bridge |
| `poll_scheduler.py` | 7-task adaptive polling | Skip-on-command, stale-read discard, `on_reconnected` hook | Any CAT-controlled radio |
| Tagged dual-codec audio | Raw PCM only | 1-byte tag (0x00=PCM, 0x01=Opus) + payload | Any codec-flexible audio transport |
| `scope_frame.py` | Scope data parsing | Shared frame format with sync pattern, quality metrics | Any FT-710 scope consumer |
| TX session logging | Silent failures with no diagnostics | Per-PTT-release: frames fed/decoded/failed, device written/errors, PCM peak | Any TX audio pipeline |

### 3.3 The SDD as Product Harness

The FT-710 SDD follows the same three-level harness structure as MRRC and SunMRRC:

| Harness Level | SDD Chapters | FDE Phase |
| --------------- | ------------- | ----------- |
| **Business Harness** (WHAT) | Ch2 Business Direction, Ch4 System Context, Ch5 NFRs, Ch6 Use Cases, Ch13 Feasibility | Fed by Echo |
| **Technical Harness** (HOW) | Ch7 Subject Model, Ch8 Arch Decisions, Ch9 Overview, Ch10 Service Model, Ch11 Components, Ch12 Operations | Fed by Delta |
| **Product Harness** (DELIVERABLE) | Ch1 Executive Summary, Ch3 Project Definition, Ch14 Version History, Ch15 PTT Safety | Fed by Product |

---

## Part 4: The FDE Blueprint — FT-710 Edition

### 4.1 How This Project Follows the 6-Step Blueprint

| Step | FT-710 Application |
| ------ | ------------------- |
| **1. Start from Leverage** | Inherited 6+ MRRC assets (PTT safety, WDSP, multi-instance, Glassmorphism, SDD template, website deploy) |
| **2. Define the Harness** | SDD V1.0: 15 chapters, 15 ADs, 65 NFRs, 8 use cases — written concurrently with first commit |
| **3. Echo: Deploy** | Field deployment on macOS + Raspberry Pi + Windows. Real daily operation generated the signal for 11 Delta cycles |
| **4. Delta: Ship** | Each SDD version shipped a validated fix within 24h of the Echo signal. V1.0 baseline in 1 day |
| **5. Product: Abstract** | 6 reusable modules extracted. Dual-codec tagging. Adaptive polling as pattern. Scope-init hook as reusable API |
| **6. Accelerate** | The next FT-710 client (iOS, desktop app) inherits all of this. WebSocket protocol is the stable contract |

### 4.2 FDE Metrics

| Metric | Value |
| -------- | ------- |
| SDD versions | 12 (V1.0→V2.1) |
| FDE cycles | 12 Echo→Delta→Product loops |
| Days from V1.0 to V2.1 | 16 days (Jul 6–22) |
| Test suite | 262 tests |
| Reusable assets created | 6 |
| MRRC assets inherited | 6+ |
| Operational gap | Inherited from MRRC's 14-month gap + concurrent FT-710 daily use |
| Key enabler | MRRC's PTT safety + audio architecture as stable foundation |

---

## Part 5: Key FDE Insights

### 5.1 What Worked

1. **Inherited leverage is the unfair advantage.** FT-710 didn't re-solve PTT safety, WDSP, or multi-instance — MRRC had already turned those into Product-phase abstractions.

2. **The version history IS the FDE log.** `14-version-history.md` records every Echo signal, Delta fix, and Product output — in a format any future contributor can read.

3. **One cycle = one real problem.** No speculative features. Each SDD version addresses exactly one concrete field signal (a log line, a user observation).

4. **Documentation is concurrent.** SDD chapters were updated during each Delta phase, not after. The harness records what worked, not what was planned.

5. **Prototypes validate the riskiest assumption.** V1.0 proved direct CAT works. V1.1 proved 48kHz audio works. V2.1 proved multi-client TX ownership works.

### 5.2 What Could Be Better

1. **No pre-git Echo phase was captured.** The F4HTB evaluation and initial protocol discovery happened before git tracking. MRRC's cycles 1–3 set the precedent here.

2. **Product phase could produce more standalone artifacts.** `scope_pipe.py` and `poll_scheduler.py` are excellent reusable modules, but they're embedded in a project-specific repo. Extracting them as independent packages would increase leverage for future projects.

3. **No dedicated FT-710 iOS client yet** — unlike SunMRRC→SunsdrMobile, the FT-710's WebSocket protocol hasn't been validated by a second consumer. The protocol is the contract; building a native client would validate it.

---

## Appendix: FDE Glossary

| Term | Definition |
| ------ | ----------- |
| **Echo** | Demand discovery — deploy to the field, capture real user signals |
| **Delta** | Rapid prototyping — ship a demo that validates the riskiest assumption |
| **Product** | Abstraction — turn the field solution into a reusable asset |
| **Gravel Road** | The rough Delta hack that works for one scenario |
| **Highway** | The Product-phase abstraction that works for the next 10 scenarios |
| **Harness** | SDD structure that locks down WHAT (Business), HOW (Technical), and DELIVERABLE (Product) |
| **Leverage** | Reusable assets inherited from prior projects — MRRC's PTT safety being the prime example |
| **Operational Gap** | Period of daily usage that accumulates real Echo signals — MRRC's 14-month gap drove explosive innovation |

---

*This document records the FT-710 project through the FDE lens defined at [vlsc.net/fde.html](https://www.vlsc.net/fde.html).*
*Cross-reference: SDD 14-version-history.md, AGENTS.md, vlsc.net FDE methodology.*
