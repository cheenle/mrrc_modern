"""Server-side CQ call playback (spec 2026-09-13 §4).

One call = one transmission: the caller supplies key/unkey callbacks (server.py
owns the TX gate, ownership and drain sequence), this module feeds the packaged
recording into the existing device-domain TX queue and reports its state.

The asset is normalised once at load time: mono, 16-bit, 48 kHz (the codec
domain `AudioHandler.feed_tx_audio` expects), sliced into 20 ms frames.
"""
from __future__ import annotations

import asyncio
import logging
import wave
from pathlib import Path
from typing import Awaitable, Callable, Optional

import numpy as np

from audio_resample import resample_pcm

logger = logging.getLogger("mrrc.cq")

CODEC_RATE = 48_000
FRAME_SAMPLES = 960                       # 20 ms at 48 kHz — same frame as Opus TX
CQ_MAX_SECONDS = 30.0
CQ_LOW_WATER_FRAMES = 4                   # feed while the device queue is this shallow
_TICK_S = FRAME_SAMPLES / CODEC_RATE
# While the queue is deep the pump has nothing to do.  Poll it at 500 Hz
# instead of `sleep(0)`: a device that stays behind for a while (a sound card
# recovering from an xrun) must not turn the pump into a busy loop.
_BACKPRESSURE_POLL_S = 0.002


class CQUnavailable(Exception):
    """A call cannot start; ``str(exc)`` is operator-facing text."""


class CQPlayer:
    """Loads one CQ asset and plays it once per call."""

    def __init__(self, *, asset_path: Optional[Path] = None,
                 max_seconds: float = CQ_MAX_SECONDS,
                 audio=None,
                 key: Optional[Callable[[Optional[str]], Awaitable[bool]]] = None,
                 unkey: Optional[Callable[[bool], Awaitable[None]]] = None,
                 is_transmitting: Optional[Callable[[], bool]] = None,
                 on_change: Optional[Callable[[dict], Awaitable[None]]] = None):
        self._asset_path = asset_path
        try:
            self._max_seconds = float(max_seconds)
        except (TypeError, ValueError):        # a bad env override must not
            self._max_seconds = CQ_MAX_SECONDS  # take the server down
        self._audio = audio
        self._key = key
        self._unkey = unkey
        self._is_transmitting = is_transmitting
        self._on_change = on_change
        self._frames: list[bytes] = []
        self._ready = False
        self._reason = "asset not loaded"
        self._duration_s = 0.0
        self._peak_dbfs = 0.0
        self._task: Optional[asyncio.Task] = None
        self._state = "idle"
        self._sent = 0
        self._started_by: Optional[str] = None
        self._owner_token: Optional[str] = None
        self._reason_code: Optional[str] = None

    # ── asset ──────────────────────────────────────────────────────

    @property
    def asset_path(self) -> Optional[Path]:
        """The file this player reads; ``None`` until one is set."""
        return Path(self._asset_path) if self._asset_path else None

    def set_asset(self, path: Optional[Path]) -> None:
        """Point the player at another file; the next ``load()`` reads it.

        The CQ-recording screen swaps between the operator's take and the shipped
        asset without restarting the server, so this must not touch the frames.
        """
        self._asset_path = Path(path) if path else None

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def unavailable_reason(self) -> str:
        return self._reason

    @property
    def frames(self) -> int:
        """Number of 20 ms frames in the loaded asset (0 when not loaded)."""
        return len(self._frames)

    @property
    def duration_s(self) -> float:
        """Playback length of the loaded asset in seconds."""
        return round(len(self._frames) * _TICK_S, 3)

    def load(self) -> bool:
        """Read + normalise the asset. False (with a reason) on any failure."""
        path = self._asset_path
        try:
            if path is None or not Path(path).is_file():
                raise ValueError(f"CQ asset not found: {path}")
            with wave.open(str(path), "rb") as w:
                width = w.getsampwidth()
                channels = w.getnchannels()
                rate = w.getframerate()
                raw = w.readframes(w.getnframes())
            if width != 2:
                raise ValueError(
                    f"CQ asset must be 16-bit PCM (got {width * 8}-bit)")
            if rate <= 0 or channels < 1:
                raise ValueError("CQ asset has an invalid rate/channel count")
            samples = np.frombuffer(raw, dtype="<i2")
            if channels > 1:
                samples = samples.reshape(-1, channels).mean(axis=1)
            samples = np.ascontiguousarray(samples, dtype="<i2")
            if rate != CODEC_RATE:
                pcm = resample_pcm(samples.tobytes(), rate, CODEC_RATE)
                samples = np.frombuffer(pcm, dtype="<i2")
            duration = samples.size / CODEC_RATE
            if duration <= 0:
                raise ValueError("CQ asset is empty")
            if duration > self._max_seconds:
                raise ValueError(
                    f"CQ asset too long: {duration:.1f}s > "
                    f"{self._max_seconds:.0f}s limit")
            frames: list[bytes] = []
            for start in range(0, samples.size, FRAME_SAMPLES):
                chunk = samples[start:start + FRAME_SAMPLES]
                if chunk.size < FRAME_SAMPLES:            # zero-pad the tail
                    chunk = np.concatenate([
                        chunk,
                        np.zeros(FRAME_SAMPLES - chunk.size, dtype="<i2")])
                frames.append(np.ascontiguousarray(chunk, dtype="<i2").tobytes())
            # Inside the guard on purpose: a malformed asset must fail into
            # "CQ asset unusable" (load() returns False, the key is disabled and
            # the reason is logged) rather than escape into startup.
            peak = int(np.abs(samples.astype(np.int32)).max()) if samples.size else 0
        except Exception as exc:                          # any failure = refusal
            self._frames = []
            self._ready = False
            self._duration_s = 0.0
            self._peak_dbfs = 0.0
            self._reason = f"CQ asset unusable: {exc}"
            logger.warning("%s", self._reason)
            return False
        self._frames = frames
        self._duration_s = len(frames) * _TICK_S
        self._peak_dbfs = round(20.0 * np.log10(peak / 32768.0), 1) if peak else 0.0
        self._ready = True
        self._reason = ""
        logger.info("CQ ready: %s (%.1f s, 48 kHz mono, %d frames)",
                    path, self._duration_s, len(frames))
        return True

    # ── lifecycle ──────────────────────────────────────────────────

    @property
    def is_calling(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _release(self, graceful: bool) -> None:
        """Unkey through the injected callback (absent in asset-only tests)."""
        if self._unkey is not None:
            await self._unkey(graceful)

    async def wait_finished(self) -> None:
        """Await the in-flight call (tests + shutdown)."""
        if self._task is not None:
            await asyncio.shield(self._task)

    async def start(self, *, started_by: str,
                    owner_token: Optional[str] = None) -> None:
        """Key the radio and play the asset once. Raises CQUnavailable."""
        if not self._ready:
            raise CQUnavailable(self._reason or "CQ asset not ready")
        if self.is_calling:
            raise CQUnavailable("CQ already in progress")
        if self._key is None or not await self._key(owner_token):
            raise CQUnavailable("CQ could not key the radio")
        self._state = "calling"
        self._sent = 0
        self._started_by = started_by
        self._owner_token = owner_token
        self._reason_code = None
        self._task = asyncio.create_task(self._run(), name="cq_player")
        await self._broadcast()

    async def abort(self, reason: str = "aborted_by_user") -> None:
        """Stop an in-flight call and release the carrier (no graceful drain).

        The release is **not** conditional on the state alone.  `_finish()` writes
        the state before it awaits the graceful drain, so a cancel landing inside
        that await sees "complete" and would otherwise skip its own release — a
        keyed radio with nobody left to send TX0.  `_release` is idempotent, so
        releasing whenever we just cancelled a live task costs one no-op PTT
        write and closes that window (disconnects and double presses land there).
        """
        task, self._task = self._task, None
        in_flight = task is not None and not task.done()
        if task is not None:
            if not task.done():
                task.cancel()
            # gather() awaits the cancelled task without re-raising its
            # CancelledError — abort itself must always complete.
            for result in await asyncio.gather(task, return_exceptions=True):
                if isinstance(result, Exception):
                    logger.warning("CQ playback task ended with %s", result)
        if self._state == "calling" or in_flight:
            await self._release(False)
            self._state = "aborted"
            self._reason_code = reason
            logger.info("CQ aborted (%s) after %d/%d frames",
                        reason, self._sent, len(self._frames))
            await self._broadcast()

    async def _run(self) -> None:
        """Feed one 20 ms frame per due tick while the device queue is shallow.

        The cadence comes from the wall clock, **not** from a per-tick counter.
        ``asyncio.sleep(0.02)`` only promises "at least 20 ms": the loop body
        (~0.9 ms of numpy resampling and queue bookkeeping) is not subtracted
        from it, so a counter-based pump feeds one 20 ms frame every **20.87 ms**
        (measured: 20 calls, 20.8–21.1 ms/frame on the W103D; reproduced on a
        mac at 20.996 ms with an empty loop body).  The sound card's 44.1 kHz
        codec clock is not negotiable — it eats one 882-sample frame every
        20.000 ms — and this pump deliberately keeps only ``CQ_LOW_WATER_FRAMES``
        (80 ms) queued, so the device ran dry roughly two seconds into every
        call: ~1 ms of silence inserted every 21 ms, which the operator hears as
        a rasp on the voice (2026-10-29 field report, "发射 CQ 的时候声音有些
        失真"; the 10.48 s asset keyed the radio for 11.00 s, and 524 frames ×
        0.87 ms = 0.46 s of the difference was exactly that missing audio).

        Two things keep the rate right: frames are scheduled against the wall
        clock (``t0 + k * _TICK_S``, so one late tick is repaid by the next),
        and the queue is refilled **to** ``CQ_LOW_WATER_FRAMES`` rather than by
        a single frame — which makes the codec's own clock the authority and
        leaves the Python timer with nothing to drift against.
        """
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        while self._sent < len(self._frames):
            if self._is_transmitting is not None and not self._is_transmitting():
                # Watchdog, TUNE or another client released the carrier.
                await self._finish("aborted", "unkeyed", graceful=False)
                return
            fed = False
            if self._audio is None:
                self._sent += 1
                fed = True
            else:
                # Top the queue up **to** the low-water mark, not by one frame.
                # Pacing off the clock alone still leaves the deficit to the
                # timer's granularity: on Windows `asyncio.sleep(0.02)` wakes
                # ~31 ms late and no amount of debt repayment can fix that, so
                # the pump measured 20.64 ms/frame there (macOS/Linux: 20.01).
                # Refilling to the mark makes the **sound card** the only clock:
                # the queue can never be fuller than the mark, so the average
                # rate is the codec's 20.000 ms by construction.
                # Bounded so a device whose depth never grows cannot spin here.
                budget = CQ_LOW_WATER_FRAMES + 2
                depth = self._audio.tx_queue_frames()
                while (self._sent < len(self._frames) and budget > 0
                       and depth <= CQ_LOW_WATER_FRAMES):
                    self._audio.feed_tx_audio(self._frames[self._sent])
                    self._sent += 1
                    fed = True
                    budget -= 1
                    if self._sent % 50 == 0:              # 1 Hz progress
                        await self._broadcast()
                    depth = self._audio.tx_queue_frames()
            delay = t0 + self._sent * _TICK_S - loop.time()
            if delay <= 0.0 and not fed:
                delay = _BACKPRESSURE_POLL_S              # device behind: poll
            await asyncio.sleep(delay if delay > 0.0 else 0.0)
        await self._finish("complete", None, graceful=True)

    async def _finish(self, state: str, reason: Optional[str], *,
                      graceful: bool) -> None:
        self._state = state
        self._reason_code = reason
        await self._release(graceful)
        logger.info("CQ %s after %d/%d frames (%.1fs)",
                    state, self._sent, len(self._frames), self._sent * _TICK_S)
        await self._broadcast()

    async def _broadcast(self) -> None:
        if self._on_change is not None:
            await self._on_change(self.status())

    # ── status ─────────────────────────────────────────────────────

    def status(self) -> dict:
        """Snapshot for the ``cqState`` broadcast and ``fullState``."""
        return {
            "state": self._state,
            "duration_s": round(self._duration_s, 2) if self._ready else 0.0,
            "elapsed_s": round(self._sent * _TICK_S, 2) if self.is_calling else 0.0,
            "peak_dbfs": self._peak_dbfs if self._ready else None,
            "frames_total": len(self._frames),
            "frames_sent": self._sent,
            "started_by": self._started_by,
            "reason": self._reason_code,
            "ready": self._ready,
        }
