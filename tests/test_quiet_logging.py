import asyncio
import logging
import logging.handlers
import tempfile
from pathlib import Path
from typing import Any, cast
import unittest
from unittest import mock

import server


class QuietLoggingSourceTests(unittest.TestCase):
    def test_periodic_scope_diagnostics_are_debug_not_info(self):
        # The scope_pipe stderr drain moved to the backend's scope producer.
        source = Path("backends/ft710/scope_producer.py").read_text(encoding="utf-8")
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

        scheduler: Any = None

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
        scheduler = PollScheduler(cast(Any, FakeCat()), state)
        scheduler._running = True

        await scheduler._poll_tx_status()

        self.assertEqual(state.power_meter, 0)
        self.assertEqual(state.alc_meter, 0)
        self.assertEqual(state.swr_meter, 0)
        self.assertEqual(state.comp_meter, 0)
        self.assertEqual(state.id_meter, 0)


class SupportLogFileTests(unittest.TestCase):
    """Spec 2026-09-17 §5: the packaged app had no server log file at all."""

    def _fresh(self, tmp):
        """Re-run the file-logging setup against a patched LOG_DIR.

        LOG_DIR is a module constant (import time), so the env var cannot be
        patched here — the env-driven path is verified by the smoke run in the
        plan (a fresh process with MRRC_LOG_DIR set).
        """
        root = logging.getLogger()
        saved = list(root.handlers)
        try:
            for handler in saved:
                root.removeHandler(handler)
            with mock.patch.object(server, "LOG_DIR", Path(tmp)):
                path = server._setup_file_logging()
            logging.getLogger("mrrc").info("hello from the test")
        finally:
            for handler in list(root.handlers):
                if isinstance(handler, logging.handlers.RotatingFileHandler):
                    handler.close()
                    root.removeHandler(handler)
            for handler in saved:
                root.addHandler(handler)
        return path

    def test_creates_a_rotating_log_under_log_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._fresh(tmp)
            if path is None:                    # must not degrade in a writable dir
                self.fail("file logging did not attach")
            self.assertTrue(str(path).startswith(tmp))
            self.assertTrue(path.exists())
            self.assertIn("hello from the test", path.read_text(encoding="utf-8"))

    def test_handler_rotates_so_the_file_stays_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fresh(tmp)
            handlers = [h for h in logging.getLogger().handlers
                        if isinstance(h, logging.handlers.RotatingFileHandler)]
            self.assertEqual(len(handlers), 1)
            self.assertEqual(handlers[0].maxBytes, 2 * 1024 * 1024)
            self.assertEqual(handlers[0].backupCount, 1)

    def test_unwritable_dir_degrades_to_console_only(self):
        """A FILE where the directory must be created: fails on every OS.

        The first version chmod'ed a directory read-only, which Windows ignores
        (POSIX modes are not ACLs) — so the test passed on macOS and failed on the
        build VM (2026-09-17).
        """
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "logs"
            blocker.write_text("not a directory", encoding="utf-8")
            with mock.patch.object(server, "LOG_DIR", blocker):
                self.assertIsNone(server._setup_file_logging())

    def test_log_dir_defaults_to_logs_next_to_the_runtime(self):
        self.assertEqual(server.LOG_DIR.name, "logs")
        self.assertEqual(server.SUPPORT_OUT_DIR, server.LOG_DIR.parent / "support-out")

    def test_support_log_file_is_attached_at_import(self):
        log_file = server.SUPPORT_LOG_FILE
        if log_file is None:
            self.fail("import-time file logging did not attach")
        self.assertEqual(log_file.name, "server.log")


if __name__ == "__main__":
    unittest.main()
