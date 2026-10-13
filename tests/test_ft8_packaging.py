"""FT8 packaging across the four platforms: the wiring, and the tricky part.

The FT8 work mode is Python plus one native library, and the library is the only
part of this product that has to be *built* for each target.  That makes the
packaging the place where the feature silently disappears: a tree full of
passing tests and an installer that answers "library missing" at the first mode
switch.  Each assertion below corresponds to one of the four platforms and to a
specific way the wiring was, or could be, forgotten.

The macOS bundling helper gets real unit tests (it parses `otool -L`, decides
what counts as a system library and walks the closure) because that logic is
what stands between a working DMG and an app whose FT8 mode fails to dlopen on
a Mac that has no /opt/local.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMON = REPO_ROOT / "packaging" / "common"
SERVICE_LAYER = COMMON / "mrrc-service-layer.sh"
PACKAGES = COMMON / "packages.txt"
UPDATE_SH = REPO_ROOT / "linux" / "mrrc_update.sh"
MAC_BUILD = REPO_ROOT / "packaging" / "macos" / "build.sh"
WIN_BUILD = REPO_ROOT / "packaging" / "windows" / "build.ps1"
SPEC = REPO_ROOT / "packaging" / "pyinstaller" / "mrrc_modern_server.spec"
BUNDLE_PY = REPO_ROOT / "packaging" / "macos" / "ft8_bundle.py"


def _load_bundle_module():
    spec = importlib.util.spec_from_file_location("ft8_bundle", BUNDLE_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ft8_bundle"] = module
    spec.loader.exec_module(module)
    return module


class LinuxImageToolchainTests(unittest.TestCase):
    """Both image lines get the DSP compiled during the build (design D-1)."""

    def test_packages_txt_carries_the_dsp_toolchain(self):
        text = PACKAGES.read_text(encoding="utf-8")
        for package in ("gfortran", "g++", "cmake", "pkg-config", "libfftw3-dev"):
            self.assertIn(f"\n{package}\n", text,
                          f"{package} is missing: the images build the DSP, so the "
                          "toolchain has to be in the shared package list")

    def test_packages_txt_does_not_pull_boost_back_in(self):
        """Boost is vendored (138 MB per image otherwise) — see design D-2."""
        text = PACKAGES.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            self.assertNotIn("boost", line,
                             "libboost-dev is back in the image package list; the two "
                             "headers the DSP needs are vendored under vendor-boost/")

    def test_service_layer_builds_the_library_and_refuses_to_ship_without_it(self):
        text = SERVICE_LAYER.read_text(encoding="utf-8")
        self.assertIn("ft8/dsp/build.sh", text)
        self.assertIn("libwsjt_core.so", text)
        # The assert is the point: an image whose DSP did not build must fail the
        # build, not boot into a UI whose FT8 menu cannot do anything.
        self.assertRegex(
            text,
            r"\[ -f \"\$MRRC_HOME/ft8/dsp/build/libwsjt_core\.so\" \] \|\| \{",
        )

    def test_service_layer_builds_before_it_hands_the_tree_to_the_service_user(self):
        """The build writes into $MRRC_HOME; the chown has to come after it."""
        text = SERVICE_LAYER.read_text(encoding="utf-8")
        build = text.index("ft8/dsp/build.sh")
        chown = text.index('chown -R "$MRRC_USER:$MRRC_USER" "$MRRC_HOME"')
        self.assertLess(build, chown)

    def test_updater_rebuilds_the_library(self):
        text = UPDATE_SH.read_text(encoding="utf-8")
        self.assertIn("ft8/dsp/build.sh", text,
                      "an update that changes ft8/dsp must rebuild the .so, or the box "
                      "runs new Python against the previous library")

    def test_updater_does_not_let_a_broken_dsp_kill_the_update(self):
        """Images before 1.27.0 have no compiler; SSB must still update."""
        text = UPDATE_SH.read_text(encoding="utf-8")
        self.assertRegex(text, r"if ! bash \"\$MRRC_HOME/ft8/dsp/build\.sh\"; then",
                         "the rebuild must be soft-failed with a warning, not abort "
                         "the update half-applied")


class MacPackagingTests(unittest.TestCase):
    def test_build_script_builds_and_bundles_the_library(self):
        text = MAC_BUILD.read_text(encoding="utf-8")
        self.assertIn("ft8/dsp/build.sh", text)
        self.assertIn("ft8_bundle.py", text)
        self.assertIn("libwsjt_core.dylib", text)

    def test_build_script_stops_when_the_library_is_missing(self):
        text = MAC_BUILD.read_text(encoding="utf-8")
        self.assertRegex(
            text,
            r"\[ -f \"\$FT8_LIB\" \] \|\| \{",
            "a DMG whose dylib did not build must fail the build",
        )


class MacBundleHelperTests(unittest.TestCase):
    """The closure walk and the rewrite, against recorded otool output."""

    def setUp(self):
        self.mod = _load_bundle_module()

    def test_parses_otool_output(self):
        text = (
            "/tmp/libwsjt_core.dylib:\n"
            "\t@rpath/libwsjt_core.dylib (compatibility version 0.0.0, current version 0.0.0)\n"
            "\t/opt/local/lib/libfftw3f_threads.3.dylib (compatibility version 11.0.0, "
            "current version 11.11.0)\n"
            "\t/usr/lib/libc++.1.dylib (compatibility version 1.0.0, current version 210.0.0)\n"
        )
        self.assertEqual(
            self.mod.parse_otool(text),
            ["@rpath/libwsjt_core.dylib",
             "/opt/local/lib/libfftw3f_threads.3.dylib",
             "/usr/lib/libc++.1.dylib"],
        )

    def test_system_paths_are_not_bundled(self):
        for path in ("/usr/lib/libSystem.B.dylib", "/System/Library/Frameworks/X",
                     "/Library/Apple/usr/lib/libX.dylib", "@rpath/libX.dylib",
                     "@loader_path/libfftw3f.3.dylib"):
            self.assertTrue(self.mod.is_system_path(path), path)
        for path in ("/opt/local/lib/libfftw3f.3.dylib", "/usr/local/lib/libX.dylib"):
            self.assertFalse(self.mod.is_system_path(path), path)

    def test_closure_is_transitive(self):
        """libgfortran pulls libquadmath; libgomp pulls libgcc_s."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "libwsjt_core.dylib").write_bytes(b"")
            (root / "libfftw3f.3.dylib").write_bytes(b"")
            (root / "libgfortran.5.dylib").write_bytes(b"")
            (root / "libquadmath.0.dylib").write_bytes(b"")

            # Install names are the temp paths here: collect_closure stats each
            # dependency, so the same strings have to name real files.  The
            # parsing and the walk do not care that they are not /opt/local.
            otool = {
                root / "libwsjt_core.dylib": [
                    root / "libfftw3f.3.dylib",
                    root / "libgfortran.5.dylib",
                    "/usr/lib/libSystem.B.dylib",
                ],
                root / "libfftw3f.3.dylib": ["/usr/lib/libSystem.B.dylib"],
                root / "libgfortran.5.dylib": [
                    root / "libquadmath.0.dylib",
                    "/usr/lib/libSystem.B.dylib",
                ],
                root / "libquadmath.0.dylib": ["/usr/lib/libSystem.B.dylib"],
            }

            def run(command):
                lines = [f"{command[-1]}:"]
                lines += [f"\t{p} (compatibility version 1.0.0)"
                          for p in otool[Path(command[-1])]]
                return "\n".join(lines)

            closure = self.mod.collect_closure(Path(root / "libwsjt_core.dylib"), run)
            self.assertEqual(
                sorted(Path(p).name for p in closure.values()),
                ["libfftw3f.3.dylib", "libgfortran.5.dylib", "libquadmath.0.dylib"],
            )

    def test_missing_dependency_is_a_hard_error(self):
        def run(command):
            return "\t/opt/local/lib/libnope.dylib (compatibility version 1.0.0)\n"

        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "libwsjt_core.dylib"
            library.write_bytes(b"")
            with self.assertRaises(SystemExit):
                self.mod.collect_closure(library, run)

    def test_check_reports_references_outside_the_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)

            def make_run(output):
                return lambda command: output

            (dest / "libwsjt_core.dylib").write_bytes(b"")
            bad = self.mod.check(
                dest,
                make_run("\t/opt/local/lib/libfftw3f.3.dylib (compatibility version 11.0.0)\n"),
            )
            self.assertEqual(bad, ["libwsjt_core.dylib: /opt/local/lib/libfftw3f.3.dylib"])

            good = self.mod.check(
                dest,
                make_run("\t@loader_path/libfftw3f.3.dylib (compatibility version 11.0.0)\n"
                         "\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0)\n"),
            )
            self.assertEqual(good, [])


class WindowsPackagingTests(unittest.TestCase):
    def test_build_script_builds_the_dll_with_minwg(self):
        text = WIN_BUILD.read_text(encoding="utf-8")
        self.assertIn("ft8\\dsp", text)
        self.assertIn("gfortran.exe", text)
        self.assertIn("build\\ft8\\windows", text)

    def test_build_script_stages_the_runtime_closure(self):
        text = WIN_BUILD.read_text(encoding="utf-8")
        # The closure is walked, not hard-coded: MSYS2 is rolling and a new
        # link-time dependency would otherwise ship as an unloadable package.
        self.assertIn("objdump.exe", text)
        self.assertIn("DLL Name:", text)


class PyInstallerSpecTests(unittest.TestCase):
    def test_spec_bundles_the_native_library_at_the_app_root(self):
        text = SPEC.read_text(encoding="utf-8")
        self.assertIn('"build" / "ft8"', text)
        self.assertIn('"."', text)

    def test_spec_names_the_ft8_python_dependencies(self):
        text = SPEC.read_text(encoding="utf-8")
        for module in ("ft8", "scipy", "soxr", "ft8.core.worker", "ft8.engine.orchestrator"):
            self.assertIn(f'"{module}"', text,
                          f"{module} must be a hidden import: it is imported lazily "
                          "or from inside a function, exactly the shape that shipped "
                          "missing in v1.24.0")


if __name__ == "__main__":
    unittest.main()


class FrozenWorkerSpawnTests(unittest.TestCase):
    """The FT8 DSP worker must be able to start inside a frozen bundle.

    The worker is a ``multiprocessing`` "spawn" child.  In a PyInstaller bundle
    the child is the *same executable* re-launched with
    ``--multiprocessing-fork``, so the entry point has to run
    ``multiprocessing.freeze_support()`` before anything reads argv.  Without it
    the child ran ``main()``, argparse printed its usage and exited 2, and the
    page offered an FT8 mode that reported "worker did not become ready" — see
    the clean-room run of the packaged macOS app, 2026-10-13.
    """

    def test_entry_point_freezes_support_before_main(self):
        import ast

        tree = ast.parse((REPO_ROOT / "server.py").read_text(encoding="utf-8"))
        block = None
        for node in tree.body:
            if not isinstance(node, ast.If):
                continue
            test = ast.dump(node.test)
            if "__main__" in test and "name" in test:
                block = node
        self.assertIsNotNone(block, "server.py has no __main__ block")

        calls: list[str] = []
        for node in ast.walk(block):
            if isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Attribute):
                    calls.append(target.attr)
                elif isinstance(target, ast.Name):
                    calls.append(target.id)
        self.assertIn(
            "freeze_support", calls,
            "multiprocessing.freeze_support() must run in the entry point, or a "
            "frozen bundle's DSP worker child re-enters main() and exits",
        )
        self.assertLess(calls.index("freeze_support"), calls.index("main"),
                        "freeze_support() has to run before main()")
