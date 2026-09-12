"""Unverified-model transmit gate (spec 2026-09-12 §6.1).

The gate must keep an unverified radio from transmitting, must never
block a PTT release, and must not touch the serial port when it refuses.
"""
import unittest
from unittest import mock

import config
from backends import create_backend
from backends.ic7300.backend import IC7300Backend
from backends.ic7300.civ_scope import CivScopeProducer


def _backend(model: str) -> IC7300Backend:
    """Construct a backend for *model* with a type the checker can see."""
    backend = create_backend(model, port="/dev/null")
    assert isinstance(backend, IC7300Backend)      # narrow for the checker
    return backend


class UnverifiedTxGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The gate warns once per process — reset it so the log-count
        # assertion cannot depend on test execution order.
        import backends.ic7300.backend as icom_backend
        icom_backend._TX_GATE_WARNED = False

    async def test_ic705_is_gated_by_default(self):
        backend = _backend("ic705")
        self.assertTrue(backend.capabilities.tx_gated)
        self.assertFalse(backend.capabilities.verified)

    @mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False, create=True)
    async def test_refused_ptt_does_not_write_to_serial(self):
        backend = _backend("ic705")
        backend._civ.send_set_command = mock.AsyncMock(return_value=True)
        self.assertFalse(await backend.set_ptt(True))
        backend._civ.send_set_command.assert_not_called()

    @mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False, create=True)
    async def test_refused_tune_does_not_write_to_serial(self):
        backend = _backend("ic7760")
        backend._civ.send_set_command = mock.AsyncMock(return_value=True)
        self.assertFalse(await backend.set_tune(True))
        backend._civ.send_set_command.assert_not_called()

    @mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False, create=True)
    async def test_release_is_never_blocked(self):
        # Refusing TX0 would strand the carrier (Chapter 15 layering):
        # only keying is gated.
        backend = _backend("ic705")
        backend._civ.set_ptt = mock.AsyncMock(return_value=True)
        self.assertTrue(await backend.set_ptt(False))
        backend._civ.set_ptt.assert_awaited_once_with(False)
        backend._civ.set_tune = mock.AsyncMock(return_value=True)
        self.assertTrue(await backend.set_tune(False))
        backend._civ.set_tune.assert_awaited_once_with(False)

    @mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True, create=True)
    async def test_env_var_allows_transmit(self):
        backend = _backend("ic705")
        self.assertFalse(backend.capabilities.tx_gated)
        backend._civ.set_ptt = mock.AsyncMock(return_value=True)
        self.assertTrue(await backend.set_ptt(True))

    @mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False, create=True)
    async def test_verified_models_are_unaffected(self):
        for model in ("ic7300", "ic7300mk2"):
            with self.subTest(model=model):
                backend = _backend(model)
                self.assertFalse(backend.capabilities.tx_gated)
                backend._civ.set_ptt = mock.AsyncMock(return_value=True)
                self.assertTrue(await backend.set_ptt(True))

    @mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False, create=True)
    async def test_gate_logs_once(self):
        backend = _backend("ic7610")
        backend._civ.send_set_command = mock.AsyncMock(return_value=True)
        with self.assertLogs("ic7300.backend", level="WARNING") as cap:
            await backend.set_ptt(True)
            await backend.set_ptt(True)
        self.assertEqual(len(cap.output), 1)
        self.assertIn("MRRC_ALLOW_UNVERIFIED_TX", cap.output[0])


class NewModelCapabilityTests(unittest.TestCase):
    def test_capabilities_are_profile_derived(self):
        ic705 = _backend("ic705").capabilities
        self.assertEqual(ic705.model_name, "ic705")
        self.assertEqual(ic705.display_name, "Icom IC-705")
        self.assertEqual(ic705.att_steps, (0, 20))
        self.assertEqual(ic705.audio_gain_boost, 1.0)
        self.assertEqual(ic705.unverified_meters,
                         ("power", "voltage", "current"))
        self.assertEqual(ic705.scope_type, "civ27")
        self.assertFalse(ic705.dual_rx)

        ic7760 = _backend("ic7760").capabilities
        self.assertEqual(len(ic7760.att_steps), 16)
        self.assertTrue(ic7760.dual_rx)

    def test_scope_geometry_reaches_the_producer(self):
        for model, (bins, amp, seq) in (
                ("ic705", (475, 160, 11)),
                ("ic7610", (689, 200, 15)),
                ("ic7760", (689, 200, 15))):
            with self.subTest(model=model):
                backend = _backend(model)
                producer = backend.create_scope_producer()
                assert isinstance(producer, CivScopeProducer)
                self.assertEqual(producer._amp_max, amp)
                self.assertEqual(producer._assembler._seq_max_hint, seq)
                self.assertEqual(producer._assembler._expected_bins, bins)

    def test_civ_addresses_are_per_model(self):
        for model, addr in (("ic705", 0xA4), ("ic7610", 0x98),
                            ("ic7760", 0xB2)):
            with self.subTest(model=model):
                backend = _backend(model)
                self.assertEqual(backend._civ.civ_addr, addr)

    def test_transceive_item_is_per_model(self):
        for model, item in (("ic705", b"\x01\x12"), ("ic7610", b"\x01\x12"),
                            ("ic7760", b"\x01\x50")):
            with self.subTest(model=model):
                backend = _backend(model)
                self.assertEqual(backend._civ._transceive_cmd,
                                 bytes((0x1A, 0x05)) + item + b"\x01")


if __name__ == "__main__":
    unittest.main()
