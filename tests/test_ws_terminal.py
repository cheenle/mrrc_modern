"""单连接终端（/WSterminal）：帧格式、分流、生命周期、扇出策略。

设计：docs/superpowers/specs/2026-11-17-ws-terminal-single-connection-design.md
全部用例都不需要电台或声卡：handler 用假 socket 驱动，扇出直接调真实函数。
"""
import asyncio
import json
import unittest
from pathlib import Path
from typing import Any, Callable, Optional
from unittest import mock

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

    def test_prefix_is_empty_for_legacy_sockets(self):
        self.assertEqual(server._terminal_prefix(object(), 0x00), b"")


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
            "terminal": set(s._terminal_ws),
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
        s._terminal_ws.clear()
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
        s._terminal_ws.clear()
        s._terminal_ws.update(self._saved["terminal"])
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
    async def test_audio_frames_get_the_channel_prefix(self):
        ws = _fake_ws(token="op-token")
        server._terminal_ws.add(ws)
        dead = await server._send_audio_frames_to_clients([b"\x01opus"], {ws})
        self.assertEqual(dead, set())
        self.assertEqual(ws.bytes, [b"\x00\x01opus"])

    async def test_legacy_sockets_keep_the_byte_for_byte_payload(self):
        ws = _fake_ws(token="op-token")
        await server._send_audio_frames_to_clients([b"\x01opus"], {ws})
        self.assertEqual(ws.bytes, [b"\x01opus"])

    async def test_backpressure_drops_frames_not_the_terminal_socket(self):
        ws = _stuck_ws(token="op-token")
        server._terminal_ws.add(ws)
        before = server._terminal_drops["audio"]
        dead = await server._send_audio_frames_to_clients(
            [b"\x01opus"], {ws}, per_frame_timeout=0.001)
        self.assertEqual(dead, set())
        self.assertGreater(server._terminal_drops["audio"], before)

    async def test_backpressure_still_evicts_a_pure_audio_socket(self):
        ws = _stuck_ws(token="op-token")
        dead = await server._send_audio_frames_to_clients(
            [b"\x01opus"], {ws}, per_frame_timeout=0.001)
        self.assertEqual(dead, {ws})

    async def test_spectrum_frames_carry_the_channel_prefix(self):
        ws = _fake_ws(token="op-token")
        server._terminal_ws.add(ws)
        server.spectrum_clients.add(ws)
        await server._spectrum_fanout({"full": b"\x01" + b"\x00" * 850}, 0)
        self.assertEqual(ws.bytes[0][0], server.TERMINAL_CHAN_SPECTRUM)
        # 通道字节 + 旧帧（1 字节版本 + 850 字节 wf1）
        self.assertEqual(len(ws.bytes[0]), 852)

    async def test_spectrum_backpressure_keeps_the_terminal_socket(self):
        class _Broken(_FakeWS):
            async def send_bytes(self, data):
                raise RuntimeError("backpressure")

        ws: Any = _Broken(token="op-token")
        server._terminal_ws.add(ws)
        server.spectrum_clients.add(ws)
        before = server._terminal_drops["spectrum"]
        dead = await server._spectrum_fanout({"full": b"\x01" + b"\x00" * 850}, 0)
        self.assertEqual(dead, set())
        self.assertGreater(server._terminal_drops["spectrum"], before)

    async def test_spectrum_failure_still_evicts_a_pure_spectrum_socket(self):
        class _Broken(_FakeWS):
            async def send_bytes(self, data):
                raise RuntimeError("gone")

        ws: Any = _Broken(token="op-token")
        server.spectrum_clients.add(ws)
        dead = await server._spectrum_fanout({"full": b"\x01" + b"\x00" * 850}, 0)
        self.assertEqual(dead, {ws})


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
    """瀑布的滚动速率与数据帧率解耦（FP75）。

    电台一个扫描帧 190 ms（5.3 帧/秒），一行一帧就是每秒 5 行 —— 看着是一顿一顿
    地走。把同一帧重复画几行，显示速率回到 15 行/秒：不重发字节，只是每行代表的
    真实时间变短。
    """

    def setUp(self):
        self.js = Path("static/terminal.js").read_text(encoding="utf-8")
        self.html = Path("static/terminal.html").read_text(encoding="utf-8")

    def test_target_rate_is_15_rows_per_second(self):
        self.assertIn("const WF_TARGET_ROWS_PER_S = 15;", self.js)

    def test_scroll_is_row_counted_not_a_hardcoded_single_row(self):
        self.assertIn("drawImage(el.waterfall, 0, rows)", self.js)
        self.assertNotIn("drawImage(el.waterfall, 0, 1)", self.js)

    def test_the_same_row_is_painted_for_every_scrolled_row(self):
        self.assertIn("for (let i = 0; i < rows; i++) wfCtx.putImageData(row, 0, i);",
                      self.js)

    def test_gap_is_clamped_so_a_hidden_tab_cannot_jump(self):
        self.assertIn("WF_MAX_ROWS_PER_FRAME", self.js)
        self.assertIn("Math.max(1, Math.min(WF_MAX_ROWS_PER_FRAME, rows))", self.js)

    def test_the_page_asks_for_a_new_js_build(self):
        """?v= 是手工 bump 的：不改它，浏览器会把旧 terminal.js 缓存一小时。"""
        self.assertIn("terminal.js?v=2", self.html)
