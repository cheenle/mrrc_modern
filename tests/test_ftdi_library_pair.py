"""The FTDI pair must reach the FT4222 chip through **one** D2XX instance.

Field incident, 2026-11-10 (W103D box MRRC-BG1SB + FT-710): the real spectrum
vanished and every ``scope_pipe`` start logged
``spi_init_retry:attempt_N:error_1000`` → ``fatal:cannot_open_device``, while
the waterfall kept showing the S-meter synthesiser. ``FT_OpenEx`` succeeded; it
was ``FT4222_SPIMaster_Init`` that answered 1000 — ``FT4222_DEVICE_NOT_SUPPORTED``,
"this device is not a FT4222 chip".

Cause: ``vendor/ftdi/libftd2xx.so`` was a **copy** of ``libft4222.so`` instead
of the symlink the tree ships (``packaging/box/vendor-ftdi.md``). FTDI's Linux
build statically links libftd2xx and re-exports its symbols, so two paths give
``dlopen`` two objects with two D2XX device tables: ``FT_OpenEx`` built the
handle in one, ``FT4222_SPIMaster_Init`` looked for it in the other. A symlink
resolves to the same inode and is loaded once — which is why the tree ships it
that way, and why the pipe that was already running (its libraries mapped
before the copy appeared) kept streaming until the next restart.

These tests pin the classification of a pair, the loader's single-instance
decision, and the tree's own invariant. No hardware, no FT-710: the loader
tests only need the vendored library file to be loadable on this host.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from backends.ft710.scope_libraries import (
    FtdiLibraries,
    classify_ftdi_pair,
    load_ftdi_libraries,
)

REPO = Path(__file__).resolve().parents[1]
VENDOR = REPO / "vendor" / "ftdi"

#: The Linux/box pair. Its second slot is a symlink **by design** — pinned by
#: tests/test_box_profiles.py::VendoredFtdiTests and packaging/box/vendor-ftdi.md.
LINUX_LIB = VENDOR / "libft4222.so"
LINUX_D2XX = VENDOR / "libftd2xx.so"

#: Desktop pairs: two genuinely different files (the FT4222 image links the
#: D2XX image), so the "same file" rule does not apply to them.
DESKTOP_PAIR = (REPO / "lib" / "libft4222.dylib", REPO / "lib" / "libftd2xx.dylib")


def _loadable_pair() -> tuple[Path, Path]:
    """The first vendored pair this host can actually dlopen, or skip."""
    candidates = [(LINUX_LIB, LINUX_D2XX), DESKTOP_PAIR,
                  (VENDOR / "macos" / "libft4222.dylib",
                   VENDOR / "macos" / "libftd2xx.dylib")]
    errors = []
    for lib, d2xx in candidates:
        if not lib.exists() or not d2xx.exists():
            continue
        try:
            import ctypes

            ctypes.CDLL(str(lib))
        except OSError as exc:  # e.g. an aarch64 ELF on macOS
            errors.append(f"{lib.name}: {exc}")
            continue
        return lib, d2xx
    raise unittest.SkipTest(
        "no vendored FTDI pair loadable here" + (f" ({errors[0]})" if errors else "")
    )


class PairClassificationTests(unittest.TestCase):
    """How the two slots relate — the fact the loader decides on."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.real = self.tmp / "libft4222.so"
        self.real.write_bytes(b"\x7fELF" + b"payload" * 64)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_symlink_is_one_file(self):
        link = self.tmp / "libftd2xx.so"
        link.symlink_to(self.real.name)
        self.assertEqual(classify_ftdi_pair(self.real, link), ("same-inode", ""))

    def test_a_copy_is_flagged_even_though_it_is_byte_identical(self):
        copy = self.tmp / "libftd2xx.so"
        copy.write_bytes(self.real.read_bytes())
        relationship, note = classify_ftdi_pair(self.real, copy)
        self.assertEqual(relationship, "same-content")
        self.assertIn("COPY", note)
        self.assertIn("symlink", note)

    def test_different_sizes_are_distinct(self):
        other = self.tmp / "libftd2xx.so"
        other.write_bytes(b"a genuinely different driver")
        self.assertEqual(classify_ftdi_pair(self.real, other), ("distinct", ""))

    def test_same_size_different_bytes_are_distinct(self):
        """Size alone would be a cheap-but-wrong test: compare the content."""
        other = self.tmp / "libftd2xx.so"
        other.write_bytes(b"\x7fELF" + b"pAyLOAD" * 64)
        self.assertEqual(other.stat().st_size, self.real.stat().st_size)
        self.assertEqual(classify_ftdi_pair(self.real, other), ("distinct", ""))

    def test_missing_metadata_is_reported_not_raised(self):
        relationship, note = classify_ftdi_pair(self.real, self.tmp / "nope.so")
        self.assertEqual(relationship, "unknown")
        self.assertIn("unreadable", note)


class LoaderSharesOneInstanceTests(unittest.TestCase):
    """The decision ``load_ftdi_libraries`` makes, on a real library file."""

    def test_the_shipped_pair_is_shared(self):
        lib, d2xx = _loadable_pair()
        # macOS keeps two real images (the FT4222 one links the D2XX one) and
        # the box keeps a single file under two names; either way FT_OpenEx and
        # FT4222_SPIMaster_Init must reach ONE D2XX device table.
        libs = load_ftdi_libraries(lib, d2xx)
        self.assertIsInstance(libs, FtdiLibraries)
        self.assertTrue(libs.shared, "FT_OpenEx and FT4222_SPIMaster_Init must "
                                     "reach the same D2XX device table")

    def test_a_copied_second_slot_still_shares_and_says_so(self):
        lib_path, _ = _loadable_pair()
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / lib_path.name.replace("ft4222", "ftd2xx")
            copy.write_bytes(lib_path.read_bytes())
            libs = load_ftdi_libraries(lib_path, copy)
            self.assertTrue(libs.shared)
            self.assertEqual(libs.relationship, "same-content")
            self.assertIn("COPY", libs.note)


class DesktopPairTests(unittest.TestCase):
    """The desktop pair is the *other* layout, and must stay that way.

    ``vendor/ftdi/libftd2xx.so`` being a symlink is already pinned by
    ``tests/test_box_profiles.py::VendoredFtdiTests`` (the invariant the image
    build gates on). macOS/Windows ship two genuinely different images instead:
    the FT4222 one links the D2XX one, which is why ``load_ftdi_libraries``
    must not require a single file — it only requires a single *instance*, and
    a linked dependency counts.
    """

    @unittest.skipUnless(sys.platform == "darwin", "desktop pair is macOS-only")
    def test_desktop_pair_keeps_two_real_images(self):
        if not DESKTOP_PAIR[0].exists():
            self.skipTest("desktop FTDI images not present in this checkout")
        self.assertFalse(
            DESKTOP_PAIR[1].is_symlink(),
            "the macOS D2XX image is a real driver, not a second name for the "
            "FT4222 image: symlinking them would need the same single-file "
            "build the Linux package uses",
        )


if __name__ == "__main__":
    unittest.main()
