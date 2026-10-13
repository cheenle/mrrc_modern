"""Two image lines, one service layer — and this is what keeps it that way.

The Pi image and the W103D box image install the same server into the same
``/opt/mrrc_modern``. Everything they share lives in ``packaging/common/`` now;
before 2026-10-11 it lived in both ``packaging/box/`` and ``packaging/rpi/``, and
the drift was concrete rather than theoretical: ``firstboot_wrapper.py`` existed
twice (differing in the boot path, the bind address and a docstring), the
``mrrc-show-password`` helper lived under ``packaging/rpi/`` and the box overlay
reached across to fetch it, the exclusion list had three byte-identical copies
kept in sync by ``ExclusionParityTests``, and the Pi's systemd unit was missing
the ``Environment=MRRC_CONFIG_FILE`` line that makes Cloud Hub work (D-11).

Every test here fails against that layout and passes against the unified one, so
the unification cannot be quietly undone by adding "just one" platform copy.
"""
from __future__ import annotations

import re
import os
import shutil
import stat
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMMON = REPO / "packaging" / "common"

#: Where the exclusion list is allowed to live. One entry, on purpose.
STAGE_TREE = COMMON / "stage-tree.sh"

#: Files the service layer generates. None of them may exist in a platform shell.
SERVICE_LAYER_OUTPUT = (
    "mrrc-modern.service",
    "mrrc-firstboot.service",
    "firstboot_wrapper.py",
    "mrrc-show-password",
    "10-mrrc",
)

#: What each platform shell must call. (path, [required substrings])
CALL_SITES = (
    ("packaging/rpi/build-image.sh", ("stage-tree.sh",)),
    ("packaging/rpi/pi-gen-stage4/01-deploy-mrrc/00-run-chroot.sh",
     ("mrrc-service-layer.sh",)),
    ("packaging/box/box-overlay.sh", ("stage-tree.sh", "mrrc-service-layer.sh")),
)

#: The only values allowed to differ between the two lines, and what they are.
WEB_HOST = {"packaging/rpi": "::", "packaging/box": "0.0.0.0"}
BOOT_DIR = {"packaging/rpi": "/boot/firmware", "packaging/box": "/boot"}

#: Packages the Pi stage may install on its own. Anything else has to come from
#: packaging/common/packages.txt — one answer to "what does the server need".
PI_PLATFORM_PACKAGES = frozenset({"e2fsprogs"})


class ExclusionListTests(unittest.TestCase):
    def _carriers(self) -> list[Path]:
        found = []
        for path in sorted(REPO.glob("packaging/**/*.sh")) + [
            REPO / "linux" / "mrrc_update.sh"
        ]:
            if "--exclude" in path.read_text(encoding="utf-8"):
                found.append(path.relative_to(REPO))
        return found

    def test_the_exclusion_list_lives_in_exactly_one_file(self):
        self.assertEqual([str(STAGE_TREE.relative_to(REPO))],
                         [str(p) for p in self._carriers()],
                         "the per-image exclusion list has a second home — that is "
                         "how a file ends up in one image and missing from the other")

    def test_the_one_list_still_carries_every_rule(self):
        """A list that got emptied to make the test above pass is worse than no test."""
        text = STAGE_TREE.read_text(encoding="utf-8")
        entries = re.findall(r'--exclude\s+"([^"]+)"', text)
        self.assertGreaterEqual(len(entries), 40, f"only {len(entries)} exclude rules")
        for canary in ("/.env", "/recordings/", "/fleet/", "website/", "packaging/",
                       "tests/"):
            with self.subTest(canary=canary):
                self.assertIn(canary, entries)

    def test_the_exclusion_list_is_byte_identical_to_what_shipped(self):
        """The list as of 2026-10-11, frozen: the refactor moved it, and every
        edit since then is written down here — a root-anchored ``/fleet/``, and
        ``/draft/`` when the Rust draft workstream landed on main (2026-10-13)."""
        expected = [
            ".git/", "venv/", ".venv/", "dist/", "build/", "logs/", "certs/",
            "promo/", "FT710Mobile/", "FT710Android/", "website/", "tests/",
            ".agents/", "docs/", "SDD/", "packaging/", "macos/", "windows/",
            "__pycache__/", "*.pyc", ".DS_Store", "atr1000_tuner.json",
            "/fleet/",
            "/draft/",
            "/.local/", "/.env", "/recordings/", "/mem_channels.json",
            "/.worktrees/", "/.superpowers/", "/.agnes/", "/.claude/",
            "/.pytest_cache/", "/.ruff_cache/", "/dev_tools/", "/tools/",
            "/support-out/", "/lib/", "/biome.json", "/build_mrrc_v1101.ps1",
            "/*.md", "/*.pdf", "/*.jpg", "/*.JPG", "/*.PNG", "/*.m4a",
        ]
        got = re.findall(r'--exclude\s+"([^"]+)"', STAGE_TREE.read_text(encoding="utf-8"))
        self.assertEqual(got, expected)


class CallSiteTests(unittest.TestCase):
    def test_every_builder_calls_the_shared_layers(self):
        for relative, required in CALL_SITES:
            text = (REPO / relative).read_text(encoding="utf-8")
            for needle in required:
                with self.subTest(path=relative, needle=needle):
                    # assertTrue, not assertIn: assertIn puts the whole file in the
                    # failure message, and these files are hundreds of lines long.
                    self.assertTrue(needle in text,
                                    f"{relative} does not use {needle}")


class PiStageTests(unittest.TestCase):
    def test_the_pi_stage_carries_no_service_layer_output(self):
        """The regression gate for every duplicate this refactor removed."""
        stage = REPO / "packaging" / "rpi" / "pi-gen-stage4"
        names = {p.name for p in stage.rglob("*") if p.is_file()}
        for name in SERVICE_LAYER_OUTPUT:
            with self.subTest(name=name):
                self.assertNotIn(name, names,
                                 f"{name!r} is generated by packaging/common — a copy "
                                 f"here is a second implementation of the same unit")
        pyc = [p for p in stage.rglob("*.pyc")]
        self.assertEqual(pyc, [], "a committed .pyc ships inside the image")

    def test_the_pi_package_list_holds_only_platform_extras(self):
        text = (REPO / "packaging" / "rpi" / "pi-gen-stage4" /
                "00-install-packages" / "00-packages").read_text(encoding="utf-8")
        entries = {line.strip() for line in text.splitlines()
                   if line.strip() and not line.strip().startswith("#")}
        self.assertTrue(entries <= PI_PLATFORM_PACKAGES,
                        f"not platform extras: {sorted(entries - PI_PLATFORM_PACKAGES)}")
        shared = {line.strip() for line in (COMMON / "packages.txt")
                  .read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.strip().startswith("#")}
        self.assertEqual(entries & shared, set(),
                         "the Pi stage re-lists packages the service layer installs")


class IntentionalDivergenceTests(unittest.TestCase):
    """Differences must be asserted, not forgotten. There are exactly two.

    Scanned over the files that *set* a platform value, not over the whole
    directory: a verifier that checks for `MRRC_WEB_HOST=::` is not a second
    place that decides it, and a directory-wide scan cannot tell the two apart
    (2026-10-11: the rewritten verify-image.sh made this test fail by quoting
    the value it asserts on).
    """

    #: The files allowed to carry a platform parameter.
    CARRIERS = {
        "packaging/rpi": (
            "packaging/rpi/build-image.sh",
            "packaging/rpi/pi-gen-stage4/prerun.sh",
            "packaging/rpi/pi-gen-stage4/01-deploy-mrrc/00-run-chroot.sh",
        ),
        "packaging/box": (
            "packaging/box/box-overlay.sh",
            "packaging/box/build-image.sh",
        ),
    }

    def _assignments(self, root: str, key: str) -> list[tuple[str, str]]:
        out = []
        for relative in self.CARRIERS[root]:
            path = REPO / relative
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(rf"{key}=(?:'|\")?([^'\"\s]+)", text):
                out.append((relative, match.group(1)))
        return out

    def test_every_carrier_exists(self):
        """A renamed builder would silently empty these tests instead of failing."""
        for root, carriers in self.CARRIERS.items():
            for relative in carriers:
                with self.subTest(path=relative):
                    self.assertTrue((REPO / relative).is_file(), root)

    def test_the_web_bind_is_the_only_bind_divergence(self):
        pi = dict(self._assignments("packaging/rpi", "MRRC_WEB_HOST"))
        box = dict(self._assignments("packaging/box", "MRRC_WEB_HOST"))
        self.assertEqual(set(pi.values()), {WEB_HOST["packaging/rpi"]})
        self.assertEqual(set(box.values()), {WEB_HOST["packaging/box"]})

    def test_the_platform_bind_is_not_left_to_install_sh(self):
        """install.sh's env template hard-codes ``MRRC_WEB_HOST=0.0.0.0``.

        The defaults block only writes a key that is absent, so an un-stripped
        template wins silently: the first refactored Pi image shipped bound to
        IPv4-only while its own unit said ``::`` (caught by the four-step
        artifact verification, 2026-10-11 — the unit had the parameter, the env
        file did not). Two things have to stay true.
        """
        text = (COMMON / "mrrc-service-layer.sh").read_text(encoding="utf-8")
        self.assertIn(r"/^#\?MRRC_WEB_HOST=/d", text,
                      "install.sh's own MRRC_WEB_HOST must be stripped, not inherited")
        self.assertIn('echo "MRRC_WEB_HOST=$MRRC_WEB_HOST"', text,
                      "the platform parameter is what gets written")

    def test_the_boot_partition_is_the_only_path_divergence(self):
        pi = dict(self._assignments("packaging/rpi", "MRRC_BOOT_DIR"))
        box = dict(self._assignments("packaging/box", "MRRC_BOOT_DIR"))
        self.assertEqual(set(pi.values()), {BOOT_DIR["packaging/rpi"]})
        self.assertEqual(set(box.values()), {BOOT_DIR["packaging/box"]})


class ShellHygieneTests(unittest.TestCase):
    """Failure modes that no test-driven shell script ever catches on its own."""

    SHELLS = ("packaging/common/*.sh", "packaging/box/*.sh", "packaging/rpi/*.sh")

    def _shells(self) -> list[Path]:
        paths: list[Path] = []
        for pattern in self.SHELLS:
            paths.extend(sorted(REPO.glob(pattern)))
        paths.append(REPO / "linux" / "mrrc_update.sh")
        return paths

    @unittest.skipUnless(os.name == "posix",
                         "NTFS 没有可执行位：这条规则在 POSIX 文件模式上有意义")
    def test_shell_scripts_are_executable(self):
        """box-overlay.sh shipped as 0644 once: "bad interpreter: Permission denied"."""
        for path in self._shells():
            with self.subTest(path=str(path.relative_to(REPO))):
                mode = path.stat().st_mode
                self.assertTrue(mode & stat.S_IXUSR, f"{path} is not executable")

    def test_shared_scripts_are_bash_32_compatible(self):
        """The Pi builder runs stage-tree.sh with macOS's /bin/bash (3.2)."""
        banned = ("declare -A", "readarray", "mapfile")
        for path in sorted(COMMON.glob("*.sh")):
            text = path.read_text(encoding="utf-8")
            for needle in banned:
                with self.subTest(path=path.name, needle=needle):
                    self.assertNotIn(needle, text)
            self.assertIsNone(re.search(r"\$\{[A-Za-z_][A-Za-z0-9_]*,,\}", text),
                              path.name + " uses a bash-4 lower-case expansion on a variable")

    def test_the_service_layer_still_reclaims_its_scratch(self):
        """Both directories hold build scratch; either one left behind ships."""
        text = (COMMON / "mrrc-service-layer.sh").read_text(encoding="utf-8")
        self.assertIn("rm -rf /tmp/*", text)
        self.assertIn('rm -rf "$MRRC_ASSETS"', text)

    @unittest.skipUnless(shutil.which("bash"),
                         "没有 bash 就没有解析器这一票（Windows 构建机）")
    def test_every_shell_script_parses(self):
        """`bash -n` on everything, because the other rules here cannot see this.

        Prose inside a shell construct is a shape this repo got wrong twice on
        2026-10-11: a comment inside a continued argument list ends the command
        (the rule below), and a single apostrophe — the word "image's" — closes a
        `bash -c '…'` block and leaves the rest of the file unparseable. Neither
        is visible to a regex, and the second one is invisible to the eye as
        well, so the parser gets a vote.
        """
        offenders = []
        for path in self._all_shells():
            proc = subprocess.run(["bash", "-n", str(path)],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                detail = (proc.stderr.strip().splitlines() or ["parse error"])[0]
                offenders.append(f"{path.relative_to(REPO)}: {detail}")
        self.assertEqual(offenders, [],
                         "a shell script in this repo does not parse: "
                         + "; ".join(offenders))

    #: Directories whose .sh files belong to somebody else (upstream clones,
    #: vendored SDKs, virtualenvs) or to another checkout of this repo.
    SKIP_DIRS = frozenset(("build", "dist", "website", "FT710Mobile",
                           "FT710Android", "venv", ".venv", ".worktrees",
                           ".git", "node_modules", "logs"))

    def _all_shells(self) -> list[Path]:
        found = []
        for path in sorted(REPO.rglob("*.sh")):
            if set(path.relative_to(REPO).parts[:-1]) & self.SKIP_DIRS:
                continue
            found.append(path)
        return found

    def test_no_comment_inside_a_continued_argument_list(self):
        r"""A `#` after a line continuation ends the COMMAND, not the line.

        Committed to all three builders on 2026-10-11 and shipped nowhere: the
        comments were inserted between the --exclude groups *after* that day's
        image was built, so the next build of either image would have died with
        `--exclude: command not found` (exit 127) at the rsync.

        Neither `bash -n` nor a regex over the text can see it — the first is a
        parse-time check and this is runtime semantics, the second finds every
        --exclude whether or not bash ever passes it to rsync. So it gets its own
        rule: a continued line whose following comment line is in turn followed
        by an indented line (i.e. more arguments) is a defect.
        """
        offenders = []
        for path in self._shells():
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines):
                if not line.rstrip().endswith("\\"):
                    continue
                j = i + 1
                saw_comment = False
                while j < len(lines) and (not lines[j].strip()
                                          or lines[j].lstrip().startswith("#")):
                    saw_comment = saw_comment or lines[j].lstrip().startswith("#")
                    j += 1
                if saw_comment and j < len(lines) and lines[j][:1].isspace():
                    offenders.append(f"{path.relative_to(REPO)}:{i + 1}")
        self.assertEqual(offenders, [],
                         "a comment inside a continued argument list turns the rest "
                         "of the options into their own command")


class BootPathParameterTests(unittest.TestCase):
    """启动分区路径必须是参数，不能是字面量。

    Armbian 挂在 /boot、Raspberry Pi OS 挂在 /boot/firmware。写死的后果不是报错而是
    **静默哑掉**：systemd 的 ConditionPathExists 不成立就干脆不跑，谁也不会发现
    （2026-10-12 抓到：服务层生成的 Wi-Fi 预置单元与 linux/wifi_preseed.py 都写死
    /boot/mrrc-wifi.txt，于是这条能力在 Pi 上是死的）。
    """

    def test_the_preseed_unit_and_script_use_the_parameter(self):
        layer = (COMMON / "mrrc-service-layer.sh").read_text(encoding="utf-8")
        script = (REPO / "linux" / "wifi_preseed.py").read_text(encoding="utf-8")
        self.assertNotIn('ConditionPathExists=/boot/mrrc-wifi.txt', layer)
        self.assertIn('ConditionPathExists=$MRRC_BOOT_DIR/mrrc-wifi.txt', layer)
        self.assertIn('Environment=MRRC_BOOT_DIR=$MRRC_BOOT_DIR', layer)
        self.assertNotIn('PRESEED_FILE = "/boot/mrrc-wifi.txt"', script)
        self.assertIn('os.environ.get("MRRC_BOOT_DIR"', script)

    def test_no_shipped_file_hardcodes_the_boot_partition(self):
        """除了参数默认值本身，任何地方都不许出现字面量的启动分区路径。"""
        offenders = []
        # 碰启动分区的三个文件：服务层（生成单元）、首启包装（读预置 env）、预置脚本（读 wifi）
        targets = [COMMON / "mrrc-service-layer.sh", COMMON / "firstboot_wrapper.py",
                   REPO / "linux" / "wifi_preseed.py"]
        for path in targets:
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("*"):
                    continue
                if '"/boot/' in line or "=/boot/" in line:
                    offenders.append(f"{path.relative_to(REPO)}:{number}")
        self.assertEqual(offenders, [],
                         "启动分区路径写死了 —— Pi 上的这条能力会静默不生效")


class PackageListTests(unittest.TestCase):
    def test_libopus_is_named(self):
        """Without it the server silently falls back to PCM instead of Opus."""
        text = (COMMON / "packages.txt").read_text(encoding="utf-8")
        self.assertIn("libopus0", text)


if __name__ == "__main__":
    unittest.main()
