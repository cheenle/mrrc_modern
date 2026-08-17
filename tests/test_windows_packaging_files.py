import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
        self.assertIn("iscc packaging\\windows\\MRRC-Modern.iss", text)
        self.assertIn("vendor\\opus\\windows", text)
        self.assertIn("opus.dll", text)
        self.assertIn("MRRC-Modern-Server", text)
        self.assertIn("MRRC-Modern-Launcher", text)

    def test_build_script_aborts_on_native_command_failure(self):
        """$ErrorActionPreference does not cover native commands — the build
        must check $LASTEXITCODE so failed tests/builds abort packaging."""
        text = (ROOT / "packaging" / "windows" / "build.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$LASTEXITCODE", text)
        self.assertIn("Invoke-Checked python -m unittest", text)
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
