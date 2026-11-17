# 8. Architecture Decisions (ART 0513)

## AD-001: Use FastAPI/Uvicorn for MRRC Modern Server

| Attribute | Value |
| ----------- | ------- |
| Type | Architectural |
| Status | Implemented |
| Decision | Use FastAPI with native WebSocket routes and Uvicorn runtime |

**Problem**: The server needs static file serving, 4 WebSocket endpoints, async serial CAT I/O, scope subprocess management, audio streaming, and auth — all in one process.

**Rationale**: FastAPI/Uvicorn provides direct async integration, lifespan management, middleware, and a small code surface. No Tornado, Flask, or Django needed.

**Consequences**: All server logic lives in `server.py` with modular imports from sibling modules. Lifecycle managed via `@asynccontextmanager lifespan`.

## AD-002: Direct Serial CAT — No Hamlib/Rigctld

| Attribute | Value |
| ----------- | ------- |
| Type | Architectural |
| Status | Implemented |
| Decision | Use `pyserial` (sync API) with `asyncio.to_thread()` for serial I/O |

**Problem**: Hamlib adds a large dependency and another process to manage. FT-710 CAT protocol is well-documented (Yaesu standard) and straightforward.

**Rationale**: A dedicated `CatController` class with an `asyncio.Lock` for serialized access and thread-pool offloading is simpler, more debuggable, and has fewer failure modes than Hamlib/rigctld.

**Consequences**: Radio protocol code lives inside pluggable `backends/<model>/` packages. Adding another radio model requires a new backend package plus factory registration, not changes to `server.py`.

## AD-003: Dirty-Field State Broadcasting

| Attribute | Value |
| ----------- | ------- |
| Type | Design |
| Status | Implemented |
| Decision | `RadioState` dataclass tracks changed fields via `_dirty_fields` set; broadcast only changed fields |

**Problem**: Full state broadcasts on every poll would be wasteful (~44 fields at 10Hz).

**Rationale**: `RadioState.update()` records which fields changed; `to_dirty_dict()` sends only those fields. Clients merge partial updates into their local state.

**Consequences**: `stateUpdate` messages are compact; clients must maintain local state mirror.

## AD-004: Tagged Dual-Codec Audio Transport (Opus + Int16 PCM)

| Attribute | Value |
| ----------- | ------- |
| Type | Architectural |
| Status | Implemented |
| Decision | Both `/WSaudioRX` and `/WSaudioTX` carry a 1-byte codec tag per frame: `0x00` = Int16 PCM, `0x01` = Opus. RX: 48kHz @ 64kbps (fullband, transparent for broadcast music). TX: 48kHz @ 64kbps CBR (voice with fidelity priority: complexity=5, SIGNAL=VOICE, VBR/FEC/DTX disabled). Default Opus; falls back to PCM. |

**Problem**: (RX) Int16 PCM at 48kHz mono costs ~768kbps — heavy on mobile/WiFi. Opus at 64kbps cuts that 12×. (TX) Browser mic Opus encoding saves uplink bandwidth. A per-frame tag removes negotiation races — receiver inspects tag and decodes accordingly.

**Rationale**: `opus_rx.py` (copied from sunmrrc) provides direct ctypes libopus bindings. Uses `max_data_bytes` cap on `opus_encode()` to control bitrate — avoids arm64 variadic `opus_encoder_ctl` issues. Browser uses WASM `OpusDecoder`/`OpusEncoder`. TX encoder configured for voice with fidelity priority (`static/modules/opus_codec.js`): complexity=5, 64kbps CBR (stable packet size), FEC=OFF (WebSocket TCP is reliable), DTX=OFF (no priming gaps), fullband, SIGNAL=VOICE, LSB depth 16. (Raised from the original 28kbps/complexity=3 for transmit-fidelity headroom.)

**Consequences**: Adds libopus dependency (optional — degrades gracefully to PCM). Codec is user-switchable. `AUDIO_TAG_PCM` / `AUDIO_TAG_OPUS` are constants in both Python and JavaScript.

**Amended (V3.26, 2026-11-17, AD-038)**: the tag table gains a third entry — `0x02` = Int16 PCM at **16 kHz** mono (`AUDIO_TAG_PCM16`, owned by `audio_profile.py` and re-exported from `opus_rx.py`). It is only ever sent on an audio socket that negotiated the PCM16 profile with an `{"type":"audioCaps","profile":"pcm16"}` text frame; `0x00` and `0x01` keep meaning 48 kHz for every client, negotiated or not — a tag is self-describing and is never reinterpreted per connection. The legacy default stream is byte-for-byte unchanged.

## AD-005: scope_pipe as Standalone Subprocess

| Attribute | Value |
| ----------- | ------- |
| Type | Architectural |
| Status | Implemented |
| Decision | FT4222 SPI I/O runs in a separate Python process (`scope_pipe.py`), communicating with the server via stdout/stderr pipes |

**Problem**: FT4222 ctypes calls are blocking and can hang. Running them in the asyncio event loop would stall the entire server. Threading is fragile with FTDI D2XX driver state.

**Rationale**: A subprocess isolates the FTDI driver. If scope_pipe crashes, the server continues (falls back to S-meter). Frame format: 4-byte BE uint32 length + payload. stderr carries machine-parseable `STATUS:` lines for diagnostics. Heartbeat frames (len=0) keep pipe alive when idle.

**Consequences**: scope_pipe is independently restartable. Server handles pipe exit gracefully (marks scope disconnected, switches to fallback). Two process lifecycle to manage.

**Amended (V2.7)**: the pipe protocol gains a stdin control channel — the server pushes `TX:1`/`TX:0` on every `tx_status` transition (`_notify_scope_pipe_tx`). While TX is active the pipe pauses SPI reads and freezes all sync/stall recovery counters (the FT-710 garbles its scope stream during TX; reading it previously churned the pipe into `fatal:too_many_reinits` after every PTT), and runs one clean re-sync on TX→RX. stdin EOF (parent died) also stops the pipe. Windows teardown uses `taskkill /PID <pid> /T /F` because `terminate()` only reaches the onefile bootloader and orphans the real worker (which then holds the FT4222: FT_DEVICE_NOT_FOUND for the next pipe). This subprocess is used only by the FT-710 backend; the IC-7300 backend receives 0x27 spectrum frames directly on the CI-V serial port.

## AD-006: Dual-Mode Spectrum (FT4222 + S-Meter Fallback)

| Attribute | Value |
| ----------- | ------- |
| Type | Design |
| Status | Implemented |
| Decision | When FT4222 is available, broadcast real 850-point FFT data. When unavailable, generate synthetic multi-peak Gaussian spectrum from CAT S-meter readings |

**Problem**: FT4222 requires specific libraries, D2XX driver config, and exclusive device access. It's not always available.

**Rationale**: The S-meter fallback provides useful visual context (shows band activity) even without hardware scope. The binary frame format is identical in both modes — clients don't care about the source.

**Consequences**: `ScopeHandler` has two code paths: `update_from_scope_frame()` (real data) and `update_from_radio_state()` (synthetic). `scope._connected` flag determines which is active.

## AD-007: PTT Release as Safety-Critical Flow

| Attribute | Value |
| ----------- | ------- |
| Type | Safety |
| Status | Implemented |
| Decision | Multiple independent release paths: normal WebSocket command (fire-and-forget TX0), PTT watchdog, dead-man switch on WS disconnect, beforeunload beacon, pagehide handler, and — since V2.63 — the **TX-phase liveness gate** (`MRRC_REMOTE_SESSION_TX_HEARTBEAT_S`, off by default) for connections that die without a TCP close. Amended V2.63: see ch15 §15.6 |

**Problem**: A lost or unprocessed PTT release command can leave the radio transmitting indefinitely — a serious safety and regulatory issue.

**Rationale**: Release is more safety-critical than keying. Each layer catches a different failure mode: lost WS message, half-open socket, browser crash, tab close, app switch. See Chapter 15 for detailed PTT Safety Architecture. **V2.63 amendment — what the original list missed:** every layer above keys off a *disconnect*, and a connection that never closes (dropped NAT entry, Wi-Fi switch, silently discarded packets) produces no disconnect to observe, while the one opt-in fallback (`MRRC_PTT_MAX_TX_SECONDS`) shipped disabled. In a LAN that is an edge case; reached through a NAT it is the normal failure mode, which is why the liveness gate exists and why the cloud hub turns it on.

**Consequences**: Frontend PTT logic is more complex; polling skip-on-PTT ensures state consistency. (V1.2 removed the 3×200ms post-release verify loop — it added ~600ms to every release; stuck-keyup detection now relies on the 500ms TX-status poll plus the browser watchdog.)

## AD-008: PyAudio Auto-Detection of Supported Radio USB Audio

| Attribute | Value |
| ----------- | ------- |
| Type | Design |
| Status | Implemented |
| Decision | Multi-layer device selection parameterized by backend: (1) explicit `MRRC_AUDIO_RX_DEVICE`/`MRRC_AUDIO_TX_DEVICE` env var (index or name substring), (2) per-backend name hints (e.g., "FT-710"/"FT710"/"YAESU" for FT-710), (3) generic "USB Audio CODEC" / "USB Audio Device" fallback (common built-in sound card names on Windows; first match wins, multi-match warns), (4) mono-channel heuristic (radio USB audio is typically mono), (5) full-duplex heuristic for TX (device with both input + output), (6) system default fallback |

**Problem**: USB audio device naming varies by radio, OS, and driver version. Hardcoding a device index is fragile. Previous version only searched by name substring and fell back to first input device — could select webcam mic instead of the radio. On Windows the FT-710's card carries no "FT-710"/"YAESU" string at all ("USB Audio CODEC" or "USB Audio Device", possibly localized/prefixed), so without tier (3) the heuristics grabbed a laptop mic (RX) or PC speakers (TX) — V2.6 field report.

**Rationale**: Name-based matching is more robust than index-based. The mono-channel heuristic is reliable: supported radio USB audio typically provides exactly 1 input channel (mono RX), while webcams and USB mics typically offer 2 (stereo). Full-duplex preference for TX ensures the same device is used for both RX and TX paths. Logs all available devices at startup for debugging. The generic USB-audio names rank below the radio-specific hints but above the channel heuristics; duplicates from per-host-API enumeration (MME/DirectSound/WASAPI) open the same hardware, and a genuine multi-device setup is warned about with a pointer to the env-var lock (`USB Audio` is the common substring covering both enumeration forms).

**Consequences**: Audio may still use wrong device if multiple mono USB audio devices are present. Configurable device override via env vars is the recommended approach for such setups.

**Amended (V2.8)**: Windows full-duplex wedge — on Windows (MME/DirectSound), opening the TX playback stream on the FT-710's C-Media codec silently wedges RX capture (stays open, error-free, delivers silence; field symptom: RX audio perfect after server restart, gone after one PTT). `AudioHandler.restart_rx()` originally reopened capture on TX→RX only on Windows.

**Amended (V3.24, 2026-11-11)**: a selected device can be *wrong and silent*. Windows exposes virtual endpoints (`主声音捕获驱动程序` / `主声音驱动程序`) and Realtek's loopback (`立体声混音` / `Stereo Mix` / `What U Hear`) in the same PortAudio enumeration as the radio codec; they open successfully and carry no radio audio, so RX runs and delivers silence with nothing in the log but a missing-device warning. Diagnostic bundle `20261111-212640-e397` (Xiegu G7250) had saved exactly one of them. `audio_handler.is_loopback_device_name()` / `LOOPBACK_NAME_HINTS` now keep tiers (2)-(6) away from those names (only loopbacks present → `None`, i.e. `No audio input device found`, rather than a guaranteed-silent stream), an explicit tier-(1) choice is honoured and warned about, and `/api/devices` carries a per-entry `loopback` flag so the connection dialog can label it 「电脑自带，非电台」. The same amendment splits the tier-(1) failure log: a name that matches only a device which just failed to open (the -9999 retry, V2.33) is no longer reported as `not found`. **Amended (V2.31)**: Field QSO recordings on macOS showed intermittent 20 ms RX frames with low-bandwidth/attenuated content after repeated PTT activity while a fresh parallel capture stream stayed clean. The workaround is now platform-independent: after every TX→RX transition, the server asynchronously reopens the long-lived RX capture stream. The reopen is off the event loop and does not change the 44.1 kHz device-domain / 48 kHz codec-domain contract.

## AD-009: 7-Task Adaptive Polling with Bounded Lock Time

| Attribute | Value |
| ----------- | ------- |
| Type | Design |
| Status | Implemented |
| Decision | Background CAT polling split into 7 cooperative tasks (IF, VFO, TX status, TX meters, settings, slow telemetry, connection watchdog), with skip-on-command and short per-query timeout |

**Problem**: Polling too fast floods the serial port; too slow makes the UI feel unresponsive. Some fields (S-meter) change rapidly; others (filter width) rarely.

**Rationale**: Fast path keeps only `FA/MD0/SM0` at 100ms. `VS/FB` run separately at 500ms, so active-VFO tracking does not bloat the IF loop. TX status and TX meters are independent 500ms tasks; TX meter polling includes `RM3/RM4/RM5/RM6` and is TX-only. Settings (2s) include `RG0`, `MS`, and tuner state; slow telemetry (5s) includes `RM7/RM8`, `PR`, `AO`, and `RI0`. Poll query timeout is 0.25s to cap lock occupancy.

**Consequences**: `PollScheduler` owns task-level cadence and backpressure controls (`skip_next_poll()`, short pause after user command, and `_cancel_polls` awareness). CAT errors remain per-command and non-fatal. Since V1.7, poll loops re-check skip state AFTER each in-flight query response and discard stale reads (a mid-flight `SH0;` response previously overwrote a just-set filter width); the filter set path additionally verifies with a 150 ms `SH0;` read-back.

## AD-010: Memory Channels as Server-Side JSON

| Attribute | Value |
| ----------- | ------- |
| Type | Design |
| Status | Implemented |
| Decision | Memory channels stored server-side in `mem_channels.json`; API: GET/POST `/api/mem_channels`; auto-broadcast to all clients on change |

**Problem**: Client-side-only storage loses channels across devices/browsers. Server-side persistence ensures all clients see the same channels.

**Rationale**: Simple JSON file is adequate for 6-99 channel slots. No database needed. Auto-broadcast keeps all clients in sync.

**Consequences**: Channels survive server restarts. File is human-editable. No per-user channel isolation (single shared-password model).

## AD-011: 48kHz Codec Domain with Per-Backend Device-Rate Bridge

| Attribute | Value |
| ----------- | ------- |
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
| ----------- | ------- |
| Type | Design |
| Status | Implemented |
| Decision | Poll `VS` + `FB` at 0.5s and treat `freq` set command as "apply to currently active VFO" |

**Problem**: Using `freq` as VFO-A-only could update the wrong oscillator when VFO-B is active.

**Rationale**: Active-VFO tracking keeps UI and CAT semantics aligned with front-panel behavior.

**Consequences**: State now carries `active_vfo`, `vfo_a_freq`, and `vfo_b_freq` continuously.

## AD-013: FT-710 Meter Calibration Tables

| Attribute | Value |
| ----------- | ------- |
| Type | Design |
| Status | Implemented |
| Decision | Convert raw RM meter values (0–255) into engineering units via piecewise-linear calibration tables in `config.py` |

**Problem**: Raw meter values are not user-meaningful and are non-linear.

**Rationale**: Calibration points from FT-710 rig data provide practical watt/SWR/volt/amp displays without firmware changes.

**Consequences**: UI meters show engineering units; calibration can be tuned independently of polling logic.

## AD-014: FT-710 CAT Errata Handling

| Attribute | Value |
| ----------- | ------- |
| Type | Design |
| Status | Implemented |
| Decision | Apply FT-710-specific command corrections: treat `DN` as step-down (never poll), use `PR00/PR01` for compressor, and map tuner control to `AC000/AC001/AC003` |

**Problem**: Yaesu CAT documentation ambiguities can cause unintended RF behavior (frequency drift, invalid tuner command forms).

**Rationale**: Runtime behavior is grounded in observed FT-710 responses and cross-command consistency.

**Consequences**: Safer default control path; ambiguous commands are either corrected or intentionally omitted.

## AD-015: Priority CAT Command Preemption

| Attribute | Value |
| ----------- | ------- |
| Type | Safety / Responsiveness |
| Status | Implemented |
| Decision | Introduce `send_priority_set_command()` and `_cancel_polls` cooperative abort so latency-sensitive commands (PTT/TUNE) preempt poll queries |

**Problem**: User TX/RX transitions can stall behind in-flight poll cycles if every query holds the serial lock to timeout.

**Rationale**: Priority commands set a cancel flag observed by poll loops, queued query waiters, and `_read_until()` threads, reducing worst-case handoff delay.

**Consequences**: Poll loops must be cancel-aware; UX is significantly more responsive during fast PTT/tune transitions.

## AD-016: Pluggable Radio Backend Architecture (FT-710 + IC-7300 via RadioBackend/Capabilities)

| Attribute | Value |
| ----------- | ------- |
| Type | Structural |
| Status | Implemented |
| Decision | Introduce a `RadioBackend` ABC + `RadioCapabilities` dataclass + `ScopeProducer` protocol in `backends/base.py`, a lazy `create_backend(model)` factory (`backends/__init__.py`, keys `ft710`/`ic7300`/`ic7300mk2`, selected by `MRRC_RADIO_MODEL`), and per-model packages `backends/ft710/` (Yaesu ASCII CAT + FT4222 scope_pipe) and `backends/ic7300/` (CI-V codec/controller at 115200 8N1 addr 0x94, 0x27 scope demux on the same port); radio-specific tables move to per-backend config modules, `fullState` carries radioModel/radioDisplayName/capabilities, and server mode/band/tune branches become backend-aware |

**Problem**: The server was hard-wired to the FT-710 — Yaesu ASCII CAT, FT4222 spectrum, 44.1 kHz USB audio — so supporting the Icom IC-7300 (CI-V protocol, in-band 0x27 spectrum, 48 kHz native audio) would have meant forking server logic.

**Rationale**: A backend ABC mirroring the CAT surface plus defaulted hooks (bands/ui_modes/filter_tables/poll-item lists/init_scope/create_scope_producer) keeps `server.py`, polling, and state radio-neutral; capabilities pushed in `fullState` let the frontend adapt bands, modes, FIL1-3 filters, ATT/PRE steps, meter visibility, and scope banner without protocol changes.

**Amended 2026-09-12 (V2.41)**: the Icom side of this layer became profile-driven. `backends/ic7300/civ_profiles.py` holds one `CivModelProfile` per model (address, Transceive set-mode item, scope geometry, bands, modes, attenuator steps, meter curves, verification flags, provenance) and the shared codec/controller/scope producer take those values as optional parameters whose defaults reproduce the previous IC-7300 constants. Adding IC-705/IC-7610/IC-7760 therefore required **no new protocol code and no `server.py` model branches** — three subclasses overriding one attribute, one registry entry, and a capability-driven attenuation bound. Models without hardware evidence set `verified=False` and refuse keying until `MRRC_ALLOW_UNVERIFIED_TX=1` (the first time this layer carries an explicit *unverified* state rather than implying acceptance). The package directory name `backends/ic7300/` now hosts the shared Icom core; renaming it to `backends/icom/` is tracked as a separate change.

**Amended 2026-11-07 (V3.19)**: the Xiegu G7250 is the first family whose **spectrum does not come from the CAT bus at all**. `backends/xiegu/` reuses the shared CI-V core for CAT (plus **address discovery** — Xiegu publishes no G7250 address, so `connect()` probes `0xA4`/`0x88`/`0x70`) and adds `lan_iq.py`, a UDP IQ `ScopeProducer`: the radio listens on 50002 for a 14-byte tune frame and pushes 768-byte int16 I/Q payloads to the PC on 50001 (48 kS/s = ±24 kHz), which the producer FFTs into the same `ScopeHandler` fields the FT4222 and CI-V producers fill. Consequences recorded in the code: `capabilities.scope_type` gained the value `lan_iq` (the frontend needed **no** change — its span table was already rebuilt from `capabilities.scope_spans`, so the one fixed span `{0: 24000}` is data), `set_frequency()` writes CAT **and** the LAN tune frame (the IQ centre only moves on the latter), `init_scope()`/`set_scope_*` are inert and `scope_on` leaves the poll table (no CI-V `0x27` on this radio), and an **empty** `CivModelProfile.transceive_item` now means "send nothing" instead of guessing a `1A 05` payload that could set an unrelated parameter. AD-019's policy applies unchanged: `verified=False`, TX gated, every unproven table marked `TODO(hw-verify)`.

**Consequences**: New radios are added as a `backends/<model>/` package plus factory registration; FT-710 behavior is unchanged (compat shims keep root imports valid); IC-7300 hardware-verification items (S-meter top point, TUNE carrier behavior, MK2 transceive item) remain flagged in code comments.

## AD-017: 服务端 QSO 录音（增量 MP3、16 kHz 存储域）

| Attribute | Value |
| ----------- | ------- |
| Type | Structural |
| Status | Implemented (V2.42) |
| Decision | 录音在服务端进行：`recorder.py` 的 `RecordingSession` 把 RX（设备域 PCM）与 TX（解码后的麦克风 PCM）放在一条 `time.monotonic_ns()` 定位的单声道时间轴上（源切换重锚定、50 ms 连续性容差吸收调度抖动、真实停顿补静音），用 **lameenc 增量编码边录边落盘**到 16 kHz 单声道 MP3；`server.py` 用**单 writer 任务 + 有界队列**驱动它（**专用单线程池**编码，绝不占用与串口/音频共享的默认 executor；事件循环零阻塞），控制面复用 `/WSradio`（`set{field:"recording"}` + `recordingState` 广播 + `fullState` 快照），文件面为三条 REST 路由（列表 / Range 流 / 删除），前端提供「录音」面板 |

**Problem**: 浏览器端录音器把到达的帧无时间戳地顺序拼接，网络抖动、jitter-buffer 补帧与队列丢弃被永久写进文件 —— 表现为回放"颤抖/哆嗦"（字段报告 2026-09-12）。此外音频只在页面可见时可靠，且 500 KB 的 MP3 编码器要下发到每个客户端。

**Rationale**: 服务端取音点是设备域 PCM（`AudioHandler.read_rx_chunk()`）与 Opus 解码输出，二者都不经过网络；时间戳 + 容差 + 空洞填静音把"抖动"与"真实停顿"分开处理，这正是回放平滑的关键。增量编码让 RAM 恒定（编码器 + FIR 状态 + 单块）且进程崩溃后磁盘上的前缀仍可播放，相对内存缓冲方案 1 小时录音省约 115 MB（本项目发布树莓派镜像）。16 kHz 是与兄弟项目 `mrrc` 一致的存储域，使 `recordings/` 目录可直接被其既有工具消费（同一命名 `<freq>kHz_<date>_<time>.mp3`）。

**Consequences**: 16 kHz 是**只写汇点**，从 48 kHz codec 域到达：44.1 kHz 设备音频先经 `audio_resample`（AD-011 唯一 SRC 桥），再经专用抗混叠 FIR 抽取 3:1；录音路径不回灌 codec/device 域，AD-011 不受影响。录音与 CAT 解耦（USB 音频可用、CAT 断线也能录，无 CAT 时频率记为 0 → 文件名 `00000kHz`）。保留策略**刻意不自动清理**（§13 R10），会话上限 `MRRC_RECORDINGS_MAX_SESSION_MIN` 只停止录音、不删除文件。不做所有权仲裁（I6 仍未解决）：任何已认证客户端可启停，状态对所有客户端广播。

**Writer 生命周期（V2.45 修正）**：writer 任务是**每会话一次性**的 —— 收到 stop 哨兵、MP3 定稿后即 `return`，这正是关闭流程能 `await` 它完成刷盘的前提。因此它的创建属于**每个会话**，而不是 lifespan：`_ensure_rec_writer()`（幂等）在录音开始、stop 入队与音频块入队前调用，发现任务已结束就重建。只在启动时创建一次会让任务在首次 REC/STOP 后永久消失 —— 此后每个会话都没有消费者，有界队列瞬间填满、所有块被丢弃，`stop()` 再用静音补齐时间轴，产出一个**大小正确但 100% 静音的 MP3**（2026-09-12 现场报告）。队列满时 stop 路径**不得**在事件循环上直接调用 `session.stop()`（writer 线程可能正在 `add_audio`）：改为丢弃最旧的一块给哨兵腾位，保持单写者不变式。丢弃计数按会话统计并随 `recordingState` 上报 —— 块被丢弃是"录音有空洞"的唯一可观测信号。

## 8.16 Decision Summary

| ID | Topic | Status |
| ---- | ------- | -------- |
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
| AD-018 | Profile-driven Yaesu ASCII-CAT core (FTDX10/FTDX101D/MP/FTX-1F/FT-891); the verified FT-710 path stays separate | Accepted (V2.46), migration deferred to phase 3 |
| AD-019 | Unverified models ship receive-only: transmit gate, read-only identity check, per-table provenance | Implemented (V2.46) |
| AD-020 | One-touch CQ (server-side one-shot automated transmission) | Implemented |
| AD-021 | Server-built, allow-list-redacted diagnostics bundle with a dedicated receiver (support chain 1/4) | Implemented (V2.51) |
| AD-022 | Update channel: generated `latest.json`, self-proving `state.json`, upgrade blocked while transmitting (support chain 3/4, slice 1); the box line carries an independent version **and a display-only fix-pack number** (`boxLine.fixpack`, 2026-10-22) | Implemented (V2.53) |
| AD-023 | Remote-session telemetry (listener concurrency + uplink, counts without identifiers) | Implemented |
| AD-024 | Session token transport (cookie → header for native clients; query deprecated) | Implemented |
| AD-025 | Spectrum bandwidth tiers (shape × divider, gated by `spectrumCaps`) | Implemented |
| AD-026 | Setup hotspot and the password-less onboarding window (opened by network path, not a flag) | Superseded (V2.80, 2026-10-11) — 真机验收失败，见正文与 2026-10-11 LAN 设计 |
| AD-027 | Factory-preset (ship-ready) box image: `--preflash`, off by default; fixed credentials + system half; Wi-Fi and the public entry are configured from `/settings` | Implemented (V2.92) |
| AD-028 | Local control surface: a Bluetooth HID remote drives the radio on-box (`local_ctl.py` + `mrrc-localctl`), keyed on HID usages, speaking the browser protocol over loopback; `HandlePowerKey=ignore` returns the 电源 key to the operator | Implemented (V2.97) |
| AD-029 | Independent SDR receiver: the box's SDRplay RSP1 published as its own public web receiver — own password/cookie/page, zero coupling to radio control; a device-exclusive subprocess (`sdr_pipe.py`) + a native two-stage channelizer (`sdr_dsp.c`, 0.086–0.151 core per demodulator measured) + a pure-logic sharing hub (30 s tuning hold, demodulator pool, 8 channels / 50 listeners) | Implemented (V3.00) |
| AD-030 | Serial instrument (NanoVNA-H on the box's USB): `/Nano` antenna bench — `nano/analysis.py` pure math, `nano/vna.py` the only serial touchpoint (`scan_bin` binary frame with exact-length validation), four guards (refuse while transmitting, one operation at a time, never the CAT port, no listen/SDR sessions), device sweep restored after every scan; firmware upgrade via ROM DFU (U3, `MRRC_NANO_ALLOW_FLASH`), version read-back as the only success criterion | Implemented (V3.02) |
| AD-032 | Box-level audio levels (`audio_levels.py`): one owner per level — `tx_mic_db` reaches only the browser microphone (CQ/FT8 untouched), `rx_db` only the copy encoded for listeners (recording, FT8 tap and a local speaker stay native); 0 dB is byte-identical, a bad value is refused, clipping is counted | Implemented |
| AD-033 | Pure-network radio backend (`backends/sunsdr/`): one UDP engine for control, IQ spectrum, 48 kHz RX/TX audio and telemetry; default audio-factory hook preserves USB-audio models; network-aware setup; bounded PTT release retry | Implemented offline (V3.21); hardware-gated |
| AD-035 | SCU-LAN10 network transport for the Yaesu family (`backends/yaesu/scu_lan/`): `5A5A` UDP framing + gTable XOR on four channels, seven-phase session, serial-port lookalike under an unchanged `YaesuCatController`; CAT + spectrum + RX audio over the customer's LAN unit; key-up refused until the TX audio frame is characterised | Implemented (2026-11-15)；2026-11-16 真机（FTDX101MP，BG7ZHS）CAT/频谱/RX 音频跑通，TX 仍拒绝、频率轴标定未闭合 |
| AD-036 | Antenna selection belongs to the **panadapter/SCU**, not the radio and not the slice alone: `slice set <n> rxant=<name>` (names come from the radio's own `ant_list`), one command changes the owning panadapter and all its slices; the S-meter is per slice (`meter … src=SLC num=<slice>`), so reading it needs `own_slice`; the engine now creates/adopts a slice on **its own** pan and refuses to run without one | Implemented (1.28.70 · FP57, meter slice-fix in 1.28.71 · FP58); TX antenna untested behind the AD-019 gate |
| AD-037 | FlexRadio 的**对象归属**决定频谱能不能用：panadapter 是电台侧独立对象，一块**没有 slice** 的 pan 照样送 FFT，但那是没有射频输入的底噪；`sub pan all` 会推**所有客户端**的 pan 状态，任何 per-object 状态必须按对象 id 分槽；显示必须挂在**我们的 slice 所在的**那块 pan 上（pan 跟着 slice 走）；像素→dBm 的孔径（`min_dbm`/`max_dbm`）归电台、必须回读，字节域恒 0..255；设置必须写进 systemd 真正读的 env 文件 | Implemented (1.28.77 · FP64, hardware-verified)；TX 未键控（AD-019 门禁） |
| AD-038 | ESP32-C3 PCM16 单模音频档位：主语音总线保持 48 kHz；per-audio-socket `audioCaps` 协商 + 自描述标签 `0x02` = 16 kHz Int16 mono PCM；RX 对 PCM16 组每拍共享一次 soxr LQ 48→16 转换（PCM16-only 不编码 Opus）；TX 全链路显式 `input_rate`，后端直达设备率；PCM16 转换失败强制 RX | Implemented offline (V3.26)；ESP32 固件与真机验收单独记录（Task 10） |

## AD-018: Yaesu 多机型走 profile 驱动的共享 ASCII-CAT 核心（FT-710 验证路径保持独立）

| Attribute | Value |
| ----------- | ------- |
| Type | Structural |
| Status | Accepted (V2.46)；FT-710 迁移列为三期 |
| Decision | 新增 `backends/yaesu/`：`yaesu_profiles.py` 是每机型差异的唯一来源（模式寄存器 **与** 独立的 CAT 字符查找表、滤波槽位、频段、衰减/前置步进、功率格式、S 表曲线、验证状态、逐表溯源），`cat_core.py` 承载从 FT-710 `cat_controller.py` **移植**过来的传输层（`;` 帧、AI 帧前缀过滤、写-only set、PTT/TUNE 优先级抢占、ENXIO 与瞬态错误分类、重连节奏），`backend.py` 由 profile 派生能力位/UI 表/状态表/poll 项。`backends/ft710/` **完全不动**。 |

**Problem**: 加 Yaesu 机型时最省事的做法是让每台新机继承 `FT710Backend`，但这会把每日在用的 FT-710 类层次拖进三台**未验证**机型的需求里，并把并非按共享核心设计的 `cat_controller.py` 事实上变成共享核心（含 FT-710 专有假设）；而「同期把 FT-710 也迁到共享核心」虽能让核心被真机验证，却让一个发布里改动用户每天通话的代码路径，爆炸半径最大。

**Rationale**: Hamlib 4.7.2 自身就是该架构的先例（`newcat.c` 13,048 行共享核心 + `ftdx10.c`/`ftdx101.c`/`ft710.c` 各 ~300 行机型表），仓库内也有已验证的同类先例（`backends/ic7300/civ_profiles.py`）。新核心因此坐在「被真机验证过的传输逻辑 + 数据化机型表」之上，同时 FT-710 的现场行为零风险。核心的无真机验证问题由 Hamlib 模拟器与 pty 假电台测试补偿。

**Consequences**: 短期存在两条 Yaesu 代码路径（框架/串口逻辑有少量重复），迁移列为三期：真机在手时逐命令 A/B 对比新旧实现，`MRRC_RADIO_MODEL=ft710` 是回滚开关。加第 5 个 Yaesu 机型只需加一张 profile 表 —— **FT-891（V2.72）实测了这句话，并暴露它差一点才成立**：它的滤波宽度**写格式**与家族不同（`SH01NN` vs `SH00NN`，Hamlib `newcat.c:9658-9663` 的 `int on = is_ft891`），纯数据表装不下「命令前缀」这类差异。修法是给 profile 加 `filter_width_prefix` 字段、核心 `set_filter_width` 改读它（对四款已发布机型字节等价），于是核心仍然零机型分支——结论修正为：加机型只需一张 profile 表，**必要时给表加一个字段，但绝不在核心里写 `if model ==`**。

## AD-019: 未验证机型仅接收（TX 门禁 + 只读身份校验 + 逐表溯源）

| Attribute | Value |
| ----------- | ------- |
| Type | Policy |
| Status | Implemented (V2.46) |
| Decision | 五台 Yaesu 机型（以及此前的 IC-705/7610/7760）在无真机证据时：`RadioCapabilities.verified=False`、`tx_gated=True`，`set_ptt(True)`/`set_tune(True)` 直接拒绝并**每进程仅告警一次**（`MRRC_ALLOW_UNVERIFIED_TX=1` 才放行，**释放永不被拦**）；`ID;` 只读校验记录实测字节，profile 未记录期望值时仅 INFO，不符只告警**绝不阻断**；每张表在 `provenance` 里写明来源文件与符号，拿不到的数据标 `TODO(hw-verify)` 并进入 `unverified_meters`；`_diag_yaesu.py` 负责在有真机时闭合这些缺口。 |

**Problem**: 无真机的实现容易被呈现为「已验证」：表头曲线看起来一样、模式表看起来合理，一旦现场表现不同，用户无法判断是电台行为、接线还是实现猜测。更危险的是 TX——在未验证的频率/功率语义上发射可能对电台或天线系统不利。

**Rationale**: 「不确定就说不确定」比「猜一个并当作数据」成本低得多：门禁把风险最高的动作（发射）变成显式选择，溯源让每张表都可追溯、可纠正，诊断脚本把闭合缺口变成一次粘贴。

**Consequences**: 新机型首次连接只收不发（UI 显示"实验性，仅接收"），需要操作者显式开启；模式/滤波/表头在获得现场回传前都带未验证标记；`dual_rx`（FTDX101D/MP 与 FTX-1F 的双接收）**只记录不实现**，留待二期独立规格。**FT-891（V2.72）给本决定补上了第二类证据**：现场支持上报也算硬件证据，可用于关闭离线源推不出的事实（它的 38400 8N1 串口参数、无 USB 声卡、S 表偏高 1–1.5 S 三项都来自老产品 2026-09-17 的一份上报，见 §13.3 A10）。这比「无真机 ⇒ 全部只能假定」更强，但纪律不变：**推论值不写成期望值**——FT-891 的 `ID;` 应答可由 `NC_RIGID_FT891=135` 推出 `0135`，仍按操作者决定留空只记日志；另一份产品的电表观测也**不**用来改本仓库的曲线。

---

## AD-020: 一键 CQ（服务端一次性自动化发射，复用 PTT 安全链路）

| Attribute | Value |
| ----------- | ------- |
| Type | Design |
| Status | Implemented (V2.49) |
| Decision | CQ 呼叫**由服务端播放**：`set{field:"cq",value:true}` → 校验（TX 门禁 → 是否已呼叫 → TUNE/PTT 占用）→ 按 PTT 链路键控（`_claim_tx_owner_for_token` + `set_ptt(True)` + `start_tx()`）→ `cq_player.py` 以 20 ms 帧、设备队列低于 `CQ_LOW_WATER_FRAMES=4` 时投喂 `audio.feed_tx_audio()` → `stop_tx(graceful=True)` 排空尾音 → `set_ptt(False)` → 广播 `cqState{state:"complete"}`。再次按下 `cq:true`（或 `cq:false`）中止：**不排空**，立即松载波，`state:"aborted"` + `reason`。 |
| Alternatives | ①**浏览器播放**（`/WSaudioTX` 上传 CQ 音频，同兄弟项目 `mrrc`）：后台标签页被节流、断网即断播、三端（Web/iOS/Android）各写一遍；②**完整 TX 源多路复用器**（mic/CQ/录音回放统一抽象）：把改动引入安全关键路径，回归面最大；③服务端播放 + 最小侵入（采纳）。 |
| Consequences | 录音随服务端一起打包（`static/audio/cq.wav`，`MRRC_CQ_FILE` 可替换），所有客户端零代码即可用；CQ 期间浏览器麦克风帧被丢弃并计数（`_tx_cq_mic_drops`），避免两种人声混进同一发射；CQ 与 TUNE/PTT 互斥；发起端断线、最后一个客户端断线、外部松键（看门狗/TUNE）、服务端退出都会立即终止呼叫；`MRRC_PTT_MAX_TX_SECONDS` 依旧生效。代价：一个新增的 TX 源标记（`tx_source` 语义落在 `_cq_player.is_calling`），以及 CQ 音频不经过 Opus 链路（服务端直接用 48 kHz PCM）。 |

**Problem**: 呼叫 CQ 是每次通联都要做的重复动作：按住 PTT、对准话筒、念完一整套呼号与"Listening"。远程操作时这套动作最容易出问题——说话时麦克风增益、网络抖动、忘记松 PTT。现场反馈（2026-09-13）希望「按一下就能 CQ」，且**每次发射必须有明确终点**。

**Rationale**: 播放端放在服务端，则音频从磁盘到声卡全程不过网络，抖动与丢包都不影响发射内容；键控路径复用已现场验证的 PTT 释放架构（AD-007 的强制松键 + 15 章的七层防线），因此"忘记停止"这一类风险由既有防线兜底，而不是新写一套超时逻辑。前端只做状态镜像（`cqState`），多端天然一致。

**Consequences**: `cq_player.py` 是纯音频/状态机模块（可脱机测试，不 import 硬件库）；服务端启动时若资产缺失/损坏/超长（>30 s）则以 **WARNING** 明确说明并禁用按键，绝不静默；`fullState.cq` 让中途刷新页面/重连的客户端也能看到进行中的呼叫。

---

## AD-021: 支持诊断包（服务端构建 + 白名单脱敏 + 独立接收端）

| Attribute | Value |
| ----------- | ------- |
| Type | Design |
| Status | Implemented (V2.51) |
| Decision | 「🐞 遇到问题」入口在**浏览器**（`static/support.html`，SPA 菜单纯 `<a target="_blank">`，不打断 WebSocket），**包由服务端构建**（`support_bundle.py` + `POST /api/support/bundle | upload | save`）：日志尾部 + 按 **env 键白名单**裁剪的`state/config-redacted.env` + 环境/电台/音频状态快照 + 浏览器侧上下文 + `diagnostics/summary.txt` 自动体检结论；上传到**独立接收端实例**（同机 systemd `support-receiver-modern`、端口 8098、存储`/var/www/support-modern`、nginx`/mrrc_modern/support/`）；无网络时`只保存到本地` 是等价按钮。 |
| Alternatives | ①**浏览器构建并下载**：拿不到服务端日志/设备/端口状态，且移动端无法把包送出去；②**上传第三方日志服务**：用户数据出境、无法保证脱敏；③**复用兄弟项目 `mrrc` 的接收端与答复页**：两个产品的包混在一张清单里，第 2 期自动分诊无法按产品过滤；④服务端构建 + 白名单脱敏 + 独立实例（采纳）。 |
| Consequences | ①**日志持久化成为前置**：打包版此前只有控制台输出（没有任何日志文件），故新增 `MRRC_LOG_DIR` 下的轮转 `server.log`（3×2 MB）与启动器 tee 的 `server-stdout.log`（只在"日志系统起来之前就死了"这段窗口写，之后仅排空管道以保证子进程不会因 64 KB 管道写满而阻塞）；RPi 与 `start.sh` 的重定向改名 `server-stdout.log`，避免与会话内 handler 双写同名文件。②**版本权威**：构建脚本写入 `version.txt`（macOS `Contents/MacOS`、Windows 安装目录、rpi64 `/opt/mrrc_modern`；旧镜像的 `VERSION` 仍被识别），既是包 manifest 的版本来源，也是第 3 期一键升级的判据。③**隐私不变量**：证书私钥、`MRRC_WEB_PASSWORD`/令牌、录音、记忆频道与天调学习值**永不入包**，由负例测试守住；`redactions` 计数写进 manifest。④**接收端 create/PUT 不鉴权**（上传端无法持有服务器密钥）：靠限速 + 不可猜 ID + 清单 Basic 鉴权 + 人工删除兜底。 |
| Status detail | 规格 `docs/superpowers/specs/2026-09-17-support-bundle-design.md`；验收：真机上传→清单可见→下载 SHA-256 与本地一致 |

**Problem**: 问题上报长期以"现象描述"到达（"有杂音""升级后不响了"），第一轮往返都花在索取证据上：哪个版本、什么平台、日志在哪。而 MRRC Modern 的打包版**根本没有服务端日志文件**——日志只进控制台窗口，窗口关掉就没了。

**Rationale**: 证据只有在**故障现场**才能收集：设备表、实际采样率、串口状态、scope 是真实还是 S 表回退、TX 门禁状态，这些都无法事后补问。因此在服务端一次性收集、脱敏、自证（含自动体检结论），把"维护者第一眼要看的东西"直接写进包里的 `summary.txt`；入口放在浏览器则三端（Web/iOS/Android）与树莓派都可用。

**Consequences**: 用户自助路径从"发消息描述问题"变成"点三下拿到编号"；维护者从"追问"变成"读摘要"。代价是一个新的常驻服务（接收端）与一条隐私契约——后者是硬约束：白名单之外一律丢弃并计数，包内出现密钥即是回归。

**Amendment (2026-09-17, 支持链路第 2 期)**: 分诊与答复由 `dev_tools/support_autopilot.py` + `support_answers.py` 完成：
轮询接收端 → 取包解包 → 组装摘要（复用第 1 期的 `collect_context`，含 `summary.txt` 与日志尾部）→
`pi`（只读工具、仓库为工作目录）按固定 JSON 契约产出结论 → 渲染成答复卡。
**发布规则**：默认只写草稿（`dist/support_answers/`），`--publish` 才更新 `website/answers/index.html`、
git commit 并**单文件 rsync** 上线（全站部署仍是交互式的，不能进 cron）；即使带 `--publish`，
`need_more_info` 也**不发布**（那是给操作员的问题，不是答复）。护栏：幂等 `state.json`、
单轮上限 2 条、`pi` 超时 540 s、半包跳过、单条失败不中断。
**公开页的隐私边界**由 `support_answers.redact_public` 承担（邮箱/手机号/设备路径/串口/内网地址/本机与临时路径），
并有用带敏感值的假包做负例测试——与第 1 期的入包白名单互为镜像。**实测边界**：真机跑通一次端到端
（4m12s，结论引用了 `server.py:_rec_enqueue` 与 `recorder.py:383` 的真实代码位置），但结论质量只在
该合成包上验证过；cron 连续运行与真实用户包的表现待观察。

---

## AD-022: 一键升级通道（生成式清单 + 自证成功 + 发射门禁）

| Attribute | Value |
| ----------- | ------- |
| Type | Design |
| Status | Slice 1 implemented (V2.53)；下载/安装为 slice 2 |
| Decision | 更新通道分两片交付。**Slice 1（本次）**：`upgrade_core.py`（清单拉取/校验/比较、`state.json` schema）+ `dev_tools/make_latest_json.py`（从**真实产物**算出 size/SHA-256 生成 `website/downloads/latest.json`）+ `GET /api/update/check`（鉴权、**只读**、失败只报原因）。**Slice 2（下次）**：下载到 `<data dir>/updates/` → SHA-256 校验 → 发射/录音中拒绝（423，交接前复查）→ 拉起安装器 → **由新版本启动时自证** `state.json.lastResult.status == ok`。 |
| Alternatives | ①**应用内自动安装**（下载后直接替换自身文件）：跨平台权限/签名/回滚都不可控，且失败会把用户留在半个安装上；②**只做提示有新版本**（跳转下载页）：零风险但把升级退化成手工三步，正是本链路要消除的摩擦；③生成式清单 + 自证状态 + 两片交付（采纳）。 |
| Consequences | ①**清单不可手写**：size/SHA 来自文件本身、installer 版本必须等于 CHANGELOG 顶版本、`previous` 必须更旧，任一不满足即拒绝生成，并由与客户端同一套 `parse_manifest` 复验；②**成功不可伪造**：只有新版本启动时看到 `pendingInstall.version == 自己` 才写 ok，`installing` 明确不等于成功（测试直接断言：旧版本无法宣称胜利）；③**发射优先**：升级请求在发射/录音期间返回 423，交接前复查（第 4 期热修沿用）；④离线检测**静默**；⑤**不自动安装**：始终由操作员按键触发。 |
| Status detail | 规格 `docs/superpowers/specs/2026-09-17-upgrade-channel-design.md`；`version.txt`（AD-021 已铺到三平台）是本决策的版本权威 |

**2026-10-22 补记 —— 盒子线的修复包编号（FP）**：`boxLine` 从 2026-10-19 起独立编号
（`box.version` 不要求等于桌面 `latest`，两条线由不同构建机产出）。现在同一份清单里再加一个
**只读标签** `boxLine.fixpack`（`FP1`、`FP2`…，全局递增）：

- **它是标签，不是版本号的一部分。** `compare_versions()` 不读它，`available` 的判定与有无 FP
  完全无关（`tests/test_box_update.py::FixPackTests::test_the_comparison_ignores_it` 钉住）。
  理由是一条实测约束：现场盒子的解析器是 `^v?(\d+)\.(\d+)\.(\d+)`，把 FP 拼成第四段
  （`1.28.5.1`）在它眼里等于 `1.28.5` ⇒ 判定「已是最新」⇒ **等于没发**。
- **每发一次盒子修复包：版本号 +1（三段）且 FP 号 +1。** 编号允许断号（被撤回的那次不复用），
  不允许复用 —— 复用一个编号等于让两份不同字节的包共用一个名字。
- **载体**：`CHANGELOG` 的 `## [镜像线 vX.Y.Z · FPn]` 标题（`make_box_source.version_of()`
  一次读两个值）、tarball 根的 `fixpack.txt`（`box_update.write_fixpack()`，空值删文件）、
  `/api/update/check` 的 `current_fixpack` / `latest_fixpack`、更新卡与页脚的 `版本 · FPn`。
- **向后兼容**：新键 → 老解析器忽略（`boxLine` 当初就是这么加进来的），所以老盒子读带 FP 的
  清单不报错，只是不显示。
- 设计 `docs/superpowers/specs/2026-10-22-box-fixpack-design.md`；守卫
  `tests/test_release_artifacts.py::BoxLineFixPackTests`（CHANGELOG 最新镜像线条目 ↔ 清单，
  此前这条对齐全靠人）。

**Problem**: 前两期把报障→结论打通了，但用户拿到修复仍要自己找下载页、对 SHA、重装。同时升级类功能最容易自欺：安装器退出码 0 不等于升级成功，已拉起安装器更不等于。

**Rationale**: 因此把成功定义成一个**只能由新版本自己写下的事实**（`lastResult.status == ok`），并把清单做成**生成物**（从产物本身算哈希）—— 这两条让升级成功与清单可信都不依赖任何人的自觉。发射门禁沿用第 15 章的既有分层：升级是维护动作，绝不与载波争资源。

**Consequences**: 维护者发版时多一步 `make_latest_json.py`，换来用户侧检查→下载→校验→安装→自证全程可判定；代价是两片交付期间只有检查可用 —— 这是有意的：半成品升级器会动用户的机器。

## AD-023: 远程会话遥测（实测 Listener 并发与上行，计数不含标识）

| Attribute | Value |
| ----------- | ------- |
| Type | Design / Capacity |
| Status | Implemented (V2.62) |
| Decision | 新增 `session_metrics.py`（纯标准库、不引应用模块）：按 `(role, kind)` 统计在活 WebSocket、按 token 统计**会话数**、生命期与滑动窗口并发峰值，并对两条上行扇出路径（`spectrum` / `audio_rx`）计量字节；`server.py` 在五个 WS 端点的 accept/finally 接线、在两处扇出发送点计量字节；新增 `GET /api/session_metrics` 与每 `MRRC_SESSION_METRICS_INTERVAL_S`（默认 300 s）一条 `Session metrics:` INFO 行。 |
| Alternatives | ①**只在客户端量**（浏览器已经在算接收速率）：看不到"有几个 Listener"这件事，而并发才是扇出的触发门槛；②**把遥测上传到中心**：需要先有设备证书与用户同意，属于 Cloud Hub 的 AD-H11/AD-H13，不在本仓先做；③**只记累计值不记峰值**：典型/峰值是设计门槛的两个输入，只有累计值无法回答；④本地计数 + 峰值 + 端点（采纳）。 |
| Consequences | ①**只记计数与字节**：token 只在模块内部作不透明键，绝不进入 `snapshot()`/`report()`，因此端点与日志可安全暴露（沿用 AD-021/NFR-068 的隐私边界）；②**双重关闭容忍**：清理既走正常返回也走异常路径，且广播可能已把 socket 移出集合，计数下溢必须不抛；③**窗口峰值不衰减**：滑动窗口内的采样是真实观测，窗口内无采样时取当前值，绝不报告低于当前值；④控制面文本帧不计量（<10 kbps，对比频谱 408 kbps），这是刻意的范围限制。〔**2026-10-07 校正，见 AD-025**：408 kbps 只在 S-meter 回退态成立；真频谱实测 11.1 fps ⇒ ~151 kbps payload，且自 AD-025 起频谱按档位可配（high/mid/low ≈ 151/38/19 kbps）。控制面 <10 kbps 至今仍未被计量，是估算而非读数。〕 |

**Problem**: Cloud Hub（`mrrc_hub/SDD` AD-H12）明确拒绝在拿到"单实例真实 Listener 并发分布"之前建设 RX 扇出，而那个数字的原材料（`spectrum_clients` / `audio_rx_clients` / `_listen_tokens`）一直只活在内存里、随进程消失。同时实测已表明单会话带宽里**频谱占 ~86%**（1701 B/帧 × 30 fps ≈ 408 kbps，对比 RX Opus 64 kbps）〔**2026-10-07 校正，见 AD-025**：30 fps 是 tick 率而非出帧率——真频谱只在硬件帧计数前进时发送，实测 11.1 fps ⇒ 频谱 ~151 kbps payload、占单会话 ~72%（不是 86%）；408 kbps 是 S-meter 回退态的数字。结论方向不变（频谱仍是主项），但 hub 的容量输入应以 151 kbps 与可配档位为准。〕—— 猜错的代价不是数字难看，而是重构错了子系统。

**Rationale**: 把"要不要做扇出"从判断变成读数：峰值会话数回答"值不值得"，两路上行字节回答"省下多少"。同时把隐私边界写死在模块里（只有计数和字节），使这份观测可以默认开启、可以放上公网端点，而不必先谈同意书。

## AD-024: 会话令牌传输（Cookie 优先、Header 给原生客户端、Query 弃用）

| Attribute | Value |
| ----------- | ------- |
| Type | Security |
| Status | Implemented (V2.62) |
| Decision | WS 与 HTTP 的会话令牌统一由 `_token_from_ws_handshake()` / `_token_from_request()` 解析，顺序为 `Authorization: Bearer`（原生客户端）→ `mrrc_auth` Cookie（浏览器；同源握手自动携带）→ `?token=` query（**弃用**，仍可用，每进程告警一次并点名迁移方式）。前端四个构造点（`ft710_main.js` ×2、`listen.js` ×2、`modules/atr1000.js` ×1）不再拼 token。 |
| Alternatives | ①**`Sec-WebSocket-Protocol` 传令牌**：可行但需要服务端在 `accept(subprotocol=...)` 里回选，五个端点都要改，收益与 Cookie 相同（Cookie 本来就自动发送）；②**直接删掉 query 支持**：已安装的 iOS/Android 客户端会当场断连——它们的 `WebSocketConnection.swift` / `ConnectionManager.kt` 拼的就是 `?token=`；③三步走：Cookie 优先 + Header 可用 + Query 弃用告警（采纳）。 |
| Consequences | ①**网页侧不再有可复用凭证进 URL**，因此 Hub 透明代理、nginx access log、`$request_uri`、浏览器历史与 Referer 都不再记录令牌；②仍支持 query 是为了不打断在装机型，但每次都是可观测事件（一条 WARNING 点名 `Authorization: Bearer`）；③Cookie 依旧是 30 天 —— 缩短会话时长属于 Cloud Hub 的 AD-H09（`NFR-H013`），本次不动；④`ws-endpoint-auth` 约束规则同步改写为"必须走 `_token_from_ws_handshake`"。 |

**Problem**: 浏览器前端把会话令牌拼进 `?token=`，而实例跑 uvicorn 默认访问日志（含 query string），项目自己也早已知道这一点（`support_bundle.py` 的脱敏正则注释点名 "a `?token=` inside a URL"）—— 但脱敏只在导出诊断包时发生。一旦进入 Cloud Hub 的透明代理模式，中心侧每一条访问日志都会记录各实例的 30 天令牌；而浏览器侧没有自定义头能力这一"技术理由"，其实并不成立：**同源 WS 握手会自动带上 Cookie**。

**Rationale**: 让凭证回到它能被系统正确保护的位置（Cookie / Header），URL 里只留资源路径。对已安装的原生客户端保留 query 兜底，是因为"安全改进"不该以现有用户的连接为代价 —— 但必须让它可观测，否则永远不会有人去改。

## AD-025: 频谱带宽档位（形状 × 分频，以 caps 协商为闸门）

| Attribute | Value |
| ----------- | ------- |
| Type | Design / Bandwidth |
| Status | Implemented (V2.75, v1.26.0) — 服务端 + Web；Android/iOS 尚未发 caps |
| Decision | 新增 `spectrum_profile.py`（纯标准库、不引应用模块）：档位 = **两个正交因子**——`shape`（`full` = `0x01`+wf1+wf2 = 1701 B；`wf1` = `0x01`+wf1 = 851 B）× `divider`（广播 tick 的 1/2/3/4），包成具名预设 `high`(full,÷1) / `mid`(wf1,÷2) / `low`(wf1,÷4) 与服务端内部的 `listen`(full,÷3)。短帧由 `full[:851]` **切片**得到，因此 `scope_handler` 与 backends 一行未改、也不存在第二条会漂移的组帧路径。客户端在 `/WSspectrum` 上发文本帧 `{"type":"spectrumCaps","profile":"mid"}` 声明档位；**未声明则永远 `high`**（逐字节等于 AD-025 之前的流）。真频谱与 S-meter 回退两条路径共用同一个 `_spectrum_fanout`，按**实发字节**计量并分档计帧。 |
| Alternatives | ①**只砍 wf2**：对浏览器几乎不省钱——uvicorn 0.52.1 与浏览器协商 `permessage-deflate`，实测 30 帧×1701 B 上线后只剩 13224 B（≈441 B/帧，3.9×），其中 850 字节零值的 wf2 压缩后≈1.4 B/帧；②**只降帧率**：对 Android 不够——OkHttp 4.12 不提供该扩展（dex 里带 `Request header not permitted: 'Sec-WebSocket-Extensions'` 守卫），1701 B 真的逐个走出去，而用户要的"每档减半"要在两种口径上都成立；③**850→425 点降采样**：给所有客户端加渲染复杂度，浏览器侧收益又最小，推迟；④**音频码率档**：`setOpusBitrate` 只存在于注释与 iOS 集成文档，服务端与客户端均未实现，先补命令再谈档位；⑤**新协议版本号 `0x02`**：会静默杀掉已装 iOS（`SpectrumProcessor.swift:57` 是 `guard version == 0x01`），改为把 SDD 里从未上线的 v1=851 B 落实、全帧保持 `0x01`；⑥形状×分频两因子 + caps 闸门（采纳）。 |
| Consequences | ①**payload 轴**：high→mid→low = 151→38→19 kbps（×¼、×⅛，真频谱 11.1 fps 实测口径）；**Android 线上轴**同比例（无 deflate）；**浏览器线上轴** ≈39→20→10 kbps（deflate 已把 wf2 压掉）。两个轴差近 4 倍，引用数字必须标明口径——`Session metrics` 行因此改标 `payload≈`。②**回退态不再比健康态贵**：修好了"S-meter 回退无帧计数闸门、每 tick 重造 1701 B"这个 bug（health 11 fps vs fallback 30 fps），两条路径现在每档字节数一致。③**兼容性靠闸门而非靠客户端升级**：已装 Android 的 `SpectrumFrame.kt:14-15` 同时硬判 `size != 1701` 与 `frame[0] != 0x01`，是唯一吃不下 851 B 帧的客户端；不发 caps 就永远拿 `high`，所以本阶段**不需要**发 APK。Web 与 iOS 的解析器本来就能吃 851 B。④**多了一份按 socket 的状态**：`_spectrum_profiles` 必须在 handler 的 `finally` 里与两个 socket 集合一起回收（三个泄漏点，不是两个），已由集成测试钉住。⑤**可观测**：`session_metrics.snapshot()["spectrum_profiles"]` 分档计帧计字节，`dev_tools/spectrum_profile_probe.py` 可当场复现验收表（`--no-deflate` 切到 Android 轴，`--no-caps` 验证兼容闸门）。⑥计量刻意放在发送的 `try` **之外**：否则计数器里的 bug 会被 `except Exception` 洗成"客户端走了"而把健康 socket 静默踢出 `spectrum_clients`。⑦边界：Android/iOS 要真正省流量得等 P2/P3（发 caps + 设置项，Android 走 APK-only 通道）；跨仓 `mrrc_hub` 的 NFR-H007（0.48 Mbps）与 AD-H14（频谱占 86%）建在 30 fps 假设上，输入数据需按 151 kbps / ~72% 修正。 |

**Problem**: 公网出口带宽与移动流量账单的主项是频谱，但"省带宽"这件事在本仓从来没有可动的旋钮：帧长恒为 1701 B、tick 恒为 30 Hz，唯一存在的节流是 listener 的 ÷3。而两个实测事实使任何单一手段都不够——浏览器侧 `permessage-deflate` 已经把 wf2 的 850 个零字节压到≈1.4 B/帧（砍它对网页几乎无效），Android 侧则完全没有压缩（砍它就是全部收益）。同时 SDD §9.2.4 定义的 v1=851 B **从未上过线**（全仓无任何代码发出 `0x02`，`git log -S` 零命中），文档与线上格式已经对不上；而回退路径因为没有帧计数闸门，坏消息反而比好消息贵（30 fps × 1701 B ≈ 408 kbps，对比真频谱 151 kbps）。

**Rationale**: 把"省带宽"从一次性的格式决定变成**每 socket 可协商的档位**，并且让兼容性由服务端闸门保证而不是由客户端升级保证——未声明能力的连接逐字节拿到今天的流，所以老 Android 不会因为服务端先上线而黑屏。两个因子分别覆盖两个轴（形状覆盖 Android 的 payload、分频覆盖浏览器的 deflate 后线上字节），这样"每档减半"在两种口径上都成立，而不是只在其中一个上好看。短帧用切片而非新组帧路径，是为了让 `scope_handler` 与 11 个 backend 完全不动——组帧代码只有一份，就不存在两份会漂移的实现。全帧保持 `0x01` 而不是改成 `0x02`，是因为已装 iOS 客户端 `guard version == 0x01`：一个"更贴合文档"的版本号会换来一批静默丢帧的用户。

## AD-026: 初始化热点与免口令引导窗口（按网络路径开门，不按标志位）

| Attribute | Value |
| ----------- | ------- |
| Type | Architectural / Security |
| Status | Implemented (V2.76, v1.25.5) — 盒子侧随包提供；真机验收（V2.80，2026-10-11）：**MT7663S 起 AP 的边界答案是否定的**（NM/wpa_supplicant 全程认为 AP 已激活，但信标不上空，手机与 Mac 均不可见），热点引导在当前驱动下不可用，走有线/HDMI 路径；切换重连体验与网段冲突两项随之不再适用 |
| Decision | 盒子**没有可用上行网络**时由 `mrrc-setup-ap.service` 开一个**开放热点**；`/setup` 向导在该热点存续期间对**来自热点网段的请求**免口令开放（设口令 + 选 Wi-Fi），成功后切 STA 并关热点。所有 nmcli 交互集中在 `net_wifi.py`（纯标准库、零应用 import、runner 可注入）。 |
| Alternatives | ①**独立 setup 服务器**：暴露面更小，但需要**第二套配置写入路径**，两个 UI 各写 env 文件会互相覆盖（否决）；②**按"是否首次开机"的标志位开门**：多一个会写失败的持久化状态，故障模式是永久敞开（否决）；③**永远要求口令**：买回来只有一台手机的人无法开始——首启生成的口令用户并不知道，等于把鸡生蛋换了个位置（否决）；④**网络状态判据 + 双重闸门 + 并入现有应用**（采纳）。 |

**Problem**: 首次配置需要**同时**具备两样互为前提的东西：要配 Wi-Fi 得先能操作盒子，要操作盒子得先有网（SSH）或有显示器键盘。而"只发一个热点"并不解决问题：连上之后到达的仍是现有界面，它要一个操作者并不知道的口令。

**Rationale**:

1. **判据是网络状态，不是"首次开机"。** 起热点 ⟺ 无网线链路 **且** 无可连上的已保存 Wi-Fi。配好的盒子因此永不再开热点，且**不存在**"是否已配置"的标志位——也就没有"标志写失败导致永久敞开"这类故障模式。
2. **闸门是网络路径，两半都要成立**：`state.json` 的 `mode == hotspot` **且心跳新鲜**（`_setup_gate_open`）**且**客户端 IP 在该热点网段内。网段那一半挡住"某个局域网客户端恰好是 `10.42.0.x`"（NM shared 默认网段与家用网段撞车是完全可能的）；心跳那一半挡住"守护进程死了但文件还在"。热点一关，那个网段**不复存在**，免口令分支自然失效。
3. **页面并进现有 FastAPI 应用**（否决独立 setup 服务器）：设口令因此**复用既有通道** `first_run.update_env_file(_config_file_path(), …)`，与 `POST /api/setup` 同函数同文件；免口令窗口可写的键集被钉成**恰好两个**（`MRRC_WEB_PASSWORD`、`MRRC_AUTO_PASSWORD`），`MRRC_CONFIG_FILE`（D-11 的载荷）与 `MRRC_ALLOW_UNVERIFIED_TX`（AD-019 / NFR-067）在任何可写集合里都不出现。
4. **单射频 ⇒ 切换而非并发**（MT7663S 的 Wi-Fi 与蓝牙同芯片、走 SDIO）。切网期间守护进程**让位**：服务端在 `wizard.json` 里声明占用，守护进程不碰射频并按让位时长**补偿**窗口。回答**先发出去**再切（手机在切换瞬间就不在盒子上了），失败则**把热点开回来**并把 nmcli 的原因留在 `wizard.json` 里给页面显示——**不许静默返回**。
5. **两份文件、两个唯一写入者**：守护进程（root）只写 `state.json`（每 tick 重写，其心跳是闸门的一半），服务端（`mrrc`）只写 `wizard.json`（占用声明）。因此**不需要锁**。
6. **Wi-Fi 密码永不离开 nmcli 的 argv**：`WizardClaim` **没有** password 字段（"不落盘"是 schema 的性质，而不是每个调用者要记住的承诺），nmcli 的错误文本在存下来之前过 `net_wifi.scrub(text, password)`。这是 `support-bundle-privacy`（NFR-068）在一个新写入面上的延伸。

**Consequences**:

- **中间件放行必须由处理函数补回角色检查，漏掉它就是权限提升。** `SETUP_GATE_PATHS`（`/setup`、`GET`/`POST /api/setup/wizard*`）在 auth 中间件里被**按相等**放行到处理函数，而中间件的 listen-only 403 对这些路径**必然被跳过**（未鉴权的热点客户端没有角色可判）。因此 `_setup_access()` 是唯一裁决点：热点路径（每次进入都写 **WARNING 审计**：谁、什么时候、哪个 IP）或**完整** admin 令牌；listen 令牌在这里被拒，且回 **403 而不是 401**——那个调用者是已鉴权的，401 会误导页面"会话过期"。
- **三条围栏压住开放窗口**：① 只在无其他上行时存在；② 30 分钟（`MRRC_SETUP_AP_TIMEOUT_MIN`，可配）无人完成引导即自动关闭，且**本次开机不再重开**（闩是进程内的；持久化它等于把盒子变砖）；③ 每次免口令进入都留日志。恢复路径是**断电重启**。
- **残留风险（诚实记录）**：热点开放期间，同一网段内的人**可以先下手设口令接管盒子**。Wi-Fi 半径十几米，公寓楼里邻居可能在里面。围栏把窗口压到"你主动插电后、且附近没有网络"的那几十分钟，但**不能消除**。缓解：地址同时打在 HDMI 控制台上（有显示器的人不必用热点）。
- **未验证边界（V2.80 真机闭环）**：**AP 模式实测不可用**——固件修复（出厂 2021 版 N9，见 V2.80）后 NM/wpa_supplicant 认为 AP 已激活（`COMPLETED`、10.42.0.1/24 就位、零报错），但信标不上空（手机与 Mac 均扫不到）；上游移植记录同样只验证过 STA。另抓到并已修复一个同链路产品 Bug：NM 1.42 的 `device wifi hotspot` 不传密码会**自动生成随机 WPA-PSK**，「开放热点」实为加密网络——`start_hotspot()` 改为显式建无 security 段的 profile。STA 模式在固件修复后达到生产质量（200 包 0.5% 丢包 / 6.3 ms / 零掉线）。盒子上手因此走有线或 HDMI 路径，设计上本就不依赖 AP 成立（HDMI + `nmtui` 兜底）。
- **镜像装机清单加 `dnsmasq`**（NM 的 `ipv4.method=shared` 靠它发 DHCP/DNS；缺它的症状是**热点出现、手机连上、拿不到地址、任何地方都不报错**）。单元**不得**排在 `network-online.target` 之后——它存在的理由就是没有网。

**接替方案（2026-10-11）**：热点这条上线路被放弃，改为「盒子接入 LAN → 打开网页
配置盒子 OS」。设计见 `docs/superpowers/specs/2026-10-11-w103d-lan-box-settings-design.md`，
实现见 `docs/superpowers/plans/2026-10-11-w103d-lan-box-settings.md`。

沿用下来的三条：**无密码面必须只有一个键宽**（只有 `MRRC_WEB_PASSWORD`）、
**判据是状态而不是标志**（旧的是「热点活着」，新的是「从未设过口令」）、
**Wi-Fi 凭据只走 `net_wifi` 的 argv**（为此拒绝了「全部 nmcli 写都过 root helper」
的路线，那会让 PSK 经第二条通道）。

没有沿用的：AP 生命周期、`/run/mrrc/setup-ap/state.json` 与 `wizard.json` 两个
跨用户信箱、`mrrc-setup-ap.service`。提权从「root supervisor 管整条热点」换成
「polkit 放行 `mrrc` 写 NM + 只管四个系统动词的小 root helper」。

## AD-027: 出厂预置（发货版）镜像 —— 固定口令 + 系统层预置，Wi-Fi 与公网入口都从设置页配

| Attribute | Value |
| ----------- | ------- |
| Type | Deployment / Security |
| Status | Implemented (V2.92, 2026-10-17) |
| Decision | 盒子线新增构建开关 `--preflash`（**默认关**）。打开时共享服务层调用 `packaging/common/preflash.sh`：env 里写 `MRRC_WEB_PASSWORD`（并把 `MRRC_AUTO_PASSWORD` 显式清空）、`chpasswd` 设 `root`/`mrrc` 口令、时区 `Asia/Shanghai`、区域 `zh_CN.UTF-8`。按批次可用 `MRRC_PREFLASH_*` 覆盖，不预置电台型号/串口。Wi-Fi 与公网入口（Cloud Hub 申请）都在 `/settings` 页面上做，服务端不加新写入路径。 |
| Alternatives | ①**另起后处理脚本**（挂载镜像改口令再打包）：多出一条会漂移的第二条构建线，以后每次改服务层都要同步（否决）；②**不加开关、直接把盒子线默认做成发货版**：镜像从此不再"每台口令不同"，也给不出自用/开发用的标准镜像（否决）；③**每台在 `/boot` 写 `mrrc.env` 预置**：eMMC 路线下用户碰不到那个 FAT 分区，且两个口令/时区/locale 都要人写（否决，能力保留但不作为发货流程）。 |
| Consequences | ①**默认关是安全属性，不是选项**：`preflash.sh` 自己在 `MRRC_PREFLASH != 1` 时拒绝写任何东西，`--preflash` 默认 0，两者都有测试钉住；不传开关的镜像与今天逐字一致（口令照旧由服务在启动时生成）。②**与 2026-10-06 设计 D-3 的边界**：D-3 禁止"烘焙**猜测**"（串口、声卡、证书），出厂口令是**声明过的值**——它由卖家选择、写在文档里、且三条都能改（NFR-069）。③**与 NFR-068 / AD-021 的关系**：设置页新增的 Wi-Fi 手输与显示/隐藏只影响**输入**，已保存的 PSK 仍不回显（那需要向 NetworkManager 索要明文，等于新增一条凭据通道）。④**公网入口面板复用既有三个端点**（`/api/cloud/{state,apply,refresh}`），不复制 Cloud Hub 状态机；"批准后自己接通"仍由服务端轮询负责。⑤**首启探测的完成判据同批改掉**（`needs_first_run` 为假且串口节点在场）：没探到电台的盒子每次开机重探——否则卖家工作台上那次开机（U 盘、无电台）会把"已完成"克隆进用户家的 eMMC。 |

## AD-028: 本地控制面 —— 蓝牙遥控器直接驱动电台（PTT / 调谐 / 数字输入）

| Attribute | Value |
| ----------- | ------- |
| Type | Product / Operability |
| Status | Implemented (V2.97, 2026-10-17) |
| Decision | 新增独立守护进程 `local_ctl.py`（systemd 单元 `mrrc-localctl`，`User=mrrc`）：读蓝牙 HID 遥控器的 `/dev/input/eventN`，按**HID 扫描码**查键位表，把命令写成一行 JSON 发给服务端的**本机控制 socket** `/run/mrrc/localctl.sock`（0600，与服务同一用户，字段白名单 `freq/mode/ptt/af_gain/vfo_*`），由服务端转进**同一条** `_execute_set_command`。纯逻辑层（事件解码 / 键位表 / 数字输入状态机 / 调谐算术）与 I/O 层分离，前者无硬件可测。`/etc/systemd/logind.conf` 的 `HandlePowerKey` 由服务层改为 `ignore`。 |
| Alternatives | ①**在 `server.py` 里直接读 evdev**：把设备 I/O 塞进事件循环，且服务重启会切断"松开即停发"的释放路径（否决）；②**只做网页端按键**：本地操作员手上没有浏览器，这正是要解决的场景（否决）；③**走 lirc / `ir-keytable` 的红外解码**：这块板（`JC-S905L3A`/W103D）虽然注册了 `meson-ir`，但实测两次抓取（移动遥控 + 电视遥控）脉冲行数为 **0** —— 接收头没焊，硬件上不成立（否决，解析层仍留在模块内可复用于有红外的板）；④**把遥控器当蓝牙音频设备（HFP）采麦克风**：它的麦克风走厂商私有 BLE 服务（`00006287…`/`0000d0ff…`），不是标准 HFP/HSP（否决，见下 Consequences ④）。 |
| Consequences | ①**按键身份必须用扫描码**：实测那支中国移动语音遥控器上 `设置`/`M`/`#` 三个键都报 keycode 240（`KEY_UNKNOWN`），只看 `KEY_*` 会把它们并成一个键；`key_ids()` 先给 `u:0x…` 再退回 `k:…`。②**配对成功 ≠ 链路加密**：第一次配对报了 `Paired: yes`，但 `btmon` 显示 `LE Start Encryption` → `Encryption Change: Disabled`，于是遥控器对**每一个** GATT 属性读都回 ATT `0x0E`，bluez 的 `hog` 插件因此建不出 HID 设备（日志 `info_read_cb/proto_mode_read_cb/report_reference_cb … unlikely error`）。删设备 + `bluetoothctl power off/on` 后重配，`Encryption: Enabled with AES-CCM` 一出现，`CMCC_Voice_Remote Keyboard` 立即被创建 —— 这是运维知识，写进 `docs/W103D_USER_GUIDE.md`。③**电源键属于操作员，不属于 logind**：遥控器 `电源` 报 `KEY_POWER`，systemd-logind 默认拿它关机（现场表现为"盒子自己重启了"，日志只有 2 秒）。服务层把 `HandlePowerKey` 改成 `ignore`，该键交给本模块。④**麦克风不在本 AD 范围**：遥控器的麦是厂商私有 BLE 通道，且 `audio_handler` 的 TX 音频只从浏览器来（`LOCAL_MIC` 全仓零命中）——本地麦需要独立的"本地麦克风 → TX 队列"能力，留作下一步。⑤**MT7663 的 BT 与 Wi-Fi 共用 SDIO**：HID 活跃期间 Wi-Fi 出现过一次瞬断（ping 恢复在数秒内），本地操作台的告警与重连必须容忍这一点。⑥**不走网页鉴权（实现中途改过一次）**：第一版让守护进程用 env 里的 Web 口令登录再连 `/WSradio`，结果每次重连都登一次，几分钟就把服务端「5 次 / 5 分钟」的登录闸门撞成 `HTTP 429`，守护进程随后完全没有连接。物理按键不该有口令：改成 Unix socket 之后**操作系统权限即认证**，没有登录、没有令牌、没有限流，而且命令与浏览器走**同一条执行路径**（不是第二条会漂移的命令通道）——socket 的字段白名单只放行遥控器真正会用的六个字段。⑦**按住不放要真的连续**：遥控器按住时发 `value 2`（`KEY_REPEAT`），而 evdev 里重复事件的 `pressed` 是 **False**——最初的重复分支要求 `pressed=True`，于是所有重复被自己丢掉，按住只有第一下生效（现场"不灵敏"的真因）。 |

## AD-029: 独立 SDR 接收机（对外发布的 Web 接收器）

| Attribute | Value |
| ----------- | ------- |
| Type | Product / Operability |
| Status | Implemented (V3.00, 2026-10-18) — 盒子（Linux/aarch64） |
| Decision | 把插在盒子上的 SDRplay RSP1 做成**独立入口的公开接收机**：自己的口令（`MRRC_SDR_PASSWORD`）、自己的 cookie（`mrrc_sdr`）、自己的页面（`/sdr`），与无线电控制面**零耦合**（没有 CAT/PTT 通路，也不返回任何电台状态）。四层：浏览器页面（瀑布/点选/解调/音频，复用既有 `opus_wasm` + `rx_worklet_processor`）→ `server.py` 的 `/WSsdr` 与独立口令角色 → `sdr_hub.py`（调谐仲裁 + 解调池 + 容量闸门 + 扇出，纯逻辑）→ `sdr_pipe.py`（独占设备的子进程，ctypes 调 `libsdrplay_api`，热路径调 `libsdr_dsp`）。**设备侧不走 Python 结构体镜像**：`sdr_api_shim.c` 用厂商头文件编译成扁平函数，IQ 落 C 的环形缓冲，回调不穿越语言边界。 |
| Alternatives | ①**跑 SDRconnect headless**（官方 WebSocket API）：实测 ARM64 包能跑、服务正常应答，但 `valid_devices` 只有 `"IQ File"` —— **SDRplay 官方明确 RSP1 不在 SDRconnect 支持列表**（硬件原因），否决；②**跑 SDR++ Server + 自写前端**：它的客户端是 SDR++ 桌面 GUI 的私有二进制协议（TCP 5259），浏览器端要重写协议 + 加 TCP↔WS 桥，且前端工作量一分不少，否决；③**OpenWebRX**：能白送解调与多人，但那是另一套系统（认证/云入口/打包都不通），且无法与电台联动，否决；④**Python 直接 ctypes 调厂商 API**：`DeviceParamsT`/`RxChannelParamsT` 嵌套十几层，镜像布局就是给偏移写错留门，改为 60 行 C shim；⑤**解调用 numpy**：实测每通道 0.075 核且做不出边带选择性，改 C（0.025–0.15 核，见 Consequences ②）。 |
| Consequences | ①**内核驱动必须先让路**：RSP1 一插上就被内核 `msi2500` 认领，而那条路径实测**是坏的**（调谐钳在 15 MHz、采样 99.7% 为 0）；`blacklist msi2500/msi001` + udev `0666` + `sdrplay.service`（root）是三个硬前提。②**单级滤波器做不到"既抗混叠又选边带"**：抽头按抗混叠定就选不出边带，按边带定就上万抽头；改成两级（fs_in → 96 kHz 中等滤波 → 48 kHz 尖锐复数滤波）。实测每通道 **0.086 核（AM）/ 0.151 核（USB）**，据此把 `MRRC_SDR_MAX_CHANNELS` 默认定为 **8**（上限 16）—— 更多通道需要换共享 FFT 信道化器（V2 路径，见 R10）。③**WFM 是第三条管线**：200 kHz 信道装不进 96 kHz 中间速率，必须先在 480 kHz 上鉴频再抽音频。④**调谐仲裁就是 I6 缺口的那个形状**（single-controller lock），但只作用在这个面上：30 s 持有锁、改中心后按**绝对频率**重算每个人的 offset、持有者断开立刻释放。⑤**平台范围收窄为盒子**：桌面包 `MRRC_SDR=off`，页面显示"SDR 仅盒子版支持"，不引入 PyInstaller/DMG 新路径。⑥**真机抓到的一个协议缺陷**：A1 帧的 seq 曾按 u8 打包（`<BBHH`），序号过 255 就把 pipe 打死、接收机变不可用——布局是 `chid u8 / seq u16 / level u8 / len u16`，编码/解析/服务端三处必须同一套 `<BHBH`。⑦**闭源二进制进镜像**：`libsdrplay_api` 与 root 守护进程随镜像分发（许可证原文随包），`MRRC_SDR=off` 可整块关闭。 |

## AD-030: 串口仪器（NanoVNA-H）的接入边界 —— 一次一件、发射中闭嘴、不抢 CAT 口

| Attribute | Value |
| ----------- | ------- |
| Type | Architecture / Operability |
| Status | Implemented (V3.02, 2026-10-18) — 测量 + 设备管理 + 固件升级（U3，默认上锁） |
| Decision | 把插在盒子 USB 上的 NanoVNA-H 当成**一台独立的串口仪器**接进来，页面是一个独立入口 `/Nano`：`nano/analysis.py` 是纯数学（Γ→SWR/Z/谐振/导出，无 I/O），`nano/vna.py` 是**唯一**碰串口的模块（端口发现 + `scan_bin` 二进制帧 + 帧长自校验），`server.py` 只做路由与四道守卫。数据只从 `scan_bin` 一条命令取（`u16 mask, u16 points`，随后每点 `u32 freq` + 每个启用数组各 `f32 re/im`），**不用 `data 0` 拼**。四道守卫：①发射中（PTT/TUNE/FT8/CQ）拒绝一切设备操作——VNA 的 CH0 就接在天线上，几十瓦射频进去是烧前端；②一次一件（`asyncio.Lock`，第二个人拿 409 而不是排队）；③端口不得是电台 CAT 口；④`listen`/`sdr` 会话 403。扫描后**无条件**把设备屏幕还原成进入时的扫频范围。 |
| Alternatives | ①**`data 0/1` + `frequencies` 的 ASCII 路径**（nanovna-saver 的做法）：401 点要 23.9 KB、两次往返，且 `sweep` 之后 `data` 会读到**上一轮**的数组——真机实测索引越大偏得越多（`data 0` 在 54 MHz 与 `scan_bin` 差 1.0），否决；②**在浏览器里算 SWR/阻抗**：同一个公式在两处各漂一次只是时间问题，改为服务端算好下发；③**裸 raw serial**：仓库已有 pyserial 依赖，且提示符同步需要 `in_waiting`，否决；④**让操作员拿电脑装 nanoVNA-QT**：那正是要被替代的现状（全段扫描 20 分钟且只有标量 SWR，见 `docs/current/antenna/`）。 |
| Consequences | ①**帧长必须精确匹配**（`4 + points × (4 + 8×(1+s21))`），不匹配就整帧丢弃重扫——半截数据会画出一条**看起来正常**的假曲线；②**发命令前必须与 `ch>` 同步**：直接 `reset_input_buffer()` 再写，会在设备还在吐上一条回复时把新行塞进去，实测表现是「`scan_bin` 一个字节都不回」；③**扫描会改设备屏幕**，所以每次扫描后要再发一次 `sweep`（+0.3 s / 101 点）——设备在用户进页面前后必须完全一致；④**破坏性命令与频率基准不进页面**（`tcxo` / `clearconfig` / `touchcal` / `sd_delete`），`tcxo` 一个手滑就是全段偏移且从网页看不出错在哪；⑤**升级走 STM32 ROM DFU**（`reset dfu` → `0483:df11` → `dfu-util -a 0 -s 0x08000000:leave`）：系统存储区不可擦写，所以刷坏只会停在 DFU、再刷即恢复，**不存在变砖**；但 MS/SI/ZK 是**编译期**开关、运行时任何命令都读不出来，所以变体必须人选 + 记忆，成功判据是**刷后设备自己报的版本**（不是 `dfu-util` 的返回码），并默认上锁（`MRRC_NANO_ALLOW_FLASH=1`）；⑥SDD 的 `cat-direct-serial-io` 约束管的是**电台 CAT**，`nano/vna.py` 是另一台仪器（不共享任何电台状态），但纪律照搬：`asyncio.Lock` 串行化 + `asyncio.to_thread` 卸载，从不阻塞事件循环。 |

## AD-032: 盒子级音频电平 —— 一个电平一个所有者，增益不碰"不是听众"的消费者

| Attribute | Value |
| ----------- | ------- |
| Type | Product / Signal chain |
| Status | Implemented (V3.06, 2026-10-26) |
| Decision | 把两条音频通路上"到底多响"这件事收归**盒子**，而不是浏览器，且两条通路各只有一个旋钮：`tx_mic_db`（−20…+25 dB）加在 `_feed_tx_from_uplink()`，**只作用于浏览器麦克风**（CQ 播放与 FT8 波形不经过它）；`rx_db`（−20…+30 dB）只加在 `encode_rx_audio()` 之前，即**只作用于下发给客户端的那份 PCM**。落地为 `audio_levels.py`（纯逻辑 + 原子 JSON 存储，stdlib + numpy），并带 `GET/POST /api/audio/levels`、`fullState.audioLevels` 与 `{"type":"audioLevels"}` 推送（**同一套键名**，前端只有一个解析器）。网页端的 🎙 Vol 上限由 2×（+6 dB）提到 +25 dB，滑块改 dB。 |
| Alternatives | ①**把 🎙 Vol 的天花板抬高就完事**：它仍是浏览器本地 cookie，一台设备一份——手机 +6 dB、电脑 0 dB，同一台电台两个声音，否决；②**一个"总 TX 增益"覆盖麦克风 + CQ + FT8**：CQ 是固定素材、FT8 是满幅波形，让"人声的旋钮"推动 FT8 的 ALC 是事故面，否决；③**RX 增益加在整条 PCM 上**：录音、FT8 解码、本地扬声器一起变响，听着像"统一"，实际是用削顶换响度——软件增益不能把电台没发出来的信息变出来，只会把解码链路顶坏，否决；④**盒子 RX 默认 +20 dB、把网页端 10× boost 收回服务端**：设计最干净，但安卓 / iOS / `/listen` / `/sdr` 各有自己的客户端增益且没有"收回"动作，等于四端必须同版本联发，推迟；⑤**走 ALSA 混音器（`amixer`/`alsactl`）**：设备相关（蓝牙设备根本没有混音器）、要固化、且对 TX 没有任何作用，否决。 |
| Consequences | ①**0 dB 是恒等，且是同一个 bytes 对象**：默认流与功能上线前逐字节相同（可 `assertIs`），这也是"其他四端不用动"的依据；②代价被写进 UI 而不是藏起来——盒子 RX 电平与浏览器 🔊 **相乘**，卡片上那句话说加完要把浏览器音量回调；③**录音录的是电台实际收到的信号**（TX 录音 tap 移到增益之后），且三种上行格式统一走这条路（此前只有 Opus 那支进 TX 录音、CQ 期间被丢弃的麦克风帧也会被录进去）；④**坏值拒绝而不是夹取**（`400` + 区间），唯一例外是手改的文件（夹取 + 警告）；⑤削顶按方向计数，单块比例 >1% 时每 30 s 最多一条警告——盒子自己造出来的失真必须由盒子说出来；⑥`listen`/`sdr` 会话沿用既有非 GET 403，UI 在只读角色下不存在。 |

## AD-033: 纯网络电台后端 —— 一个 UDP 设备引擎同时承载控制、IQ 频谱与 48 kHz 音频

| Attribute | Value |
| --- | --- |
| Type | Architectural / Signal chain |
| Status | Implemented offline (V3.21)；2026-11-08 已完成基础真机验收，但仍 `verified=False`、TX gated（SSB 主观音质 / S 表校准 / CQ·FT8 RF 未完成） |
| Decision | 新增 `backends/sunsdr/`：`protocol.py` 只编解码抓包验证过的 SunSDR2 DX UDP 帧；`engine.py` 独占控制 50001 与 IQ 50002，统一启动、心跳、恢复、遥测、RX/TX 队列和 PTT；`dsp.py` 在 IQ 与 48 kHz PCM 间转换；`backend.py`、`audio.py`、`scope.py` 分别适配 `RadioBackend`、服务器音频循环与 `ScopeHandler`。`RadioBackend.create_audio_handler()` 默认返回 None，只有网络音频机型替换 PyAudio，既有串口/USB 机型不变。设置元数据由 `model_connection()` 给出 `serial | network`，网络机型配置主机而非串口。 |
| Alternatives | 桥接旧 SunMRRC 会留下第二套认证、PTT 与媒体状态机，否决；只接控制并外接声卡不能提供原生网络 RX/TX，否决；频谱和音频各自打开 UDP 会争端口且重复 boot，否决。 |
| Consequences | ① 浏览器/手机协议不变：48 kHz Opus/PCM 与 850-bin 频谱继续复用，录音、FT8、CQ、本地扬声器走原 tap；② 引擎在连接时启动，频谱客户端数量不决定 RX/FT8 的存活；③ UDP 主机启动时解析一次，两个端口绑定成功才 boot，控制写与 paced boot 串行化；④ PTT 释放不等待回读，发送失败有有界、代数保护的 retry，停机还有最后一次同步释放；⑤ 不伪造设备没有的 SWR/ALC/ATU，AM/FM/CW TX 未验证不开放；⑥ **2026-11-08 参考实现移植**：850-bin 频谱在 engine 内按 +IF 旋转到 VFO 居中（否则频率轴偏 30.5 kHz）；PTT ON 2 s 未确认重发一次；TX 起始 200-sample 斜坡、欠载重灌注 8 包；USB/LSB/DIGI 可发射，CW/AM/NFM/FM 显式拒绝并在采样率切换后保持边带；`DUCC_NUM_THREADS=1` 规避 macOS scipy fork 崩溃；⑦ 设计与验收见 `docs/superpowers/specs/2026-11-08-sunsdr2dx-backend-design.md`、`docs/SUNSDR2DX_USER_GUIDE.md`、`docs/sunsdr2dx-protocol.md` §9。 |

## AD-035: SCU-LAN10 网络传输 —— 换传输层，不换电台家族（CAT/频谱/音频走客户已买的 LAN 单元）

| Attribute | Value |
| --- | --- |
| Type | Architectural / Signal chain |
| Status | Implemented (2026-11-15)；**2026-11-16 首次真机（FTDX101MP + SCU-LAN10，BG7ZHS）**：CAT/频谱/RX 音频已在真机跑通（修掉两处静默缺陷 + 一处启动死锁，见下 Amended）；**TX 仍按设计拒绝**（668 B TX 帧内层头未解），频谱频率轴的结构标定尚未闭合（设计记录 §15.4）。设置页有「SCU-LAN10 网络接入」卡片（`GET/POST /api/box/scu-lan`、`POST /api/box/scu-lan/test` 只握手不落盘，盒子正在使用时 409）。见 `docs/superpowers/specs/2026-11-15-scu-lan-backend-design.md` §13–§15 |
| Decision | FTDX101MP/D、FTDX10、FT-710 的瀑布只有两条来源：背后 13-pin ACC 的 SPI（要自己做转接板，每台一份硬件）或客户已经买下的 SCU-LAN10（一根网线）。选后者。新增 `backends/yaesu/scu_lan/`：`protocol.py` 只做线缆层（`5A5A` 组帧、8 字节头、gTable XOR、四通道/消息类型、auth 与会话负载、序号计数器，无 socket 无状态）；`client.py` 是七阶段会话（5 条硬编码初始化包 → auth → session id → continuation → CAT/AUDIO/SCOPE 三组 setup → 四通道心跳）+ 1 Hz 级心跳 + CAT 发两遍 + 断线重建；`transport.py` 把 CAT 通道包装成**串口外观**，`YaesuCatController` 一行不改跑在 UDP 上；`scope.py`（`ScopeProducer`）与 `audio.py`（16 kHz 立体声 → 48 kHz 单声道）分别接服务器既有的瀑布与音频循环。`MRRC_SCU_LAN=1` + `_HOST`/`_USER`/`_PASS` 一个开关同时点亮四个 Yaesu 机型。电台仍是同一台电台：profile 表、滤波器、表头、PTT 规则全部复用 —— 变的是"怎么跟它说话"。 |
| Alternatives | ①**做 ACC 转接板**（ESP32/STM32 复刻 LAN 单元的角色）：代码更少，但每台机器一份硬件、要动电烙铁，且 ACC Pin8 有 13.8 V / 3.15 A 一次性保险丝，否决（留作无 SCU-LAN10 客户的后续路线）；②**让盒子读电台 USB 的 FT4222**：FTDX10/FTDX101 的 USB 侧没有该接口（FT-710 才有，且要菜单 `SCU-LAN10=ON`），而 USB 口本来就归 LAN 单元所有，否决；③**新开一个电台家族 `ScuLanBackend`**：频谱与音频来源确实换了，但 CAT 命令集、频段表、滤波器、表头、PTT 全是 profile 的事——另起家族等于把这些抄一遍，否决；④**只做 CAT 不做音频/频谱**：客户要的正是瀑布与声音，否决。 |
| Consequences | ①**镜像方向与前提**：盒子是客户端，主动向单位的 50000–50003 发 UDP，单位按"上次收到包的源地址"回包 ⇒ 只要盒子出向可达，**不需要端口映射、不需要入向规则**；客户端只绑 IPv4（单位不支持 IPv6）。装机前提三条：电台菜单 **CAT RATE=38400**、CAT TIMEOUT 10 ms、CAT RTS ON。②**接线互斥**：单位占用电台的 USB 口，所以"盒子插 USB"与"盒子走网络"不能同时存在，同一台电台只有一个 USB 口。③**带宽 1.4 Mbit/s**（频谱 822 kbit/s + 音频 542 kbit/s），盒子侧走 Wi-Fi（MT7663S 1×1 2.4 GHz）时这一段是最弱环节，建议网线。④**CAT 两遍必须换线缆头序号**、**post-auth CTRL 序号从 07 继续**：单位按"见过就丢"处理包，字节相同的重发与序号重开都会被静默丢弃（现场症状分别是"偶发丢命令"与"setup 被忽略"）——两处都由上游规范写明，并由测试钉住。⑤**ack 不是数据**：`0x86` port-init ack 也落在数据端口上，按通道分流会把 4 个字节画成一条频谱行，必须按消息类型分流。⑥**会话独占且无恢复**：雅司 FT-Control 一连上我们这条就停，掉线必须走完整七阶段；掉心跳数秒即终止，故心跳取 2 Hz。⑦**TX 关闭**：668 字节 TX 音频帧的内层头尚未解出，按键只会得到静音载波，`_tx_allowed()` 在网络模式拒绝 key-up（释放永不拦），解出后翻一个 flag。⑧**异常语义按 `_is_device_fatal` 对齐**：会话断开是**可自愈**的瞬时故障，传输层抛 `SerialTimeoutException`，只有对象本身不可用才抛 `SerialException`——否则 UI 会把一次 LAN 抖动报成"USB 被拔了"。 |

**Amended (2026-11-16, 真机 FTDX101MP + SCU-LAN10, BG7ZHS — 设计记录 §13–§15)**: 首次真机暴露三处「静默」缺陷并已修复。① **序号计数器属于端口，不属于 `b2` 里的通道号**——`b2` 指正在被配置的**下一个**通道（规范原文的 quirk），两条走 CTRL 的 setup 包却按 CAT/AUDIO/SCOPE 取号，三个 port-init 全以 `seq 2` 发出（应为 `1`）被单位静默丢弃；CTRL 上的 auth/keepalive ack 让链路一直「活着」，现场是**已连接、`reconnects: 0`，而 `cat_frames`/`scope_packets`/`audio_packets` 全 0、瀑布 6 分钟只发 1 帧**（`_sequence` 现按 `ctrl`/`cat`/`audio`/`scope` 计数）。② **单位发出的数据置方向位 `0x80`**：数据是 `MSG_ACK_DATA`(0x87) 不是客户端的 `MSG_DATA`(0x07)，`_dispatch` 的 `!= MSG_DATA` 判定把每条 CAT 回帧/音频体/扫描都送进 `_on_ack` 丢掉，而不认识的 ack 只写 DEBUG（判定改为 `not in (MSG_DATA, MSG_ACK_DATA)`）。③ **启动期死锁**（§14）：lifespan 在事件循环上阻塞等 `start_rx`，而 handler 又 `run_coroutine_threadsafe(...).result()` 等那个被堵住的循环 → 8 s 超时 → RX 循环根本没建；修法为 handler 永不等循环、`start()` 加 `_starting` 闩、服务端改 `await asyncio.wait_for(wrap_future(...))`。**真机实测**：`ID0682;`；CAT 往返中位 **9 ms**；`SS0560000;`（span 码 6 = 200 kHz）、`SS0650000;`（P3=5 = 居中）；频谱 **25.0/s**、边界标记 **bin 1700 → 850 bin**（`marker // 2` 规则在 FTDX10 之外成立）；音频 **100.0/s**、640 B/包 = 160 立体声帧 = 16 kHz 10 ms。**仍未闭合**：TX 音频帧内层头（key-up 依旧拒绝、释放永不拦）；频谱频率轴的结构标定（§15.4：刻度像 850 bin、结构证据指向 610 bin，需一次屏幕对照定案，在此之前不改轴）。守卫：`tests/test_scu_lan_client.py` 假单元按硬件发 `0x87` 并逐端口校验序号；`StartupDeadlockTests` 盯住死锁三修法。

## AD-036: 天线选择属于 panadapter/SCU，不属于电台、也不单独属于 slice

| Attribute | Value |
| --- | --- |
| Type | Architectural / Radio semantics |
| Status | Implemented（1.28.70 · FP57；S 表按 slice 修正 1.28.71 · FP58）。协议依据 `docs/flexradio-antenna-protocol.md`，实现设计 `docs/superpowers/specs/2026-11-16-flex-antenna-selection-design.md` |
| Decision | 天线选择按**电台自己的命名**做，作用域跟电台一致：WS `{"type":"set","field":"antenna","value":"ANT1"}` → `set_antenna_name()` → `slice set <own_slice> rxant=<name>`；候选来自 `slice` 状态里的 `ant_list`，进 `RadioState.antenna_list`（`antenna_name` 是当前值），**不做静态能力表**。同时修掉两个让「切了没反应」的根因：`s_meter_dbm()` 改按 `own_slice` 取 `SLC/LEVEL`（否则读的是第一条 slice，别人的表）；新增 `ensure_slice()`（带 handle → 自己 pan 上 `active=1` → 自己 pan 上任意一条并激活 → `slice create pan=`），并让 `ensure_pan()` 在 `create` 被拒（`0x50000009`）时只认带我们 handle 的 pan、否则**点名失败**而不是把错误回执当 pan id。 |
| Alternatives | ①**沿用已有的 `set_antenna(int)`（1/2/3）**：串口后端是 `AN1/AN2` 的整数语义，但 6600 报的是六个名字（`ANT1,ANT2,RX_A,RX_B,XVTA,XVTB`），整数既表达不了也猜不出顺序 → 保留整数入口兼容旧客户端，权威走名字；②**把候选写进 `RadioCapabilities`**：候选是连上之后才由电台报的、随变频器配置变化，静态表只会撒谎（本机 `ant_list` 与 `tx_ant_list` 长度就不同），故放 `RadioState`；③**同时写 `txant`**：TX 在 AD-019 门禁后未验，一次只动一件事；④**直接改 pan 上的 `rxant`**：`display pan set <pan> rxant=` 回 0 但语义未证（`ant=` 在 pan 上反而报 `0x5000002D`），不赌。 |
| Consequences | ①**一个命令改整块 pan**：手册原文「Changing the receive antenna for a single slice will change the receive antenna for its host Panadapter and all Slices within」——所以一个 pan 上有多条 slice 时**不要逐条去纠正**，会互相打架。同一个界面上它看起来像"这条 slice 的天线"，实际是这块 panadapter（=一个 SCU）的。②**`ant_list` 必须实时读**：6400/6500 只有一路 SCU、配了变频器才多出 XVTR 口，硬编码必然在某个型号上撒谎。③**`ant=` 是假键，而 `slice s` 更糟**：`ant=` 在 slice 上没有这个键（在 pan 上才报 `0x5000002D`）；而 1.28.69–1.28.76 的实现发的是 **`slice s <n> …`（少了 `set`）** —— SmartSDR 对**对象名不认识**的命令一律「接受、不报错、不执行」，于是模式、滤波、音频电平、RF Gain、AGC-T、NB/NR/ANF、`rxant`/`txant` **全都没有到达电台**（1.28.77 · FP64 修正）。教训：**「没报错」不等于「生效了」，必须回读状态**。④**S 表必须按 slice 取**：`meter N.src=SLC#num=<slice>#nam=LEVEL`，一台四 slice 的机器上有四组，取第一条等于显示别人的信号强度（现场：−109.7 / −112.8 / −110.4 / −109.9）。⑤**没有自己的 slice 就没有一切**：引擎从不建 slice 时 `_slice_id()` 回退 `min(slices)`，调频与天线都打在别人身上——这条同时也是"盒子显示瀑布但没有信号"的成因。⑥**`listen` 角色不得改天线**：这是射频路径，服务端沿用现有 set 白名单拒绝。⑦**UI 是循环键不是下拉框**：候选数量随型号变（本机 6 个），手机上循环点比下拉更稳；`antenna_list` 为空时整个键不出现（FT-710 这类看不到）。 |

## AD-037: FlexRadio 的对象归属 —— 频谱能不能用，由「谁拥有什么」决定

| Attribute | Value |
| --- | --- |
| Type | Architectural / Signal chain |
| Status | Implemented（1.28.77 · FP64，**真机验证**）。协议依据与全部实测数据：`docs/flexradio-architecture.md`；天线部分见 AD-036 |
| Decision | 把 FlexRadio 当成「**电台拥有对象、客户端只是订阅者之一**」来写，而不是像串口电台那样当成一组寄存器：①`_update_pan()` 只接受**自己 pan id** 的状态（`sub pan all` 会推所有客户端的状态）；②显示挂在**我们的 slice 所在的**那块 pan 上（`_slice_pan()` / `ensure_pan()` / `ensure_slice()`，必要时把显示搬过去）；③像素→dBm 用**回读的**电台孔径（`min_dbm`/`max_dbm`），字节域恒 0..255，超量程桶钳位并计数；④横轴起点/带宽以 pan 的实际值为准（`scope_axis_from_pan`，能力位下发）；⑤界面上的天线选择记进 `MRRC_FLEX_RX_ANT` 并在每次连上后重放（电台不保留 `rxant`）；⑥保存设置一律写进 systemd 真正读的 env 文件。 |
| Alternatives | ①**继续按「一块 pan = 我们 create 出来的那块」写**：正是 2026-11-16 全天「声音有、频谱没信号」的来源 —— create 在 pan 槽位满时会给新 id，而 slice 还留在上一轮那块，于是显示的是无射频的空 pan；②**只过滤 FFT 的 stream id（FP55 的做法）**：数据确实过滤了，但**坐标与标定**仍取自别人的 pan（实测 3 ms 内在 −140/−50 与 −135/−40 之间来回跳），画面因此错位；③**把孔径写死成我们请求的值**：电台会夹（请求 −140/−50、实际 −135/−40），整幅瀑布就带一个未知 dB 偏移，这也是此前三次"调亮度窗口还是黑"的原因；④**用 `y_pixels` 当像素量程**：本机回报 100 而 FFT 字节跑到 228，整帧会被压成 0（我们自己也踩过一次）。 |
| Consequences | ①**没有 slice 的 pan 照样送 FFT**：换 ANT1→ANT2→RX_A→RX_B 对那块 pan 的影响 ≤ 0.05 dB，而切到 slice 所在的 pan 后同一副天线给出 22 dB 的动态范围 —— 所以"瀑布有没有信号"首先要问"这块 pan 上有没有我的 slice"；②**`slice set <n>`，不是 `slice s <n>`**：后者被静默接受且不执行（模式/滤波/AGC-T/天线全部失效，见 AD-036③）；③**S 表可以校准频谱**：`S 表 − 10·log10(BW_demod/BW_bin)` 应与每桶噪底一致（实测 −122.5 vs −122.7 dBm），这是判断频谱数据真伪最快的手段，不需要信号源；④**天线会被电台复位**：每次客户端会话恢复 `rxant` 到默认口，界面选择必须自己记住（`_restore_antenna()`）；⑤**多客户端是常态**：`licensed_clients=2`，同机 SmartSDR 的 pan/slice 状态与我们混流到达，任何"最后写入者胜"的全局变量都是缺陷。 |


## AD-038: ESP32-C3 PCM16 单模音频档位

| Attribute | Value |
| --- | --- |
| Type | Architectural / Audio transport |
| Status | Implemented offline；ESP32 与真机验收单独记录 |
| Decision | 主语音总线继续维持 48 kHz。新增 per-audio-socket `audioCaps` 协商和 `0x02 = 16 kHz Int16 mono PCM`；未声明 socket 的 Opus48/PCM48 行为不变。RX 对 PCM16 组每拍共享一次 soxr LQ 48→16 转换，PCM16-only 不编码 Opus。TX 全链路显式携带 `input_rate`，由后端直接转到设备率；PCM16 转换失败强制 RX。 |
| Alternatives | 全局 16 kHz 会无谓改动全部客户端；在 `/WSradio` 会话上协商会混淆同 token 的多个音频 socket；复用 `0x00` 会让帧失去自描述性；QQ 降采样实测不抑制 12 kHz 混叠，禁用。 |
| Consequences | ESP32 PCM16 单模固件可移除 libopus，双向各 256 kbps；旧服务端不应答时固件必须报错，TX 未确认不得 key-up。PTT owner、队列水位与释放路径不变。 |

**Problem**: ESP32-C3 客户端的资源预算装不下 libopus 与 48 kHz 音频缓存，而最初「服务端/浏览器/手机全部改 16 kHz」的全局方案会无谓改动所有后端与现有客户端。实际目标只是让嵌入式客户端以 16 kHz Int16 单声道 20 ms PCM 双向通信（每方向 256 kbps，典型帧 `1 字节标签 + 640 字节 PCM` = 641 B），其余客户端一个字节都不变。设计权威：`docs/superpowers/specs/2026-11-17-voice-16k-design.md`。

**Rationale**: 复用 AD-025 的兼容原则——能力按 **socket** 协商、兼容性由服务端闸门保证而不是由客户端升级保证。`audio_profile.py`（纯标准库、零应用 import，与 `spectrum_profile.py` 同形）持有协议常量与 `parse_caps()`/`accepted_caps()`/`rejected_caps()`；`/WSaudioRX` 与 `/WSaudioTX` 各自保存 `_audio_rx_profiles`/`_audio_tx_profiles`（键是 WebSocket 对象，不是 token——一个 30 天会话可以同时持有两种 profile 的音频 socket），断开即清理。未发送 caps、JSON 无效或 profile 未知的 socket 维持 legacy，逐字节等于 AD-038 之前的流。RX fan-out 按 profile 分两组：legacy 组继续 Opus48（libopus 缺失时 PCM48），PCM16 组共享一条 `Pcm16RxEncoder`（`audio_resample.py`，soxr `ResampleStream` LQ 48→16），每拍最多转换一次、只发完整 641 B 帧；只有 PCM16 听众时完全跳过 Opus 编码，录音/FT8/本地扬声器 tap 不受影响。最后一个 PCM16 socket 离开后转换器 reset 并丢弃，新订阅不继承旧滤波尾帧。TX 侧 `_feed_tx_from_uplink(pcm, input_rate=48000)` → `_rec_tap_tx(pcm, rate=input_rate)` → `audio.feed_tx_audio(pcm, input_rate=…)`：默认 48000 的 legacy 路径（Opus 解码、PCM48、无标签帧、机内麦克风、CQ、FT8）逐字节不变，只有 `0x02` 分支传 16000；PyAudio handler 对非默认率用按 `(input_rate, device_rate)` 缓存的流式 SRC 直达设备率（FT-710 16→44.1 kHz 一步，不经中间 48 kHz），网络适配器各自实现或透传（Flex 16→24 kHz、SunSDR 16→48 kHz、Icom LAN/SCU-LAN 委托声卡）。

**Consequences（实现事实，离线验证）**:

- **LQ 是强制的，不是偏好**：2026-11-17 实测 48→16 kHz 的 12 kHz 满幅正弦，soxr QQ 输出 RMS ≈ 0.707（归一化；未抑制，混叠进带内），LQ ≈ 0.000366。`StreamingPcmResampler` 固定 `quality="LQ"`，有状态跨块、保留奇数字节尾样本，`reset()` 重建流。`Pcm16RxEncoder.push()` 返回**变长**帧列表（预卷积期为空、追赶期一拍多帧），调用方绝不能把列表长度当 tick 数。
- **TX 协商有真实闸门**：`_pcm16_tx_supported()` 检查 `tx_audio_available` 与 soxr 探针（按真实设备率构造一支 `StreamingPcmResampler(16000, device_rate)`），失败回 `{"accepted":false,"reason":"pcm16-tx-unsupported"}`。SCU-LAN10 网络模式 TX 本来就关（AD-035），因此永不应答成功。固件在收到 TX 成功应答前不得发麦克风帧、不得 key-up。
- **转换失败 = 强制 RX，绝不静默丢帧**：`0x02` 上行帧的任何异常（坏率 `ValueError`、soxr 失效、handler 会话中途消失）都走 `_force_rx_for_audio_error()`——单次 `set_ptt(False)` + 清 `_ptt_key_ws` + TX 表归零；socket **不关闭**（与 RX 侧 4004 刻意不对称：一次坏帧是抖动，ESP32 每掉一包就重新协商不可用）。该 helper 不读任何 per-socket profile 表——释放永远与协商状态无关（ch15）。RX 侧转换器失败则以 4004 `PCM16 resampler unavailable` 只关 PCM16 组，legacy 组同一拍继续收 Opus。
- **`_tx_session_decode_fail` 的口径扩大**：现在计「owner 帧没能交给设备队列」——Opus 解不出**或 PCM16 转换抛错**都算；handler 消失既不是喂过也不是解码失败（计 0，证据是 force-RX warning 行）。
- **`written` 的单位随后端而异，读数时必须知道**：PyAudio handler 与 SunSDR 适配器按**设备域 20 ms 帧**计（SunSDR 为 48 kHz/960 样本帧，`max(1, len(converted)//1920)`，PCM16 与 legacy 同一定义）；Flex 适配器按**发出的 Opus/VITA 包**计（24 kHz 立体声 10 ms 包，一秒语音 ≈ 100 包而不是 50 帧）。同一份 `TX session: … written=…` 日志在两类后端上不可直接互比。
- **启动诊断的已知局限**：`RX audio broadcast active: <tag>` 行的 tag 名取自 `audio.opus_enabled`（Opus/Int16 PCM），描述的是 legacy 编码器；PCM16-only 的盒子没有 Opus 帧，这行仍写 "Opus"，peak 测量本身取原生块、内容无害。按组分型留待后续（Task 4 遗留，未在本文档任务中改代码）。
- **带宽与部署**：PCM16 每方向 256 kbps，高于 Opus48 的 ~64 kbps；公网/Cloud Hub 路径需实测 Wi-Fi、上行与隧道余量（spec §8）。soxr 已是 `requirements.txt` 既有依赖（FT8 RX tap 引入），PyInstaller spec 已列 hiddenimport；缺失时 RX 组 4004、TX 协商拒绝，legacy 不受影响。
- **未验证边界**：全部离线（fake radio/fake audio + 真 soxr/真 Opus/真 VITA 组帧）。ESP32 固件 map/RAM/Flash 对比、FT-710 44.1 kHz 与 IC-7300/MK2 48 kHz 真机 TX 边界、混合客户端共存、旧服务端拒绝行为，归 Task 10 验收；Flex/SunSDR 的 PCM16 通路同样未上真机（AD-019 门禁仍在）。
