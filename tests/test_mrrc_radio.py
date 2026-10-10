"""Switching the radio model: the write, the gate, and what must not move.

Everything here runs without a radio. `plan()` is pure, so the interesting
assertions are about what ends up in the env file — in particular that
switching models cannot carry a stale serial port over (the IC-7300 answers on
ttyACM0 while the FT-710 needs one of two ttyUSB nodes) and cannot carry an
opened transmit gate over.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

from linux import mrrc_radio

REPO = Path(__file__).resolve().parents[1]


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "profiles").mkdir()
        (self.dir / "profiles" / "ft710.env").write_text(
            "# comment\nMRRC_RADIO_MODEL=ft710\n", encoding="utf-8"
        )
        (self.dir / "profiles" / "ftdx10.env").write_text(
            "MRRC_RADIO_MODEL=ftdx10\nMRRC_ALLOW_UNVERIFIED_TX=0\n", encoding="utf-8"
        )
        self.profiles = self.dir / "profiles"

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_explicit_port_is_written(self):
        out = mrrc_radio.plan(
            "ft710", {"MRRC_RADIO_MODEL": "ic7300"}, "/dev/ttyUSB1", self.profiles
        )
        self.assertEqual(out["MRRC_SERIAL_PORT"], "/dev/ttyUSB1")

    def test_changing_model_without_a_port_clears_the_old_one(self):
        """A stale port is a silently dead CAT link, not a cosmetic bug."""
        out = mrrc_radio.plan(
            "ft710",
            {"MRRC_RADIO_MODEL": "ic7300", "MRRC_SERIAL_PORT": "/dev/ttyACM0"},
            None,
            self.profiles,
        )
        self.assertEqual(out["MRRC_SERIAL_PORT"], "")

    def test_same_model_without_a_port_leaves_the_port_alone(self):
        out = mrrc_radio.plan(
            "ft710",
            {"MRRC_RADIO_MODEL": "ft710", "MRRC_SERIAL_PORT": "/dev/ttyUSB0"},
            None,
            self.profiles,
        )
        self.assertNotIn("MRRC_SERIAL_PORT", out)

    def test_the_gate_is_reasserted_from_the_profile_not_inherited(self):
        """Switching away from an opened model must not leave the next one open."""
        out = mrrc_radio.plan(
            "ft710", {"MRRC_ALLOW_UNVERIFIED_TX": "1"}, None, self.profiles
        )
        self.assertEqual(out["MRRC_ALLOW_UNVERIFIED_TX"], "0")

    def test_an_unverified_profile_is_accepted_and_keeps_its_zero(self):
        out = mrrc_radio.plan("ftdx10", {}, None, self.profiles)
        self.assertEqual(out["MRRC_ALLOW_UNVERIFIED_TX"], "0")

    def test_an_unknown_model_is_refused(self):
        with self.assertRaises(ValueError):
            mrrc_radio.plan("ft999", {}, None, self.profiles)

    def test_a_missing_profile_is_refused(self):
        with self.assertRaises(FileNotFoundError):
            mrrc_radio.plan("ic7300", {}, None, self.profiles)


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / "mrrc.env"
        self.env.write_text(
            "# operator's file\nMRRC_WEB_PASSWORD=secret\nMRRC_RADIO_MODEL=ic7300\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_write_preserves_everything_it_does_not_own(self):
        mrrc_radio.apply(self.env, {"MRRC_RADIO_MODEL": "ft710"})
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("MRRC_WEB_PASSWORD=secret", text)
        self.assertIn("MRRC_RADIO_MODEL=ft710", text)
        self.assertNotIn("MRRC_RADIO_MODEL=ic7300", text)

    def test_a_backup_is_left_behind(self):
        mrrc_radio.apply(self.env, {"MRRC_RADIO_MODEL": "ft710"})
        self.assertTrue(self.env.with_suffix(".env.bak").is_file())

    def test_a_dry_run_writes_nothing(self):
        before = self.env.read_text(encoding="utf-8")
        mrrc_radio.apply(self.env, {"MRRC_RADIO_MODEL": "ft710"}, dry_run=True)
        self.assertEqual(self.env.read_text(encoding="utf-8"), before)


class InstalledCopyTests(unittest.TestCase):
    """On the box the CLI is a copy at /usr/local/bin/mrrc-radio.

    ``parents[1]`` of that path is /usr/local — not a tree — so the module has
    to take the tree from MRRC_HOME (the variable linux/mrrc_update.sh already
    reads) and find the profiles where the overlay puts them, at
    ``<MRRC_HOME>/profiles`` (packaging/ is excluded from the image, so the
    checkout's ``packaging/box/profiles`` path cannot survive). The 1.25.4 image
    shipped with neither: the installed CLI died on ``import backends``.

    Each test stages the image's tree shape under a temporary root, so it fails
    for the same reason the box does rather than because of where the test node
    happens to live.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = (root / "opt" / "mrrc_modern").resolve()
        self.bin = root / "usr" / "local" / "bin"
        self.bin.mkdir(parents=True)
        self.home.mkdir(parents=True)
        for path in REPO.glob("*.py"):
            shutil.copy(path, self.home / path.name)
        shutil.copytree(REPO / "backends", self.home / "backends",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.home / "linux").mkdir()
        shutil.copy(REPO / "linux" / "first_run.py",
                    self.home / "linux" / "first_run.py")
        shutil.copytree(REPO / "packaging" / "box" / "profiles",
                        self.home / "profiles")
        shutil.copy(REPO / "linux" / "mrrc_radio.py", self.bin / "mrrc-radio")
        self._path = list(sys.path)

    def tearDown(self):
        sys.path[:] = self._path
        self.tmp.cleanup()

    def _load(self):
        """Import the copy the way the box runs it: outside its source tree."""
        with mock.patch.dict(os.environ, {"MRRC_HOME": str(self.home)}):
            target = self.bin / "mrrc-radio"
            loader = SourceFileLoader("mrrc_radio_installed", str(target))
            spec = importlib.util.spec_from_file_location(
                "mrrc_radio_installed", target, loader=loader)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        return mod

    def test_the_installed_copy_resolves_the_image_tree(self):
        mod = self._load()
        self.assertEqual(mod.REPO, self.home)
        self.assertTrue((mod.REPO / "backends").is_dir(),
                        "REPO must be a tree that carries backends/")
        self.assertEqual(mod.PROFILE_DIR, self.home / "profiles")
        self.assertEqual(len(list(mod.PROFILE_DIR.glob("*.env"))), 11)

    def test_the_installed_copy_reads_a_profile(self):
        mod = self._load()
        self.assertEqual(mod.read_profile("ft710")["MRRC_RADIO_MODEL"], "ft710")

    def test_a_checkout_still_uses_the_checkout_layout(self):
        self.assertEqual(mrrc_radio.REPO, REPO)
        self.assertEqual(mrrc_radio.PROFILE_DIR,
                         REPO / "packaging" / "box" / "profiles")
