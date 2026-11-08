"""SunSDR2 DX native backend — Task 4: RadioBackend wiring, network audio
adapter and scope adapter over the Task-3 engine.

Everything runs on real UDP loopback (127.0.0.1) with the same fake-radio
pattern as the engine tests: two asyncio datagram endpoints record what the
engine sends and push RX IQ / telemetry back.  No real hardware.

Coverage against the Task-4 spec:

- model registry: "sunsdr2dx" registered, lazy factory, model_verification,
  factory port/baud compatibility;
- capabilities: 48k RX/TX, no USB-audio hints, lan_iq scope with the four
  real spans (half-widths 19531/39062/78125/156250), no ATU/auto-notch,
  no fabricated SWR/ALC, verified=False with the dynamic TX gate;
- the controller behind ``backend.cat`` implements the full RadioBackend
  abstract surface; genuinely supported: connect/freq/mode/PTT/DRIVE,
  combined 4-level ATT/preamp, filters, AGC, S-meter/telemetry, scope spans;
  unsupported items explicitly return False/None and never appear in poll
  lists; initial_state_sync reports real state only;
- TX gate both ways (refuse + one warning per process while gated; release
  never intercepted), TUNE generates a safe 700 Hz test tone through the
  engine TX path and always releases;
- SunSDRAudioHandler matches the AudioHandler surface the server drives
  (start/stop/read/feed/stats, _rx_running/_rx_dev_rate/_rx_last_peak,
  opus_enabled) with Opus → PCM fallback, all over the shared engine;
- SunSDRScopeProducer shares the same engine, writes the ScopeHandler fields,
  never stops the engine on stop(), notify_tx is a no-op;
- engine on_event (telemetry/ptt/connected) reaches the broadcast callback;
- server audio-factory selection: backend factory wins, None falls back to
  the PyAudio AudioHandler with the backend's caps (existing models
  byte-for-byte unchanged).
"""
import asyncio
import json
import logging
import struct
import time
import unittest
from unittest import mock

import numpy as np

import config
from backends import create_backend, known_models, model_verification
from backends.base import RadioBackend
from backends.sunsdr import protocol as sdp
from backends.sunsdr.engine import SunSDREngine

logger = logging.getLogger(__name__)


def _ctrl_cmd(data: bytes) -> int:
    return struct.unpack_from("<H", data, 2)[0]


def _ctrl_trailing(data: bytes) -> int:
    return struct.unpack_from("<I", data, len(data) - 4)[0]


def _stream_fields(data: bytes):
    return struct.unpack_from("<HHIH", data, 0)


def _tone(n, hz=1500.0, fs=78125.0, amp=0.5):
    t = np.arange(n, dtype=np.float64)
    return (amp * np.exp(2j * np.pi * hz * t / fs)).astype(np.complex64)


def _pcm16(n, hz=1000.0, fs=48000, amp=0.4):
    t = np.arange(n, dtype=np.float64)
    return (np.clip(np.rint(amp * 32767.0 * np.sin(2 * np.pi * hz * t / fs)),
                    -32768, 32767).astype("<i2").tobytes())


async def _wait_until(pred, timeout=3.0, interval=0.005):
    t0 = time.monotonic()
    while not pred():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(interval)


class FakeRadio:
    """Two UDP endpoints emulating the SunSDR2 DX device (see Task-3 tests)."""

    def __init__(self):
        self.control = []
        self.stream = []
        self._control_addrs = []
        self._stream_addrs = []
        self._transports = []

    @classmethod
    async def create(cls):
        loop = asyncio.get_running_loop()
        self = cls()

        def make(which):
            log = getattr(self, which)
            addrs = getattr(self, "_" + which + "_addrs")

            class _P(asyncio.DatagramProtocol):
                def datagram_received(self_, data, addr):
                    log.append((addr, bytes(data)))
                    if addr not in addrs:
                        addrs.append(addr)

                def error_received(self_, exc):
                    pass

                def connection_lost(self_, exc):
                    pass

            return _P

        t1, _ = await loop.create_datagram_endpoint(
            make("control"), local_addr=("127.0.0.1", 0))
        t2, _ = await loop.create_datagram_endpoint(
            make("stream"), local_addr=("127.0.0.1", 0))
        self._transports = [t1, t2]
        self.control_port = t1.get_extra_info("sockname")[1]
        self.stream_port = t2.get_extra_info("sockname")[1]
        return self

    def close(self):
        for t in self._transports:
            t.close()

    def control_cmds(self):
        return [_ctrl_cmd(d) for _, d in self.control]

    def stream_subtypes(self):
        return [_stream_fields(d)[1] for _, d in self.stream]

    def _stream_target(self):
        if not self._stream_addrs:
            raise AssertionError("engine has not sent to the stream port yet")
        return self._stream_addrs[0]

    def send_raw_stream(self, data: bytes):
        self._transports[1].sendto(data, self._stream_target())

    def send_iq(self, samples, counter=0x0100, flags=0x0001):
        pkt = (struct.pack("<HHIH", 0xFF32, 0xFFFE, counter, flags)
               + sdp.pack_iq_24bit(samples))
        self.send_raw_stream(pkt)

    def send_power_telemetry(self, fwd_w=55.5, volts=13.1, temp=44.0,
                             counter=0x0200):
        raw = bytearray(34)
        struct.pack_into("<HHIH", raw, 0, 0xFF32, 0x1F00, counter, 0x0001)
        struct.pack_into("<H", raw, 16, int(round(volts * 10)))
        struct.pack_into("<f", raw, 18, temp)
        struct.pack_into("<f", raw, 22, fwd_w / 3.0)
        struct.pack_into("<f", raw, 30, fwd_w)
        self.send_raw_stream(bytes(raw))

    def send_ptt_status(self, tx, counter=0x0300):
        raw = bytearray(22)
        struct.pack_into("<HHIH", raw, 0, 0xFF32, 0x1F01, counter, 0x0001)
        struct.pack_into("<I", raw, 18, 0x00010000 if tx else 0)
        self.send_raw_stream(bytes(raw))


def _loopback_engine(radio: FakeRadio, **kw) -> SunSDREngine:
    args = dict(host="127.0.0.1", local_host="127.0.0.1",
                control_port=radio.control_port, iq_port=radio.stream_port,
                bind_control_port=0, bind_iq_port=0,
                heartbeat_s=0.02, boot_spacing_s=0.0)
    args.update(kw)
    return SunSDREngine(**args)


def _backend(radio: FakeRadio = None, **kw) -> "SunSDR2DXBackend":
    """A backend wired to a loopback engine (or an unconfigured one, which
    touches no sockets until connect())."""
    from backends.sunsdr.backend import SunSDR2DXBackend
    engine_kw = {k: kw.pop(k) for k in tuple(kw)
                 if k in ("heartbeat_s", "boot_spacing_s", "tx_interval_s")}
    engine = (_loopback_engine(radio, **engine_kw) if radio is not None
              else SunSDREngine(host="", **engine_kw))
    return SunSDR2DXBackend(port="", baud_rate=115200, engine=engine, **kw)


async def _pump_tone(radio: FakeRadio, n_samples=6000, hz=1500.0):
    # ``hz`` is a VFO-relative offset; the raw stream carries the VFO at
    # -IF_OFFSET (the RX demodulator and the spectrum both shift it up).
    full = _tone(n_samples, hz=hz - sdp.IF_OFFSET_HZ)
    for i, off in enumerate(range(0, n_samples, 200)):
        radio.send_iq(full[off:off + 200], counter=0x1000 + off)
        if i % 10 == 9:
            await asyncio.sleep(0)


# ── registry / factory ────────────────────────────────────────────

class RegistryTests(unittest.TestCase):
    def test_model_registered(self):
        self.assertIn("sunsdr2dx", known_models())

    def test_factory_constructs_with_port_baud_compat(self):
        from backends.sunsdr.backend import SunSDR2DXBackend
        backend = create_backend("sunsdr2dx", "/dev/null", 115200)
        self.assertIsInstance(backend, SunSDR2DXBackend)
        backend2 = create_backend("sunsdr2dx", port="", baud_rate=115200)
        self.assertIsInstance(backend2, SunSDR2DXBackend)

    def test_model_verification_entry(self):
        info = model_verification()["sunsdr2dx"]
        self.assertEqual(info["name"], "Expert Electronics SunSDR2 DX")
        self.assertFalse(info["verified"])

    def test_every_other_model_still_registered(self):
        self.assertEqual(
            known_models(),
            ("ft710", "ic7300", "ic7300mk2", "ic705",
             "ic7610", "ic7760", "ftdx10", "ftdx101d",
             "ftdx101mp", "ftx1", "ft891", "ft991a",
             "g7250", "sunsdr2dx"))


# ── construction / abstract surface ───────────────────────────────

class ConstructionTests(unittest.TestCase):
    def test_backend_and_controller_implement_whole_abc(self):
        from backends.sunsdr.backend import (
            SunSDR2DXBackend, SunSDR2DXController)
        backend = _backend()
        self.assertIsInstance(backend, RadioBackend)
        self.assertFalse(SunSDR2DXBackend.__abstractmethods__)
        self.assertIsInstance(backend.cat, SunSDR2DXController)
        self.assertFalse(getattr(SunSDR2DXController,
                                 "__abstractmethods__", frozenset()))
        for name in RadioBackend.__abstractmethods__:
            if name == "capabilities":
                continue   # backend-level property — the controller is the
                           # CAT command surface, capabilities belong to the
                           # backend (same split as the Yaesu family)
            self.assertTrue(hasattr(backend.cat, name),
                            f"controller missing {name}")

    def test_model_and_connected(self):
        backend = _backend()
        self.assertEqual(backend.model, "sunsdr2dx")
        self.assertFalse(backend.connected)

    def test_cat_property_is_controller(self):
        backend = _backend()
        self.assertIs(backend.cat, backend._cat)

    def test_poll_scheduler_cancel_event_contract(self):
        backend = _backend()
        self.assertIsInstance(backend.cat._cancel_polls, asyncio.Event)
        self.assertFalse(backend.cat._cancel_polls.is_set())


# ── capabilities ──────────────────────────────────────────────────

class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.caps = _backend().capabilities

    def test_identity(self):
        self.assertEqual(self.caps.model_name, "sunsdr2dx")
        self.assertEqual(self.caps.display_name,
                         "Expert Electronics SunSDR2 DX")

    def test_audio_is_network_48k_no_hints(self):
        self.assertEqual(self.caps.audio_rx_rate, 48000)
        self.assertEqual(self.caps.audio_tx_rate, 48000)
        self.assertEqual(self.caps.audio_name_hints, ())

    def test_scope_lan_iq_four_real_spans(self):
        self.assertEqual(self.caps.scope_type, "lan_iq")
        self.assertEqual(self.caps.scope_spans,
                         {0: 19531, 1: 39062, 2: 78125, 3: 156250})
        self.assertEqual(self.caps.scope_speeds, ())

    def test_no_atu_auto_notch_vd_id(self):
        self.assertFalse(self.caps.has_atu)
        self.assertFalse(self.caps.has_auto_notch)
        self.assertFalse(self.caps.has_vd_id_meters)
        self.assertFalse(self.caps.vfo_b_direct)
        self.assertEqual(self.caps.tune_via, "tx2")

    def test_verification_boundary(self):
        self.assertFalse(self.caps.verified)
        self.assertTrue(self.caps.tx_gated)
        self.assertIn("s_meter", self.caps.unverified_meters)
        self.assertIn("power", self.caps.unverified_meters)

    def test_att_preamp_combined_four_levels(self):
        self.assertEqual(self.caps.att_steps, ())
        self.assertEqual(self.caps.preamp_steps,
                         ("-20 dB", "-10 dB", "OFF", "+10 dB"))

    def test_to_dict_json_serializable(self):
        json.dumps(self.caps.to_dict())


class UiTableTests(unittest.TestCase):
    def setUp(self):
        self.backend = _backend()

    def test_bands_hf_six_meter_shape(self):
        bands = self.backend.bands
        names = [b["name"] for b in bands]
        for band in ("160m", "80m", "40m", "20m", "10m", "6m"):
            self.assertIn(band, names)
        for b in bands:
            self.assertEqual(set(b), {"name", "start", "end", "default_freq"})
            self.assertLess(b["start"], b["end"])
            self.assertLessEqual(b["end"], 54_000_000)
        self.assertGreaterEqual(bands[0]["start"], 1_800_000)
        json.dumps(bands)

    def test_ui_modes_match_mode_map(self):
        self.assertEqual(self.backend.ui_modes,
                         ["USB", "LSB", "CW-U", "DIGI-U", "AM", "NFM", "FM"])
        self.assertEqual(sorted(self.backend.mode_name_to_num),
                         sorted(self.backend.ui_modes))
        tables = self.backend.filter_tables()
        self.assertIn("CW-U", tables["narrowModes"])
        json.dumps(tables)

    def test_state_tables_cover_state_contract(self):
        tables = self.backend.state_tables()
        for key in ("mode_num_to_name", "preamp_labels",
                    "attenuator_labels", "get_band_for_frequency",
                    "get_filter_hz", "raw_to_dbm", "raw_to_s_unit",
                    "raw_to_power"):
            self.assertIn(key, tables)
        self.assertEqual(tables["preamp_labels"][3], "+10 dB")
        self.assertEqual(tables["mode_num_to_name"][2], "USB")

    def test_default_audio_hook_on_base_and_existing_backends(self):
        self.assertIsNone(RadioBackend.create_audio_handler(self.backend))
        self.assertIsNone(create_backend("ft710", port="/dev/null")
                          .create_audio_handler())
        self.assertIsNone(create_backend("ic7300", port="/dev/null")
                          .create_audio_handler())
        self.assertIsNone(create_backend("g7250", port="/dev/null")
                          .create_audio_handler())


# ── async harness ─────────────────────────────────────────────────

class BackendCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from backends.sunsdr.backend import SunSDR2DXBackend
        self.radio = await FakeRadio.create()
        self.engine = _loopback_engine(self.radio)
        self.backend = SunSDR2DXBackend(port="", baud_rate=115200,
                                        engine=self.engine)
        self.cat = self.backend.cat

    async def asyncTearDown(self):
        try:
            await asyncio.wait_for(self.engine.stop(), timeout=2.0)
        finally:
            self.radio.close()


# ── connection / basic control ────────────────────────────────────

class ConnectionTests(BackendCase):
    async def test_connect_disconnect_reconnect(self):
        self.assertFalse(self.cat.connected)
        self.assertTrue(await self.cat.connect())
        self.assertTrue(self.cat.connected)
        self.assertTrue(self.backend.connected)
        await self.cat.disconnect()
        self.assertFalse(self.cat.connected)
        self.assertTrue(await self.cat.reconnect_loop())
        self.assertTrue(self.cat.connected)
        self.assertTrue(await self.cat.boot_verify())

    async def test_connect_without_engine_host_fails_clearly(self):
        from backends.sunsdr.backend import SunSDR2DXBackend
        backend = SunSDR2DXBackend(port="", baud_rate=115200, host="")
        with self.assertLogs("backends.sunsdr.engine", level="ERROR"):
            self.assertFalse(await backend.cat.connect())
        self.assertFalse(backend.connected)

    async def test_frequency_roundtrip_single_vfo(self):
        await self.cat.connect()
        self.assertTrue(await self.cat.set_frequency(7_100_000))
        self.assertEqual(await self.cat.get_frequency(), 7_100_000)
        self.assertEqual(await self.cat.get_frequency("B"), None)
        self.assertEqual(await self.cat.get_active_vfo(), "A")
        self.assertFalse(await self.cat.set_frequency(7_200_000, vfo="B"))
        self.assertTrue(await self.cat.set_vfo("A"))
        self.assertFalse(await self.cat.set_vfo("B"))

    async def test_mode_roundtrip_uses_ui_numbers(self):
        await self.cat.connect()
        self.assertEqual(await self.cat.get_mode(), 2)   # USB default
        self.assertTrue(await self.cat.set_mode(1))      # LSB
        self.assertEqual(self.engine.demodulator.mode, "LSB")
        self.assertEqual(self.engine.tx_modulator.mode, "LSB")
        self.assertEqual(await self.cat.get_mode(), 1)
        self.assertTrue(await self.cat.set_mode(3))      # CW-U
        self.assertEqual(self.engine.demodulator.mode, "CW")
        # TX modulator refuses RX-only modes quietly (engine contract)
        self.assertEqual(self.engine.tx_modulator.mode, "LSB")
        self.assertFalse(await self.cat.set_mode(99))

    async def test_drive_and_rf_power_share_one_control(self):
        await self.cat.connect()
        self.assertTrue(await self.cat.set_rf_power(40))
        self.assertTrue(await self.cat.set_drive(40))
        self.assertAlmostEqual(self.engine.drive_pct, 40.0)
        self.radio.control.clear()
        self.assertTrue(await self.cat.set_drive(55))
        await _wait_until(lambda: any(
            _ctrl_cmd(d) == sdp.CmdID.DRIVE for _, d in self.radio.control))
        pkts = [d for _, d in self.radio.control
                if _ctrl_cmd(d) == sdp.CmdID.DRIVE]
        self.assertEqual(_ctrl_trailing(pkts[-1]), sdp.drive_byte(55))

    async def test_att_preamp_four_levels_combined(self):
        await self.cat.connect()
        self.assertEqual(self.engine.att_preamp, 2)      # 0 dB bypass
        self.assertTrue(await self.cat.set_preamp(3))    # +10 dB preamp
        self.assertEqual(self.engine.att_preamp, 3)
        self.assertTrue(await self.cat.set_preamp(0))    # -20 dB
        self.assertEqual(self.engine.att_preamp, 0)
        self.assertFalse(await self.cat.set_attenuator(1))

    async def test_agc_mapping(self):
        await self.cat.connect()
        self.assertEqual(await self.cat.get_agc(), 3)    # SLOW (DSP 2)
        self.assertTrue(await self.cat.set_agc(1))       # FAST
        self.assertEqual(self.engine.demodulator._agc_mode, 4)
        self.assertEqual(await self.cat.get_agc(), 1)
        self.assertTrue(await self.cat.set_agc(0))       # OFF
        self.assertEqual(self.engine.demodulator._agc_mode, 0)
        self.assertFalse(await self.cat.set_agc(9))

    async def test_filter_width_by_mode(self):
        await self.cat.connect()
        self.assertEqual(await self.cat.get_filter_width(), 2)
        self.assertTrue(await self.cat.set_filter_width(0))   # 1800 Hz
        self.assertEqual(self.engine.demodulator._bp_low, 300.0)
        self.assertEqual(self.engine.demodulator._bp_high, 2100.0)
        self.assertTrue(await self.cat.set_mode(3))           # CW-U
        self.assertTrue(await self.cat.set_filter_width(1))   # 800 Hz @700
        self.assertAlmostEqual(self.engine.demodulator._bp_high
                               - self.engine.demodulator._bp_low, 800.0)
        self.assertAlmostEqual((self.engine.demodulator._bp_high
                                + self.engine.demodulator._bp_low) / 2,
                               700.0, delta=1.0)
        self.assertFalse(await self.cat.set_filter_width(99))

    async def test_s_meter_honest_none_before_iq(self):
        await self.cat.connect()
        self.assertIsNone(await self.cat.get_s_meter())
        self.assertIsNone(await self.cat.get_meter("RM5"))

    async def test_s_meter_raw_after_iq(self):
        await self.cat.connect()
        await _wait_until(lambda: len(self.radio.stream) > 0)
        await _pump_tone(self.radio)
        await _wait_until(
            lambda: self.engine.demodulator.get_s_meter() is not None)
        raw = await self.cat.get_s_meter()
        self.assertIsInstance(raw, int)
        self.assertTrue(0 <= raw <= 255)

    async def test_ptt_readback(self):
        await self.cat.connect()
        self.assertEqual(await self.cat.get_ptt(), 0)
        await self.cat.set_ptt(True)     # gate may refuse; use engine below
        if self.engine.ptt_requested:
            self.assertEqual(await self.cat.get_ptt(), 1)
            await self.cat.set_ptt(False)
            self.assertEqual(await self.cat.get_ptt(), 0)

    async def test_scope_span_switches_engine_rate(self):
        await self.cat.connect()
        self.assertEqual(self.engine.sample_rate, 78125)
        self.assertTrue(await self.cat.set_scope_span(2))
        self.assertEqual(self.engine.sample_rate, 156250)
        self.assertTrue(await self.cat.set_scope_span(0))
        self.assertEqual(self.engine.sample_rate, 39062)
        self.assertFalse(await self.cat.set_scope_span(9))
        self.assertTrue(await self.cat.set_scope_on(True))
        self.assertFalse(await self.cat.set_scope_on(False))
        self.assertFalse(await self.cat.set_scope_speed(1))
        self.assertTrue(await self.cat.set_scope_mode(0))
        self.assertFalse(await self.cat.set_scope_mode(1))


# ── unsupported surface explicit refusal ──────────────────────────

class UnsupportedSurfaceTests(BackendCase):
    async def test_unsupported_setters_refuse_explicitly(self):
        await self.cat.connect()
        for coro in (
            self.cat.set_split(True), self.cat.set_split(False),
            self.cat.set_antenna(1), self.cat.set_tuner(1),
            self.cat.set_squelch(5), self.cat.set_mic_gain(50),
            self.cat.set_monitor(True), self.cat.set_monitor_gain(5),
            self.cat.set_vox(True), self.cat.set_break_in(True),
            self.cat.set_key_speed(20), self.cat.set_cw_pitch(700),
            self.cat.set_rit(True), self.cat.set_rit_freq(100),
            self.cat.set_xit(True), self.cat.set_band_stack(0),
            self.cat.set_noise_blanker(True), self.cat.set_noise_reduction(True),
            self.cat.set_auto_notch(True), self.cat.set_compressor(True),
            self.cat.set_compressor_level(5), self.cat.set_amc_level(5),
            self.cat.set_meter_display(1), self.cat.set_dnr(1),
            self.cat.set_contour(1), self.cat.set_af_gain(100),
            self.cat.set_rf_gain(200), self.cat.set_power(True),
            self.cat.send_set_command("FA;"),
            self.cat.send_priority_set_command("TX1;"),
            self.cat.set("FA;"),
        ):
            self.assertFalse(await coro)

    async def test_unsupported_getters_return_none(self):
        await self.cat.connect()
        self.assertIsNone(await self.cat.get_antenna())
        self.assertIsNone(await self.cat.get_meter_display())
        self.assertIsNone(await self.cat.get_amc_level())
        self.assertIsNone(await self.cat.get_dnr())
        self.assertIsNone(await self.cat.get_contour())
        self.assertIsNone(await self.cat.get_nb_level())
        self.assertIsNone(await self.cat.get_nr_level())
        self.assertIsNone(await self.cat.get_info())
        self.assertIsNone(await self.cat.get_radio_info())
        self.assertIsNone(await self.cat.send_command("FA;"))
        self.assertIsNone(await self.cat.query("FA;"))

    async def test_poll_items_only_cover_supported_state(self):
        settings_fields = {f for f, _ in self.cat.settings_poll_items()}
        self.assertEqual(settings_fields,
                         {"rf_power", "preamp", "agc", "filter_width"})
        slow_keys = {k for k, _ in self.cat.slow_poll_items()}
        self.assertEqual(slow_keys, {"freq_mode"})
        always = {(lbl, fld) for lbl, fld, _ in self.cat.always_meter_items()}
        self.assertEqual(always, {("S", "s_meter")})
        tx_meters = {(lbl, fld) for lbl, fld, _ in self.cat.tx_meter_items()}
        self.assertEqual(tx_meters, {("Power", "power_meter")})


# ── initial state sync ────────────────────────────────────────────

class InitialStateSyncTests(BackendCase):
    async def test_sync_reports_real_state_only(self):
        await self.cat.connect()
        await self.cat.set_frequency(10_136_000)
        await self.cat.set_mode(1)
        await self.cat.set_rf_power(35)
        await self.cat.set_preamp(3)
        await self.cat.set_agc(1)
        await self.cat.set_scope_span(2)
        sync = await self.cat.initial_state_sync()
        self.assertEqual(sync["vfo_a_freq"], 10_136_000)
        self.assertEqual(sync["active_vfo"], "A")
        self.assertEqual(sync["mode"], 1)
        self.assertEqual(sync["rf_power"], 35)
        self.assertEqual(sync["preamp"], 3)
        self.assertEqual(sync["agc"], 1)
        self.assertEqual(sync["filter_width"], 2)
        self.assertEqual(sync["scope_span"], 2)
        self.assertIn(sync["tx_status"], (0, 1))
        for key, value in sync.items():
            self.assertIsNotNone(value, f"{key} must be real, not None")

    def test_sync_result_applies_to_radio_state(self):
        from radio_state import RadioState
        sync = {"vfo_a_freq": 7_074_000, "mode": 2, "rf_power": 50,
                "preamp": 2, "agc": 3, "filter_width": 2, "tx_status": 0,
                "active_vfo": "A", "scope_span": 1}
        state = RadioState.from_sync_result(sync)
        self.assertEqual(state.vfo_a_freq, 7_074_000)
        self.assertEqual(state.mode_name, "USB")


# ── TX gate ───────────────────────────────────────────────────────

class TxGateTests(BackendCase):
    async def test_ptt_refused_and_warned_once_while_gated(self):
        from backends.sunsdr import backend as sb
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False), \
                mock.patch.object(sb, "_TX_GATE_WARNED", False):
            with self.assertLogs("backends.sunsdr.backend",
                                 level="WARNING") as cm:
                self.assertFalse(await self.cat.set_ptt(True))
                self.assertFalse(await self.cat.set_ptt(True))
            warnings = [m for m in cm.output if "not hardware-verified" in m]
            self.assertEqual(len(warnings), 1)
        self.assertFalse(self.engine.ptt_requested)

    async def test_tune_refused_while_gated(self):
        from backends.sunsdr import backend as sb
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False), \
                mock.patch.object(sb, "_TX_GATE_WARNED", False):
            with self.assertLogs("backends.sunsdr.backend", level="WARNING"):
                self.assertFalse(await self.cat.set_tune(True))
        self.assertFalse(self.engine.ptt_requested)
        self.assertFalse(self.engine.tx_active)

    async def test_release_never_intercepted_by_gate(self):
        await self.cat.connect()
        from backends.sunsdr import backend as sb
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False), \
                mock.patch.object(sb, "_TX_GATE_WARNED", False):
            with self.assertNoLogs("backends.sunsdr.backend", level="WARNING"):
                self.assertTrue(await self.cat.set_ptt(False))
        self.assertFalse(self.engine.ptt_requested)

    async def test_ptt_allowed_when_env_opt_in(self):
        await self.cat.connect()
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            self.assertTrue(await self.cat.set_ptt(True))
        self.assertTrue(self.engine.ptt_requested)
        self.radio.control.clear()
        self.assertTrue(await self.cat.set_ptt(False))
        await _wait_until(lambda: any(
            _ctrl_cmd(d) == sdp.CmdID.PTT for _, d in self.radio.control))
        pkts = [d for _, d in self.radio.control
                if _ctrl_cmd(d) == sdp.CmdID.PTT]
        self.assertEqual(_ctrl_trailing(pkts[-1]), 0)
        self.assertFalse(self.engine.ptt_requested)

    async def test_am_fm_cw_refuse_keying_even_with_env_opt_in(self):
        # DIGI-U is USB for TX; CW/AM/NFM/FM have no verified transmit path
        # and must refuse instead of silently sending the previous SSB mode.
        await self.cat.connect()
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            for mode_num in (3, 5, 6, 7):       # CW-U, AM, NFM, FM
                self.assertTrue(await self.cat.set_mode(mode_num))
                self.assertFalse(await self.cat.set_ptt(True))
                self.assertFalse(await self.cat.set_tune(True))
                self.assertFalse(self.engine.ptt_requested)
            self.assertTrue(await self.cat.set_mode(4))   # DIGI-U
            self.assertTrue(await self.cat.set_ptt(True))
            await self.cat.set_ptt(False)
            self.assertTrue(await self.cat.set_mode(2))   # USB
            self.assertTrue(await self.cat.set_ptt(True))
            await self.cat.set_ptt(False)


# ── TUNE ──────────────────────────────────────────────────────────

class TuneTests(BackendCase):
    async def test_tune_keys_feeds_tone_and_releases(self):
        await self.cat.connect()
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            self.assertTrue(await self.cat.set_tune(True))
        self.assertTrue(self.engine.ptt_requested)
        self.assertTrue(self.engine.tx_active)
        await _wait_until(lambda: len(self.engine._tx_queue) > 0
                          or self.engine.underruns > 0)
        self.assertEqual(await self.cat.get_ptt(), 2)
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            self.assertTrue(await self.cat.set_tune(False))
        self.assertFalse(self.engine.ptt_requested)
        self.assertFalse(self.engine.tx_active)
        task = self.cat._tune_task
        self.assertTrue(task is None or task.done())

        def _ptt_trailings():
            return [_ctrl_trailing(d) for _, d in self.radio.control
                    if _ctrl_cmd(d) == sdp.CmdID.PTT]

        # key + release deliver (UDP ordering preserved on loopback); the
        # release is the final word and nothing re-keys afterwards
        await _wait_until(lambda: len(_ptt_trailings()) >= 2)
        self.assertEqual(_ptt_trailings()[0], 1)
        self.assertEqual(_ptt_trailings()[-1], 0)
        n = len(_ptt_trailings())
        await asyncio.sleep(0.1)
        self.assertEqual(len(_ptt_trailings()), n)   # no stray re-key

    async def test_direct_ptt_release_also_stops_tune_tone(self):
        await self.cat.connect()
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            self.assertTrue(await self.cat.set_tune(True))
            self.assertTrue(await self.cat.set_ptt(False))
        task = self.cat._tune_task
        self.assertTrue(task is None or task.done())
        self.assertFalse(self.engine.tx_active)
        self.assertFalse(self.engine.ptt_requested)

    async def test_tune_tone_reaches_stream_as_nonzero_iq(self):
        await self.cat.connect()
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            self.assertTrue(await self.cat.set_tune(True))
        self.radio.stream.clear()
        await _wait_until(
            lambda: any(_stream_fields(d)[1] == 0xFFFD
                        and d[10:] != b"\x00" * sdp.IQ_PAYLOAD_BYTES
                        for _, d in self.radio.stream))
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            await self.cat.set_tune(False)

    async def test_tune_failure_releases_and_leaves_nothing_pending(self):
        # Engine with no transports: keying fails → TUNE must not linger.
        from backends.sunsdr.backend import SunSDR2DXBackend
        backend = SunSDR2DXBackend(port="", baud_rate=115200, host="")
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            self.assertFalse(await backend.cat.set_tune(True))
        self.assertFalse(backend.engine.ptt_requested)
        self.assertFalse(backend.engine.tx_active)
        self.assertTrue(backend.cat._tune_task is None
                        or backend.cat._tune_task.done())

    async def test_tune_bounded_self_release(self):
        await self.cat.connect()
        from backends.sunsdr import backend as sb
        # the patch must stay active while the tone task runs: the task
        # reads TUNE_MAX_S when it first executes, after set_tune returns
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True), \
                mock.patch.object(sb, "TUNE_MAX_S", 0.25):
            self.assertTrue(await self.cat.set_tune(True))
            await _wait_until(lambda: not self.engine.ptt_requested,
                              timeout=3.0)
        self.assertFalse(self.engine.tx_active)
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            self.assertTrue(await self.cat.set_tune(False))


# ── engine events → broadcast ─────────────────────────────────────

class EngineEventTests(BackendCase):
    async def test_telemetry_reaches_broadcast_callback(self):
        events = []
        self.backend.set_broadcast_callback(
            lambda field, value: events.append((field, value)))
        await self.cat.connect()
        await _wait_until(lambda: len(self.radio.stream) > 0)
        self.radio.send_power_telemetry(fwd_w=55.0, volts=13.8)
        await _wait_until(lambda: self.engine.latest_telemetry is not None)
        await _wait_until(lambda: ("power_meter", 140) in events)
        self.assertIn(("vd_meter", 220), events)
        # swr/alc are never fabricated
        self.assertFalse(any(f in ("swr_meter", "alc_meter")
                             for f, _ in events))

    async def test_ptt_confirm_reaches_broadcast_callback(self):
        events = []
        self.backend.set_broadcast_callback(
            lambda field, value: events.append((field, value)))
        await self.cat.connect()
        await _wait_until(lambda: len(self.radio.stream) > 0)
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            self.assertTrue(await self.cat.set_ptt(True))
        # the engine only accepts a confirm while a key is requested
        self.radio.send_ptt_status(True)
        await _wait_until(lambda: ("tx_status", 1) in events)
        self.radio.send_ptt_status(False)
        await _wait_until(lambda: ("tx_status", 0) in events)
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            await self.cat.set_ptt(False)

    async def test_ptt_confirm_does_not_downgrade_tune_status(self):
        events = []
        self.backend.set_broadcast_callback(
            lambda field, value: events.append((field, value)))
        await self.cat.connect()
        await _wait_until(lambda: len(self.radio.stream) > 0)
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            self.assertTrue(await self.cat.set_tune(True))
        self.radio.send_ptt_status(True)      # device confirms the carrier
        await _wait_until(lambda: self.engine.ptt_confirmed)
        await asyncio.sleep(0.05)
        # tx_status stays the WS tune flow's concern (2), never downgraded
        self.assertFalse(any(f == "tx_status" for f, _ in events))
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            await self.cat.set_tune(False)

    async def test_connect_event_broadcasts_serial_state(self):
        events = []
        self.backend.set_broadcast_callback(
            lambda field, value: events.append((field, value)))
        await self.cat.connect()
        self.assertIn(("serial_connected", True), events)
        await self.cat.disconnect()
        self.assertIn(("serial_connected", False), events)


# ── audio adapter ─────────────────────────────────────────────────

class AudioAdapterTests(BackendCase):
    async def test_factory_idempotent_and_shares_engine(self):
        from backends.sunsdr.audio import SunSDRAudioHandler
        handler = self.backend.create_audio_handler()
        self.assertIsInstance(handler, SunSDRAudioHandler)
        self.assertIs(self.backend.create_audio_handler(), handler)
        self.assertIs(handler._engine, self.engine)
        self.assertIs(self.backend.engine, self.engine)

    async def test_start_rx_stays_live_while_device_reconnects(self):
        handler = self.backend.create_audio_handler()
        self.assertFalse(handler._rx_running)
        self.assertTrue(handler.start_rx())      # wait for the network link
        self.assertTrue(handler._rx_running)
        self.assertIsNone(handler.read_rx_chunk())
        await self.cat.connect()
        self.assertTrue(handler.start_rx())      # idempotent
        self.assertTrue(handler._rx_running)
        handler.stop_rx()
        self.assertFalse(handler._rx_running)
        self.assertIsNone(handler.read_rx_chunk())

    async def test_server_visible_attributes(self):
        handler = self.backend.create_audio_handler()
        self.assertEqual(handler._rx_dev_rate, 48000)
        self.assertEqual(handler.device_rates, {"rx": 48000, "tx": 48000})
        self.assertTrue(hasattr(handler, "opus_enabled"))
        self.assertTrue(hasattr(handler, "_rx_last_peak"))

    async def test_rx_flow_from_shared_engine(self):
        handler = self.backend.create_audio_handler()
        await self.cat.connect()
        self.assertTrue(handler.start_rx())
        await _wait_until(lambda: len(self.radio.stream) > 0)
        await _pump_tone(self.radio)
        await _wait_until(lambda: len(self.engine.rx_audio) > 0)
        block = handler.read_rx_chunk()
        self.assertIsNotNone(block)
        self.assertEqual(len(block) % 2, 0)
        self.assertTrue(any(block))
        self.assertIsNone(handler.read_rx_chunk())   # drained for now
        handler.note_rx_chunk(block)
        self.assertGreater(handler._rx_last_peak, 0)
        self.assertEqual(handler.rx_silent_seconds(), 0.0)
        handler.note_rx_chunk(b"\x00" * 1920)
        handler.note_rx_chunk(b"\x00" * 1920)
        self.assertGreater(handler.rx_silent_seconds(), 0.0)
        handler.note_rx_chunk(block)
        self.assertEqual(handler.rx_silent_seconds(), 0.0)

    async def test_rx_read_discards_stale_backlog(self):
        handler = self.backend.create_audio_handler()
        handler.start_rx()
        self.engine.rx_audio.extend(bytes([i]) * 1920 for i in range(10))
        block = handler.read_rx_chunk()
        self.assertEqual(block, bytes([5]) * 1920)
        self.assertEqual([chunk[0] for chunk in self.engine.rx_audio], [6, 7, 8, 9])
        self.assertEqual(self.engine.rx_drops, 5)

    async def test_encode_rx_audio_opus_and_pcm_fallback(self):
        handler = self.backend.create_audio_handler()
        pcm = _pcm16(960)
        frames = handler.encode_rx_audio(pcm)
        self.assertTrue(frames)
        if handler.opus_enabled:
            self.assertTrue(all(f[0] == 0x01 for f in frames), frames)
            real = handler.opus_encoder
            class _Boom:
                def push(self, _):
                    raise RuntimeError("encoder exploded")
            handler.opus_encoder = _Boom()
            try:
                frames = handler.encode_rx_audio(pcm)
            finally:
                handler.opus_encoder = real
            self.assertEqual(len(frames), 1)
            self.assertEqual(frames[0][0], 0x00)
            self.assertEqual(frames[0][1:], pcm)
        else:
            self.assertEqual(frames, [b"\x00" + pcm])
        self.assertEqual(handler.encode_rx_audio(b"\x00" * 10), [])

    async def test_tx_flow_matches_server_contract(self):
        handler = self.backend.create_audio_handler()
        await self.cat.connect()
        # server keys PTT first, then starts TX audio via to_thread
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            self.assertTrue(await self.cat.set_ptt(True))
        self.assertTrue(await asyncio.to_thread(handler.start_tx))
        self.assertTrue(self.engine.tx_active)
        self.assertTrue(handler.has_pending_tx_audio())
        for _ in range(50):                    # ~1 s of audio fed at once
            handler.feed_tx_audio(_pcm16(960))
        self.assertGreater(handler.tx_queue_frames(), 0)   # backpressure
        self.assertIsNone(handler.write_tx_chunk())   # pacer owns the wire
        stats = handler.tx_stats()
        for key in ("written", "write_err", "peak", "queued", "queue_drops"):
            self.assertIn(key, stats)
        self.assertGreaterEqual(stats["written"], 50)
        self.assertGreater(stats["peak"], 0)
        await _wait_until(lambda: any(
            _stream_fields(d)[1] == 0xFFFD for _, d in self.radio.stream))
        await asyncio.to_thread(handler.stop_tx, True)   # graceful release
        self.assertFalse(self.engine.tx_active)
        self.assertFalse(handler.has_pending_tx_audio())
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            await self.cat.set_ptt(False)

    async def test_start_tx_refused_without_device_link(self):
        handler = self.backend.create_audio_handler()
        self.assertFalse(await asyncio.to_thread(handler.start_tx))

    async def test_close_stops_consuming_and_keeps_engine_alive(self):
        handler = self.backend.create_audio_handler()
        await self.cat.connect()
        handler.start_rx()
        handler.close()
        self.assertFalse(handler._rx_running)
        self.assertTrue(self.engine.connected)   # backend owns the engine
        handler.close()                          # repeatable


# ── scope adapter ─────────────────────────────────────────────────

class ScopeAdapterTests(BackendCase):
    async def _make_scope(self):
        from scope_handler import ScopeHandler
        scope = ScopeHandler()
        producer = self.backend.create_scope_producer(
            scope, self._on_frame)
        return scope, producer

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.frames = 0

    async def _on_frame(self, scope):
        self.frames += 1

    async def test_factory_idempotent_and_shares_engine(self):
        from backends.sunsdr.scope import SunSDRScopeProducer
        scope, producer = await self._make_scope()
        self.assertIsInstance(producer, SunSDRScopeProducer)
        self.assertIs(self.backend.create_scope_producer(scope, None),
                      producer)
        self.assertIs(producer._engine, self.engine)
        self.assertIs(self.backend.engine, self.engine)

    async def test_emits_scope_handler_fields(self):
        scope, producer = await self._make_scope()
        await self.cat.connect()
        await _wait_until(lambda: len(self.radio.stream) > 0)
        await producer.start()
        try:
            await _pump_tone(self.radio)
            await _wait_until(lambda: scope._frame_count >= 1)
            self.assertTrue(scope.connected)
            self.assertEqual(len(scope.spectrum_rx1), 850)
            self.assertTrue(all(isinstance(v, int) and 0 <= v <= 255
                                for v in scope.spectrum_rx1))
            self.assertEqual(scope.spectrum_rx2, [0] * 850)
            self.assertEqual(scope.scope_mode, 0)            # CENTER
            self.assertIn(scope.scope_span, (0, 1, 2, 3))
            self.assertEqual(scope.scope_start_freq,
                             int(self.engine.freq)
                             - self.backend.capabilities.scope_spans[
                                 scope.scope_span])
            self.assertGreater(scope.last_update, 0)
            await _wait_until(lambda: self.frames >= 1)
            # start is idempotent: no duplicate task
            await producer.start()
            self.assertTrue(producer._emit_task is not None)
        finally:
            await producer.stop()
        self.assertTrue(producer._emit_task is None or
                        producer._emit_task.done())

    async def test_stop_keeps_engine_running_for_audio_and_ft8(self):
        scope, producer = await self._make_scope()
        await self.cat.connect()
        await producer.start()
        await producer.stop()
        self.assertTrue(self.engine.connected)
        # audio consumers still get PCM after the scope producer stopped
        await _wait_until(lambda: len(self.radio.stream) > 0)
        await _pump_tone(self.radio)
        await _wait_until(lambda: len(self.engine.rx_audio) > 0)

    async def test_notify_tx_is_noop(self):
        _, producer = await self._make_scope()
        await self.cat.connect()
        self.assertIsNone(producer.notify_tx(True))
        self.assertIsNone(producer.notify_tx(False))
        await producer.start()
        await _wait_until(lambda: len(self.radio.stream) > 0)
        await _pump_tone(self.radio)
        from scope_handler import ScopeHandler
        scope = ScopeHandler()
        producer2 = self.backend.create_scope_producer(scope, None)
        await producer2.start()
        producer2.notify_tx(True)
        await _pump_tone(self.radio)
        await _wait_until(lambda: scope._frame_count >= 1)
        await producer2.stop()

    async def test_span_changes_reach_the_frames(self):
        scope, producer = await self._make_scope()
        await self.cat.connect()
        await producer.start()
        try:
            await self.cat.set_scope_span(3)   # propagates to the producer
            self.assertEqual(producer.span_index, 3)
            await _wait_until(lambda: len(self.radio.stream) > 0)
            await _pump_tone(self.radio)
            await _wait_until(lambda: scope._frame_count >= 1)
            self.assertEqual(scope.scope_span, 3)
            self.assertEqual(scope.scope_start_freq,
                             int(self.engine.freq) - 156250)
        finally:
            await producer.stop()


# ── server audio factory ──────────────────────────────────────────

class ServerAudioFactoryTests(unittest.TestCase):
    def test_backend_factory_wins(self):
        import server
        sentinel = object()

        class _Backend:
            capabilities = create_backend("ft710", port="/dev/null").capabilities

            def create_audio_handler(self):
                return sentinel

        self.assertIs(server._make_audio_handler(_Backend()), sentinel)

    def test_none_factory_falls_back_to_pyaudio_with_caps(self):
        import server
        from audio_handler import AudioHandler

        class _Backend:
            capabilities = create_backend("ic7300",
                                          port="/dev/null").capabilities

            def create_audio_handler(self):
                return None

        handler = server._make_audio_handler(_Backend())
        self.assertIsInstance(handler, AudioHandler)
        self.assertEqual(handler._rx_dev_rate, 48000)
        self.assertEqual(handler._tx_dev_rate, 48000)
        handler.close()

    def test_existing_backends_keep_pyaudio_path(self):
        import server
        from audio_handler import AudioHandler
        for model in ("ft710", "ic7300", "g7250"):
            with self.subTest(model=model):
                handler = server._make_audio_handler(
                    create_backend(model, port="/dev/null"))
                self.assertIsInstance(handler, AudioHandler)
                handler.close()

    def test_sunsdr_backend_selects_network_handler(self):
        import server
        from backends.sunsdr.audio import SunSDRAudioHandler
        handler = server._make_audio_handler(_backend())
        self.assertIsInstance(handler, SunSDRAudioHandler)


if __name__ == "__main__":
    unittest.main()
