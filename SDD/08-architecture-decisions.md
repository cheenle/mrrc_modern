# 8. Architecture Decisions (ART 0513)

## AD-001: Use FastAPI/Uvicorn for MRRC Modern Server

| Attribute | Value |
|-----------|-------|
| Type | Architectural |
| Status | Implemented |
| Decision | Use FastAPI with native WebSocket routes and Uvicorn runtime |

**Problem**: The server needs static file serving, 4 WebSocket endpoints, async serial CAT I/O, scope subprocess management, audio streaming, and auth — all in one process.

**Rationale**: FastAPI/Uvicorn provides direct async integration, lifespan management, middleware, and a small code surface. No Tornado, Flask, or Django needed.

**Consequences**: All server logic lives in `server.py` with modular imports from sibling modules. Lifecycle managed via `@asynccontextmanager lifespan`.

## AD-002: Direct Serial CAT — No Hamlib/Rigctld

| Attribute | Value |
|-----------|-------|
| Type | Architectural |
| Status | Implemented |
| Decision | Use `pyserial` (sync API) with `asyncio.to_thread()` for serial I/O |

**Problem**: Hamlib adds a large dependency and another process to manage. FT-710 CAT protocol is well-documented (Yaesu standard) and straightforward.

**Rationale**: A dedicated `CatController` class with an `asyncio.Lock` for serialized access and thread-pool offloading is simpler, more debuggable, and has fewer failure modes than Hamlib/rigctld.

**Consequences**: Radio protocol code lives inside pluggable `backends/<model>/` packages. Adding another radio model requires a new backend package plus factory registration, not changes to `server.py`.

## AD-003: Dirty-Field State Broadcasting

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | `RadioState` dataclass tracks changed fields via `_dirty_fields` set; broadcast only changed fields |

**Problem**: Full state broadcasts on every poll would be wasteful (~44 fields at 10Hz).

**Rationale**: `RadioState.update()` records which fields changed; `to_dirty_dict()` sends only those fields. Clients merge partial updates into their local state.

**Consequences**: `stateUpdate` messages are compact; clients must maintain local state mirror.

## AD-004: Tagged Dual-Codec Audio Transport (Opus + Int16 PCM)

| Attribute | Value |
|-----------|-------|
| Type | Architectural |
| Status | Implemented |
| Decision | Both `/WSaudioRX` and `/WSaudioTX` carry a 1-byte codec tag per frame: `0x00` = Int16 PCM, `0x01` = Opus. RX: 48kHz @ 64kbps (fullband, transparent for broadcast music). TX: 48kHz @ 64kbps CBR (voice with fidelity priority: complexity=5, SIGNAL=VOICE, VBR/FEC/DTX disabled). Default Opus; falls back to PCM. |

**Problem**: (RX) Int16 PCM at 48kHz mono costs ~768kbps — heavy on mobile/WiFi. Opus at 64kbps cuts that 12×. (TX) Browser mic Opus encoding saves uplink bandwidth. A per-frame tag removes negotiation races — receiver inspects tag and decodes accordingly.

**Rationale**: `opus_rx.py` (copied from sunmrrc) provides direct ctypes libopus bindings. Uses `max_data_bytes` cap on `opus_encode()` to control bitrate — avoids arm64 variadic `opus_encoder_ctl` issues. Browser uses WASM `OpusDecoder`/`OpusEncoder`. TX encoder configured for voice with fidelity priority (`static/modules/opus_codec.js`): complexity=5, 64kbps CBR (stable packet size), FEC=OFF (WebSocket TCP is reliable), DTX=OFF (no priming gaps), fullband, SIGNAL=VOICE, LSB depth 16. (Raised from the original 28kbps/complexity=3 for transmit-fidelity headroom.)

**Consequences**: Adds libopus dependency (optional — degrades gracefully to PCM). Codec is user-switchable. `AUDIO_TAG_PCM` / `AUDIO_TAG_OPUS` are constants in both Python and JavaScript.

## AD-005: scope_pipe as Standalone Subprocess

| Attribute | Value |
|-----------|-------|
| Type | Architectural |
| Status | Implemented |
| Decision | FT4222 SPI I/O runs in a separate Python process (`scope_pipe.py`), communicating with the server via stdout/stderr pipes |

**Problem**: FT4222 ctypes calls are blocking and can hang. Running them in the asyncio event loop would stall the entire server. Threading is fragile with FTDI D2XX driver state.

**Rationale**: A subprocess isolates the FTDI driver. If scope_pipe crashes, the server continues (falls back to S-meter). Frame format: 4-byte BE uint32 length + payload. stderr carries machine-parseable `STATUS:` lines for diagnostics. Heartbeat frames (len=0) keep pipe alive when idle.

**Consequences**: scope_pipe is independently restartable. Server handles pipe exit gracefully (marks scope disconnected, switches to fallback). Two process lifecycle to manage.

**Amended (V2.7)**: the pipe protocol gains a stdin control channel — the server pushes `TX:1`/`TX:0` on every `tx_status` transition (`_notify_scope_pipe_tx`). While TX is active the pipe pauses SPI reads and freezes all sync/stall recovery counters (the FT-710 garbles its scope stream during TX; reading it previously churned the pipe into `fatal:too_many_reinits` after every PTT), and runs one clean re-sync on TX→RX. stdin EOF (parent died) also stops the pipe. Windows teardown uses `taskkill /PID <pid> /T /F` because `terminate()` only reaches the onefile bootloader and orphans the real worker (which then holds the FT4222: FT_DEVICE_NOT_FOUND for the next pipe). This subprocess is used only by the FT-710 backend; the IC-7300 backend receives 0x27 spectrum frames directly on the CI-V serial port.

## AD-006: Dual-Mode Spectrum (FT4222 + S-Meter Fallback)

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | When FT4222 is available, broadcast real 850-point FFT data. When unavailable, generate synthetic multi-peak Gaussian spectrum from CAT S-meter readings |

**Problem**: FT4222 requires specific libraries, D2XX driver config, and exclusive device access. It's not always available.

**Rationale**: The S-meter fallback provides useful visual context (shows band activity) even without hardware scope. The binary frame format is identical in both modes — clients don't care about the source.

**Consequences**: `ScopeHandler` has two code paths: `update_from_scope_frame()` (real data) and `update_from_radio_state()` (synthetic). `scope._connected` flag determines which is active.

## AD-007: PTT Release as Safety-Critical Flow

| Attribute | Value |
|-----------|-------|
| Type | Safety |
| Status | Implemented |
| Decision | Multiple independent release paths: normal WebSocket command (fire-and-forget TX0), PTT watchdog, dead-man switch on WS disconnect, beforeunload beacon, pagehide handler |

**Problem**: A lost or unprocessed PTT release command can leave the radio transmitting indefinitely — a serious safety and regulatory issue.

**Rationale**: Release is more safety-critical than keying. Each layer catches a different failure mode: lost WS message, half-open socket, browser crash, tab close, app switch. See Chapter 15 for detailed PTT Safety Architecture.

**Consequences**: Frontend PTT logic is more complex; polling skip-on-PTT ensures state consistency. (V1.2 removed the 3×200ms post-release verify loop — it added ~600ms to every release; stuck-keyup detection now relies on the 500ms TX-status poll plus the browser watchdog.)

## AD-008: PyAudio Auto-Detection of Supported Radio USB Audio

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | Multi-layer device selection parameterized by backend: (1) explicit `MRRC_AUDIO_RX_DEVICE`/`MRRC_AUDIO_TX_DEVICE` env var (index or name substring), (2) per-backend name hints (e.g., "FT-710"/"FT710"/"YAESU" for FT-710), (3) generic "USB Audio CODEC" / "USB Audio Device" fallback (common built-in sound card names on Windows; first match wins, multi-match warns), (4) mono-channel heuristic (radio USB audio is typically mono), (5) full-duplex heuristic for TX (device with both input + output), (6) system default fallback |

**Problem**: USB audio device naming varies by radio, OS, and driver version. Hardcoding a device index is fragile. Previous version only searched by name substring and fell back to first input device — could select webcam mic instead of the radio. On Windows the FT-710's card carries no "FT-710"/"YAESU" string at all ("USB Audio CODEC" or "USB Audio Device", possibly localized/prefixed), so without tier (3) the heuristics grabbed a laptop mic (RX) or PC speakers (TX) — V2.6 field report.

**Rationale**: Name-based matching is more robust than index-based. The mono-channel heuristic is reliable: supported radio USB audio typically provides exactly 1 input channel (mono RX), while webcams and USB mics typically offer 2 (stereo). Full-duplex preference for TX ensures the same device is used for both RX and TX paths. Logs all available devices at startup for debugging. The generic USB-audio names rank below the radio-specific hints but above the channel heuristics; duplicates from per-host-API enumeration (MME/DirectSound/WASAPI) open the same hardware, and a genuine multi-device setup is warned about with a pointer to the env-var lock (`USB Audio` is the common substring covering both enumeration forms).

**Consequences**: Audio may still use wrong device if multiple mono USB audio devices are present. Configurable device override via env vars is the recommended approach for such setups.

**Amended (V2.8)**: Windows full-duplex wedge — on Windows (MME/DirectSound), opening the TX playback stream on the FT-710's C-Media codec silently wedges RX capture (stays open, error-free, delivers silence; field symptom: RX audio perfect after server restart, gone after one PTT). `AudioHandler.restart_rx()` originally reopened capture on TX→RX only on Windows.

**Amended (V2.31)**: Field QSO recordings on macOS showed intermittent 20 ms RX frames with low-bandwidth/attenuated content after repeated PTT activity while a fresh parallel capture stream stayed clean. The workaround is now platform-independent: after every TX→RX transition, the server asynchronously reopens the long-lived RX capture stream. The reopen is off the event loop and does not change the 44.1 kHz device-domain / 48 kHz codec-domain contract.

## AD-009: 7-Task Adaptive Polling with Bounded Lock Time

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | Background CAT polling split into 7 cooperative tasks (IF, VFO, TX status, TX meters, settings, slow telemetry, connection watchdog), with skip-on-command and short per-query timeout |

**Problem**: Polling too fast floods the serial port; too slow makes the UI feel unresponsive. Some fields (S-meter) change rapidly; others (filter width) rarely.

**Rationale**: Fast path keeps only `FA/MD0/SM0` at 100ms. `VS/FB` run separately at 500ms, so active-VFO tracking does not bloat the IF loop. TX status and TX meters are independent 500ms tasks; TX meter polling includes `RM3/RM4/RM5/RM6` and is TX-only. Settings (2s) include `RG0`, `MS`, and tuner state; slow telemetry (5s) includes `RM7/RM8`, `PR`, `AO`, and `RI0`. Poll query timeout is 0.25s to cap lock occupancy.

**Consequences**: `PollScheduler` owns task-level cadence and backpressure controls (`skip_next_poll()`, short pause after user command, and `_cancel_polls` awareness). CAT errors remain per-command and non-fatal. Since V1.7, poll loops re-check skip state AFTER each in-flight query response and discard stale reads (a mid-flight `SH0;` response previously overwrote a just-set filter width); the filter set path additionally verifies with a 150 ms `SH0;` read-back.

## AD-010: Memory Channels as Server-Side JSON

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | Memory channels stored server-side in `mem_channels.json`; API: GET/POST `/api/mem_channels`; auto-broadcast to all clients on change |

**Problem**: Client-side-only storage loses channels across devices/browsers. Server-side persistence ensures all clients see the same channels.

**Rationale**: Simple JSON file is adequate for 6-99 channel slots. No database needed. Auto-broadcast keeps all clients in sync.

**Consequences**: Channels survive server restarts. File is human-editable. No per-user channel isolation (single shared-password model).

## AD-011: 48kHz Codec Domain with Per-Backend Device-Rate Bridge

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | TX audio chain runs at 48 kHz in the codec domain (browser capture → Opus encode → server decode). The server bridges to the radio's native USB audio rate via `audio_resample.py` when needed: FT-710 uses a 44.1 kHz device rate (960↔882 frame-aligned resample, ratio 160:147); IC-7300/MK2 uses 48 kHz native USB audio (no resample). RX uses the inverse bridge only when the capture rate differs from 48 kHz. |

**Problem**: V1.0 captured mic audio at 16 kHz (320 samples/20ms frame) but PyAudio played back at 48 kHz (expecting 960 samples/20ms). The 3:1 rate mismatch caused the output stream to underrun — every 20ms Opus frame produced 320 samples that filled only 1/3 of the 960-sample playback buffer. The remaining 2/3 was stale/residual buffer data, producing audible crackling ("咔咔咔") on transmitted audio. The first fix unified everything at 48 kHz — but later measurement showed the FT-710 USB audio interface natively runs at **44.1 kHz**, so 48 kHz PCM still could not be written straight to the device stream. The IC-7300/MK2 USB audio interface natively runs at 48 kHz, so no bridge is needed for that backend.

**Rationale**: Opus mandates 48 kHz; supported radios have different native USB audio rates. A stateless numpy linear-interp resampler bridges the two domains per 20 ms frame with exact integer alignment (960→882 for FT-710), costing ~µs per call with zero phase drift. Browser capture at 48 kHz works on all modern platforms (iOS 15+, Chrome, Firefox).

**Consequences**: Each direction has at most one SRC step at the server boundary, owned by `audio_resample.py`. Browser and codec stay at 48 kHz. FT-710 PyAudio streams run at 44.1 kHz with the bridge; IC-7300/MK2 PyAudio streams run at 48 kHz with no bridge. The v1.0 underrun class of bug is impossible in both domains.

**Amended (V2.9)**: the device-domain rate is host-API-dependent, not universally 44.1 kHz. On macOS CoreAudio the codec runs natively at 44.1 kHz (bridge required, unchanged). On Windows the same C-Media codec's native audio-engine mix rate is 48 kHz, and its MME 44.1 kHz playback path paces ~1.4× slow (measured on the Win11 KVM rig: 50×20 ms writes block 1.36–1.42 s) — the TX drain falls behind, the 400 ms cap drops 24–34 % of voice frames, TX audio crackles. Windows TX therefore opens the same-name WASAPI entry at its native rate (48 kHz) and `feed_tx_audio` passes 48 kHz PCM through unchanged; the 44.1 kHz bridge applies only when the stream rate is actually 44.1 kHz. RX capture stays at 44.1 kHz MME on Windows (paces correctly).

**Amended (V2.14, supersedes V2.9)**: Windows follows the same fixed boundary as every other platform: browser/Opus remains 48 kHz and the FT-710 PyAudio device stream remains 44.1 kHz. The prior WASAPI `defaultSampleRate` was a Windows shared-mode mix rate, not evidence of the radio's USB hardware clock; the KVM pacing result is retained as incident evidence but withdrawn as a device-rate policy. `feed_tx_audio()` therefore always performs 960→882 SRC. PortAudio reinitialization and the Windows TX→RX capture reopen workaround remain unchanged.

## AD-012: Active-VFO-Aware Frequency Model

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | Poll `VS` + `FB` at 0.5s and treat `freq` set command as "apply to currently active VFO" |

**Problem**: Using `freq` as VFO-A-only could update the wrong oscillator when VFO-B is active.

**Rationale**: Active-VFO tracking keeps UI and CAT semantics aligned with front-panel behavior.

**Consequences**: State now carries `active_vfo`, `vfo_a_freq`, and `vfo_b_freq` continuously.

## AD-013: FT-710 Meter Calibration Tables

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | Convert raw RM meter values (0–255) into engineering units via piecewise-linear calibration tables in `config.py` |

**Problem**: Raw meter values are not user-meaningful and are non-linear.

**Rationale**: Calibration points from FT-710 rig data provide practical watt/SWR/volt/amp displays without firmware changes.

**Consequences**: UI meters show engineering units; calibration can be tuned independently of polling logic.

## AD-014: FT-710 CAT Errata Handling

| Attribute | Value |
|-----------|-------|
| Type | Design |
| Status | Implemented |
| Decision | Apply FT-710-specific command corrections: treat `DN` as step-down (never poll), use `PR00/PR01` for compressor, and map tuner control to `AC000/AC001/AC003` |

**Problem**: Yaesu CAT documentation ambiguities can cause unintended RF behavior (frequency drift, invalid tuner command forms).

**Rationale**: Runtime behavior is grounded in observed FT-710 responses and cross-command consistency.

**Consequences**: Safer default control path; ambiguous commands are either corrected or intentionally omitted.

## AD-015: Priority CAT Command Preemption

| Attribute | Value |
|-----------|-------|
| Type | Safety / Responsiveness |
| Status | Implemented |
| Decision | Introduce `send_priority_set_command()` and `_cancel_polls` cooperative abort so latency-sensitive commands (PTT/TUNE) preempt poll queries |

**Problem**: User TX/RX transitions can stall behind in-flight poll cycles if every query holds the serial lock to timeout.

**Rationale**: Priority commands set a cancel flag observed by poll loops, queued query waiters, and `_read_until()` threads, reducing worst-case handoff delay.

**Consequences**: Poll loops must be cancel-aware; UX is significantly more responsive during fast PTT/tune transitions.

## AD-016: Pluggable Radio Backend Architecture (FT-710 + IC-7300 via RadioBackend/Capabilities)

| Attribute | Value |
|-----------|-------|
| Type | Structural |
| Status | Implemented |
| Decision | Introduce a `RadioBackend` ABC + `RadioCapabilities` dataclass + `ScopeProducer` protocol in `backends/base.py`, a lazy `create_backend(model)` factory (`backends/__init__.py`, keys `ft710`/`ic7300`/`ic7300mk2`, selected by `MRRC_RADIO_MODEL`), and per-model packages `backends/ft710/` (Yaesu ASCII CAT + FT4222 scope_pipe) and `backends/ic7300/` (CI-V codec/controller at 115200 8N1 addr 0x94, 0x27 scope demux on the same port); radio-specific tables move to per-backend config modules, `fullState` carries radioModel/radioDisplayName/capabilities, and server mode/band/tune branches become backend-aware |

**Problem**: The server was hard-wired to the FT-710 — Yaesu ASCII CAT, FT4222 spectrum, 44.1 kHz USB audio — so supporting the Icom IC-7300 (CI-V protocol, in-band 0x27 spectrum, 48 kHz native audio) would have meant forking server logic.

**Rationale**: A backend ABC mirroring the CAT surface plus defaulted hooks (bands/ui_modes/filter_tables/poll-item lists/init_scope/create_scope_producer) keeps `server.py`, polling, and state radio-neutral; capabilities pushed in `fullState` let the frontend adapt bands, modes, FIL1-3 filters, ATT/PRE steps, meter visibility, and scope banner without protocol changes.

**Amended 2026-09-12 (V2.41)**: the Icom side of this layer became profile-driven. `backends/ic7300/civ_profiles.py` holds one `CivModelProfile` per model (address, Transceive set-mode item, scope geometry, bands, modes, attenuator steps, meter curves, verification flags, provenance) and the shared codec/controller/scope producer take those values as optional parameters whose defaults reproduce the previous IC-7300 constants. Adding IC-705/IC-7610/IC-7760 therefore required **no new protocol code and no `server.py` model branches** — three subclasses overriding one attribute, one registry entry, and a capability-driven attenuation bound. Models without hardware evidence set `verified=False` and refuse keying until `MRRC_ALLOW_UNVERIFIED_TX=1` (the first time this layer carries an explicit *unverified* state rather than implying acceptance). The package directory name `backends/ic7300/` now hosts the shared Icom core; renaming it to `backends/icom/` is tracked as a separate change.

**Consequences**: New radios are added as a `backends/<model>/` package plus factory registration; FT-710 behavior is unchanged (compat shims keep root imports valid); IC-7300 hardware-verification items (S-meter top point, TUNE carrier behavior, MK2 transceive item) remain flagged in code comments.

## AD-017: 服务端 QSO 录音（增量 MP3、16 kHz 存储域）

| Attribute | Value |
|-----------|-------|
| Type | Structural |
| Status | Implemented (V2.42) |
| Decision | 录音在服务端进行：`recorder.py` 的 `RecordingSession` 把 RX（设备域 PCM）与 TX（解码后的麦克风 PCM）放在一条 `time.monotonic_ns()` 定位的单声道时间轴上（源切换重锚定、50 ms 连续性容差吸收调度抖动、真实停顿补静音），用 **lameenc 增量编码边录边落盘**到 16 kHz 单声道 MP3；`server.py` 用**单 writer 任务 + 有界队列**驱动它（**专用单线程池**编码，绝不占用与串口/音频共享的默认 executor；事件循环零阻塞），控制面复用 `/WSradio`（`set{field:"recording"}` + `recordingState` 广播 + `fullState` 快照），文件面为三条 REST 路由（列表 / Range 流 / 删除），前端提供「录音」面板 |

**Problem**: 浏览器端录音器把到达的帧无时间戳地顺序拼接，网络抖动、jitter-buffer 补帧与队列丢弃被永久写进文件 —— 表现为回放"颤抖/哆嗦"（字段报告 2026-09-12）。此外音频只在页面可见时可靠，且 500 KB 的 MP3 编码器要下发到每个客户端。

**Rationale**: 服务端取音点是设备域 PCM（`AudioHandler.read_rx_chunk()`）与 Opus 解码输出，二者都不经过网络；时间戳 + 容差 + 空洞填静音把"抖动"与"真实停顿"分开处理，这正是回放平滑的关键。增量编码让 RAM 恒定（编码器 + FIR 状态 + 单块）且进程崩溃后磁盘上的前缀仍可播放，相对内存缓冲方案 1 小时录音省约 115 MB（本项目发布树莓派镜像）。16 kHz 是与兄弟项目 `mrrc` 一致的存储域，使 `recordings/` 目录可直接被其既有工具消费（同一命名 `<freq>kHz_<date>_<time>.mp3`）。

**Consequences**: 16 kHz 是**只写汇点**，从 48 kHz codec 域到达：44.1 kHz 设备音频先经 `audio_resample`（AD-011 唯一 SRC 桥），再经专用抗混叠 FIR 抽取 3:1；录音路径不回灌 codec/device 域，AD-011 不受影响。录音与 CAT 解耦（USB 音频可用、CAT 断线也能录，无 CAT 时频率记为 0 → 文件名 `00000kHz`）。保留策略**刻意不自动清理**（§13 R10），会话上限 `MRRC_RECORDINGS_MAX_SESSION_MIN` 只停止录音、不删除文件。不做所有权仲裁（I6 仍未解决）：任何已认证客户端可启停，状态对所有客户端广播。

**Writer 生命周期（V2.45 修正）**：writer 任务是**每会话一次性**的 —— 收到 stop 哨兵、MP3 定稿后即 `return`，这正是关闭流程能 `await` 它完成刷盘的前提。因此它的创建属于**每个会话**，而不是 lifespan：`_ensure_rec_writer()`（幂等）在录音开始、stop 入队与音频块入队前调用，发现任务已结束就重建。只在启动时创建一次会让任务在首次 REC/STOP 后永久消失 —— 此后每个会话都没有消费者，有界队列瞬间填满、所有块被丢弃，`stop()` 再用静音补齐时间轴，产出一个**大小正确但 100% 静音的 MP3**（2026-09-12 现场报告）。队列满时 stop 路径**不得**在事件循环上直接调用 `session.stop()`（writer 线程可能正在 `add_audio`）：改为丢弃最旧的一块给哨兵腾位，保持单写者不变式。丢弃计数按会话统计并随 `recordingState` 上报 —— 块被丢弃是"录音有空洞"的唯一可观测信号。

## 8.16 Decision Summary

| ID | Topic | Status |
|----|-------|--------|
| AD-001 | FastAPI/Uvicorn backend | Implemented |
| AD-002 | Direct serial CAT (no Hamlib) | Implemented |
| AD-003 | Dirty-field state broadcasting | Implemented |
| AD-004 | Tagged dual-codec audio (Opus + PCM) | Implemented |
| AD-005 | scope_pipe standalone subprocess | Implemented |
| AD-006 | Dual-mode spectrum (FT4222 + fallback) | Implemented |
| AD-007 | PTT release safety flow | Implemented |
| AD-008 | PyAudio supported-radio auto-detection | Implemented |
| AD-009 | 7-task adaptive polling with bounded lock time | Implemented |
| AD-010 | Memory channels as server-side JSON | Implemented |
| AD-011 | 48 kHz codec / per-backend device-rate bridge | Implemented |
| AD-012 | Active-VFO-aware frequency model | Implemented |
| AD-013 | FT-710 meter calibration tables | Implemented |
| AD-014 | FT-710 CAT errata handling | Implemented |
| AD-015 | Priority CAT command preemption | Implemented |
| AD-016 | Pluggable radio backend architecture (FT-710 + IC-7300) | Implemented |
| AD-017 | Server-side QSO recording (incremental MP3, 16 kHz storage domain) | Implemented |
| AD-018 | Profile-driven Yaesu ASCII-CAT core (FTDX10/FTDX101D/MP/FTX-1F); the verified FT-710 path stays separate | Accepted (V2.46), migration deferred to phase 3 |
| AD-019 | Unverified models ship receive-only: transmit gate, read-only identity check, per-table provenance | Implemented (V2.46) |

## AD-018: Yaesu 多机型走 profile 驱动的共享 ASCII-CAT 核心（FT-710 验证路径保持独立）

| Attribute | Value |
|-----------|-------|
| Type | Structural |
| Status | Accepted (V2.46)；FT-710 迁移列为三期 |
| Decision | 新增 `backends/yaesu/`：`yaesu_profiles.py` 是每机型差异的唯一来源（模式寄存器 **与** 独立的 CAT 字符查找表、滤波槽位、频段、衰减/前置步进、功率格式、S 表曲线、验证状态、逐表溯源），`cat_core.py` 承载从 FT-710 `cat_controller.py` **移植**过来的传输层（`;` 帧、AI 帧前缀过滤、写-only set、PTT/TUNE 优先级抢占、ENXIO 与瞬态错误分类、重连节奏），`backend.py` 由 profile 派生能力位/UI 表/状态表/poll 项。`backends/ft710/` **完全不动**。 |

**Problem**: 加 Yaesu 机型时最省事的做法是让每台新机继承 `FT710Backend`，但这会把每日在用的 FT-710 类层次拖进三台**未验证**机型的需求里，并把并非按共享核心设计的 `cat_controller.py` 事实上变成共享核心（含 FT-710 专有假设）；而「同期把 FT-710 也迁到共享核心」虽能让核心被真机验证，却让一个发布里改动用户每天通话的代码路径，爆炸半径最大。

**Rationale**: Hamlib 4.7.2 自身就是该架构的先例（`newcat.c` 13,048 行共享核心 + `ftdx10.c`/`ftdx101.c`/`ft710.c` 各 ~300 行机型表），仓库内也有已验证的同类先例（`backends/ic7300/civ_profiles.py`）。新核心因此坐在「被真机验证过的传输逻辑 + 数据化机型表」之上，同时 FT-710 的现场行为零风险。核心的无真机验证问题由 Hamlib 模拟器与 pty 假电台测试补偿。

**Consequences**: 短期存在两条 Yaesu 代码路径（框架/串口逻辑有少量重复），迁移列为三期：真机在手时逐命令 A/B 对比新旧实现，`MRRC_RADIO_MODEL=ft710` 是回滚开关。加第 5 个 Yaesu 机型只需加一张 profile 表。

## AD-019: 未验证机型仅接收（TX 门禁 + 只读身份校验 + 逐表溯源）

| Attribute | Value |
|-----------|-------|
| Type | Policy |
| Status | Implemented (V2.46) |
| Decision | 四台 Yaesu 机型（以及此前的 IC-705/7610/7760）在无真机证据时：`RadioCapabilities.verified=False`、`tx_gated=True`，`set_ptt(True)`/`set_tune(True)` 直接拒绝并**每进程仅告警一次**（`MRRC_ALLOW_UNVERIFIED_TX=1` 才放行，**释放永不被拦**）；`ID;` 只读校验记录实测字节，profile 未记录期望值时仅 INFO，不符只告警**绝不阻断**；每张表在 `provenance` 里写明来源文件与符号，拿不到的数据标 `TODO(hw-verify)` 并进入 `unverified_meters`；`_diag_yaesu.py` 负责在有真机时闭合这些缺口。 |

**Problem**: 无真机的实现容易被呈现为「已验证」：表头曲线看起来一样、模式表看起来合理，一旦现场表现不同，用户无法判断是电台行为、接线还是实现猜测。更危险的是 TX——在未验证的频率/功率语义上发射可能对电台或天线系统不利。

**Rationale**: 「不确定就说不确定」比「猜一个并当作数据」成本低得多：门禁把风险最高的动作（发射）变成显式选择，溯源让每张表都可追溯、可纠正，诊断脚本把闭合缺口变成一次粘贴。

**Consequences**: 新机型首次连接只收不发（UI 显示"实验性，仅接收"），需要操作者显式开启；模式/滤波/表头在获得现场回传前都带未验证标记；`dual_rx`（FTDX101D/MP 与 FTX-1F 的双接收）**只记录不实现**，留待二期独立规格。
