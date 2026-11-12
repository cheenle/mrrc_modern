"""Xiegu G7250 backend (spec 2026-11-07).

Hardware-independent coverage for the first non-Icom/non-Yaesu family:

- registry/profile/capability wiring (a registered model that no dialog can
  reach was a real bug class on the Icom round);
- the CI-V address probe — the G7250's address is undocumented, so "answered
  on the second candidate" and "answered nowhere" both have to behave;
- the LAN IQ protocol helpers, byte-for-byte against the published capture,
  plus the FFT peak position (a mirrored or offset spectrum would be invisible
  in any test that only checked "850 bins of something");
- the TX gate (unverified model must not key) and the no-0x27 rule.
"""
import asyncio
import os
import unittest
from unittest import mock

import numpy as np

import config
from backends import create_backend, known_models, model_verification
from backends.xiegu import lan_iq, xiegu_profiles
from backends.xiegu.backend import G7250Backend


def _backend(**kwargs) -> G7250Backend:
    backend = create_backend("g7250", "/dev/null", **kwargs)
    assert isinstance(backend, G7250Backend)
    return backend


class ProfileAndRegistryTests(unittest.TestCase):
    def test_registered_and_reachable(self):
        self.assertIn("g7250", known_models())
        self.assertEqual(model_verification()["g7250"]["name"], "Xiegu G7250")
        self.assertFalse(model_verification()["g7250"]["verified"])

    def test_profile_is_xiegu_shaped(self):
        p = xiegu_profiles.get_profile("g7250")
        self.assertEqual(p.model_key, "g7250")
        self.assertEqual(p.civ_addr, xiegu_profiles.DEFAULT_CIV_ADDRESS)
        # No documented 1A 05 item: the empty payload is what makes
        # CivController skip the transceive step instead of guessing.
        self.assertEqual(p.transceive_item, b"")
        self.assertFalse(p.verified)
        self.assertTrue(p.has_atu)
        self.assertEqual(p.filter_model, "fil123")

    def test_bands_and_modes_match_the_radio(self):
        p = xiegu_profiles.get_profile("g7250")
        names = [b["name"] for b in p.bands]
        self.assertEqual(names[0], "160m")
        self.assertEqual(names[-1], "6m")
        # The G7250 is HF + 6 m only: no 4 m / 70 cm rows inherited.
        self.assertNotIn("70cm", names)
        self.assertNotIn("2m", names)
        self.assertEqual(set(p.mode_num_to_name.values()),
                         {"LSB", "USB", "AM", "CW-U", "CW-L", "FM"})
        self.assertNotIn("RTTY-L", p.mode_name_to_num)

    def test_default_baud_is_115200(self):
        self.assertEqual(config.default_baud_for("g7250"), 115200)


class CapabilityTests(unittest.TestCase):
    def test_no_lan_configured_falls_back_to_synthetic_scope(self):
        backend = _backend()
        caps = backend.capabilities
        self.assertEqual(caps.scope_type, "none")
        self.assertEqual(dict(caps.scope_spans), {})
        self.assertIsNone(backend.create_scope_producer(None, None))
        self.assertEqual(caps.default_baud, 115200)

    def test_lan_configured_advertises_one_span(self):
        backend = _backend(lan_ip="192.168.3.199")
        caps = backend.capabilities
        self.assertEqual(caps.scope_type, "lan_iq")
        self.assertEqual(dict(caps.scope_spans),
                         {lan_iq.LAN_SPAN_INDEX: lan_iq.LAN_IQ_HALF_SPAN_HZ})
        producer = backend.create_scope_producer(None, None)
        self.assertIsInstance(producer, lan_iq.G7250LanIQProducer)
        self.assertEqual(producer.radio_ip, "192.168.3.199")

    def test_unverified_model_is_tx_gated(self):
        backend = _backend()
        self.assertFalse(backend.capabilities.verified)
        self.assertTrue(backend.capabilities.tx_gated)

    @mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False, create=True)
    async def test_refused_ptt_never_writes(self):
        backend = _backend()
        backend._civ.send_set_command = mock.AsyncMock(return_value=True)
        self.assertFalse(await backend.set_ptt(True))
        backend._civ.send_set_command.assert_not_called()

    def test_ui_modes_have_no_rtty(self):
        self.assertEqual(_backend().ui_modes, ["LSB", "USB", "CW-U", "AM", "FM"])

    def test_scope_on_is_not_polled(self):
        fields = [f for f, _ in _backend().settings_poll_items()]
        self.assertNotIn("scope_on", fields)
        self.assertIn("filter_width", fields)


class _FakeController:
    """Stand-in for CivController: remembers the address, answers or not."""

    instances: list = []

    def __init__(self, port, baudrate, civ_addr, **kwargs):
        self.port, self.baudrate, self.civ_addr = port, baudrate, civ_addr
        self.transceive_cmd = kwargs.get("transceive_cmd")
        self.connected = False
        self.connect_calls = 0
        _FakeController.instances.append(self)

    async def connect(self):
        self.connected = True
        self.connect_calls += 1
        return True

    async def disconnect(self):
        self.connected = False

    async def get_frequency(self, vfo="A", timeout=None):
        if self.civ_addr == getattr(_FakeController, "answering_addr", None):
            return 14_074_000
        raise TimeoutError("no answer")

    async def send_set_command(self, cmd):
        return True


class AddressProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _FakeController.instances = []
        self._real = G7250Backend.__dict__.get("_make_controller")
        G7250Backend._make_controller = (
            lambda self, addr: _FakeController(self._port, self._baud, addr,
                                               transceive_cmd=b""))

    def tearDown(self):
        if self._real is None:
            del G7250Backend._make_controller
        else:                                              # pragma: no cover
            G7250Backend._make_controller = self._real

    async def test_second_candidate_is_adopted(self):
        _FakeController.answering_addr = 0x88
        backend = _backend()
        self.assertTrue(await backend.connect())
        self.assertEqual(backend.resolved_civ_addr, 0x88)
        # 0xA4 was tried first (and its controller was disconnected again).
        tried = [c.civ_addr for c in _FakeController.instances]
        self.assertEqual(tried, [0xA4, 0x88])
        self.assertFalse(_FakeController.instances[0].connected)

    async def test_no_answer_reports_every_candidate(self):
        _FakeController.answering_addr = None
        backend = _backend()
        self.assertFalse(await backend.connect())
        self.assertIsNone(backend.resolved_civ_addr)
        self.assertEqual([c.civ_addr for c in _FakeController.instances],
                         list(xiegu_profiles.CANDIDATE_CIV_ADDRESSES))
        self.assertTrue(all(not c.connected for c in _FakeController.instances))

    async def test_pinned_address_is_the_only_attempt(self):
        _FakeController.answering_addr = 0x70
        with mock.patch.object(config, "G7250_CIV_ADDR", "0x70"):
            backend = _backend()
            self.assertTrue(await backend.connect())
        self.assertEqual([c.civ_addr for c in _FakeController.instances], [0x70])


class LanProtocolTests(unittest.IsolatedAsyncioTestCase):
    def test_tune_frame_matches_the_published_capture(self):
        # 14.074 MHz = AA 55 55 AA 00 01 00 00 00 00 00 D6 C0 90
        self.assertEqual(lan_iq.build_tune_frame(14_074_000).hex(),
                         "aa5555aa00010000000000d6c090")

    def test_tune_frame_clamps_to_the_documented_range(self):
        self.assertEqual(lan_iq.build_tune_frame(10)[-8:],
                         lan_iq.build_tune_frame(lan_iq.LAN_TUNE_MIN_HZ)[-8:])
        self.assertEqual(lan_iq.build_tune_frame(99_000_000)[-8:],
                         lan_iq.build_tune_frame(lan_iq.LAN_TUNE_MAX_HZ)[-8:])

    def test_iq_payload_decode(self):
        raw = np.array([1000, -1000, 2000, -2000], dtype="<i2").tobytes()
        samples = lan_iq.parse_iq_payload(raw)
        self.assertEqual(len(samples), 2)
        self.assertAlmostEqual(samples[0].real, 1000 / 32768.0, places=5)
        self.assertAlmostEqual(samples[0].imag, -1000 / 32768.0, places=5)
        # A truncated/odd datagram must not raise.
        self.assertEqual(len(lan_iq.parse_iq_payload(b"\x01\x02\x03")), 0)

    def test_spectrum_peak_lands_on_the_offset(self):
        rate = lan_iq.LAN_IQ_RATE_HZ
        t = np.arange(rate) / rate
        iq = (0.5 * np.exp(2j * np.pi * 5000 * t)).astype(np.complex64)
        spec = lan_iq.iq_to_spectrum(iq)
        self.assertEqual(len(spec), 850)
        peak = int(np.argmax(spec))
        expected = 425 + int(5000 / lan_iq.LAN_IQ_HALF_SPAN_HZ * 425)
        self.assertLessEqual(abs(peak - expected), 1)
        self.assertEqual(spec[peak], 255)
        self.assertEqual(min(spec), 0)

    def test_empty_buffer_is_silent(self):
        self.assertEqual(lan_iq.iq_to_spectrum(np.empty(0, np.complex64)),
                         [0] * 850)

    async def test_producer_writes_the_scope_frame(self):
        class _Scope:
            spectrum_rx1 = [0] * 850
            spectrum_rx2 = [0] * 850
            scope_mode = -1
            scope_span = -1
            scope_start_freq = 0
            s_meter = -1
            last_update = 0.0
            _frame_count = 0

        scope = _Scope()
        seen = []

        async def on_frame(s):
            seen.append(s)

        producer = lan_iq.G7250LanIQProducer("127.0.0.1", scope, on_frame)
        await producer.tune(14_074_000)          # no transport yet: remembered
        iq = (np.exp(2j * np.pi * 1000 * np.arange(4800) / lan_iq.LAN_IQ_RATE_HZ)
              * 20000).astype(np.complex64)
        payload = np.stack([iq.real, iq.imag], axis=1).astype("<i2").tobytes()
        for _ in range(4):
            producer._ingest(payload)
        self.assertEqual(producer.packets_received, 4)
        self.assertTrue(await producer.emit_once())
        self.assertEqual(len(scope.spectrum_rx1), 850)
        self.assertEqual(scope.scope_span, lan_iq.LAN_SPAN_INDEX)
        self.assertEqual(scope.scope_mode, 0)
        self.assertEqual(scope.scope_start_freq,
                         14_074_000 - lan_iq.LAN_IQ_HALF_SPAN_HZ)
        # No S-meter in the IQ stream: the sentinel must survive so the CAT
        # poll keeps owning that field (same rule as CivScopeProducer).
        self.assertEqual(scope.s_meter, -1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(scope._frame_count, 1)

    async def test_producer_binds_and_sends_tune_over_udp(self):
        """End-to-end over loopback: bind, ingest a datagram, tune the radio."""
        producer = lan_iq.G7250LanIQProducer(
            "127.0.0.1", None, None, iq_port=0, control_port=0)
        received: list = []

        class _Sink(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                received.append(data)

        loop = asyncio.get_running_loop()
        control, _ = await loop.create_datagram_endpoint(
            _Sink, local_addr=("127.0.0.1", 0))
        producer._control_port = control.get_extra_info("sockname")[1]
        await producer.start()
        sender = None
        try:
            self.assertIsNotNone(producer.bound_port)
            await producer.tune(7_074_000, force=True)
            sender, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
            payload = b"\x00" * 768
            sender.sendto(payload, ("127.0.0.1", producer.bound_port))
            for _ in range(50):
                if producer.packets_received:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(producer.packets_received, 1)
        finally:
            await producer.stop()
            if sender is not None:
                sender.close()
            control.abort()
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], lan_iq.build_tune_frame(7_074_000))

    async def test_stop_is_idempotent(self):
        producer = lan_iq.G7250LanIQProducer("127.0.0.1", None, None, iq_port=0)
        await producer.stop()
        await producer.stop()


if __name__ == "__main__":
    unittest.main()


class DemodIfOffsetTests(unittest.TestCase):
    """SunSDR 的解调器要能当 G7250 的用：后者的 IQ 就以 VFO 为中心。

    `AudioDemodulator` 原本把 30.5 kHz 的 IF 偏移写死在模块常量里（那是
    SunSDR2 DX 的 RX DDS 约定）。G7250 的网口 IQ 是 ±24 kHz、VFO 在 0 Hz，
    所以这个偏移必须能按实例给 —— 否则解出来的音频整体偏 30.5 kHz，落在
    48 kHz 带宽之外，听起来就是「一片安静」。
    """

    def _tone_peak_hz(self, demod, signal_hz, raw_hz):
        n = raw_hz
        t = np.arange(n)
        iq = (0.4 * np.exp(2j * np.pi * signal_hz * t / raw_hz)).astype(np.complex64)
        out = np.frombuffer(demod.demodulate(iq), dtype="<i2").astype(np.float64)
        self.assertGreater(out.size, raw_hz // 2, "解调器没吐够音频")
        tail = out[-out.size // 2:]
        spec = np.abs(np.fft.rfft(tail * np.hanning(tail.size)))
        return np.argmax(spec) * raw_hz / tail.size

    def test_a_zero_offset_demodulates_a_baseband_tone(self):
        from backends.sunsdr.dsp import AudioDemodulator
        demod = AudioDemodulator(sample_rate=48_000, audio_rate=48_000,
                                 if_offset_hz=0.0)
        demod.set_mode("USB")
        self.assertAlmostEqual(self._tone_peak_hz(demod, 1000, 48_000), 1000,
                               delta=25)

    def test_the_default_keeps_the_sunsdr_offset(self):
        """默认值不能变：SunSDR 的 30.5 kHz 约定已经硬件验证过。"""
        from backends.sunsdr import dsp
        demod = dsp.AudioDemodulator(sample_rate=48_000, audio_rate=48_000)
        self.assertEqual(demod.if_offset_hz, dsp.IF_OFFSET)
        demod.set_mode("USB")
        # VFO 落在 -IF 处的 IQ 才是「中心有信号」：0 Hz 的音在默认下调不出来。
        self.assertNotAlmostEqual(self._tone_peak_hz(demod, 1000, 48_000), 1000,
                                  delta=200)


class _FakeTx:
    """Stands in for the PyAudio handler in the TX-delegation tests."""

    def __init__(self, *_args, **_kwargs):
        # Same constructor keywords create_audio_handler passes a real handler.
        self.calls = []
        self.queue = 0
        self.session = False

    def start_tx(self):
        self.calls.append("start_tx")
        self.session = True
        return True

    def stop_tx(self, graceful=False, **_kw):
        self.calls.append(f"stop_tx({graceful})")
        self.session = False

    def feed_tx_audio(self, pcm):
        self.calls.append(f"feed({len(pcm)})")

    def write_tx_chunk(self):
        self.calls.append("write")

    def tx_stats(self):
        return {"written": 7, "write_err": 0, "peak": 100, "queued": 0,
                "queue_drops": 0}

    def tx_queue_frames(self):
        return self.queue

    def has_pending_tx_audio(self):
        return self.session

    @property
    def device_rates(self):
        return {"rx": 44100, "tx": 44100}

    def close(self):
        self.calls.append("close")


class LanAudioTests(unittest.TestCase):
    """RX audio demodulated from the LAN IQ stream.

    Field case (BG7YQV, 2026-11-12, bundle `20261111-212640-e397`): CAT fine,
    the radio's built-in USB sound card enumerated on Windows, and its capture
    stream sat at rms=2 / peak=9 for three days.  The customer's own sentence —
    "the network port carries the baseband IQ" — is the requirement this class
    holds the code to.
    """

    def _handler(self, mode=None):
        from backends.xiegu.audio import G7250AudioHandler

        class _Producer:
            def __init__(self):
                self.on_iq = None

        producer = _Producer()
        handler = G7250AudioHandler(producer, tx_handler=_FakeTx())
        handler.start_rx()
        if mode:
            handler.set_mode(mode)
        return producer, handler

    @staticmethod
    def _tone(freq_hz, n=48_000):
        t = np.arange(n)
        return (0.4 * np.exp(2j * np.pi * freq_hz * t / lan_iq.LAN_IQ_RATE_HZ)
                ).astype(np.complex64)

    @staticmethod
    def _peak_hz(pcm):
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
        spec = np.abs(np.fft.rfft(audio * np.hanning(audio.size)))
        return np.argmax(spec) * lan_iq.LAN_IQ_RATE_HZ / audio.size

    def test_the_producer_feeds_the_demodulator(self):
        producer, handler = self._handler()
        # Bound methods are re-created on each access, so compare ownership.
        self.assertIs(producer.on_iq.__self__, handler)
        self.assertEqual(producer.on_iq.__func__, handler.feed_iq.__func__)

    def test_received_iq_becomes_48k_audio(self):
        producer, handler = self._handler("USB")
        producer.on_iq(self._tone(1000))
        chunks = []
        while True:
            chunk = handler.read_rx_chunk()
            if chunk is None:
                break
            chunks.append(chunk)
        self.assertTrue(chunks, "IQ 进来了却没有音频")
        joined = b"".join(chunks)
        self.assertEqual(len(joined) % 2, 0)
        self.assertAlmostEqual(self._peak_hz(joined), 1000, delta=40)
        self.assertGreater(handler.iq_blocks, 0)

    def test_the_sideband_follows_the_radio(self):
        """LSB 收 -1 kHz 的音频信号 —— 选错边带就只剩噪声。"""
        _producer, handler = self._handler("LSB")
        self.assertEqual(handler.mode, "LSB")
        handler.feed_iq(self._tone(-1000))
        chunk = handler.read_rx_chunk()
        self.assertIsNotNone(chunk)
        peaks = []
        while chunk is not None:
            peaks.append(self._peak_hz(chunk))
            chunk = handler.read_rx_chunk()
        self.assertAlmostEqual(min(peaks, key=lambda p: abs(p - 1000)), 1000,
                               delta=40)

    def test_cw_and_fm_modes_map_onto_the_demodulator(self):
        from backends.xiegu.audio import demod_mode_for
        self.assertEqual(demod_mode_for("CW-U"), "CW")
        self.assertEqual(demod_mode_for("cw-l"), "CW")
        self.assertEqual(demod_mode_for("FM"), "NFM")
        self.assertEqual(demod_mode_for("LSB"), "LSB")
        # An unknown name must never raise: this runs from a UDP callback.
        self.assertEqual(demod_mode_for("SOMETHING-NEW"), "USB")
        self.assertEqual(demod_mode_for(None), "USB")

    def test_nothing_is_produced_before_start_rx(self):
        from backends.xiegu.audio import G7250AudioHandler

        class _Producer:
            on_iq = None

        handler = G7250AudioHandler(_Producer(), tx_handler=None)
        handler.feed_iq(self._tone(1000))
        self.assertIsNone(handler.read_rx_chunk())
        self.assertEqual(handler.rx_chunks, 0)

    def test_the_queue_stays_bounded_and_keeps_the_newest(self):
        producer, handler = self._handler("USB")
        for _ in range(80):                    # 80 blocks, queue holds 50
            producer.on_iq(self._tone(1000, n=960))
        self.assertGreater(handler.rx_drops, 0)
        # A consumer that just arrived must not be played a second of history.
        handler.read_rx_chunk()
        self.assertLessEqual(len(handler._rx_audio), 5)

    def test_the_silence_watchdog_counts_only_zero(self):
        _producer, handler = self._handler("USB")
        handler.note_rx_chunk(b"\x00\x00" * 960)
        self.assertGreaterEqual(handler.rx_silent_seconds(), 0.0)
        handler.note_rx_chunk(None)
        handler.note_rx_chunk((np.ones(960) * 500).astype("<i2").tobytes())
        self.assertEqual(handler.rx_silent_seconds(), 0.0)

    def test_opus_frames_replace_pcm_when_available(self):
        _producer, handler = self._handler("USB")
        pcm = (np.sin(np.linspace(0, 200 * np.pi, 960)) * 8000).astype("<i2").tobytes()
        frames = handler.encode_rx_audio(pcm)
        self.assertTrue(frames)
        tags = {f[0] for f in frames}
        self.assertTrue(tags <= {0x00, 0x01})
        self.assertEqual(handler.encode_rx_audio(b"\x00" * 8), [])

    def test_transmit_is_delegated_to_the_sound_card(self):
        _producer, handler = self._handler("USB")
        self.assertTrue(handler.start_tx())
        handler.feed_tx_audio(b"\x01\x02" * 480)
        self.assertTrue(handler.has_pending_tx_audio())
        self.assertEqual(handler.tx_stats()["written"], 7)
        handler.stop_tx(True)
        handler.write_tx_chunk()
        self.assertEqual(handler._tx.calls,
                         ["start_tx", "feed(960)", "stop_tx(True)", "write"])

    def test_without_a_sound_card_tx_refuses_instead_of_pretending(self):
        from backends.xiegu.audio import G7250AudioHandler

        class _Producer:
            on_iq = None

        handler = G7250AudioHandler(_Producer(), tx_handler=None)
        self.assertFalse(handler.start_tx())
        self.assertEqual(handler.tx_stats()["written"], 0)
        self.assertFalse(handler.has_pending_tx_audio())

    def test_close_detaches_from_the_producer(self):
        producer, handler = self._handler("USB")
        handler.close()
        self.assertIsNone(producer.on_iq)
        self.assertIsNone(handler.read_rx_chunk())


class LanAudioWiringTests(unittest.TestCase):
    """Who builds the adapter, and when the stream has to outlive the socket."""

    def test_the_backend_builds_it_only_with_a_lan_address(self):
        from unittest import mock
        with mock.patch("audio_handler.AudioHandler", _FakeTx):
            backend = _backend(lan_ip="192.168.110.199")
            handler = backend.create_audio_handler()
            self.assertIsNotNone(handler)
            self.assertIs(handler._producer, backend._lan)

            off = _backend(lan_ip="")
            self.assertIsNone(off.create_audio_handler())

    def test_the_operator_can_turn_lan_audio_off(self):
        from unittest import mock
        with mock.patch("audio_handler.AudioHandler", _FakeTx), \
                mock.patch.object(config, "G7250_LAN_AUDIO", False):
            backend = _backend(lan_ip="192.168.110.199")
            self.assertIsNone(backend.create_audio_handler())

    def test_audio_and_spectrum_share_one_producer(self):
        """一个 UDP 端口只能有一个主人：两条产品共用一个 socket。"""
        from unittest import mock
        with mock.patch("audio_handler.AudioHandler", _FakeTx):
            backend = _backend(lan_ip="192.168.110.199")
            audio = backend.create_audio_handler()
            scope = mock.MagicMock()
            producer = backend.create_scope_producer(scope, None)
            self.assertIs(producer, audio._producer)
            self.assertIs(producer._scope, scope)

    def test_the_stream_outlives_the_last_waterfall_socket(self):
        from backends.base import scope_keeps_running
        backend = _backend(lan_ip="192.168.110.199")
        producer = backend.create_scope_producer(None, None)
        self.assertTrue(producer.keeps_running())
        self.assertTrue(scope_keeps_running(producer))
        # Every existing producer keeps the historical answer.
        self.assertFalse(scope_keeps_running(object()))

    def test_the_mode_reaches_the_demodulator_through_the_controller(self):
        """模式来自 CAT，而且走的是**控制器**（server 直接用 backend.cat）。"""
        from unittest import mock
        with mock.patch("audio_handler.AudioHandler", _FakeTx):
            backend = _backend(lan_ip="192.168.110.199")
            handler = backend.create_audio_handler()
            controller = backend._make_controller(0xA4)
            self.assertIsNotNone(controller.on_mode)
            controller.on_mode(0x00)                 # LSB
            self.assertEqual(handler.mode, "LSB")
            controller.on_mode(0x01)                 # USB
            self.assertEqual(handler.mode, "USB")

    def test_the_mode_seen_before_the_audio_exists_is_not_lost(self):
        from unittest import mock
        with mock.patch("audio_handler.AudioHandler", _FakeTx):
            backend = _backend(lan_ip="192.168.110.199")
            backend._on_civ_mode(0x07)               # CW-L, during initial sync
            handler = backend.create_audio_handler()
            self.assertEqual(handler.mode, "CW")


class LanAudioEndToEndTests(unittest.TestCase):
    """真 UDP：电台那端的 768 字节 IQ 包 -> 解调 -> 音频块。

    这是本机能做到的最接近真机的一段：`_IQProtocol` -> `_ingest` ->
    `on_iq` -> `AudioDemodulator` -> `read_rx_chunk`，整条链都跑，只有
    「电台真的按这个格式发包」留给真机。
    """

    def test_datagrams_become_audio(self):
        from unittest import mock

        from backends.xiegu.audio import G7250AudioHandler

        async def scenario():
            producer = lan_iq.G7250LanIQProducer(
                "127.0.0.1", None, None, iq_port=0, control_port=0)
            with mock.patch("audio_handler.AudioHandler", _FakeTx):
                pass
            handler = G7250AudioHandler(producer, tx_handler=None)
            handler.set_mode("USB")
            handler.start_rx()
            await producer.start()
            try:
                sender, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                    asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
                # 250 packets/s of 192 pairs each: one second of a 1 kHz tone.
                n = 192
                for block in range(40):
                    t = (np.arange(block * n, (block + 1) * n) / lan_iq.LAN_IQ_RATE_HZ)
                    # int16 full-ish scale, exactly what the radio sends: the
                    # parser divides by 32768, so a 0.4-amplitude float here
                    # truncates to all-zero IQ (which is how this test first
                    # "failed" instead of finding anything).
                    tone = np.exp(2j * np.pi * 1000 * t)
                    payload = np.stack([tone.real * 20000, tone.imag * 20000],
                                       axis=1).astype("<i2").tobytes()
                    self.assertEqual(len(payload), 768)
                    sender.sendto(payload, ("127.0.0.1", producer.bound_port))
                    await asyncio.sleep(0.005)
                for _ in range(100):
                    if handler.rx_chunks and len(handler._rx_audio) > 2:
                        break
                    await asyncio.sleep(0.01)
            finally:
                sender.close() if sender else None
                await producer.stop()
            chunks = []
            while True:
                chunk = handler.read_rx_chunk()
                if chunk is None:
                    break
                chunks.append(chunk)
            return handler, chunks

        handler, chunks = asyncio.run(scenario())
        self.assertGreater(handler.iq_blocks, 5, "769 字节包没有进到解调器")
        self.assertTrue(chunks, "IQ 进来了却没有音频输出")
        joined = b"".join(chunks)
        peaks = LanAudioTests._peak_hz(joined)
        self.assertAlmostEqual(peaks, 1000, delta=80)


class LanTuneSeedTests(unittest.TestCase):
    """开流的前提是把调谐帧发给电台（2026-11-12 现场）。

    诊断包 `20261112-114916-7b99`：`MRRC_G7250_LAN_IP` 已配、socket 已绑，
    每次启动都是 `listening on UDP 50001` 之后 0.4 s 就 `S-meter fallback`，
    四个会话一模一样 —— 一个 IQ 包都没来。原因不在网络：14 字节调谐帧既是
    「调到这个频率」也是「往我这里送流」的请求，而它只在操作员**改频率**时
    才发。客户从头到尾没在网页上调过频率（`Initial state: freq=…` 是 CAT 轮询
    读来的），于是电台从来没被请求过。
    """

    def test_start_sends_the_frequency_remembered_before_the_socket(self):
        async def scenario():
            producer = lan_iq.G7250LanIQProducer(
                "127.0.0.1", None, None, iq_port=0, control_port=0)
            await producer.tune(7_025_900)          # no transport yet: remembered
            received: list = []

            class _Sink(asyncio.DatagramProtocol):
                def datagram_received(self, data, addr):
                    received.append(data)

            control, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                _Sink, local_addr=("127.0.0.1", 0))
            producer._control_port = control.get_extra_info("sockname")[1]
            try:
                await producer.start()
                for _ in range(50):
                    if received:
                        break
                    await asyncio.sleep(0.01)
            finally:
                await producer.stop()
                control.abort()
            return received, producer

        received, producer = asyncio.run(scenario())
        self.assertEqual(received, [lan_iq.build_tune_frame(7_025_900)],
                         "start() 没有把记住的频率发给电台 —— 电台不会开流")

    def test_a_dry_stream_repeats_the_request(self):
        """流一直不来时，调谐帧要重复（电台重启 / 一开始就没收到）。"""
        producer = lan_iq.G7250LanIQProducer("127.0.0.1", None, None, iq_port=0)
        sent: list = []

        class _Transport:
            def sendto(self, data, addr):
                sent.append((data, addr))
            def close(self):
                pass

        producer._transport = _Transport()
        producer._tuned_hz = 14_074_000
        producer._last_retune_at = 100.0              # as start() leaves it

        async def scenario():
            # First call inside the quiet window: nothing yet.
            await producer._maybe_resend_tune(now=100.0)
            first = len(sent)
            await producer._maybe_resend_tune(now=101.0)
            in_window = len(sent)
            # Past the interval, still dry -> send again.
            await producer._maybe_resend_tune(now=100.0 + lan_iq.RETUNE_WHILE_DRY_S + 1)
            return first, in_window, len(sent)

        first, in_window, after = asyncio.run(scenario())
        self.assertEqual(first, 0, "第一次不该发：还没到间隔")
        self.assertEqual(in_window, 0)
        self.assertEqual(after, 1)
        self.assertEqual(sent[0][1], ("127.0.0.1", 50002))

    def test_a_healthy_stream_is_not_retuned(self):
        producer = lan_iq.G7250LanIQProducer("127.0.0.1", None, None, iq_port=0)
        sent: list = []

        class _Transport:
            def sendto(self, data, addr):
                sent.append(data)

        producer._transport = _Transport()
        producer._tuned_hz = 14_074_000
        producer.packets_received = 5                 # packets are arriving
        asyncio.run(producer._maybe_resend_tune(now=1000.0))
        self.assertEqual(sent, [])


class BackendTuneSeedTests(unittest.TestCase):
    """`init_scope()` 用 CAT 读到的频率把流叫醒。"""

    def test_init_scope_tunes_the_lan_stream(self):
        from unittest import mock

        backend = _backend(lan_ip="192.168.110.199")
        producer = backend.create_scope_producer(None, None)
        fake_civ = mock.MagicMock()

        async def get_frequency(timeout=None):
            return 7_025_900

        fake_civ.get_frequency = get_frequency
        backend._civ = fake_civ

        async def scenario():
            await backend.init_scope()
            return producer._tuned_hz

        self.assertEqual(asyncio.run(scenario()), 7_025_900)

    def test_no_lan_means_no_scope_and_no_cat_read(self):
        from unittest import mock
        backend = _backend(lan_ip="")
        fake_civ = mock.MagicMock()
        backend._civ = fake_civ
        asyncio.run(backend.init_scope())
        fake_civ.get_frequency.assert_not_called()


class CivObservationTests(unittest.TestCase):
    """服务端只和 `cat`（控制器）说话 —— 观察点必须在控制器上。

    2026-11-12 的第二个包（`20261112-122412-f0aa`）把这条钉死了：操作员在网页上
    12:11–12:23 连续调了十来次频率（一串 `vfo_a_freq changed … _execute_set_command`），
    `LAN tune failed` 一次都没有，可一个 IQ 包都没回来 —— 因为调谐帧住在
    `G7250Backend.set_frequency()` 里，而 `server.py` 的十处调用全是
    `await cat.set_frequency(...)`，**那个 override 从来没被调用过**。
    """

    def _controller(self, **stub):
        from backends.ic7300.civ_controller import CivController
        from backends.xiegu.backend import _G7250CivController
        ctl = _G7250CivController("/dev/null", 115200, civ_addr=0xA4)
        seen = {"mode": [], "freq": []}
        ctl.on_mode = seen["mode"].append
        ctl.on_frequency = seen["freq"].append
        return ctl, seen, CivController

    def test_a_web_tune_reports_the_frequency(self):
        from unittest import mock
        ctl, seen, base = self._controller()

        async def scenario():
            with mock.patch.object(base, "set_frequency", return_value=True):
                ok = await ctl.set_frequency(7_074_000, "A")
            with mock.patch.object(base, "set_frequency", return_value=False):
                bad = await ctl.set_frequency(3_600_000, "A")
            return ok, bad

        ok, bad = asyncio.run(scenario())
        self.assertTrue(ok)
        self.assertFalse(bad)
        # Only the successful set is reported: a refused command must not move
        # the waterfall to a frequency the radio never took.
        self.assertEqual(seen["freq"], [7_074_000])

    def test_the_panel_knob_reports_through_the_poll(self):
        from unittest import mock
        ctl, seen, base = self._controller()

        async def scenario():
            with mock.patch.object(base, "get_frequency", return_value=14_074_000):
                await ctl.get_frequency()
            with mock.patch.object(base, "get_frequency", return_value=None):
                await ctl.get_frequency()

        asyncio.run(scenario())
        self.assertEqual(seen["freq"], [14_074_000])

    def test_the_backend_pushes_that_frequency_at_the_stream(self):
        backend = _backend(lan_ip="192.168.110.199")
        producer = backend.create_scope_producer(None, None)
        backend._on_civ_frequency(7_074_000)
        self.assertEqual(producer.tuned_hz, 7_074_000)

    def test_the_production_path_puts_the_frame_on_the_wire(self):
        """端到端：controller.set_frequency → backend → producer → UDP。

        这条就是「包里的十六进制到底有没有出去」的回归测试。
        """
        from unittest import mock
        from backends.ic7300.civ_controller import CivController

        backend = _backend(lan_ip="127.0.0.1")
        producer = backend.create_scope_producer(None, None)
        sent: list = []

        class _Transport:
            def sendto(self, data, addr):
                sent.append((data, addr))

            def close(self):
                pass

        producer._transport = _Transport()
        controller = backend._make_controller(0xA4)

        async def scenario():
            with mock.patch.object(CivController, "set_frequency", return_value=True):
                await controller.set_frequency(7_074_000, "A")

        asyncio.run(scenario())
        self.assertEqual(sent, [(lan_iq.build_tune_frame(7_074_000),
                                 ("127.0.0.1", lan_iq.LAN_CONTROL_PORT))])
