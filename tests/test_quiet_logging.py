import asyncio
from pathlib import Path
import unittest


class QuietLoggingSourceTests(unittest.TestCase):
    def test_periodic_scope_diagnostics_are_debug_not_info(self):
        source = Path("server.py").read_text(encoding="utf-8")
        branch = source.split('elif "heartbeat:" in payload or "diag:" in payload:', 1)[1]
        branch = branch.split("else:", 1)[0]
        self.assertIn('logger.debug("scope_pipe: %s", payload)', branch)
        self.assertNotIn("logger.info", branch)

    def test_periodic_audio_loop_health_is_debug_not_info(self):
        source = Path("server.py").read_text(encoding="utf-8")
        branch = source.split("# Periodic health log", 1)[1]
        branch = branch.split("# RX silence watchdog", 1)[0]
        self.assertIn('logger.debug("Audio loop:', branch)
        self.assertNotIn("logger.info", branch)

    def test_meter_broadcast_uses_debug_logging(self):
        source = Path("server.py").read_text(encoding="utf-8")
        branch = source.split('"Meter broadcast dirty=%s', 1)[0]
        last_logger_call = branch.rsplit("logger.", 1)[1]
        self.assertTrue(last_logger_call.startswith("debug("))


class TXOnlyMeterResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_tx_to_rx_clears_id_meter_with_other_tx_only_meters(self):
        from poll_scheduler import PollScheduler
        from radio_state import RadioState

        scheduler = None

        class FakeCat:
            connected = True
            _cancel_polls = asyncio.Event()

            async def get_ptt(self, timeout=None):
                scheduler._running = False
                return 0

        state = RadioState(
            tx_status=1,
            power_meter=120,
            alc_meter=80,
            swr_meter=30,
            comp_meter=20,
            id_meter=70,
        )
        scheduler = PollScheduler(FakeCat(), state)
        scheduler._running = True

        await scheduler._poll_tx_status()

        self.assertEqual(state.power_meter, 0)
        self.assertEqual(state.alc_meter, 0)
        self.assertEqual(state.swr_meter, 0)
        self.assertEqual(state.comp_meter, 0)
        self.assertEqual(state.id_meter, 0)


if __name__ == "__main__":
    unittest.main()
