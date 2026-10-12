"""TX sink: the FT8 waveform's way into the platform's TX path.

``PlatformTxPlayer`` is injected into the engine's safety controller in place
of MRRC-FT8's local-soundcard ``TxPlayer``.  It feeds the 48 kHz float32
waveform into ``AudioHandler.feed_tx_audio`` (the codec domain the platform's
CQ player already uses) as 20 ms int16 frames, paced by the TX queue depth.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import numpy as np

from ft8.engine.audio_tx import PlayResult, validate_tx_buffer

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

    async def play(self, samples: np.ndarray) -> PlayResult:
        pcm = validate_tx_buffer(samples)
        pcm16 = np.clip(pcm, -1.0, 1.0)
        pcm16 = (pcm16 * 32767.0).astype("<i2").tobytes()
        started = time.monotonic()
        try:
            offset = 0
            deadline = started + len(pcm) / CODEC_RATE + PACING_TIMEOUT_S
            while offset < len(pcm16):
                while self._queued() > self._low_water:
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
            return PlayResult(samples_written=0, cancelled=True,
                              elapsed_seconds=time.monotonic() - started)
        return PlayResult(samples_written=int(pcm.size), cancelled=False,
                          elapsed_seconds=time.monotonic() - started)
