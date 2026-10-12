"""TX sink: the FT8 waveform's way into the platform's TX path.

``PlatformTxPlayer`` is injected into the engine's safety controller in place
of MRRC-FT8's local-soundcard ``TxPlayer``.  It feeds the 48 kHz float32
waveform into ``AudioHandler.feed_tx_audio`` (the codec domain the platform's
CQ player already uses) as 20 ms int16 frames, paced by the TX queue depth.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

import numpy as np

from ft8.engine.audio_tx import PlayResult, validate_tx_buffer

log = logging.getLogger("mrrc.ft8.tx")

CODEC_RATE = 48_000
FRAME_SAMPLES = 960                      # 20 ms at 48 kHz — same frame as CQ
FRAME_SECONDS = FRAME_SAMPLES / CODEC_RATE
LOW_WATER_FRAMES = 4                     # keep at most this much queued
PACING_TIMEOUT_S = 2.0


class PlatformTxPlayer:
    """Play one bounded waveform through the platform TX queue."""

    def __init__(self, audio, *, queued_frames: Callable[[], int],
                 low_water_frames: int = LOW_WATER_FRAMES) -> None:
        self._audio = audio
        self._queued = queued_frames
        self._low_water = max(1, int(low_water_frames))
        self._cancelled = False

    def cancel(self) -> None:
        """Abort any active playback; synchronous and idempotent.

        ``safety.stop_tx`` calls this from the priority STOP path (§15.3):
        it must never block and never raise, so it only raises a flag the
        feed loop observes.  Releasing PTT / clearing the device queue is
        the TX gate's job (unkey), which runs right after.
        """

        self._cancelled = True

    async def play(self, samples: np.ndarray) -> PlayResult:
        self._cancelled = False          # a new transmission starts clean
        pcm = validate_tx_buffer(samples)
        pcm16 = np.clip(pcm, -1.0, 1.0)
        pcm16 = (pcm16 * 32767.0).astype("<i2").tobytes()
        started = time.monotonic()
        try:
            offset = 0
            deadline = started + len(pcm) / CODEC_RATE + PACING_TIMEOUT_S
            while offset < len(pcm16) and not self._cancelled:
                while self._queued() > self._low_water and not self._cancelled:
                    if time.monotonic() > deadline:
                        break
                    await asyncio.sleep(0.005)
                self._audio.feed_tx_audio(pcm16[offset:offset + FRAME_SAMPLES * 2])
                offset += FRAME_SAMPLES * 2
            # Let the device drain what is still queued before returning.
            remaining = (len(pcm) / CODEC_RATE) - (time.monotonic() - started)
            if remaining > 0:
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            log.info("FT8 TX cancelled after %d/%d frames",
                     offset // (FRAME_SAMPLES * 2), len(pcm16) // (FRAME_SAMPLES * 2))
            return PlayResult(samples_written=0, cancelled=True,
                              elapsed_seconds=time.monotonic() - started)
        if self._cancelled:
            log.info("FT8 TX cancelled after %d/%d frames",
                     offset // (FRAME_SAMPLES * 2), len(pcm16) // (FRAME_SAMPLES * 2))
            return PlayResult(samples_written=0, cancelled=True,
                              elapsed_seconds=time.monotonic() - started)
        elapsed = time.monotonic() - started
        log.info("FT8 TX fed %d frames (%d samples, %.2f s) to the TX queue in %.2f s",
                 len(pcm16) // (FRAME_SAMPLES * 2), int(pcm.size),
                 len(pcm) / CODEC_RATE, elapsed)
        return PlayResult(samples_written=int(pcm.size), cancelled=False,
                          elapsed_seconds=elapsed)
