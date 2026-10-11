"""Structural tests for the Raspberry Pi image packaging (mirrors
test_windows_packaging_files.py).

Since 2026-10-11 the Pi stage is a thin shell over ``packaging/common/``: the
systemd units, the first-boot wrapper and the password helper are generated once
for both image lines, so these tests point at the shared files and assert only
the Pi-specific bindings (boot partition, bind address, the call into the shared
service layer).
"""
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RPI = REPO_ROOT / "packaging" / "rpi"
STAGE = RPI / "pi-gen-stage4"
COMMON = REPO_ROOT / "packaging" / "common"
SERVICE_LAYER = COMMON / "mrrc-service-layer.sh"


class RpiPackagingFilesTests(unittest.TestCase):
    def test_stage_layout_exists(self):
        self.assertTrue((STAGE / "prerun.sh").is_file())
        self.assertIn("copy_previous", (STAGE / "prerun.sh").read_text(encoding="utf-8"))
        self.assertTrue((STAGE / "00-install-packages" / "00-packages").is_file())
        self.assertTrue((STAGE / "01-deploy-mrrc" / "00-run-chroot.sh").is_file())

    def test_the_stage_hands_over_to_the_shared_service_layer(self):
        """The Pi stage creates the Pi's conditions and nothing else.

        It used to build the virtualenv itself (``python3 -m venv`` + pip), which
        made a second dependency install to keep in step with install.sh's and a
        second place for the units to drift from the box's.
        """
        rc = (STAGE / "01-deploy-mrrc" / "00-run-chroot.sh").read_text(encoding="utf-8")
        self.assertIn("mrrc-service-layer.sh", rc)
        self.assertIn(". /opt/mrrc-build/build.env", rc)
        self.assertNotIn("python3 -m venv", rc,
                         "the venv belongs to install.sh, which owns the dependency list")

    def test_service_units_pin_user_and_env(self):
        """The one unit template, which both images install verbatim."""
        unit = SERVICE_LAYER.read_text(encoding="utf-8")
        self.assertIn("User=$MRRC_USER", unit)
        self.assertIn("WorkingDirectory=$MRRC_HOME", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("Type=oneshot", unit)
        self.assertIn("ConditionPathExists=!/var/lib/mrrc/firstboot-done", unit)
        # D-11: the same file twice, or Cloud Hub writes where the service
        # does not read and the UI reports 已连接 while upstream TLS fails.
        self.assertIn("EnvironmentFile=$ENV_FILE", unit)
        self.assertIn("Environment=MRRC_CONFIG_FILE=$ENV_FILE", unit)
        # The two platform facts, passed into the firstboot unit because the
        # wrapper cannot guess either one.
        self.assertIn("Environment=MRRC_BOOT_DIR=$MRRC_BOOT_DIR", unit)
        self.assertIn("Environment=MRRC_WEB_HOST=$MRRC_WEB_HOST", unit)

    def test_build_and_verify_scripts_exist(self):
        for name in ("build-image.sh", "verify-image.sh"):
            p = RPI / name
            self.assertTrue(p.is_file(), name)
            self.assertIn("set -euo pipefail", p.read_text(encoding="utf-8"))

    def test_password_helper_requires_root_path(self):
        helper = (COMMON / "mrrc-show-password").read_text(encoding="utf-8")
        self.assertIn("/opt/mrrc_modern/env/mrrc.env", helper)
        self.assertIn("MRRC_WEB_PASSWORD", helper)

    def test_firstboot_wrapper_handles_preseed_and_cert(self):
        w = (COMMON / "firstboot_wrapper.py").read_text(encoding="utf-8")
        # The boot partition is a parameter now: /boot on the box, /boot/firmware
        # here. A literal would be the second copy this file exists to prevent.
        self.assertIn("MRRC_BOOT_DIR", w)
        self.assertIn("ensure_self_signed", w)
        self.assertIn("firstboot-done", w)
        self.assertIn("mrrc.env.applied", w)


class FirstbootPreseedEncodingTests(unittest.TestCase):
    """The preseed travels on a SD card — the first boot must not die on it.

    Field class 2026-09-12 (Windows launcher): a config saved by an ANSI (GBK)
    editor is not valid UTF-8.  Here the file is written on the operator's PC
    and copied into the boot partition, so a strict read in adopt_preseed() would
    fail mrrc-firstboot.service on the very first boot.
    """

    def _load_wrapper(self):
        import importlib.util
        import sys
        from typing import Any, cast
        sys.path.insert(0, str(REPO_ROOT / "linux"))       # sibling first_run.py
        path = COMMON / "firstboot_wrapper.py"
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


class FirstbootCertPermissionTests(unittest.TestCase):
    """``ensure_cert()`` runs as root; the server does not.

    ssl_bootstrap writes the key 0600 and the wrapper used to hand only the env
    directory to ``mrrc`` — so on the first real box the service died with
    ``PermissionError`` inside uvicorn's load_cert_chain and restarted every 3 s
    with nothing but a traceback in server-stdout.log (2026-10-11). There is one
    wrapper now (packaging/common/), and it must give the service user the
    certificate.
    """

    WRAPPER = COMMON / "firstboot_wrapper.py"

    def _calls(self, path, name):
        import importlib.util
        import sys as _sys
        import tempfile
        from unittest import mock
        from typing import Any, cast
        _sys.path.insert(0, str(REPO_ROOT / "linux"))       # sibling first_run.py
        spec = cast(Any, importlib.util.spec_from_file_location(name, path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp:
            cert_dir = Path(tmp)
            with mock.patch.object(mod, "CERT_DIR", cert_dir), \
                 mock.patch.object(mod.subprocess, "run") as run:
                mod.ensure_cert()
            return [list(call.args[0]) for call in run.call_args_list], cert_dir

    def test_the_certificate_belongs_to_the_service_user(self):
        calls, cert_dir = self._calls(self.WRAPPER, "mrrc_firstboot_wrapper")
        self.assertIn(["chown", "-R", "mrrc:mrrc", str(cert_dir)], calls)
        self.assertIn(["chmod", "600", str(cert_dir / "server.key")], calls)
        self.assertIn(["chmod", "644", str(cert_dir / "server.crt")], calls)
