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

import os
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
    """Every copier of /opt/mrrc_modern must copy exactly the same things.

    Three places now carry this list — the Pi builder, the box overlay and the
    in-place updater. Two of them disagreeing is how a file ends up in one
    image and missing from the other, or vanishes on the first update, and the
    symptom is a runtime ImportError rather than a build error.
    """

    EXCLUDE_RE = re.compile(r'--exclude\s+"([^"]+)"')

    IMPLEMENTERS = (
        ("the Pi image builder", "packaging/rpi/build-image.sh"),
        ("the box overlay", "packaging/box/box-overlay.sh"),
        ("the in-place updater", "linux/mrrc_update.sh"),
    )

    def _excludes(self, rel: str) -> list[str]:
        return self.EXCLUDE_RE.findall((REPO / rel).read_text(encoding="utf-8"))

    def test_every_implementer_excludes_the_same_paths(self):
        reference: list[str] = []
        for label, rel in self.IMPLEMENTERS:
            with self.subTest(implementer=label):
                got = self._excludes(rel)
                self.assertTrue(got, f"{label} returned an empty list — parser drift?")
                if not reference:
                    reference = got
                self.assertEqual(got, reference, f"{label} has drifted from {rel}")


class VendoredFtdiTests(unittest.TestCase):
    """The FT-710's scope libraries ship in the repo, and stay aarch64.

    The architecture assertion is the point: fetching the wrong build-*
    directory produces a file that exists, is named correctly, passes every
    other check in this plan, and fails only on the box.
    """

    FTDI = REPO / "vendor" / "ftdi"

    def test_the_real_library_is_an_aarch64_elf(self):
        header = (self.FTDI / "libft4222.so").read_bytes()[:20]
        self.assertEqual(header[:4], b"\x7fELF")
        self.assertEqual(header[4], 2, "not a 64-bit ELF")
        e_machine = int.from_bytes(header[18:20], "little")
        self.assertEqual(e_machine, 183, "not AArch64 (183); x86-64 is 62")

    def test_the_second_name_points_at_the_first(self):
        second = self.FTDI / "libftd2xx.so"
        self.assertTrue(second.is_symlink(), "must be a symlink, see vendor-ftdi.md")
        self.assertEqual(second.resolve(), (self.FTDI / "libft4222.so").resolve())


class BoxBuildIntegrityTests(unittest.TestCase):
    """Three failures the first real build found, none visible from reading.

    They share a shape: the build runs a Debian rootfs in a chroot on macOS,
    where assumptions that hold on a normal Linux host quietly stop holding — a
    bind mount stops being reachable, a resolver symlink points at nothing, and
    a base image is missing packages a design note said it carried. Each one
    costs a whole build cycle (an 852 MiB fetch is cached by then, but the
    decompress and the overlay are not free) to discover, so each one is
    asserted here instead of in a comment.
    """

    BUILD = REPO / "packaging" / "box" / "build-image.sh"
    OVERLAY = REPO / "packaging" / "box" / "box-overlay.sh"

    def test_everything_the_chrooted_command_uses_is_bound_into_the_chroot(self):
        """A chrooted /src resolves to /mnt/src, and mounting is not automatic.

        The failure mode is a bare "No such file or directory" for a path that
        plainly exists in the container.
        """
        script = self.BUILD.read_text(encoding="utf-8")
        chrooted = [line for line in script.splitlines() if "chroot /mnt" in line]
        self.assertTrue(chrooted, "build-image.sh no longer chroots?")
        referenced: set[str] = set()
        for line in chrooted:
            referenced |= set(re.findall(r"(/src/[A-Za-z0-9_./-]+)", line))
        self.assertTrue(referenced, "the chrooted command references nothing?")
        for path in sorted(referenced):
            top = "/" + path.split("/")[1]
            self.assertIn(
                f"--bind {top} /mnt{top}",
                script,
                f"{path} is used inside the chroot but {top} is not bound into it",
            )

    def test_the_chroot_gets_a_working_resolver(self):
        """apt and pip both run inside it, where systemd-resolved is not up.

        The rootfs resolv.conf is a symlink to /run/systemd/resolve/…, which is
        a dangling link off a booted system, so every lookup inside the chroot
        fails and the build dies in the middle of an apt transaction.
        """
        script = self.BUILD.read_text(encoding="utf-8")
        self.assertIn("/mnt/etc/resolv.conf", script)
        self.assertRegex(script, r"cp\s+/etc/resolv\.conf\s+/mnt/etc/resolv\.conf")

    def test_the_venv_bootstrap_precedes_install_sh(self):
        """install.sh cannot fix this itself, so the overlay must.

        It creates the virtualenv in STEP 2 and installs system packages in
        STEP 3, so a missing ensurepip is fatal before apt ever runs — and a
        virtualenv created without it has no pip, which fails the dependency
        install further down and ships an image whose server cannot start.
        """
        overlay = self.OVERLAY.read_text(encoding="utf-8")
        self.assertIn("python3.11-venv", overlay)
        self.assertLess(
            overlay.index("python3.11-venv"),
            overlay.index("./install.sh"),
            "the venv bootstrap must be installed before install.sh runs",
        )

    def test_opus_is_installed_rather_than_fallen_back_from(self):
        """Without libopus0 the server degrades to PCM and still looks healthy.

        The pinned image does not carry it, whatever the design note said.
        """
        self.assertIn("libopus0", self.OVERLAY.read_text(encoding="utf-8"))

    def test_the_mirror_swap_covers_both_debian_suites_and_not_armbian(self):
        """The base image carries two Debian stanzas in deb822 files.

        deb.debian.org/debian and security.debian.org are separate entries, so
        a rewrite keyed on one hostname alone looks like it worked and then
        stalls on the other. The Armbian archive must not be caught by it: that
        is where the kernel packages come from, and pointing it at a Debian
        mirror breaks apt outright rather than slowing it down.
        """
        script = self.BUILD.read_text(encoding="utf-8")
        self.assertIn("MRRC_BOX_APT_MIRROR", script, "the mirror must be overridable")
        rewrite = "\n".join(line for line in script.splitlines() if r"debian\.org" in line)
        self.assertIn(r"deb\.debian\.org", rewrite)
        self.assertIn(r"security\.debian\.org", rewrite)
        self.assertNotIn("armbian", rewrite.lower())

    def test_pip_is_pointed_away_from_pypi_before_the_chroot_runs(self):
        """PyPI measured 37 KB/s here and stalled the dependency install cold.

        The index travels through the chroot's inherited environment rather
        than being written into the rootfs, because the box never runs pip —
        the virtualenv is already baked. The ordering matters: exporting it
        after the chroot would have no effect at all.
        """
        script = self.BUILD.read_text(encoding="utf-8")
        self.assertIn("MRRC_BOX_PIP_INDEX", script, "the index must be overridable")
        self.assertIn('if [ -n "${MRRC_BOX_PIP_INDEX:-}" ]', script)
        self.assertLess(
            script.index("PIP_INDEX_URL"),
            script.index("chroot /mnt /bin/bash"),
            "the index must be exported before the chroot inherits it",
        )

    def test_the_image_gets_a_version_stamp(self):
        """version.txt is generated, not checked in, and the tree needs it.

        The support bundle manifest and the upgrade channel both read it from
        /opt/mrrc_modern, so an image that lacks it fails those two things while
        looking complete. The first real build did exactly that.
        """
        overlay = self.OVERLAY.read_text(encoding="utf-8")
        self.assertIn('"$MRRC_HOME/version.txt"', overlay)
        self.assertIn('"$MRRC_HOME/VERSION"', overlay)
        self.assertIn("CHANGELOG.md", overlay, "the version comes from the CHANGELOG")

    def test_the_artifact_name_carries_the_changelog_version(self):
        """`cat version.txt` produced MRRC-Modern-dev-w103d.img.

        version.txt is a build output rather than a source file, so reading it
        from the repository always yields the fallback. The Pi and macOS
        builders both derive it from the CHANGELOG; so does this one now.
        """
        script = self.BUILD.read_text(encoding="utf-8")
        self.assertIn("CHANGELOG.md", script)
        self.assertNotIn('cat "$REPO/version.txt"', script)
        self.assertIn("MRRC-Modern-${VERSION#v}-w103d.img", script)

    def test_every_shell_script_here_is_executable(self):
        """box-overlay.sh shipped as 0644 and the build died on the first run.

        Invoking it as `bash script.sh` would paper over this, but the build
        calls it directly — it has a shebang for a reason — so the bit is
        load-bearing rather than decoration. The reported symptom is a
        misleading `bad interpreter: Permission denied`.
        """
        for script in sorted(self.BUILD.parent.glob("*.sh")):
            with self.subTest(script=script.name):
                self.assertTrue(
                    os.access(script, os.X_OK),
                    f"{script.name} is not executable; the build runs it directly",
                )
