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
import subprocess
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


class VendoredMt7663Tests(unittest.TestCase):
    """The W103D's WiFi N9 firmware ships in the repo, pinned by SHA-256.

    The linux-firmware 2020 build associates and then progressively dies on
    this board (field report 2026-10-11); the factory 2021 build is the one
    the kernel port was validated against. A byte drift here is invisible
    until a box's WiFi wedges in the field. See packaging/box/vendor-mt7663.md.
    """

    N9_SHA256 = "223f73f17f0f986dc4e7167daa6eef14ffb41c713f22d70f9645eb049bdec80a"

    def test_the_vendored_file_is_the_pinned_factory_build(self):
        import hashlib
        blob = (REPO / "vendor" / "mediatek" / "mt7663_n9_v3.bin").read_bytes()
        self.assertEqual(hashlib.sha256(blob).hexdigest(), self.N9_SHA256)

    def test_the_overlay_installs_verifies_and_protects_it(self):
        """Install without the sha256 check would ship a silent swap; without
        the diversion the next linux-firmware upgrade puts the broken build
        back on boxes in the field."""
        overlay = (REPO / "packaging" / "box" / "box-overlay.sh").read_text(
            encoding="utf-8")
        self.assertIn("vendor/mediatek/mt7663_n9_v3.bin", overlay)
        self.assertIn(self.N9_SHA256, overlay)
        self.assertIn("dpkg-divert", overlay)


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

    def test_avahi_is_installed_and_dnsmasq_is_not(self):
        """`dnsmasq` was there for the hotspot's shared mode; `avahi-daemon` is
        there for <hostname>.local. Swapping them is the whole packaging change
        for the new onboarding path, and a stray `dnsmasq` would keep pulling a
        daemon that collides with systemd-resolved for port 53."""
        text = self.OVERLAY.read_text(encoding="utf-8")
        packages = text.split("--no-install-recommends", 1)[1].split("\n\n", 1)[0]
        self.assertIn("avahi-daemon", packages)
        self.assertNotIn("dnsmasq", packages)

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

    @unittest.skipIf(os.name == "nt", "runs the box's bash parse block; Windows has no bash")
    def test_the_frp_source_list_keeps_its_last_resort(self):
        """A comma list ending in an empty entry has to yield that empty entry.

        The empty entry means "and finally GitHub itself". `read -a` does not
        produce a trailing empty field, so the first version of this list
        quietly dropped its last resort: with every mirror down the build gave
        up instead of falling back, which is how it behaved in practice before
        anyone looked at the list.

        The parse is lifted out of the script and executed rather than
        restated here — a copy in the test would keep passing after the
        original changed.
        """
        script = (self.BUILD.parent / "fetch-frpc.sh").read_text(encoding="utf-8")
        self.assertNotIn(
            "read -r -a PREFIXES", script, "read -a silently drops a trailing empty field"
        )
        start = script.index("PREFIXES=()")
        parse = script[start : script.index("\ndone", start) + len("\ndone")]
        self.assertIn("while", parse, "the parse block moved — extraction is stale")

        def sources(value: str) -> list[str]:
            result = subprocess.run(
                ["bash", "-c", parse + '\nprintf "%s\\n" "${PREFIXES[@]}"'],
                env={**os.environ, "MRRC_FRP_PROXY": value},
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.split("\n")[:-1]  # drop the trailing newline

        self.assertEqual(sources("a,b,"), ["a", "b", ""], "last resort lost")
        self.assertEqual(sources("a"), ["a"])
        self.assertEqual(sources(""), [""], "an empty list must mean GitHub itself")
        self.assertEqual(sources("a,,b"), ["a", "", "b"])

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


class VerifyScriptTests(unittest.TestCase):
    """verify.sh is the only diagnostic that works on a box nobody can reach —
    but only *after* onboarding, over the network the wizard just configured."""

    SCRIPT = REPO / "packaging" / "box" / "verify.sh"

    def setUp(self):
        self.text = self.SCRIPT.read_text(encoding="utf-8")

    def test_the_section_numbers_are_contiguous_and_agree_on_a_total(self):
        """Renumbering by hand is how a script ends up printing 3/10 in a run of
        eleven checks. The headers are the authority."""
        heads = re.findall(r'head_ "(\d+)/(\d+) ', self.text)
        self.assertTrue(heads, "the header shape changed — update this guard")
        totals = {total for _, total in heads}
        self.assertEqual(len(totals), 1, f"mixed denominators: {heads}")
        self.assertEqual(int(totals.pop()), len(heads))
        self.assertEqual([int(n) for n, _ in heads],
                         list(range(1, len(heads) + 1)))

    def test_it_checks_the_onboarding_channel(self):
        """The three things that make the LAN onboarding path work, and whose
        absence is invisible until an operator is standing at a box they cannot
        reach: the polkit rule that lets the server write NetworkManager, the
        mDNS name that is the only way back after a network switch, and the root
        helper socket for the four system verbs (design §7, §8)."""
        self.assertIn("49-mrrc-nm.rules", self.text)
        self.assertIn("avahi-daemon", self.text)
        self.assertIn("/run/mrrc/box-helper.sock", self.text)

    def test_it_no_longer_checks_a_hotspot(self):
        """The feature failed its hardware test on 2026-10-11 (spec §1). A check
        for machinery that no longer ships is a check that fails every good
        box."""
        self.assertNotIn("mrrc-setup-ap", self.text)
        self.assertNotIn("/run/mrrc/setup-ap", self.text)

    def test_no_pipe_into_a_late_exiting_matcher(self):
        """`cmd | grep -q` under `set -o pipefail` fails when it *succeeds*.

        grep exits at the first match, the writer takes SIGPIPE (128+13=141) and
        pipefail reports the whole pipeline as failed — so a check that just
        found what it was looking for announces the opposite. Measured on the
        first real box (2026-10-11):
        `systemctl list-unit-files --no-legend | grep -q '^mrrc-setup-ap.service'`
        exited 141 and the acceptance script printed "mrrc-setup-ap.service is
        not installed" about a unit that was running.

        `grep -c` is deliberately not matched: it reads to EOF, so nothing gets
        SIGPIPE and the count is the point.
        """
        offenders = [line.strip() for line in self.text.splitlines()
                     if not line.strip().startswith("#")
                     and re.search(r"\|\s*grep\s+-[a-zA-Z]*q", line)]
        self.assertEqual(offenders, [],
                         "a pipe into `grep -q` fails under pipefail with SIGPIPE; "
                         "capture the output first and match the variable")

    def test_the_failure_branches_are_about_the_machinery_not_the_mode(self):
        """What may legitimately fail: the polkit rule missing, avahi down, the
        mDNS name not resolving, or the helper socket absent. All of them are
        about the machinery being there, which is what a post-onboarding run can
        still verify."""
        section = self.text[self.text.index("box identity"):]
        for expected in ("polkit rule is missing", "avahi-daemon is not running",
                         "box-helper.sock is missing"):
            with self.subTest(expected=expected):
                self.assertIn(expected, section)


class BoxFirstBootHardwareTests(unittest.TestCase):
    """What the first real box proved the overlay was missing.

    Every one of these is invisible on a desktop and only bites on the hardware:
    an FT4222 the service user cannot open (no real FFT at all), the build host's
    serial port baked into the env, a unit line systemd refuses to parse (so the
    HDMI banner never appears), and Debian's dnsmasq unit fighting
    systemd-resolved for port 53. Found on the W103D, 2026-10-11 — a batch of
    hardware-session findings, each with the command that proves it.
    """

    OVERLAY = REPO / "packaging" / "box" / "box-overlay.sh"

    def setUp(self):
        self.text = self.OVERLAY.read_text(encoding="utf-8")

    def test_the_service_user_gets_the_scope_interface(self):
        """libft4222 opens the USB device as `mrrc`; root:root 0664 is FTDI error 2.

        Measured: the scope pipe run as root reached spi_ready on the first
        attempt, run as mrrc it failed every attempt — CAT and audio unaffected,
        which is what makes it confusing.
        """
        self.assertIn("99-mrrc-ftdi.rules", self.text)
        self.assertIn('ATTR{idVendor}=="0403"', self.text)
        self.assertIn('ATTR{idProduct}=="601c"', self.text)
        self.assertIn('GROUP="dialout"', self.text)

    def test_the_build_hosts_hardware_guesses_are_stripped_from_the_env(self):
        """install.sh's template is written on the build host: it carries that
        machine's serial port and a password it generated there. On a box both
        are first-boot facts (design D-3) — 2026-10-11 the box came up pointing
        at a `/dev/cu.SLAB_USBtoUART` that only exists on the developer's Mac."""
        self.assertIn(r"/^#\?MRRC_SERIAL_PORT=/d", self.text)
        self.assertIn(r"/^#\?MRRC_WEB_PASSWORD=/d", self.text)

    def test_no_unit_line_uses_the_bare_console_specifier(self):
        """`StandardOutput=console` is not a systemd specifier.

        systemd logs "Failed to parse output specifier, ignoring: console" and
        the operator with only an HDMI monitor never sees the first-boot banner
        with their web password (design D-6 relies on that banner). Valid forms:
        `tty`, `journal`, `journal+console`.
        """
        units = [self.OVERLAY, *sorted((REPO / "packaging" / "rpi").rglob("*.service"))]
        for path in units:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line.startswith(("StandardOutput=", "StandardError=")):
                    continue
                spec = line.split("=", 1)[1]
                with self.subTest(unit=path.name, line=line):
                    self.assertNotEqual(spec, "console")
                    if "console" in spec:
                        self.assertEqual(spec, "journal+console")

    def test_debians_dnsmasq_unit_is_masked(self):
        """NM's shared mode spawns its own dnsmasq; the packaged unit loses port
        53 to systemd-resolved and shows up as a failed unit on a fresh box."""
        self.assertIn("systemctl mask dnsmasq.service", self.text)

    def test_the_capture_buffer_default_is_visible_in_the_env_file(self):
        """The code default is the same on Linux; writing it into mrrc.env makes
        the knob discoverable for the operator who has to raise it (guide §13)."""
        self.assertIn("MRRC_RX_BUFFER_MS=100", self.text)
