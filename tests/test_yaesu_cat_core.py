"""Transport tests for the shared Yaesu core (spec 2026-09-12 §4.2).

No serial hardware: `_FakeSerial` implements the pyserial surface the core
uses (`is_open`, `in_waiting`, `read`, `write`, `flush`,
`reset_input_buffer`) and serves scripted responses, so framing, prefix
skipping, priority preemption and the error classification are all exercised
deterministically.
"""
import asyncio
import errno
import unittest
from typing import Any, Tuple

import serial

from backends.yaesu.cat_core import YaesuCatController
from backends.yaesu.yaesu_profiles import get_profile


class _FakeSerial:
    """Minimal pyserial stand-in with scripted answers.

    `reset_input_buffer()` is deliberately a no-op: the scripted answers
    stand in for data that arrives *after* a write, which is the timing the
    production path sees.  Real device timing is covered by the pty fake
    radio in `tests/test_yaesu_fake_radio.py`.
    """

    def __init__(self, responses=(), fail_on_write=None):
        self._rx = bytearray()
        for r in responses:
            self._rx.extend(r.encode("ascii"))
        self.writes = []
        self.is_open = True
        self.fail_on_write = fail_on_write

    @property
    def in_waiting(self):
        return len(self._rx)

    def read(self, n):
        chunk = bytes(self._rx[:n])
        del self._rx[:n]
        return chunk

    def write(self, data):
        if self.fail_on_write is not None:
            raise self.fail_on_write
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def close(self):
        self.is_open = False


def _controller(responses=(), model="ftdx10",
                fail_on_write=None) -> Tuple[YaesuCatController, Any]:
    """Build a controller whose serial port is a scripted test double.

    The double is returned as ``Any``: it implements the pyserial surface
    this transport uses, while the controller's own field is declared as
    ``Optional[serial.Serial]``.
    """
    ctrl = YaesuCatController("/dev/null", baudrate=38400,
                              profile=get_profile(model))
    fake: Any = _FakeSerial(responses, fail_on_write=fail_on_write)
    ctrl._ser = fake
    ctrl._connected = True
    return ctrl, fake


class FramingTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_returns_answer_without_terminator(self):
        ctrl, fake = _controller(["FA014074000;"])
        self.assertEqual(await ctrl.query("FA"), "FA014074000")
        self.assertEqual(fake.writes, [b"FA;"])   # ';' appended here

    async def test_query_skips_frames_of_other_commands(self):
        """Auto-Information frames of *another* command must be skipped.

        A stale frame of the same command is indistinguishable from the
        answer (both carry the prefix), which is why the filter keys on the
        command as the expected prefix.
        """
        ctrl, _ = _controller(["MD02;",          # AI frame of another command
                               "FA014100000;"])  # real answer
        self.assertEqual(await ctrl.query("FA"), "FA014100000")

    async def test_one_answer_per_chunk_is_returned_and_the_rest_dropped(self):
        """Documents the ported contract: a chunk may carry several frames.

        Only the frame matching the queried prefix is returned; whatever
        followed it in the same chunk is discarded (the verified FT-710 path
        behaves the same and has done so in the field).  A query with no
        fresh data therefore times out instead of returning a stale frame.
        """
        ctrl, _ = _controller(["FA014074000;TX0;"])
        self.assertEqual(await ctrl.query("FA"), "FA014074000")
        self.assertIsNone(await ctrl.query("TX", timeout=0.05))

    async def test_timeout_returns_none_after_the_configured_budget(self):
        ctrl, _ = _controller([])
        ctrl._timeout = 0.05
        self.assertIsNone(await ctrl.query("FA", timeout=0.05))


class SetCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_is_write_only(self):
        ctrl, fake = _controller(["FA014074000;"])  # answer must be ignored
        self.assertTrue(await ctrl.set("FA014074000"))
        self.assertEqual(fake.writes, [b"FA014074000;"])

    async def test_priority_set_preempts_a_pending_poll(self):
        ctrl, fake = _controller(["FA014074000;"])
        ctrl._timeout = 0.2
        poll = asyncio.create_task(ctrl.query("FA"))
        await asyncio.sleep(0.01)
        self.assertTrue(await ctrl.send_priority_set_command("TX1"))
        await poll                                    # poll aborts, no hang
        self.assertIn(b"TX1;", fake.writes)
        self.assertFalse(ctrl._cancel_polls.is_set())  # cleared in finally


class ErrorClassificationTests(unittest.TestCase):
    def test_enxio_is_device_gone(self):
        self.assertTrue(YaesuCatController._is_device_gone(
            OSError(errno.ENXIO, "Device not configured")))
        self.assertTrue(YaesuCatController._is_device_gone(
            OSError(errno.ENOENT, "No such file or directory")))

    def test_protocol_error_is_not_device_gone(self):
        self.assertFalse(YaesuCatController._is_device_gone(ValueError("bad frame")))

    def test_serial_timeout_is_not_fatal(self):
        """A marginal write under contention must not latch 'disconnected'."""
        self.assertFalse(YaesuCatController._is_device_fatal(
            serial.SerialTimeoutException("write timeout")))

    def test_serial_exception_is_fatal(self):
        self.assertTrue(YaesuCatController._is_device_fatal(
            serial.SerialException("port not open")))

    def test_missing_profile_is_refused(self):
        with self.assertRaises(ValueError):
            YaesuCatController("/dev/null", profile=None)


class WriteFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_fatal_write_clears_connectivity(self):
        ctrl, _ = _controller([], fail_on_write=serial.SerialException("gone"))
        self.assertFalse(await ctrl.set("FA014074000"))
        self.assertFalse(ctrl._connected)

    async def test_timeout_write_keeps_connectivity(self):
        ctrl, _ = _controller([], fail_on_write=serial.SerialTimeoutException("busy"))
        self.assertFalse(await ctrl.set("FA014074000"))
        self.assertTrue(ctrl._connected)


if __name__ == "__main__":
    unittest.main()
