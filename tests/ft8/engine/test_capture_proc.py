from __future__ import annotations

import multiprocessing as mp
import threading
import time
from types import SimpleNamespace

import numpy as np

from ft8.engine.audio_rx import DECODER_SAMPLE_RATE, RX_SAMPLE_RATE, UtcRing
from ft8.engine.capture_proc import CaptureProcess, capture_child_main


class FakeStream:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        pass


def test_child_forwards_converted_blocks() -> None:
    parent, child = mp.Pipe(duplex=True)
    created: list[FakeStream] = []

    def factory(**kwargs: object) -> FakeStream:
        stream = FakeStream(**kwargs)
        created.append(stream)
        return stream

    thread = threading.Thread(
        target=capture_child_main,
        args=(child,),
        kwargs={"device": None, "stream_factory": factory},
        daemon=True,
    )
    thread.start()
    for _ in range(50):
        if created:
            break
        time.sleep(0.05)
    stream = created[0]
    block = np.zeros((48_000, 1), dtype="<i2")
    stream.callback(block, 48_000, None, None)
    stream.callback(block, 48_000, None, None)

    first = parent.recv()
    second = parent.recv()
    assert first[0] == 1 and second[0] == 2  # seq increments
    assert isinstance(first[1], float)
    payload = np.frombuffer(first[2], dtype="<i2")
    assert payload.size == RX_SAMPLE_RATE // (RX_SAMPLE_RATE // DECODER_SAMPLE_RATE)
    parent.close()
    thread.join(5)
    assert not thread.is_alive()


def _frame_child(conn, *, device: object = None, channel: int = 0) -> None:
    samples = (np.ones(12_000, dtype="<i2") * 100).tobytes()
    for index in range(16):
        conn.send((index + 1, float(index), samples))
        time.sleep(0.02)
    try:
        while True:
            if conn.poll(1.0):
                conn.recv()
    except (EOFError, OSError):
        pass


def _dying_child(conn, *, device: object = None) -> None:
    return


def test_parent_consumes_frames_into_ring_and_tap() -> None:
    ring = UtcRing(seconds=60.0)
    tapped: list[tuple[int, float]] = []
    process = CaptureProcess(
        ring,
        device=None,
        tap=lambda samples, epoch: tapped.append((samples.size, epoch)),
        child_target=_frame_child,
    )
    process.start()
    try:
        deadline = time.monotonic() + 20
        while ring.read_slot(0) is None and time.monotonic() < deadline:
            time.sleep(0.2)
        assert ring.read_slot(0) is not None
        assert tapped and tapped[0][0] == 12_000
        assert process.healthy
    finally:
        process.stop()
        process.stop()  # idempotent


def test_watchdog_restarts_dead_child() -> None:
    ring = UtcRing(seconds=60.0)
    process = CaptureProcess(ring, device=None, child_target=_dying_child, stall_timeout=0.5)
    process.start()
    try:
        deadline = time.monotonic() + 20
        while process.generation < 2 and time.monotonic() < deadline:
            time.sleep(0.2)
        assert process.generation >= 2
        assert process.restart_count >= 1
    finally:
        process.stop()
