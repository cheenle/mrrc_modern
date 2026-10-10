"""The image is the repo minus an exclusion list — the code has to survive that.

The Pi and box images are not a checkout: the tree under ``/opt/mrrc_modern`` is
whatever survives the rsync ``--exclude`` list in ``packaging/rpi/build-image.sh``
(mirrored by ``packaging/box/box-overlay.sh`` and ``linux/mrrc_update.sh``, and
pinned byte-identical by ``test_box_profiles.ExclusionParityTests``). Nothing
checked the consequence until 2026-10-10:

``server.py`` imported ``macos.first_run`` at module level, the images exclude
``macos/`` on purpose, and ``python server.py`` died with ``ModuleNotFoundError``
before the logger existed — a systemd restart loop with no log line to explain
it. The 1.25.4 box image shipped that way (verified by importing ``server`` from
the published bytes), and both build gates stayed green because they ``py_compile``
server.py and import the *third-party* dependencies: neither imports a
first-party entry point from the tree the image actually carries.

So this file does. Each entry point runs in a fresh interpreter in which the
excluded top-level names cannot be found in the repo, exactly as they cannot be
found in ``/opt/mrrc_modern``.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: The authoritative exclusion list (the Pi builder's; the other two must match
#: it — ExclusionParityTests fails otherwise).
BUILDER = REPO / "packaging" / "rpi" / "build-image.sh"

#: Names the emulation must hide for it to mean anything. ``macos`` is the
#: defect this file was written for; ``packaging`` is where the box profiles
#: come from (the overlay copies them to ``/opt/mrrc_modern/profiles``), so a
#: tree that carries ``packaging/`` is a different image than the one tested.
CANARIES = ("macos", "packaging")

#: Runs in a subprocess because the finder has to be installed before the first
#: import: a module already in sys.modules would prove nothing.
PROBE = r'''
import importlib.abc
import importlib.machinery
import os
import runpy
import shutil
import sys
import tempfile

repo = os.path.abspath(sys.argv[1])
hidden = {name for name in sys.argv[2].split(",") if name}


class ImageTree(importlib.abc.MetaPathFinder):
    """Nothing under the repo root resolves for the excluded names.

    A name some *other* distribution provides must still resolve — ``packaging``
    is both a repo directory and a common PyPI library — so the fall-through
    search runs with the repo root off sys.path, exactly like the box. When
    nothing else provides it, the import fails the way it fails on the box.
    """

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] not in hidden:
            return None
        saved = list(sys.path)
        try:
            sys.path = [p for p in sys.path if os.path.abspath(p or ".") != repo]
            spec = importlib.machinery.PathFinder.find_spec(fullname)
        finally:
            sys.path = saved
        if spec is None:
            raise ModuleNotFoundError(
                f"No module named {fullname!r} (excluded from the image tree)",
                name=fullname)
        return spec


sys.meta_path.insert(0, ImageTree())
sys.path.insert(0, repo)

failures = []


def probe(label, call):
    try:
        call()
    except BaseException as exc:                       # noqa: BLE001
        failures.append(f"{label}: {type(exc).__name__}: {exc}")


def script(relative):
    runpy.run_path(os.path.join(repo, relative), run_name="mrrc_image_probe")


def box_firstboot():
    """The overlay installs packaging/box/firstboot_wrapper.py as linux/firstboot_wrapper.py.

    Its sibling import is ``first_run``, which the image has at
    ``linux/first_run.py`` — the repo keeps the two in different directories, so
    the probe stages the pair the way the image does.
    """
    staging = tempfile.mkdtemp(prefix="mrrc-image-tree-")
    linux = os.path.join(staging, "linux")
    os.makedirs(linux)
    shutil.copy(os.path.join(repo, "linux", "first_run.py"),
                os.path.join(linux, "first_run.py"))
    shutil.copy(os.path.join(repo, "packaging", "box", "firstboot_wrapper.py"),
                os.path.join(linux, "firstboot_wrapper.py"))
    runpy.run_path(os.path.join(linux, "firstboot_wrapper.py"),
                   run_name="mrrc_image_probe")


probe("server", lambda: __import__("server"))
probe("linux/firstboot_wrapper.py", box_firstboot)
probe("linux/setup_ap.py", lambda: script("linux/setup_ap.py"))
probe("linux/mrrc_radio.py", lambda: script("linux/mrrc_radio.py"))

for line in failures:
    print(line)
sys.exit(1 if failures else 0)
'''


def excluded_top_level_names() -> set[str]:
    """Directory names the image build drops from the repo root.

    Derived from the builder rather than typed here: a hand-copied list would
    drift the moment somebody adds an exclusion, and the drift would look like a
    passing test.
    """
    text = BUILDER.read_text(encoding="utf-8")
    names = set()
    for raw in re.findall(r'--exclude\s+"([^"]+)"', text):
        if raw.endswith("/") and "*" not in raw:
            top = raw.rstrip("/")
            if "/" not in top:
                names.add(top)
    return names


class ImageTreeImportTests(unittest.TestCase):
    def test_the_exclusion_parse_keeps_its_canaries(self):
        """A parse that silently returns less would make every test below vacuous."""
        names = excluded_top_level_names()
        for canary in CANARIES:
            with self.subTest(canary=canary):
                self.assertIn(canary, names)

    def test_every_image_entry_point_imports_from_the_image_tree(self):
        """The commands the units and the CLI actually run, in a tree that looks
        like the image: ``server.py`` (mrrc-modern.service), the box's first-boot
        wrapper, the setup hotspot daemon, and the ``mrrc-radio`` CLI."""
        proc = subprocess.run(
            [sys.executable, "-", str(REPO), ",".join(sorted(excluded_top_level_names()))],
            input=PROBE, capture_output=True, text=True, cwd=str(REPO),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(
            proc.returncode, 0,
            "an image entry point cannot import from the tree the image carries:\n"
            + (proc.stdout or "") + (proc.stderr or ""))


if __name__ == "__main__":
    unittest.main()
