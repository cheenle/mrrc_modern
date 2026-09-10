"""Tests for macos.first_run first-launch auto-config."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from macos import first_run as fr


class _FakeComPort:
    def __init__(self, device, description="", hwid=""):
        self.device = device
        self.description = description
        self.hwid = hwid


class _FakeSerialFT:
    """Fake serial that answers the FT-710 ASCII ID; query."""
    def __init__(self, *a, **k):
        pass
    def reset_input_buffer(self):
        pass
    def write(self, b):
        return len(b)
    def read(self, n):
        return b"ID017;"


class _FakeSerialIC:
    """Fake serial that answers the IC-7300 CI-V 0x19 model query."""
    def __init__(self, *a, **k):
        pass
    def reset_input_buffer(self):
        pass
    def write(self, b):
        return len(b)
    def read(self, n):
        # FE FE <from=0x94> <to=0xE0> 19 <model=0x94> <cks> FD
        return bytes([0xFE, 0xFE, 0x94, 0xE0, 0x19, 0x94, 0xFD, 0xFD])


class _FakeSerialNull:
    """Fake serial that never answers."""
    def __init__(self, *a, **k):
        pass
    def reset_input_buffer(self):
        pass
    def write(self, b):
        return len(b)
    def read(self, n):
        return b""


class NeedsFirstRunTests(unittest.TestCase):
    def test_true_when_no_done_flag(self):
        self.assertTrue(fr.needs_first_run({"MRRC_WEB_PASSWORD": "x"}))

    def test_true_when_default_password(self):
        env = {"MRRC_FIRST_RUN_DONE": "1", "MRRC_WEB_PASSWORD": fr.DEFAULT_WEB_PASSWORD}
        self.assertTrue(fr.needs_first_run(env))

    def test_true_when_serial_is_default(self):
        env = {"MRRC_FIRST_RUN_DONE": "1", "MRRC_SERIAL_PORT": "/dev/cu.SLAB_USBtoUART"}
        self.assertTrue(fr.needs_first_run(env))

    def test_false_when_done_and_configured(self):
        env = {
            "MRRC_FIRST_RUN_DONE": "1",
            "MRRC_WEB_PASSWORD": "S3cret!long",
            "MRRC_SERIAL_PORT": "/dev/cu.usbserial-A1",
            "MRRC_RADIO_MODEL": "ic7300",
        }
        self.assertFalse(fr.needs_first_run(env))

    def test_false_when_port_confirmed_on_default(self):
        env = {
            "MRRC_FIRST_RUN_DONE": "1", "MRRC_PORT_CONFIRMED": "1",
            "MRRC_WEB_PASSWORD": "x",
            "MRRC_SERIAL_PORT": "/dev/cu.SLAB_USBtoUART",
            "MRRC_RADIO_MODEL": "ft710",
        }
        self.assertFalse(fr.needs_first_run(env))


class GeneratePasswordTests(unittest.TestCase):
    def test_length_and_randomness(self):
        a, b = fr.generate_password(), fr.generate_password()
        self.assertGreaterEqual(len(a), 16)
        self.assertNotEqual(a, b)


class DetectSerialPortsTests(unittest.TestCase):
    def test_cp210x_ports_sort_first(self):
        ports = [
            _FakeComPort("/dev/cu.usbserial-OTHER", "USB Serial", "USB"),
            _FakeComPort("/dev/cu.SLAB_USBtoUART", "CP210x USB to UART Bridge", "USB"),
            _FakeComPort("/dev/cu.Bluetooth-Incoming-Port", "", ""),
        ]
        with mock.patch("macos.first_run.sys.platform", "darwin"):
            result = fr.detect_serial_ports(ports)
        self.assertEqual(result[0], "/dev/cu.SLAB_USBtoUART")
        self.assertEqual(len(result), 2)  # Bluetooth port excluded

    def test_empty_when_no_ports(self):
        self.assertEqual(fr.detect_serial_ports([]), [])


class DetectSerialPortsWindowsTests(unittest.TestCase):
    def test_windows_lists_com_ports(self):
        ports = [
            _FakeComPort("COM1", "Communications Port", ""),
            _FakeComPort("COM3", "CP210x USB to UART Bridge", "USB VID:PID=10C4:EA60"),
        ]
        with mock.patch("macos.first_run.sys.platform", "win32"):
            result = fr.detect_serial_ports(ports)
        self.assertEqual(result, ["COM3", "COM1"])  # CP210x sorts first

    def test_windows_no_bogus_filter_issue(self):
        with mock.patch("macos.first_run.sys.platform", "win32"):
            self.assertEqual(fr.detect_serial_ports([]), [])

    def test_windows_candidates_keeps_all_devices(self):
        ports = [_FakeComPort("COM3", "CP210x USB to UART Bridge", "USB")]
        with mock.patch("macos.first_run.sys.platform", "win32"):
            self.assertEqual([p.device for p in fr._candidates(ports)], ["COM3"])


class ProbeRadioModelTests(unittest.TestCase):
    def test_ft710_detected(self):
        def open_func(**kw):
            return _FakeSerialFT()
        self.assertEqual(fr.probe_radio_model("/dev/cu.SLAB_USBtoUART", open_func=open_func), "ft710")

    def test_ic7300_detected(self):
        def open_func(**kw):
            return _FakeSerialIC()
        self.assertEqual(fr.probe_radio_model("/dev/cu.usbserial-A1", open_func=open_func), "ic7300")

    def test_none_when_no_radio(self):
        def open_func(**kw):
            return _FakeSerialNull()
        self.assertIsNone(fr.probe_radio_model("/dev/cu.usbserial-A1", open_func=open_func))


class UpdateEnvFileTests(unittest.TestCase):
    def test_updates_in_place_and_appends_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mrrc_modern.env"
            path.write_text("# comment\nMRRC_WEB_PASSWORD=old\nMRRC_RADIO_MODEL=ft710\n", encoding="utf-8")
            fr.update_env_file(path, {"MRRC_WEB_PASSWORD": "new", "MRRC_FIRST_RUN_DONE": "1"})
            text = path.read_text(encoding="utf-8")
        self.assertIn("# comment", text)
        self.assertIn("MRRC_WEB_PASSWORD=new", text)
        self.assertNotIn("=old", text)
        self.assertIn("MRRC_FIRST_RUN_DONE=1", text)


class ApplyFirstRunTests(unittest.TestCase):
    def _tmp_config(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "mrrc_modern.env"
        path.write_text(body, encoding="utf-8")
        return path

    def test_generates_password_and_marks_done(self):
        path = self._tmp_config("MRRC_WEB_PASSWORD=\nMRRC_SERIAL_PORT=\nMRRC_RADIO_MODEL=\n")
        with mock.patch.object(fr, "detect_serial_ports", return_value=[]):
            env = fr.apply_first_run({}, path, open_func=lambda **kw: _FakeSerialNull())
        self.assertEqual(env["MRRC_AUTO_PASSWORD"], "1")
        self.assertEqual(env["MRRC_FIRST_RUN_DONE"], "1")
        self.assertEqual(env["MRRC_RADIO_MODEL"], "ft710")
        self.assertNotEqual(env["MRRC_WEB_PASSWORD"], "")
        text = path.read_text(encoding="utf-8")
        self.assertIn(f"MRRC_WEB_PASSWORD={env['MRRC_WEB_PASSWORD']}", text)
        self.assertIn("MRRC_FIRST_RUN_DONE=1", text)

    def test_detects_port_and_model_from_probe(self):
        path = self._tmp_config("MRRC_WEB_PASSWORD=already-set\nMRRC_SERIAL_PORT=\nMRRC_RADIO_MODEL=\n")
        with mock.patch.object(
            fr, "detect_serial_ports", return_value=["/dev/cu.SLAB_USBtoUART"]
        ):
            env = fr.apply_first_run({}, path, open_func=lambda **kw: _FakeSerialFT())
        self.assertEqual(env["MRRC_SERIAL_PORT"], "/dev/cu.SLAB_USBtoUART")
        self.assertEqual(env["MRRC_RADIO_MODEL"], "ft710")
        self.assertEqual(env["MRRC_PORT_CONFIRMED"], "1")

    def test_keeps_existing_config(self):
        path = self._tmp_config(
            "MRRC_WEB_PASSWORD=S3cret!long\nMRRC_SERIAL_PORT=/dev/cu.usbserial-A1\n"
            "MRRC_RADIO_MODEL=ic7300\n"
        )
        # In the real flow the launcher calls load_env(config_path()) first,
        # so apply_first_run receives the existing values in ``env``.
        env = {
            "MRRC_WEB_PASSWORD": "S3cret!long",
            "MRRC_SERIAL_PORT": "/dev/cu.usbserial-A1",
            "MRRC_RADIO_MODEL": "ic7300",
        }
        with mock.patch.object(fr, "detect_serial_ports") as detect:
            result = fr.apply_first_run(env, path, open_func=lambda **kw: _FakeSerialNull())
        detect.assert_not_called()
        self.assertEqual(result["MRRC_RADIO_MODEL"], "ic7300")
        self.assertEqual(result["MRRC_SERIAL_PORT"], "/dev/cu.usbserial-A1")


class ApplyFirstRunBaudLinkageTests(unittest.TestCase):
    """V2.33: legacy installer templates pre-filled MRRC_BAUD_RATE=38400
    (the FT-710 value) regardless of model. apply_first_run probes the
    radio and must align the stored baud with the discovered model, or a
    discovered IC-7300 keeps the stale 38400 and the CI-V scope stream
    (which requires 115200) never works. An explicitly customized baud
    (anything other than the template default) is preserved."""

    def _tmp_config(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "mrrc_modern.env"
        path.write_text(body, encoding="utf-8")
        return path

    def _run(self, env, body, open_func):
        path = self._tmp_config(body)
        with mock.patch.object(fr, "detect_serial_ports",
                               return_value=["/dev/cu.usbserial-A1"]):
            env = fr.apply_first_run(env, path, open_func=open_func)
        return env, path

    def test_template_38400_becomes_115200_for_ic7300(self):
        env, path = self._run({"MRRC_BAUD_RATE": "38400"},
                              "MRRC_BAUD_RATE=38400\n",
                              lambda **kw: _FakeSerialIC())
        self.assertEqual(env["MRRC_BAUD_RATE"], "115200")
        self.assertIn("MRRC_BAUD_RATE=115200", path.read_text(encoding="utf-8"))

    def test_template_38400_stays_38400_for_ft710(self):
        env, path = self._run({"MRRC_BAUD_RATE": "38400"},
                              "MRRC_BAUD_RATE=38400\n",
                              lambda **kw: _FakeSerialFT())
        self.assertEqual(env["MRRC_BAUD_RATE"], "38400")
        self.assertIn("MRRC_BAUD_RATE=38400", path.read_text(encoding="utf-8"))

    def test_explicit_custom_baud_is_preserved(self):
        env, path = self._run({"MRRC_BAUD_RATE": "57600"},
                              "MRRC_BAUD_RATE=57600\n",
                              lambda **kw: _FakeSerialIC())
        self.assertEqual(env["MRRC_BAUD_RATE"], "57600")
        self.assertIn("MRRC_BAUD_RATE=57600", path.read_text(encoding="utf-8"))

    def test_unset_baud_is_filled_from_model(self):
        env, path = self._run({}, "", lambda **kw: _FakeSerialIC())
        self.assertEqual(env["MRRC_BAUD_RATE"], "115200")
        self.assertIn("MRRC_BAUD_RATE=115200", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
