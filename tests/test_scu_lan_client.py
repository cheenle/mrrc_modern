"""SCU-LAN10 client against a scripted unit (no hardware, real UDP).

The fake below plays the *unit's* side of the seven phases exactly as the
protocol write-up describes them: silence on the five init packets, an auth
answer carrying a session id, acks for the continuation and the three channel
setups, scope bodies on port+3, 16 kHz audio on port+2, and CAT replies keyed
by the command mnemonic.  That cannot prove the wire format (only the design
record's captures can), but it does prove our *sequence*: what we send, on
which port, with which channel id and session id, that CAT goes out twice with
one `cmd_seq`, and that a quiet link is rebuilt instead of hanging.
"""
import asyncio
import socket
import unittest

from backends.yaesu.scu_lan import protocol as proto
from backends.yaesu.scu_lan import scope_format as sf
from backends.yaesu.scu_lan.client import (
    CAPABILITY_BYTES, ScuLanClient, ScuLanError,
)


class FakeScuLan:
    """A scripted SCU-LAN10 on loopback, four consecutive UDP ports."""

    #: How many of each data body to push once a session is up.
    SCOPE_PUSH = 3
    AUDIO_PUSH = 2

    def __init__(self, *, password: str = "defaultuser",
                 session_id: int = 0x28, scope_marker: int = 1900) -> None:
        self.password = password
        self.session_id = session_id
        self.scope_marker = scope_marker
        self.base_port = 0
        self.transports: dict[str, asyncio.DatagramTransport] = {}
        self.addrs: dict[str, tuple] = {}
        self._sequence = {channel: proto.SequenceCounter(0x01)
                          for channel in proto.CHANNELS}
        #: Everything the client sent, for assertions.
        self.init_packets = 0
        self.auth_packets: list[proto.Packet] = []
        self.continuation: list[bytes] = []
        self.setups: list[tuple[int, bytes]] = []
        self.port_inits: list[int] = []
        self.cat_requests: list[tuple[int, str]] = []
        self.keepalives: dict[int, int] = {c: 0 for c in proto.CHANNELS}
        self.authenticated = 0
        self.silent = False           # True = answer nothing (link died)

    # ── unit side ────────────────────────────────────────────────

    async def start(self) -> int:
        loop = asyncio.get_running_loop()
        for base in range(42000, 60000, 4):
            try:
                await self._bind_all(loop, base)
                self.base_port = base
                return base
            except OSError:
                await self.stop()
        raise RuntimeError("no four consecutive free UDP ports")

    async def _bind_all(self, loop, base: int) -> None:
        for index, kind in enumerate(("ctrl", "cat", "audio", "scope")):
            transport, _ = await loop.create_datagram_endpoint(
                lambda kind=kind: _UnitProtocol(self, kind),
                local_addr=("127.0.0.1", base + index),
                family=socket.AF_INET)
            self.transports[kind] = transport
            self.addrs[kind] = ("127.0.0.1", base + index)

    async def stop(self) -> None:
        for transport in self.transports.values():
            transport.close()
        self.transports.clear()

    def _send(self, kind: str, body: bytes, msg_type: int, channel: int,
              addr) -> None:
        seq = self._sequence[channel].next()
        raw = proto.frame(body, seq=seq, channel=channel, msg_type=msg_type)
        self.transports[kind].sendto(raw, addr)

    # ── inbound ──────────────────────────────────────────────────

    def on_datagram(self, kind: str, data: bytes, addr) -> None:
        packet = proto.try_parse(data)
        if packet is None:
            return
        if packet.msg_type == proto.MSG_KEEPALIVE:
            self.keepalives[packet.channel] += 1
            if not self.silent and packet.channel == proto.CHANNEL_CTRL:
                self._send("ctrl", b"\x00", proto.MSG_ACK_KEEPALIVE,
                           packet.channel, addr)
            return
        if self.silent:
            return
        if packet.msg_type == proto.MSG_AUTH:
            self.auth_packets.append(packet)
            if packet.body[35:68].rstrip(b"\x00") == self.password.encode():
                self.authenticated += 1
                body = bytes((self.session_id,)) + b"\x00" * 8 + CAPABILITY_BYTES
                self._send("ctrl", body, proto.MSG_ACK_AUTH,
                           proto.CHANNEL_CTRL, addr)
            # A bad credential gets nothing at all — the unit is mute.
            return
        if packet.msg_type == proto.MSG_PORT_INIT:
            self.port_inits.append(packet.channel)
            self._send(kind, b"\x00\x00\x00\x00", proto.MSG_ACK_PORT_INIT,
                       packet.channel, addr)
            if packet.channel == proto.CHANNEL_SCOPE:
                self._push_scope(addr)
            elif packet.channel == proto.CHANNEL_AUDIO:
                self._push_audio(addr)
            return
        if packet.msg_type == proto.MSG_SETUP:
            self.setups.append((packet.channel, packet.body))
            if packet.body[4:6] == b"\x51\xc3":
                self.continuation.append(packet.body)
            self._send("ctrl", b"\x00\x00\x00\x00", proto.MSG_ACK_SETUP,
                       proto.CHANNEL_CTRL, addr)
            return
        if packet.msg_type == proto.MSG_DATA and kind == "cat":
            self._on_cat(packet, addr)

    def _on_cat(self, packet: proto.Packet, addr) -> None:
        cmd_seq = packet.body[4]
        # Client→server bodies carry an 8-byte header (the reply's is 4), so
        # `proto.cat_ascii` — which strips 4 — is not the right reader here.
        text = packet.body[8:].decode("ascii", "replace").strip()
        self.cat_requests.append((cmd_seq, text))
        if text.startswith("FA"):
            answer = bytes((pin_key(cmd_seq), 0, 0, 0)) + b"FA007007000;"
            self._send("cat", answer, proto.MSG_DATA, proto.CHANNEL_CAT, addr)
            # The radio also pushes meters nobody asked for.
            self._send("cat", bytes((0, 0, 0, 0)) + b"RM1;047;",
                       proto.MSG_DATA, proto.CHANNEL_CAT, addr)

    def _scope_body(self, index: int) -> bytes:
        body = bytearray([210] * 4096)
        for i in range(self.scope_marker):
            body[i] = 200 - (i * 37) % 150
        body[self.scope_marker] = 1
        body[0] = 0                      # one strong signal at the low edge
        body[self.scope_marker + 4] = (index + 1) & 0xFF
        return bytes(body)

    def _audio_body(self, index: int) -> bytes:
        payload = bytes((index * 7 + i) & 0xFF for i in range(640))
        header = bytearray(24)
        header[0] = index & 0xFF
        header[1], header[2], header[3] = 0x01, 0x02, 0x03
        header[4:6] = (16000).to_bytes(2, "little")
        header[22:24] = (len(payload)).to_bytes(2, "little")
        return bytes(header) + payload

    def _push_scope(self, addr) -> None:
        for index in range(self.SCOPE_PUSH):
            self._send("scope", self._scope_body(index), proto.MSG_DATA,
                       proto.CHANNEL_SCOPE, addr)

    def _push_audio(self, addr) -> None:
        for index in range(self.AUDIO_PUSH):
            self._send("audio", self._audio_body(index), proto.MSG_DATA,
                       proto.CHANNEL_AUDIO, addr)


def pin_key(cmd_seq: int) -> int:
    """Reply sequence byte — deliberately unrelated to `cmd_seq`.

    The unit identifies a CAT reply by the command mnemonic, never by the
    request id (that is why this layer does not correlate).
    """
    return (cmd_seq + 0x40) & 0xFF


class _UnitProtocol(asyncio.DatagramProtocol):
    def __init__(self, unit: FakeScuLan, kind: str) -> None:
        self._unit = unit
        self._kind = kind

    def datagram_received(self, data: bytes, addr) -> None:
        self._unit.on_datagram(self._kind, data, addr)


class ClientTestCase(unittest.IsolatedAsyncioTestCase):
    """Shared fixture: a fake unit plus a client wired to short timings."""

    FAKE_KWARGS: dict = {}

    async def asyncSetUp(self) -> None:
        self.fake = FakeScuLan(**self.FAKE_KWARGS)
        self.base_port = await self.fake.start()
        self.scope: list[bytes] = []
        self.audio: list[bytes] = []
        self.cat: list[str] = []
        self.states: list[dict] = []
        self.client = self.make_client()
        await self.client.start()

    def make_client(self, *, password: str = "defaultuser",
                    auth_timeout: float = 0.4,
                    keepalive_interval: float = 0.05,
                    link_timeout: float = 1.0) -> ScuLanClient:
        return ScuLanClient(
            host="127.0.0.1", username="defaultuser", password=password,
            base_port=self.base_port,
            on_scope=self.scope.append, on_audio=self.audio.append,
            on_cat=self.cat.append, on_state=self.states.append,
            auth_timeout=auth_timeout, keepalive_interval=keepalive_interval,
            link_timeout=link_timeout, setup_step_s=0.01,
        )

    async def asyncTearDown(self) -> None:
        await self.client.stop()
        await self.fake.stop()

    async def handshake(self, timeout: float = 3.0) -> bool:
        return await self.client.wait_until_connected(timeout)

    async def settle(self, seconds: float = 0.15) -> None:
        await asyncio.sleep(seconds)


class HandshakeTests(ClientTestCase):
    async def test_the_session_comes_up_with_the_units_session_id(self):
        self.assertTrue(await self.handshake())
        self.assertEqual(self.client.session_id, self.fake.session_id)
        self.assertEqual(self.client.stats.handshakes, 1)
        self.assertEqual(self.client.stats.capability, CAPABILITY_BYTES)
        self.assertFalse(self.client.auth_failed)

    async def test_the_five_init_packets_precede_the_auth(self):
        self.assertTrue(await self.handshake())
        # The unit sees them as keepalives on the CTRL channel before auth.
        self.assertGreaterEqual(self.fake.keepalives[proto.CHANNEL_CTRL], 5)
        self.assertEqual(len(self.fake.auth_packets), 1)
        auth = self.fake.auth_packets[0]
        self.assertEqual(auth.body[2:35].rstrip(b"\x00"), b"defaultuser")

    async def test_the_continuation_and_both_setups_carry_the_session_id(self):
        self.assertTrue(await self.handshake())
        self.assertEqual(len(self.fake.continuation), 1)
        self.assertEqual(self.fake.continuation[0][0], self.fake.session_id)
        marks = [body[4:6] for _channel, body in self.fake.setups]
        self.assertIn(b"\x52\xc3", marks)          # CAT port id
        self.assertIn(b"\x53\xc3", marks)          # audio port id
        for _channel, body in self.fake.setups:
            self.assertEqual(body[0], self.fake.session_id)

    async def test_every_channel_is_initialised_on_its_own_port(self):
        self.assertTrue(await self.handshake())
        self.assertEqual(sorted(self.fake.port_inits),
                         sorted([proto.CHANNEL_CAT, proto.CHANNEL_AUDIO,
                                 proto.CHANNEL_SCOPE]))


class AuthenticationTests(ClientTestCase):
    FAKE_KWARGS = {"password": "something-else"}

    async def test_a_wrong_password_leaves_us_unconnected_and_named(self):
        self.assertFalse(await self.client.wait_until_connected(0.6))
        self.assertEqual(self.client.stats.handshakes, 0)
        self.assertEqual(self.fake.authenticated, 0)
        self.assertTrue(self.client.auth_failed,
                        "a mute unit must be recorded, not retried blindly")
        self.assertTrue(self.states, "the failure must be published")


class CatTests(ClientTestCase):
    async def test_a_command_goes_out_twice_with_one_cmd_seq(self):
        self.assertTrue(await self.handshake())
        self.assertTrue(self.client.send_cat("FA;"))
        await self.settle()
        sent = [entry for entry in self.fake.cat_requests
                if entry[1].startswith("FA")]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], sent[1])

    async def test_a_reply_and_an_unsolicited_meter_both_reach_the_callback(self):
        self.assertTrue(await self.handshake())
        self.client.send_cat("FA;")
        await self.settle()
        self.assertIn("FA007007000;", self.cat)
        self.assertIn("RM1;047;", self.cat)
        self.assertGreaterEqual(self.client.stats.cat_frames, 2)

    async def test_a_command_from_another_thread_reaches_the_unit(self):
        """The seam `YaesuCatController._write` actually uses (`to_thread`)."""
        self.assertTrue(await self.handshake())
        self.assertTrue(await asyncio.to_thread(
            self.client.send_cat_threadsafe, "ID;"))
        await self.settle()
        self.assertIn("ID;", [text for _seq, text in self.fake.cat_requests])

    async def test_the_threadsafe_entry_point_refuses_a_dead_session(self):
        self.assertFalse(self.client.send_cat_threadsafe("FA;"))

    async def test_a_repeat_command_takes_a_fresh_cmd_seq(self):
        """A retried release has to act — the unit dedupes on this field."""
        self.assertTrue(await self.handshake())
        self.client.send_cat("TX0;")
        self.client.send_cat("TX0;")
        await self.settle()
        seqs = [seq for seq, text in self.fake.cat_requests if text == "TX0;"]
        self.assertEqual(len(seqs), 4, "each send goes out twice")
        self.assertEqual(len(set(seqs)), 2, "but the retry changes cmd_seq")

    async def test_a_command_before_the_session_is_up_is_refused(self):
        refused = self.make_client(auth_timeout=0.01)
        self.assertFalse(refused.send_cat("FA;"))
        await refused.stop()

    async def test_a_bare_mnemonic_gains_its_terminator(self):
        self.assertTrue(await self.handshake())
        self.client.send_cat("FA")
        await self.settle()
        self.assertTrue(all(entry[1].endswith(";")
                            for entry in self.fake.cat_requests
                            if entry[1].startswith("FA")))


class StreamTests(ClientTestCase):
    async def test_scope_bodies_arrive_whole_and_decode(self):
        self.assertTrue(await self.handshake())
        await self.settle()
        self.assertGreaterEqual(len(self.scope), 1)
        self.assertEqual(len(self.scope[0]), 4096)
        bins = sf.to_scope_bins(self.scope[0])
        self.assertEqual(len(bins), sf.SPECTRUM_BINS)
        self.assertEqual(bins[0], 255)             # the strong signal at bin 0
        self.assertGreaterEqual(self.client.stats.scope_packets, 1)
        self.assertTrue(self.client.snapshot()["scope_age_s"] >= 0.0)

    async def test_audio_arrives_as_the_payload_without_the_inner_header(self):
        self.assertTrue(await self.handshake())
        await self.settle()
        self.assertGreaterEqual(len(self.audio), 1)
        self.assertEqual(len(self.audio[0]), 640)
        self.assertEqual(self.audio[0], bytes(i & 0xFF for i in range(640)),
                         "the 24-byte inner header must be gone, the 640-byte "
                         "payload intact")
        self.assertGreaterEqual(self.client.stats.audio_packets, 1)

    async def test_keepalives_cover_all_four_channels(self):
        self.assertTrue(await self.handshake())
        await self.settle(0.3)
        for channel in proto.CHANNELS:
            self.assertGreaterEqual(self.fake.keepalives[channel], 2,
                                    f"channel 0x{channel:02X} not kept alive")


class RecoveryTests(ClientTestCase):
    async def test_a_quiet_link_is_rebuilt_instead_of_hanging(self):
        self.assertTrue(await self.handshake())
        self.fake.silent = True
        deadline = asyncio.get_running_loop().time() + 4.0
        while asyncio.get_running_loop().time() < deadline:
            if self.client.stats.reconnects >= 1:
                break
            await asyncio.sleep(0.05)
        self.assertGreaterEqual(self.client.stats.reconnects, 1)
        self.fake.silent = False
        self.assertTrue(await self.client.wait_until_connected(6.0),
                        "the client must re-handshake after the unit returns")
        self.assertGreaterEqual(self.client.stats.handshakes, 2)


class ConstructionTests(unittest.TestCase):
    def test_a_non_numeric_port_is_named_not_a_traceback(self):
        with self.assertRaises(ScuLanError):
            ScuLanClient("127.0.0.1", "u", "p", base_port="fifty-thousand")

    def test_the_default_port_is_the_factory_base(self):
        client = ScuLanClient("127.0.0.1", "u", "p")
        self.assertEqual(client.base_port, 50000)


if __name__ == "__main__":
    unittest.main()
