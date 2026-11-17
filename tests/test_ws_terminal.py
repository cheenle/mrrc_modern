"""单连接终端（/WSterminal）：帧格式、分流、生命周期、扇出策略。

设计：docs/superpowers/specs/2026-11-17-ws-terminal-single-connection-design.md
全部用例都不需要电台或声卡：handler 用假 socket 驱动，扇出直接调真实函数。
"""
import asyncio
import json
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Optional
from unittest import mock

import audio_levels
import server


class _FakeWS:
    """Starlette WebSocket 的最小替身：能收、能发、能记。"""

    def __init__(self, *, token=None, incoming=None):
        self.headers = {}
        self.cookies = {"mrrc_auth": token} if token else {}
        self.query_params = {}
        self.texts = []
        self.bytes = []
        self.closed = None
        self.accepted = False
        #: 每次 receive() 前拍一张注册表快照（handler 的 finally 会把它们清掉，
        #: 所以「连接期间注册了哪些表」只能在循环里看）。
        self.on_receive: Optional[Callable[[], None]] = None
        self._incoming = list(incoming or [])

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    async def receive(self):
        if self.on_receive is not None:
            self.on_receive()
        if self._incoming:
            return self._incoming.pop(0)
        return {"type": "websocket.disconnect"}

    async def send_text(self, data):
        self.texts.append(data)

    async def send_bytes(self, data):
        self.bytes.append(data)


def _fake_ws(**kwargs) -> Any:
    """A socket stand-in: deliberately outside the WebSocket type system."""
    return _FakeWS(**kwargs)


def _stuck_ws(**kwargs) -> Any:
    return _StuckWS(**kwargs)


class _StuckWS(_FakeWS):
    """A socket whose TCP buffer never drains."""

    async def send_bytes(self, data):
        await asyncio.sleep(0.05)


def _sent_types(ws):
    return [json.loads(t).get("type") for t in ws.texts]


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
            self.assertEqual(server._terminal_split(frame), (-1, b""))

    def test_offer_prefixes_the_frame_and_reports_a_full_queue(self):
        import asyncio
        q = asyncio.Queue(maxsize=1)
        self.assertTrue(server._terminal_offer(q, server.TERMINAL_CHAN_RX_AUDIO, b"abc"))
        self.assertEqual(q.get_nowait(), b"\x00abc")
        self.assertTrue(server._terminal_offer(q, server.TERMINAL_CHAN_RX_AUDIO, b"x"))
        self.assertFalse(server._terminal_offer(q, server.TERMINAL_CHAN_RX_AUDIO, b"y"))


class TerminalWiringTests(unittest.TestCase):
    def test_ws_path_is_never_redirected(self):
        # WS 升级跟不了 302：漏了这一步，客户端只会看到一次登录跳转
        self.assertIn("/WSterminal", server.WS_PATHS)

    def test_metrics_knows_the_terminal_kind(self):
        import session_metrics
        self.assertIn("terminal", session_metrics.KINDS)
        # 字节仍记在 audio_rx / spectrum 上：那才是这两条路的真实出流量
        self.assertNotIn("terminal", session_metrics.METERED_KINDS)


class TXUplinkSharedPathTests(unittest.TestCase):
    def test_both_endpoints_call_the_shared_uplink_helper(self):
        import inspect
        src = inspect.getsource(server.ws_audio_tx)
        self.assertIn("_feed_tx_uplink_frame", src)
        # 解码与 owner 判定不允许在这条端点里再写一遍
        self.assertNotIn("_tx_session_frames += 1", src)
        # 单连接端点走同一个 helper（经二进制分流），不是第二份解码
        self.assertIn("_feed_tx_uplink_frame",
                      inspect.getsource(server._terminal_handle_binary))


class _TerminalTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        s = server
        self._saved = {
            "tokens": set(s._auth_tokens),
            "listen": set(s._listen_tokens),
            "ws_tokens": dict(s._ws_tokens),
            "ctrl": set(s.ctrl_clients),
            "rx": set(s.audio_rx_clients),
            "tx": set(s.audio_tx_clients),
            "spectrum": set(s.spectrum_clients),
            "profiles": dict(s._spectrum_profiles),
            "rx_profiles": dict(s._audio_rx_profiles),
            "tx_profiles": dict(s._audio_tx_profiles),
            "pcm16_encoder": s._pcm16_rx_encoder,
            "terminal": set(s._terminal_ws),
            "terminal_out": dict(s._terminal_out),
            "owner": s._tx_owner_ws,
            "keyer": s._ptt_key_ws,
            "audio": s.audio,
            "cat": s.cat,
            "tx_status": s.radio.tx_status,
            "full_state": s._full_state_message,
            "seq": dict(getattr(s, "_tx_socket_seq", {})),
        }
        s._auth_tokens.clear()
        s._auth_tokens.add("op-token")
        s._listen_tokens.clear()
        s._ws_tokens.clear()
        s.ctrl_clients.clear()
        s.audio_rx_clients.clear()
        s.audio_tx_clients.clear()
        s.spectrum_clients.clear()
        s._spectrum_profiles.clear()
        s._audio_rx_profiles.clear()
        s._audio_tx_profiles.clear()
        s._pcm16_rx_encoder = None
        s._terminal_ws.clear()
        s._terminal_out.clear()
        s._tx_owner_ws = None
        s._ptt_key_ws = None
        s.audio = None
        s.cat = None
        s.radio.update(tx_status=0)
        s._full_state_message = lambda data, channels: {
            "type": "fullState", "data": {},
        }
        seq = getattr(s, "_tx_socket_seq", None)
        if seq is not None:
            seq.clear()

    def tearDown(self):
        s = server
        s._auth_tokens.clear()
        s._auth_tokens.update(self._saved["tokens"])
        s._listen_tokens.clear()
        s._listen_tokens.update(self._saved["listen"])
        s._ws_tokens.clear()
        s._ws_tokens.update(self._saved["ws_tokens"])
        s.ctrl_clients.clear()
        s.ctrl_clients.update(self._saved["ctrl"])
        s.audio_rx_clients.clear()
        s.audio_rx_clients.update(self._saved["rx"])
        s.audio_tx_clients.clear()
        s.audio_tx_clients.update(self._saved["tx"])
        s.spectrum_clients.clear()
        s.spectrum_clients.update(self._saved["spectrum"])
        s._spectrum_profiles.clear()
        s._spectrum_profiles.update(self._saved["profiles"])
        s._audio_rx_profiles.clear()
        s._audio_rx_profiles.update(self._saved["rx_profiles"])
        s._audio_tx_profiles.clear()
        s._audio_tx_profiles.update(self._saved["tx_profiles"])
        s._pcm16_rx_encoder = self._saved["pcm16_encoder"]
        s._terminal_ws.clear()
        s._terminal_ws.update(self._saved["terminal"])
        s._terminal_out.clear()
        s._terminal_out.update(self._saved["terminal_out"])
        s._tx_owner_ws = self._saved["owner"]
        s._ptt_key_ws = self._saved["keyer"]
        s.audio = self._saved["audio"]
        s.cat = self._saved["cat"]
        s.radio.update(tx_status=self._saved["tx_status"])
        s._full_state_message = self._saved["full_state"]
        seq = getattr(s, "_tx_socket_seq", None)
        if seq is not None:
            seq.clear()
            seq.update(self._saved["seq"])

    async def run_terminal(self, ws):
        await server.ws_terminal(ws)


class TerminalConnectTests(_TerminalTestBase):
    async def test_rejected_without_a_valid_token(self):
        ws = _fake_ws(token="nope")
        await self.run_terminal(ws)
        self.assertEqual(ws.closed[0], 4001)
        self.assertNotIn(ws, server._terminal_ws)

    async def test_registers_in_every_registry_and_pushes_full_state(self):
        ws = _fake_ws(token="op-token")
        seen = {}

        def snapshot():
            seen.update({
                "ctrl": ws in server.ctrl_clients,
                "rx": ws in server.audio_rx_clients,
                "tx": ws in server.audio_tx_clients,
                "terminal": ws in server._terminal_ws,
                "token": server._ws_tokens.get(ws),
            })

        ws.on_receive = snapshot
        await self.run_terminal(ws)
        self.assertTrue(ws.accepted)
        self.assertEqual(seen, {"ctrl": True, "rx": True, "tx": True,
                                "terminal": True, "token": "op-token"})
        self.assertIn("fullState", _sent_types(ws))

    async def test_text_frames_go_through_the_wsradio_protocol(self):
        ws = _fake_ws(token="op-token", incoming=[
            {"type": "websocket.receive", "text": '{"type":"ping"}'},
        ])
        await self.run_terminal(ws)
        self.assertIn("pong", _sent_types(ws))

    async def test_disconnect_leaves_no_registry_behind(self):
        ws = _fake_ws(token="op-token")
        await self.run_terminal(ws)
        self.assertNotIn(ws, server.ctrl_clients)
        self.assertNotIn(ws, server.audio_rx_clients)
        self.assertNotIn(ws, server.audio_tx_clients)
        self.assertNotIn(ws, server._terminal_ws)
        self.assertNotIn(ws, server._ws_tokens)


class TerminalDisconnectTests(_TerminalTestBase):
    async def test_every_registry_is_cleaned_and_rx_forced_once(self):
        ws = _fake_ws(token="op-token")
        forced = []

        class _Cat:
            connected = True

            async def set_ptt(self, on):
                forced.append(on)
                return True

        server.ctrl_clients.add(ws)
        server.audio_rx_clients.add(ws)
        server.audio_tx_clients.add(ws)
        server.spectrum_clients.add(ws)
        server._terminal_ws.add(ws)
        server._ws_tokens[ws] = "op-token"
        server._tx_owner_ws = ws
        server._ptt_key_ws = ws
        server.cat = _Cat()
        server.audio = mock.Mock()
        server.radio.update(tx_status=1)

        await server._terminal_disconnect(ws, "operator", "op-token")

        self.assertEqual(forced, [False])          # 一次，不是两次
        self.assertNotIn(ws, server.ctrl_clients)
        self.assertNotIn(ws, server.audio_rx_clients)
        self.assertNotIn(ws, server.audio_tx_clients)
        self.assertNotIn(ws, server.spectrum_clients)
        self.assertNotIn(ws, server._terminal_ws)
        self.assertIsNone(server._tx_owner_ws)
        self.assertIsNone(server._ptt_key_ws)

    async def test_a_plain_rx_disconnect_does_not_touch_ptt(self):
        ws = _fake_ws(token="op-token")
        forced = []

        class _Cat:
            connected = True

            async def set_ptt(self, on):
                forced.append(on)
                return True

        server.cat = _Cat()
        server.radio.update(tx_status=1)
        await server._terminal_disconnect(ws, "operator", "op-token")
        self.assertEqual(forced, [])


class TerminalFanoutTests(_TerminalTestBase):
    """媒体到终端的唯一通道是它自己的有界队列（FP77）。

    为什么不是「短超时就丢帧」：`send_bytes()` 先把整帧写进 transport 再
    `await drain()`，`wait_for(..., 12 ms)` 到点时**帧已经在缓冲区里**，所谓
    「丢帧」只是不再等它 —— 缓冲区继续涨，最后把没有超时的频谱扇出一起拖住，
    卡的是所有客户端。满队列才是真的丢。
    """

    async def test_audio_frames_are_queued_with_the_channel_prefix(self):
        ws = _fake_ws(token="op-token")
        q = asyncio.Queue(maxsize=8)
        server._terminal_out[ws] = q
        dead = await server._send_audio_frames_to_clients([b"\x01opus"], {ws})
        self.assertEqual(dead, set())
        self.assertEqual(q.get_nowait(), b"\x00\x01opus")

    async def test_legacy_socket_keeps_the_byte_for_byte_payload(self):
        ws = _fake_ws(token="op-token")
        await server._send_audio_frames_to_clients([b"\x01opus"], {ws})
        self.assertEqual(ws.bytes, [b"\x01opus"])

    async def test_a_full_queue_drops_the_frame_and_keeps_the_socket(self):
        ws = _fake_ws(token="op-token")
        q = asyncio.Queue(maxsize=1)
        q.put_nowait(b"\x00old")
        server._terminal_out[ws] = q
        before = server._terminal_drops["audio"]
        dead = await server._send_audio_frames_to_clients([b"\x01opus"], {ws})
        self.assertEqual(dead, set())
        self.assertGreater(server._terminal_drops["audio"], before)
        self.assertEqual(q.qsize(), 1)               # 旧帧还在，新的被丢

    async def test_a_stalled_pure_audio_socket_is_still_evicted(self):
        ws = _stuck_ws(token="op-token")
        dead = await server._send_audio_frames_to_clients(
            [b"\x01opus"], {ws}, per_frame_timeout=0.001)
        self.assertEqual(dead, {ws})

    async def test_spectrum_frames_are_queued_with_the_channel_prefix(self):
        ws = _fake_ws(token="op-token")
        q = asyncio.Queue(maxsize=8)
        server._terminal_out[ws] = q
        server.spectrum_clients.add(ws)
        await server._spectrum_fanout({"full": b"\x01" + b"\x00" * 850}, 0)
        frame = q.get_nowait()
        self.assertEqual(frame[0], server.TERMINAL_CHAN_SPECTRUM)
        self.assertEqual(len(frame), 852)

    async def test_a_full_spectrum_queue_drops_and_keeps_the_socket(self):
        ws = _fake_ws(token="op-token")
        q = asyncio.Queue(maxsize=1)
        q.put_nowait(b"old")
        server._terminal_out[ws] = q
        server.spectrum_clients.add(ws)
        before = server._terminal_drops["spectrum"]
        dead = await server._spectrum_fanout({"full": b"\x01" + b"\x00" * 850}, 0)
        self.assertEqual(dead, set())
        self.assertGreater(server._terminal_drops["spectrum"], before)

    async def test_the_pump_writes_queued_frames_in_order(self):
        ws = _fake_ws(token="op-token")
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(b"\x02one")
        q.put_nowait(b"\x02two")
        task = asyncio.create_task(server._terminal_pump(ws, q))
        for _ in range(50):
            if len(ws.bytes) >= 2:
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with self.subTest():
            self.assertEqual(ws.bytes, [b"\x02one", b"\x02two"])

    async def test_the_pump_survives_a_send_error_and_stops(self):
        class _Broken(_FakeWS):
            async def send_bytes(self, data):
                raise RuntimeError("gone")

        ws = _Broken(token="op-token")
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(b"x")
        await server._terminal_pump(ws, q)           # 不抛，直接返回


class TerminalSpectrumAndRoleTests(_TerminalTestBase):
    async def test_no_spectrum_before_the_declaration(self):
        ws = _fake_ws(token="op-token", incoming=[
            {"type": "websocket.receive", "text": '{"type":"ping"}'},
        ])
        await self.run_terminal(ws)
        self.assertNotIn(ws, server.spectrum_clients)

    async def test_declaration_subscribes_and_sets_the_tier(self):
        ws = _fake_ws(token="op-token")
        await server._terminal_handle_text(
            ws, '{"type":"spectrumCaps","profile":"low"}')
        self.assertIn(ws, server.spectrum_clients)
        self.assertEqual(server._spectrum_profiles[ws], "low")

    async def test_unknown_profile_is_ignored(self):
        ws = _fake_ws(token="op-token")
        await server._terminal_handle_text(
            ws, '{"type":"spectrumCaps","profile":"turbo"}')
        self.assertNotIn(ws, server.spectrum_clients)

    async def test_listen_session_cannot_stream_mic_frames(self):
        server._listen_tokens.add("listen-token")
        ws = _fake_ws(token="listen-token")
        before = server._terminal_drops["tx"]
        await server._terminal_handle_binary(ws, b"\x01\x00pcm", listen_only=True)
        self.assertEqual(server._terminal_drops["tx"], before + 1)
        self.assertIn(server.LISTEN_ONLY_MESSAGE, ws.texts[0])

    async def test_unknown_channel_is_counted_not_raised(self):
        ws = _fake_ws(token="op-token")
        before = server._terminal_drops["unknown"]
        await server._terminal_handle_binary(ws, b"\x7f\x00", listen_only=False)
        self.assertEqual(server._terminal_drops["unknown"], before + 1)

    async def test_listen_session_never_registers_an_uplink(self):
        server._listen_tokens.add("listen-token")
        ws = _fake_ws(token="listen-token")
        await self.run_terminal(ws)
        self.assertNotIn(ws, server.audio_tx_clients)


class TerminalPageContractTests(unittest.TestCase):
    """参考页的静态契约（文件在任务 9 落地）。"""

    def test_page_is_not_wired_into_the_guarded_frontend(self):
        js = Path("static/terminal.js").read_text(encoding="utf-8")
        self.assertNotIn("ft710_main.js", js)
        self.assertNotIn("ft710_ui.js", js)

    def test_page_derives_its_base_path(self):
        js = Path("static/terminal.js").read_text(encoding="utf-8")
        self.assertIn("lastIndexOf('/terminal')", js)

    def test_page_talks_to_the_single_socket_endpoint(self):
        js = Path("static/terminal.js").read_text(encoding="utf-8")
        self.assertIn("/WSterminal", js)


class _PageReq:
    def __init__(self, *, listen=False, sdr=False):
        cookies = {}
        if listen:
            cookies["mrrc_auth"] = "listen-token"
        if sdr:
            cookies["mrrc_sdr"] = "sdr-token"
        self.headers = {}
        self.cookies = cookies
        self.query_params = {}


class TerminalPageGateTests(unittest.TestCase):
    def test_listen_session_is_refused(self):
        server._listen_tokens.add("listen-token")
        try:
            self.assertIsNotNone(server._terminal_forbidden(_PageReq(listen=True)))
        finally:
            server._listen_tokens.discard("listen-token")

    def test_operator_session_is_allowed(self):
        self.assertIsNone(server._terminal_forbidden(_PageReq()))


if __name__ == "__main__":
    unittest.main()


class TerminalPageTxWiringTests(unittest.TestCase):
    """页面侧的 TX 封装：`encode_float()` 给的是 ArrayBuffer，不是 typed array。

    2026-11-17 的 FP73 现场：`new Uint8Array(1 + 1 + packet.length)` 里
    `ArrayBuffer.length` 是 undefined ⇒ 目标缓冲 0 字节 ⇒ `set()` 抛
    RangeError ⇒ catch 只加一个丢弃计数。症状是「PTT 键上了，服务端一个字节
    都没收到」，而页面没有任何提示。守卫钉住两件事：按 ArrayBuffer 包装、
    失败要能被看见。
    """

    def setUp(self):
        self.js = Path("static/terminal.js").read_text(encoding="utf-8")

    def test_packet_is_wrapped_as_bytes_not_length_indexed(self):
        body = self.js.split("function sendTxPacket(packet)", 1)[1].split("\n}", 1)[0]
        self.assertIn("packet instanceof ArrayBuffer", body)
        self.assertIn("new Uint8Array(packet)", body)
        # 旧写法：把 ArrayBuffer 当数组用（.length 是 undefined）
        self.assertNotIn("packet.length", body)

    def test_every_frame_carries_channel_and_codec_tag(self):
        body = self.js.split("function sendTxPacket(packet)", 1)[1].split("\n}", 1)[0]
        self.assertIn("CHAN_TX_AUDIO", body)
        self.assertIn("AUDIO_TAG_OPUS", body)
        self.assertIn("body.set(opus, 2)", body)

    def test_encode_failure_is_visible_not_just_counted(self):
        block = self.js.split("txNode.port.onmessage", 1)[1].split("};", 1)[0]
        self.assertIn("showStatus(", block)


class TerminalPageWaterfallRateTests(unittest.TestCase):
    """瀑布的观感 = 画布行数 + 配色/窗口，两样都必须与主界面一致。

    2026-11-17 现场先后两次：先是画布 320 行（同样帧率看着慢 4 倍），再是同
    一份数据两边「差距不小」—— 离线把两条渲染管线画出来对比，左侧几乎全黑
    （噪底 RGB(30,0,0)），右侧噪底亮蓝、载波青绿红。差的是配色与窗函数。
    """

    def setUp(self):
        self.js = Path("static/terminal.js").read_text(encoding="utf-8")
        self.html = Path("static/terminal.html").read_text(encoding="utf-8")

    def test_canvas_has_the_product_row_count(self):
        self.assertIn("const WF_ROWS = 80;", self.js)
        self.assertIn('height="80"', self.html)

    def test_scrolls_up_and_paints_the_new_row_at_the_bottom(self):
        # 与 ft710_ui.js 同方向：新数据在底部
        self.assertIn("drawImage(el.waterfall, 0, 1, w, wfH - 1, 0, 0, w, wfH - 1)", self.js)
        self.assertIn("putImageData(row, 0, wfH - 1)", self.js)

    def test_uses_the_product_palette_and_window(self):
        # jet + scopeFloor/scopeCeil（5..220）—— 不套窗口就只有一片黑
        self.assertIn("function jet(v)", self.js)
        self.assertIn("const SCOPE_FLOOR = 5;", self.js)
        self.assertIn("const SCOPE_CEIL = 220;", self.js)
        self.assertIn("jet(scopeValue(wf1, x, w))", self.js)
        self.assertNotIn("colormap[wf1[i]", self.js)

    def test_fft_line_matches_the_product(self):
        self.assertIn("'#06b6d4'", self.js)                 # 青色描边
        self.assertIn("mapped * 2.0", self.js)             # 2× 幅度

    def test_canvas_height_self_heals_to_the_constant(self):
        self.assertIn("el.waterfall.height = WF_ROWS", self.js)

    def test_the_page_asks_for_a_new_js_build(self):
        self.assertIn("terminal.js?v=4", self.html)


class TerminalSpectrumParityTests(_TerminalTestBase):
    """`/WSterminal` 的频谱与 `/WSspectrum` **同一帧、同一个门**（只是投递方式不同）。

    现场疑虑（2026-11-17）：「/WSterminal 是不是把帧率缩减了？我记得是 30」。
    30 是 `SPECTRUM_BROADCAST_FPS`（循环滴答率），不是数据帧率；真实帧率是电台
    扫描率。两条 socket 都在 `spectrum_clients` 里、走同一个 `frame_due` —— 终端
    只是把帧放进队列由 pump 写，帧数一帧不少。
    """

    async def test_both_kinds_get_every_frame_on_the_same_tick(self):
        plain = _fake_ws(token="op-token")
        terminal = _fake_ws(token="op-token")
        q = asyncio.Queue(maxsize=32)
        server._terminal_out[terminal] = q
        server.spectrum_clients.update({plain, terminal})

        payload = b"\x01" + b"\x00" * 850
        for tick in range(1, 6):
            dead = await server._spectrum_fanout({"full": payload}, tick)
            self.assertEqual(dead, set())

        self.assertEqual(len(plain.bytes), 5)
        self.assertEqual(q.qsize(), 5)
        self.assertEqual(plain.bytes[0], payload)
        self.assertEqual(q.get_nowait(),
                         bytes((server.TERMINAL_CHAN_SPECTRUM,)) + payload)

    async def test_a_lower_tier_throttles_both_kinds_identically(self):
        plain_high = _fake_ws(token="op-token")
        plain_low = _fake_ws(token="op-token")
        terminal_low = _fake_ws(token="op-token")
        q = asyncio.Queue(maxsize=32)
        server._terminal_out[terminal_low] = q
        server.spectrum_clients.update({plain_high, plain_low, terminal_low})
        server._spectrum_profiles[plain_low] = "low"
        server._spectrum_profiles[terminal_low] = "low"

        for tick in range(1, 9):
            await server._spectrum_fanout({"full": b"\x01", "wf1": b"\x02"}, tick)

        self.assertEqual(len(plain_high.bytes), 8)       # divider 1
        self.assertEqual(len(plain_low.bytes), 2)        # divider 4: tick 4, 8
        self.assertEqual(q.qsize(), 2)


class TerminalAudioCapsTests(_TerminalTestBase):
    """ESP32-C3 终端只有两个档位：PCM48 与 PCM16，没有 Opus。

    一条 socket 同时承载 RX/TX，所以一次 ``audioCaps`` 声明必须把两个方向
    一起登记、只回一次应答。专用端点（/WSaudioRX、/WSaudioTX）仍然只认
    pcm16 —— 终端的双档位解析是它自己的契约，不放宽旧端点。
    """

    def _register(self, ws, *, listen=False):
        server.audio_rx_clients.add(ws)
        if not listen:
            server.audio_tx_clients.add(ws)

    def _tx_ok_audio(self):
        audio = mock.Mock()
        audio.tx_audio_available = True
        audio.device_rates = {"rx": 48000, "tx": 48000}
        server.audio = audio
        return audio

    async def test_pcm48_registers_both_directions_and_answers_once(self):
        ws = _fake_ws(token="op-token")
        self._register(ws)
        await server._terminal_handle_text(
            ws, '{"type":"audioCaps","profile":"pcm48"}')
        self.assertEqual(len(ws.texts), 1)
        payload = json.loads(ws.texts[0])
        self.assertEqual(payload["type"], "audioCaps")
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["profile"], "pcm48")
        self.assertEqual(payload["rate"], 48000)
        self.assertEqual(payload["format"], "s16le")
        self.assertEqual(server._audio_rx_profiles.get(ws), "pcm48")
        self.assertEqual(server._audio_tx_profiles.get(ws), "pcm48")

    async def test_pcm16_registers_both_directions_and_answers_once(self):
        self._tx_ok_audio()
        ws = _fake_ws(token="op-token")
        self._register(ws)
        await server._terminal_handle_text(
            ws, '{"type":"audioCaps","profile":"pcm16"}')
        self.assertEqual(len(ws.texts), 1)
        payload = json.loads(ws.texts[0])
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["profile"], "pcm16")
        self.assertEqual(payload["rate"], 16000)
        self.assertEqual(server._audio_rx_profiles.get(ws), "pcm16")
        self.assertEqual(server._audio_tx_profiles.get(ws), "pcm16")

    async def test_pcm16_rejected_when_tx_unsupported(self):
        server.audio = None
        ws = _fake_ws(token="op-token")
        self._register(ws)
        await server._terminal_handle_text(
            ws, '{"type":"audioCaps","profile":"pcm16"}')
        self.assertEqual(len(ws.texts), 1)
        payload = json.loads(ws.texts[0])
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["reason"], "pcm16-tx-unsupported")
        self.assertNotIn(ws, server._audio_rx_profiles)
        self.assertNotIn(ws, server._audio_tx_profiles)

    async def test_unknown_profile_is_rejected_not_silent(self):
        ws = _fake_ws(token="op-token")
        self._register(ws)
        await server._terminal_handle_text(
            ws, '{"type":"audioCaps","profile":"opus"}')
        self.assertEqual(len(ws.texts), 1)
        payload = json.loads(ws.texts[0])
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["reason"], "unknown-profile")
        self.assertNotIn(ws, server._audio_rx_profiles)
        self.assertNotIn(ws, server._audio_tx_profiles)

    async def test_listen_socket_registers_rx_only(self):
        server.audio = None                     # TX 不可用也不影响只听
        for profile in ("pcm48", "pcm16"):
            with self.subTest(profile=profile):
                ws = _fake_ws(token="listen-token")
                self._register(ws, listen=True)
                await server._terminal_handle_text(
                    ws, json.dumps({"type": "audioCaps", "profile": profile}))
                self.assertEqual(len(ws.texts), 1)
                self.assertTrue(json.loads(ws.texts[0])["accepted"])
                self.assertEqual(server._audio_rx_profiles.get(ws), profile)
                self.assertNotIn(ws, server._audio_tx_profiles)

    async def test_declaration_after_cleanup_registers_nothing(self):
        ws = _fake_ws(token="op-token")         # 不在任何注册表里
        await server._terminal_handle_text(
            ws, '{"type":"audioCaps","profile":"pcm48"}')
        self.assertEqual(ws.texts, [])
        self.assertNotIn(ws, server._audio_rx_profiles)
        self.assertNotIn(ws, server._audio_tx_profiles)

    async def test_redeclaration_switches_both_directions(self):
        self._tx_ok_audio()
        ws = _fake_ws(token="op-token")
        self._register(ws)
        await server._terminal_handle_text(
            ws, '{"type":"audioCaps","profile":"pcm48"}')
        await server._terminal_handle_text(
            ws, '{"type":"audioCaps","profile":"pcm16"}')
        self.assertEqual(len(ws.texts), 2)
        self.assertTrue(all(json.loads(t)["accepted"] for t in ws.texts))
        self.assertEqual(server._audio_rx_profiles.get(ws), "pcm16")
        self.assertEqual(server._audio_tx_profiles.get(ws), "pcm16")


class _LoopAudio:
    """够驱动真实 _audio_rx_loop 的最小音频替身（无声卡、无 Opus 库）。"""

    _rx_running = True
    _rx_last_peak = 100
    opus_enabled = True
    tx_audio_available = True
    device_rates = {"rx": 48000, "tx": 48000}

    def __init__(self, block):
        self.block = block
        self.encode_calls = 0

    def read_rx_chunk(self):
        return self.block

    def note_rx_chunk(self, pcm):
        pass

    def encode_rx_audio(self, pcm):
        self.encode_calls += 1
        return [b"\x01opus"]

    def rx_silent_seconds(self):
        return 0.0


class TerminalAudioFanoutTests(_TerminalTestBase):
    """RX 扇出按 socket 声明分三组：pcm16 / pcm48 / legacy。

    关键契约：声明了 pcm48 的终端**永远**收到 0x00 裸 PCM，与全局 libopus
    是否可用无关 —— 这正是 ESP32「没有 Opus 选项」的服务端保证。未声明的
    socket 保持 legacy（Opus 可用时发 Opus），参考页因此不用改。
    这里驱动的是真实 _audio_rx_loop，不是分组函数的替身。
    """

    BLOCK = b"\x01\x00" * 960                   # 20 ms @ 48 kHz

    def setUp(self):
        super().setUp()
        for name, value in (
            ("_ft8_runtime", None),
            ("_local_speaker", None),
            ("scheduler", None),
            ("_rec_session", mock.Mock(active=False)),
            ("_audio_levels", audio_levels.AudioLevels()),
            ("_audio_levels_clip", audio_levels.ClipMonitor()),
        ):
            patcher = mock.patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.audio = _LoopAudio(self.BLOCK)
        server.audio = self.audio

    async def _drive_loop(self, ready, timeout=3.0):
        task = asyncio.create_task(server._audio_rx_loop())
        try:
            deadline = time.monotonic() + timeout
            while not ready():
                if time.monotonic() > deadline:
                    self.fail("RX loop did not produce the expected frames")
                await asyncio.sleep(0.01)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _terminal(self, profile=None):
        ws = _fake_ws(token="op-token")
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        server._terminal_out[ws] = q
        server.audio_rx_clients.add(ws)
        if profile is not None:
            server._audio_rx_profiles[ws] = profile
        return ws, q

    @staticmethod
    def _drain(q):
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        return out

    async def test_pcm48_terminal_gets_raw_pcm_even_with_opus_available(self):
        ws, q = self._terminal("pcm48")
        await self._drive_loop(lambda: not q.empty())
        frames = self._drain(q)
        self.assertTrue(frames)
        for frame in frames:
            self.assertEqual(frame[0], server.TERMINAL_CHAN_RX_AUDIO)
            self.assertEqual(frame[1], server.AUDIO_TAG_PCM)
            self.assertEqual(frame[2:], self.BLOCK * server.RX_DRAIN_MAX_CHUNKS)
        self.assertEqual(self.audio.encode_calls, 0)   # Opus 一次都没跑

    async def test_pcm16_terminal_gets_640_byte_frames(self):
        ws, q = self._terminal("pcm16")
        frames = []

        def ready():
            frames.extend(self._drain(q))
            return any(f[1] == server.AUDIO_TAG_PCM16 for f in frames)
        await self._drive_loop(ready)
        for frame in frames:
            self.assertEqual(frame[0], server.TERMINAL_CHAN_RX_AUDIO)
            self.assertEqual(frame[1], server.AUDIO_TAG_PCM16)
            self.assertEqual(len(frame), 1 + 1 + 640)
        self.assertEqual(self.audio.encode_calls, 0)

    async def test_mixed_groups_each_get_their_own_wire_format(self):
        ws48, q48 = self._terminal("pcm48")
        ws16, q16 = self._terminal("pcm16")
        legacy = _fake_ws(token="op-token")
        server.audio_rx_clients.add(legacy)

        def ready():
            # priming 阶段 PCM16 组不产帧，所以非空即已有 0x02 帧
            return legacy.bytes and not q48.empty() and not q16.empty()
        await self._drive_loop(ready)
        self.assertTrue(all(b == b"\x01opus" for b in legacy.bytes))
        for frame in self._drain(q48):
            self.assertEqual(frame[1], server.AUDIO_TAG_PCM)
        for frame in self._drain(q16):
            self.assertEqual(frame[1], server.AUDIO_TAG_PCM16)
            self.assertEqual(len(frame), 642)
        # 每组每 tick 编码一次，不是每客户端一次
        self.assertGreaterEqual(self.audio.encode_calls, 1)

    async def test_undeclared_terminal_stays_on_legacy_opus(self):
        ws, q = self._terminal()                # 未声明 → legacy
        await self._drive_loop(lambda: not q.empty())
        frames = self._drain(q)
        self.assertTrue(frames)
        for frame in frames:
            self.assertEqual(frame, b"\x00\x01opus")
        self.assertGreaterEqual(self.audio.encode_calls, 1)


class TerminalAudioTxTests(_TerminalTestBase):
    """终端上行：0x00 = PCM48（默认 48 kHz），0x02 = PCM16（input_rate=16000）。

    解码与 owner 判定在共享 helper _feed_tx_uplink_frame 里（已有测试盯住），
    这里钉的是终端二进制分流把正确的 tag/速率交进去，以及 PCM16 转换失败
    时 force-RX 但不关 socket。
    """

    def setUp(self):
        super().setUp()
        server.audio = mock.Mock()
        self.ws = _fake_ws(token="op-token")
        server._tx_owner_ws = self.ws

    async def test_pcm48_uplink_feeds_at_the_default_rate(self):
        payload = b"\x00\x01" * 960
        with mock.patch.object(server, "_feed_tx_from_uplink",
                               new=mock.AsyncMock()) as feed:
            await server._terminal_handle_binary(
                self.ws, b"\x01\x00" + payload, listen_only=False)
        feed.assert_awaited_once_with(payload)

    async def test_pcm16_uplink_feeds_at_16k(self):
        payload = b"\x00\x01" * 320
        with mock.patch.object(server, "_feed_tx_from_uplink",
                               new=mock.AsyncMock()) as feed:
            await server._terminal_handle_binary(
                self.ws, b"\x01\x02" + payload, listen_only=False)
        feed.assert_awaited_once_with(payload, input_rate=16000)

    async def test_pcm16_uplink_failure_forces_rx_and_keeps_the_socket(self):
        forced = []

        class _Cat:
            connected = True

            async def set_ptt(self, on):
                forced.append(on)
                return True

        server.cat = _Cat()
        server._ptt_key_ws = self.ws
        server.radio.update(tx_status=1)
        payload = b"\x00\x01" * 320
        with mock.patch.object(server, "_feed_tx_from_uplink",
                               new=mock.AsyncMock(
                                   side_effect=RuntimeError("src gone"))):
            await server._terminal_handle_binary(
                self.ws, b"\x01\x02" + payload, listen_only=False)
        self.assertEqual(forced, [False])
        self.assertIsNone(server._ptt_key_ws)
        self.assertEqual(server.radio.tx_status, 0)
        self.assertIsNone(self.ws.closed)       # 不关 socket


class TerminalAudioEndToEndTests(_TerminalTestBase):
    async def test_declare_pcm48_then_stream_mic_over_the_same_socket(self):
        server.audio = mock.Mock()
        ws = _fake_ws(token="op-token", incoming=[
            {"type": "websocket.receive",
             "text": '{"type":"audioCaps","profile":"pcm48"}'},
            {"type": "websocket.receive",
             "bytes": b"\x01\x00" + b"\x00\x01" * 960},
        ])
        with mock.patch.object(server, "_feed_tx_from_uplink",
                               new=mock.AsyncMock()) as feed:
            await self.run_terminal(ws)
        caps = [json.loads(t) for t in ws.texts if '"audioCaps"' in t]
        self.assertEqual(len(caps), 1)          # 恰好一次应答
        self.assertTrue(caps[0]["accepted"])
        self.assertEqual(caps[0]["rate"], 48000)
        feed.assert_awaited_once()              # 0x00 上行真的进了共享 helper
        # 断连清理把两个方向都带走了
        self.assertNotIn(ws, server._audio_rx_profiles)
        self.assertNotIn(ws, server._audio_tx_profiles)


class TerminalAudioCleanupTests(_TerminalTestBase):
    async def test_profiles_and_encoder_die_with_the_socket(self):
        ws = _fake_ws(token="op-token")
        server.audio_rx_clients.add(ws)
        server.audio_tx_clients.add(ws)
        server._audio_rx_profiles[ws] = "pcm16"
        server._audio_tx_profiles[ws] = "pcm16"
        encoder = mock.Mock()
        server._pcm16_rx_encoder = encoder
        await server._terminal_disconnect(ws, "operator", "op-token")
        self.assertNotIn(ws, server._audio_rx_profiles)
        self.assertNotIn(ws, server._audio_tx_profiles)
        encoder.reset.assert_called_once_with()
        self.assertIsNone(server._pcm16_rx_encoder)

    async def test_profiles_dropped_even_when_a_later_await_raises(self):
        ws = _fake_ws(token="op-token")
        server.audio_tx_clients.add(ws)
        server._audio_rx_profiles[ws] = "pcm48"
        server._audio_tx_profiles[ws] = "pcm48"
        server._tx_owner_ws = ws
        server.audio = mock.Mock()
        server.audio.stop_tx.side_effect = RuntimeError("teardown cut short")
        with self.assertRaises(RuntimeError):
            await server._terminal_disconnect(ws, "operator", "op-token")
        self.assertNotIn(ws, server._audio_rx_profiles)
        self.assertNotIn(ws, server._audio_tx_profiles)

    async def test_two_terminals_keep_independent_profiles(self):
        ws1 = _fake_ws(token="op-token")
        ws2 = _fake_ws(token="op-token")
        for ws in (ws1, ws2):
            server.audio_rx_clients.add(ws)
            server.audio_tx_clients.add(ws)
        server._audio_rx_profiles[ws1] = "pcm48"
        server._audio_tx_profiles[ws1] = "pcm48"
        server._audio_rx_profiles[ws2] = "pcm16"
        server._audio_tx_profiles[ws2] = "pcm16"
        encoder = mock.Mock()
        server._pcm16_rx_encoder = encoder
        await server._terminal_disconnect(ws1, "operator", "op-token")
        self.assertNotIn(ws1, server._audio_rx_profiles)
        self.assertNotIn(ws1, server._audio_tx_profiles)
        self.assertEqual(server._audio_rx_profiles.get(ws2), "pcm16")
        self.assertEqual(server._audio_tx_profiles.get(ws2), "pcm16")
        encoder.reset.assert_not_called()       # pcm16 组还有人
        self.assertIs(server._pcm16_rx_encoder, encoder)
