import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class IcomProfileHiddenImportTests(unittest.TestCase):
    """PyInstaller must bundle the per-model profile module.

    The frozen server imports backends.ic7300.civ_profiles lazily via the
    backend module; without an explicit hiddenimport the packaged build
    would fail at runtime for every Icom model.
    """

    def test_civ_profiles_is_bundled(self):
        spec = (ROOT / "packaging" / "pyinstaller"
                / "mrrc_modern_server.spec").read_text(encoding="utf-8")
        self.assertIn("backends.ic7300.civ_profiles", spec)

    def test_every_ic7300_module_is_bundled(self):
        import backends.ic7300 as pkg
        spec = (ROOT / "packaging" / "pyinstaller"
                / "mrrc_modern_server.spec").read_text(encoding="utf-8")
        for name in ("backend", "civ_codec", "civ_controller", "civ_profiles",
                     "civ_scope", "config_ic7300"):
            with self.subTest(module=name):
                self.assertIn(f"backends.ic7300.{name}", spec)
        self.assertTrue(pkg.__name__)


class RecordingDependencyPackagingTests(unittest.TestCase):
    """The MP3 encoder must ship in the installers (spec §8)."""

    def test_lameenc_is_in_requirements(self):
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("lameenc", text)

    def test_pyinstaller_collects_the_lameenc_extension(self):
        spec = (ROOT / "packaging" / "pyinstaller"
                / "mrrc_modern_server.spec").read_text(encoding="utf-8")
        # lameenc is a single compiled extension module (no submodules).
        self.assertIn('"lameenc"', spec)

    def test_encoder_imports_on_this_host(self):
        import lameenc
        self.assertTrue(callable(lameenc.Encoder))


class WindowsPackagingFilesTests(unittest.TestCase):
    def test_pyinstaller_specs_use_repo_root(self):
        for spec in (
            ROOT / "packaging" / "pyinstaller" / "mrrc_modern_server.spec",
            ROOT / "packaging" / "pyinstaller" / "scope_pipe.spec",
            ROOT / "packaging" / "pyinstaller" / "mrrc_modern_launcher.spec",
        ):
            text = spec.read_text(encoding="utf-8")
            self.assertIn("ROOT = Path(SPECPATH).parents[1]", text)

    def test_build_script_runs_all_packaging_steps(self):
        text = (ROOT / "packaging" / "windows" / "build.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("scope_pipe.spec", text)
        self.assertIn("mrrc_modern_server.spec", text)
        self.assertIn("mrrc_modern_launcher.spec", text)
        self.assertIn("iscc", text)
        # 出到临时目录再复制：真机上 iscc 会被实时杀毒锁住输出文件（Error 32）
        self.assertIn('"/O$scratch"', text)
        self.assertIn("Copy-Item", text)
        self.assertIn("vendor\\opus\\windows", text)
        self.assertIn("opus.dll", text)
        self.assertIn("MRRC-Modern-Server", text)
        self.assertIn("MRRC-Modern-Launcher", text)

    def test_build_script_purges_the_pyinstaller_workpath_first(self):
        """PyInstaller re-analyzes a module only when its (size, mtime) changed,
        and a tree shipped as a tar keeps the build Mac's mtimes — so a CHANGED
        file can look older than the cache. v1.25.3 froze the pre-fix server
        entry exactly that way (only COLLECT-00.toc was rewritten; version.txt,
        the test gate and iscc all looked green). The workpath must be removed
        before PyInstaller runs, as packaging/macos/build.sh already does."""
        text = (ROOT / "packaging" / "windows" / "build.ps1").read_text(
            encoding="utf-8"
        )
        first_invocation = text.index("Invoke-Checked pyinstaller")
        self.assertIn(
            'Remove-Item "build\\pyinstaller"', text[:first_invocation]
        )

    def test_build_script_aborts_on_native_command_failure(self):
        """$ErrorActionPreference does not cover native commands — the build
        must check $LASTEXITCODE so failed tests/builds abort packaging."""
        text = (ROOT / "packaging" / "windows" / "build.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$LASTEXITCODE", text)
        # 2026-09-17: PowerShell 5.1 turns a native command's stderr output into
        # a NativeCommandError, which $ErrorActionPreference="Stop" then escalates —
        # and unittest writes everything to stderr. Hence Start-Process with
        # explicit redirects, plus both an exit-code and a FAIL/ERROR check.
        self.assertIn("-RedirectStandardError $utErr", text)
        self.assertIn("$utExit = $utProc.ExitCode", text)
        self.assertIn("-Pattern '^(FAIL|ERROR): '", text)
        self.assertIn("Invoke-Checked pyinstaller", text)

    def test_inno_setup_script_uses_modern_branding(self):
        text = (ROOT / "packaging" / "windows" / "MRRC-Modern.iss").read_text(
            encoding="utf-8"
        )
        self.assertIn('MyAppName "MRRC Modern"', text)
        self.assertIn('MyAppPublisher "cheenle"', text)
        self.assertIn("github.com/cheenle/mrrc_modern", text)
        self.assertIn("MRRC-Modern-Server.exe", text)
        self.assertIn("MRRC-Modern-Launcher.exe", text)
        self.assertIn("MRRC_RADIO_MODEL", text)


if __name__ == "__main__":
    unittest.main()
