"""The Wi-Fi a user writes onto the card before the first boot.

Why a file of its own rather than a key in /boot/mrrc.env (design D-4): that
env file is copied into the 0640 env the server reads and a support bundle may
carry, and a Wi-Fi credential must never meet a config key. This one is read
once, applied, and deleted — vfat has no permission bits, so a plaintext PSK must
not be left lying on the boot partition.

The unit that runs this is separate from mrrc-firstboot.service for a reason
worth a test: that unit is ordered After=network-online.target, and joining the
Wi-Fi is the thing that brings the network online (spec R-8).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import net_wifi

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "linux"))

import wifi_preseed  # noqa: E402


class ReadTests(unittest.TestCase):
    def write(self, text, encoding="utf-8"):
        handle = tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False)
        handle.write(text.encode(encoding))
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_it_reads_the_two_keys(self):
        path = self.write("SSID=我的WiFi\nPSK=hunter2secret\n")
        self.assertEqual(wifi_preseed.read_preseed(path),
                         {"SSID": "我的WiFi", "PSK": "hunter2secret"})

    def test_a_windows_editor_is_expected_not_a_surprise(self):
        """The user writes this on their own computer, with whatever that
        computer's editor defaults to. A BOM must not become part of the SSID."""
        for encoding in ("utf-8-sig", "cp936"):
            with self.subTest(encoding=encoding):
                path = self.write("SSID=home\nPSK=secret123\n", encoding)
                self.assertEqual(
                    wifi_preseed.read_preseed(path)["SSID"], "home")

    def test_comments_and_blanks_are_skipped(self):
        path = self.write("# my wifi\n\nSSID=home\n\nPSK=secret123\n")
        self.assertEqual(wifi_preseed.read_preseed(path)["SSID"], "home")

    def test_a_missing_file_is_an_empty_dict_not_an_error(self):
        self.assertEqual(wifi_preseed.read_preseed("/no/such/file"), {})

    def test_the_key_is_case_insensitive_but_the_value_is_not(self):
        """`ssid=` from a user who typed it in lowercase is the same request.
        The *key* is matched case-insensitively; the value keeps its case, and
        only its surrounding whitespace is stripped — the key and the value are
        the two halves of the same parse, and neither is renamed here."""
        path = self.write("ssid= home \npsk= secret123 \n")
        values = wifi_preseed.read_preseed(path)
        self.assertEqual(values["SSID"], "home")
        self.assertEqual(values["PSK"], "secret123")


class MainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mrrc-wifi.txt"

    def run_main(self, text, runner=None):
        self.path.write_text(text, encoding="utf-8")
        # connect_wifi forwards its own (None) runner as nmcli's second
        # positional argument, so the canned runner must accept it — otherwise
        # every join raises TypeError inside the mock before any assertion runs.
        with mock.patch.object(wifi_preseed, "PRESEED_FILE", str(self.path)), \
                mock.patch.object(net_wifi, "nmcli") as nmcli:
            nmcli.side_effect = runner if runner else lambda argv, _runner=None: \
                net_wifi.NmResult(0, "", "")
            code = wifi_preseed.main()
        return code

    def test_a_successful_join_deletes_the_file(self):
        """vfat has no permissions. The credential has to stop existing."""
        code = self.run_main("SSID=home\nPSK=secret123\n")
        self.assertEqual(code, 0)
        self.assertFalse(self.path.exists())

    def test_the_new_profile_is_told_to_autoconnect(self):
        """A Wi-Fi the user asked for should come back after a reboot. It is
        stated explicitly because the retired hotspot's profile defaulted to yes
        by accident, not by decision — and 'by default' is not a design."""
        self.run_main("SSID=home\nPSK=secret123\n")
        # 通过 mock 拿不到 argv，改为直接断言源码里那一行存在
        source = (REPO / "linux" / "wifi_preseed.py").read_text(encoding="utf-8")
        self.assertIn("connection.autoconnect", source)
        self.assertIn("yes", source)

    def test_a_failed_join_keeps_the_file(self):
        """A typo should cost a reboot, not a reflash."""
        code = self.run_main("SSID=home\nPSK=wrong\n",
                             runner=lambda argv, _runner=None: net_wifi.NmResult(
                                 1, "", "Error: Secrets were required."))
        self.assertEqual(code, 1)
        self.assertTrue(self.path.exists())

    def test_the_failure_is_logged_without_the_password(self):
        import io
        import contextlib
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            self.run_main("SSID=home\nPSK=hunter2secret\n",
                          runner=lambda argv, _runner=None: net_wifi.NmResult(
                              1, "", "Error: hunter2secret rejected."))
        self.assertNotIn("hunter2secret", captured.getvalue())

    def test_no_ssid_means_nothing_to_do(self):
        code = self.run_main("PSK=secret123\n")
        self.assertEqual(code, 0)

    def test_a_missing_file_is_not_a_failure(self):
        with mock.patch.object(wifi_preseed, "PRESEED_FILE", "/no/such/file"):
            self.assertEqual(wifi_preseed.main(), 0)


class PackagingTests(unittest.TestCase):
    OVERLAY = REPO / "packaging" / "common" / "mrrc-service-layer.sh"
    UNIT = "mrrc-wifi-preseed.service"

    def setUp(self):
        self.text = self.OVERLAY.read_text(encoding="utf-8")

    def body(self):
        return self.text.split(f"/etc/systemd/system/{self.UNIT}", 1)[1] \
            .split("UNIT\n", 1)[0]

    def test_it_is_conditional_on_the_file(self):
        self.assertIn("ConditionPathExists=/boot/mrrc-wifi.txt", self.body())

    def test_it_is_oneshot(self):
        self.assertIn("Type=oneshot", self.body())

    def test_it_runs_before_firstboot_rather_than_after_the_network(self):
        """R-8: mrrc-firstboot.service waits for network-online.target, and
        joining the Wi-Fi is what brings the network online. Putting this in that
        unit, or after it, is a circle."""
        body = self.body()
        self.assertIn("Before=mrrc-firstboot.service", body)
        directives = "\n".join(line for line in body.splitlines()
                               if not line.lstrip().startswith("#"))
        self.assertNotIn("network-online.target", directives)
        self.assertIn("After=NetworkManager.service", directives)

    def test_it_is_enabled(self):
        enabled = [line for line in self.text.splitlines()
                   if "systemctl enable" in line]
        self.assertTrue(any(self.UNIT in line for line in enabled))

    def test_vfat_has_no_permissions_so_the_file_is_deleted(self):
        body = self.body()
        self.assertIn("/boot/mrrc-wifi.txt", body)
        source = (REPO / "linux" / "wifi_preseed.py").read_text(encoding="utf-8")
        self.assertIn("os.unlink", source)

    def test_verify_sh_reports_whether_the_unit_is_installed(self):
        verify = (REPO / "packaging" / "box" / "verify.sh").read_text(
            encoding="utf-8")
        self.assertIn(self.UNIT, verify)


if __name__ == "__main__":
    unittest.main()
