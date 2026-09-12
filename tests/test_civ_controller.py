"""
Tests for CivController — the IC-7300 CI-V async controller.
All tests run without hardware: a FakeSerial with an in-memory RX
buffer stands in for the radio (bus echo + scripted responses).
"""
import asyncio
import threading
import unittest
from typing import Callable, Optional
from unittest.mock import patch

import serial

import backends.ic7300.civ_controller as civ_module
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
        self.responder: Optional[Callable[[bytes], Optional[bytes]]] = None
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

    async def test_custom_mk2_address_keeps_mk2_transceive_item(self):
        # Model identity chooses item 0089 even when the operator changes
        # the MK2 from its factory 0xB6 CI-V address.
        self.fake.written.clear()
        ctl = CivController(
            "/dev/fake",
            civ_addr=0xA2,
            transceive_cmd=SETMODE_CIV_TRANSCEIVE_MK2,
            query_timeout=0.05,
        )
        with patch("backends.ic7300.civ_controller.serial.Serial",
                   return_value=self.fake):
            ok = await ctl.connect()
        self.assertTrue(ok)
        expected = build_frame(
            SETMODE_CIV_TRANSCEIVE_MK2[0],
            SETMODE_CIV_TRANSCEIVE_MK2[1:],
            to=0xA2,
        )
        self.assertIn(expected, bytes(self.fake.written))
        self.assertNotIn(b"\x00\x71", bytes(self.fake.written))
        await ctl.disconnect()

    async def test_regular_model_does_not_infer_mk2_from_address(self):
        self.fake.written.clear()
        ctl = CivController(
            "/dev/fake",
            civ_addr=MK2_CIV_ADDR,
            transceive_cmd=SETMODE_CIV_TRANSCEIVE_ON,
            query_timeout=0.05,
        )
        with patch("backends.ic7300.civ_controller.serial.Serial",
                   return_value=self.fake):
            ok = await ctl.connect()
        self.assertTrue(ok)
        expected = build_frame(
            SETMODE_CIV_TRANSCEIVE_ON[0],
            SETMODE_CIV_TRANSCEIVE_ON[1:],
            to=MK2_CIV_ADDR,
        )
        self.assertIn(expected, bytes(self.fake.written))
        self.assertNotIn(b"\x00\x89", bytes(self.fake.written))
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


class PowerCommandTests(CivControllerTestBase):
    def test_power_on_preamble_counts_match_icom_table(self):
        counts = {
            115200: 150,
            57600: 75,
            38400: 50,
            19200: 25,
            9600: 13,
            4800: 7,
        }
        for baud, expected_count in counts.items():
            with self.subTest(baud=baud):
                raw = civ_module.build_power_on_frame(baud, 0x94)
                actual_count = len(raw) - len(raw.lstrip(b"\xfe"))
                self.assertEqual(actual_count, expected_count)
                self.assertTrue(raw.endswith(bytes.fromhex("94 E0 18 01 FD")))

    def test_unknown_baud_uses_standard_frame(self):
        self.assertEqual(
            civ_module.build_power_on_frame(230400, 0x94),
            build_frame(0x18, b"\x01", to=0x94),
        )

    async def test_power_on_writes_documented_preamble(self):
        self.fake.written.clear()
        self.assertTrue(await self.ctl.set_power(True))
        self.assertEqual(
            bytes(self.fake.written),
            civ_module.build_power_on_frame(self.ctl.baudrate, self.ctl.civ_addr),
        )

    async def test_power_off_keeps_standard_frame(self):
        self.fake.written.clear()
        self.assertTrue(await self.ctl.set_power(False))
        self.assertEqual(
            bytes(self.fake.written),
            build_frame(0x18, b"\x00", to=self.ctl.civ_addr),
        )


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


class CivControllerProfileTests(unittest.IsolatedAsyncioTestCase):
    """Profile-supplied address / queue / attenuator steps (task 4)."""

    def test_default_construction_matches_legacy_behaviour(self):
        civ = CivController("/dev/null")
        self.assertEqual(civ.civ_addr, 0x94)
        self.assertEqual(civ._att_steps, (0, 20))
        self.assertEqual(civ._model, "IC-7300")
        self.assertEqual(civ.scope_queue.maxsize, 44)   # 4 x 11 segments

    def test_profile_supplies_address_transceive_and_queue(self):
        from backends.ic7300.civ_profiles import PROFILES
        profile = PROFILES["ic7760"]
        civ = CivController("/dev/null", 115200, civ_addr=profile.civ_addr,
                            transceive_cmd=profile.transceive_cmd,
                            profile=profile, att_steps=profile.att_steps)
        self.assertEqual(civ.civ_addr, 0xB2)
        self.assertEqual(civ.scope_queue.maxsize, 60)   # 4 x 15 segments
        self.assertEqual(civ._att_steps[-1], 45)
        self.assertIn("IC-7760", civ._model)

    def test_explicit_kwargs_beat_profile(self):
        from backends.ic7300.civ_profiles import PROFILES
        civ = CivController("/dev/null", 115200, civ_addr=0x99,
                            profile=PROFILES["ic7300"])
        self.assertEqual(civ.civ_addr, 0x99)

    async def test_set_attenuator_writes_db_from_profile_steps(self):
        from backends.ic7300.civ_profiles import PROFILES
        civ = CivController("/dev/null", 115200,
                            att_steps=PROFILES["ic7760"].att_steps)
        sent = []

        async def fake_send(cmd):
            sent.append(cmd)
            return True

        civ.send_set_command = fake_send
        self.assertTrue(await civ.set_attenuator(0))
        self.assertTrue(await civ.set_attenuator(5))
        self.assertFalse(await civ.set_attenuator(16))   # out of range
        # Wire bytes are packed BCD: 0 dB -> 0x00, 15 dB -> 0x15.
        self.assertEqual(sent, [bytes((0x11, 0x00)), bytes((0x11, 0x15))])

    async def test_set_attenuator_legacy_steps_unchanged(self):
        civ = CivController("/dev/null")
        sent = []

        async def fake_send(cmd):
            sent.append(cmd)
            return True

        civ.send_set_command = fake_send
        self.assertTrue(await civ.set_attenuator(1))
        self.assertFalse(await civ.set_attenuator(2))
        # 20 dB is BCD 0x20 on the wire (not decimal 20 = 0x14) — the
        # IC-7300MK2 CI-V reference documents "00/20" and wfview encodes
        # this byte with its BCD helpers.
        self.assertEqual(sent, [bytes((0x11, 0x20))])

    async def test_get_attenuator_returns_index_for_3db_steps(self):
        from backends.ic7300.civ_profiles import PROFILES
        civ = CivController("/dev/null", 115200,
                            att_steps=PROFILES["ic7760"].att_steps)

        async def fake_query(command, sub=None, timeout=None):
            return bytes((0x15,))          # BCD 15 dB

        civ._query_data = fake_query
        self.assertEqual(await civ._get_attenuator(), 5)

    async def test_get_attenuator_legacy_and_unknown_values(self):
        civ = CivController("/dev/null")

        async def fake_query(command, sub=None, timeout=None):
            return bytes((0x20,))          # BCD 20 dB -> index 1

        civ._query_data = fake_query
        self.assertEqual(await civ._get_attenuator(), 1)

        async def fake_unknown(command, sub=None, timeout=None):
            return bytes((0x07,))          # not a step -> index 0 (off)

        civ._query_data = fake_unknown
        self.assertEqual(await civ._get_attenuator(), 0)

    async def test_get_model_id_reads_19_00(self):
        civ = CivController("/dev/null")
        seen = []

        async def fake_query(command, sub=None, timeout=None):
            seen.append((command, sub))
            return bytes((0xB2,))

        civ._query_data = fake_query
        self.assertEqual(await civ.get_model_id(), bytes((0xB2,)))
        self.assertEqual(seen, [(0x19, 0x00)])

    async def test_get_model_id_none_when_radio_does_not_answer(self):
        civ = CivController("/dev/null")

        async def fake_query(command, sub=None, timeout=None):
            return None

        civ._query_data = fake_query
        self.assertIsNone(await civ.get_model_id())

    async def test_get_model_id_none_when_reply_is_empty(self):
        civ = CivController("/dev/null")

        async def fake_query(command, sub=None, timeout=None):
            return b""

        civ._query_data = fake_query
        self.assertIsNone(await civ.get_model_id())


if __name__ == "__main__":
    unittest.main()
