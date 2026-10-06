"""Switching the radio model: the write, the gate, and what must not move.

Everything here runs without a radio. `plan()` is pure, so the interesting
assertions are about what ends up in the env file — in particular that
switching models cannot carry a stale serial port over (the IC-7300 answers on
ttyACM0 while the FT-710 needs one of two ttyUSB nodes) and cannot carry an
opened transmit gate over.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from linux import mrrc_radio


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
