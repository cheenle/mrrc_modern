"""Tests for the Phase 1 backend wiring: model factory, capabilities,
per-backend UI tables, and the fullState capabilities fields."""
import json
import unittest

from backends import create_backend
from backends.base import RadioCapabilities
from backends.ft710.backend import FT710Backend
from backends.ft710 import config_ft710
import config

try:
    import server
except ImportError:
    server = None  # fastapi not available in test environment


class CreateBackendTests(unittest.TestCase):
    def test_ft710_returns_ft710_backend(self):
        backend = create_backend("ft710", port="/dev/null", baud_rate=38400)
        self.assertIsInstance(backend, FT710Backend)

    def test_model_key_is_normalized(self):
        backend = create_backend("  FT710\n", port="/dev/null")
        self.assertIsInstance(backend, FT710Backend)

    def test_unknown_model_raises_value_error(self):
        with self.assertRaises(ValueError):
            create_backend("no-such-radio", port="/dev/null")

    def test_ic7300_returns_ic7300_backend(self):
        from backends.ic7300.backend import IC7300Backend
        backend = create_backend("ic7300", port="/dev/null")
        self.assertIsInstance(backend, IC7300Backend)

    def test_ic7300mk2_alias(self):
        from backends.ic7300.backend import IC7300Backend, IC7300MK2Backend
        backend = create_backend("ic7300mk2", port="/dev/null")
        self.assertIsInstance(backend, IC7300Backend)
        self.assertIsInstance(backend, IC7300MK2Backend)
        self.assertEqual(backend.capabilities.display_name, "Icom IC-7300MK2")

    def test_radio_model_default_from_env(self):
        # MRRC_RADIO_MODEL is unset in the test environment
        self.assertEqual(config.RADIO_MODEL, "ft710")


class CapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.backend = create_backend("ft710", port="/dev/null")
        self.caps = self.backend.capabilities

    def test_capabilities_keys_and_values(self):
        d = self.caps.to_dict()
        self.assertEqual(d["model_name"], "ft710")
        self.assertEqual(d["display_name"], "Yaesu FT-710")
        self.assertEqual(d["default_baud"], 38400)
        self.assertEqual(d["audio_rx_rate"], 44100)
        self.assertEqual(d["audio_tx_rate"], 44100)
        self.assertEqual(d["scope_type"], "ft4222")
        self.assertEqual(d["filter_model"], "width_table")
        self.assertEqual(d["tune_via"], "tx2")
        self.assertTrue(d["has_atu"])
        self.assertTrue(d["has_auto_notch"])
        self.assertTrue(d["has_vd_id_meters"])
        self.assertTrue(d["vfo_b_direct"])
        self.assertEqual(d["att_steps"], [0, 6, 12, 18])
        self.assertEqual(d["preamp_steps"], ["OFF", "AMP1", "AMP2"])
        self.assertEqual(d["scope_spans"], config_ft710.SCOPE_SPANS)
        # tuples are converted to lists for JSON
        self.assertIsInstance(d["audio_name_hints"], list)

    def test_capabilities_to_dict_is_json_serializable(self):
        json.dumps(self.caps.to_dict())

    def test_radio_capabilities_dataclass_round_trip(self):
        caps = RadioCapabilities(
            model_name="x", display_name="X", default_baud=1,
            audio_rx_rate=1, audio_tx_rate=1,
        )
        d = caps.to_dict()
        self.assertEqual(d["model_name"], "x")
        json.dumps(d)


class BackendUiTableTests(unittest.TestCase):
    def setUp(self):
        self.backend = create_backend("ft710", port="/dev/null")

    def test_bands_match_config_ft710(self):
        self.assertEqual(self.backend.bands, config_ft710.BANDS)

    def test_ui_modes_match_config(self):
        self.assertEqual(self.backend.ui_modes, config.UI_MODES)

    def test_filter_tables_match_config(self):
        tables = self.backend.filter_tables()
        self.assertEqual(tables["voice"], config_ft710.FILTER_WIDTHS_VOICE)
        self.assertEqual(tables["narrow"], config_ft710.FILTER_WIDTHS_NARROW)
        self.assertEqual(tables["narrowModes"], sorted(config.NARROW_MODES))
        json.dumps(tables)

    def test_scope_producer_created(self):
        producer = self.backend.create_scope_producer(scope_handler=None)
        self.assertIsNotNone(producer)
        # ScopeProducer protocol surface
        for attr in ("start", "stop", "notify_tx", "set_on_frame"):
            self.assertTrue(hasattr(producer, attr), attr)


class IC7300CapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.backend = create_backend("ic7300", port="/dev/null")
        self.caps = self.backend.capabilities

    def test_capabilities_values(self):
        d = self.caps.to_dict()
        self.assertEqual(d["model_name"], "ic7300")
        self.assertEqual(d["display_name"], "Icom IC-7300")
        self.assertEqual(d["default_baud"], 115200)
        self.assertEqual(d["audio_rx_rate"], 48000)
        self.assertEqual(d["audio_tx_rate"], 48000)
        self.assertEqual(d["scope_type"], "civ27")
        self.assertEqual(d["filter_model"], "fil123")
        self.assertEqual(d["tune_via"], "atu")
        self.assertTrue(d["has_atu"])
        self.assertFalse(d["has_auto_notch"])
        self.assertFalse(d["has_vd_id_meters"])
        self.assertFalse(d["vfo_b_direct"])
        self.assertEqual(d["att_steps"], [0, 20])
        self.assertEqual(d["preamp_steps"], ["OFF", "AMP1", "AMP2"])
        json.dumps(d)

    def test_scope_producer_created(self):
        from backends.ic7300.civ_scope import CivScopeProducer
        producer = self.backend.create_scope_producer(scope_handler=None)
        self.assertIsInstance(producer, CivScopeProducer)
        # ScopeProducer protocol surface
        for attr in ("start", "stop", "notify_tx", "set_on_frame"):
            self.assertTrue(hasattr(producer, attr), attr)
        # Producer consumes the backend's own controller queue.
        self.assertIs(producer._civ, self.backend.cat)

    def test_ui_tables(self):
        from backends.ic7300 import config_ic7300
        self.assertEqual(self.backend.bands, config_ic7300.BANDS)
        self.assertNotIn("DATA-L", self.backend.ui_modes)
        tables = self.backend.filter_tables()
        self.assertEqual(tables["model"], "fil123")
        self.assertEqual(tables["voice"][0], (1, 3000))   # FIL1 SSB default
        self.assertIn("narrowModes", tables)
        json.dumps(tables)

    def test_backend_exposes_cat_surface(self):
        # server.py does `cat = backend.cat`
        from backends.ic7300.civ_controller import CivController
        self.assertIsInstance(self.backend.cat, CivController)


@unittest.skipIf(server is None, "fastapi not available in test environment")
class FullStateCapabilitiesTests(unittest.TestCase):
    """server._full_state_message() must include the Phase 1 fields."""

    def _build(self, backend):
        old = server.backend
        server.backend = backend
        try:
            return server._full_state_message({"vfo_a_freq": 14_200_000}, [None] * 6)
        finally:
            server.backend = old

    def test_fullstate_includes_radio_model_and_capabilities(self):
        backend = create_backend("ft710", port="/dev/null")
        msg = self._build(backend)
        self.assertEqual(msg["type"], "fullState")
        self.assertEqual(msg["radioModel"], "ft710")
        self.assertEqual(msg["radioDisplayName"], "Yaesu FT-710")
        self.assertEqual(msg["capabilities"]["model_name"], "ft710")
        self.assertEqual(msg["capabilities"]["scope_type"], "ft4222")
        # Whole payload stays JSON-serializable (WS send path)
        json.dumps(msg)

    def test_fullstate_tables_come_from_backend(self):
        backend = create_backend("ft710", port="/dev/null")
        msg = self._build(backend)
        self.assertEqual(msg["bands"], config_ft710.BANDS)
        self.assertEqual(msg["modes"], config.UI_MODES)
        self.assertEqual(msg["filterTables"]["voice"], config_ft710.FILTER_WIDTHS_VOICE)
        self.assertEqual(msg["filterTables"]["narrowModes"], sorted(config.NARROW_MODES))

    def test_fullstate_falls_back_without_backend(self):
        msg = self._build(None)
        self.assertEqual(msg["radioModel"], config.RADIO_MODEL)
        self.assertEqual(msg["capabilities"], {})
        self.assertEqual(msg["bands"], config_ft710.BANDS)
        self.assertEqual(msg["modes"], config.UI_MODES)
        json.dumps(msg)


if __name__ == "__main__":
    unittest.main()
