"""The log must be able to answer "how often did we escalate, and what fell late?"

The 2026-10-14 w103d deploy (Phase A) finally let the deep path run, and the
operator's first question — *how many slots escalated, how many crossed the
cutoff?* — turned out to be unanswerable from the log:

* ``cascade.py`` logs the **refusals** ("budget too tight") and nothing else, so
  a successful escalation leaves no trace at all.
* ``late`` existed only in the WebSocket frame (``_on_batch``'s
  ``frame["late"]``); the per-slot log line said ``%d message(s)`` and stopped.
* The only readable numbers were ``GET /api/ft8/engine/stats``, i.e. an
  authenticated HTTP call with a 30-day session cookie.

So the field diagnosis had to be done by *inference* from publish offsets, and
two such inferences were wrong before being retracted (the native-library
banner is not a deep-path marker; "offset > 2 s" is not an escalation marker —
empty slots are slow too, and the fast band itself drifts).

These tests pin the log as a self-sufficient source: the per-slot line names its
path and whether it escalated or fell late, and a cumulative counters line
appears every ``_COUNTERS_EVERY_SLOTS`` slots.

The fakes live here rather than in a shared module — same convention as
``test_runtime_cascade_wiring.py``, which they deliberately do not import (a
test module importing another test module makes one file's fakes load-bearing
for the other).
"""
from __future__ import annotations

import logging

from ft8 import runtime as runtime_mod
from ft8.core.models import DecodeBatch, DecodePath
from ft8.engine.orchestrator import OrchestratorCounters, SlotDecode
from ft8.engine.sequencer import QSOState


class _Rig:
    async def set_ptt(self, transmit: bool) -> None:
        pass


class _TxPlayer:
    def cancel(self) -> None:
        pass


class _Sequencer:
    """Only ``my_call`` / ``state`` are read on this path."""

    def __init__(self) -> None:
        self.my_call = ""
        self.state = QSOState.IDLE

    def stop(self, *args, **kwargs) -> None:
        pass


class _StubOrchestrator:
    def __init__(self, counters: OrchestratorCounters) -> None:
        self.counters = counters


def _runtime(tmp_path, counters: OrchestratorCounters | None = None):
    rt = runtime_mod.Ft8Runtime(
        rig=_Rig(),
        tx_player=_TxPlayer(),
        sequencer_factory=_Sequencer,
        data_dir=tmp_path,
    )
    if counters is not None:
        rt.orchestrator = _StubOrchestrator(counters)
    return rt


def _slot(slot_id: int = 100, *, late: bool = False,
          escalated: bool = False, path: DecodePath = DecodePath.FAST) -> SlotDecode:
    """A slot with **no** messages: keeps the cty/DXCC lookups out of the test."""

    return SlotDecode(
        slot_id=slot_id,
        dispatched_epoch=(slot_id + 1) * 15.0 + 0.2,
        finished_epoch=(slot_id + 1) * 15.0 + 0.4,
        late=late,
        batch=DecodeBatch(
            slot_id=slot_id,
            path=path,
            results=(),
            overflow=False,
            elapsed_seconds=0.4,
            escalated=escalated,
        ),
        messages=(),
    )


def _slot_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("slot ")]


def _markers(line: str) -> list[str]:
    """The bracketed flag tokens, split — never a substring search.

    ``"escalated"`` *contains* ``"late"``, so ``assert "late" not in line`` was
    green-by-accident in reverse: it failed on a correctly-escalated line.  The
    tokens must be compared whole.
    """

    return line[line.index("[") + 1:line.rindex("]")].split()


def _counter_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "decodes=" in r.getMessage()]


def test_a_slot_line_names_the_path_and_the_escalation(caplog, tmp_path) -> None:
    """Phase A's whole question — "is the deep path running?" — must be readable
    from one line.  Before this, a successful escalation was invisible and the
    operator had to infer it from publish offsets (and infer wrongly, twice)."""

    rt = _runtime(tmp_path, OrchestratorCounters())

    with caplog.at_level(logging.INFO, logger=runtime_mod.log.name):
        rt._on_batch(_slot(late=False, escalated=True, path=DecodePath.IMPROVED))

    markers = _markers(_slot_lines(caplog)[-1])
    assert "improved" in markers, markers
    assert "escalated" in markers, markers
    assert "late" not in markers, "a slot inside the cutoff must not be marked late"


def test_a_late_slot_is_marked_late_in_the_log(caplog, tmp_path) -> None:
    """``late`` was frame-only.  The slots Phase A made late are exactly the ones
    the sequencer may not use, so they must be greppable, not inferred."""

    rt = _runtime(tmp_path, OrchestratorCounters())

    with caplog.at_level(logging.INFO, logger=runtime_mod.log.name):
        rt._on_batch(_slot(late=True))

    markers = _markers(_slot_lines(caplog)[-1])
    assert "late" in markers, markers
    assert "fast" in markers, markers


def test_the_counters_are_logged_every_slots_marks(caplog, tmp_path) -> None:
    """The cumulative line is what Task 8's baseline harvests, and it is the
    replacement for the authenticated ``/api/ft8/engine/stats`` call."""

    counters = OrchestratorCounters(
        slots_started=20, decodes=20, fast_decodes=15,
        escalations=5, deadline_misses=2, decode_errors=1,
    )
    rt = _runtime(tmp_path, counters)
    every = runtime_mod._COUNTERS_EVERY_SLOTS

    with caplog.at_level(logging.INFO, logger=runtime_mod.log.name):
        for i in range(every):
            rt._on_batch(_slot(slot_id=1_000 + i))

    lines = _counter_lines(caplog)
    assert len(lines) == 1, f"expected exactly one summary after {every} slots: {lines}"
    line = lines[0]
    assert "decodes=20" in line, line
    assert "fast=15" in line, line
    assert "escalations=5" in line, line
    assert "late=2" in line, line
    assert "errors=1" in line, line


def test_the_slot_count_in_the_line_is_the_slots_the_engine_saw(caplog, tmp_path) -> None:
    """``after N slot(s)`` has to mean slots, not decodes.

    ``runtime._slots_logged`` counts ``_on_batch`` calls, and ``_on_batch`` only
    runs for a slot that produced a decode — a skipped slot (no ring data within
    the grace window, ``orchestrator.py:261``) or an errored one never gets
    there.  So the cadence counter is *definitionally* ``counters.decodes``, and
    printing it under the name "slot(s)" reports a number the operator cannot
    use to tell "the receiver heard nothing" from "the band was empty".  That is
    the one question this line was added to answer.

    The two numbers are made to differ here on purpose: if the line goes back to
    printing ``_slots_logged``, it prints 20 where the engine saw 23.
    """

    counters = OrchestratorCounters(
        slots_started=23, slots_skipped=3, decodes=20, fast_decodes=15,
        escalations=5, deadline_misses=0, decode_errors=0,
    )
    rt = _runtime(tmp_path, counters)
    every = runtime_mod._COUNTERS_EVERY_SLOTS

    with caplog.at_level(logging.INFO, logger=runtime_mod.log.name):
        for i in range(every):
            rt._on_batch(_slot(slot_id=3_000 + i))

    lines = _counter_lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    assert "after 23 slot(s)" in line, (
        f"the line says the wrong number of slots — the engine saw 23, and a "
        f"skipped slot is exactly what this number exists to reveal: {line}"
    )
    assert "skipped=3" in line, f"skipped slots are invisible: {line}"


def test_no_counters_line_before_the_interval(caplog, tmp_path) -> None:
    """The summary must not fire on every slot — that would be the per-slot line
    again, at a fifth of the readability."""

    rt = _runtime(tmp_path, OrchestratorCounters())
    every = runtime_mod._COUNTERS_EVERY_SLOTS

    with caplog.at_level(logging.INFO, logger=runtime_mod.log.name):
        for i in range(every - 1):
            rt._on_batch(_slot(slot_id=2_000 + i))

    assert _counter_lines(caplog) == []
