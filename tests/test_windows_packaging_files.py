import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsPackagingFilesTests(unittest.TestCase):
    def test_pyinstaller_specs_use_repo_root(self):
        for spec in (
            ROOT / "packaging" / "pyinstaller" / "ft710_server.spec",
            ROOT / "packaging" / "pyinstaller" / "scope_pipe.spec",
            ROOT / "packaging" / "pyinstaller" / "ft710_launcher.spec",
        ):
            text = spec.read_text(encoding="utf-8")
            self.assertIn("ROOT = Path(SPECPATH).parents[1]", text)

    def test_build_script_runs_all_packaging_steps(self):
        text = (ROOT / "packaging" / "windows" / "build.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("scope_pipe.spec", text)
        self.assertIn("ft710_server.spec", text)
        self.assertIn("ft710_launcher.spec", text)
        self.assertIn("iscc packaging\\windows\\MRRC-FT710.iss", text)
        self.assertIn("vendor\\opus\\windows", text)
        self.assertIn("opus.dll", text)

    def test_build_script_aborts_on_native_command_failure(self):
        """$ErrorActionPreference does not cover native commands — the build
        must check $LASTEXITCODE so failed tests/builds abort packaging."""
        text = (ROOT / "packaging" / "windows" / "build.ps1").read_text(
            encoding="utf-8"
        )
        self.assertIn("$LASTEXITCODE", text)
        self.assertIn("Invoke-Checked python -m unittest", text)
        self.assertIn("Invoke-Checked pyinstaller", text)


if __name__ == "__main__":
    unittest.main()
