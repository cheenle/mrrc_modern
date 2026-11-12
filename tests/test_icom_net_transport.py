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
