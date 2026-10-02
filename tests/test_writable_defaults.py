"""No writable default may point inside the program directory.

The class of bug this prevents, measured on real machines three times in one day: a default that
only made sense on the build machine (a certificate path, a log directory, a serial port) shipped in
the package, and on a normal user's machine it was either missing or read-only. The symptom was a
console full of warnings, no logs, a browser showing a protocol error, and a radio that was never
found - each of which cost a release to find.

Anything this program writes to at runtime must default to a directory the user owns.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class WritableDefaultsTests(unittest.TestCase):
    def _program_dir(self) -> Path:
        import server

        return Path(server.__file__).resolve().parent

    def test_runtime_paths_are_outside_the_program_directory(self):
        import config
        import server

        program = self._program_dir()
        candidates = {
            "LOG_DIR": server.LOG_DIR,
            "SUPPORT_OUT_DIR": server.SUPPORT_OUT_DIR,
            "MEM_FILE": server.MEM_FILE,
            "RECORDINGS_DIR": server.RECORDINGS_DIR,
            "CERT_DIR": config.CERT_DIR,
            "SSL_CERTFILE": config.SSL_CERTFILE,
            "SSL_KEYFILE": config.SSL_KEYFILE,
        }
        frozen = bool(getattr(sys, "frozen", False))
        for name, value in candidates.items():
            path = Path(value).resolve()
            inside = program == path or program in path.parents
            if not inside:
                continue
            if frozen:
                self.fail(f"{name} defaults to {path}, inside the program directory {program} - "
                          f"a packaged install cannot write there")
            # Source mode may keep its state in the repository, but only where it can write:
            # the rule the code claims is "never default to a directory this process cannot write".
            probe = path if path.is_dir() else path.parent
            self.assertTrue(os.access(probe, os.W_OK),
                            f"{name} defaults to {path}, inside the program directory and not writable")

    def test_a_stripped_environment_still_yields_a_usable_path(self):
        """The launcher seeds the environment; a bare start has almost nothing set."""
        saved = dict(os.environ)
        try:
            for key in ("LOCALAPPDATA", "APPDATA", "USERPROFILE", "HOME", "XDG_DATA_HOME", "TMPDIR", "TEMP"):
                os.environ.pop(key, None)
            import importlib

            import config

            importlib.reload(config)
            path = Path(config.CERT_DIR)
            self.assertTrue(path.is_absolute(), f"CERT_DIR is not absolute: {path}")
        finally:
            os.environ.clear()
            os.environ.update(saved)
            import importlib

            import config

            importlib.reload(config)


if __name__ == "__main__":
    unittest.main()
