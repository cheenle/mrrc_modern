"""Tests for the backend scope-init hook (moved from server._init_scope_cat
to RadioBackend.init_scope / FT710Backend.init_scope in Phase 1)."""
import unittest

from backends.ft710.backend import FT710Backend


class FakeCat:
    connected = True

    def __init__(self):
        self.commands = []
        self.serial = None   # Will be checked by getattr

    async def send_command(self, cmd):
        self.commands.append(cmd)
        return "OK"

    async def connect(self):
        self.connected = True


class FakeSerial:
    is_open = True


def _backend_with_fake_cat(fake) -> FT710Backend:
    backend = FT710Backend(port="dummy")
    backend._cat = fake
    return backend


class BackendScopeInitTests(unittest.IsolatedAsyncioTestCase):
    async def test_scope_init_sends_cat_commands(self):
        fake = FakeCat()
        fake.serial = FakeSerial()  # Simulate open serial port
        backend = _backend_with_fake_cat(fake)
        await backend.init_scope()

        # Should send both scope init commands
        self.assertEqual(fake.commands, ["EX040101", "EX040200"])

    async def test_scope_init_skips_when_no_serial(self):
        fake = FakeCat()
        fake.serial = None  # No serial port
        backend = _backend_with_fake_cat(fake)
        await backend.init_scope()

        # Should not send any commands when serial is absent
        self.assertEqual(fake.commands, [])


if __name__ == "__main__":
    unittest.main()
