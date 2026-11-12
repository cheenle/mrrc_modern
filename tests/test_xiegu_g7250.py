"""Xiegu G7250 backend.

Hardware-independent coverage for the first non-Icom/non-Yaesu family:

- registry/profile/capability wiring (a registered model that no dialog can
  reach was a real bug class on the Icom round);
- the CI-V address probe — the G7250's address is undocumented, so "answered
  on the second candidate" and "answered nowhere" both have to behave;
- the **CI-V 0x27** scope geometry the vendor rig file documents
  (475 bins / 11 segments / amp 160), which is what replaced the raw-IQ
  producer an earlier revision assumed — see docs/g7250-wfview-analysis.md;
- the TX gate (unverified model must not key).

The retired LAN-IQ modules are gone; the protocol facts that matter live in the
analysis document, not in dead code.
"""
import asyncio
import os
import unittest
from unittest import mock

import config
from backends import create_backend, known_models, model_verification
from backends.xiegu import xiegu_profiles
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
    def test_spectrum_is_ci_v_not_lan_iq(self):
        """厂商 rig 文件：HasSpectrum=true / 475 / 11 / 160。

        这套几何与 IC-7300 的 0x27 完全一致，也正是我们 CI-V scope 生产者
        期望的形状 —— 频谱走 CAT，与网口无关。
        """
        backend = _backend()
        caps = backend.capabilities
        self.assertEqual(caps.scope_type, "civ27")
        self.assertEqual(caps.default_baud, 115200)
        p = xiegu_profiles.get_profile("g7250")
        self.assertEqual((p.scope_bins, p.scope_seq_max, p.scope_amp_max),
                         (475, 11, 160))

    def test_the_scope_producer_is_the_shared_ci_v_one(self):
        from backends.ic7300.civ_scope import CivScopeProducer
        producer = _backend().create_scope_producer(None, None)
        self.assertIsInstance(producer, CivScopeProducer)

    def test_the_vendor_table_corrects_two_capability_flags(self):
        """`16 41` 自动陷波、`15 15/16` Vd/Id —— 共享默认是 False。"""
        caps = _backend().capabilities
        self.assertTrue(caps.has_auto_notch)
        self.assertTrue(caps.has_vd_id_meters)

    def test_scope_on_is_polled_again(self):
        """没有 0x27 的前提没了：scope_on 必须回到轮询表。"""
        fields = [f for f, _ in _backend().settings_poll_items()]
        self.assertIn("scope_on", fields)
        self.assertIn("filter_width", fields)

    def test_unverified_model_is_tx_gated(self):
        backend = _backend()
        self.assertFalse(backend.capabilities.verified)
        self.assertTrue(backend.capabilities.tx_gated)

    def test_refused_ptt_never_writes(self):
        backend = _backend()
        backend._civ.send_set_command = mock.AsyncMock(return_value=True)
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False, create=True):
            self.assertFalse(asyncio.run(backend.set_ptt(True)))
        backend._civ.send_set_command.assert_not_called()

    def test_ui_modes_have_no_rtty(self):
        self.assertEqual(_backend().ui_modes, ["LSB", "USB", "CW-U", "AM", "FM"])

    def test_a_stale_lan_address_is_named_and_ignored(self):
        """老 env 里的 MRRC_G7250_LAN_IP 不再悄悄失效，而是点名一次。"""
        with mock.patch.object(config, "G7250_LAN_IP", "192.168.110.199"):
            backend = _backend()
            with self.assertLogs("xiegu.backend", level="WARNING") as logs:
                backend._warn_about_the_lan_hint_once()
                backend._warn_about_the_lan_hint_once()
        hits = [line for line in logs.output if "MRRC_G7250_LAN_IP" in line]
        self.assertEqual(len(hits), 1, logs.output)


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


if __name__ == "__main__":
    unittest.main()
