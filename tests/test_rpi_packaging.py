"""Structural tests for the Raspberry Pi image packaging (mirrors
test_windows_packaging_files.py)."""
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RPI = REPO_ROOT / "packaging" / "rpi"
STAGE = RPI / "pi-gen-stage4"


class RpiPackagingFilesTests(unittest.TestCase):
    def test_stage_layout_exists(self):
        self.assertTrue((STAGE / "prerun.sh").is_file())
        self.assertIn("copy_previous", (STAGE / "prerun.sh").read_text(encoding="utf-8"))
        self.assertTrue((STAGE / "00-install-packages" / "00-packages").is_file())
        self.assertTrue((STAGE / "01-deploy-mrrc" / "00-run.sh").is_file())

    def test_service_units_pin_user_and_env(self):
        svc = (STAGE / "01-deploy-mrrc" / "files" / "etc" / "systemd" / "system"
               / "mrrc-modern.service").read_text(encoding="utf-8")
        self.assertIn("User=mrrc", svc)
        self.assertIn("EnvironmentFile=/opt/mrrc_modern/env/mrrc.env", svc)
        self.assertIn("Restart=on-failure", svc)
        self.assertIn("WorkingDirectory=/opt/mrrc_modern", svc)
        fb = (STAGE / "01-deploy-mrrc" / "files" / "etc" / "systemd" / "system"
              / "mrrc-firstboot.service").read_text(encoding="utf-8")
        self.assertIn("ConditionPathExists=!/var/lib/mrrc/firstboot-done", fb)
        self.assertIn("Type=oneshot", fb)

    def test_build_and_verify_scripts_exist(self):
        for name in ("build-image.sh", "verify-image.sh"):
            p = RPI / name
            self.assertTrue(p.is_file(), name)
            self.assertIn("set -euo pipefail", p.read_text(encoding="utf-8"))

    def test_password_helper_requires_root_path(self):
        helper = (STAGE / "01-deploy-mrrc" / "files" / "usr" / "local" / "bin"
                  / "mrrc-show-password").read_text(encoding="utf-8")
        self.assertIn("/opt/mrrc_modern/env/mrrc.env", helper)
        self.assertIn("MRRC_WEB_PASSWORD", helper)

    def test_firstboot_wrapper_handles_preseed_and_cert(self):
        w = (STAGE / "01-deploy-mrrc" / "files" / "opt" / "mrrc_modern" / "linux"
             / "firstboot_wrapper.py").read_text(encoding="utf-8")
        self.assertIn("/boot/firmware/mrrc.env", w)
        self.assertIn("ensure_self_signed", w)
        self.assertIn("firstboot-done", w)
        self.assertIn("mrrc.env.applied", w)
