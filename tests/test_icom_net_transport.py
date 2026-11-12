"""The network CI-V transport: a serial port that is not a serial port.

`CivController` is a serial-port consumer — a blocking reader thread plus
`write()` from the executor.  Swapping in a network transport must not change
those semantics, because the controller's framing, timeouts and reconnect logic
all assume them.  What is worth pinning down here:

* `read()` blocks for its timeout and then returns **b''** (not None, not an
  exception) — that is what a `serial.Serial(timeout=0.1)` does, and the reader
  thread spins on it;
* `write()` never touches the datagram socket from the calling thread;
* a frame that arrives while nobody is reading is delivered to the next read,
  not lost;
* `close()` detaches the callback so a stopped controller does not keep
  consuming (and so the audio-only path is not taxed by CAT frames).
"""
import threading
import time
import unittest

from backends.icom_net.transport import NetworkCivTransport


class _StubClient:
    def __init__(self):
        self.on_civ = None
        self.opened = 0
        self.sent = []
        self.threadsafe = []

    def open_civ(self):
        self.opened += 1
        return True

    def send_civ_threadsafe(self, frame):
        self.threadsafe.append(bytes(frame))

    def deliver(self, frame):
        if self.on_civ is not None:
            self.on_civ(frame)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.client = _StubClient()
        self.t = NetworkCivTransport(self.client, read_timeout=0.05)
        self.t.open()

    def test_open_attaches_and_nudges_the_tunnel(self):
        self.assertEqual(self.client.opened, 1)
        self.assertIsNotNone(self.client.on_civ)
        self.assertTrue(self.t.is_open)

    def test_read_returns_a_whole_frame(self):
        self.client.deliver(bytes.fromhex("FEFE94E003FD"))
        self.assertEqual(self.t.read(256), bytes.fromhex("FEFE94E003FD"))

    def test_read_blocks_then_returns_empty(self):
        start = time.monotonic()
        self.assertEqual(self.t.read(256), b"")
        self.assertGreaterEqual(time.monotonic() - start, 0.04)

    def test_a_large_read_is_split_not_truncated(self):
        frame = bytes(range(64))
        self.client.deliver(frame)
        first = self.t.read(16)
        second = self.t.read(256)
        self.assertEqual(first + second, frame)

    def test_write_goes_through_the_threadsafe_path(self):
        self.assertEqual(self.t.write(b"\x01\x02"), 2)
        self.assertEqual(self.client.threadsafe, [b"\x01\x02"])
        self.assertEqual(self.client.sent, [])

    def test_reset_drops_what_queued(self):
        self.client.deliver(b"\x01")
        self.t.reset_input_buffer()
        self.assertEqual(self.t.read(256), b"")

    def test_the_pyserial_surface_civ_controller_uses_exists(self):
        """`flush()` 的缺失让每一条 CI-V 写都抛 AttributeError。

        现场日志（1.28.55）：`CI-V write error for cmd 1A: 'NetworkCivTransport'
        object has no attribute 'flush'`，控制器于是判定隧道静默、退回串口。
        CivController 只用 pyserial 的这一小组方法，缺一个都不行。
        """
        self.assertIsNone(self.t.flush())
        self.assertIsNone(self.t.flushOutput())
        self.t.flushInput()
        self.assertEqual(self.t.in_waiting, 0)
        self.assertTrue(self.t.is_open)
        self.assertIn("icom-net:", self.t.port)

    def test_close_detaches(self):
        self.t.close()
        self.assertIsNone(self.client.on_civ)
        self.assertEqual(self.t.read(256), b"")


class BlockingReadTests(unittest.TestCase):
    """The reader thread is a real thread; make sure the queue crosses it."""

    def test_frame_from_the_event_loop_reaches_the_reader_thread(self):
        client = _StubClient()
        t = NetworkCivTransport(client, read_timeout=1.0)
        t.open()
        got = []

        def reader():
            got.append(t.read(256))
            got.append(t.read(256))

        thread = threading.Thread(target=reader)
        thread.start()
        time.sleep(0.05)
        client.deliver(b"\xAA\xBB")
        thread.join(2.0)
        self.assertEqual(got[0], b"\xAA\xBB")
        self.assertEqual(got[1], b"")


class _EndToEndTests(unittest.IsolatedAsyncioTestCase):
    """整条链路：假电台 → 会话 → 传输层 → 真正的 CivController。

    这是 1.28.53–1.28.56 四轮现场修复的浓缩：每一轮都是一个「接线」错误，
    单独看每个部件都正常，接起来就不通。把 4 个部件真接一次（不是 mock），
    才能在发版前抓到 `flush()` 缺失这类问题。
    """

    async def asyncSetUp(self):
        import sys
        sys.path.insert(0, "tests")
        from test_icom_net_client import FakeRadio
        from backends.icom_net.client import IcomNetworkClient
        from backends.ic7300.civ_controller import CivController
        from backends.icom_net.transport import NetworkCivTransport

        self.radio = FakeRadio()
        await self.radio.start()
        self.client = IcomNetworkClient(
            "127.0.0.1", "G7250", "beta_tester",
            control_port=self.radio.port,
            are_you_there_period=0.02, idle_period=0.02)
        self.transport = NetworkCivTransport(self.client)
        self.controller = CivController(
            self.transport.port, 115200, civ_addr=0x94,
            transceive_cmd=b"", transport=self.transport)
        await self.client.start()
        self.assertTrue(await self.client.wait_until_streaming(8.0),
                        "会话没开流")

    async def asyncTearDown(self):
        try:
            await self.controller.disconnect()
        finally:
            await self.client.stop()
            self.radio.close()

    async def test_a_frequency_read_round_trips_over_the_tunnel(self):
        self.assertTrue(await self.controller.connect())
        freq = await self.controller.get_frequency(timeout=2.0)
        self.assertEqual(freq, 14270000)      # what the fake radio answers
        self.assertGreaterEqual(self.radio.civ_answers, 1)

    async def test_every_write_reaches_the_radio(self):
        """`flush()` 缺失时这里会是 0 —— 每次写都在 AttributeError 上丢掉。"""
        await self.controller.connect()
        before = len(self.radio.civ_received)
        await self.controller.get_frequency(timeout=2.0)
        self.assertGreater(len(self.radio.civ_received), before)
        self.assertTrue(self.radio.civ_received)
