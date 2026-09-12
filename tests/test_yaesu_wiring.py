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


class BaudTests(unittest.TestCase):
    def test_yaesu_models_default_to_38400(self):
        for key in YAESU_KEYS:
            self.assertEqual(default_baud_for(key), 38400)

    def test_every_registered_model_has_a_baud_entry(self):
        for key in known_models():
            self.assertIn(default_baud_for(key), (38400, 115200))
