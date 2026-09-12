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
        rc = STAGE / "01-deploy-mrrc" / "00-run-chroot.sh"
        self.assertTrue(rc.is_file())
        self.assertIn("python3 -m venv /opt/mrrc_modern/venv", rc.read_text(encoding="utf-8"))
        self.assertIn("systemctl enable mrrc-firstboot.service", rc.read_text(encoding="utf-8"))

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


class FirstbootPreseedEncodingTests(unittest.TestCase):
    """The preseed travels on a SD card — the first boot must not die on it.

    Field class 2026-09-12 (Windows launcher): a config saved by an ANSI (GBK)
    editor is not valid UTF-8.  Here the file is written on the operator's PC
    and copied into /boot/firmware/, so a strict read in adopt_preseed() would
    fail mrrc-firstboot.service on the very first boot.
    """

    def _load_wrapper(self):
        import importlib.util
        import sys
        from typing import Any, cast
        sys.path.insert(0, str(REPO_ROOT / "linux"))       # sibling first_run.py
        path = (STAGE / "01-deploy-mrrc" / "files" / "opt" / "mrrc_modern"
                / "linux" / "firstboot_wrapper.py")
        spec = cast(Any, importlib.util.spec_from_file_location(
            "mrrc_firstboot_wrapper", path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _adopt(self, raw):
        import tempfile
        from unittest import mock
        mod = self._load_wrapper()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            preseed = tmp / "mrrc.env"
            preseed.write_bytes(raw)
            envdir = tmp / "env"
            with mock.patch.object(mod, "PRESEED", preseed), \
                 mock.patch.object(mod, "ENV_DIR", envdir), \
                 mock.patch.object(mod, "ENV_FILE", envdir / "mrrc.env"):
                adopted = mod.adopt_preseed()
            return adopted, (envdir / "mrrc.env").read_bytes()

    def test_gbk_preseed_is_adopted_as_utf8(self):
        name = "麦克风 (USB Audio CODEC)"
        raw = (f"MRRC_WEB_PASSWORD=secret\nMRRC_AUDIO_RX_DEVICE={name}\n").encode("cp936")
        adopted, data = self._adopt(raw)
        self.assertTrue(adopted)
        text = data.decode("utf-8")                        # must be valid UTF-8
        self.assertIn("MRRC_WEB_PASSWORD=secret", text)
        self.assertIn(name, text)

    def test_damaged_preseed_from_the_field_report_is_adopted(self):
        raw = (b"# mrrc_modern.env \xe2\x80?MRRC Modern launcher\n"
               b"MRRC_WEB_PASSWORD=secret\n")
        adopted, data = self._adopt(raw)
        self.assertTrue(adopted)
        self.assertIn("MRRC_WEB_PASSWORD=secret", data.decode("utf-8"))
