"""
FT-710 scope producer — FT4222 scope_pipe subprocess management
================================================================
Implements the ``ScopeProducer`` protocol from ``backends.base`` for the
Yaesu FT-710.  The actual SPI I/O runs in a standalone subprocess
(``backends.ft710.scope_pipe``, launched as
``python -m backends.ft710.scope_pipe`` with cwd=repo root) to avoid
asyncio/ctypes conflicts; this module spawns it, reads its binary frame
stream, writes parsed fields into the server's shared ``ScopeHandler``,
and auto-restarts it on unexpected exit while spectrum clients remain.

Moved verbatim from ``server.py`` in Phase 1 (multi-radio refactor):
``_scope_pipe_command`` → :func:`pipe_command`,
``_terminate_process_tree_sync`` → :func:`terminate_process_tree_sync`,
``_ensure_scope_pipe_running``/``_stop_scope_pipe``/``_read_scope_pipe``/
``_notify_scope_pipe_tx`` → :class:`FT710ScopeProducer`.

Radio-state coupling (merging s_meter/scope_span/... into ``RadioState``
and broadcasting) stays in ``server.py`` via the ``on_frame`` callback.

Frame format on the pipe's stdout: 4-byte BE uint32 length + payload.
A len=0 frame is a heartbeat (pipe alive but idle).  The pipe's stdin
accepts ``TX:1``/``TX:0`` control lines — the FT-710 garbles its scope
stream during TX, so SPI reads pause while TX is active.
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from backends.ft710.scope_frame import parse_pipe_payload, WF_SIZE

if TYPE_CHECKING:
    from scope_handler import ScopeHandler

logger = logging.getLogger("ft710.scope.pipe")

OnFrameCallback = Callable[["ScopeHandler"], Awaitable[None]]


def pipe_command(repo_root: Path) -> Optional[list[str]]:
    """Return the command used to start the FT4222 scope pipe."""
    if getattr(sys, "frozen", False):
        exe_name = "scope_pipe.exe" if sys.platform == "win32" else "scope_pipe"
        pipe_exe = Path(sys.executable).resolve().parent / exe_name
        if pipe_exe.exists():
            return [str(pipe_exe)]

    scope_pipe_path = repo_root / "backends" / "ft710" / "scope_pipe.py"
    if scope_pipe_path.exists():
        # Run as a package module so its package-relative imports resolve.
        # The subprocess is spawned with cwd=repo_root.
        return [sys.executable, "-m", "backends.ft710.scope_pipe"]
    return None


def terminate_process_tree_sync(pid: int) -> None:
    """Kill a process and (on Windows) its whole tree, best-effort.

    scope_pipe.exe is a PyInstaller onefile bootloader: terminate() only
    reaches the bootloader, orphaning the real worker which keeps the
    FT4222 device open — the next pipe then fails FT_OpenEx with
    FT_DEVICE_NOT_FOUND for seconds.  taskkill /T /F kills the tree
    atomically.  POSIX terminate() signals the process directly.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5,
            )
            return
        except Exception as e:
            logger.debug("taskkill failed, falling back to terminate: %s", e)
    try:
        os.kill(pid, 15)  # SIGTERM
    except Exception:
        pass


class FT710ScopeProducer:
    """ScopeProducer for the FT-710: owns the scope_pipe subprocess.

    Parameters
    ----------
    repo_root:
        Repository root; the pipe subprocess is spawned with this as cwd
        so ``python -m backends.ft710.scope_pipe`` resolves.
    scope:
        The server's shared ``ScopeHandler``; parsed frame fields are
        written into it in-place (may be None in tests).
    on_frame:
        Async callback invoked with the scope handler after each parsed
        frame (server.py merges scope metadata into RadioState there).
    """

    def __init__(
        self,
        repo_root: Path,
        scope: Optional[ScopeHandler] = None,
        on_frame: Optional[OnFrameCallback] = None,
    ):
        self._repo_root = Path(repo_root)
        self._scope = scope
        self._on_frame = on_frame
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._read_task: Optional[asyncio.Task] = None
        self._lock: Optional[asyncio.Lock] = None
        # Last TX state written to the pipe (dedup) and last TX state
        # reported by the server (re-synced into the pipe after a spawn).
        self._last_tx: Optional[bool] = None
        self._tx_state: bool = False
        # True between start() and stop() — gates auto-restart on exit.
        self._active: bool = False

    # ── ScopeProducer protocol ─────────────────────────────────────

    def set_on_frame(self, cb: OnFrameCallback) -> None:
        self._on_frame = cb

    def notify_tx(self, tx: bool, force: bool = False) -> None:
        """Tell scope_pipe when the radio enters/leaves TX (PTT/TUNE).

        The FT-710 garbles its scope stream during TX; the pipe pauses
        SPI reads while TX is active instead of churning through sync
        recovery (which previously ran it into fatal:too_many_reinits on
        every PTT).
        """
        tx = bool(tx)
        self._tx_state = tx
        if not force and tx == self._last_tx:
            return
        self._last_tx = tx
        proc = self._proc
        if proc is None or proc.returncode is not None or proc.stdin is None:
            return
        try:
            proc.stdin.write(b"TX:1\n" if tx else b"TX:0\n")
            asyncio.get_running_loop().create_task(self._drain(proc.stdin))
        except Exception as e:
            logger.debug("scope_pipe TX notify failed: %s", e)

    @staticmethod
    async def _drain(stdin) -> None:
        try:
            await stdin.drain()
        except Exception:
            pass

    async def start(self) -> None:
        """Start the FT4222 scope subprocess (idempotent)."""
        self._active = True
        if self._read_task and not self._read_task.done():
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._read_task and not self._read_task.done():
                return
            scope_pipe_cmd = pipe_command(self._repo_root)
            if not scope_pipe_cmd:
                logger.warning("scope_pipe worker not found — spectrum will use S-meter fallback only")
                return
            logger.info("Starting scope_pipe subprocess for spectrum client...")
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *scope_pipe_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    # `python -m backends.ft710.scope_pipe` needs the repo
                    # root on sys.path; pinning cwd guarantees that
                    # regardless of where the server itself was launched.
                    cwd=str(self._repo_root),
                )
                self._read_task = asyncio.create_task(
                    self._read_pipe(self._proc), name="scope_pipe_read"
                )
                logger.info("scope_pipe subprocess started (pid=%d)", self._proc.pid)
                # Sync the pipe with the current TX state (e.g. pipe
                # started while the radio is already transmitting).
                self.notify_tx(self._tx_state, force=True)
            except Exception as e:
                self._proc = None
                self._read_task = None
                logger.warning("Failed to start scope_pipe: %s", e)
                logger.warning("Spectrum will use S-meter fallback only")

    async def stop(self) -> None:
        """Stop the FT4222 scope subprocess when no spectrum clients remain."""
        self._active = False
        task = self._read_task
        self._read_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        proc = self._proc
        self._proc = None
        if proc and proc.returncode is None:
            try:
                await asyncio.to_thread(terminate_process_tree_sync, proc.pid)
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
            except Exception:
                pass
        if self._scope:
            self._scope._connected = False

    # ── Pipe reader ────────────────────────────────────────────────

    async def _read_pipe(self, proc):
        """Read binary spectrum frames from scope_pipe subprocess stdout.

        Frame format: 4-byte BE uint32 length + payload bytes.
        Heartbeat (len=0) means pipe is alive but idle.

        Updates the shared scope handler's spectrum data in-place.
        Stderr lines starting with "STATUS:" are machine-parseable status
        messages from the pipe process.
        """
        scope = self._scope
        logger.info("Reading from scope_pipe (pid=%d)...", proc.pid)
        _first_frame = True
        _stderr_task = None

        async def _drain_stderr():
            """Continuously read stderr and log STATUS lines at INFO level."""
            try:
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text.startswith("STATUS:"):
                        payload = text[7:]
                        # Log important status messages at INFO level
                        if any(kw in payload for kw in (
                            "fatal:", "pipe_error:", "spi_init_failed",
                            "too_many_errors", "reinitializing_device",
                            "sync_lost", "sync_recovered",
                        )):
                            logger.warning("scope_pipe: %s", payload)
                        elif "heartbeat:" in payload or "diag:" in payload:
                            logger.debug("scope_pipe: %s", payload)
                        else:
                            logger.info("scope_pipe: %s", payload)
                    else:
                        logger.debug("scope_pipe(stderr): %s", text)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug("scope_pipe stderr drain: %s", e)

        # Start stderr reader in background
        _stderr_task = asyncio.create_task(_drain_stderr(), name="scope_stderr")

        try:
            while True:
                # Read 4-byte length header
                header = await proc.stdout.read(4)
                if not header or len(header) < 4:
                    break

                frame_len = struct.unpack('>I', header)[0]

                # Heartbeat frame (len=0): pipe is alive but no data
                if frame_len == 0:
                    continue

                if frame_len < 1 or frame_len > 8192:
                    logger.warning("scope_pipe: bad frame length %d", frame_len)
                    continue

                # Read payload
                payload = await proc.stdout.read(frame_len)
                if len(payload) < frame_len:
                    break

                # Parse pipe payload
                if scope and len(payload) >= 1 + WF_SIZE:
                    try:
                        parsed = parse_pipe_payload(payload)
                    except ValueError as e:
                        logger.warning("scope_pipe: bad payload: %s", e)
                        continue

                    scope.spectrum_rx1 = parsed.wf1
                    scope.spectrum_rx2 = parsed.wf2
                    scope.scope_mode = parsed.scope_mode
                    scope.scope_span = parsed.scope_span
                    scope.preamp = parsed.preamp
                    scope.attenuator = parsed.attenuator
                    scope.mode = parsed.mode
                    scope.s_meter = parsed.s_meter
                    scope.vfoa_freq = parsed.vfoa_freq
                    scope.scope_start_freq = parsed.scope_start_freq
                    scope._frame_count += 1
                    scope.last_update = time.time()

                    # Mark scope as connected on first successful frame
                    if _first_frame:
                        _first_frame = False
                        scope._connected = True
                        logger.info("scope_pipe: first frame received — spectrum active "
                                    "(span=%d, s_meter=%d, wf1_max=%d)",
                                    parsed.scope_span, parsed.s_meter, max(parsed.wf1))

                    if self._on_frame:
                        await self._on_frame(scope)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("scope_pipe read error: %s", e)
        finally:
            # Clean up stderr reader
            if _stderr_task:
                _stderr_task.cancel()
                try:
                    await _stderr_task
                except asyncio.CancelledError:
                    pass

            logger.warning("scope_pipe exited (frames=%d, connected=%s)",
                           scope._frame_count if scope else 0,
                           scope._connected if scope else False)
            if scope:
                scope._connected = False
            if proc.returncode is None:
                await asyncio.to_thread(terminate_process_tree_sync, proc.pid)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        await proc.wait()
                    except Exception:
                        pass
            if self._proc is proc:
                self._proc = None
            if self._read_task is asyncio.current_task():
                self._read_task = None

            # ── Auto-restart ───────────────────────────────────────
            # If the producer is still active (spectrum clients remain)
            # when the pipe exits, restart it after a short delay (1s) so
            # transient USB glitches don't require a manual reconnect.
            if self._active and pipe_command(self._repo_root):
                logger.info("scope_pipe exited while active — will restart in 1s")
                await asyncio.sleep(1.0)
                # Only restart if no other pipe has been started meanwhile
                if self._proc is None:
                    await self.start()
