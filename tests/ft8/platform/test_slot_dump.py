"""The slot dump exists for exactly one reason: to give the recall rig real slots.

Two properties matter more than the file format.

* It is **off by default** — a diagnostic that writes 360 KB per slot must
  never appear because someone forgot to unset something.
* It captures **what the decoder was handed**, including the zero padding the
  ring adds when a slot is dispatched early (the FT8 waveform ends 12.64 s into
  the 15 s slot, so early dispatch is the design).  A "cleaner" copy taken later
  would measure a different input than production decodes.
"""
from __future__ import annotations

from ft8.platform.slot_dump import SlotDumper

SLOT = bytes(range(256)) * 1406 + bytes(64)  # 360_000 bytes, non-uniform


def test_no_directory_means_no_dump() -> None:
    assert SlotDumper.from_environ({}) is None
    assert SlotDumper.from_environ({"MRRC_FT8_SLOT_DUMP_DIR": "   "}) is None


def test_each_slot_lands_once_with_its_id_in_the_name(tmp_path) -> None:
    dumper = SlotDumper.from_environ({"MRRC_FT8_SLOT_DUMP_DIR": str(tmp_path)})
    assert dumper is not None

    dumper.write(31_234, SLOT)

    files = sorted(tmp_path.iterdir())
    assert [p.name for p in files] == ["slot_000000031234.raw"]
    assert files[0].read_bytes() == SLOT


def test_the_first_write_for_a_slot_wins(tmp_path) -> None:
    """A later poll must not replace the bytes the decoder actually received."""

    dumper = SlotDumper.from_environ({"MRRC_FT8_SLOT_DUMP_DIR": str(tmp_path)})
    assert dumper is not None

    dumper.write(7, SLOT)
    dumper.write(7, bytes(360_000))

    assert (tmp_path / "slot_000000000007.raw").read_bytes() == SLOT


def test_a_write_failure_never_raises_into_the_decode_path(tmp_path) -> None:
    # A *file* stands in for the directory: writing into it must fail, and the
    # assertion holds for root too, unlike a read-only directory.
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    dumper = SlotDumper.from_environ({"MRRC_FT8_SLOT_DUMP_DIR": str(blocker)})
    assert dumper is not None

    dumper.write(1, SLOT)  # must not raise

    assert blocker.read_text() == "x"


def test_the_dump_keeps_only_the_newest_files(tmp_path) -> None:
    """A research dump must not be able to fill the device it runs on.

    15 s per slot at 360 KB is 2 GB a day; the box accumulated 1.3 GB in seven
    hours before this existed.  The cap is checked by writing past it and asking
    which files survived, not by trusting the arithmetic.
    """

    dumper = SlotDumper(tmp_path, keep=12)
    for slot_id in range(1, 32):
        dumper.write(slot_id, b"x" * 16)
        if slot_id % 10 == 0:
            dumper._prune()
    dumper._prune()
    remaining = sorted(path.name for path in tmp_path.glob("slot_*.raw"))
    assert len(remaining) == 12
    assert remaining[-1] == f"slot_{31:012d}.raw"
    assert remaining[0] == f"slot_{20:012d}.raw"


def test_keep_is_configurable_and_never_zero(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MRRC_FT8_SLOT_DUMP_DIR", str(tmp_path))
    monkeypatch.setenv("MRRC_FT8_SLOT_DUMP_KEEP", "7")
    assert SlotDumper.from_environ(__import__("os").environ)._keep == 7
    monkeypatch.setenv("MRRC_FT8_SLOT_DUMP_KEEP", "0")
    assert SlotDumper.from_environ(__import__("os").environ)._keep == 1
