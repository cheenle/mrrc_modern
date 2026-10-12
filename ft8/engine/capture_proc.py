"""Isolated audio capture process (2026-08-03 field findings).

Overnight A/B evidence: the server process's own CoreAudio stream silently
degrades ~60 s in (content permanently ~10.5 s stale, decode dies, ring
metrics stay green), while any fresh *process* — direct or spawn child —
captures the same device indefinitely without issue.  The poison lives in
the server-process environment and does not cross a process boundary, so
the capture seam runs in its own supervised subprocess, mirroring the DSP
Worker isolation (AD-003).

The child reuses :class:`AudioCapture` (int16 capture, ADC-timestamp
anchoring) verbatim and forwards every converted 12 kHz block as one
``(seq, epoch, payload)`` tuple over the control pipe.  The callback never
blocks on IPC: frames pass through a bounded deque (oldest dropped when
the parent falls behind — stale audio is worthless for decode and blocking
the callback is how the original corruption starts), and a dedicated
sender thread owns the pipe.  The parent's reader thread writes blocks
into the parent-owned :class:`UtcRing` and runs the waterfall tap — which
also moves the waterfall FFT off the audio callback thread.  A watchdog
restarts a dead or stalled child with bounded backoff; every restart is a
guaranteed-fresh capture session.
"""

from __future__ import annotations

import collections
import logging
import multiprocessing as mp
import threading
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any

import numpy as np

from .audio_rx import AudioCapture, UtcRing

log = logging.getLogger("mrrc-ft8.capture")

STALL_TIMEOUT_S = 10.0  # spawn imports take seconds; generous grace
WATCHDOG_PERIOD_S = 1.0
STOP_TIMEOUT_S = 5.0
PENDING_FRAMES = 256  # ~21 s of audio; oldest dropped, callback never blocks

Frame = tuple[int, float, bytes]  # seq, block epoch, int16 payload


def capture_child_main(
    conn: Connection,
    *,
    device: int | str | None = None,
    channel: int = 0,
    stream_factory: Callable[..., object] | None = None,
) -> None:
    """Child entry point: capture and forward every converted block.

    Runs until the pipe dies or the parent terminates the process.
    ``stream_factory`` is injectable so tests stay hardware-free.
    """

    pending: collections.deque[tuple[float, bytes]] = collections.deque(
        maxlen=PENDING_FRAMES
    )
    dead = threading.Event()

    def forward(samples: np.ndarray, epoch: float) -> None:
        pending.append((epoch, samples.tobytes()))

    def sender() -> None:
        seq = 0
        while not dead.is_set() or pending:
            try:
                epoch, payload = pending.popleft()
            except IndexError:
                dead.wait(0.05)
                continue
            seq += 1
            try:
                conn.send((seq, epoch, payload))
            except (OSError, EOFError, BrokenPipeError):
                dead.set()
                return

    # The child never reads its own ring; AudioCapture simply requires one.
    capture = AudioCapture(
        UtcRing(), device=device, channel=channel, stream_factory=stream_factory, tap=forward
    )
    capture.start()
    thread = threading.Thread(target=sender, name="capture-sender", daemon=True)
    thread.start()
    try:
        while not dead.is_set():
            if conn.poll(0.5):
                conn.recv()  # no commands; parent close raises EOFError below
    except (EOFError, OSError):
        dead.set()
    finally:
        capture.stop()
        thread.join(STOP_TIMEOUT_S)


class CaptureProcess:
    """Parent-side supervisor for the isolated capture child.

    ``start()`` spawns the child and the reader/watchdog threads;
    ``restart()`` swaps in a fresh child (new CoreAudio session);
    ``stop()`` is idempotent and always leaves the device released.
    """

    def __init__(
        self,
        ring: UtcRing,
        *,
        device: int | str | None = None,
        channel: int = 0,
        tap: Callable[[np.ndarray, float], None] | None = None,
        child_target: Callable[..., None] = capture_child_main,
        stall_timeout: float = STALL_TIMEOUT_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if stall_timeout <= 0:
            raise ValueError("stall timeout must be positive")
        self._ring = ring
        self._device = device
        self._channel = channel
        self._tap = tap
        self._child_target = child_target
        self._stall_timeout = stall_timeout
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._process: mp.Process | None = None
        self._conn: Connection | None = None
        self._threads: list[threading.Thread] = []
        self._last_block = 0.0
        self._generation = 0
        self.restart_count = 0

    @property
    def generation(self) -> int:
        """Current child generation; increments on every (re)start."""

        return self._generation

    @property
    def healthy(self) -> bool:
        """Child alive and producing blocks within the stall timeout.

        Locked against ``restart()``: an unlocked read could sample the
        teardown→spawn intermediate state (``_process is None`` for a few
        ms) and make the watchdog double-restart right behind a caller's
        own ``restart()`` (field finding 2026-08-05: proactive band-switch
        restart was immediately followed by a watchdog restart).
        """

        with self._lock:
            process = self._process
            return (
                process is not None
                and process.is_alive()
                and self._monotonic() - self._last_block <= self._stall_timeout
            )

    def start(self) -> None:
        """Spawn the first child and begin consuming frames."""

        with self._lock:
            if self._process is not None:
                raise ValueError("capture process already started")
            self._stop.clear()
            self._spawn_locked()
            for target, name in (
                (self._read_loop, "capture-reader"),
                (self._watchdog_loop, "capture-watchdog"),
            ):
                thread = threading.Thread(target=target, name=name, daemon=True)
                thread.start()
                self._threads.append(thread)

    def restart(self) -> None:
        """Swap in a fresh child; the ring sees a gap, never stale audio."""

        with self._lock:
            self.restart_count += 1
            self._teardown_locked()
            self._spawn_locked()
            self._last_block = self._monotonic()

    def stop(self) -> None:
        """Stop threads and terminate the child; idempotent."""

        self._stop.set()
        with self._lock:
            self._teardown_locked()
        for thread in self._threads:
            thread.join(STOP_TIMEOUT_S)
        self._threads.clear()

    # ---- internals ------------------------------------------------------

    def _spawn_locked(self) -> None:
        context = mp.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        self._generation += 1
        process = context.Process(
            target=self._child_target,
            name=f"mrrc-capture-g{self._generation}",
            args=(child,),
            kwargs={"device": self._device, "channel": self._channel},
            daemon=True,
        )
        try:
            process.start()
        except Exception:
            parent.close()
            child.close()
            raise
        child.close()
        self._process = process
        self._conn = parent
        self._last_block = self._monotonic()

    def _teardown_locked(self) -> None:
        process, conn = self._process, self._conn
        self._process = None
        self._conn = None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        if process is not None:
            if process.is_alive():
                process.terminate()
                process.join(STOP_TIMEOUT_S)
                if process.is_alive():
                    process.kill()
                    process.join(STOP_TIMEOUT_S)
            else:
                process.join(STOP_TIMEOUT_S)

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            conn = self._conn
            if conn is None:
                self._stop.wait(0.1)
                continue
            try:
                if not conn.poll(0.5):
                    continue
                item: Frame = conn.recv()
            except (EOFError, OSError):
                self._stop.wait(0.1)
                continue
            _seq, epoch, payload = item
            self._last_block = self._monotonic()
            samples = np.frombuffer(payload, dtype="<i2")
            self._ring.write(samples, epoch)
            if self._tap is not None:
                self._tap(samples, epoch)

    def _watchdog_loop(self) -> None:
        backoff = WATCHDOG_PERIOD_S
        while not self._stop.wait(backoff):
            if self.healthy:
                backoff = WATCHDOG_PERIOD_S
                continue
            process = self._process
            log.warning(
                "capture child unhealthy (alive=%s, silent %.1f s); restarting",
                process is not None and process.is_alive(),
                self._monotonic() - self._last_block,
            )
            try:
                self.restart()
            except Exception:
                log.exception("capture child restart failed")
            backoff = min(backoff * 2, 5.0)

    # ``Any`` keeps the composition root duck-typed like AudioCapture.
    def __enter__(self) -> Any:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
