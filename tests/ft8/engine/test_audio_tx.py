"""Regressions for the bounded 48 kHz TX playback seam (§11.2)."""

from __future__ import annotations

import asyncio
import threading

import numpy as np
import pytest

from ft8.engine.audio_tx import (
    MAX_TX_SAMPLES,
    TX_SAMPLE_RATE,
    TxAudioError,
    TxPlayer,
)


class FakeOutputStream:
    """sounddevice.OutputStream stand-in with blocking/failure injection."""

    instances: list[FakeOutputStream] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.blocks: list[np.ndarray] = []
        self.started = False
        self.stopped = False
        self.closed = False
        self.aborted = False
        self.gate: threading.Event | None = None  # when set, write waits on it
        self.write_started = threading.Event()
        self.fail_with: Exception | None = None
        FakeOutputStream.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def abort(self) -> None:
        self.aborted = True
        if self.gate is not None:
            self.gate.set()

    def write(self, block: np.ndarray) -> None:
        self.write_started.set()
        if self.fail_with is not None:
            raise self.fail_with
        if self.gate is not None:
            self.gate.wait(timeout=5.0)
        self.blocks.append(block.copy())


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


async def wait_stream() -> FakeOutputStream:
    """Yield until the playback task has constructed its stream."""

    for _ in range(1_000):
        await asyncio.sleep(0)
        if FakeOutputStream.instances:
            return FakeOutputStream.instances[0]
    raise AssertionError("stream was never created")


@pytest.fixture(autouse=True)
def _reset_instances() -> None:
    FakeOutputStream.instances = []


def make_player(blocksize: int = 4_096) -> TxPlayer:
    return TxPlayer(stream_factory=FakeOutputStream, blocksize=blocksize)


def test_play_writes_every_block_and_closes() -> None:
    async def main() -> None:
        player = make_player()
        pcm = np.arange(10_000, dtype=np.float32)
        result = await player.play(pcm)
        stream = FakeOutputStream.instances[0]
        assert result.samples_written == 10_000
        assert not result.cancelled
        assert result.elapsed_seconds >= 0.0
        assert np.concatenate([b[:, 0] for b in stream.blocks]).tolist() == pcm.tolist()
        assert stream.started and stream.stopped and stream.closed
        assert stream.kwargs["samplerate"] == TX_SAMPLE_RATE
        assert stream.kwargs["channels"] == 1
        assert stream.kwargs["dtype"] == "float32"
        assert not player.playing

    run(main())


@pytest.mark.parametrize(
    "samples",
    [
        np.zeros(100, dtype=np.int16),               # wrong dtype
        np.zeros((100, 1), dtype=np.float32),        # not 1-D
        np.zeros(0, dtype=np.float32),               # empty
        np.zeros(MAX_TX_SAMPLES + 1, dtype=np.float32),  # beyond one waveform
    ],
)
def test_invalid_buffers_are_rejected_before_opening_a_stream(
    samples: np.ndarray,
) -> None:
    async def main() -> None:
        with pytest.raises(ValueError):
            await make_player().play(samples)

    run(main())
    assert FakeOutputStream.instances == []


def test_cancel_aborts_a_blocked_playback() -> None:
    async def main() -> None:
        gate = threading.Event()

        def factory(**kwargs: object) -> FakeOutputStream:
            stream = FakeOutputStream(**kwargs)
            stream.gate = gate
            return stream

        player = TxPlayer(stream_factory=factory)
        task = asyncio.create_task(player.play(np.zeros(8_192, dtype=np.float32)))
        stream = await wait_stream()
        for _ in range(1_000):
            await asyncio.sleep(0)
            if stream.write_started.is_set():
                break
        assert stream.write_started.is_set()
        player.cancel()
        result = await asyncio.wait_for(task, timeout=2.0)
        assert result.cancelled
        assert stream.aborted and stream.stopped and stream.closed
        assert not player.playing

    run(main())


def test_device_loss_raises_tx_audio_error_and_closes() -> None:
    async def main() -> None:
        def factory(**kwargs: object) -> FakeOutputStream:
            stream = FakeOutputStream(**kwargs)
            stream.fail_with = OSError("device unplugged")
            return stream

        player = TxPlayer(stream_factory=factory)
        with pytest.raises(TxAudioError, match="device unplugged"):
            await player.play(np.zeros(4_096, dtype=np.float32))
        stream = FakeOutputStream.instances[0]
        assert stream.closed
        assert not player.playing

    run(main())


def test_second_play_is_rejected_while_one_is_active() -> None:
    async def main() -> None:
        gate = threading.Event()

        def factory(**kwargs: object) -> FakeOutputStream:
            stream = FakeOutputStream(**kwargs)
            stream.gate = gate
            return stream

        player = TxPlayer(stream_factory=factory)
        task = asyncio.create_task(player.play(np.zeros(4_096, dtype=np.float32)))
        stream = await wait_stream()
        for _ in range(1_000):
            await asyncio.sleep(0)
            if stream.write_started.is_set():
                break
        with pytest.raises(RuntimeError, match="already in progress"):
            await player.play(np.zeros(4_096, dtype=np.float32))
        player.cancel()
        await asyncio.wait_for(task, timeout=2.0)

    run(main())


def test_cancel_when_idle_is_a_noop() -> None:
    player = make_player()
    player.cancel()
    player.cancel()
    assert not player.playing
    assert FakeOutputStream.instances == []


def test_write_failure_after_cancel_is_normal_cancellation() -> None:
    """Real PortAudio can fail the blocked write when abort() lands (FT-710
    acceptance: PaErrorCode -9986); that is cancellation, not device loss."""

    class AbortRaisesStream(FakeOutputStream):
        def write(self, block: np.ndarray) -> None:
            self.write_started.set()
            if self.gate is not None:
                self.gate.wait(timeout=5.0)
            if self.aborted:
                raise OSError("Internal PortAudio error")
            self.blocks.append(block.copy())

    async def main() -> None:
        gate = threading.Event()

        def factory(**kwargs: object) -> AbortRaisesStream:
            stream = AbortRaisesStream(**kwargs)
            stream.gate = gate
            return stream

        player = TxPlayer(stream_factory=factory)
        task = asyncio.create_task(player.play(np.zeros(8_192, dtype=np.float32)))
        stream = await wait_stream()
        for _ in range(1_000):
            await asyncio.sleep(0)
            if stream.write_started.is_set():
                break
        player.cancel()
        result = await asyncio.wait_for(task, timeout=2.0)
        assert result.cancelled
        assert stream.aborted and stream.closed

    run(main())

def test_txplayer_construction_is_lazy_on_sounddevice(monkeypatch) -> None:
    """构造 TxPlayer 不得导入 sounddevice：CoreAudio 枚举卡死时 server 仍能启动。

    2026-08-10 现场：FT8 聚合设备引用未加电的 FT-710，``import sounddevice``
    在 PortAudio 枚举处挂起；若 TxPlayer.__init__ 急切导入，create_server 永远
    卡在 uvicorn.run 之前。
    """

    import builtins

    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "sounddevice":
            raise ImportError("blocked sounddevice import (CoreAudio wedged)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    player = TxPlayer()  # must not raise
    assert player.playing is False
