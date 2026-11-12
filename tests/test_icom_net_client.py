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
        self.transport: "asyncio.DatagramTransport | None" = None
        self.client_id = 0
        self.client_addr = None
        self.tok_request = 0
        self.radio_id = 0x0A0B0C0D
        self.civ_port = 0
        self.audio_port = 0
        self.stream_requested = False
        self.logins = 0
        self.tokens = 0
        #: CI-V frames the client pushed through the tunnel, and what we answered.
        self.civ_received: list[bytes] = []
        self.civ_answers = 0
        #: The announced CI-V port. The fake radio listens on ONE socket, so a
        #: test that exercises the tunnel rewrites this to its own port.
        self.announced_civ_port = 50002
        self.announced_audio_port = 50003
        self._civ_seq = 0
        self._audio_task = None
        self._sequence = 0

    # ── radio side ────────────────────────────────────────────────

    async def start(self):
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: _RadioProtocol(self), local_addr=("127.0.0.1", 0))
        self.port = self.transport.get_extra_info("sockname")[1]
        return self.port

    def close(self):
        if self._audio_task:
            self._audio_task.cancel()
        if self.transport:
            self.transport.close()

    def _send(self, payload, addr=None):
        assert self.transport is not None
        self.transport.sendto(payload, addr or self.client_addr)

    def on_datagram(self, data, addr):
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
            reply = bytearray(proto.TOKEN_SIZE)
            struct.pack_into("<IHHII", reply, 0, proto.TOKEN_SIZE, 0x01, 0,
                             self.radio_id, self.client_id)
            reply[0x14] = 0x02
            reply[0x15] = data[0x15]
            struct.pack_into("<i", reply, 0x30, 0)
            self._send(bytes(reply))
            if data[0x15] == proto.REQ_TOKEN:
                self._send(self._capabilities())
                self._send(self._conninfo())
        elif header.length == proto.CONNINFO_SIZE:
            if data[0x15] == proto.REQ_STREAM:
                self.stream_requested = True
                (self.civ_port,) = struct.unpack(">I", data[0x7C:0x80])
                (self._client_audio_port,) = struct.unpack(">I", data[0x80:0x84])
                status = bytearray(proto.STATUS_SIZE)
                struct.pack_into("<IHHII", status, 0, proto.STATUS_SIZE, 0x01,
                                 0, self.radio_id, self.client_id)
                struct.pack_into("<i", status, 0x30, 0)
                struct.pack_into(">I", status, 0x42, self.announced_civ_port)
                struct.pack_into(">I", status, 0x46, self.announced_audio_port)
                self._send(bytes(status))
                self._audio_task = asyncio.create_task(self._stream_audio())

    def _send_civ(self, frame: bytes, addr=None):
        """Answer on the CI-V tunnel (same wire format as the client's)."""
        assert self.transport is not None
        self._civ_seq = (self._civ_seq + 1) & 0xFFFF
        self.civ_answers += 1
        self.transport.sendto(proto.build_civ_data(
            frame, seq=self._civ_seq, sent_id=self.radio_id,
            rcvd_id=self.client_id, send_seq=self._civ_seq), addr or self.client_addr)

    def _handle_civ(self, data, addr):
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
                self._send_civ(bytes.fromhex("FEFE94E0") + bytes([cmd])
                               + bytes.fromhex("00502100FD"), addr)
            elif cmd == 0x27 and len(frame) > 5 and frame[5] == 0x00:
                self._send_civ(bytes.fromhex("FEFE94E027000101FD"), addr)

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
            self.transport.sendto(bytes(header) + tone,
                                  ("127.0.0.1", self._client_audio_port))
            await asyncio.sleep(0.02)


class _RadioProtocol(asyncio.DatagramProtocol):
    def __init__(self, radio: FakeRadio):
        self.radio = radio

    def datagram_received(self, data, addr):
        self.radio.on_datagram(data, addr)


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
        radio.announced_civ_port = radio.port          # one socket in the fake
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
        await client.open_civ()
        self.assertTrue(client.send_civ(bytes.fromhex("FEFE94E003FD")))
        for _ in range(100):
            if frames:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(radio.civ_received, "隧道没有收到我们的帧")
        self.assertEqual(radio.civ_received[0].hex(), "fefe94e003fd")
        self.assertTrue(frames, "电台的应答没有回到 on_civ")
        self.assertTrue(frames[0].startswith(bytes.fromhex("FEFE94E0")))

    async def test_the_handshake_survives_a_civ_looking_type_byte(self):
        """Login/token/conninfo all use type 0x01 — the demux must not steal them."""
        client, radio, _frames = await self._streaming_client()
        self.assertTrue(radio.stream_requested, "握手被 CI-V 的判断吃掉了")
        self.assertGreaterEqual(radio.tokens, 1)

    async def test_sending_before_the_stream_is_refused_not_crashed(self):
        client = IcomNetworkClient("127.0.0.1", "G7250", "x",
                                   control_port=1, are_you_there_period=0.02)
        self.assertFalse(client.send_civ(bytes.fromhex("FEFE94E003FD")))
