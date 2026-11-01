# MRRC Modern / `mrrc_modern` SDD — Software Design Description

> Web Remote Control for supported radios (Yaesu FT-710, Icom IC-7300 / IC-7300MK2)  
> IBM Team Solution Design (TeamSD) v2.3.2 aligned documentation set

## Purpose

This SDD is the canonical design record for the `mrrc_modern` codebase — a Python FastAPI server that bridges a browser to one or more supported HF/50MHz transceivers via pluggable radio backends. It captures requirements, architecture, design decisions, component boundaries, operational model, capability inventory, known gaps, and evolution history.

Runtime facts are derived from `server.py`, `backends/*`, `audio_handler.py`, `radio_state.py`, `poll_scheduler.py`, `scope_handler.py`, `config.py`, `opus_rx.py`, `static/index.html`, `static/ft710_main.js`, `static/ft710_ui.js`, and `static/modules/*`.

## Document Index

| # | Chapter | ART Code | File |
| --- | --------- | ---------- | ------ |
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

| Record | Repo | Authority |
| --- | --- | --- |
| [MRRC Cloud Hub SDD V0.1](../../mrrc_hub/SDD/README.md) | `mrrc_hub` | **公网远程接入**：实例出站隧道、Access/Tunnel Gateway、Registry、Ticket/Lease、设备证书、Portal、OTA 灰度、审计 |
| [Fleet Hub 设计评审（2026-09-30）](../../mrrc_hub/docs/2026-09-30-fleet-hub-design-review.md) | `mrrc_hub` | 对本仓远程接入现有能力的代码取证评审（含本仓侧需改动的 C1–C6） |
| `docs/legacy/sdd/original-sdd/` | `mrrc` | 旧架构（前身项目）设计记录 |

**边界**：**电台控制与媒体**（CAT/CI-V、Opus 音频、频谱/瀑布、PTT 多层安全释放、5 个 WS 端点）
的权威是**本 SDD**；**接入与租约**（隧道、子域路由、凭证、Operator 租约、Portal）的权威是
`mrrc_hub/SDD/`。两者交叉引用，不互相复制。注意 `mrrc_hub` 的 ID 带 `-H` 中缀
（`AD-H06` / `NFR-H006` / `R-H1`），以免与本章集的裸 ID 撞号。

## Quick Facts

| Attribute | Value |
| ----------- | ------- |
| Document ID | SDD-MRRC-MODERN-2026-001 |
| SDD Version | V3.10 |
| Baseline Date | 2026-11-01 |
| Status | **V3.10（2026-11-01）**：损坏的 wheel 会静默吃掉整条 FT8（RECORD sha256 校验进构建门禁 + `/api/ft8` 回 503）、4800 波特的串口饱和（轮询按链路速率拉伸，38400 以上不变）、Yaesu 的 NB/NR 电平回读与 `PC` 功率等级位、应用重启后远程协助端口变黑洞、启动时的假采集停滞（盒子线 **v1.28.28 · FP24**）。测试 3310 项（159 模块，1 skip on macOS）。上一版 **V3.09（2026-11-01）**：2 m/70 cm 机型不再被当成 HF 机 —— 频率可信窗口由机型 band 表派生、页头补百 M 位、Yaesu 静噪 `SQ0` 回读、`scope_span/speed/mode` 不再抛 `AttributeError`、FT-991A 的 CAT 速率默认 4800。测试 3273 项（158 模块，1 skip on macOS）。 上一版 **V3.08（2026-11-01）**：支持看板网页决策——操作员在 <https://www.vlsc.net/mrrc_modern/support/board/>（Support 口令）排期/拒绝/退回 FDE 卡片；决策入接收端 durable 队列，Mac 每分钟 `--sync-decisions` 应用（唯一状态权威），发布公开页+私有镜像成功后才确认，`.unpublished` 标记驱动补发。测试 3246 项（157 模块，3 skip on macOS）。上一版 **v1.28.23（2026-10-31）**：FT-991A 机型接入（第 6 个 Yaesu profile）——桌面 Windows + macOS 安装包与盒子热修 FP22 同发。macOS DMG **86,385,066 bytes** `2d5ae649…`；Windows Setup **80,113,936 bytes** `263e80a0…`（VM `Get-FileHash` == Mac `shasum`）；盒子 FP22 源码包 18,711,097 B `87b57470…` · 补丁 38,589 B `8e82a737…`；两端各跑洁净室真跑。 |
| Project | MRRC Modern / `mrrc_modern` |
| Primary Radios | Yaesu FT-710 and Icom IC-7300 / IC-7300MK2 (verified); Icom IC-705 / IC-7610 / IC-7760 and Yaesu FTDX10 / FTDX101D / FTDX101MP / FTX-1F / FT-891 / FT-991A (profiles without hardware verification, TX gated, no scope stream for the Yaesu family) — all selectable via backend |
| Backend Selection | `MRRC_RADIO_MODEL=ft710\|ic7300\|ic7300mk2\|ic705\|ic7610\|ic7760\|ftdx10\|ftdx101d\|ftdx101mp\|ftx1\|ft891\|ft991a` (default `ft710`); `MRRC_ALLOW_UNVERIFIED_TX=1` unlocks transmit on the nine unverified models |
| Runtime | Python 3.12+, FastAPI, Uvicorn, NumPy, PyAudio |
| Frontend | HTML5, CSS3, vanilla JavaScript, Web Audio API (no client-side MP3 encoder; CQ playback is server-side, AD-020) |
| Transport | HTTP/WS for browser; Serial CAT (Yaesu) or USB CI-V (Icom) for radio; FT4222 SPI for FT-710 scope; recording as server-side 16 kHz MP3 (AD-017); CI-V 0x27 spectrum for the Icom family (475 bins on IC-7300/IC-7300MK2/IC-705, 689 bins on IC-7610/IC-7760) |
| Default Entry | `https://localhost:8888` (TLS only; self-signed until a certificate is enrolled) |

## System at a Glance

```text
Browser (iPhone / Desktop / Tablet)
  | HTTP + WebSocket: /WSradio /WSaudioRX /WSaudioTX /WSspectrum (+optional /WSatr1000)
  v
FastAPI/Uvicorn MRRC Server (server.py)
  | Pluggable backend selected by MRRC_RADIO_MODEL
  |   ft710: Serial CAT (USB Enhanced COM Port, 38400 baud) → Yaesu FT-710
  |          FT4222 SPI (scope_pipe subprocess) → real spectrum data
  |   ic7300/ic7300mk2: USB CI-V serial (115200 8N1, addr 0x94/0xB6) → Icom IC-7300/MK2
  |                     CI-V 0x27 spectrum on the same port (display+data enabled; 44-segment newest-data queue)
  | PyAudio (USB Audio device) → RX/TX audio capture/playback
  | Opus codec (libopus) → compressed audio transport
  v
Yaesu FT-710 or Icom IC-7300 / IC-7300MK2 Radio
```

## Capability Summary

| Area | Status | Notes |
| ------ | -------- | ------- |
| Mobile UI | Implemented | `static/index.html`, `ft710.css`, `ft710_main.js`, `ft710_ui.js` |
| Radio control | Implemented | Backend-specific command set: frequency, mode, filter, PTT, gains, etc. |
| Spectrum waterfall | Implemented | FT-710: FT4222 SPI real + S-meter fallback; IC-7300: CI-V 0x27 real + S-meter fallback |
| RX audio | Implemented | PyAudio capture → Opus/PCM → /WSaudioRX → browser playback (per-backend sample rate) |
| TX audio | Implemented | Browser mic → /WSaudioTX → Opus decode → PyAudio → radio (per-backend sample rate) |
| S-meter + Multi-meter | Implemented | Backend-specific meter polling (e.g., FT-710 RM3-RM8) |
| Radio telemetry (RI) | Implemented | Hi-SWR, recorder, RX/TX, tuner, scan, squelch-open status from `RI0;` |
| Meter display + AMC | Implemented | `MS`/`AO` commands exposed in state and control path |
| Memory channels | Implemented | `/api/mem_channels` GET/POST with JSON persistence |
| Session authentication | Implemented | Shared-password login; `_auth_tokens` + `mrrc_auth` cookie, **persisted** in `data/auth-tokens.json` (0600, 30-day) so a restart keeps sessions; all WS gated. Token transport is header → cookie → deprecated `?token=` (AD-024); no frontend builds a token URL |
| PTT safety | Implemented | Touch-and-hold, PTT watchdog, dead-man switch, unload beacon, **TX-phase liveness gate** (opt-in via `MRRC_REMOTE_SESSION_TX_HEARTBEAT_S`; catches connections that die without a TCP close — §15.6) |
| Remote-session telemetry | Implemented | `session_metrics.py` + `GET /api/session_metrics`: listener/operator concurrency (sessions + sockets), lifetime/window peaks, spectrum+audio_rx uplink bytes — counts only, no identifiers (AD-023) |
| Scope visualization | Implemented | 850-point FFT waterfall, frequency scale, S-meter bar |
