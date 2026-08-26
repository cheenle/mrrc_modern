"""Regression coverage for the opt-in max-TX watchdog (MRRC_PTT_MAX_TX_SECONDS).

SDD ch15 layering: client watchdogs + the disconnect dead-man switch are the
primary release layers, but a client that hangs WITHOUT disconnecting (zombie
socket, multi-client session) is caught by neither. The watchdog closes that
gap: after MRRC_PTT_MAX_TX_SECONDS of continuous transmit it unkeys via
set_ptt(False) — fire-and-forget, no blocking verify (V1.2 rule) — zeroes the
TX meters and toasts every control client.
"""
import asyncio
import json
import unittest
from unittest.mock import patch

import server


class _FakeCat:
    def __init__(self):
        self.connected = True
        self.commands = []

    async def set_ptt(self, tx):
        self.commands.append("TX1" if tx else "TX0")


class _FakeRadio:
    is_transmitting = True

    def __init__(self):
        self.updates = []

    def update(self, **fields):
        self.updates.append(fields)
        return set(fields)


class MaxTxWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.cat = _FakeCat()
        self.radio = _FakeRadio()
        # 0.05 s limit with 0.01 s ticks → fires on tick ~6.
        patchers = [
            patch.object(server, "cat", self.cat),
            patch.object(server, "radio", self.radio),
            patch.object(server, "PTT_MAX_TX_SECONDS", 0.05),
            patch.object(server, "MAX_TX_WATCHDOG_INTERVAL", 0.01),
            patch.object(server, "scheduler", None),
            patch.object(server, "ctrl_clients", set()),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def _run_watchdog_until(self, predicate, timeout=2.0):
        async def runner():
            task = asyncio.create_task(server._max_tx_watchdog())
            try:
                deadline = asyncio.get_event_loop().time() + timeout
                while not predicate():
                    if asyncio.get_event_loop().time() > deadline:
                        self.fail("watchdog condition not reached in time")
                    await asyncio.sleep(0.005)
            finally:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        asyncio.run(runner())

    def test_forces_rx_after_continuous_tx_limit(self):
        self._run_watchdog_until(lambda: "TX0" in self.cat.commands)
        self.assertIn("TX0", self.cat.commands)
        # TX meters zeroed alongside the forced release
        released = [u for u in self.radio.updates if u.get("tx_status") == 0]
        self.assertTrue(released)
        self.assertIn("power_meter", released[-1])

    def test_no_verify_loop_after_release(self):
        """SDD ch15/V1.2: exactly one unkey write, no blocking verify retries."""
        self._run_watchdog_until(lambda: "TX0" in self.cat.commands)
        self.assertEqual(self.cat.commands.count("TX0"), 1)


class MaxTxWatchdogDisabledTests(unittest.TestCase):
    def test_disabled_by_default_never_unkeys(self):
        cat = _FakeCat()
        radio = _FakeRadio()
        with patch.object(server, "cat", cat), \
             patch.object(server, "radio", radio), \
             patch.object(server, "PTT_MAX_TX_SECONDS", 0.0), \
             patch.object(server, "MAX_TX_WATCHDOG_INTERVAL", 0.01), \
             patch.object(server, "ctrl_clients", set()):
            async def runner():
                task = asyncio.create_task(server._max_tx_watchdog())
                await asyncio.sleep(0.08)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            asyncio.run(runner())
        self.assertEqual(cat.commands, [])


if __name__ == "__main__":
    unittest.main()
