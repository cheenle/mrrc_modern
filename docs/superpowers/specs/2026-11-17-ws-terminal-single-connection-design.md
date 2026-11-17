# /WSterminal 单连接复用 —— 实现设计

日期：2026-11-17
状态：已获用户批准（A：参考页功能边界；B：不加菜单入口），待实施计划
需求来源：`mrrc-passport/docs/design-v1.md` §9.2（V2 项）
相关：`docs/superpowers/specs/2026-11-13-esp32-handheld-terminal-design.md`（客户端侧契约）、
`docs/superpowers/specs/2026-11-17-voice-16k-design.md`（AD-038 音频档位）、
AD-001（所有服务端逻辑在 `server.py`）、AD-024（token 传输）、AD-025（频谱档位）

## 1. 背景

`mrrc-passport` 的实测画像（`design-v1.md` §13）：稳态可用堆 31–33 KB、最大连续块
9–10 KB、历史最低 5 KB；PTT 期间 15 KB / 7 KB。每条 TLS 会话实测约 16 KB
（`SSL_IN 6144 / OUT 2048`）。固件为了给发射腾地方，必须在按键时**拆掉下行
socket、重新握手一条上行 socket、再等 200 ms settle**（`main/app_main.c:156-176`、
`main/mrrc_tx.c`）——这同时是内存峰值、首个音节丢失和 TX owner 竞态的根源。

服务端目前是四条独立端点：

| 端点 | 载荷 | 现状 |
| --- | --- | --- |
| `/WSradio` | 文本 JSON（`_handle_ws_message`） | 控制面，`ctrl_clients` |
| `/WSaudioRX` | 二进制 `tag + payload` | 下行，`audio_rx_clients` |
| `/WSaudioTX` | 二进制 `tag + payload` | 上行，`audio_tx_clients` + owner 仲裁 |
| `/WSspectrum` | 二进制整帧（AD-025 档位） | 瀑布，`spectrum_clients` |

本设计增加**第五条**端点 `/WSterminal`：一条 WebSocket 同时承载以上四条的内容，
外加一个自包含参考页 `/terminal`。旧四条端点**一行不改**，浏览器 / iOS / Android
不受任何影响。

与 `2026-11-13-esp32-handheld-terminal-design.md` 的关系：那份文档写着「不动服务端
协议（唯一例外是 RX 码率一个环境变量）」，那是当时为了不扩大范围。本文是**唯一
被批准的例外**，并且是纯增量——没有它，任何客户端都只能二选一：省内存（少连一条）
或功能完整（四条都连）。

## 2. 目标与非目标

**目标**

1. 一条已认证的 WebSocket 上完成：控制、RX 音频、TX 音频、频谱。
2. 服务端能力与 `/WSradio` 对齐（它就是复用 `_handle_ws_message`），不是子集。
3. 客户端音频/频谱**拥塞时丢帧，不丢 socket**——音频不能把控制信道和 PTT 安全链一起带走。
4. 有一个可跑的参考客户端（`/terminal`），能真实听、真实发、真实看瀑布。
5. 旧端点与旧客户端行为逐字节不变。

**非目标**

- 不改浏览器主界面（`ft710_main.js` / `ft710_ui.js`，这两个文件有护栏）。
- 不改 iOS / Android 客户端。
- 不改 ESP32 固件（另一条线，另一仓）。
- 不引入第二套状态机、第二套帧编解码、客户端版本协商。
- 不新造音频/频谱档位机制：直接复用 AD-025 的 `spectrumCaps` 与 AD-038 的 `audioCaps`。
- 不做 `/WSterminal` 的 SDR / listen 专用变体。

## 3. 线协议

### 3.1 认证

与其余端点同一个解析顺序（`_token_from_ws_handshake()`，AD-024）：
`Authorization: Bearer` → `mrrc_auth` cookie → 已废弃的 `?token=`。
只接受 `_auth_tokens`；**不接受** `/WSsdr` 的 `mrrc_sdr` cookie。

### 3.2 文本帧（双向）

出站与入站都是 `/WSradio` 的 JSON 协议原样，`_handle_ws_message(ws, text)` 直接吃：
`fullState` / `stateUpdate` / `seterror` / `pong` / `tx_status` / `ptt_keyed_by_other` 等
全部照旧，**没有任何消息需要客户端学两遍**。

本端点唯一专有的入站消息（其余端点不认识、也不需要认识）：

```json
{"type":"spectrumCaps","profile":"high|mid|low"}
```

收到即：校验（`spectrum_profile.parse_caps()`）、记为本 socket 档位、**把 socket 加入
`spectrum_clients` 并启动 scope 产线**。不发这条消息的客户端不产生任何频谱开销。

### 3.3 二进制帧

第 1 字节是通道号，其余字节与旧端点的载荷**逐字节相同**：

| chan | 方向 | 载荷 = 旧端点载荷 |
| --- | --- | --- |
| `0x00` | S→C | `/WSaudioRX`：`tag + payload`（`0x00` PCM48 / `0x01` Opus48 / AD-038 之后还有 `0x02` PCM16） |
| `0x01` | C→S | `/WSaudioTX`：`tag + payload` |
| `0x02` | S→C | `/WSspectrum`：整帧（`0x01` + wf1 [+ wf2]，按本 socket 档位） |
| `0x03`–`0xFF` | — | 保留；收到即丢弃并计数 |

选择「通道字节 + 旧载荷」而不是「通道 + tag + 载荷」，是因为前者让载荷保持与
今天完全一致：已会解析 `/WSaudioRX` 的客户端只需剥一个字节，服务端只需在扇出处
加一个前缀，不产生第二套编解码。

### 3.4 不做的事

- 不回发 `spectrumCaps` 应答（`/WSspectrum` 今天也不回），客户端用"收到 0x02 帧"自证。
- 不新增心跳：沿用 `{"type":"ping"}` / `txhb`。
- 不为单连接新增加密或压缩层。

## 4. 服务端设计

全部改动在 `server.py`（AD-001：服务端逻辑集中在一个模块；跨模块只会制造双向依赖）。
新增约 200–260 行，集中在四条既有 WS handler 之后。

### 4.1 状态

```python
_terminal_ws: set[WebSocket] = set()      # 单连接终端（用于扇出分支判别）
_terminal_drops = {"audio": 0, "spectrum": 0, "tx": 0, "unknown": 0}
```

终端 socket 同时进入**既有的四张表**：`ctrl_clients`、`audio_rx_clients`、
`audio_tx_clients`、`spectrum_clients`（后者仅在收到 `spectrumCaps` 后）。
不新建并行数据结构——`_broadcast_state`、`_send_audio_frames_to_clients`、
`_spectrum_fanout`、`scheduler.set_active`、`_broadcast_client_count`、
`_claim_tx_owner_for_token` 全部原样复用。

### 4.2 连接建立（顺序有意义）

```
1. token = _token_from_ws_handshake(ws); 不在 _auth_tokens → close(4001)
2. accept
3. ctrl_clients.add / _ws_tokens[ws] = token / metrics.open(role, "terminal")
4. audio_rx_clients.add / _terminal_ws.add
5. role == operator 时：
      audio_tx_clients.add / _register_tx_socket(ws) /
      _assign_tx_owner_on_connect(ws, token)      # 已有 uplink 主人时不抢
6. scheduler.set_active(len(ctrl_clients) > 0) / _broadcast_client_count()
7. 发 fullState：_full_state_message(radio.to_dict(), _load_mem_channels())
```

第 5 步不能省：`_claim_tx_owner_for_token()` 按 token 扫 `audio_tx_clients`，
没注册就扫不到，结果是「电台 key 了但没有调制」——这正是
`mrrc_tx.c` 里那段 200 ms settle 与「先连后按键」契约存在的原因。

### 4.3 接收循环

```
msg = await ws.receive()
├─ "bytes"：chan = data[0]
│    ├─ 0x01 → 与 /WSaudioTX 完全相同的分支：
│    │        owner 判定（_tx_owner_ws）→ tag 派发 → _feed_tx_from_uplink()
│    │        listen 角色直接拒绝（见 4.6）
│    └─ 其它 → _terminal_drops["unknown"] += 1
└─ "text"：
     ├─ {"type":"spectrumCaps"} → parse_caps → 记档位 → 加入 spectrum_clients
     │                            → _scope_producer.start()（幂等）
     ├─ {"type":"audioCaps"} → _terminal_audio_caps（2026-11-17 修订，见 §9）：
     │        parse_terminal_caps 只认 pcm48/pcm16；一次声明登记 RX+TX 两个
     │        方向、只回一次应答；listen socket 只登记 RX
     └─ 其它 → _handle_ws_message(ws, text)
```

`_handle_ws_message` 内部的所有闸门自动生效：listen 角色白名单、
`_ptt_key_ws` 控制面仲裁、`txhb` 能力声明、unverified-tx 拒发、CQ/FT8 互斥、
断电守卫。这就是「能力与 `/WSradio` 对齐」的全部含义——没有第二套实现。

### 4.4 下行扇出（本改动唯一有风险的地方）

两处热路径各加三条逻辑：

`_send_audio_frames_to_clients()`（`server.py:2251`，`AUDIO_RX_SEND_TIMEOUT = 0.012`）：

1. 终端 socket 的载荷前加 `b"\x00"`；
2. 超时 / 异常时：终端 socket **不加入 `dead`**，只 `_terminal_drops["audio"] += 1`；
3. 非终端 socket 行为不变。

`_spectrum_fanout()`（`server.py:2226`）：同样加 `b"\x02"` 前缀、同样丢帧不杀 socket。

理由：这个超时函数原本带着「一个慢客户端只死自己」的语义，对纯音频 socket 是对的；
对复用 socket，它等于「音频堵 12 ms → 控制信道与 PTT 安全链一起断」。`/WSsdr`
（`server.py:6796`）已经用有界队列证明过：媒体拥塞的代价应该是丢帧。

### 4.5 断开清理（一个 `finally`，顺序固定）

| 顺序 | 动作 |
| --- | --- |
| 1 | `ctrl_clients.discard` / `_ws_tokens.pop` / `_tx_hb_capable.discard` / `_tx_hb_last.pop` / `metrics.close` |
| 2 | `audio_rx_clients.discard` / `_terminal_ws.discard` |
| 3 | 曾是 `_tx_owner_ws` → `None` + 强制 RX + `audio.stop_tx` |
| 4 | `audio_tx_clients.discard` / `_forget_tx_socket` / `_promote_tx_owner` |
| 5 | `spectrum_clients.discard` / `_spectrum_profiles.pop` / `_listen_spectrum_clients.discard`；空则停 scope |
| 6 | 曾是 `_ptt_key_ws` → 强制 RX |
| 7 | `scheduler.set_active(...)` / `_broadcast_client_count()` / `_cq_abort_if_client_gone` |

在终端 socket 上 `_ptt_key_ws` 与 `_tx_owner_ws` **是同一个对象**，第 3 和第 6 步会命中
同一次 `set_ptt(False)`；两处都写幂等，但实现里用一次性标志避免两条 WARNING 重复刷。
「强制 RX」永不因为释放而阻塞——沿用 V1.2 的 TX0 即发即忘纪律。

### 4.6 角色

| token 类型 | 控制 | RX 音频 | 频谱 | TX 上行 |
| --- | --- | --- | --- | --- |
| operator | 全部（`_handle_ws_message`） | ✅ | ✅ | ✅ |
| listen（`_listen_tokens`） | 仅 freq/mode/memRecall（现成闸门） | ✅ | ✅ | ❌ 拒绝 + 计数 + 回一条 `error` |
| sdr（`_sdr_tokens`） | ❌ 不接受 | ❌ | ❌ | ❌ |

listen 会话**不注册** `audio_tx_clients`，因此在第 4.2 步就不存在 uplink。

### 4.7 计量

- `session_metrics.KINDS` 增加 `"terminal"`；一个终端 socket 只 `open`/`close` 一次，
  不会把「一个会话几条 socket」的计数放大四倍。
- `METERED_KINDS` **不变**：终端产生的音频/频谱字节仍记进 `audio_rx` / `spectrum`，
  因为那就是这两条路真实的出流量，hub 的容量判断要看的是字节，不是 socket 种类。

### 4.8 中间件

`WS_PATHS`（`server.py:4928`）必须加入 `"/WSterminal"`。否则 WS 升级会被
`auth_middleware` 302 到 `/login`，而 WebSocket 跟不了重定向——这个坑该文件注释里
已经写过一次。

### 4.9 不拆新模块的理由

端点需要 `ctrl_clients`、`radio`、`_ptt_key_ws`、`_feed_tx_from_uplink`、`_scope_producer`
等十余个模块级对象。拆出去只能靠反向导入 `server`，与 AD-001 相悖。纯逻辑（帧拆分、
通道合法性、丢帧策略判定）抽成模块级小函数，便于无硬件单测。

## 5. 参考页 `/terminal`

### 5.1 入口策略（已定：不加菜单）

- 路由 `GET /terminal` → `_html_page(STATIC_DIR / "terminal.html")`。
- `_terminal_forbidden(request)`：listen / sdr 会话 → 403（提示用 `/listen`）。
  与 `_perf_forbidden` / `_nano_forbidden` 同款。注意这是**页面级**的门：端点本身
  接受 listen token（见 4.6，能听能控、不能发），页面则是给操作员的操作台，
  两种会话的产品答案不同。
- **不进主界面 ☰ 菜单**，只记录在文档与 README 里。`static/index.html` 不动。
- **不进 `sw.js` 预缓存列表**（那是 SPA 外壳；`/listen`、`/sdr` 也不在里面）。
  `sw.js` 是 cache-first + 未命中就联网，未预缓存的页面走网络，行为正确。

### 5.2 文件与复用

| 文件 | 作用 |
| --- | --- |
| `static/terminal.html` | 自带样式（照 `listen.html` 的深色琥珀主题），无内联 JS |
| `static/terminal.js` | 一条 `/WSterminal`、帧分流、UI 渲染 |
| 复用（不改） | `modules/opus_wasm.js`、`modules/opus_codec.js`、`rx_worklet_processor.js`、`tx_capture_worklet.js`、`tx_opus_worker.js`、`modules/ptt_manager.js`、`modules/settings_manager.js` |

`ptt_manager.js` 依赖页面的三个全局：`sendMsg`、`sendCommand`、`handlePTTStart/End`。
参考页提供它们即可，模块本身零改动。

### 5.3 功能边界（已定）

**做**：频率/模式显示与调谐、S 表、瀑布、AF 音量、PTT + 麦克风、状态条。

**不做**：memory 频道管理、录音、FT8、设备设置、ATR1000、云端接入、断电/重启。
这些能力服务端**支持**（走 `_handle_ws_message`），只是参考页不提供入口。

### 5.4 前缀安全

与 `listen.js` 同款：

```js
const URL_BASE = location.pathname.slice(0, location.pathname.lastIndexOf('/terminal'));
```

所有 fetch / WS / 静态资源 URL 由此拼出。`tests/test_path_prefix.py` 会扫静态文件，
绝对路径会当场变红。

### 5.5 状态条（兼作诊断）

显示：链路状态、RX 帧数、TX 帧数、丢帧计数（三类）、频谱档位、当前 token 角色。
这张页面同时是协议验收工具——没有它，`/WSterminal` 就是一段没被跑过的代码。

## 6. 错误与边界

| 情形 | 行为 |
| --- | --- |
| 终端下行拥塞 | 丢帧 + 计数，socket 保留，控制与 PTT 照常 |
| 未知通道号 | 丢弃 + `_terminal_drops["unknown"] += 1`（限流告警，不刷屏） |
| 老服务端（无此路由） | 404；客户端回退旧三端点。服务端无需任何代码 |
| 两客户端抢 uplink | 沿用 `_claim_tx_owner_for_token`：谁按键谁拿走 |
| 终端发射中断线 | 第 4.5 步一次强制 RX |
| listen 会话发 `chan 0x01` | 丢弃 + 计数 + 一条 `error` 文本 |
| 文本与二进制并发写同一 socket | 沿用 `/WSsdr` 的生产先例（接收循环 + 扇出同写一条 socket），不引入发送锁；若验收发现帧序异常再加 |
| `_broadcast_state` 对每个 socket 顺序 await、无超时 | 既有行为，本次不改（不顺手扩大范围）；终端掉线最坏阻塞到 TCP 超时 |
| 终端在 `onlineUsers` 里算一个用户 | 符合直觉（一个终端 = 一个操作者），无需特殊处理 |

## 7. 测试

全部可无电台运行（`unittest`；假 socket 风格沿用 `tests/test_server_ws_protocol.py`
的 `_fake_socket()` 与 `IsolatedAsyncioTestCase`）。

**纯逻辑**（新 `tests/test_ws_terminal.py`）

- 帧拆分：`0x00/0x01/0x02` 正确、空帧、单字节帧、未知通道。
- `spectrumCaps` 解析：白名单内/外、缺字段、非法 JSON。
- 丢帧策略判定：终端 socket 失败 → 不判死；普通 socket 失败 → 判死。

**handler 级**（同一模块，直接 await 真实 handler + 假 ws）

- 连接：四张表都注册、fullState 已发、`metrics` 记 `terminal`。
- 文本：`ping` → `pong`；`get fullState` → `fullState`。
- 二进制：`0x01` 进入 TX 会话（`_tx_session_frames` 增长、owner 判定生效）。
- 下行：`0x00` 前缀；未订阅不收 `0x02`；`spectrumCaps` 后收 `0x02` 且档位生效。
- listen：`chan 0x01` 被拒、仍可接收下行。
- 断开：强制 RX 恰好一次；`_promote_tx_owner` 被调用；四张表都清空。

**静态契约**

- `WS_PATHS` 含 `/WSterminal`（防 302 老坑）。
- `terminal.js` 不引用 `ft710_main.js` / `ft710_ui.js`；无绝对路径（`test_path_prefix.py` 自动覆盖）。
- `sw.js` 预缓存不含 `/terminal`。

**手工验收**（本机，无电台）

1. `python server.py` 起服务，登录 → `https://127.0.0.1:8888/terminal`。
2. 状态条：链路 up、fullState 到达、S 表有值。
3. 瀑布：S-meter fallback 曲线出现（无电台时这就是正确行为）。
4. 按 PTT：服务端日志出现 TX 会话计数（`tx: frames=…`），松手后归零。
5. 关页面：服务端日志出现一次强制 RX（若当时在发射）。

真机调制不在本次验收范围（没有固件消费者；`/terminal` 是页面不是电台）。

## 8. 文档同步与交付拆分

**提交 1（服务端）**

- `server.py`：端点 + 扇出分支 + 状态 + `WS_PATHS` + 计量
- `session_metrics.py`：`KINDS` 增加 `terminal`
- `tests/test_ws_terminal.py`、`tests/test_session_metrics.py`（若断言种类集合）
- 文档：`SDD/09-architecture-overview.md` §9.2 新增 9.2.6 小节；`SDD/08` 新 AD；
  `SDD/14-version-history.md` 新条目；`SDD/README.md` Quick Facts；
  `AGENTS.md` 模块表（`/WSterminal` 一句话）；`README.md`；`tests/README.md` 计数

**提交 2（参考页）**

- `static/terminal.html`、`static/terminal.js`、`GET /terminal` 路由、页面契约测试
- 文档：`docs/` 里的客户端指引补一节（页面地址、能力边界、为什么不进菜单）

**发布**：服务端先发（纯增量，老客户端无感）。固件的回退逻辑属于另一条线。

## 9. 音频档位（AD-038 PCM16 × AD-039 终端，2026-11-17 已实现）

`docs/superpowers/specs/2026-11-17-voice-16k-design.md` 与
`docs/superpowers/plans/2026-11-17-esp32-pcm16-profile.md` 的 PCM16 线已合并，
耦合点兑现为：**终端 socket 的音频载荷由该 socket 的音频档位决定**。

ESP32-C3 终端**没有 Opus 选项**，只有两个裸 PCM 档位，所以终端的协商与专用
音频端点不同：

- `{"type":"audioCaps","profile":"pcm48"}` → `0x00` + 48 kHz mono s16le；
  `{"type":"audioCaps","profile":"pcm16"}` → `0x02` + 16 kHz mono s16le（640 B/20 ms）。
- 解析用 `audio_profile.parse_terminal_caps`（只认这两个名字）；专用
  `/WSaudioRX`/`/WSaudioTX` 的 `parse_caps` 仍只认 `pcm16`，不被放宽。
- 一条 socket 同时承载 RX/TX，所以 `_terminal_audio_caps` 一次声明把两个方向
  一起登记、**只回一次应答**（不是早期草案里 `_handle_audio_caps(tx=False/True)`
  各调一次——那会答两次，等单条 ack 的固件会把第二条当协议错误）。
  应答带 rate/channels/format/frameMs；未知名字答 `unknown-profile`，
  PCM16 无可用 TX 路答 `pcm16-tx-unsupported`；listen socket 只登记 RX，
  TX 支持门不拦一个根本没有上行的会话。
- RX 扇出分三组：PCM16（共享 soxr LQ 48→16，每 tick 一次）、PCM48
  （`0x00` + 增益后裸块，**不经过 Opus，与 libopus 可用性无关**——这正是
  无 Opus 终端声明 pcm48 要换来的保证）、legacy（未声明 socket 字节级保持
  Opus48/PCM48，`/terminal` 参考页因此一行不用改）。
- TX 不变：`0x00` 走默认 48 kHz，`0x02` 走 `input_rate=16000`，共用
  `_feed_tx_uplink_frame`；PCM16 转换失败 force-RX 不关 socket。
- 断连在第一个 await 之前 `_audio_rx_profiles.pop` / `_audio_tx_profiles.pop`
  并 `_reset_pcm16_rx_encoder_if_idle()`——清理被 await 处的取消打断也不留
  ghost profile（与 `ws_audio_tx` 的 finally 同一条规则）。

守卫：`tests/test_ws_terminal.py` 的 `TerminalAudioCapsTests` /
`TerminalAudioFanoutTests`（驱动真实 `_audio_rx_loop`）/ `TerminalAudioTxTests` /
`TerminalAudioCleanupTests`。真机/ESP32 固件验收仍未完成。

## 10. 风险与未决

| 风险 | 影响 | 处置 |
| --- | --- | --- |
| 同一 socket 上文本与二进制并发写 | 帧序异常（不会损坏帧） | 沿用 `/WSsdr` 先例；验收阶段观察，必要时加 per-socket 锁 |
| 终端 socket 计入 `ctrl_clients`，`_broadcast_state` 无超时 | 弱网终端可能拖慢状态广播 | 既有风险，不扩大；若现场出现，单独给广播加超时 |
| 参考页被当成产品界面 | 操作员找不到 memory/录音 | 文档与页头写明「参考实现」，入口不进菜单 |
| 一个终端四条逻辑通道共用一个 socket | 单点故障面变大 | 断线清理一次性覆盖四张表（4.5），并有测试钉住 |
