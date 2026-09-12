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


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_id_is_reported_verbatim(self):
        ctrl, fake = _controller(["ID0840;"])
        self.assertEqual(await ctrl.get_model_id(), "0840")

    async def test_model_id_none_when_silent(self):
        ctrl, fake = _controller([])
        ctrl._timeout = 0.05
        self.assertIsNone(await ctrl.get_model_id(timeout=0.05))


class FrequencyAndVfoTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_frequency_uses_fa_for_a_and_fb_for_b(self):
        ctrl, fake = _controller([])
        await ctrl.set_frequency(7_074_000, vfo="A")
        await ctrl.set_frequency(7_074_000, vfo="B")
        self.assertEqual(fake.writes, [b"FA007074000;", b"FB007074000;"])

    async def test_get_frequency_parses_nine_digit_hz(self):
        ctrl, fake = _controller(["FA007074000;"])
        self.assertEqual(await ctrl.get_frequency("A"), 7_074_000)

    async def test_get_active_vfo_reads_vs(self):
        for answer, expected in (("VS0;", "A"), ("VS1;", "B")):
            ctrl, fake = _controller([answer])
            self.assertEqual(await ctrl.get_active_vfo(), expected)


class ModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_mode_uses_the_profile_register(self):
        """The register is an int (RadioBackend.set_mode(mode_num) contract)."""
        ctrl, fake = _controller([])
        await ctrl.set_mode(0xC)                     # FTDX10 DATA-U
        self.assertEqual(fake.writes, [b"MD0C;"])

    async def test_set_mode_round_trip_for_c4fm(self):
        ctrl, fake = _controller([])
        await ctrl.set_mode(0xE)
        self.assertEqual(fake.writes, [b"MD0E;"])

    async def test_ftx1_c4fm_codes_are_not_hex_formatted(self):
        """0x11 must go out as 'I', never as "MD011" (review finding)."""
        ctrl, fake = _controller([], model="ftx1")
        await ctrl.set_mode(0x11)
        # The FTX-1 profile always checks memory mode first (see the two
        # leave-memory tests below); the mode register is what matters here.
        self.assertEqual(fake.writes[-1], b"MD0I;")
        self.assertNotIn(b"MD011;", fake.writes)

    async def test_unknown_register_is_refused_without_a_write(self):
        ctrl, fake = _controller([])
        self.assertFalse(await ctrl.set_mode(0x99))
        self.assertEqual(fake.writes, [])

    async def test_ftx1_leaves_memory_mode_before_setting_mode(self):
        """A memory-mode MD set does not persist (ftx1_mode.c)."""
        ctrl, fake = _controller(["VM1;", "MD02;"], model="ftx1")
        self.assertTrue(await ctrl.set_mode(0x2))
        self.assertEqual(fake.writes, [b"VM;", b"VM000;", b"MD02;"])

    async def test_ftx1_skips_the_leave_step_when_already_in_vfo_mode(self):
        ctrl, fake = _controller(["VM0;"], model="ftx1")
        await ctrl.set_mode(0x2)
        self.assertEqual(fake.writes, [b"VM;", b"MD02;"])

    async def test_ftdx10_does_not_query_memory_mode(self):
        ctrl, fake = _controller([])
        await ctrl.set_mode(0xC)
        self.assertEqual(fake.writes, [b"MD0C;"])

    async def test_get_mode_reads_the_register_after_md0(self):
        ctrl, fake = _controller(["MD0C;"])
        self.assertEqual(await ctrl.get_mode(), 0xC)

    async def test_ftx1_get_mode_maps_i_back_to_0x11(self):
        """The reverse lookup must handle the non-hex characters too."""
        ctrl, fake = _controller(["MD0I;"], model="ftx1")
        self.assertEqual(await ctrl.get_mode(), 0x11)

    async def test_unknown_mode_character_is_not_guessed(self):
        ctrl, fake = _controller(["MD0Z;"])
        self.assertIsNone(await ctrl.get_mode())


class FilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_filter_width_uses_the_two_digit_slot(self):
        ctrl, fake = _controller([])
        await ctrl.set_filter_width(3)
        self.assertEqual(fake.writes, [b"SH0003;"])

    async def test_get_filter_width_reads_the_slot(self):
        ctrl, fake = _controller(["SH0003;"])
        self.assertEqual(await ctrl.get_filter_width(), 3)


class TransmitTests(unittest.IsolatedAsyncioTestCase):
    async def test_ptt_uses_the_priority_path(self):
        ctrl, fake = _controller([])
        self.assertTrue(await ctrl.set_ptt(True))
        self.assertEqual(fake.writes, [b"TX1;"])
        await ctrl.set_ptt(False)
        self.assertEqual(fake.writes[-1], b"TX0;")

    async def test_tune_is_tx2(self):
        ctrl, fake = _controller([])
        await ctrl.set_tune(True)
        self.assertEqual(fake.writes, [b"TX2;"])

    async def test_get_ptt(self):
        ctrl, fake = _controller(["TX1;"])
        self.assertEqual(await ctrl.get_ptt(), 1)


class MeterAndGainTests(unittest.IsolatedAsyncioTestCase):
    async def test_s_meter_reads_sm0(self):
        ctrl, fake = _controller(["SM0123;"])
        self.assertEqual(await ctrl.get_s_meter(), 123)

    async def test_get_meter_uses_the_first_three_digits(self):
        """RM answers are 'RM' + meter + 6 digits; the raw value is the first 3."""
        ctrl, fake = _controller(["RM5150000;"])
        self.assertEqual(await ctrl.get_meter("RM5"), 150)

    async def test_gain_commands(self):
        ctrl, fake = _controller([])
        await ctrl.set_af_gain(120)
        await ctrl.set_rf_gain(80)
        await ctrl.set_squelch(15)
        await ctrl.set_mic_gain(50)
        self.assertEqual(fake.writes, [b"AG0120;", b"RG0080;",
                                            b"SQ0015;", b"MG0050;"])


class PowerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_models_send_three_digit_watts(self):
        ctrl, fake = _controller([], model="ftdx10")
        await ctrl.set_rf_power(75)
        self.assertEqual(fake.writes, [b"PC075;"])

    async def test_ftx1_detects_the_100w_configuration(self):
        """PC2xxx answer = SPA-1/Optima (5-100 W)."""
        ctrl, fake = _controller(["PC2100;"], model="ftx1")
        self.assertEqual(await ctrl.detect_power_config(), ("PC2", 100))
        self.assertEqual(await ctrl.effective_power_max(), 100)

    async def test_ftx1_detects_the_field_head_configuration(self):
        """PC1xxx answer = Field head (6 W on battery, 10 W on 12 V)."""
        ctrl, fake = _controller(["PC1010;"], model="ftx1")
        self.assertEqual(await ctrl.detect_power_config(), ("PC1", 10))
        self.assertEqual(await ctrl.effective_power_max(), 10)

    async def test_ftx1_clamps_a_request_above_the_detected_maximum(self):
        ctrl, fake = _controller(["PC1010;"], model="ftx1")
        await ctrl.set_rf_power(100)
        self.assertEqual(fake.writes[-1], b"PC010;")   # clamped to 10 W

    async def test_ftx1_unknown_configuration_falls_back_to_the_profile_max(self):
        ctrl, fake = _controller([], model="ftx1")                 # no PC answer
        ctrl._timeout = 0.05
        await ctrl.detect_power_config()
        self.assertEqual(await ctrl.effective_power_max(), 100)


if __name__ == "__main__":
    unittest.main()
