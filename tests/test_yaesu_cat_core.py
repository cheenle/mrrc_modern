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
from dataclasses import replace
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

    def feed(self, text):
        """Queue one more answer (``read(n)`` returns the whole buffer)."""
        self._rx.extend(text.encode("ascii"))

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
        """A memory-mode MD set does not persist (ftx1_mode.c).

        The ``VM`` answer is ``VM`` + P1 (main/sub) + P2P2, and ``11`` is a
        memory channel — the earlier double used ``VM1;``, which encoded the
        slice-by-P1 bug this test now guards against.
        """
        ctrl, fake = _controller(["VM011;", "MD02;"], model="ftx1")
        self.assertTrue(await ctrl.set_mode(0x2))
        self.assertEqual(fake.writes, [b"VM0;", b"VM000;", b"MD02;"])

    async def test_ftx1_skips_the_leave_step_when_already_in_vfo_mode(self):
        ctrl, fake = _controller(["VM000;"], model="ftx1")
        await ctrl.set_mode(0x2)
        self.assertEqual(fake.writes, [b"VM0;", b"MD02;"])

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


class FTX1MemoryAndReadbackTests(unittest.IsolatedAsyncioTestCase):
    """FTX-1 only: leaving a memory channel, and proving a set landed.

    Every case here is behaviour the other four Yaesu profiles must **not**
    pick up — the field report (2026-10-28) was one radio, and the shared
    transport serves all five.  The negative controls at the bottom are as
    important as the positive ones.
    """

    async def test_ftx1_leaves_memory_before_a_frequency_set(self):
        """The reported symptom: a memory-channel radio ignores `FA`."""
        ctrl, fake = _controller(["VM011;"], model="ftx1")
        self.assertTrue(await ctrl.set_frequency(29_570_556))
        self.assertEqual(fake.writes, [b"VM0;", b"VM000;", b"FA029570556;"])

    async def test_ftx1_probes_memory_mode_once_per_interval(self):
        """A tuning drag must not spend a query on every single set."""
        ctrl, fake = _controller(["VM000;"], model="ftx1")
        await ctrl.set_frequency(29_570_556)
        await ctrl.set_frequency(29_537_810)
        self.assertEqual(fake.writes.count(b"VM0;"), 1)

    async def test_no_other_model_probes_memory_mode_when_tuning(self):
        for model in ("ftdx10", "ftdx101d", "ftdx101mp", "ft891"):
            with self.subTest(model=model):
                ctrl, fake = _controller([], model=model)
                await ctrl.set_frequency(29_570_556)
                self.assertEqual(fake.writes, [b"FA029570556;"])

    async def test_ftx1_warns_when_the_radio_did_not_take_the_frequency(self):
        ctrl, fake = _controller(["FA029600000;"], model="ftx1")
        self.assertEqual(await ctrl.get_frequency("A"), 29_600_000)
        fake.feed("VM000;")                # the probe's answer: VFO mode
        with self.assertLogs("backends.yaesu.cat_core", level="WARNING") as logs:
            await ctrl.set_frequency(29_570_556)
            fake.feed("FA029600000;")      # the poll: the radio never moved
            self.assertEqual(await ctrl.get_frequency("A"), 29_600_000)
        self.assertIn("did not take", "\n".join(logs.output))

    async def test_ftx1_is_quiet_when_the_frequency_lands(self):
        ctrl, fake = _controller(["FA029600000;"], model="ftx1")
        await ctrl.get_frequency("A")
        fake.feed("VM000;")
        with self.assertNoLogs("backends.yaesu.cat_core", level="WARNING"):
            await ctrl.set_frequency(29_570_556)
            fake.feed("FA029570556;")
            self.assertEqual(await ctrl.get_frequency("A"), 29_570_556)

    async def test_ftx1_is_quiet_when_a_newer_set_moved_it(self):
        """Two sets in flight: a stale answer to the first is not a failure."""
        ctrl, fake = _controller(["FA029600000;"], model="ftx1")
        await ctrl.get_frequency("A")
        fake.feed("VM000;")
        with self.assertNoLogs("backends.yaesu.cat_core", level="WARNING"):
            await ctrl.set_frequency(29_570_556)
            await ctrl.set_frequency(29_537_810)   # probe is cached now
            fake.feed("FA029570556;")     # first set landed, second not yet
            self.assertEqual(await ctrl.get_frequency("A"), 29_570_556)
            fake.feed("FA029537810;")
            self.assertEqual(await ctrl.get_frequency("A"), 29_537_810)

    async def test_no_other_model_judges_the_readback(self):
        ctrl, fake = _controller(["FA029600000;"], model="ftdx10")
        await ctrl.get_frequency("A")
        with self.assertNoLogs("backends.yaesu.cat_core", level="WARNING"):
            await ctrl.set_frequency(29_570_556)
            fake.feed("FA029600000;")
            self.assertEqual(await ctrl.get_frequency("A"), 29_600_000)

    async def test_ftx1_reports_a_rejected_set(self):
        """`?;` is the only witness a write-only set has (see 附录 B item 2)."""
        ctrl, fake = _controller(["?;"], model="ftx1")
        with self.assertLogs("backends.yaesu.cat_core", level="WARNING") as logs:
            self.assertTrue(await ctrl.set("MG0123"))
        self.assertIn("rejected", "\n".join(logs.output))
        self.assertEqual(fake.writes, [b"MG0123;"])

    async def test_ftx1_is_silent_when_a_set_is_accepted(self):
        ctrl, fake = _controller([], model="ftx1")
        with self.assertNoLogs("backends.yaesu.cat_core", level="WARNING"):
            self.assertTrue(await ctrl.set("MG0123"))

    async def test_no_other_model_reads_the_set_answer(self):
        """The FT-710 path is hardware-verified: leave its writes alone."""
        ctrl, fake = _controller(["?;"], model="ftdx10")
        with self.assertNoLogs("backends.yaesu.cat_core", level="WARNING"):
            self.assertTrue(await ctrl.set("MG0123"))
        self.assertEqual(fake.writes, [b"MG0123;"])


class FTX1CommandFormTests(unittest.IsolatedAsyncioTestCase):
    """The FTX-1 set-command forms its reference manual states exactly.

    These are not new features: every one of them is a control the web page
    already offers and that currently does nothing on this radio, because the
    shared transport sends the FT-710 shape.  Only the forms the manual gives
    unambiguously are changed — `PR` (1/2 vs the FT-710 errata) and `KP` (a
    00–75 index whose unit is unknown) are deliberately left alone.

    Each case is paired with a negative control: the other four profiles must
    keep the family form byte for byte.
    """

    async def test_ftx1_mic_gain_drops_the_channel_digit(self):
        ctrl, fake = _controller([], model="ftx1")
        await ctrl.set_mic_gain(50)
        self.assertEqual(fake.writes, [b"MG050;"])

    async def test_family_mic_gain_keeps_the_channel_digit(self):
        ctrl, fake = _controller([], model="ftdx10")
        await ctrl.set_mic_gain(50)
        self.assertEqual(fake.writes, [b"MG0050;"])

    async def test_ftx1_proc_level_and_amc_drop_the_channel_digit(self):
        ctrl, fake = _controller([], model="ftx1")
        await ctrl.set_compressor_level(50)
        await ctrl.set_amc_level(50)
        self.assertEqual(fake.writes, [b"PL050;", b"AO050;"])

    async def test_family_proc_level_and_amc_keep_the_channel_digit(self):
        ctrl, fake = _controller([], model="ftdx10")
        await ctrl.set_compressor_level(50)
        await ctrl.set_amc_level(50)
        self.assertEqual(fake.writes, [b"PL0050;", b"AO0050;"])

    async def test_ftx1_nr_level_is_two_digits(self):
        """`RL` + P1 + 2 digits; three digits answer `?`."""
        ctrl, fake = _controller([], model="ftx1")
        await ctrl.set_nr_level(5)
        self.assertEqual(fake.writes, [b"RL005;"])

    async def test_family_nr_level_stays_three_digits(self):
        ctrl, fake = _controller([], model="ftdx10")
        await ctrl.set_nr_level(5)
        self.assertEqual(fake.writes, [b"RL0005;"])

    async def test_ftx1_nr_toggle_uses_the_value_command(self):
        """FTX-1 has no `NR`; the switch lives in `RL`'s 00 (docs §4)."""
        ctrl, fake = _controller([], model="ftx1")
        await ctrl.set_noise_reduction(True)
        await ctrl.set_noise_reduction(False)
        self.assertEqual(fake.writes, [b"RL005;", b"RL000;"])

    async def test_ftx1_nr_toggle_remembers_the_level_it_was_set_to(self):
        ctrl, fake = _controller([], model="ftx1")
        await ctrl.set_nr_level(8)
        await ctrl.set_noise_reduction(False)
        await ctrl.set_noise_reduction(True)
        self.assertEqual(fake.writes, [b"RL008;", b"RL000;", b"RL008;"])

    async def test_ftx1_nb_toggle_uses_the_value_command(self):
        """FTX-1 has no `NB`; `NL` 000 is off (docs §7)."""
        ctrl, fake = _controller([], model="ftx1")
        await ctrl.set_noise_blanker(True)
        await ctrl.set_noise_blanker(False)
        self.assertEqual(fake.writes, [b"NL0005;", b"NL0000;"])

    async def test_family_nb_and_nr_toggles_keep_their_own_commands(self):
        ctrl, fake = _controller([], model="ftdx10")
        await ctrl.set_noise_blanker(True)
        await ctrl.set_noise_reduction(True)
        self.assertEqual(fake.writes, [b"NB01;", b"NR01;"])

    async def test_ftx1_attenuator_is_on_off(self):
        """`RA` P2 is 0/1 on this model — the step index was out of range ≥2."""
        ctrl, fake = _controller([], model="ftx1")
        await ctrl.set_attenuator(1)
        await ctrl.set_attenuator(0)
        await ctrl.set_attenuator(7)          # any non-zero index means ON
        self.assertEqual(fake.writes, [b"RA01;", b"RA00;", b"RA01;"])

    async def test_family_attenuator_still_sends_the_step_index(self):
        ctrl, fake = _controller([], model="ftdx10")
        await ctrl.set_attenuator(2)
        self.assertEqual(fake.writes, [b"RA02;"])

    async def test_ftx1_power_set_carries_the_detected_class(self):
        """`PC` write needs P1 = 1 (body) / 2 (SPA-1); the read already says which."""
        ctrl, fake = _controller(["PC1005;"], model="ftx1")
        await ctrl.set_rf_power(5)
        self.assertEqual(fake.writes, [b"PC;", b"PC1005;"])   # the read, then the set

    async def test_ftx1_power_set_follows_the_100w_configuration(self):
        ctrl, fake = _controller(["PC2025;"], model="ftx1")
        await ctrl.set_rf_power(50)
        self.assertEqual(fake.writes, [b"PC;", b"PC2050;"])

    async def test_family_power_set_has_no_class_digit(self):
        ctrl, fake = _controller([], model="ft891")
        await ctrl.set_rf_power(50)
        self.assertEqual(fake.writes, [b"PC050;"])

    async def test_ftx1_preamp_carries_the_band_digit(self):
        """`PA` P1 is the band: 0 HF/50, 1 VHF, 2 UHF (fixed 0 broke 144/430)."""
        for answer, freq, expected in (("FA144500000;", 144_500_000, b"PA11;"),
                                       ("FA007074000;", 7_074_000, b"PA01;"),
                                       ("FA435000000;", 435_000_000, b"PA21;")):
            with self.subTest(freq=freq):
                ctrl, fake = _controller([answer], model="ftx1")
                self.assertEqual(await ctrl.get_frequency("A"), freq)
                await ctrl.set_preamp(1)
                self.assertEqual(fake.writes[-1], expected)

    async def test_ftx1_preamp_without_a_known_frequency_stays_on_hf(self):
        ctrl, fake = _controller([], model="ftx1")
        await ctrl.set_preamp(1)
        self.assertEqual(fake.writes, [b"PA01;"])

    async def test_family_preamp_has_no_band_digit(self):
        ctrl, fake = _controller(["FA144500000;"], model="ftdx10")
        await ctrl.get_frequency("A")
        await ctrl.set_preamp(2)
        self.assertEqual(fake.writes[-1], b"PA02;")        # FA; then PA02;


class FilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_filter_width_uses_the_two_digit_slot(self):
        ctrl, fake = _controller([])
        await ctrl.set_filter_width(3)
        self.assertEqual(fake.writes, [b"SH0003;"])

    async def test_the_sh_prefix_comes_from_the_profile(self):
        """The FT-891 needs P2=1 (Hamlib newcat.c:9658-9663, `int on = is_ft891`).

        Driven through `replace()` on an existing profile so this test does not
        depend on the ft891 registry key existing yet.
        """
        profile = replace(get_profile("ftdx10"), filter_width_prefix="SH01")
        ctrl = YaesuCatController("/dev/null", baudrate=38400, profile=profile)
        fake: Any = _FakeSerial([])
        ctrl._ser = fake
        ctrl._connected = True
        await ctrl.set_filter_width(3)
        self.assertEqual(fake.writes, [b"SH0103;"])

    async def test_every_family_model_still_sends_sh00(self):
        """Byte-identical to the pre-change behaviour, per model."""
        for model in ("ftdx10", "ftdx101d", "ftdx101mp", "ftx1"):
            ctrl, fake = _controller([], model=model)
            await ctrl.set_filter_width(3)
            self.assertEqual(fake.writes, [b"SH0003;"], model)

    async def test_get_filter_width_parses_both_prefixes(self):
        """The read-back takes the trailing two digits, so it is prefix-agnostic."""
        for answer in ("SH0003;", "SH0103;"):
            ctrl, _fake = _controller([answer])
            self.assertEqual(await ctrl.get_filter_width(), 3, answer)

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

    async def test_preamp_and_attenuator_read_back_the_step_index(self):
        """The two settings the poll tiers and initial_state_sync both ask for.

        They were referenced by `backend.settings_poll_items()` and
        `cat_core.initial_state_sync()` without ever existing on the
        controller, so the first successful connect raised AttributeError —
        invisible until then because no model in this family had hardware to
        connect to. Answers carry the step *index* (PA01 = AMP1, RA02 = the
        third attenuator step), which is what RadioState stores.
        """
        ctrl, fake = _controller(["PA01;"])
        self.assertEqual(await ctrl.get_preamp(), 1)
        self.assertEqual(fake.writes, [b"PA0;"])

        ctrl, fake = _controller(["RA02;"])
        self.assertEqual(await ctrl.get_attenuator(), 2)
        self.assertEqual(fake.writes, [b"RA0;"])

    async def test_preamp_and_attenuator_tolerate_a_silent_radio(self):
        for answer in ("", "PA0X;"):
            ctrl, _fake = _controller([answer] if answer else [])
            self.assertIsNone(await ctrl.get_preamp(), repr(answer))
            self.assertIsNone(await ctrl.get_attenuator(), repr(answer))


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
        self.assertEqual(fake.writes[-1], b"PC1010;")  # clamped to 10 W, class kept

    async def test_ftx1_unknown_configuration_falls_back_to_the_profile_max(self):
        ctrl, fake = _controller([], model="ftx1")                 # no PC answer
        ctrl._timeout = 0.05
        await ctrl.detect_power_config()
        self.assertEqual(await ctrl.effective_power_max(), 100)


if __name__ == "__main__":
    unittest.main()
