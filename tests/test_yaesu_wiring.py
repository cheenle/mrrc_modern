"""Wiring tests: registry, baud table, factory and packaging (spec §4.4)."""
import unittest

from backends import create_backend, known_models
from config import default_baud_for

YAESU_KEYS = ("ftdx10", "ftdx101d", "ftdx101mp", "ftx1")


class RegistryTests(unittest.TestCase):
    def test_keys_are_registered(self):
        for key in YAESU_KEYS:
            self.assertIn(key, known_models())

    def test_factory_returns_the_model_backend(self):
        for key in YAESU_KEYS:
            backend = create_backend(key, "/dev/null")
            self.assertEqual(backend.model, key)
            self.assertEqual(backend.capabilities.model_name, key)

    def test_ft710_is_untouched(self):
        from backends.ft710.backend import FT710Backend
        backend = create_backend("ft710", "/dev/null")
        self.assertIsInstance(backend, FT710Backend)
        self.assertEqual(backend.capabilities.model_name, "ft710")
        self.assertTrue(backend.capabilities.verified)
        # Its `model` property reports the radio's self-identified model
        # ("Unknown" until CAT answers), not the registry key — unlike the
        # profile-driven backends, whose key is static.
        self.assertEqual(backend.model, "Unknown")

    def test_unknown_model_still_raises(self):
        with self.assertRaises(ValueError):
            create_backend("ftdx9999", "/dev/null")


class ServerVisibleSurfaceTests(unittest.TestCase):
    """Guards the surface `server.py`/`poll_scheduler.py` read from a backend.

    The lifespan startup reads `backend.cat` unconditionally, so a backend
    without it aborts application startup entirely — not a degraded feature,
    a dead server.  (`set_broadcast_callback` is absent from this list: the
    server calls it behind `hasattr`, and the ASCII-CAT family has no
    transceive broadcast to forward.)  Found by the task-9 boot smoke test.
    """

    REQUIRED = (
        "cat", "capabilities", "bands", "ui_modes", "mode_name_to_num",
        "filter_tables", "state_tables", "settings_poll_items",
        "slow_poll_items", "tx_meter_items", "always_meter_items",
        "connected", "model", "init_scope", "boot_verify",
        "initial_state_sync", "create_scope_producer",
    )

    def test_every_registered_backend_exposes_the_required_surface(self):
        for key in known_models():
            backend = create_backend(key, "/dev/null")
            for name in self.REQUIRED:
                self.assertTrue(hasattr(backend, name), f"{key}.{name}")

    def test_the_yaesu_cat_property_returns_the_controller(self):
        backend = create_backend("ftx1", "/dev/null")
        self.assertIs(backend.cat, backend._cat)


class BaudTests(unittest.TestCase):
    def test_yaesu_models_default_to_38400(self):
        for key in YAESU_KEYS:
            self.assertEqual(default_baud_for(key), 38400)

    def test_every_registered_model_has_a_baud_entry(self):
        for key in known_models():
            self.assertIn(default_baud_for(key), (38400, 115200))
