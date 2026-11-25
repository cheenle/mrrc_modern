"""Publishing a Passport firmware: the merged layout, the dirty guard, the catalog.

The merge is what the box writes at offset 0, so a wrong offset here is a board
that does not boot.  These tests build a synthetic ESP-IDF build directory and
assert the byte layout rather than trusting `flash_args`.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev_tools"))

import make_passport_firmware as mk  # noqa: E402


class MergeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / "bootloader").mkdir()
        (self.dir / "partition_table").mkdir()
        (self.dir / "bootloader/bootloader.bin").write_bytes(b"\xe9" + b"B" * 31)
        (self.dir / "partition_table/partition-table.bin").write_bytes(b"\xaa\x50" + b"P" * 30)
        (self.dir / "mrrc-terminal.bin").write_bytes(b"\xe9" + b"A" * 63)
        (self.dir / "flash_args").write_text(
            "--flash_mode dio --flash_freq 80m --flash_size 8MB\n"
            "0x0 bootloader/bootloader.bin\n"
            "0x8000 partition_table/partition-table.bin\n"
            "0x10000 mrrc-terminal.bin\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _merge(self):
        text = (self.dir / "flash_args").read_text(encoding="utf-8")
        _opts, images = mk.parse_flash_args(text)
        return mk.merge_images(self.dir, images)

    def test_images_land_at_their_flash_offsets(self):
        blob = self._merge()
        self.assertEqual(blob[0x0], 0xE9)
        self.assertEqual(blob[0x8000:0x8002], b"\xaa\x50")
        self.assertEqual(blob[0x10000], 0xE9)

    def test_the_rest_is_erased_and_the_size_is_sector_aligned(self):
        blob = self._merge()
        self.assertEqual(len(blob) % 4096, 0)
        # The gap between the bootloader and the partition table stays 0xFF.
        self.assertEqual(blob[0x100:0x200], b"\xff" * 0x100)
        # And so does the NVS window the box writes separately.
        self.assertEqual(blob[0x9000:0x9100], b"\xff" * 0x100)

    def test_an_overlapping_placement_is_refused(self):
        images = {"0x0": "bootloader/bootloader.bin", "0x10": "mrrc-terminal.bin"}
        with self.assertRaises(mk.BuildError):
            mk.merge_images(self.dir, images)

    def test_a_missing_partition_table_is_refused(self):
        images = {"0x0": "bootloader/bootloader.bin", "0x10000": "mrrc-terminal.bin"}
        with self.assertRaises(mk.BuildError):
            mk.merge_images(self.dir, images)

    def test_flash_args_is_parsed_as_data(self):
        options, images = mk.parse_flash_args(
            '--flash_size 8MB\n0x0 bootloader/bootloader.bin\n')
        self.assertEqual(options["flash_size"], "8MB")
        self.assertEqual(images, {"0x0": "bootloader/bootloader.bin"})


class DirtyGuardTests(unittest.TestCase):
    def test_a_dirty_stamp_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mrrc-terminal.bin"
            path.write_bytes(b"\xe9" + b"\x00" * 40 + b"v1.2.1-13-g2a6cc82-dirty" + b"\x00" * 64)
            self.assertTrue(mk.build_is_dirty(Path(tmp)))

    def test_a_clean_build_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mrrc-terminal.bin"
            path.write_bytes(b"\xe9" + b"\x00" * 40 + b"v1.2.1" + b"\x00" * 64)
            self.assertFalse(mk.build_is_dirty(Path(tmp)))


class CatalogTests(unittest.TestCase):
    def _entry(self, version, size=10):
        return {"version": version, "chip": "esp32c3", "url": f"https://x/{version}",
                "size": size, "sha256": "0" * 64, "notes": "", "_filename": f"f-{version}.bin"}

    def test_versions_are_kept_newest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "firmware.json"
            for version in ("1.0.0", "1.10.0", "1.9.9"):
                mk.update_catalog(path, self._entry(version))
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([b["version"] for b in doc["builds"]],
                             ["1.10.0", "1.9.9", "1.0.0"])

    def test_republishing_a_version_replaces_it_instead_of_duplicating(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "firmware.json"
            mk.update_catalog(path, self._entry("1.2.1", size=10))
            mk.update_catalog(path, self._entry("1.2.1", size=99))
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(doc["builds"]), 1)
            self.assertEqual(doc["builds"][0]["size"], 99)

    def test_private_filename_never_reaches_the_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "firmware.json"
            mk.update_catalog(path, self._entry("1.2.1"))
            self.assertNotIn("_filename", path.read_text(encoding="utf-8"))

    def test_the_published_entry_is_accepted_by_the_box_parser(self):
        """The catalog and the box must agree without a shared module."""
        import passport_flash
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "firmware.json"
            blob = b"\xe9" * 16
            mk.update_catalog(path, mk.entry_for("1.2.1", blob, "notes"))
            builds = passport_flash.parse_catalog(path.read_text(encoding="utf-8"))
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0].version, "1.2.1")
        self.assertEqual(builds[0].size, 16)
        self.assertEqual(builds[0].sha256, mk.hashlib.sha256(blob).hexdigest())


if __name__ == "__main__":
    unittest.main()
