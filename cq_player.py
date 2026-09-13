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
        self._task: Optional[asyncio.Task] = None
        self._state = "idle"
        self._sent = 0
        self._started_by: Optional[str] = None
        self._owner_token: Optional[str] = None
        self._reason_code: Optional[str] = None

    # ── asset ──────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def unavailable_reason(self) -> str:
        return self._reason

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
        except Exception as exc:                          # any failure = refusal
            self._frames = []
            self._ready = False
            self._duration_s = 0.0
            self._reason = f"CQ asset unusable: {exc}"
            logger.warning("%s", self._reason)
            return False
        self._frames = frames
        self._duration_s = len(frames) * _TICK_S
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
        """Stop an in-flight call and release the carrier (no graceful drain)."""
        task, self._task = self._task, None
        if task is not None:
            if not task.done():
                task.cancel()
            # gather() awaits the cancelled task without re-raising its
            # CancelledError — abort itself must always complete.
            for result in await asyncio.gather(task, return_exceptions=True):
                if isinstance(result, Exception):
                    logger.warning("CQ playback task ended with %s", result)
        if self._state == "calling":
            await self._release(False)
            self._state = "aborted"
            self._reason_code = reason
            logger.info("CQ aborted (%s) after %d/%d frames",
                        reason, self._sent, len(self._frames))
            await self._broadcast()

    async def _run(self) -> None:
        """Feed one 20 ms frame per tick while the device queue is shallow."""
        while self._sent < len(self._frames):
            if self._is_transmitting is not None and not self._is_transmitting():
                # Watchdog, TUNE or another client released the carrier.
                await self._finish("aborted", "unkeyed", graceful=False)
                return
            if self._audio is None or \
                    self._audio.tx_queue_frames() <= CQ_LOW_WATER_FRAMES:
                if self._audio is not None:
                    self._audio.feed_tx_audio(self._frames[self._sent])
                self._sent += 1
                if self._sent % 50 == 0:                  # 1 Hz progress
                    await self._broadcast()
            await asyncio.sleep(_TICK_S)
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
            "frames_total": len(self._frames),
            "frames_sent": self._sent,
            "started_by": self._started_by,
            "reason": self._reason_code,
            "ready": self._ready,
        }
