"""Wiring tests: registry, baud table, factory and packaging (spec §4.4)."""
import re
import unittest

from backends import create_backend, known_models
from config import default_baud_for

YAESU_KEYS = ("ftdx10", "ftdx101d", "ftdx101mp", "ftx1", "ft891")


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


class BackendToControllerSurfaceTests(unittest.TestCase):
    """Guards the *inner* surface: what a backend asks of its controller.

    `ServerVisibleSurfaceTests` above proves the backend exposes what the
    server reads. This proves the reverse direction actually resolves: every
    method a poll-item closure or an `initial_state_sync` getter calls must
    exist on the controller. Two of them (`get_preamp`, `get_attenuator`)
    did not, so the first successful Yaesu connect raised AttributeError —
    and nothing caught it, because a hardware-free boot never reaches the
    sync and no test called it on this family.
    """

    POLL_GROUPS = ("settings_poll_items", "slow_poll_items",
                   "tx_meter_items", "always_meter_items")

    def _referenced_methods(self, fn) -> tuple:
        """Method names a closure calls, from its code object (no invocation)."""
        code = getattr(fn, "__code__", None)
        if code is None:
            return ()
        return tuple(n for n in code.co_names if n.startswith(("get_", "set_")))

    def test_every_poll_item_resolves_on_the_controller(self):
        for key in YAESU_KEYS:
            backend = create_backend(key, "/dev/null")
            controller = backend.cat
            for group in self.POLL_GROUPS:
                # Shapes differ per group: the settings/slow tiers yield
                # (field, fn), the two meter tiers yield (label, field, fn).
                # Pick the callable out rather than assuming a position.
                for item in getattr(backend, group)():
                    callables = [c for c in item if callable(c)]
                    self.assertEqual(len(callables), 1, f"{key}.{group}: {item!r}")
                    for name in self._referenced_methods(callables[0]):
                        self.assertTrue(
                            hasattr(controller, name),
                            f"{key}.{group}{item[:-1]} calls "
                            f"{type(controller).__name__}.{name}, which does not exist")

    def test_initial_state_sync_getters_resolve_on_the_controller(self):
        """The sync is only reachable after a successful connect, so a missing
        method here is a crash in the field rather than a failed test."""
        import inspect
        from backends.yaesu import cat_core
        source = inspect.getsource(cat_core.YaesuCatController.initial_state_sync)
        called = set(re.findall(r"self\.(get_[a-z_]+)\(", source))
        self.assertTrue(called, "no getters found — did the sync change shape?")
        for key in YAESU_KEYS:
            controller = create_backend(key, "/dev/null").cat
            for name in sorted(called):
                self.assertTrue(
                    hasattr(controller, name),
                    f"{key}: initial_state_sync calls {name}, which does not exist")


class BaudTests(unittest.TestCase):
    def test_yaesu_models_default_to_38400(self):
        for key in YAESU_KEYS:
            self.assertEqual(default_baud_for(key), 38400)

    def test_every_registered_model_has_a_baud_entry(self):
        for key in known_models():
            self.assertIn(default_baud_for(key), (38400, 115200))
