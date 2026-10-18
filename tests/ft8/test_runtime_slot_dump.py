"""The runtime must be able to hand out the exact bytes it decoded.

The dump's module has its own tests; this one is here because the hook is what
can be wired wrong: the runtime has two ways to serve a slot (the complete one
and the zero-padded partial that early dispatch uses), and only dumping the
first would silently capture a different input than production decodes — the
recall measurement would then be answering a question nobody asked.
"""
from __future__ import annotations

import asyncio

import numpy as np

from tests.ft8.test_runtime_cascade_wiring import (
    _FakeRig,
    _FakeSequencer,
    _FakeSupervisor,
    _FakeTxPlayer,
)

import ft8.runtime as runtime_mod

SLOT_ID = 100
SLOT_SAMPLES = 180_000


def _runtime(monkeypatch, tmp_path, **env):
    monkeypatch.setattr(runtime_mod, "WorkerSupervisor", _FakeSupervisor)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return runtime_mod.Ft8Runtime(
        rig=_FakeRig(),
        tx_player=_FakeTxPlayer(),
        sequencer_factory=_FakeSequencer,
        data_dir=tmp_path,
    )


def _fill_one_slot(runtime) -> bytes:
    """Write one whole slot into the ring and return what a reader sees."""

    samples = np.arange(SLOT_SAMPLES, dtype="<i2")
    runtime.ring.write(samples, SLOT_ID * 15.0)
    raw = runtime.orchestrator._slot_source(SLOT_ID)
    assert raw is not None
    return raw


def test_the_decoded_slot_lands_in_the_dump_directory(monkeypatch, tmp_path) -> None:
    dump = tmp_path / "slots"
    dump.mkdir()
    runtime = _runtime(monkeypatch, tmp_path, MRRC_FT8_SLOT_DUMP_DIR=str(dump))

    async def _run() -> None:
        await runtime.start()
        try:
            raw = _fill_one_slot(runtime)
        finally:
            await runtime.stop()
        return raw

    raw = asyncio.run(_run())

    files = list(dump.iterdir())
    assert [p.name for p in files] == [f"slot_{SLOT_ID:012d}.raw"]
    assert files[0].read_bytes() == raw, "the dump must be the bytes the decoder got"


def test_without_the_variable_nothing_is_written(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("MRRC_FT8_SLOT_DUMP_DIR", raising=False)
    runtime = _runtime(monkeypatch, tmp_path)

    async def _run() -> None:
        await runtime.start()
        try:
            assert runtime._slot_dump is None
            _fill_one_slot(runtime)
        finally:
            await runtime.stop()

    asyncio.run(_run())

    assert list(tmp_path.rglob("*.raw")) == []
