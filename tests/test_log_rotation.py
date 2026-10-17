"""Nothing an image writes may grow without a ceiling (2026-10-17).

A shipped box is unattended for months, so "the log will not get that big" is
not a design — it is an assumption with a deadline. The measured case: a defect
that logged one warning per 20 ms audio frame wrote 17 MB in five hours on a
6.5 GB root (W103D, 2026-10-17). Nothing about that mechanism is self-limiting;
a smaller defect is only slower.

Two of the ceilings live in the application and are tested where they are
implemented (``server.log``'s RotatingFileHandler, ``ft8-worker.log``'s
rotate-at-2 MB). The third — ``server-stdout.log``, the file systemd appends the
server's stdout/stderr to — has no ceiling it can express itself, so it is owned
by the service layer, and this is what keeps that ownership from being dropped
while someone edits a unit file.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMMON = REPO / "packaging" / "common"
SERVICE_LAYER = COMMON / "mrrc-service-layer.sh"
PACKAGES = COMMON / "packages.txt"


class _ServiceLayerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SERVICE_LAYER.read_text(encoding="utf-8")

    def find(self, pattern: str, where: str):
        match = re.search(pattern, self.text, re.M)
        self.assertIsNotNone(match, where)
        return match


class StdoutLogRotationTests(_ServiceLayerCase):
    """The file the unit appends to must be the file logrotate rotates."""

    def _append_target(self) -> str:
        match = self.find(r"^StandardOutput=append:\$MRRC_HOME/(\S+)\s*$",
                          "the unit no longer appends the server's stdout to a file — "
                          "if that changed on purpose, the rotation below has to "
                          "change with it")
        return match.group(1)

    def _rotated_path(self) -> str:
        match = self.find(r"^\$MRRC_HOME/(\S+) \{\s*$", "no logrotate stanza was written")
        return match.group(1)

    def test_the_rotated_file_is_the_unit_append_target(self):
        """A rename on one side of this pair is silent: the file grows forever.

        Neither `append:` nor logrotate complains about a path they no longer
        share — the config simply matches nothing, and `missingok` swallows even
        that.
        """
        self.assertEqual(self._append_target(), self._rotated_path())

    def test_rotation_truncates_in_place(self):
        """copytruncate, because systemd holds the descriptor open.

        With a plain rename the writer keeps appending to the unlinked inode: the
        directory looks rotated while the disk keeps filling, which is worse than
        no rotation at all because it also hides the growth.
        """
        start = self.text.index("logrotate.d/mrrc")
        stanza = self.text[start:start + 900]
        self.assertIn("copytruncate", stanza)

    def test_every_generation_is_bounded(self):
        """A cap without a size trigger and a rotation count is not a cap."""
        self.find(r"^\tmaxsize \d+M$", "the rotation has no size trigger")
        self.find(r"^\trotate [1-9]\d*$", "the rotation keeps an unbounded generation count")
        self.find(r"^\tcompress$", "old generations are not compressed")

    def test_the_scheduler_is_ours(self):
        """Not the distro's: a masked cron or a missing timer is invisible here.

        The service layer used to have no rotation at all — Debian's cron.daily
        hook would have run it if a config had existed. Relying on that is what
        made the gap undetectable, so the unit we install is the one that must be
        enabled.
        """
        self.find(r"^\[Timer\]$", "no timer unit is installed")
        self.assertIn("mrrc-logrotate.timer", self.text)
        self.assertIn("/usr/sbin/logrotate", self.text)
        self.assertIn("/etc/logrotate.d/mrrc", self.text)
        self.find(r"^systemctl enable .*mrrc-logrotate\.timer",
                  "the rotation timer is installed but never enabled")

    def test_logrotate_is_installed_by_the_shared_package_list(self):
        """The timer execs a binary; on a bare base that binary has to exist."""
        packages = [
            line.strip() for line in PACKAGES.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertIn("logrotate", packages)


class JournaldCeilingTests(_ServiceLayerCase):
    def test_journald_is_capped(self):
        """Default is 10% of the filesystem — a sixth of this box's disk.

        Every unit but the server logs to the journal, so it is the second
        largest unbounded growth on a device that must survive months of
        unattended operation.
        """
        self.assertIn("journald.conf.d", self.text)
        self.find(r"^SystemMaxUse=\d+M$", "the journal has no size cap")
        self.find(r"^SystemKeepFree=\d+M$",
                  "the journal cannot yield space back when the disk gets tight")


if __name__ == "__main__":
    unittest.main()
