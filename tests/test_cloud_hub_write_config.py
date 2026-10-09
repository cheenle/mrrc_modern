"""cloud_hub._write_config is the Cloud Hub's door into the env file.

It was one of the seven writers, and it carried two of the defects: it dropped
every comment, and its temp file did not inherit the mode of the file it
replaced. On the box that file holds the web password at 0640, so the first
Cloud Hub connect widened it to 0644 — and nothing about the file looked
different afterwards, which is why it survived a field report.

It now delegates to env_store.update_env_file; these tests hold the call site to
what that writer promises.
"""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

import cloud_hub


class WriteConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "mrrc.env"

    def tearDown(self):
        self._tmp.cleanup()

    @unittest.skipIf(os.name == "nt", "POSIX file modes; Windows has no 0640")
    def test_the_mode_of_the_file_survives_the_write(self):
        """The defect, at the call site rather than one layer down."""
        self.path.write_text("MRRC_WEB_PASSWORD=secret\n", encoding="utf-8")
        os.chmod(self.path, 0o640)
        cloud_hub._write_config(self.path, {"MRRC_WEB_PORT": "8888"})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

    def test_an_operators_comment_survives_the_write(self):
        """This file is edited by hand on the box. A writer that silently
        deletes the explanation beside a setting makes the next edit harder."""
        self.path.write_text("# 改这个之前先看 docs/W103D_GUIDE.md\nMRRC_WEB_PORT=8888\n",
                             encoding="utf-8")
        cloud_hub._write_config(self.path, {"MRRC_WEB_PORT": "9000"})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("改这个之前先看", text)
        self.assertIn("MRRC_WEB_PORT=9000", text)

    def test_a_key_it_does_not_own_is_left_alone(self):
        self.path.write_text("MRRC_WEB_PASSWORD=keepme\nMRRC_WEB_PORT=8888\n",
                             encoding="utf-8")
        cloud_hub._write_config(self.path, {"MRRC_WEB_PORT": "9000"})
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("MRRC_WEB_PASSWORD=keepme", text)
        self.assertIn("MRRC_WEB_PORT=9000", text)

    def test_it_still_creates_a_missing_file(self):
        cloud_hub._write_config(self.path, {"MRRC_WEB_PORT": "8888"})
        self.assertIn("MRRC_WEB_PORT=8888", self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
