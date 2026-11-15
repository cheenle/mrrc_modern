"""The serial-port lookalike over the SCU-LAN10 CAT tunnel.

The point of these tests is the **error classification**, not the plumbing: a
lost UDP session must look *transient* to `YaesuCatController`, because that
classifier decides whether the UI says "retrying" or "your USB cable is
unplugged" (`_is_device_fatal`, cat_core.py:114).  Getting that backwards is
invisible in a lab and very loud in the field.
"""
import threading
import unittest
from typing import Callable, Optional

import serial

from backends.yaesu.cat_core import YaesuCatController
from backends.yaesu.scu_lan.transport import ScuLanCatTransport


class FakeClient:
    """The bits of `ScuLanClient` the transport touches, without sockets."""

    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.sent: list[str] = []
        self.on_cat: Optional[Callable[[str], None]] = None
        self.host = "192.168.1.50"
        self.base_port = 50000
        self.session_id = 0x28

    def send_cat_threadsafe(self, command: str) -> bool:
        if not self.connected:
            return False
        self.sent.append(command)
        return True


class LifecycleTests(unittest.TestCase):
    def test_it_starts_closed_and_flips_with_open_close(self):
        transport = ScuLanCatTransport(FakeClient())
        self.assertFalse(transport.is_open)
        transport.open()
        self.assertTrue(transport.is_open)
        transport.close()
        self.assertFalse(transport.is_open)

    def test_opening_steals_the_callback_and_closing_gives_it_back(self):
        client = FakeClient()
        previous = lambda text: None                          # noqa: E731
        client.on_cat = previous
        transport = ScuLanCatTransport(client)
        transport.open()
        self.assertIs(getattr(client.on_cat, "__self__", None), transport)
        transport.close()
        self.assertIs(client.on_cat, previous)

    def test_a_write_on_a_closed_transport_is_the_fatal_kind(self):
        transport = ScuLanCatTransport(FakeClient())
        with self.assertRaises(serial.SerialException) as caught:
            transport.write(b"FA;")
        self.assertNotIsInstance(caught.exception, serial.SerialTimeoutException)
        self.assertTrue(YaesuCatController._is_device_fatal(caught.exception))

    def test_describe_names_the_tunnel_for_the_startup_log(self):
        transport = ScuLanCatTransport(FakeClient())
        self.assertIn("192.168.1.50:50001", transport.describe())
        self.assertIn("0x28", transport.describe())


class WriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeClient()
        self.transport = ScuLanCatTransport(self.client)
        self.transport.open()

    def test_a_command_reaches_the_session_verbatim(self):
        self.assertEqual(self.transport.write(b"FA;"), 3)
        self.assertEqual(self.client.sent, ["FA;"])

    def test_a_dead_session_is_transient_not_fatal(self):
        self.client.connected = False
        with self.assertRaises(serial.SerialTimeoutException) as caught:
            self.transport.write(b"TX0;")
        # This is the guard the whole class exists for: transient means the
        # controller retries instead of latching serial_connected=False.
        self.assertFalse(YaesuCatController._is_device_fatal(caught.exception))

    def test_a_restored_session_accepts_the_retry_again(self):
        self.client.connected = False
        with self.assertRaises(serial.SerialTimeoutException):
            self.transport.write(b"TX0;")
        self.client.connected = True
        self.transport.write(b"TX0;")
        self.assertEqual(self.client.sent, ["TX0;"])


class ReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeClient()
        self.transport = ScuLanCatTransport(self.client, read_timeout=0.05)
        self.transport.open()

    def test_a_fed_frame_is_counted_and_read(self):
        self.transport.feed("FA007007000;")
        self.assertEqual(self.transport.in_waiting, 12)
        self.assertEqual(self.transport.read(12), b"FA007007000;")
        self.assertEqual(self.transport.in_waiting, 0)

    def test_a_partial_read_keeps_the_rest(self):
        self.transport.feed("ID0680;")
        self.assertEqual(self.transport.read(2), b"ID")
        self.assertEqual(self.transport.read(5), b"0680;")

    def test_reading_more_than_arrived_returns_what_is_there(self):
        self.transport.feed("MD0;")
        self.assertEqual(self.transport.read(64), b"MD0;")

    def test_an_idle_read_times_out_without_blocking_forever(self):
        self.assertEqual(self.transport.read(4), b"")

    def test_reset_drops_stale_input(self):
        self.transport.feed("RM1;100;")
        self.transport.reset_input_buffer()
        self.assertEqual(self.transport.in_waiting, 0)
        self.assertEqual(self.transport.read(4), b"")

    def test_a_frame_fed_from_another_thread_wakes_the_reader(self):
        result: list[bytes] = []

        def reader() -> None:
            result.append(self.transport.read(4))

        thread = threading.Thread(target=reader)
        thread.start()

        def feeder() -> None:
            self.transport.feed("TX1;")

        threading.Timer(0.01, feeder).start()
        thread.join(timeout=2.0)
        self.assertEqual(result, [b"TX1;"])

    def test_frames_arrive_in_order(self):
        for text in ("FA;", "FB;", "MD0;"):
            self.transport.feed(text)
        self.assertEqual(self.transport.read(12), b"FA;FB;MD0;")


class FlushTests(unittest.TestCase):
    def test_flush_is_a_no_op_and_safe_before_open(self):
        transport = ScuLanCatTransport(FakeClient())
        transport.flush()                                    # must not raise
        transport.open()
        transport.flush()


if __name__ == "__main__":
    unittest.main()
