"""The runtime's cascade wiring: the orchestrator must decode through it.

Task 11's review ran four mutations.  Mutation (b) reverted
``Orchestrator(self._cascade, …)`` back to ``Orchestrator(self.decoder, …)``
— the cascade built and never used, every slot falling back to the deep path
— and the whole suite stayed green (709 passed).  Nothing turned red because
no test ever built a real ``Ft8Runtime``: the only wiring evidence,
``tests/ft8/test_server_wiring.py:165``, is a source-substring assertion and
pins that the *cutoff constant* reaches the call, not that ``_cascade``
reaches the orchestrator.

This module closes exactly that hole.  It drives the real ``start()`` path —
the same one the server uses to bring FT8 up — and pins the single object the
orchestrator holds for decoding.  Reverting the wiring makes it an
``AssertionError`` (``SupervisorDecoder`` is not ``CascadingDecoder``);
leaving it wired passes.

The fakes below serve only this file, so they live here rather than in the
shared wiring module or a conftest.
"""
from __future__ import annotations

import asyncio

from ft8 import runtime as runtime_mod
from ft8.engine.cascade import CascadePolicy
from ft8.engine.sequencer import QSOState


class _FakeSupervisor:
    """Stand-in for the DSP Worker supervisor.

    ``ft8/runtime.py`` hard-codes ``WorkerSupervisor()`` in ``start()``, so
    monkeypatching the module attribute is the only injection point.  Both
    ``start`` and ``stop`` are invoked through ``asyncio.to_thread(...)`` and
    must therefore be plain synchronous methods — an ``async def`` would hand
    ``to_thread`` a coroutine object that is never awaited.
    """

    def __init__(self, *args, **kwargs) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class _FakeRig:
    """``SafetyController.start()`` touches the rig only to drop PTT."""

    def __init__(self) -> None:
        self.ptt = True  # start() must drive it false

    async def set_ptt(self, transmit: bool) -> None:
        self.ptt = bool(transmit)


class _FakeTxPlayer:
    """``SafetyController`` calls ``cancel()`` on the stop path; nothing else."""

    def __init__(self) -> None:
        self.cancelled = 0

    def cancel(self) -> None:
        self.cancelled += 1


class _FakeSequencer:
    """Enough for ``SafetyController.start()`` (``stop``) and the cascade's
    ``ap_context`` (``my_call`` / ``state``)."""

    def __init__(self) -> None:
        self.my_call = ""
        self.state = QSOState.IDLE

    def stop(self, *args, **kwargs) -> None:
        pass


def test_the_orchestrator_actually_gets_the_cascade(monkeypatch, tmp_path) -> None:
    """The cascade must reach the orchestrator through the real ``start()``.

    Mutation (b): change ``Orchestrator(self._cascade, …)`` back to
    ``Orchestrator(self.decoder, …)`` and this test must go red.  The Task 11
    review measured that without it, that mutation left the full suite at
    ``709 passed`` with no failure.  ``start()`` spins up the real
    ``Orchestrator`` with the real ``_cascade``, so the assertion below is the
    only thing standing between "the cascade exists" and "the cascade is
    used"; the orchestrator holds its decoder privately as ``_decoder``
    (``orchestrator.py``, used in ``_slot_ended``).
    """

    monkeypatch.setattr(runtime_mod, "WorkerSupervisor", _FakeSupervisor)

    rt = runtime_mod.Ft8Runtime(
        rig=_FakeRig(),
        tx_player=_FakeTxPlayer(),
        sequencer_factory=_FakeSequencer,
        data_dir=tmp_path,
    )

    async def _run() -> None:
        await rt.start()
        try:
            assert rt.orchestrator._decoder is rt._cascade, (
                "the orchestrator must decode through the CascadingDecoder, "
                "not the raw SupervisorDecoder"
            )
        finally:
            # start() leaves an orchestrator task running; cancel it here so
            # it cannot leak into the rest of the session.
            await rt.stop()

    asyncio.run(_run())


def test_start_builds_the_cascade_from_the_injected_policy(monkeypatch, tmp_path) -> None:
    """必须 1: the provider's policy object must reach the live cascade.

    Mutation: revert ``policy = self._cascade_policy_provider() …`` back to
    ``policy = CascadePolicy()`` and this must go red.  Without the fix a
    persisted threshold change applies only until the next engine start, then
    silently reverts to the defaults — settings the operator can see but not
    feel.
    """

    monkeypatch.setattr(runtime_mod, "WorkerSupervisor", _FakeSupervisor)
    sentinel = CascadePolicy(escalate_on_weak=False, min_peak_snr=-18, min_decodes=4)

    rt = runtime_mod.Ft8Runtime(
        rig=_FakeRig(),
        tx_player=_FakeTxPlayer(),
        sequencer_factory=_FakeSequencer,
        data_dir=tmp_path,
        cascade_policy_provider=lambda: sentinel,
    )

    async def _run() -> None:
        await rt.start()
        try:
            assert rt._cascade._policy is sentinel, (
                "start() must build the cascade from cascade_policy_provider, "
                "not from the hard-coded CascadePolicy() defaults"
            )
        finally:
            await rt.stop()

    asyncio.run(_run())


def test_constructing_the_runtime_creates_a_fresh_data_dir(tmp_path) -> None:
    """A machine that has never run FT8 has no <runtime>/ft8/ — and sqlite
    cannot create missing parents, so Repository's first connect raised
    ``OperationalError: unable to open database file`` and took the whole FT8
    composition down with it (server.py catches that and runs SSB-only).

    Found by the macOS clean-room run of the packaged app on 2026-10-13: the
    box never showed it because M5's manual acceptance had already created the
    directory.  Building the runtime must therefore create its own data dir,
    not depend on Ft8SettingsStore.save() having run at some point.
    """

    data_dir = tmp_path / "never-used-before" / "ft8"
    assert not data_dir.exists()

    runtime_mod.Ft8Runtime(
        rig=_FakeRig(),
        tx_player=_FakeTxPlayer(),
        sequencer_factory=_FakeSequencer,
        data_dir=data_dir,
    )

    assert (data_dir / "qso.db").is_file(), "the repository must be usable"


def _cloud_runtime(monkeypatch, tmp_path, **env):
    """This file's own construction shape, plus a controlled cloud environment."""

    monkeypatch.setattr(runtime_mod, "WorkerSupervisor", _FakeSupervisor)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return runtime_mod.Ft8Runtime(
        rig=_FakeRig(),
        tx_player=_FakeTxPlayer(),
        sequencer_factory=_FakeSequencer,
        data_dir=tmp_path,
    )


def test_a_configured_hub_reaches_the_live_cascade(monkeypatch, tmp_path) -> None:
    runtime = _cloud_runtime(
        monkeypatch, tmp_path,
        MRRC_FT8_CLOUD_DECODE_URL="https://www.vlsc.net/mrrc-decode",
        MRRC_FT8_CLOUD_DECODE_TOKEN="tok",
    )

    async def _run() -> None:
        await runtime.start()
        try:
            assert runtime._cascade._remote is not None
            assert runtime._cascade._cloud_deadline_s == 2.5
        finally:
            await runtime.stop()

    asyncio.run(_run())


def test_no_url_means_no_cloud_path(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("MRRC_FT8_CLOUD_DECODE_URL", raising=False)
    runtime = _cloud_runtime(monkeypatch, tmp_path)

    async def _run() -> None:
        await runtime.start()
        try:
            assert runtime._cascade._remote is None
        finally:
            await runtime.stop()

    asyncio.run(_run())


def test_the_cascade_stats_carry_the_cloud_counters(monkeypatch, tmp_path) -> None:
    runtime = _cloud_runtime(monkeypatch, tmp_path)

    async def _run() -> None:
        await runtime.start()
        try:
            stats = runtime.cascade_stats          # a property, not a method
            assert {"cloud_ok", "cloud_fallback", "cloud_seconds"} <= set(stats)
        finally:
            await runtime.stop()

    asyncio.run(_run())
