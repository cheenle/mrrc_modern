"""TX sink: the FT8 waveform's way into the platform's TX path."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from ft8.engine.audio_tx import MAX_TX_SAMPLES
from ft8.platform.tx_sink import PlatformTxPlayer


class FakeAudio:
    def __init__(self):
        self.frames: list[bytes] = []

    def tx_queue_frames(self) -> int:
        return 0

    def feed_tx_audio(self, pcm: bytes) -> None:
        self.frames.append(pcm)


def test_play_converts_float32_to_int16_20ms_frames():
    audio = FakeAudio()
    player = PlatformTxPlayer(audio, queued_frames=lambda: audio.tx_queue_frames())
    wave = np.zeros(960 * 3, dtype=np.float32)
    wave[0] = 1.0
    result = asyncio.run(player.play(wave))
    assert all(len(f) == 960 * 2 for f in audio.frames)      # 20 ms @48k int16
    assert len(audio.frames) == 3
    first = np.frombuffer(audio.frames[0], dtype="<i2")
    assert first[0] == 32767
    assert result.samples_written == wave.size
    assert not result.cancelled


def test_play_rejects_wrong_shape():
    player = PlatformTxPlayer(FakeAudio(), queued_frames=lambda: 0)
    with pytest.raises(ValueError):
        asyncio.run(player.play(np.zeros(MAX_TX_SAMPLES + 1, dtype=np.float32)))


def test_play_respects_queue_water_level():
    """Feeding must pause while the device queue is above the low-water mark."""

    class GatedAudio(FakeAudio):
        def __init__(self):
            super().__init__()
            self.depth = 0

        def tx_queue_frames(self) -> int:
            return self.depth

        def feed_tx_audio(self, pcm: bytes) -> None:
            self.depth = 8                      # pretend the queue filled up
            super().feed_tx_audio(pcm)

    audio = GatedAudio()
    player = PlatformTxPlayer(audio, queued_frames=lambda: audio.tx_queue_frames(),
                              low_water_frames=2)

    async def run():
        task = asyncio.create_task(player.play(np.zeros(960 * 4, dtype=np.float32)))
        await asyncio.sleep(0.05)
        drained = len(audio.frames)
        audio.depth = 0                          # drain, let it finish
        result = await task
        return drained, result

    drained, result = asyncio.run(run())
    assert drained >= 1
    assert result.samples_written == 960 * 4
