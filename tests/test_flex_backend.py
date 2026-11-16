"""Contract tests for the FlexRadio backend (P4, design §7-§8).

Everything here is offline: the CAT layer is driven with a recording session
stub, so what is asserted is **which command a UI action becomes** and which
capability bits the frontend is allowed to see.
"""

import asyncio
import pathlib
import unittest
from typing import Any, cast

from backends.base import RadioBackend, RadioCapabilities
from backends.flex import protocol, vita
from backends.flex.engine import SmartSDREngine
from backends.flex.backend import (
    MODEL_KEY, VERIFIED, FlexBackend, _dbm_to_raw, _raw_to_dbm, _raw_to_s_unit,
    _raw_to_swr,
)
import config


class _RecordingSession:
    """Minimal stand-in for ``SmartSDRSession``."""

    def __init__(self, ok: bool = True):
        self.commands = []
        self.ok = ok
        self.handle = 0xDE300001
        self.interlock_state = "READY"
        self.connected = True

    async def send_ok(self, command, timeout=None):
        self.commands.append(command)
        return self.ok

    create_reply = ""
    pan_reply = ""

    async def close(self):
        self.commands.append("close")

    async def send(self, command, timeout=None):
        self.commands.append(command)
        if command.startswith("slice create"):
            body = self.create_reply
        elif command.startswith("display panafall create"):
            body = self.pan_reply
        else:
            body = ""
        return protocol.Response(seq=1, code=0 if self.ok else 0x50000016, body=body)


def _make_backend() -> FlexBackend:
    # The abstract surface is satisfied by the delegate table built from the
    # ABC at import time (same pattern as the SunSDR/Yaesu backends), which a
    # static checker cannot see.
    return FlexBackend()  # type: ignore[abstract]


def _backend_with_recorder(ok: bool = True):
    backend = _make_backend()
    session = _RecordingSession(ok=ok)
    backend.engine.session = cast(Any, session)
    backend.engine.connected = True
    backend.engine.own_slice = 0
    backend.engine.slices[0] = {"freq_hz": 14_074_000, "mode": "USB",
                                "filter_lo": 0, "filter_hi": 2700,
                                "audio_level": 40}
    return backend, session


class ConstructionTests(unittest.TestCase):
    def test_backend_satisfies_the_abc(self):
        backend = _make_backend()
        self.assertIsInstance(backend, RadioBackend)
        self.assertEqual(FlexBackend.__abstractmethods__, frozenset())

    def test_controller_answers_every_abstract_method(self):
        """The delegate table is built from the ABC; a new abstract method
        must not silently become an AttributeError at runtime."""
        backend = _make_backend()
        # The ABC's three properties are answered by the backend itself.
        owned = {"capabilities", "connected", "model"}
        missing = [name for name in RadioBackend.__abstractmethods__ - owned
                   if not hasattr(backend.cat, name)]
        self.assertEqual(missing, [])

    def test_capabilities_are_a_radiacapabilities(self):
        self.assertIsInstance(_make_backend().capabilities, RadioCapabilities)

    def test_state_tables_configure_the_shared_radio_state(self):
        from radio_state import RadioState
        state = RadioState()
        state.configure(**_make_backend().state_tables())   # raises on unknown


class CapabilityTests(unittest.TestCase):
    def test_transmit_is_gated_while_unverified(self):
        caps = _make_backend().capabilities
        self.assertFalse(VERIFIED)
        self.assertFalse(caps.verified)
        self.assertTrue(caps.tx_gated)

    def test_gate_lifts_with_the_env_opt_in(self):
        original = config.ALLOW_UNVERIFIED_TX
        config.ALLOW_UNVERIFIED_TX = True
        try:
            self.assertFalse(_make_backend().capabilities.tx_gated)
        finally:
            config.ALLOW_UNVERIFIED_TX = original

    def test_unsupported_controls_are_hidden(self):
        caps = _make_backend().capabilities
        self.assertFalse(caps.has_split)
        self.assertFalse(caps.has_vfo_b)
        self.assertFalse(caps.has_radio_power)
        self.assertFalse(caps.dual_rx)
        # rfgain 能下发，但**量程未证**（99 被拒："RF Gain out of range"），
        # 在知道上限之前不显示那个按 0-255 百分比解释的滑块。
        self.assertFalse(caps.has_rf_gain)

    def test_atu_follows_the_radio_not_the_model(self):
        backend = _make_backend()
        self.assertFalse(backend.capabilities.has_atu)
        backend.engine._handle_status(protocol.parse_status("S0|atu 0 state=READY"))
        self.assertTrue(backend.capabilities.has_atu)

    def test_display_name_uses_the_radios_own_model_string(self):
        backend = _make_backend()
        self.assertEqual(backend.capabilities.display_name, "FlexRadio (SmartSDR)")
        backend.engine.model = "FLEX-6400"
        self.assertEqual(backend.capabilities.display_name, "FlexRadio FLEX-6400")

    def test_scope_spans_are_full_widths_not_half_widths(self):
        caps = _make_backend().capabilities
        self.assertEqual(caps.scope_type, "flex_vita")
        self.assertEqual(caps.scope_spans[3]["freq"], 100_000)

    def test_poll_tables_are_empty_because_the_radio_pushes(self):
        backend = _make_backend()
        self.assertEqual(backend.settings_poll_items(), [])
        self.assertEqual(backend.slow_poll_items(), [])
        names = [item[1] for item in backend.always_meter_items()]
        self.assertIn("s_meter", names)


class EnsurePanTests(unittest.IsolatedAsyncioTestCase):
    """A refused `display panafall create` must never become a pan id.

    Field 2026-11-16: the radio's panadapter slots were taken, `create` answered an
    error, `_first_int()` scraped a number out of the error body and the engine
    then sent `display pan set 0x40000000 …` — i.e. it retuned the other
    operator's waterfall.  A box may also simply be full.
    """

    def _engine(self):
        engine = SmartSDREngine("127.0.0.1")
        engine.session = _RecordingSession(ok=False)
        return engine

    async def test_a_refused_create_with_no_pan_of_ours_fails_cleanly(self):
        engine = self._engine()
        self.assertFalse(await engine.ensure_pan())
        self.assertEqual(engine.pan_id, 0)

    async def test_a_refused_create_reuses_the_pan_that_carries_our_handle(self):
        engine = self._engine()
        handle = f"0x{engine.session.handle:08X}"
        engine.pans[0x40000003] = {"client_handle": handle}
        self.assertTrue(await engine.ensure_pan())
        self.assertEqual(engine.pan_id, 0x40000003)

    async def test_the_session_does_not_run_half_built_without_a_pan(self):
        """No panadapter → no session: `_slice_id()` would otherwise drift onto
        another client's slice (the bug `EnsureSliceTests` documents)."""
        engine = SmartSDREngine("127.0.0.1")
        engine.session = _RecordingSession(ok=False)
        engine._probe_radio = lambda: asyncio.sleep(0)
        self.assertFalse(await engine._connect_once())
        self.assertFalse(engine.connected)

    async def test_a_successful_create_uses_the_reply_id(self):
        engine = SmartSDREngine("127.0.0.1")
        session = _RecordingSession()
        session.create_reply = ""
        session.pan_reply = "0x40000007,0x42000007"
        engine.session = session
        self.assertTrue(await engine.ensure_pan())
        self.assertEqual(engine.pan_id, 0x40000007)


class ReleaseResourcesTests(unittest.IsolatedAsyncioTestCase):
    """`stop()` must give the panadapter back — a reconnect used to leak one.

    Field 2026-11-16: after the crash loop (98 restarts) the radio reported
    ``available_panadapters=0``, so the next connection could not create one
    (`0x50000009`) and the engine served nothing at all.
    """

    async def test_our_slice_and_panadapter_are_removed_on_stop(self):
        engine = SmartSDREngine("127.0.0.1")
        session = _RecordingSession()
        engine.session = session
        engine.connected = True
        engine.pan_id = 0x40000002
        engine.own_slice = 1
        engine.slices[1] = {"client_handle": f"0x{session.handle:08X}",
                            "pan_id": 0x40000002}
        await engine.stop()
        self.assertIn("slice remove 1", session.commands)
        self.assertIn("display panafall remove 1073741826", session.commands)
        self.assertEqual(engine.pan_id, 0)

    async def test_another_clients_slice_is_not_deleted(self):
        engine = SmartSDREngine("127.0.0.1")
        session = _RecordingSession()
        engine.session = session
        engine.pan_id = 0x40000002
        engine.own_slice = 1
        engine.slices[1] = {"client_handle": "0x650F1147", "pan_id": 0x40000002}
        await engine.stop()
        self.assertNotIn("slice remove 1", session.commands)
        self.assertIn("display panafall remove 1073741826", session.commands)


class EnsureSliceTests(unittest.IsolatedAsyncioTestCase):
    """The engine must drive a slice on **its own** panadapter (field 2026-11-16).

    On a radio where another client creates the slices, none ever carries our
    handle, so `own_slice` stayed None and `_slice_id()` fell back to
    ``min(slices)`` — the other operator's slice.  Tuning then wrote his
    frequency and an antenna change would have switched his connector.
    """

    def _engine(self, slices):
        engine = SmartSDREngine("127.0.0.1")
        engine.pan_id = 0x40000002
        engine.slices = slices
        session = _RecordingSession()
        engine.session = session
        return engine, session

    async def test_the_active_slice_on_our_pan_wins(self):
        engine, session = self._engine({
            0: {"pan_id": 0x40000001, "client_handle": "0x650F1147", "active": 1},
            1: {"pan_id": 0x40000002, "client_handle": "0x5C097BF7", "active": 0},
            2: {"pan_id": 0x40000002, "client_handle": "0x5C097BF7", "active": 1},
        })
        self.assertTrue(await engine.ensure_slice())
        self.assertEqual(engine.own_slice, 2)
        self.assertNotIn("slice set 2 active=1", session.commands)   # already active

    async def test_an_inactive_slice_on_our_pan_is_activated(self):
        engine, session = self._engine({
            0: {"pan_id": 0x40000001, "client_handle": "0x650F1147", "active": 1},
            1: {"pan_id": 0x40000002, "client_handle": "0x5C097BF7", "active": 0},
        })
        self.assertTrue(await engine.ensure_slice())
        self.assertEqual(engine.own_slice, 1)
        self.assertIn("slice set 1 active=1", session.commands)

    async def test_our_own_handle_beats_the_pan_heuristic(self):
        session = _RecordingSession()
        mine = f"0x{session.handle:08X}"
        engine, session = self._engine({
            0: {"pan_id": 0x40000001, "client_handle": "0x650F1147", "active": 1},
            1: {"pan_id": 0x40000002, "client_handle": "0x5C097BF7", "active": 1},
            2: {"pan_id": 0x40000003, "client_handle": mine, "active": 0},
        })
        self.assertTrue(await engine.ensure_slice())
        self.assertEqual(engine.own_slice, 2)

    async def test_a_radio_with_no_slice_on_our_pan_gets_one_created(self):
        engine, session = self._engine({
            0: {"pan_id": 0x40000001, "client_handle": "0x650F1147", "active": 1},
        })
        session.create_reply = "3"
        self.assertTrue(await engine.ensure_slice())
        self.assertIn("slice create pan=1073741826", session.commands)
        self.assertEqual(engine.own_slice, 3)


class AntennaSelectionTests(unittest.IsolatedAsyncioTestCase):
    """`slice set <n> rxant=<name>` — the only command that routes RF.

    Measured on a FLEX-6600 (v4.3.16, 2026-11-16): slices carry
    `rxant`/`txant`/`ant_list`, and there is **no** `ant` key, so the old
    `ant=ANT1` was accepted and ignored — the box stayed on whatever connector
    the radio had chosen and could be tuned and deaf at the same time.
    """

    ANTENNAS = ["ANT1", "ANT2", "RX_A", "RX_B", "XVTA", "XVTB"]

    async def test_the_name_is_sent_as_rxant(self):
        backend, session = _backend_with_recorder()
        backend.engine.slices[0]["antenna_list"] = list(self.ANTENNAS)
        self.assertTrue(await backend.cat.set_antenna_name("ANT2"))
        self.assertEqual(session.commands[-1], "slice set 0 rxant=ANT2")
        self.assertEqual(session.commands[-1].split()[-1], "rxant=ANT2")

    async def test_the_name_is_case_insensitive(self):
        backend, session = _backend_with_recorder()
        backend.engine.slices[0]["antenna_list"] = list(self.ANTENNAS)
        self.assertTrue(await backend.cat.set_antenna_name("rx_b"))
        self.assertEqual(session.commands[-1], "slice set 0 rxant=RX_B")

    async def test_a_connector_the_radio_does_not_have_is_refused(self):
        """Better a visible refusal than a command the radio silently drops."""
        backend, session = _backend_with_recorder()
        backend.engine.slices[0]["antenna_list"] = list(self.ANTENNAS)
        before = len(session.commands)
        self.assertFalse(await backend.cat.set_antenna_name("ANT3"))
        self.assertEqual(len(session.commands), before)

    async def test_the_legacy_integer_picks_from_the_radios_list(self):
        backend, session = _backend_with_recorder()
        backend.engine.slices[0]["antenna_list"] = list(self.ANTENNAS)
        self.assertTrue(await backend.cat.set_antenna(3))        # 1-based
        self.assertEqual(session.commands[-1], "slice set 0 rxant=RX_A")

    async def test_the_transmit_antenna_is_per_slice_and_validated(self):
        backend, session = _backend_with_recorder()
        backend.engine.slices[0]["tx_ant_list"] = ["ANT1", "ANT2"]
        self.assertTrue(await backend.cat.set_tx_antenna_name("ANT2"))
        self.assertEqual(session.commands[-1], "slice set 0 txant=ANT2")
        before = len(session.commands)
        # RX_A/RX_B are receive-only: the radio does not offer them for TX.
        self.assertFalse(await backend.cat.set_tx_antenna_name("RX_A"))
        self.assertEqual(len(session.commands), before)

    async def test_the_state_sync_carries_the_list_and_the_current_one(self):
        backend, _session = _backend_with_recorder()
        backend.engine.slices[0].update(
            {"rxant": "ANT2", "antenna_list": list(self.ANTENNAS)})
        sync = await backend.cat.initial_state_sync()
        self.assertEqual(sync["antenna_name"], "ANT2")
        self.assertEqual(sync["antenna_list"], self.ANTENNAS)
        self.assertEqual(sync["antenna"], 2)                     # ANT2 is the 2nd


class CommandMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_receive_gain_controls_map_to_the_radio_fields(self):
        """rfgain / agc_threshold — 决定"信号强不强"的两个旋钮。

        厂商的调法是「预放 → AGC 速度 → AGC-T」，而我们此前一个都没下发
        （`set_rf_gain` 直接 return False，AGC-T 根本没有），于是 LSB 下信号
        微弱时操作员无从下手（现场 2026-11-16）。
        """
        backend, session = _backend_with_recorder()
        self.assertTrue(await backend.cat.set_rf_gain(40))
        self.assertEqual(session.commands[-1], "slice set 0 rfgain=40")
        self.assertTrue(await backend.cat.set_agc_threshold(80))
        self.assertEqual(session.commands[-1],
                         "slice set 0 agc_threshold=80 agc_threshold_mode=manual")
        # 范围夹在 0..100，而不是把越界值丢给电台
        self.assertTrue(await backend.cat.set_agc_threshold(500))
        self.assertEqual(session.commands[-1],
                         "slice set 0 agc_threshold=100 agc_threshold_mode=manual")

    async def test_tuning_inside_the_span_does_not_drag_the_panadapter(self):
        backend, session = _backend_with_recorder()
        backend.engine.bandwidth_hz = 200_000
        backend.engine.pan_center_hz = 14_100_000
        self.assertTrue(await backend.cat.set_frequency(14_074_000))
        self.assertEqual(session.commands[-1], "slice t 0 14.074000 autopan=0")
        self.assertFalse(any(c.startswith("display pan set") for c in session.commands))

    async def test_tuning_outside_the_span_recentres_the_panadapter(self):
        """音频在 15.8 MHz、瀑布停在 14.15 MHz = 「有声但看不到信号」。

        现场 2026-11-16：`autopan=0` 让跨段调谐后 pan 留在旧频段，操作员看到的是
        另一条带，S 表/音频都正常，于是"信号在哪"永远找不到。
        """
        backend, session = _backend_with_recorder()
        backend.engine.bandwidth_hz = 200_000
        backend.engine.pan_center_hz = 14_150_000
        backend.engine.pan_id = 0x40000002
        self.assertTrue(await backend.cat.set_frequency(15_800_000))
        self.assertIn("display pan set 1073741826 center=15.800000", session.commands)
        self.assertEqual(backend.engine.pan_center_hz, 15_800_000)

    async def test_usb_mode_sets_the_voice_passband(self):
        backend, session = _backend_with_recorder()
        self.assertTrue(await backend.cat.set_mode(2))          # USB
        self.assertEqual(session.commands[-1],
                         "slice set 0 mode=USB filter_lo=0 filter_hi=2700")

    async def test_cw_mode_centres_the_filter_on_the_pitch(self):
        backend, session = _backend_with_recorder()
        self.assertTrue(await backend.cat.set_mode(4))          # CW-U
        command = session.commands[-1]
        self.assertIn("mode=CW", command)
        # A mode switch lands on that mode's default width: 500 Hz for CW,
        # centred on the 700 Hz pitch -> 450..950 Hz.
        self.assertIn("filter_lo=450", command)
        self.assertIn("filter_hi=950", command)

    async def test_filter_index_maps_to_a_width(self):
        backend, session = _backend_with_recorder()
        self.assertTrue(await backend.cat.set_filter_width(1))
        self.assertIn("filter_hi=2100", session.commands[-1])
        self.assertEqual(await backend.cat.get_filter_width(), 1)

    async def test_power_and_mic_gain_go_through_transmit_set(self):
        backend, session = _backend_with_recorder()
        await backend.cat.set_rf_power(50)
        await backend.cat.set_mic_gain(30)
        self.assertEqual(session.commands[0], "transmit set rfpower=50")
        self.assertEqual(session.commands[1], "transmit set miclevel=30")

    async def test_compressor_level_collapses_to_three_steps(self):
        backend, session = _backend_with_recorder()
        await backend.cat.set_compressor_level(80)
        self.assertEqual(session.commands[-1],
                         "transmit set speech_processor_level=2")

    async def test_unsupported_surfaces_answer_false_without_a_command(self):
        backend, session = _backend_with_recorder()
        for coro in (backend.cat.set_vfo("B"), backend.cat.set_split(True),
                     backend.cat.set_power(True), backend.cat.set_preamp(1),
                     backend.cat.set_attenuator(1),
                     backend.cat.set_contour(10), backend.cat.set_key_speed(20)):
            self.assertFalse(await coro)
        self.assertEqual(session.commands, [])

    async def test_ptt_is_refused_while_the_gate_is_closed(self):
        backend, session = _backend_with_recorder()
        self.assertFalse(await backend.cat.set_ptt(True))
        self.assertEqual(session.commands, [])

    async def test_unconfirmed_audio_path_refuses_key_up(self):
        backend, session = _backend_with_recorder()
        backend.tx_audio_path_unconfirmed = True
        original = config.ALLOW_UNVERIFIED_TX
        config.ALLOW_UNVERIFIED_TX = True
        try:
            self.assertFalse(await backend.cat.set_ptt(True))
        finally:
            config.ALLOW_UNVERIFIED_TX = original
        self.assertEqual(session.commands, [])

    async def test_release_is_never_gated(self):
        """A gate that can block an unkey is a hazard, not a safety feature."""
        backend, session = _backend_with_recorder()
        backend.tx_audio_path_unconfirmed = True
        await backend.cat.set_ptt(False)
        self.assertIn("xmit 0", session.commands)

    async def test_break_in_uses_the_cw_command(self):
        backend, session = _backend_with_recorder()
        await backend.cat.set_break_in(True)
        self.assertEqual(session.commands[-1], "cw break_in 1")

    async def test_failed_command_reports_false(self):
        backend, session = _backend_with_recorder(ok=False)
        self.assertFalse(await backend.cat.set_rf_power(50))


class MeterMappingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        backend, _ = _backend_with_recorder()
        self.backend = backend
        backend.engine._handle_status(protocol.parse_status(
            "S0|meter 7.src=SLC#7.nam=LEVEL#7.unit=dBm#7.fps=10"
            "#15.src=TX-#15.nam=FWDPWR#15.unit=dBm"
            "#17.src=TX-#17.nam=SWR#17.unit=SWR"
            "#18.src=TX-#18.nam=ALC#18.unit=dBFS"))
        backend.engine.meter_values.update({7: -9344, 15: 5120, 17: 256, 18: 0})

    async def test_s_meter_maps_dbm_into_the_shared_raw_scale(self):
        raw = await self.backend.cat.get_s_meter()
        assert raw is not None
        self.assertAlmostEqual(_raw_to_dbm(raw), -73.0, delta=1.0)
        self.assertEqual(_raw_to_s_unit(raw), 9)

    async def test_power_and_swr_come_from_the_radio_meters(self):
        power = await self.backend.cat.get_meter("power")
        swr = await self.backend.cat.get_meter("swr")
        assert power is not None and swr is not None
        self.assertGreater(power, 0)
        self.assertAlmostEqual(_raw_to_swr(swr), 2.0, delta=0.05)  # raw/128 = 2.0

    async def test_unknown_meter_is_none(self):
        self.assertIsNone(await self.backend.cat.get_meter("nope"))

    def test_raw_scale_endpoints(self):
        self.assertEqual(_dbm_to_raw(-140.0), 0)
        self.assertEqual(_dbm_to_raw(-20.0), 255)


class StateSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_state_reports_what_the_radio_sent(self):
        backend, _ = _backend_with_recorder()
        state = await backend.cat.initial_state_sync()
        self.assertEqual(state["freq_hz"], 14_074_000)
        self.assertEqual(state["mode_num"], 2)          # USB
        self.assertEqual(state["af_gain"], 40)


if __name__ == "__main__":
    unittest.main()


class SliceCommandFormTests(unittest.TestCase):
    """Slice-side commands must be ``slice set <n> …``.

    `slice s <n> …` (one missing "set") is **accepted and ignored** by
    SmartSDR: no error, no effect.  That single typo meant mode, filter,
    audio level, RF gain, AGC-T, NB/NR/ANF and the RX/TX antenna never reached
    the radio — the operator's antenna was on ANT2 while the slice listened on
    an empty ANT1, so the box had audio (noise) and a waterfall with no signals
    (2026-11-16, FLEX-6600).
    """

    def test_every_slice_command_uses_set(self):
        backend = pathlib.Path("backends/flex/backend.py").read_text(encoding="utf-8")
        self.assertNotIn('f"slice s {', backend)
        self.assertIn('f"slice set {self._slice_id()} {command}"', backend)

    def test_the_engine_also_uses_set(self):
        engine = pathlib.Path("backends/flex/engine.py").read_text(encoding="utf-8")
        self.assertNotIn('f"slice s {', engine)


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Whole chain against the scripted radio: handshake → pan → streams →
    spectrum → audio.  No hardware, no monkeypatched transport."""

    async def asyncSetUp(self):
        from tests.flex_fake_radio import FakeRadio
        self.radio = await FakeRadio.create()
        self.radio.set_reply("display panafall create", body="pan=0x40000000")
        self.radio.set_reply("stream create type=remote_audio_rx",
                             body="0x04000001")
        self.radio.set_reply("stream create type=remote_audio_tx",
                             body="0x0B000002")
        self.radio.set_reply("info", body="model=FLEX-6400,chassis_serial=1234-5678,"
                                          "software_ver=4.1.5")
        self.backend = FlexBackend(host="127.0.0.1")
        self.backend.engine.port = self.radio.port
        self.assertTrue(await self.backend.cat.connect())

    async def asyncTearDown(self):
        await self.backend.cat.disconnect()
        self.radio.close()

    async def test_handshake_builds_pan_and_both_streams(self):
        commands = self.radio.commands
        self.assertTrue(any(c.startswith("sub slice all") for c in commands))
        self.assertTrue(any(c.startswith("client program") for c in commands))
        self.assertTrue(any(c.startswith("display panafall create") for c in commands))
        self.assertIn("stream create type=remote_audio_rx compression=none",
                      commands)
        self.assertIn("stream create type=remote_audio_tx", commands)
        self.assertEqual(self.backend.engine.model, "FLEX-6400")
        self.assertEqual(self.backend.capabilities.display_name,
                         "FlexRadio FLEX-6400")

    async def test_a_pushed_fft_packet_reaches_the_scope_bins(self):
        from tests.test_flex_engine import _fft_packet, _packet
        # 桶 0–25 是电台 FFT 的固定边缘伪影，引擎会把它们抹平，所以把
        # 有辨识度的值放在中段（2026-11-16）。
        bins = [200] * 850
        bins[30], bins[31] = 0, 255         # top of the aperture / bottom
        await self.radio.push_packet(
            _packet(vita.PCC_FFT, _fft_packet(bins),
                    stream_id=self.backend.engine.pan_id))
        for _ in range(50):
            if self.backend.engine.spectrum_frames:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(self.backend.engine.spectrum_frames, 1)
        out = self.backend.engine.spectrum_bins()
        assert out is not None
        self.assertEqual(out[30], 255)      # brightest byte
        self.assertEqual(out[31], 0)        # darkest byte

    async def test_a_pushed_audio_packet_becomes_a_48k_chunk(self):
        import numpy as np
        from tests.test_flex_engine import _audio_packet
        frames = np.zeros((480, 2), dtype=np.float32)
        # On the stream id the radio answered to `stream create` — the engine now
        # drops datagrams belonging to any other stream (a second client's audio
        # used to be decoded into our resampler).
        await self.radio.push_packet(
            _audio_packet(frames, stream_id=self.backend.engine.rx_stream_id))
        handler = self.backend.create_audio_handler()
        handler._loop = asyncio.get_running_loop()
        handler.start_rx()
        chunk = None
        for _ in range(50):
            chunk = handler.read_rx_chunk()
            if chunk:
                break
            await asyncio.sleep(0.01)
        assert chunk is not None
        self.assertEqual(len(chunk), 1920)          # 960 samples at 48 kHz
        handler.close()
