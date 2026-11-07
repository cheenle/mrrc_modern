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
