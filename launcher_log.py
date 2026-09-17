"""Launcher-side startup tee (spec 2026-09-17 §5).

The launcher is the only component that sees a server which dies before its own
logging exists (PyInstaller missing module, "Failed to load Python shared
library", port already in use).  The tee captures that window into a bounded
file AND echoes it, so the Windows console keeps showing what it always showed.

It stops as soon as the server answers HTTP: from then on `logs/server.log`
(written inside the server) is the canonical record, and a second copy would only
double the support bundle.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

DEFAULT_NAME = "server-stdout.log"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024


class StartupTee:
    """Drain a child's stdout/stderr into a bounded file until told to stop."""

    def __init__(self, log_dir, name: str = DEFAULT_NAME,
                 max_bytes: int = DEFAULT_MAX_BYTES, echo: bool = True):
        self.dir = Path(log_dir)
        self.name = name
        self.max_bytes = max_bytes
        self.echo = echo
        self.path = self.dir / name
        self.attached = False
        self._fh = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._rotate()

    def _rotate(self) -> None:
        """Replace the previous run's file (and its single backup) at launch."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                os.replace(self.path, self.dir / (self.name + ".1"))
        except OSError:
            pass

    def start(self, proc) -> None:
        """Begin draining `proc.stdout` (the child must be spawned with a pipe)."""
        if proc is None or proc.stdout is None:
            return
        try:
            self._fh = open(self.path, "w", encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"Startup log disabled ({e})", file=sys.stderr)
            return
        self.attached = True
        self._thread = threading.Thread(target=self._pump, args=(proc,),
                                        name="startup-tee", daemon=True)
        self._thread.start()

    def _pump(self, proc) -> None:
        """Drain the child's stdout until EOF.

        `stop()` only closes the file — this loop keeps reading and echoing for
        the whole session on purpose:
          * a closed read end would make a chatty server die of EPIPE / block on a
            full 64 KB pipe once the launcher stopped reading;
          * the Windows console showed the server's output before this change and
            must keep showing it (no regression).
        """
        try:
            for line in proc.stdout:
                with self._lock:
                    if self._fh is not None:
                        self._fh.write(line)
                        self._fh.flush()
                if self.echo:
                    try:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                    except (OSError, ValueError):
                        pass
        except (OSError, ValueError):
            pass

    def stop(self, timeout: float = 2.0) -> None:
        """Stop writing to the file (idempotent).

        The drain thread is deliberately not joined: it must outlive this call to
        keep the pipe empty, and it exits by itself when the child does.
        """
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                finally:
                    self._fh = None
