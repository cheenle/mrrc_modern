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


class SetupApPackagingTests(unittest.TestCase):
    """The setup hotspot's place in the image (design 2026-10-08-w103d-setup-ap).

    Every one of these is invisible from reading the overlay and expensive in the
    field, because the box being onboarded is by definition a box nobody can ssh
    into. A missing dnsmasq means the hotspot appears and a phone joins it and
    gets no address; a unit ordered after network-online.target means the service
    that exists precisely to cover "there is no network" waits for one.
    """

    OVERLAY = REPO / "packaging" / "box" / "box-overlay.sh"
    VERIFY = REPO / "packaging" / "box" / "verify.sh"
    UNIT = "mrrc-setup-ap.service"

    def setUp(self):
        self.text = self.OVERLAY.read_text(encoding="utf-8")

    def unit_body(self) -> str:
        """The heredoc body of this unit's `cat > … <<'UNIT'` block.

        Slicing from the unit's name to the next "UNIT" does not work: the name
        appears on the `cat` line, whose own heredoc marker is the next thing on
        it, so the slice comes back empty and every assertion passes vacuously.

        Nor does searching for the literal "UNIT\\n": the opener is quoted
        (`<<'UNIT'`), so the bytes there are `UNIT'\\n` and the first bare
        "UNIT\\n" in the file is the *closer* — slicing from it finds nothing and
        raises. Take the end of the `cat` line, then the next line that is
        exactly UNIT.
        """
        start = self.text.index(f"/etc/systemd/system/{self.UNIT}")
        opener = self.text.index("\n", start) + 1
        closer = self.text.index("\nUNIT\n", opener)
        body = self.text[opener:closer]
        self.assertIn("[Service]", body, "the slice missed the unit body")
        return body

    def apt_packages(self) -> list:
        """The package tokens of the overlay's single `apt-get install`.

        Parsed as tokens rather than grepped as a substring, because a substring
        match is satisfied by the *comment explaining why the package is there*.
        That is not hypothetical: mutating the install list to
        "# dnsmasq deliberately omitted" left a substring assertion green while
        shipping an image whose hotspot hands out no addresses.
        """
        install = self.text[self.text.index("apt-get install"):]
        install = install[:install.index("\n\n")]
        packages: list = []
        for line in install.splitlines():
            line = line.split("#", 1)[0]              # drop trailing comments
            for token in line.replace("\\", " ").split():
                if token in ("DEBIAN_FRONTEND=noninteractive", "apt-get",
                             "install", "-y", "-qq", "--no-install-recommends"):
                    continue
                packages.append(token)
        return packages

    def test_dnsmasq_is_in_the_apt_install_list(self):
        """Design D-2. NM's shared mode hands out addresses through dnsmasq; the
        pinned base image does not carry it."""
        self.assertIn("dnsmasq", self.apt_packages())

    def test_the_package_list_is_what_the_overlay_actually_installs(self):
        """Guard the guard: if this parser drifts, the assertion above goes
        vacuous instead of loudly wrong."""
        packages = self.apt_packages()
        for expected in ("python3.11-venv", "python3-dev", "portaudio19-dev",
                         "libportaudio2", "libasound2-dev", "libopus0",
                         "libopus-dev", "dnsmasq"):
            with self.subTest(package=expected):
                self.assertIn(expected, packages)
        self.assertNotIn("apt-get", packages)
        self.assertTrue(all("#" not in p for p in packages), packages)

    def test_dnsmasq_joins_the_existing_install_rather_than_a_second_apt_run(self):
        """A second `apt-get install` in a chroot costs another resolver hit and
        another chance to stall on a slow mirror (the w103d-box skill's 坑 5)."""
        self.assertEqual(self.text.count("apt-get install"), 1)

    def test_the_unit_is_written(self):
        self.assertIn(f"/etc/systemd/system/{self.UNIT}", self.text)

    def test_the_unit_runs_the_daemon_with_the_venv_python(self):
        """The system python has none of the dependencies, and the script's exec
        bit is not what starts it — the w103d-box skill's 坑 4 is exactly a script
        whose permissions the build did not set."""
        body = self.unit_body()
        self.assertIn("/opt/mrrc_modern/venv/bin/python", body)
        self.assertIn("/opt/mrrc_modern/linux/setup_ap.py", body)

    def directives(self) -> str:
        """The unit body with its comments stripped.

        Assertions about ordering have to look at directives, not prose: the unit
        carries a comment explaining *why* it does not wait for a network, and
        that comment names the target. Grepping the raw body would make the
        explanation look like the mistake it is warning against.
        """
        body = self.unit_body()
        return "\n".join(line for line in body.splitlines()
                         if not line.lstrip().startswith("#"))

    def test_the_unit_does_not_wait_for_a_network(self):
        """The whole point of this service is that there isn't one.

        mrrc-modern.service does wait for network-online.target, and copying
        those two lines here is the obvious mistake: the hotspot would then
        appear only after NM's wait-online times out, or not at all.
        """
        directives = self.directives()
        self.assertNotIn("network-online.target", directives)
        self.assertIn("After=NetworkManager.service", directives)
        self.assertIn("Wants=NetworkManager.service", directives)

    def test_the_comment_that_explains_the_omission_stays(self):
        """The reasoning is the part a future editor needs. Stripping comments to
        satisfy the assertion above must not mean deleting the explanation."""
        self.assertIn("network-online.target", self.unit_body())

    def test_the_unit_is_resident_not_oneshot(self):
        """Design §7: the wizard page — and later /manage — need to ask whether
        the hotspot is up and how long is left. A oneshot has exited by then."""
        body = self.unit_body()
        self.assertNotIn("Type=oneshot", body)
        self.assertIn("Restart=always", body)

    def test_the_unit_runs_as_root(self):
        """nmcli needs polkit authority over system connections, and the state
        directory under /run has to be creatable with group `mrrc` so the server
        can leave its switch request there. `User=mrrc` here is a hotspot that
        never comes up."""
        self.assertNotIn("User=mrrc", self.unit_body())

    def test_the_banner_reaches_the_hdmi_console(self):
        """Design D-6's mitigation for "a neighbour wins the race": the address is
        printed on the console too, so an operator with a monitor never needs the
        open hotspot at all. Journal-only output does not reach the getty."""
        self.assertIn("console", self.unit_body())

    def test_the_unit_is_enabled(self):
        enabled = [line for line in self.text.splitlines()
                   if "systemctl enable" in line]
        self.assertTrue(enabled, "the overlay no longer enables any service?")
        self.assertIn(self.UNIT, enabled[-1])

    def test_the_daemon_and_its_module_reach_the_image(self):
        """Both ride the existing rsync — `linux/` and the repo root are not in the
        exclusion list. Worth asserting because the symptom of getting it wrong is
        a unit that fails to start on a box nobody can reach."""
        excludes = ExclusionParityTests.EXCLUDE_RE.findall(self.text)
        self.assertTrue(excludes, "the parser found no excludes — drift?")
        self.assertNotIn("linux/", excludes)
        self.assertNotIn("net_wifi.py", excludes)
        self.assertTrue((REPO / "linux" / "setup_ap.py").is_file())
        self.assertTrue((REPO / "net_wifi.py").is_file())

    def test_the_overlay_leaves_the_env_file_alone(self):
        """D-4: the WiFi domain has one writer and it is not the env file. Nothing
        in the overlay may add an MRRC_SETUP_AP_* key to mrrc.env — the defaults
        live in net_wifi.ap_settings()."""
        for key in ("MRRC_SETUP_AP_SSID", "MRRC_SETUP_AP_STATE_DIR",
                    "MRRC_SETUP_AP_TIMEOUT_MIN"):
            with self.subTest(key=key):
                self.assertNotIn(key, self.text)


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

    def test_it_checks_the_setup_hotspot(self):
        self.assertIn("mrrc-setup-ap.service", self.text)
        self.assertIn("dnsmasq", self.text)

    def test_a_box_that_already_has_a_network_is_not_failed_for_having_no_hotspot(self):
        """By design (D-6 fence 1) the hotspot is DOWN once the box has an uplink.
        A check that demands it be up fails on every healthy box, and the operator
        reads "your image is broken" about a box that is fine.

        Asserted structurally, because a substring search for a *regex* is
        vacuous (it matches nothing, ever, and so passes no matter what the
        script does): `$mode` may be **reported** — inside an assignment or an
        `ok`/`printf` line — but must never be **tested**. A conditional on it is
        the bug.
        """
        section = self.text[self.text.index("setup hotspot"):]
        section = section[:section.index("printf '\\n────")]
        mentioning = [line.strip() for line in section.splitlines()
                      if "$mode" in line or "mode=" in line]
        self.assertTrue(mentioning,
                        "the section no longer reports the mode at all — "
                        "this guard has gone vacuous")
        for line in mentioning:
            with self.subTest(line=line):
                self.assertFalse(line.startswith(("if ", "elif ", "while ", "[ ")),
                                 "the published mode is being tested, not reported")
                self.assertNotIn("!=", line)

    def test_the_failure_branches_are_about_the_machinery_not_the_mode(self):
        """What may legitimately fail: the unit missing, not enabled, dnsmasq
        absent, or the daemon dead. All four are about the machinery being there,
        which is what a post-onboarding run can still verify."""
        section = self.text[self.text.index("setup hotspot"):]
        for expected in ("is not installed", "not enabled", "dnsmasq is missing",
                         "is not running"):
            with self.subTest(expected=expected):
                self.assertIn(expected, section)
