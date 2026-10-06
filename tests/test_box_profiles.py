"""The box image's radio profiles: one per registry key, and no TX gate opened.

The profiles are data, and this file is what keeps them data. Three things are
worth failing the build over:

* a registry key with no profile — the switcher would offer a model this image
  cannot serve;
* a profile that grows into a second source of truth for a value the backend
  already derives (baud, audio device) or that firstboot owns (password,
  serial port) — design D-2;
* the transmit gate of a hardware-unverified model being flipped. That one
  ships an image whose first connect can key the radio on tables nobody has
  measured (AD-019 / NFR-067), so it is the assertion this file exists for.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from backends import known_models

REPO = Path(__file__).resolve().parents[1]
PROFILE_DIR = REPO / "packaging" / "box" / "profiles"

#: Every key a profile may carry. Anything else is either derived by the
#: backend (baud), resolved on first boot (password, serial port, audio device)
#: or set by the systemd unit — see design D-2.
ALLOWED_KEYS = frozenset({"MRRC_RADIO_MODEL", "MRRC_ALLOW_UNVERIFIED_TX"})

#: Registry keys with no hardware evidence. Held against the backend tables by
#: test_unverified_set_matches_the_backend_registries below.
UNVERIFIED = frozenset(
    {"ic705", "ic7610", "ic7760", "ftdx10", "ftdx101d", "ftdx101mp", "ftx1", "ft891"}
)

VERIFIED = ("ft710", "ic7300", "ic7300mk2")


def _read(path: Path) -> dict[str, str]:
    """KEY=VALUE of one profile, comments and blanks skipped."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


class ProfileSetTests(unittest.TestCase):
    def test_registry_and_profiles_agree_exactly(self):
        self.assertEqual(
            {p.stem for p in PROFILE_DIR.glob("*.env")},
            set(known_models()),
        )

    def test_each_profile_names_its_own_file(self):
        for path in sorted(PROFILE_DIR.glob("*.env")):
            self.assertEqual(_read(path).get("MRRC_RADIO_MODEL"), path.stem, path.name)

    def test_no_profile_carries_a_key_outside_the_allow_list(self):
        for path in sorted(PROFILE_DIR.glob("*.env")):
            extra = set(_read(path)) - ALLOWED_KEYS
            self.assertFalse(extra, f"{path.name} carries {sorted(extra)}")

    def test_no_profile_opens_the_transmit_gate(self):
        """NFR-067: MRRC_ALLOW_UNVERIFIED_TX=1 must never ship in a profile."""
        for path in sorted(PROFILE_DIR.glob("*.env")):
            self.assertNotEqual(
                _read(path).get("MRRC_ALLOW_UNVERIFIED_TX"), "1", path.name
            )

    def test_unverified_models_state_the_gate_explicitly(self):
        """A default is not a decision: the eight say it in the file."""
        for model in sorted(UNVERIFIED):
            self.assertEqual(
                _read(PROFILE_DIR / f"{model}.env").get("MRRC_ALLOW_UNVERIFIED_TX"),
                "0",
                model,
            )

    def test_verified_models_do_not_mention_the_gate(self):
        for model in VERIFIED:
            self.assertNotIn(
                "MRRC_ALLOW_UNVERIFIED_TX", _read(PROFILE_DIR / f"{model}.env"), model
            )

    def test_unverified_set_matches_the_backend_registries(self):
        """The list above is checked against the tables that own `verified`."""
        from backends.ic7300.civ_profiles import get_profile as icom_profile
        from backends.ic7300.civ_profiles import known_models as icom_models
        from backends.yaesu.yaesu_profiles import PROFILES as yaesu_profiles

        derived = {m for m in icom_models() if not icom_profile(m).verified}
        derived |= {m for m, p in yaesu_profiles.items() if not p.verified}
        self.assertEqual(derived, set(UNVERIFIED))


class ExclusionParityTests(unittest.TestCase):
    """The box overlay must copy exactly what the Pi builder copies.

    Two builders that disagree about what belongs in /opt/mrrc_modern is how a
    file ends up in one image and missing from the other, and the symptom is a
    runtime ImportError rather than a build error.
    """

    EXCLUDE_RE = re.compile(r'--exclude\s+"([^"]+)"')

    def _excludes(self, path: Path) -> list[str]:
        return self.EXCLUDE_RE.findall(path.read_text(encoding="utf-8"))

    def test_the_two_builders_exclude_the_same_paths(self):
        pi = self._excludes(REPO / "packaging" / "rpi" / "build-image.sh")
        box = self._excludes(REPO / "packaging" / "box" / "box-overlay.sh")
        self.assertTrue(pi, "the Pi builder's list came back empty — parser drift?")
        self.assertEqual(box, pi)
