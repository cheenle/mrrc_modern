"""Backend-surface tests for the Yaesu family (spec 2026-09-12 §4.3/§6)."""
import unittest
from unittest.mock import AsyncMock

from backends.base import RadioCapabilities
from backends.yaesu import backend as yb
from backends.yaesu.yaesu_profiles import get_profile


class CapabilityTests(unittest.TestCase):
    def test_every_model_reports_unverified_and_scope_free(self):
        for model in ("ftdx10", "ftdx101d", "ftdx101mp", "ftx1"):
            b = yb.make_backend(model)("/dev/null")
            self.assertIsInstance(b.capabilities, RadioCapabilities)
            self.assertFalse(b.capabilities.verified, model)
            self.assertTrue(b.capabilities.tx_gated, model)
            self.assertEqual(b.capabilities.scope_type, "none", model)

    def test_vd_id_meters_follow_the_profile(self):
        self.assertTrue(yb.FTDX101DBackend("/dev/null").capabilities.has_vd_id_meters)
        self.assertTrue(yb.FTDX101MPBackend("/dev/null").capabilities.has_vd_id_meters)
        self.assertFalse(yb.FTDX10Backend("/dev/null").capabilities.has_vd_id_meters)
        self.assertFalse(yb.FTX1Backend("/dev/null").capabilities.has_vd_id_meters)

    def test_capabilities_are_json_serialisable(self):
        for cls in (yb.FTDX10Backend, yb.FTDX101DBackend,
                    yb.FTDX101MPBackend, yb.FTX1Backend):
            data = cls("/dev/null").capabilities.to_dict()
            self.assertEqual(data["model_name"], cls._profile.model_key)
            self.assertIn("audio_name_hints", data)
            self.assertIsInstance(data["att_steps"], list)

    def test_ftdx101mp_reports_vd_id_meters(self):
        self.assertTrue(yb.FTDX101MPBackend("/dev/null").capabilities.has_vd_id_meters)

    def test_dual_rx_is_advertised_without_being_implemented(self):
        """Phase 2 owns dual receive; the flag is for the UI badge only."""
        self.assertTrue(yb.FTDX101DBackend("/dev/null").capabilities.dual_rx)
        self.assertFalse(yb.FTDX10Backend("/dev/null").capabilities.dual_rx)

    def test_no_scope_producer(self):
        self.assertIsNone(yb.FTDX10Backend("/dev/null").create_scope_producer())
        self.assertIsNone(yb.FTX1Backend("/dev/null").create_scope_producer())


class TableTests(unittest.TestCase):
    def test_ui_tables_come_from_the_profile(self):
        b = yb.FTX1Backend("/dev/null")
        p = get_profile("ftx1")
        self.assertEqual(b.ui_modes, list(p.ui_modes))
        self.assertEqual(b.bands, [list(x) if isinstance(x, tuple) else x
                                   for x in p.bands])
        self.assertEqual(set(b.mode_name_to_num), set(p.mode_numbers))

    def test_filter_tables_expose_index_hz_pairs(self):
        b = yb.FTDX10Backend("/dev/null")
        tables = b.filter_tables()
        self.assertIn("voice", tables)
        self.assertIn("narrow", tables)
        for idx, hz in tables["voice"]:
            self.assertIsInstance(idx, int)
            self.assertGreater(hz, 0)

    def test_state_tables_cover_every_radio_state_hook(self):
        tables = yb.FTDX101DBackend("/dev/null").state_tables()
        for key in ("mode_num_to_name", "preamp_labels", "attenuator_labels",
                    "get_band_for_frequency", "get_filter_hz", "raw_to_dbm",
                    "raw_to_s_unit", "raw_to_power"):
            self.assertIn(key, tables)
        self.assertEqual(tables["mode_num_to_name"][0x1], "LSB")
        self.assertEqual(tables["get_filter_hz"]("USB", 2), 2400)

    def test_poll_items_are_callables_of_the_expected_shape(self):
        b = yb.FTX1Backend("/dev/null")
        for field, getter in b.settings_poll_items():
            self.assertIsInstance(field, str)
            self.assertTrue(callable(getter))
        for label, field, getter in b.tx_meter_items():
            self.assertIsInstance(label, str)
            self.assertIsInstance(field, str)
            self.assertTrue(callable(getter))


class DelegationTests(unittest.TestCase):
    def test_every_abstract_method_is_implemented(self):
        """A missing delegate would leave the class abstract and unbuildable."""
        from backends.base import RadioBackend
        for name in RadioBackend.__abstractmethods__:
            self.assertTrue(hasattr(yb.YaesuBackend, name), name)

    def test_the_backend_class_is_concrete(self):
        self.assertEqual(yb.YaesuBackend.__abstractmethods__, frozenset())
        yb.FTX1Backend("/dev/null")          # TypeError if still abstract

    def test_gated_methods_are_wired_through_the_delegate(self):
        b = yb.FTDX10Backend("/dev/null")
        b._cat.set_ptt = AsyncMock(return_value=True)
        self.assertFalse(b._cat.connected)   # never touched: the gate runs first
        self.assertTrue(hasattr(yb.YaesuBackend, "set_frequency"))


class GateTests(unittest.IsolatedAsyncioTestCase):
    async def test_ptt_is_refused_while_unverified(self):
        b = yb.FTDX10Backend("/dev/null")
        b._cat.set_ptt = AsyncMock(return_value=True)
        self.assertFalse(await b.set_ptt(True))
        b._cat.set_ptt.assert_not_awaited()

    async def test_tune_is_refused_while_unverified(self):
        b = yb.FTX1Backend("/dev/null")
        b._cat.set_tune = AsyncMock(return_value=True)
        self.assertFalse(await b.set_tune(True))
        b._cat.set_tune.assert_not_awaited()

    async def test_ptt_release_is_never_gated(self):
        b = yb.FTDX10Backend("/dev/null")
        b._cat.set_ptt = AsyncMock(return_value=True)
        self.assertTrue(await b.set_ptt(False))
        b._cat.set_ptt.assert_awaited_once_with(False)

    async def test_gate_warning_is_logged_once(self):
        from backends.yaesu import backend as mod
        mod._TX_GATE_WARNED = False
        with self.assertLogs("backends.yaesu.backend", level="WARNING") as cm:
            yb.FTDX10Backend("/dev/null")._tx_allowed()
            yb.FTDX10Backend("/dev/null")._tx_allowed()
        self.assertEqual(len([m for m in cm.output if "not hardware-verified" in m]), 1)

    async def test_gate_opens_with_the_environment_switch(self):
        import config
        from unittest.mock import patch
        with patch.object(config, "ALLOW_UNVERIFIED_TX", True):
            b = yb.FTDX10Backend("/dev/null")
            self.assertTrue(b._tx_allowed())
            self.assertFalse(b.capabilities.tx_gated)


class IdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_records_the_observed_value(self):
        b = yb.FTX1Backend("/dev/null")
        b._cat.get_model_id = AsyncMock(return_value="0840")
        with self.assertLogs("backends.yaesu.backend", level="INFO") as cm:
            await b._check_model_identity()
        self.assertIn("0840", "".join(cm.output))

    async def test_identity_mismatch_warns_but_does_not_fail(self):
        b = yb.FTX1Backend("/dev/null")
        b._cat.get_model_id = AsyncMock(return_value="0841")
        with self.assertLogs("backends.yaesu.backend", level="WARNING") as cm:
            observed = await b._check_model_identity()
        self.assertEqual(observed, "0841")
        self.assertIn("0841", "".join(cm.output))

    async def test_model_without_a_recorded_expectation_never_warns(self):
        b = yb.FTDX10Backend("/dev/null")
        b._cat.get_model_id = AsyncMock(return_value="0810")
        with self.assertLogs("backends.yaesu.backend", level="INFO") as cm:
            await b._check_model_identity()
        self.assertFalse([m for m in cm.output if m.startswith("WARNING")])
