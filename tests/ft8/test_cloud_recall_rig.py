"""The rig's arithmetic is what decides whether the cloud channel survives.

Only the pure parts are tested here — loading, differencing and the summary —
because those are the parts that can be wrong in a way nobody notices: a rig
that quietly compares the wrong two sets still prints a number, and that number
is the entire justification for the channel.  The decode step itself needs the
native library and is exercised end to end in the plan's task 10.

``tools/`` is not a package on purpose; the module is loaded by path, the same
way ``tests/ft8/test_i9_field_twin.py`` loads the field twin.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "ft8" / "cloud_recall_rig.py"


def _load_rig():
    assert TOOL.is_file(), f"the rig moved: {TOOL}"
    spec = importlib.util.spec_from_file_location("mrrc_cloud_recall_rig", TOOL)
    module = importlib.util.module_from_spec(spec)
    # Required, not decoration: dataclasses resolves ``cls.__module__`` through
    # sys.modules, so a module loaded by path without this line dies with
    # "'NoneType' object has no attribute '__dict__'" the moment it declares one.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_slot_file_that_is_not_one_slot_is_refused(tmp_path) -> None:
    rig = _load_rig()
    path = tmp_path / "slot_000000000001.raw"
    path.write_bytes(bytes(359_998))
    with pytest.raises(ValueError, match="360000"):
        rig.load_slot(path)


def test_a_slot_file_round_trips_with_its_id(tmp_path) -> None:
    rig = _load_rig()
    path = tmp_path / "slot_000000000031234.raw"
    path.write_bytes(bytes(360_000))
    slot_id, samples = rig.load_slot(path)
    assert slot_id == 31_234
    assert len(samples) == 360_000


def test_the_diff_is_per_slot_and_names_both_directions() -> None:
    rig = _load_rig()
    gained, lost = rig.diff({"CQ AAA", "CQ BBB"}, {"CQ BBB", "CQ CCC"})
    assert gained == {"CQ CCC"}
    assert lost == {"CQ AAA"}


def test_the_summary_prints_the_net_gain_that_decides_the_channel() -> None:
    """The last three lines are the go/no-go test (design §12), so they are fixed.

    Two numbers, because they answer different questions: how many more decodes
    the operator would see (each one a chance to answer), and how many distinct
    messages they were made of.
    """

    rig = _load_rig()
    rows = [
        rig.SlotRun(slot_id=1, local=("CQ AAA",), hub_same=("CQ AAA",),
                    hub_deep=("CQ AAA", "CQ CCC")),
        rig.SlotRun(slot_id=2, local=("CQ DDD",), hub_same=("CQ DDD",),
                    hub_deep=("CQ DDD",)),
    ]
    text = rig.summarize(rows)

    assert "slots: 2" in text
    assert "net gain (hub same depth vs local): 0 decodes / 0 distinct" in text
    assert "net gain (deep vs local): 1 decodes / 1 distinct" in text


def test_a_station_heard_in_three_slots_counts_three_decodes_but_one_message() -> None:
    """A repeated CQ is three chances to answer and one new callsign."""

    rig = _load_rig()
    rows = [
        rig.SlotRun(slot_id=1, local=(), hub_same=(), hub_deep=("CQ AAA",)),
        rig.SlotRun(slot_id=2, local=(), hub_same=(), hub_deep=("CQ AAA",)),
    ]
    text = rig.summarize(rows)
    assert "net gain (deep vs local): 2 decodes / 1 distinct" in text
