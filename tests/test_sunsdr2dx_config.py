import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import env_store
import server
from backends import known_models, model_connection
from macos import first_run


class ConnectionMetadataTests(unittest.TestCase):
    def test_network_and_serial_metadata_is_registry_driven(self):
        self.assertEqual(model_connection("sunsdr2dx"),
                         {"transport": "network", "host_required": True})
        self.assertEqual(model_connection("ft710"),
                         {"transport": "serial", "host_required": False})
        self.assertEqual(set(known_models()),
                         set(server._setup_status()["connections"]))

    def test_environment_values_are_bounded(self):
        with mock.patch.dict("os.environ", {
                "MRRC_SUNSDR_HOST": " radio.local ",
                "MRRC_SUNSDR_CONTROL_PORT": "70000",
                "MRRC_SUNSDR_IQ_PORT": "bad",
                "MRRC_SUNSDR_SAMPLE_RATE": "12345"}, clear=False):
            self.assertEqual(config._env("MRRC_SUNSDR_HOST", "").strip(),
                             "radio.local")
            self.assertEqual(config._env_port("MRRC_SUNSDR_CONTROL_PORT", 50001),
                             50001)
            self.assertEqual(config._env_port("MRRC_SUNSDR_IQ_PORT", 50002), 50002)
            self.assertEqual(config._env_choice("MRRC_SUNSDR_SAMPLE_RATE", 78125,
                                                (39062, 78125, 156250, 312500)),
                             78125)


class DuccThreadGuardTests(unittest.TestCase):
    def test_server_sets_ducc_single_thread_before_radio_imports(self):
        # scipy's DUCC FFT pool + subprocess fork() is a macOS crash.  The
        # reference server fixes it by setting DUCC_NUM_THREADS=1 before
        # numpy/scipy load; server.py must keep that ordering.
        source = Path(server.__file__).read_text(encoding="utf-8")
        marker = 'os.environ.setdefault("DUCC_NUM_THREADS", "1")'
        self.assertIn(marker, source)
        pos = source.index(marker)
        for later in ("from backends import", "from backends.ft710",
                      "import numpy", "from scipy", "import scipy"):
            idx = source.find(later)
            if idx != -1:
                self.assertLess(pos, idx, later)


class _Request:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class NetworkSetupTests(unittest.TestCase):
    def _save(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "mrrc.env"
        with mock.patch.object(server, "_verify_auth", return_value=True), \
                mock.patch.object(server, "_schedule_restart") as restart, \
                mock.patch.object(server, "_config_file_path", return_value=path):
            response = asyncio.run(server.api_setup_save(_Request(body)))
        return response, env_store.load(path), restart

    def test_network_save_needs_host_not_serial(self):
        response, _values, restart = self._save({"radio_model": "sunsdr2dx"})
        self.assertEqual(response.status_code, 400)
        restart.assert_not_called()

        response, values, restart = self._save({
            "radio_model": "sunsdr2dx",
            "sunsdr_host": "192.0.2.20",
            "sunsdr_local_host": "192.0.2.10",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(values["MRRC_SUNSDR_HOST"], "192.0.2.20")
        self.assertEqual(values["MRRC_SUNSDR_LOCAL_HOST"], "192.0.2.10")
        self.assertEqual(values["MRRC_SERIAL_PORT"], "")
        restart.assert_called_once()

    def test_serial_models_still_need_a_port(self):
        response, _values, restart = self._save({"radio_model": "ic7300"})
        self.assertEqual(response.status_code, 400)
        restart.assert_not_called()

    def test_network_status_includes_current_host(self):
        with mock.patch.object(server, "SUNSDR_HOST", "radio.local"), \
                mock.patch.object(server, "SUNSDR_LOCAL_HOST", "192.0.2.10"):
            status = server._setup_status()
        self.assertEqual(status["sunsdr_host"], "radio.local")
        self.assertEqual(status["sunsdr_local_host"], "192.0.2.10")
        self.assertEqual(status["connections"]["sunsdr2dx"]["transport"],
                         "network")

    def test_network_startup_has_no_empty_serial_warning(self):
        with mock.patch.object(server, "RADIO_MODEL", "sunsdr2dx"), \
                mock.patch.object(server, "SERIAL_PORT", ""):
            self.assertEqual(server._serial_port_problem(), "")


class NetworkFirstRunTests(unittest.TestCase):
    def test_configured_network_model_does_not_need_serial_probe(self):
        env = {
            "MRRC_WEB_PASSWORD": "a-real-password",
            "MRRC_RADIO_MODEL": "sunsdr2dx",
            "MRRC_SUNSDR_HOST": "radio.local",
            "MRRC_PORT_CONFIRMED": "1",
        }
        self.assertFalse(first_run.needs_first_run(env))

    def test_network_first_run_never_opens_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"MRRC_RADIO_MODEL": "sunsdr2dx",
                   "MRRC_WEB_PASSWORD": "a-real-password",
                   "MRRC_SUNSDR_HOST": "radio.local"}
            with mock.patch.object(first_run, "detect_serial_ports",
                                   side_effect=AssertionError("serial probe")):
                result = first_run.apply_first_run(env, Path(tmp) / "mrrc.env",
                                                   open_func=object())
        self.assertEqual(result["MRRC_RADIO_MODEL"], "sunsdr2dx")
        self.assertEqual(result["MRRC_SERIAL_PORT"], "")


class ConnectionMarkupTests(unittest.TestCase):
    def test_network_fields_and_external_module_are_present(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "static/index.html").read_text(encoding="utf-8")
        self.assertIn('value="sunsdr2dx"', html)
        self.assertIn('id="cs-sunsdr-host"', html)
        self.assertIn('id="cs-sunsdr-local-host"', html)
        self.assertIn('modules/connection_settings.js', html)
        js = (root / "static/modules/connection_settings.js").read_text(encoding="utf-8")
        self.assertIn("transport", js)
        self.assertNotIn('model === "sunsdr2dx"', js)


if __name__ == "__main__":
    unittest.main()
