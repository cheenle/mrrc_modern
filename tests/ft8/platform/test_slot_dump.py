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
