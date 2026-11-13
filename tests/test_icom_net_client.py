"""Icom network client against a scripted radio (no hardware, real UDP).

The fake radio below implements the *radio side* of the handshake exactly as the
design record describes it: answer are-you-there, offer "I am ready", answer the
login, accept the token, list its capabilities, say who owns it, and — once the
client asks for a stream — start pushing 16-bit PCM at the announced port.

That does not prove the protocol is right (only a real radio can), but it does
prove our side's *sequence*: what we send, in which order, with which ids and
token, and that audio that arrives ends up at the callback with the right shape.
"""
import asyncio
import contextlib
import struct
import unittest

from backends.icom_net import protocol as proto
from backends.icom_net.client import IcomNetworkClient, LoginFailed, RadioBusy


class FakeRadio:
    """A scripted Icom-networked radio on loopback."""

    def __init__(self, *, busy=False, bad_password=False, client_name_owner="LAPTOP"):
        self.busy = busy
        self.bad_password = bad_password
        self.owner = client_name_owner
        self.port = 0
        self.civ_port = 0
        self.audio_port = 0
        self.transport: "asyncio.DatagramTransport | None" = None
        self.civ_transport = None
        self.audio_transport = None
        self.civ_addr = None
        self.audio_addr = None
        self.client_civ_port = 0
        self.client_audio_port = 0
        self.client_id = 0
        self.client_addr = None
        self.tok_request = 0
        self.radio_id = 0x0A0B0C0D
        self.stream_requested = False
        self.logins = 0
        self.tokens = 0
        #: CI-V frames the client pushed through the tunnel, and what we answered.
        self.civ_received: list[bytes] = []
        self.civ_answers = 0
        #: The ports the "radio" announces — real bound sockets, because the
        #: client now opens one socket per stream and puts data on the port the
        #: stream request announced.
        self._civ_seq = 0
        self._audio_task = None
        self._sequence = 0

    # ── radio side ────────────────────────────────────────────────

    async def start(self):
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: _RadioProtocol(self, "control"), local_addr=("127.0.0.1", 0))
        self.port = self.transport.get_extra_info("sockname")[1]
        self.civ_transport, _ = await loop.create_datagram_endpoint(
            lambda: _RadioProtocol(self, "civ"), local_addr=("127.0.0.1", 0))
        self.civ_port = self.civ_transport.get_extra_info("sockname")[1]
        self.audio_transport, _ = await loop.create_datagram_endpoint(
            lambda: _RadioProtocol(self, "audio"), local_addr=("127.0.0.1", 0))
        self.audio_port = self.audio_transport.get_extra_info("sockname")[1]
        return self.port

    def close(self):
        if self._audio_task:
            self._audio_task.cancel()
        for t in (self.transport, self.civ_transport, self.audio_transport):
            if t:
                t.close()

    def _send(self, payload, addr=None):
        assert self.transport is not None
        self.transport.sendto(payload, addr or self.client_addr)

    def on_datagram(self, data, addr, kind="control"):
        if kind == "civ":
            self.civ_addr = addr
            if len(data) == 16:
                self._sub_control(data, addr, kind)
                return
            self._handle_civ(data, addr)
            return
        if kind == "audio":
            self.audio_addr = addr
            if len(data) == 16:
                self._sub_control(data, addr, kind)
            return
        self.client_addr = addr
        header = proto.parse_header(data)
        if header is None:
            return
        self.client_id = header.sent_id
        if proto.looks_like_civ(data):
            self._handle_civ(data, addr)
            return
        if header.length == proto.CONTROL_SIZE:
            if header.type == proto.T_ARE_YOU_THERE:
                self._send(proto.build_control(proto.T_I_AM_HERE, seq=0,
                                               sent_id=self.radio_id,
                                               rcvd_id=self.client_id))
                self._send(proto.build_control(proto.T_I_AM_READY, seq=1,
                                               sent_id=self.radio_id,
                                               rcvd_id=self.client_id))
        elif header.length == proto.LOGIN_SIZE:
            (self.tok_request,) = struct.unpack("<H", data[0x1A:0x1C])
            self.logins += 1
            reply = bytearray(proto.LOGIN_RESPONSE_SIZE)
            struct.pack_into("<IHHII", reply, 0, proto.LOGIN_RESPONSE_SIZE,
                             0x01, 0, self.radio_id, self.client_id)
            struct.pack_into("<H", reply, 0x1A, self.tok_request)
            struct.pack_into("<I", reply, 0x1C, 0xCAFEBABE)      # token
            struct.pack_into("<i", reply, 0x30,
                             -0x01000001 if self.bad_password else 0)
            reply[0x40:0x46] = b"WFVIEW"
            self._send(bytes(reply))
        elif header.length == proto.TOKEN_SIZE:
            self.tokens += 1
            # The field radio sends **no** token reply (measured 2026-11-12):
            # it answers the token request with capabilities + conninfo and
            # considers the stream request the next step.  Answering with a
            # token reply here made the client depend on one that never comes.
            if data[0x15] == proto.REQ_TOKEN:
                self._send(self._capabilities())
                self._send(self._conninfo())
        elif header.length == proto.CONNINFO_SIZE:
            if data[0x15] == proto.REQ_STREAM:
                self.stream_requested = True
                (self.client_civ_port,) = struct.unpack(">I", data[0x7C:0x80])
                (self.client_audio_port,) = struct.unpack(">I", data[0x80:0x84])
                status = bytearray(proto.STATUS_SIZE)
                struct.pack_into("<IHHII", status, 0, proto.STATUS_SIZE, 0x01,
                                 0, self.radio_id, self.client_id)
                struct.pack_into("<i", status, 0x30, 0)
                struct.pack_into(">H", status, 0x42, self.civ_port)
                struct.pack_into(">H", status, 0x46, self.audio_port)
                self._send(bytes(status))
                self._audio_task = asyncio.create_task(self._stream_audio())

    def _send_civ(self, frame: bytes, addr=None):
        """Answer on the CI-V tunnel (same wire format as the client's)."""
        assert self.civ_transport is not None
        self._civ_seq = (self._civ_seq + 1) & 0xFFFF
        self.civ_answers += 1
        self.civ_transport.sendto(proto.build_civ_data(
            frame, seq=self._civ_seq, sent_id=self.radio_id,
            rcvd_id=self.client_id, send_seq=self._civ_seq),
            addr or self.civ_addr or ("127.0.0.1", self.client_civ_port))

    def _handle_civ(self, data, addr):
        if not proto.looks_like_civ(data):
            return                      # open/close or an idle packet
        frame = proto.parse_civ_data(data)
        if not frame:
            return
        self.civ_received.append(frame)
        # A real radio answers `03` (read frequency) and refuses `27 00` on the
        # USB side — we answer both, so a test can tell the two apart.
        # FE FE <radio> <us> <cmd> FD  — the radio's own address is frame[2]
        if len(frame) >= 5 and frame[2] in (0x94, 0xA0):
            cmd = frame[4]
            if cmd == 0x03:
                # The real shape, copied from the field log: five BCD bytes,
                # LSB first (`FE FE E0 A0 03 00 00 27 14 00 FD` = 14.270 MHz).
                # Four bytes here looked like a frequency read but the CI-V
                # parser correctly refused the frame as malformed.
                # Direction matters: `FE FE <to> <from>`, and the reply goes
                # TO the controller (0xE0) FROM the radio (0x94) — the field
                # frame is `FE FE E0 A0 03 …` for the same reason. Reversed,
                # CivController's demux never matches it to the pending query.
                self._send_civ(bytes.fromhex("FEFEE094") + bytes([cmd])
                               + bytes.fromhex("0000271400FD"), addr)
            elif cmd == 0x27 and len(frame) > 5 and frame[5] == 0x00:
                self._send_civ(bytes.fromhex("FEFEE0942700010100FD"), addr)

    def _sub_control(self, data, addr, kind):
        """Sub-stream handshake (measured: the radio answers it per socket)."""
        header = proto.parse_header(data)
        if header is None:
            return
        if header.type == proto.T_ARE_YOU_THERE:
            self.sub_hellos = getattr(self, "sub_hellos", {})
            self.sub_hellos[kind] = self.sub_hellos.get(kind, 0) + 1
            reply = proto.build_control(proto.T_I_AM_HERE, seq=0,
                                        sent_id=self.radio_id,
                                        rcvd_id=header.sent_id)
            transport = self.civ_transport if kind == "civ" else self.audio_transport
            transport.sendto(reply, addr)

    def _capabilities(self):
        body = bytearray(proto.CAPABILITIES_HEADER_SIZE + proto.RADIO_CAP_SIZE)
        struct.pack_into("<IHHII", body, 0, len(body), 0x01, 0, self.radio_id,
                         self.client_id)
        struct.pack_into(">H", body, 0x40, 1)
        off = proto.CAPABILITIES_HEADER_SIZE
        body[off + 0x07:off + 0x09] = struct.pack(">H", 0x8010)
        body[off + 0x10:off + 0x18] = b"IC-7300\x00"
        body[off + 0x30:off + 0x3A] = b"ICOM_VAUDIO"
        body[off + 0x52] = 0xA0
        body[off + 0x53:off + 0x55] = struct.pack(">H", 16000)
        body[off + 0x55:off + 0x57] = struct.pack(">H", 16000)
        return bytes(body)

    def _conninfo(self):
        body = bytearray(proto.CONNINFO_SIZE)
        struct.pack_into("<IHHII", body, 0, proto.CONNINFO_SIZE, 0x01, 0, 
                         self.radio_id, self.client_id)
        struct.pack_into("<I", body, 0x60, 1 if self.busy else 0)
        if self.busy:
            body[0x64:0x64 + len(self.owner)] = self.owner.encode()
        return bytes(body)

    async def _stream_audio(self):
        """Push 20 ms of a 700 Hz tone at 16 kHz, forever (the radio's job)."""
        import numpy as np
        # Faithful to the field radio: nothing arrives on the audio socket until
        # that socket has been through its own are-you-there handshake.
        while not getattr(self, "sub_hellos", {}).get("audio"):
            await asyncio.sleep(0.02)
        phase = 0.0
        while True:
            n = 320
            t = np.arange(phase, phase + n) / 16000.0
            phase += n
            tone = (np.sin(2 * np.pi * 700 * t) * 8000).astype("<i2").tobytes()
            self._sequence += 1
            header = bytearray(proto.AUDIO_HEADER_SIZE)
            struct.pack_into("<IHHII", header, 0, proto.AUDIO_HEADER_SIZE + len(tone),
                             0x00, self._sequence & 0xFFFF, self.radio_id,
                             self.client_id)
            struct.pack_into("<H", header, 0x10, 0x0000)
            struct.pack_into("<H", header, 0x12, self._sequence & 0xFFFF)
            struct.pack_into(">H", header, 0x16, len(tone))
            assert self.transport is not None
            assert self.audio_transport is not None
            self.audio_transport.sendto(bytes(header) + tone,
                                        ("127.0.0.1", self.client_audio_port))
            await asyncio.sleep(0.02)


class _RadioProtocol(asyncio.DatagramProtocol):
    def __init__(self, radio: FakeRadio, kind: str = "control"):
        self.radio = radio
        self.kind = kind

    def datagram_received(self, data, addr):
        self.radio.on_datagram(data, addr, self.kind)


class HandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, radio_kwargs=None, expect_audio=8):
        radio = FakeRadio(**(radio_kwargs or {}))
        await radio.start()
        chunks: list[bytes] = []
        client = IcomNetworkClient(
            "127.0.0.1", "G7250", "beta_tester",
            on_audio=lambda pcm, rate: chunks.append(pcm),
            control_port=radio.port,
            are_you_there_period=0.02,
            idle_period=0.02,
            ping_period=0.05,
        )
        try:
            await client.start()
            for _ in range(400):
                if len(chunks) >= expect_audio:
                    break
                await asyncio.sleep(0.02)
            # Capture the live state before the teardown in `finally` sets it
            # back to "stopped".
            return client, radio, chunks, client.stats.state, client.stats.audio_packets
        finally:
            await client.stop()
            radio.close()

    async def test_the_whole_handshake_ends_in_audio(self):
        client, radio, chunks, state, packets = await self._run()
        self.assertTrue(radio.stream_requested, "客户端没有请求串流")
        self.assertEqual(radio.logins, 1)
        self.assertGreaterEqual(radio.tokens, 1)
        self.assertTrue(chunks, "握手走完了却没有音频")
        self.assertEqual(state, "streaming")
        self.assertGreaterEqual(packets, 8)
        self.assertEqual(client.stats.radio_name, "IC-7300")
        self.assertEqual(client.stats.radio_civ_address, 0xA0)

    async def test_the_audio_payload_is_pcm_at_the_announced_rate(self):
        import numpy as np
        _client, _radio, chunks, _state, _n = await self._run()
        pcm = b"".join(chunks[:10])
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        self.assertGreater(samples.size, 1000)
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
        peak_hz = int(np.argmax(spectrum) * 16000 / samples.size)
        self.assertAlmostEqual(peak_hz, 700, delta=25)

    async def test_a_bad_password_stops_the_handshake(self):
        radio = FakeRadio(bad_password=True)
        await radio.start()
        client = IcomNetworkClient(
            "127.0.0.1", "G7250", "wrong", control_port=radio.port,
            are_you_there_period=0.02, idle_period=0.02)
        errors: list[Exception] = []
        try:
            await client.start()
            for _ in range(200):
                if client.stats.logins and client.stats.state != "logging-in":
                    break
                await asyncio.sleep(0.02)
        finally:
            await client.stop()
            radio.close()
        # The client does not raise inside the protocol callback; the state
        # stays short of "streaming" and no audio is ever requested.
        self.assertFalse(radio.stream_requested)
        self.assertNotEqual(client.stats.state, "streaming")
        self.assertEqual(errors, [])

    async def test_a_busy_radio_is_not_taken_over(self):
        radio = FakeRadio(busy=True, client_name_owner="SOMEONE-ELSE")
        await radio.start()
        client = IcomNetworkClient(
            "127.0.0.1", "G7250", "beta_tester", control_port=radio.port,
            are_you_there_period=0.02, idle_period=0.02)
        try:
            await client.start()
            for _ in range(200):
                if client.stats.logins:
                    await asyncio.sleep(0.2)
                    break
                await asyncio.sleep(0.02)
        finally:
            await client.stop()
            radio.close()
        self.assertFalse(radio.stream_requested, "别人占着电台时不该请求串流")


if __name__ == "__main__":
    unittest.main()


class DeadHandshakeTests(unittest.IsolatedAsyncioTestCase):
    """A radio that answers once and then goes away must not wedge the link.

    Field 2026-11-13 (BG7YQV, G7250 on its LAN port): the radio's own LAN server
    went down mid-session.  Our watchdog only watched ``audio_age_s`` — which
    does not exist until a stream was asked for — and the branch for
    "handshake not finished" was a bare ``continue``.  The log stopped at
    "radio answered" and stayed there for 90+ s: no retry, no warning, and the
    UI kept showing the last frequency from the stale cache.
    """

    async def test_a_stuck_handshake_is_retried_and_reported(self):
        import asyncio
        import time

        client = IcomNetworkClient("127.0.0.1", "G7250", "x",
                                   control_port=50001, login_timeout=0.4)
        resets = []
        client._reset_link = lambda: resets.append(time.monotonic())
        client._running = True
        client._stream_asked = False
        client._handshake_at = time.monotonic() - 5.0
        task = asyncio.create_task(client._watchdog_loop())
        try:
            with self.assertLogs("icom.net", level="WARNING") as logs:
                await asyncio.sleep(2.3)
        finally:
            client._running = False
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        self.assertGreaterEqual(len(resets), 2, "a stuck handshake must be retried")
        stuck = [line for line in logs.output if "no handshake" in line]
        self.assertEqual(len(stuck), 1, "one line a minute, not one per retry")
        self.assertIn("127.0.0.1:50001", stuck[0])

    async def test_a_healthy_handshake_is_left_alone(self):
        """Progress toward a stream must not be mistaken for a dead radio."""
        import asyncio
        import time

        client = IcomNetworkClient("127.0.0.1", "G7250", "x",
                                   control_port=50001, login_timeout=0.4)
        resets = []
        client._reset_link = lambda: resets.append(True)
        client._running = True
        client._stream_asked = False
        client._handshake_at = time.monotonic()          # just started
        task = asyncio.create_task(client._watchdog_loop())
        try:
            await asyncio.sleep(0.3)                     # inside the window
        finally:
            client._running = False
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.assertEqual(resets, [], "a young handshake was reset too early")


class CivTunnelTests(unittest.IsolatedAsyncioTestCase):
    """CI-V over the network tunnel — the path that may replace USB CAT.

    The field unit ignores `0x27` on its USB port while wfview's waterfall comes
    from this tunnel, so "does the tunnel carry CI-V at all" has to be testable
    without the radio.  The demultiplexer is the delicate part: `T_CIV_DATA` is
    the *same* byte as login/token/conninfo, so a type-only check eats the
    handshake — that is what the `conninfo` step here guards.
    """

    async def _streaming_client(self):
        radio = FakeRadio()
        await radio.start()
        frames: list[bytes] = []
        client = IcomNetworkClient(
            "127.0.0.1", "G7250", "beta_tester",
            on_audio=lambda pcm, rate: None,
            control_port=radio.port, are_you_there_period=0.02, idle_period=0.02)
        client.on_civ = frames.append
        await client.start()
        for _ in range(200):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        self.addAsyncCleanup(self._close, client, radio)
        return client, radio, frames

    async def _close(self, client, radio):
        await client.stop()
        radio.close()

    async def test_the_tunnel_is_opened_and_carries_a_read(self):
        client, radio, frames = await self._streaming_client()
        self.assertTrue(client.civ_ready, "电台报了 CI-V 端口，客户端却没准备好")
        client.open_civ()
        self.assertTrue(client.send_civ(bytes.fromhex("FEFE94E003FD")))
        for _ in range(100):
            if frames:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(radio.civ_received, "隧道没有收到我们的帧")
        self.assertEqual(radio.civ_received[0].hex(), "fefe94e003fd")
        self.assertTrue(frames, "电台的应答没有回到 on_civ")
        # The answer is addressed TO us: FE FE E0 94 …
        self.assertTrue(frames[0].startswith(bytes.fromhex("FEFEE094")))

    async def test_the_handshake_survives_a_civ_looking_type_byte(self):
        """Login/token/conninfo all use type 0x01 — the demux must not steal them."""
        client, radio, _frames = await self._streaming_client()
        self.assertTrue(radio.stream_requested, "握手被 CI-V 的判断吃掉了")
        self.assertGreaterEqual(radio.tokens, 1)

    async def test_sending_before_the_stream_is_refused_not_crashed(self):
        client = IcomNetworkClient("127.0.0.1", "G7250", "x",
                                   control_port=1, are_you_there_period=0.02)
        self.assertFalse(client.send_civ(bytes.fromhex("FEFE94E003FD")))


class NoTokenReplyTests(unittest.IsolatedAsyncioTestCase):
    """The radio never answers the token request — the session must not stall.

    Field log 2026-11-12 (1.28.54): `logged in` at 33.395, capabilities at
    33.423, then **nothing** for ten seconds, then "did not open a stream".
    The client was waiting for a token reply that the radio does not send, so
    the stream request was never issued.  The token request is a credential,
    not a request/response.
    """

    async def test_a_stream_is_requested_without_any_token_reply(self):
        radio = FakeRadio()
        await radio.start()
        client = IcomNetworkClient(
            "127.0.0.1", "G7250", "beta_tester",
            on_audio=lambda pcm, rate: None,
            control_port=radio.port, are_you_there_period=0.02, idle_period=0.02)
        try:
            await client.start()
            for _ in range(200):
                if client.civ_port:
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(radio.stream_requested, "电台没收到串流请求")
            self.assertGreaterEqual(radio.tokens, 1)
            self.assertTrue(client.connected)
        finally:
            await client.stop()
            radio.close()

    async def test_the_civ_port_comes_from_the_status_reply(self):
        radio = FakeRadio()
        await radio.start()
        client = IcomNetworkClient(
            "127.0.0.1", "G7250", "beta_tester", control_port=radio.port,
            are_you_there_period=0.02, idle_period=0.02)
        try:
            await client.start()
            for _ in range(200):
                if client.civ_port:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(client.civ_port, radio.civ_port)
            self.assertEqual(client.audio_port, radio.audio_port)
            self.assertEqual(client.civ_local_port,
                             radio.client_civ_port)
            self.assertEqual(client.audio_local_port,
                             radio.client_audio_port)
        finally:
            await client.stop()
            radio.close()


class SubStreamHandshakeTests(unittest.IsolatedAsyncioTestCase):
    """子流 socket 也要跑一遍 are-you-there，否则电台不往这两个端口发东西。

    现场（2026-11-12，1.28.57 诊断包）：CAT 全通（`model ID 00 A0`、`7230000 Hz`
    都是经隧道读到的），但 `no audio for 16 s` 且瀑布一直 stalled —— 音频与
    频谱**一个包都没到**。对照那个能收到 664 字节音频包和 518 字节瀑布帧的
    现场探测器，差别只有一条：它在 civ/audio 两个 socket 上各发了几次
    `are you there`，收到 `I am here` 后回 `I am ready`。
    """

    async def test_both_sub_streams_are_announced(self):
        radio = FakeRadio()
        await radio.start()
        client = IcomNetworkClient(
            "127.0.0.1", "G7250", "beta_tester",
            on_audio=lambda pcm, rate: None,
            control_port=radio.port, are_you_there_period=0.02, idle_period=0.02)
        try:
            await client.start()
            for _ in range(300):
                hellos = getattr(radio, "sub_hellos", {})
                if len(hellos) >= 2 and client._sub_data["audio"]:
                    break
                await asyncio.sleep(0.02)
            hellos = getattr(radio, "sub_hellos", {})
            self.assertIn("civ", hellos, "civ 子流没发 are-you-there")
            self.assertIn("audio", hellos, "audio 子流没发 are-you-there")
            self.assertTrue(client._sub_data["audio"], "音频包没到")
            # CI-V only flows when we ask something (polls), so the flag stays
            # False until the first query — the handshake is what this test is
            # about, and it is visible in the radio's counters above.
            self.assertTrue(client._sub_ready["civ"], "civ 子流没完成握手")
        finally:
            await client.stop()
            radio.close()
