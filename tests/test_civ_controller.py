"""
Tests for CivController — the IC-7300 CI-V async controller.
All tests run without hardware: a FakeSerial with an in-memory RX
buffer stands in for the radio (bus echo + scripted responses).
"""
import asyncio
import threading
import unittest
from unittest.mock import patch

import serial

from backends.ic7300.civ_controller import (
    CivController, CivTimeoutError, CivNakError,
    SETMODE_CIV_TRANSCEIVE_ON, SETMODE_CIV_TRANSCEIVE_MK2,
)
from backends.ic7300.civ_codec import (
    build_frame, encode_freq_bcd, encode_level_bcd,
    CONTROLLER_ADDR, RADIO_ADDR,
)
from backends.ic7300.config_ic7300 import MK2_CIV_ADDR


class FakeSerial:
    """In-memory serial port: captures writes, auto-echoes them (CI-V
    bus behaviour), and feeds scripted responder bytes back via read()."""

    def __init__(self):
        self.is_open = True
        self.written = bytearray()
        self._rx = bytearray()
        self._cond = threading.Condition()
        self.responder = None       # callable(bytes) -> bytes | None
        self.echo = True
        self.fail_writes = False
        self.fail_reads = False

    def write(self, data):
        if self.fail_writes:
            raise serial.SerialException("device gone")
        self.written += data
        if self.echo:
            self.feed(bytes(data))  # simplex CI-V bus echo
        if self.responder is not None:
            resp = self.responder(bytes(data))
            if resp:
                self.feed(resp)
        return len(data)

    def feed(self, data: bytes):
        with self._cond:
            self._rx += data
            self._cond.notify_all()

    def read(self, n=1):
        if self.fail_reads:
            raise serial.SerialException("read: device gone")
        with self._cond:
            if not self._rx:
                self._cond.wait(timeout=0.05)
            out = bytes(self._rx[:n])
            del self._rx[:n]
            return out

    def flush(self):
        pass

    def reset_input_buffer(self):
        with self._cond:
            self._rx.clear()

    def close(self):
        self.is_open = False


def radio_frame(command: int, data: bytes = b"") -> bytes:
    """A frame as the radio sends it: to=controller, from=radio."""
    return build_frame(command, data, to=CONTROLLER_ADDR, from_addr=RADIO_ADDR)


def broadcast_frame(command: int, data: bytes = b"") -> bytes:
    """A transceive broadcast frame (to=0x00)."""
    return build_frame(command, data, to=0x00, from_addr=RADIO_ADDR)


class CivControllerTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeSerial()
        self.ctl = CivController("/dev/fake", query_timeout=0.05)
        with patch("backends.ic7300.civ_controller.serial.Serial",
                   return_value=self.fake):
            ok = await self.ctl.connect()
        self.assertTrue(ok)
        self.assertTrue(self.ctl.connected)

    async def asyncTearDown(self):
        await self.ctl.disconnect()

    def written_frames(self) -> list[bytes]:
        """Split captured writes into individual CI-V frames."""
        frames = []
        buf = bytes(self.fake.written)
        for part in buf.split(b"\xfe\xfe"):
            if part:
                frames.append(b"\xfe\xfe" + part)
        return frames


class ConnectTests(CivControllerTestBase):
    async def test_connect_enables_civ_transceive(self):
        expected = build_frame(SETMODE_CIV_TRANSCEIVE_ON[0],
                               SETMODE_CIV_TRANSCEIVE_ON[1:])
        self.assertIn(expected, self.written_frames())

    async def test_mk2_connect_uses_mk2_transceive_item(self):
        # The MK2 (address 0xB6) enables CI-V Transceive via set-mode
        # item 0089 — item 0071 is "AF Output Level" on the MK2.
        self.fake.written.clear()
        ctl = CivController("/dev/fake", civ_addr=MK2_CIV_ADDR,
                            query_timeout=0.05)
        with patch("backends.ic7300.civ_controller.serial.Serial",
                   return_value=self.fake):
            ok = await ctl.connect()
        self.assertTrue(ok)
        expected = build_frame(SETMODE_CIV_TRANSCEIVE_MK2[0],
                               SETMODE_CIV_TRANSCEIVE_MK2[1:],
                               to=MK2_CIV_ADDR)
        frames = []
        for part in bytes(self.fake.written).split(b"\xfe\xfe"):
            if part:
                frames.append(b"\xfe\xfe" + part)
        self.assertIn(expected, frames)
        self.assertNotIn(b"\x00\x71", bytes(self.fake.written))
        await ctl.disconnect()


class FrequencyTests(CivControllerTestBase):
    async def test_get_frequency_with_echo_and_broadcast_interleaved(self):
        """Echo frames + a transceive broadcast arriving between request
        and response must not disturb the pending query."""
        seen_broadcasts = []
        self.ctl.set_broadcast_callback(
            lambda field, value: seen_broadcasts.append((field, value)))

        def responder(data: bytes):
            if data == build_frame(0x03):
                # Broadcast first (stale-ish freq), then the real answer.
                return (broadcast_frame(0x00, encode_freq_bcd(7_050_000))
                        + radio_frame(0x03, encode_freq_bcd(14_074_000)))
            return None

        self.fake.responder = responder
        freq = await self.ctl.get_frequency()
        self.assertEqual(freq, 14_074_000)
        # The interleaved broadcast still reached the callback.
        self.assertEqual(seen_broadcasts, [("vfo_a_freq", 7_050_000)])

    async def test_set_frequency_frame_format(self):
        ok = await self.ctl.set_frequency(14_074_000)
        self.assertTrue(ok)
        self.assertIn(build_frame(0x05, encode_freq_bcd(14_074_000)),
                      self.written_frames())

    async def test_set_frequency_vfo_b_rejected(self):
        # vfo_b_direct=False: no swap-read-swap emulation.
        self.assertFalse(await self.ctl.set_frequency(7_050_000, vfo="B"))


class ModeTests(CivControllerTestBase):
    async def test_get_mode_decodes_mode_and_fil(self):
        def responder(data: bytes):
            if data == build_frame(0x04):
                return radio_frame(0x04, bytes((0x01, 0x02)))  # USB, FIL2
            return None

        self.fake.responder = responder
        mode = await self.ctl.get_mode()
        self.assertEqual(mode, 0x01)
        self.assertEqual(self.ctl._fil, 2)

    async def test_set_mode_resends_current_fil(self):
        self.ctl._fil = 3
        ok = await self.ctl.set_mode(0x03)  # CW-U
        self.assertTrue(ok)
        self.assertIn(build_frame(0x06, bytes((0x03, 0x03))),
                      self.written_frames())


class AckTests(CivControllerTestBase):
    async def test_ng_raises_civ_nak(self):
        def responder(data: bytes):
            # Radio answers the mode-set with NG.
            if data.startswith(b"\xfe\xfe") and data[4] == 0x06:
                return radio_frame(0xFA)
            return None

        self.fake.responder = responder
        with self.assertRaises(CivNakError):
            await self.ctl.set_with_ack(0x06, bytes((0x01, 0x01)))

    async def test_ok_resolves_set_with_ack(self):
        def responder(data: bytes):
            if data[4] == 0x16:  # any 0x16 switch set
                return radio_frame(0xFB)
            return None

        self.fake.responder = responder
        self.assertTrue(await self.ctl.set_with_ack(0x16, bytes((0x22, 0x01))))


class TimeoutTests(CivControllerTestBase):
    async def test_timeout_retries_once_then_raises(self):
        self.fake.responder = lambda data: None  # radio stays silent
        before = len(self.fake.written)
        with self.assertRaises(CivTimeoutError):
            await self.ctl.transact(0x03)
        # Exactly two attempts (initial + one retry).
        frame = build_frame(0x03)
        self.assertEqual(bytes(self.fake.written[before:]).count(frame), 2)

    async def test_send_command_returns_none_on_timeout(self):
        """Poll-path parity with the FT-710: no raise, just None."""
        self.fake.responder = lambda data: None
        self.assertIsNone(await self.ctl.get_frequency())


class ScopeDemuxTests(CivControllerTestBase):
    async def test_scope_segment_between_request_and_response(self):
        """0x27 0x00 segments interleaved in a query exchange must land
        on scope_queue while the query still resolves correctly."""
        scope_segment = radio_frame(
            0x27, bytes((0x00, 0x00, 0x01, 0x11)) + b"\x00" * 12)

        def responder(data: bytes):
            if data == build_frame(0x15, bytes((0x02,))):
                return scope_segment + radio_frame(
                    0x15, bytes((0x02,)) + encode_level_bcd(120))
            return None

        self.fake.responder = responder
        smeter = await self.ctl.get_s_meter()
        self.assertEqual(smeter, 120)
        seg = self.ctl.scope_queue.get_nowait()
        self.assertEqual(seg.sequence, 1)
        self.assertEqual(seg.sequence_max, 11)


class PendingFifoTests(CivControllerTestBase):
    async def test_same_key_pending_futures_resolve_in_order(self):
        loop = asyncio.get_running_loop()
        key = (0x15, 0x02)
        fut1 = self.ctl._register_pending(key)
        fut2 = self.ctl._register_pending(key)
        self.fake.feed(radio_frame(0x15, bytes((0x02,)) + encode_level_bcd(10)))
        self.fake.feed(radio_frame(0x15, bytes((0x02,)) + encode_level_bcd(200)))
        r1 = await asyncio.wait_for(fut1, 1.0)
        r2 = await asyncio.wait_for(fut2, 1.0)
        from backends.ic7300.civ_codec import decode_level_bcd
        self.assertEqual(decode_level_bcd(r1.data[1:]), 10)
        self.assertEqual(decode_level_bcd(r2.data[1:]), 200)
        self.assertNotIn(key, self.ctl._pending)
        self.assertIs(loop, asyncio.get_running_loop())


class FatalErrorTests(CivControllerTestBase):
    async def test_fatal_write_error_flips_connected_false(self):
        self.fake.fail_writes = True
        ok = await self.ctl.send_set_command(bytes((0x05,)) + encode_freq_bcd(1))
        self.assertFalse(ok)
        self.assertFalse(self.ctl.connected)

    async def test_fatal_read_error_flips_connected_false(self):
        self.fake.fail_reads = True
        for _ in range(50):
            await asyncio.sleep(0.02)
            if not self.ctl.connected:
                break
        self.assertFalse(self.ctl.connected)


class PriorityTests(CivControllerTestBase):
    async def test_set_ptt_uses_priority_path(self):
        ok = await self.ctl.set_ptt(True)
        self.assertTrue(ok)
        self.assertIn(build_frame(0x1C, bytes((0x00, 0x01))),
                      self.written_frames())
        # The preemption flag is cleared after the priority write.
        self.assertFalse(self.ctl._cancel_polls.is_set())

    async def test_send_command_yields_when_polls_cancelled(self):
        self.ctl._cancel_polls.set()
        try:
            self.assertIsNone(await self.ctl.send_command(0x03))
        finally:
            self.ctl._cancel_polls.clear()


if __name__ == "__main__":
    unittest.main()
