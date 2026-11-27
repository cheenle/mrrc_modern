"""The box-side Passport flasher: NVS encoding, catalog, ports, the flash plan.

Everything here runs without a serial port.  The NVS encoder is pinned to a
**byte-for-byte** reference produced by ESP-IDF's own `nvs_partition_gen`
(`esp-idf-nvs-partition-gen` 0.4.0, the same generator the CSV/vs-code flow uses),
so a byte of drift in the on-flash format fails the suite instead of bricking a
board.  The esptool invocations are data until an injected runner executes them,
and every test drives a fake runner.
"""
import hashlib
import json
import unittest

import passport_flash as pf

REFERENCE = {
    "ssid": "home",
    "pass": "secret1234",
    "host": "192.168.1.217",
    "srv_pass": "abcd!1234",
}
#: sha256 of the image ESP-IDF's generator produces for REFERENCE at 0x6000.
REFERENCE_SHA = "2ac628f56908d1073b87cbe147c96f7d0678fc7eba6e39166bb680c76c30eb5a"


class NvsImageTests(unittest.TestCase):
    def test_the_image_is_byte_identical_to_the_esp_idf_generator(self):
        """The one test that makes the whole feature safe to ship.

        A wrong page header, a wrong entry CRC or a wrong span is not a
        recoverable error on the device — NVS reports "no free pages" and the
        firmware falls back to its compiled-in defaults, i.e. it silently loses
        the Wi-Fi we just provisioned.  Byte equality with ESP-IDF's generator
        means the format is right for the reason the device cares about, not
        because it looks right.
        """
        image = pf.build_nvs_image(REFERENCE)
        self.assertEqual(len(image), pf.NVS_SIZE)
        self.assertEqual(hashlib.sha256(image).hexdigest(), REFERENCE_SHA,
                         "NVS layout drifted from ESP-IDF's generator")

    def test_page_header_is_active_v2(self):
        image = pf.build_nvs_image(REFERENCE)
        self.assertEqual(int.from_bytes(image[0:4], "little"), pf.PAGE_STATE_ACTIVE)
        self.assertEqual(image[8], pf.NVS_VERSION2)
        # The rest of the partition stays erased: only one page has data.
        self.assertEqual(image[pf.NVS_PAGE_SIZE:], b"\xff" * (pf.NVS_SIZE - pf.NVS_PAGE_SIZE))

    def test_round_trip(self):
        got = pf.parse_nvs_image(pf.build_nvs_image(REFERENCE))
        self.assertEqual(got, {pf.CFG_NAMESPACE: REFERENCE})

    def test_long_values_span_extra_entries_and_still_round_trip(self):
        values = {"ssid": "a-very-long-network-name", "pass": "x" * 40,
                  "host": "w3d7.mrrc.vlsc.net", "srv_pass": "p" * 30}
        self.assertEqual(pf.parse_nvs_image(pf.build_nvs_image(values)),
                         {pf.CFG_NAMESPACE: values})

    def test_missing_or_unknown_keys_are_refused(self):
        with self.assertRaises(pf.NvsError):
            pf.build_nvs_image({"ssid": "x"})
        with self.assertRaises(pf.NvsError):
            pf.build_nvs_image({**REFERENCE, "token": "abc"})
        with self.assertRaises(pf.NvsError):
            pf.build_nvs_image({**REFERENCE, "pass": 1234})

    def test_a_corrupted_entry_is_not_parsed_as_success(self):
        image = bytearray(pf.build_nvs_image(REFERENCE))
        image[pf.NVS_FIRST_ENTRY + 40] ^= 0xFF        # flip a byte inside the namespace key
        with self.assertRaises(pf.NvsError):
            pf.parse_nvs_image(bytes(image))

    def test_a_truncated_read_is_refused(self):
        with self.assertRaises(pf.NvsError):
            pf.parse_nvs_image(pf.build_nvs_image(REFERENCE)[:-16])


class CatalogTests(unittest.TestCase):
    def _doc(self, **over):
        row = {"version": "1.2.1", "url": "https://www.vlsc.net/x/merged.bin",
               "sha256": "a" * 64, "size": 1234}
        row.update(over)
        return {"builds": [row]}

    def test_a_good_catalog_parses(self):
        builds = pf.parse_catalog(json.dumps(self._doc()))
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0].version, "1.2.1")
        self.assertEqual(builds[0].chip, "esp32c3")

    def test_newest_first(self):
        doc = {"builds": [
            {"version": "1.0.0", "url": "https://a/x", "sha256": "b" * 64, "size": 1},
            {"version": "1.10.0", "url": "https://a/y", "sha256": "c" * 64, "size": 1},
            {"version": "1.9.9", "url": "https://a/z", "sha256": "d" * 64, "size": 1},
        ]}
        self.assertEqual([b.version for b in pf.parse_catalog(doc)],
                         ["1.10.0", "1.9.9", "1.0.0"])

    def test_bad_entries_are_refused_rather_than_guessed(self):
        for broken in ({"url": "http://insecure/x"},          # not https
                       {"sha256": "abc"},                     # not a sha
                       {"size": 0},
                       {"version": None}):
            with self.subTest(broken=broken), self.assertRaises(pf.CatalogError):
                pf.parse_catalog(self._doc(**broken))
        with self.assertRaises(pf.CatalogError):
            pf.parse_catalog({"builds": []})
        with self.assertRaises(pf.CatalogError):
            pf.parse_catalog(b"not json")


class PortTests(unittest.TestCase):
    def test_the_radio_port_is_never_a_candidate(self):
        ports = ["/dev/ttyACM0", "/dev/ttyUSB0", "/dev/ttyUSB1"]
        self.assertEqual(pf.candidate_ports(ports, radio_port="/dev/ttyUSB0"),
                         ["/dev/ttyACM0", "/dev/ttyUSB1"])

    def test_every_interface_of_the_radio_bridge_is_excluded(self):
        """Excluding a path is not excluding the radio.

        The FT-710's CP2105 exposes *two* ttys from one chip: if00 is CAT (what
        `cat.port` names) and if01 is the scope.  Field 2026-11-27 on the box at
        192.168.1.217: `/api/passport/state` listed `/dev/ttyUSB1` as a
        Passport candidate — that is the radio's scope port, one flash away
        from writing 1.7 MB into the radio's USB bridge.
        """
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            bid = os.path.join(tmp, "by-id")
            os.makedirs(bid)
            cp2105 = ("usb-Silicon_Labs_CP2105_Dual_USB_to_UART_Bridge_"
                      "Controller_0121DB3A")
            os.symlink("../../ttyUSB0", os.path.join(bid, cp2105 + "-if00-port0"))
            os.symlink("../../ttyUSB1", os.path.join(bid, cp2105 + "-if01-port0"))
            os.symlink("../../ttyACM0", os.path.join(
                bid, "usb-Espressif_USB_JTAG_serial_debug_unit_ABC123-if00"))

            ports = ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyACM0"]
            self.assertEqual(
                pf.candidate_ports(ports, radio_port="/dev/ttyUSB0", by_id_dir=bid),
                ["/dev/ttyACM0"],
                "the radio's second interface was offered as a Passport")

    def test_native_usb_and_bridges_are_labelled(self):
        self.assertEqual(pf.port_kind("/dev/ttyACM0"), "acm")
        self.assertEqual(pf.port_kind("/dev/cu.usbmodem1101"), "acm")
        self.assertEqual(pf.port_kind("/dev/ttyUSB0"), "usb")


class EsptoolBundleTests(unittest.TestCase):
    """The flasher must arrive with the files, not with pip.

    A hot-fix patch moves files and cannot install a Python package, so a box
    that reached this version that way had the route and `passport_flash.py`
    but no `esptool` — `/passport` answered 501 on exactly the boxes the
    feature exists for (field 2026-11-27, box 192.168.1.217).
    """

    def test_a_tree_without_the_bundle_has_none(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pf.esptool_bundle_dir(tmp), "")

    def test_the_bundle_is_found_by_its_package_marker(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "vendor", "esptool", "esptool"))
            with open(os.path.join(tmp, "vendor", "esptool", "esptool",
                                   "__init__.py"), "w") as fh:
                fh.write("")
            self.assertEqual(pf.esptool_bundle_dir(tmp),
                             os.path.join(tmp, "vendor", "esptool"))

    def test_pythonpath_is_prepended_without_mutating_the_source_env(self):
        import os
        base = {"PYTHONPATH": "/existing", "HOME": "/root"}
        env = pf.esptool_env("/bundle", base)
        self.assertEqual(env["PYTHONPATH"], "/bundle" + os.pathsep + "/existing")
        self.assertEqual(base["PYTHONPATH"], "/existing",
                         "the caller's environment was mutated")
        self.assertEqual(env["HOME"], "/root")

    def test_an_empty_bundle_leaves_the_environment_alone(self):
        base = {"PYTHONPATH": "/existing"}
        self.assertEqual(pf.esptool_env("", base)["PYTHONPATH"], "/existing")


class FlashPlanTests(unittest.TestCase):
    BUILD = pf.Build(version="1.2.1", url="https://x/y", sha256="0" * 64, size=1)

    def test_the_plan_writes_the_image_at_zero_and_the_config_at_0x9000(self):
        steps = pf.plan_flash("/dev/ttyACM0", "/tmp/merged.bin", "/tmp/nvs.bin", self.BUILD)
        text = " ".join(s.as_line() for s in steps)
        self.assertIn("0x0 /tmp/merged.bin", text)
        self.assertIn(f"0x{pf.NVS_OFFSET:x} /tmp/nvs.bin", text)
        self.assertIn("--chip esp32c3", text)
        self.assertIn("--verify", text)
        self.assertNotIn("--flash_size detect", text)

    def test_the_plan_ends_with_a_hard_reset(self):
        steps = pf.plan_flash("/dev/ttyACM0", "m", "n", self.BUILD)
        self.assertEqual(steps[-1].label, "reset")
        self.assertIn("hard_reset", steps[-1].as_line())


class _FakeRunner:
    """A serial port that never existed: returns canned outputs per step."""

    def __init__(self, *, chip="ESP32-C3 (QFN32) (revision v0.4)",
                 read_back=None, fail_at=None, fail_text=""):
        self.calls = []
        self.chip = chip
        self.read_back = read_back
        self.fail_at = fail_at
        self.fail_text = fail_text

    def __call__(self, argv):
        line = " ".join(argv)
        self.calls.append(line)
        if self.fail_at and self.fail_at in line:
            return 2, f"{self.fail_text}\n".encode()
        if "chip_id" in line:
            return 0, f"Chip is {self.chip}\nMAC: 00:00:00:00:00:00\n".encode()
        if "read_flash" in line:
            return 0, (self.read_back if self.read_back is not None else b"")
        if "run" in argv[-1:]:
            return 0, b""
        return 0, b"Writing at 0x00009000... (100 %)\n"


class RunFlashTests(unittest.TestCase):
    BUILD = pf.Build(version="1.2.1", url="https://x/y", sha256="0" * 64, size=1)

    def _run(self, runner, values=None):
        return pf.run_flash(port="/dev/ttyACM0", merged="/tmp/merged.bin",
                            values=values or dict(REFERENCE), build=self.BUILD,
                            runner=runner, workspace="/tmp")

    def test_a_clean_flash_is_verified_from_the_read_back(self):
        image = pf.build_nvs_image(REFERENCE)
        result = self._run(_FakeRunner(read_back=image))
        self.assertTrue(result.ok, result.reason)
        self.assertTrue(result.verified)
        self.assertIn("ESP32-C3", result.chip)

    def test_a_wrong_chip_aborts_before_writing(self):
        result = self._run(_FakeRunner(chip="ESP32-S3 (QFN56)"))
        self.assertFalse(result.ok)
        self.assertIn("unexpected chip", result.reason)
        # Nothing but chip_id ran — no write, no read-back.
        self.assertFalse(any("write_flash" in line for line in result.lines))
        self.assertFalse(any("read_flash" in line for line in result.lines))

    def test_a_read_back_that_does_not_match_the_intent_fails_loudly(self):
        wrong = pf.build_nvs_image({**REFERENCE, "ssid": "other-network"})
        result = self._run(_FakeRunner(read_back=wrong))
        self.assertFalse(result.ok)
        self.assertFalse(result.verified)
        self.assertIn("read-back mismatch", result.reason)

    def test_a_short_read_back_is_a_failure_not_a_trim(self):
        result = self._run(_FakeRunner(read_back=pf.build_nvs_image(REFERENCE)[:1024]))
        self.assertFalse(result.ok)
        self.assertIn("read-back", result.reason)

    def test_a_missing_board_says_how_to_get_into_download_mode(self):
        result = self._run(_FakeRunner(fail_at="chip_id",
                                       fail_text="A fatal error occurred: "
                                                 "Failed to connect to ESP32-C3: "
                                                 "No serial data received."))
        self.assertFalse(result.ok)
        self.assertIn("BOOT", result.reason)

    def test_no_secret_ever_appears_in_the_reported_lines(self):
        """The page's log pane ends up in screenshots and support bundles."""
        result = self._run(_FakeRunner(read_back=pf.build_nvs_image(REFERENCE)))
        blob = "\n".join(result.lines)
        for secret in (REFERENCE["pass"], REFERENCE["srv_pass"]):
            self.assertNotIn(secret, blob)

    def test_redact_removes_a_secret_from_a_line(self):
        self.assertEqual(pf.redact("pass=secret1234 ok", "secret1234"),
                         "pass=*** ok")
        self.assertEqual(pf.redact("nothing here", "secret1234"), "nothing here")


if __name__ == "__main__":
    unittest.main()
