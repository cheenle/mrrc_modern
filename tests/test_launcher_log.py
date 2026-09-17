"""StartupTee: the crash net for "the server died before logging existed" (spec §5)."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import launcher_log


class StartupTeeTests(unittest.TestCase):
    def _spawn(self, code: str) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", code],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
        self.addCleanup(self._close, proc)
        return proc

    @staticmethod
    def _close(proc: subprocess.Popen) -> None:
        """Reap the child and close its pipe (no ResourceWarning noise)."""
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()

    def test_captures_child_output_and_can_be_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tee = launcher_log.StartupTee(tmp, echo=False)
            proc = self._spawn("print('early crash line')")
            tee.start(proc)
            self._close(proc)
            tee.stop()
            self.assertIn("early crash line", tee.path.read_text(encoding="utf-8"))

    def test_stop_is_idempotent_and_file_closes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tee = launcher_log.StartupTee(tmp, echo=False)
            proc = self._spawn("print('x')")
            tee.start(proc)
            self._close(proc)
            tee.stop()
            tee.stop()                                  # must not raise
            self.assertTrue(tee.path.exists())

    def test_rotates_at_launch_so_the_file_stays_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "server-stdout.log"
            log.write_text("old" * 100, encoding="utf-8")
            launcher_log.StartupTee(tmp, max_bytes=10, echo=False)
            self.assertTrue((Path(tmp) / "server-stdout.log.1").exists())
            self.assertFalse(log.exists())

    def test_unwritable_dir_returns_a_disabled_tee(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ro"
            target.mkdir()
            target.chmod(0o500)
            try:
                tee = launcher_log.StartupTee(str(target / "logs"), echo=False)
                proc = self._spawn("print('y')")
                tee.start(proc)
                self._close(proc)
                tee.stop()
                self.assertFalse(tee.attached)
            finally:
                target.chmod(0o700)

    def test_start_without_a_pipe_is_a_no_op(self):
        """A launcher that forgets stdout=PIPE must not crash the app."""
        with tempfile.TemporaryDirectory() as tmp:
            tee = launcher_log.StartupTee(tmp, echo=False)
            proc = subprocess.Popen([sys.executable, "-c", "pass"])
            self.addCleanup(proc.wait)
            tee.start(proc)
            self.assertFalse(tee.attached)

    def test_drains_after_stop_so_a_chatty_child_cannot_block(self):
        """200 KB > the 64 KB pipe buffer: without draining the child hangs."""
        with tempfile.TemporaryDirectory() as tmp:
            tee = launcher_log.StartupTee(tmp, echo=False)
            proc = self._spawn("print('x' * (1024 * 200)); print('done')")
            tee.start(proc)
            tee.stop()
            self._close(proc)
            self.assertEqual(proc.returncode, 0)

    def test_nothing_is_written_after_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            tee = launcher_log.StartupTee(tmp, echo=False)
            proc = self._spawn("import time; time.sleep(0.3); print('late')")
            tee.start(proc)
            tee.stop()                                      # before the child prints
            self._close(proc)
            self.assertEqual(tee.path.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
