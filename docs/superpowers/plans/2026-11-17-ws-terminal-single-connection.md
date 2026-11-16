# /WSterminal 单连接复用 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 增加 `/WSterminal` —— 一条 WebSocket 同时承载控制、上下行音频与频谱；旧四条端点一行不改。

**架构：** 端点把同一个 socket 注册进 `ctrl_clients` / `audio_rx_clients` / `audio_tx_clients`（operator 时）/ `spectrum_clients`（声明档位后），文本帧原样交给 `_handle_ws_message`，二进制帧用「1 字节通道号 + 旧端点载荷」封装；两处扇出为终端 socket 加通道前缀，并在媒体拥塞时丢帧而不是丢 socket（那条 socket 还背着控制与 PTT 安全链）。

**技术栈：** Python 3.12、FastAPI WebSocket、unittest；设计权威 `docs/superpowers/specs/2026-11-17-ws-terminal-single-connection-design.md`。

---

## 文件结构

| 文件 | 责任 |
| --- | --- |
| `server.py` | 端点、文本/二进制分流、扇出通道前缀与丢帧策略、断开清理；`WS_PATHS` 放行 |
| `session_metrics.py` | `KINDS` 增加 `terminal`（sockets 计数）；`METERED_KINDS` 不变 |
| `tests/test_ws_terminal.py`（新建） | 帧拆分、分流、注册/清理、角色、扇出策略 |
| `static/terminal.html` + `static/terminal.js`（新建） | 自包含参考页 |
| `docs/…`、`SDD/…`、`AGENTS.md`、`README.md`、`tests/README.md` | 协议、测试计数、版本历史同步 |

---

## 任务 1：二进制帧的通道拆分（纯逻辑，先写测试）

**文件：**

- 修改：`server.py`（新增两个模块级函数）
- 测试：`tests/test_ws_terminal.py`

- [ ] **步骤 1：写失败的测试**

```python
"""单连接终端（/WSterminal）：帧格式、分流、生命周期、扇出策略。"""
import unittest

import server


class TerminalFrameSplitTests(unittest.TestCase):
    def test_rx_audio_channel_keeps_the_legacy_payload(self):
        chan, body = server._terminal_split(b"\x00\x01opus")
        self.assertEqual(chan, server.TERMINAL_CHAN_RX_AUDIO)
        self.assertEqual(body, b"\x01opus")

    def test_tx_audio_channel_keeps_the_codec_tag(self):
        chan, body = server._terminal_split(b"\x01\x00pcm")
        self.assertEqual(chan, server.TERMINAL_CHAN_TX_AUDIO)
        self.assertEqual(body, b"\x00pcm")

    def test_spectrum_channel(self):
        chan, body = server._terminal_split(b"\x02\x01" + b"\x00" * 850)
        self.assertEqual(chan, server.TERMINAL_CHAN_SPECTRUM)
        self.assertEqual(len(body), 851)

    def test_empty_and_single_byte_frames_are_rejected(self):
        for frame in (b"", b"\x01"):
            chan, body = server._terminal_split(frame)
            self.assertEqual((chan, body), (-1, b""))

    def test_prefix_is_empty_for_legacy_sockets(self):
        self.assertEqual(server._terminal_prefix(object(), 0x00), b"")
```

- [ ] **步骤 2：运行确认失败**

```bash
python -m unittest tests.test_ws_terminal -v
```

预期：`AttributeError: module 'server' has no attribute '_terminal_split'`

- [ ] **步骤 3：实现**

在 `server.py` 的 `/WSaudioTX` handler 之后、`# ── SDR receiver (AD-029)` 之前插入：

```python
# ── Terminal WebSocket (one socket: control + RX/TX audio + spectrum) ──
#
# 给「TLS 会话与连续堆才是最贵资源」的客户端用（ESP32 级硬件；见
# docs/superpowers/specs/2026-11-17-ws-terminal-single-connection-design.md）。
# 它不是第二条控制协议：文本帧原样交给 _handle_ws_message，四条旧端点一行
# 不改，本端点只是把同一批注册表多填一次。

#: Sockets carrying more than one logical channel over one WebSocket.
_terminal_ws: set[WebSocket] = set()
#: Channel byte in front of every binary frame on this endpoint.
TERMINAL_CHAN_RX_AUDIO = 0x00
TERMINAL_CHAN_TX_AUDIO = 0x01
TERMINAL_CHAN_SPECTRUM = 0x02
#: Media that could not be delivered because the socket was backpressured.
#: Counted instead of dropping the socket: it also carries control and the PTT
#: safety chain (design §4.4).
_terminal_drops = {"audio": 0, "spectrum": 0, "tx": 0, "unknown": 0}


def _terminal_split(data: bytes) -> tuple[int, bytes]:
    """Split a /WSterminal binary frame into (channel, legacy body).

    Returns (-1, b"") for anything too short to be a frame: a channel byte with
    no body carries nothing, and guessing would hand an empty payload to the TX
    decoder.
    """
    if len(data) < 2:
        return -1, b""
    return data[0], data[1:]


def _terminal_prefix(ws: WebSocket, chan: int) -> bytes:
    """The channel byte a fan-out puts in front of this socket's payload."""
    return bytes((chan,)) if ws in _terminal_ws else b""
```

- [ ] **步骤 4：运行确认通过**

```bash
python -m unittest tests.test_ws_terminal -v
```

- [ ] **步骤 5：Commit**

```bash
git add server.py tests/test_ws_terminal.py
git commit -m "feat(ws): 单连接终端的通道拆分与前缀"
```

---

## 任务 2：把 TX 上行解码抽成一处共用

**文件：**

- 修改：`server.py:6353-6448`（`/WSaudioTX` 的二进制分支）
- 测试：`tests/test_ws_terminal.py`（新增一个共用性断言）

- [ ] **步骤 1：写失败的测试**

```python
class TXUplinkSharedPathTests(unittest.TestCase):
    def test_both_endpoints_call_the_shared_uplink_helper(self):
        import inspect
        src = inspect.getsource(server.ws_audio_tx)
        self.assertIn("_feed_tx_uplink_frame", src)
        # 解码与 owner 判定不允许在这条端点里再写一遍
        self.assertNotIn("_tx_session_frames += 1", src)
```

- [ ] **步骤 2：运行确认失败**

```bash
python -m unittest tests.test_ws_terminal.TXUplinkSharedPathTests -v
```

- [ ] **步骤 3：在 `/WSaudioTX` 之上插入共用函数，并替换原分支**

```python
async def _feed_tx_uplink_frame(ws: WebSocket, data: bytes) -> None:
    """Decode one codec-tagged mic frame and queue it for the radio.

    Shared by /WSaudioTX and /WSterminal: ownership arbitration, the codec-tag
    dispatch and the per-session counters must exist once.  /WSterminal carries
    the uplink on the same socket as the control channel, so a second copy of
    this logic would be a second place for the owner rule to drift.
    """
    global _tx_session_frames, _tx_session_decoded, _tx_session_decode_fail
    global _tx_non_owner_drops

    if len(data) < 2:
        return

    # Only the owner's audio reaches the radio; ignore others.
    if ws is not _tx_owner_ws:
        # A same-session socket that is streaming IS the operator (the token is
        # a 30-day cookie, so one browser session can hold several of them) —
        # hand the uplink over rather than mute the transmission this frame
        # belongs to.
        if not _same_session_uplink_takeover(ws):
            _tx_non_owner_drops += 1
            if _tx_non_owner_drops % 250 == 1:
                logger.warning(
                    "TX audio from non-owner client ignored "
                    "(drops=%d) — another client owns the uplink",
                    _tx_non_owner_drops)
            return

    tag = data[0]
    frame = data[1:]
    _tx_session_frames += 1

    if tag == AUDIO_TAG_OPUS and _opus_tx_decoder:
        pcm = _opus_tx_decoder.decode(frame)
        if pcm and audio:
            await _feed_tx_from_uplink(pcm)
            _tx_session_decoded += 1
        elif not pcm:
            _tx_session_decode_fail += 1
    elif tag == AUDIO_TAG_PCM:
        if audio:
            await _feed_tx_from_uplink(frame)
            _tx_session_decoded += 1
    else:
        # Legacy: untagged, assume PCM
        if audio:
            await _feed_tx_from_uplink(data)
            _tx_session_decoded += 1
```

`/WSaudioTX` 的循环体里，把整段「owner 判定 + tag 派发」替换成一行（`else` 分支保持不动）：

```python
            if "bytes" in msg and msg["bytes"] is not None:
                await _feed_tx_uplink_frame(ws, msg["bytes"])
```

同时删除 `ws_audio_tx` 里已不再使用的 global 声明（`_tx_session_frames` /
`_tx_session_decoded` / `_tx_session_decode_fail` / `_tx_non_owner_drops`）。

- [ ] **步骤 4：运行确认通过（含既有 TX 所有权测试）**

```bash
python -m unittest tests.test_ws_terminal tests.test_server_ws_protocol -v
```

- [ ] **步骤 5：Commit**

```bash
git add server.py tests/test_ws_terminal.py
git commit -m "refactor(ws): TX 上行解码抽成 /WSaudioTX 与 /WSterminal 共用路径"
```

---

## 任务 3：放行路径与计量种类

**文件：**

- 修改：`server.py:4928`（`WS_PATHS`）、`session_metrics.py:37`（`KINDS`）
- 测试：`tests/test_ws_terminal.py`

- [ ] **步骤 1：写失败的测试**

```python
class TerminalWiringTests(unittest.TestCase):
    def test_ws_path_is_never_redirected(self):
        # WS 升级跟不了 302；漏了这一步，浏览器只会看到一次登录跳转
        self.assertIn("/WSterminal", server.WS_PATHS)

    def test_metrics_knows_the_terminal_kind(self):
        import session_metrics
        self.assertIn("terminal", session_metrics.KINDS)
        self.assertNotIn("terminal", session_metrics.METERED_KINDS)
```

- [ ] **步骤 2：运行确认失败**

```bash
python -m unittest tests.test_ws_terminal.TerminalWiringTests -v
```

- [ ] **步骤 3：实现**

```python
WS_PATHS = {"/WSradio", "/WSspectrum", "/WSaudioRX", "/WSaudioTX", "/WSsdr",
            "/WSterminal"}
```

```python
KINDS = ("control", "spectrum", "audio_rx", "audio_tx", "atr", "sdr", "terminal")
```

- [ ] **步骤 4：运行确认通过**

```bash
python -m unittest tests.test_ws_terminal tests.test_session_metrics -v
```

- [ ] **步骤 5：Commit**

```bash
git add server.py session_metrics.py tests/test_ws_terminal.py
git commit -m "feat(ws): /WSterminal 放行路径与 terminal 计量种类"
```

---

## 任务 4：端点本体（注册、fullState、文本分流）

**文件：**

- 修改：`server.py`（插入 `_terminal_handle_text` / `_terminal_subscribe_spectrum` / `ws_terminal`）
- 测试：`tests/test_ws_terminal.py`

- [ ] **步骤 1：写失败的测试（假 socket + 真实 handler）**

```python
import asyncio
import json


class _FakeWS:
    """Starlette WebSocket 的最小替身：只要能收能发能记。"""

    def __init__(self, *, token=None, incoming=None):
        self.headers = {}
        self.cookies = {"mrrc_auth": token} if token else {}
        self.query_params = {}
        self.texts = []
        self.bytes = []
        self.closed = None
        self._incoming = list(incoming or [])
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    async def receive(self):
        if self._incoming:
            return self._incoming.pop(0)
        return {"type": "websocket.disconnect"}

    async def send_text(self, data):
        self.texts.append(data)

    async def send_bytes(self, data):
        self.bytes.append(data)


class _TerminalTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.s = server
        self._saved = {
            "tokens": dict(server._auth_tokens),
            "ws_tokens": dict(server._ws_tokens),
            "ctrl": set(server.ctrl_clients),
            "rx": set(server.audio_rx_clients),
            "tx": set(server.audio_tx_clients),
            "spectrum": set(server.spectrum_clients),
            "terminal": set(server._terminal_ws),
            "owner": server._tx_owner_ws,
            "keyer": server._ptt_key_ws,
        }
        server.ctrl_clients.clear()
        server.audio_rx_clients.clear()
        server.audio_tx_clients.clear()
        server.spectrum_clients.clear()
        server._terminal_ws.clear()
        server._ws_tokens.clear()
        server._tx_owner_ws = None
        server._ptt_key_ws = None
        server._auth_tokens.clear()
        server._auth_tokens.add("op-token")
        self._full_state = server._full_state_message
        server._full_state_message = lambda data, channels: {
            "type": "fullState", "data": {},
        }

    def tearDown(self):
        self.s._auth_tokens.clear()
        self.s._auth_tokens.update(self._saved["tokens"])
        self.s._ws_tokens.clear()
        self.s._ws_tokens.update(self._saved["ws_tokens"])
        self.s.ctrl_clients.clear()
        self.s.ctrl_clients.update(self._saved["ctrl"])
        self.s.audio_rx_clients.clear()
        self.s.audio_rx_clients.update(self._saved["rx"])
        self.s.audio_tx_clients.clear()
        self.s.audio_tx_clients.update(self._saved["tx"])
        self.s.spectrum_clients.clear()
        self.s.spectrum_clients.update(self._saved["spectrum"])
        self.s._terminal_ws.clear()
        self.s._terminal_ws.update(self._saved["terminal"])
        self.s._tx_owner_ws = self._saved["owner"]
        self.s._ptt_key_ws = self._saved["keyer"]
        self.s._full_state_message = self._full_state

    async def run_terminal(self, ws):
        await self.s.ws_terminal(ws)


class TerminalConnectTests(_TerminalTestBase):
    async def test_rejected_without_a_valid_token(self):
        ws = _FakeWS(token="nope")
        await self.run_terminal(ws)
        self.assertEqual(ws.closed[0], 4001)
        self.assertNotIn(ws, self.s._terminal_ws)

    async def test_registers_in_every_registry_and_pushes_full_state(self):
        ws = _FakeWS(token="op-token")
        await self.run_terminal(ws)
        self.assertTrue(ws.accepted)
        self.assertIn(ws, self.s.ctrl_clients)
        self.assertIn(ws, self.s.audio_rx_clients)
        self.assertIn(ws, self.s.audio_tx_clients)
        self.assertIn(ws, self.s._terminal_ws)
        self.assertEqual(self.s._ws_tokens[ws], "op-token")
        self.assertEqual(json.loads(ws.texts[0])["type"], "fullState")

    async def test_text_frames_go_through_the_wsradio_protocol(self):
        ws = _FakeWS(token="op-token", incoming=[
            {"type": "websocket.receive", "text": '{"type":"ping"}'},
        ])
        await self.run_terminal(ws)
        self.assertIn('{"type": "pong"}', ws.texts)
```

- [ ] **步骤 2：运行确认失败**

```bash
python -m unittest tests.test_ws_terminal.TerminalConnectTests -v
```

- [ ] **步骤 3：实现**

```python
async def _terminal_subscribe_spectrum(ws: WebSocket, profile: str) -> None:
    """Declare this socket's spectrum tier and start its scope stream.

    Idempotent.  Nothing is produced until the first declaration, because a
    control-only terminal must not spin the scope pipe (30 Hz of SPI reads
    nobody asked for).
    """
    if ws in spectrum_clients and _spectrum_profiles.get(ws) == profile:
        return
    _spectrum_profiles[ws] = profile
    spectrum_clients.add(ws)
    if _scope_producer is not None:
        await _scope_producer.start()
    tier = spectrum_profile.PROFILES[profile]
    logger.info("Terminal spectrum profile %s (%s frame, divider %d)",
                profile, tier.shape, tier.divider)


async def _terminal_handle_text(ws: WebSocket, text: str) -> None:
    """Route a text frame: spectrumCaps is ours, everything else is /WSradio."""
    obj = None
    try:
        obj = json.loads(text)
    except Exception:
        pass
    if isinstance(obj, dict) and obj.get("type") == "spectrumCaps":
        profile = spectrum_profile.parse_caps(text)
        if profile:
            await _terminal_subscribe_spectrum(ws, profile)
        return
    await _handle_ws_message(ws, text)


@app.websocket("/WSterminal")
async def ws_terminal(ws: WebSocket):
    """Single-socket client: control + RX audio + TX audio + spectrum.

    Text frames are the /WSradio protocol (plus this endpoint's own
    ``spectrumCaps`` subscription); binary frames are ``channel byte + the
    payload the dedicated endpoint would have sent`` (design §3).
    """
    token = _token_from_ws_handshake(ws)
    if not token or token not in _auth_tokens:
        await ws.close(code=4001, reason="Unauthorized")
        return
    role = _role_for_token(token)
    listen_only = token in _listen_tokens

    await ws.accept()
    ctrl_clients.add(ws)
    _ws_tokens[ws] = token
    _terminal_ws.add(ws)
    audio_rx_clients.add(ws)
    metrics.open(role, "terminal", token)
    if not listen_only:
        # Registering here is what lets _claim_tx_owner_for_token() find this
        # socket at key-up; without it the radio keys with no modulation.
        audio_tx_clients.add(ws)
        _register_tx_socket(ws)
        owner = _assign_tx_owner_on_connect(ws, token)
    else:
        owner = False
    logger.info("Terminal client connected (%d total, role=%s, owner=%s)",
                len(_terminal_ws), role, owner)
    if scheduler:
        scheduler.set_active(len(ctrl_clients) > 0)
    await _broadcast_client_count()

    try:
        await ws.send_text(json.dumps(
            _full_state_message(radio.to_dict(), _load_mem_channels())))
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                await _terminal_handle_binary(ws, msg["bytes"],
                                              listen_only=listen_only)
            elif msg.get("text"):
                await _terminal_handle_text(ws, msg["text"])
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as e:
        logger.debug("Terminal WS error: %s", e)
    finally:
        await _terminal_disconnect(ws, role, token)
```

（`_terminal_handle_binary` / `_terminal_disconnect` 在任务 5、6 落地；本任务的测试只走
文本与连接路径，所以先以最小实现占位**不允许**——本步骤同时写入任务 5 的两个函数骨架：

```python
async def _terminal_handle_binary(ws: WebSocket, data: bytes, *,
                                  listen_only: bool) -> None:
    """Route a binary frame by channel byte (design §3.3)."""
    chan, body = _terminal_split(data)
    if chan == TERMINAL_CHAN_TX_AUDIO:
        if listen_only:
            _terminal_drops["tx"] += 1
            await ws.send_text(json.dumps({
                "type": "error", "message": LISTEN_ONLY_MESSAGE,
            }))
            return
        await _feed_tx_uplink_frame(ws, body)
        return
    # 0x00 / 0x02 are server→client channels; anything else is a client bug.
    _terminal_drops["unknown"] += 1


async def _terminal_disconnect(ws: WebSocket, role: str, token: str) -> None:
    """One socket, four registrations — each has a safety rule attached."""
    global _tx_owner_ws, _ptt_key_ws

    ctrl_clients.discard(ws)
    _terminal_ws.discard(ws)
    _ws_tokens.pop(ws, None)
    _tx_hb_capable.discard(ws)
    _tx_hb_last.pop(ws, None)
    audio_rx_clients.discard(ws)
    _spectrum_profiles.pop(ws, None)
    spectrum_clients.discard(ws)
    _listen_spectrum_clients.discard(ws)
    audio_tx_clients.discard(ws)
    _forget_tx_socket(ws)
    metrics.close(role, "terminal", token)

    # The keyer and the uplink owner are the same socket here, so this is the
    # one place the dead-man switch can fire twice — force RX once.
    was_keyer = ws is _ptt_key_ws
    was_owner = ws is _tx_owner_ws
    if was_keyer:
        _ptt_key_ws = None
    if was_owner:
        _tx_owner_ws = None
    if (was_keyer or was_owner) and radio.is_transmitting and cat and cat.connected:
        logger.warning("Terminal client disconnected during TX! Forcing RX.")
        try:
            await cat.set_ptt(False)
            radio.update(tx_status=0)
        except Exception:
            pass
    if was_owner and audio:
        await asyncio.to_thread(audio.stop_tx)
    if was_owner and _promote_tx_owner() is not None:
        logger.info("TX audio ownership promoted to remaining client (%d total)",
                    len(audio_tx_clients))

    if (not spectrum_clients and _scope_producer is not None
            and not scope_keeps_running(_scope_producer)):
        await _scope_producer.stop()

    logger.info("Terminal client disconnected (%d remain)", len(_terminal_ws))
    if scheduler:
        scheduler.set_active(len(ctrl_clients) > 0)
    try:
        await _broadcast_client_count()
    except Exception:
        pass
    try:
        await _cq_abort_if_client_gone(ws)
    except Exception as e:
        logger.warning("CQ abort on disconnect failed: %s", e)
```

- [ ] **步骤 4：运行确认通过**

```bash
python -m unittest tests.test_ws_terminal -v
```

- [ ] **步骤 5：Commit**

```bash
git add server.py tests/test_ws_terminal.py
git commit -m "feat(ws): /WSterminal 端点——注册、fullState 与文本分流"
```

---

## 任务 5：断开清理的语义（强制 RX 只一次、四张表全清）

**文件：**

- 修改：`server.py`（`_terminal_disconnect`，任务 4 已写入，本任务补测试钉住）
- 测试：`tests/test_ws_terminal.py`

- [ ] **步骤 1：写失败的测试**

```python
class TerminalDisconnectTests(_TerminalTestBase):
    async def test_every_registry_is_cleaned_and_rx_forced_once(self):
        ws = _FakeWS(token="op-token")
        keyed = []

        async def _set_ptt(on):
            keyed.append(on)
            return True

        class _Cat:
            connected = True

            async def set_ptt(self, on):
                return await _set_ptt(on)

        async def _noop():
            return None

        self.s.ctrl_clients.update([ws])
        self.s.audio_rx_clients.update([ws])
        self.s.audio_tx_clients.update([ws])
        self.s.spectrum_clients.update([ws])
        self.s._terminal_ws.add(ws)
        self.s._ws_tokens[ws] = "op-token"
        self.s._tx_owner_ws = ws
        self.s._ptt_key_ws = ws

        saved_cat, saved_txing, saved_audio = self.s.cat, self.s.radio.is_transmitting, self.s.audio
        saved_stop = getattr(self.s.audio, "stop_tx", None)
        try:
            self.s.cat = _Cat()
            self.s.radio.is_transmitting = True
            self.s.audio = mock.Mock()
            self.s.audio.stop_tx = _noop
            await self.s._terminal_disconnect(ws, "operator", "op-token")
        finally:
            self.s.cat = saved_cat
            self.s.radio.is_transmitting = saved_txing
            self.s.audio = saved_audio

        self.assertEqual(keyed, [False])          # 一次，不是两次
        self.assertNotIn(ws, self.s.ctrl_clients)
        self.assertNotIn(ws, self.s.audio_rx_clients)
        self.assertNotIn(ws, self.s.audio_tx_clients)
        self.assertNotIn(ws, self.s.spectrum_clients)
        self.assertNotIn(ws, self.s._terminal_ws)
        self.assertIsNone(self.s._tx_owner_ws)
        self.assertIsNone(self.s._ptt_key_ws)
```

（`mock` 需要 `from unittest import mock`；`radio.is_transmitting` 是派生属性时改用
`mock.patch.object(type(self.s.radio), "is_transmitting", new_callable=mock.PropertyMock,
return_value=True)`。）

- [ ] **步骤 2：运行确认失败 → 补实现 → 通过**

```bash
python -m unittest tests.test_ws_terminal.TerminalDisconnectTests -v
```

- [ ] **步骤 3：Commit**

```bash
git add tests/test_ws_terminal.py server.py
git commit -m "test(ws): 钉住 /WSterminal 的断开清理与单次强制 RX"
```

---

## 任务 6：下行扇出——通道前缀 + 丢帧不丢 socket

**文件：**

- 修改：`server.py:2251-2280`（`_send_audio_frames_to_clients`）、`server.py:2226-2250`（`_spectrum_fanout`）
- 测试：`tests/test_ws_terminal.py`

- [ ] **步骤 1：写失败的测试**

```python
class TerminalFanoutTests(_TerminalTestBase):
    async def test_audio_frames_get_the_channel_prefix(self):
        ws = _FakeWS(token="op-token")
        self.s._terminal_ws.add(ws)
        dead = await self.s._send_audio_frames_to_clients([b"\x01opus"], {ws})
        self.assertEqual(dead, set())
        self.assertEqual(ws.bytes, [b"\x00\x01opus"])

    async def test_legacy_sockets_keep_the_byte_for_byte_payload(self):
        ws = _FakeWS(token="op-token")
        await self.s._send_audio_frames_to_clients([b"\x01opus"], {ws})
        self.assertEqual(ws.bytes, [b"\x01opus"])

    async def test_backpressure_drops_frames_not_the_terminal_socket(self):
        class _Stuck(_FakeWS):
            async def send_bytes(self, data):
                await asyncio.sleep(0.05)

        ws = _Stuck(token="op-token")
        self.s._terminal_ws.add(ws)
        before = self.s._terminal_drops["audio"]
        dead = await self.s._send_audio_frames_to_clients(
            [b"\x01opus"], {ws}, per_frame_timeout=0.001)
        self.assertEqual(dead, set())
        self.assertGreater(self.s._terminal_drops["audio"], before)

    async def test_backpressure_still_evicts_a_pure_audio_socket(self):
        class _Stuck(_FakeWS):
            async def send_bytes(self, data):
                await asyncio.sleep(0.05)

        ws = _Stuck(token="op-token")
        dead = await self.s._send_audio_frames_to_clients(
            [b"\x01opus"], {ws}, per_frame_timeout=0.001)
        self.assertEqual(dead, {ws})

    async def test_spectrum_frames_carry_the_channel_prefix(self):
        ws = _FakeWS(token="op-token")
        self.s._terminal_ws.add(ws)
        self.s.spectrum_clients.add(ws)
        await self.s._spectrum_fanout({"high": b"\x01" + b"\x00" * 850}, 0)
        self.assertEqual(ws.bytes[0][0], self.s.TERMINAL_CHAN_SPECTRUM)
```

- [ ] **步骤 2：运行确认失败**

```bash
python -m unittest tests.test_ws_terminal.TerminalFanoutTests -v
```

- [ ] **步骤 3：实现**

`_send_audio_frames_to_clients`：

```python
    async def _send_one(ws: WebSocket):
        prefix = _terminal_prefix(ws, TERMINAL_CHAN_RX_AUDIO)
        for frame in frames:
            payload = prefix + frame
            await asyncio.wait_for(ws.send_bytes(payload), timeout=per_frame_timeout)
            metrics.add_bytes("audio_rx", len(payload))
```

死客户端判定：

```python
    dead: set[WebSocket] = set()
    for ws, res in zip(clients_snapshot, results):
        if not isinstance(res, Exception):
            continue
        if ws in _terminal_ws:
            # A single-socket client also carries control and the PTT safety
            # chain: media congestion costs frames, never the session.
            _terminal_drops["audio"] += 1
            continue
        dead.add(ws)
    return dead
```

`_spectrum_fanout`：

```python
        payload = spectrum_profile.variant_for(variants, profile)
        prefix = _terminal_prefix(ws, TERMINAL_CHAN_SPECTRUM)
        try:
            await ws.send_bytes(prefix + payload)
        except Exception:
            if ws in _terminal_ws:
                _terminal_drops["spectrum"] += 1
                continue
            dead.add(ws)
            continue
```

- [ ] **步骤 4：运行确认通过**

```bash
python -m unittest tests.test_ws_terminal tests.test_audio -v
```

- [ ] **步骤 5：Commit**

```bash
git add server.py tests/test_ws_terminal.py
git commit -m "feat(ws): 终端扇出加通道前缀，媒体拥塞丢帧不丢控制信道"
```

---

## 任务 7：频谱订阅与角色

**文件：**

- 修改：`server.py`（已写入的 `_terminal_subscribe_spectrum` / `_terminal_handle_binary`）
- 测试：`tests/test_ws_terminal.py`

- [ ] **步骤 1：写失败的测试**

```python
class TerminalSpectrumAndRoleTests(_TerminalTestBase):
    async def test_no_spectrum_before_the_declaration(self):
        ws = _FakeWS(token="op-token", incoming=[
            {"type": "websocket.receive", "text": '{"type":"ping"}'},
        ])
        await self.run_terminal(ws)
        self.assertNotIn(ws, self.s.spectrum_clients)

    async def test_declaration_subscribes_and_sets_the_tier(self):
        ws = _FakeWS(token="op-token", incoming=[
            {"type": "websocket.receive",
             "text": '{"type":"spectrumCaps","profile":"low"}'},
        ])
        await self.run_terminal(ws)
        self.assertNotIn(ws, self.s.spectrum_clients)   # 断开时被清掉
        self.assertEqual(self.s._spectrum_profiles.get(ws), None)

    async def test_listen_session_cannot_stream_mic_frames(self):
        self.s._listen_tokens.add("listen-token")
        try:
            ws = _FakeWS(token="listen-token", incoming=[
                {"type": "websocket.receive", "bytes": b"\x01\x00pcm"},
            ])
            await self.run_terminal(ws)
        finally:
            self.s._listen_tokens.discard("listen-token")
        self.assertNotIn(ws, self.s.audio_tx_clients)
        self.assertTrue(any("listen" in t.lower() for t in ws.texts))

    async def test_unknown_channel_is_counted_not_raised(self):
        before = self.s._terminal_drops["unknown"]
        ws = _FakeWS(token="op-token")
        await self.s._terminal_handle_binary(ws, b"\x7f\x00", listen_only=False)
        self.assertEqual(self.s._terminal_drops["unknown"], before + 1)
```

- [ ] **步骤 2：运行确认失败 → 实现 → 通过**

```bash
python -m unittest tests.test_ws_terminal -v
```

- [ ] **步骤 3：Commit**

```bash
git add server.py tests/test_ws_terminal.py
git commit -m "feat(ws): 终端按需订阅频谱，listen 会话不得上行"
```

---

## 任务 8：文档、SDD 与测试计数同步

**文件：**

- 修改：`SDD/09-architecture-overview.md`（§9.2 新增 9.2.6）、`SDD/08-architecture-decisions.md`（新 AD）、`SDD/14-version-history.md`、`SDD/README.md`、`AGENTS.md`、`README.md`、`tests/README.md`

- [ ] **步骤 1：写 SDD §9.2.6**

内容：端点、认证顺序、文本协议= `/WSradio`、二进制通道表、频谱按需订阅、
listen 角色矩阵、扇出丢帧策略、与 `/WSaudioRX` 等的字节兼容关系。

- [ ] **步骤 2：写 AD**

```
## AD-039: 单连接复用端点 /WSterminal
Type: Architectural / Status: Implemented
Problem: ESP32 级客户端每条 TLS 约 16 KB，按键要拆一条握手一条，峰值堆只剩 5–15 KB。
Decision: 新增 /WSterminal（通道字节 + 旧载荷），旧四条端点不变。
Consequences: 客户端省一条 TLS 与一次按键握手；代价是媒体与控制在一条 socket 上，
  因此媒体拥塞必须丢帧而不是断连（有测试钉住）。
```

- [ ] **步骤 3：版本历史 + Quick Facts + AGENTS/README/tests/README**

- [ ] **步骤 4：全量测试与守卫**

```bash
python -m unittest discover -s tests 2>&1 | tail -5
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged
```

- [ ] **步骤 5：Commit**

```bash
git add -A SDD AGENTS.md README.md tests/README.md docs
git commit -m "docs(ws): 记录 /WSterminal 协议、AD 与测试计数"
```

---

## 任务 9：参考页 `/terminal`

**文件：**

- 创建：`static/terminal.html`、`static/terminal.js`
- 修改：`server.py`（`GET /terminal` 路由 + `_terminal_forbidden`）
- 测试：`tests/test_ws_terminal.py`

- [ ] **步骤 1：写失败的测试（静态契约）**

```python
class TerminalPageTests(unittest.TestCase):
    def test_page_is_not_wired_into_the_guarded_frontend(self):
        js = Path("static/terminal.js").read_text(encoding="utf-8")
        self.assertNotIn("ft710_main.js", js)
        self.assertNotIn("ft710_ui.js", js)

    def test_page_derives_its_base_path(self):
        js = Path("static/terminal.js").read_text(encoding="utf-8")
        self.assertIn("lastIndexOf('/terminal')", js)

    def test_route_refuses_listen_and_sdr_sessions(self):
        self.assertTrue(callable(server._terminal_forbidden))


class _PageReq:
    def __init__(self, *, listen=False):
        self.headers = {"cookie": "mrrc_auth=listen-token" if listen else ""}
        self.cookies = {"mrrc_auth": "listen-token"} if listen else {}
        self.query_params = {}


class TerminalPageGateTests(unittest.TestCase):
    def test_listen_session_is_refused(self):
        import server as s
        s._listen_tokens.add("listen-token")
        try:
            self.assertIsNotNone(s._terminal_forbidden(_PageReq(listen=True)))
        finally:
            s._listen_tokens.discard("listen-token")
```

- [ ] **步骤 2：运行确认失败**

- [ ] **步骤 3：实现路由**

```python
def _terminal_forbidden(request: Request) -> Optional[tuple[int, str]]:
    """参考页只给主站操作员：listen / sdr 会话各有自己的页面。

    注意这是**页面级**的门——/WSterminal 端点本身接受 listen token
    （能听能控、不能发），两种会话的产品答案不同。
    """
    if _is_listen_request(request):
        return (403, "listen-only session")
    if _sdr_token_from_request(request) in _sdr_tokens:
        return (403, "sdr session")
    return None


@app.get("/terminal", include_in_schema=False)
async def terminal_page(request: Request):
    """单连接参考客户端（设计 §5）。"""
    reason = _terminal_forbidden(request)
    if reason is not None:
        raise HTTPException(status_code=reason[0], detail=reason[1])
    page = STATIC_DIR / "terminal.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="terminal.html missing")
    return _html_page(page)
```

- [ ] **步骤 4：写 `static/terminal.html` / `static/terminal.js`**

页面要求（设计 §5.3）：频率/模式 + 调谐、S 表、瀑布、AF 音量、PTT + 麦克风、
状态条（链路 / RX 帧 / TX 帧 / 三类丢帧 / 频谱档位 / 角色）。必须：

- 只用 `URL_BASE` 派生 URL；
- 文本帧与二进制帧按通道分流（`0x00` 音频、`0x02` 频谱）；
- PTT 走 `modules/ptt_manager.js`，页面提供 `sendMsg` / `sendCommand` / `handlePTTStart|End`。

- [ ] **步骤 5：验收**

```bash
python -m unittest tests.test_ws_terminal tests.test_path_prefix -v
python server.py  # 另开浏览器：https://127.0.0.1:8888/terminal
```

- [ ] **步骤 6：Commit**

```bash
git add static/terminal.html static/terminal.js server.py tests/test_ws_terminal.py
git commit -m "feat(web): /terminal 单连接参考页"
```
