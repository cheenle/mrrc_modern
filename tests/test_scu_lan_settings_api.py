"""The SCU-LAN10 card on `/settings` and the two endpoints behind it.

The card is the only way a customer can turn this wiring on without SSH, so
three properties get pinned here:

* the **password never comes back** to the browser, and a blank one on save
  means "keep the stored one" — otherwise every edit would demand the password;
* turning it on **requires** a host, a user and a password, and says which one
  is missing instead of writing a half-configured box;
* "restart required" is computed from the **live** wiring, not from the switch —
  the switch is a wish, the backend is a fact, and the page has to say which.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import config
import server
from tests.test_server_box_pages import run

PAGE = Path(__file__).resolve().parent.parent / "static" / "settings.html"


def request(path: str, body: dict | None = None) -> Any:
    """A stand-in for Starlette's Request, shaped like the handlers read it."""
    req = SimpleNamespace(headers={}, cookies={"mrrc_auth": "t"},
                          client=SimpleNamespace(host="127.0.0.1"),
                          url=SimpleNamespace(path=path, query=""))

    async def payload() -> dict:
        return body or {}

    req.json = payload
    return req


def body_text(response: Any) -> str:
    data = response.body
    if isinstance(data, memoryview):
        data = data.tobytes()
    return data.decode("utf-8") if isinstance(data, bytes) else str(data)


def body_json(response: Any) -> dict:
    return json.loads(body_text(response))


class ScuLanSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_path = Path(self.tmp.name) / "mrrc_modern.env"
        self.env_path.write_text("MRRC_SERIAL_PORT=/dev/ttyUSB0\n",
                                 encoding="utf-8")
        self._saved = {name: getattr(config, name) for name in (
            "SCU_LAN", "SCU_LAN_HOST", "SCU_LAN_USER", "SCU_LAN_PASS",
            "SCU_LAN_PORT", "RADIO_MODEL")}
        self.addCleanup(self._restore)
        config.RADIO_MODEL = "ftdx101mp"
        config.SCU_LAN = False
        config.SCU_LAN_HOST = ""
        config.SCU_LAN_USER = ""
        config.SCU_LAN_PASS = ""
        config.SCU_LAN_PORT = 50000
        self.live = mock.patch.object(server, "backend", None)
        self.live.start()
        self.addCleanup(self.live.stop)

    def _restore(self) -> None:
        for name, value in self._saved.items():
            setattr(config, name, value)

    # ── helpers ─────────────────────────────────────────────────

    def get(self) -> dict:
        """Read the state exactly as `setUp` left the configuration."""
        with mock.patch.object(server, "_verify_auth", return_value=True):
            response = run(server.api_scu_lan_state(request("/api/box/scu-lan")))
        return body_json(response)

    def post(self, body: dict):
        with mock.patch.object(server, "_verify_auth", return_value=True), \
                mock.patch.object(server, "_config_file_path",
                                  return_value=self.env_path):
            response = run(server.api_scu_lan_save(
                request("/api/box/scu-lan", body)))
        return response.status_code, json.loads(response.body)

    # ── the card ────────────────────────────────────────────────

    def test_the_card_says_transmit_is_off_and_why(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn("SCU-LAN10 网络接入", page)
        self.assertIn("目前不能发射", page)
        self.assertIn("静音载波", page)
        self.assertIn("独占", page)
        self.assertIn("重启服务", page)

    def test_the_card_only_talks_to_the_scu_lan_endpoints(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn("/api/box/scu-lan/test", page)
        self.assertIn("/api/box/scu-lan", page)
        self.assertIn("留空表示不改动已保存的口令", page)

    # ── state ───────────────────────────────────────────────────

    def test_state_never_carries_the_password(self):
        config.SCU_LAN_PASS = "topsecret"
        state = self.get()
        self.assertTrue(state["password_set"])
        self.assertNotIn("topsecret", json.dumps(state))

    def test_a_switch_that_differs_from_the_wiring_asks_for_a_restart(self):
        config.SCU_LAN = True                 # saved on, backend still serial
        self.assertTrue(self.get()["restart_required"])
        config.SCU_LAN = False
        self.assertFalse(self.get()["restart_required"])

    def test_a_model_without_the_unit_is_reported_unsupported(self):
        config.RADIO_MODEL = "ic7300"
        self.assertFalse(self.get()["supported"])

    # ── save ────────────────────────────────────────────────────

    def test_enabling_needs_all_three_and_names_what_is_missing(self):
        status, payload = self.post({"enabled": True, "host": "192.168.1.50"})
        self.assertEqual(status, 400)
        self.assertIn("用户名", payload["error"])
        self.assertIn("口令", payload["error"])

    def test_a_successful_save_writes_the_env_file_once_per_key(self):
        status, payload = self.post({
            "enabled": True, "host": "192.168.1.50", "user": "defaultuser",
            "password": "secret", "port": "50000",
        })
        self.assertEqual(status, 200, payload)
        text = self.env_path.read_text(encoding="utf-8")
        self.assertIn("MRRC_SCU_LAN=1", text)
        self.assertIn("MRRC_SCU_LAN_HOST=192.168.1.50", text)
        self.assertIn("MRRC_SCU_LAN_PASS=secret", text)
        self.assertEqual(text.count("MRRC_SCU_LAN_HOST="), 1,
                         "the writer must never leave the key twice")
        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["restart_required"],
                        "the running backend is still on the serial wiring")

    def test_a_blank_password_keeps_the_stored_one(self):
        config.SCU_LAN_PASS = "stored"
        status, _payload = self.post({
            "enabled": True, "host": "192.168.1.10", "user": "u",
            "password": "",
        })
        self.assertEqual(status, 200)
        text = self.env_path.read_text(encoding="utf-8")
        self.assertNotIn("MRRC_SCU_LAN_PASS", text,
                         "an unchanged credential must not be rewritten")
        self.assertEqual(config.SCU_LAN_PASS, "stored")

    def test_disabling_keeps_the_credentials_for_the_next_time(self):
        config.SCU_LAN_PASS = "stored"
        status, payload = self.post({"enabled": False})
        self.assertEqual(status, 200)
        text = self.env_path.read_text(encoding="utf-8")
        self.assertIn("MRRC_SCU_LAN=0", text)
        self.assertNotIn("MRRC_SCU_LAN_PASS", text)
        self.assertFalse(payload["enabled"])

    def test_enabling_on_an_unsupported_model_is_refused(self):
        config.RADIO_MODEL = "ft991a"
        status, payload = self.post({
            "enabled": True, "host": "1.2.3.4", "user": "u", "password": "p"})
        self.assertEqual(status, 400)
        self.assertIn("SCU-LAN10", payload["error"])

    def test_a_bad_port_is_refused_not_clamped(self):
        status, payload = self.post({
            "enabled": True, "host": "1.2.3.4", "user": "u", "password": "p",
            "port": "70000"})
        self.assertEqual(status, 400)
        self.assertIn("1..65535", payload["error"])

    def test_enabled_must_be_a_boolean(self):
        status, payload = self.post({"enabled": "yes"})
        self.assertEqual(status, 400)
        self.assertIn("true", payload["error"])


class ScuLanProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: getattr(config, name) for name in (
            "SCU_LAN", "SCU_LAN_HOST", "SCU_LAN_USER", "SCU_LAN_PASS",
            "SCU_LAN_PORT", "RADIO_MODEL")}
        self.addCleanup(self._restore)
        config.RADIO_MODEL = "ftdx101mp"
        config.SCU_LAN_HOST = ""
        config.SCU_LAN_USER = ""
        config.SCU_LAN_PASS = ""
        config.SCU_LAN_PORT = 50000
        self.live = mock.patch.object(server, "backend", None)
        self.live.start()
        self.addCleanup(self.live.stop)

    def _restore(self) -> None:
        for name, value in self._saved.items():
            setattr(config, name, value)

    def test_it_refuses_while_the_box_is_using_the_unit(self):
        """A second client would take over our own session — 409, not a handshake."""
        fake_backend = SimpleNamespace(
            network_state=lambda: {"connected": True, "session_id": 0x28})
        with mock.patch.object(server, "backend", fake_backend), \
                mock.patch.object(server, "_verify_auth", return_value=True):
            response = run(server.api_scu_lan_test(
                request("/api/box/scu-lan/test",
                        {"host": "1.2.3.4", "user": "u", "password": "p"})))
        self.assertEqual(response.status_code, 409)
        self.assertIn("顶掉", body_text(response))

    def test_it_names_the_missing_field(self):
        with mock.patch.object(server, "_verify_auth", return_value=True):
            response = run(server.api_scu_lan_test(
                request("/api/box/scu-lan/test", {"host": "1.2.3.4"})))
        self.assertEqual(response.status_code, 400)
        self.assertIn("用户名", body_text(response))

    def test_a_silent_unit_is_reported_as_credentials_not_as_network(self):
        """The unit answers nothing on a bad login; the page must say so."""
        class Silent:
            def __init__(self, *a, **kw):
                self.auth_failed = True
                self.on_cat = None

            async def start(self):
                return None

            async def wait_until_connected(self, timeout=0.0):
                return False

            async def stop(self):
                return None

        with mock.patch("backends.yaesu.scu_lan.client.ScuLanClient", Silent), \
                mock.patch.object(server, "_verify_auth", return_value=True):
            response = run(server.api_scu_lan_test(
                request("/api/box/scu-lan/test",
                        {"host": "1.2.3.4", "user": "u", "password": "p"})))
        payload = body_json(response)
        self.assertFalse(payload["ok"])
        self.assertIn("用户名或口令", payload["error"])

    def test_a_working_unit_reports_the_session_and_the_cat_answer(self):
        class Working:
            def __init__(self, host, user, password, *, base_port=50000, **kw):
                self.on_cat = kw.get("on_cat")
                self.auth_failed = False
                self.host = host
                self.base_port = base_port

            async def start(self):
                if self.on_cat:
                    self.on_cat("FA007007000;")

            async def wait_until_connected(self, timeout=0.0):
                return True

            def send_cat(self, command):
                return True

            def snapshot(self):
                return {"session_id": 0x33, "capability": "0125013f51",
                        "scope_packets": 5, "audio_packets": 7,
                        "connected": True}

            async def stop(self):
                return None

        with mock.patch("backends.yaesu.scu_lan.client.ScuLanClient", Working), \
                mock.patch.object(server, "_verify_auth", return_value=True):
            response = run(server.api_scu_lan_test(
                request("/api/box/scu-lan/test",
                        {"host": "1.2.3.4", "user": "u", "password": "p"})))
        payload = body_json(response)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["session_id"], 0x33)
        self.assertEqual(payload["cat"], "FA007007000;")


class SetupSaveTests(unittest.TestCase):
    """The connection dialog / first-boot onboarding must not fight this wiring.

    `/api/setup` used to read `transport == "network"` as "this is a SunSDR":
    it required a SunSDR host and then blanked the serial port and the audio
    devices.  A Yaesu owner behind an SCU-LAN10 has neither a serial port nor a
    SunSDR host, so that branch would have refused their save (or, worse, wiped
    the settings they need again after switching back).
    """

    class _Req:
        def __init__(self, body):
            self._body = body

        async def json(self):
            return self._body

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_path = Path(self.tmp.name) / "mrrc.env"
        self.env_path.write_text(
            "MRRC_SERIAL_PORT=/dev/ttyUSB0\n"
            "MRRC_AUDIO_RX_DEVICE=USB Audio CODEC\n"
            "MRRC_AUDIO_TX_DEVICE=USB Audio CODEC\n"
            "MRRC_SUNSDR_HOST=10.9.9.9\n", encoding="utf-8")
        self._saved = {name: getattr(config, name) for name in ("SCU_LAN",)}
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for name, value in self._saved.items():
            setattr(config, name, value)

    def _save(self, body):
        import env_store
        with mock.patch.object(server, "_verify_auth", return_value=True), \
                mock.patch.object(server, "_schedule_restart"), \
                mock.patch.object(server, "_config_file_path",
                                  return_value=self.env_path):
            response = run(server.api_setup_save(self._Req(body)))
        return response, env_store.load(self.env_path)

    def test_the_serial_wiring_still_requires_a_port(self):
        config.SCU_LAN = False
        response, _env = self._save({"radio_model": "ftdx101mp"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("serial_port", body_text(response))

    def test_the_network_wiring_saves_without_a_port_or_a_sunsdr_host(self):
        config.SCU_LAN = True
        response, env = self._save({"radio_model": "ftdx101mp"})
        self.assertEqual(response.status_code, 200, body_text(response))
        # Nothing the dialog cannot show may be blanked by a save.
        self.assertEqual(env["MRRC_SERIAL_PORT"], "/dev/ttyUSB0")
        self.assertEqual(env["MRRC_AUDIO_RX_DEVICE"], "USB Audio CODEC")
        self.assertEqual(env["MRRC_SUNSDR_HOST"], "10.9.9.9",
                         "a Yaesu save must not adopt the SunSDR key")

    def test_a_sunsdr_model_still_requires_its_host(self):
        config.SCU_LAN = True
        response, _env = self._save({"radio_model": "sunsdr2dx"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("sunsdr_host", body_text(response))


class SupportBundleTests(unittest.TestCase):
    """A bundle from a customer with this wiring must carry the evidence."""

    def test_the_config_allow_list_keeps_the_wiring_and_drops_the_login(self):
        import support_bundle
        keys = support_bundle.CONFIG_KEY_ALLOWLIST
        for key in ("MRRC_SCU_LAN", "MRRC_SCU_LAN_HOST", "MRRC_SCU_LAN_PORT"):
            self.assertIn(key, keys)
        for key in ("MRRC_SCU_LAN_USER", "MRRC_SCU_LAN_PASS"):
            self.assertNotIn(key, keys,
                             "a bundle is mailed around; the login stays out")

    def test_the_snapshot_carries_the_live_link_without_the_password(self):
        class FakeBackend:
            def network_state(self):
                return {"connected": True, "session_id": 0x28,
                        "scope_packets": 7, "tx_audio": False}

        with mock.patch.object(server, "backend", FakeBackend()), \
                mock.patch.object(config, "SCU_LAN", True), \
                mock.patch.object(config, "SCU_LAN_PASS", "topsecret"):
            snapshot = server._support_env_snapshot()
        block = snapshot["scu_lan"]
        self.assertTrue(block["connected"])
        self.assertEqual(block["session_id"], 0x28)
        self.assertFalse(block["tx_audio"])
        self.assertTrue(block["password_set"])
        self.assertNotIn("topsecret", json.dumps(block))

    def test_a_backend_without_the_hook_leaves_no_block(self):
        with mock.patch.object(server, "backend", None):
            self.assertNotIn("scu_lan", server._support_env_snapshot())


class ModelConnectionTests(unittest.TestCase):
    def test_the_yaesu_hybrids_answer_network_only_when_the_switch_is_on(self):
        from backends import model_connection
        with mock.patch.object(config, "SCU_LAN", True):
            self.assertEqual(model_connection("ftdx101mp")["transport"], "network")
            self.assertEqual(model_connection("ft710")["transport"], "network")
        with mock.patch.object(config, "SCU_LAN", False):
            self.assertEqual(model_connection("ftdx101mp")["transport"], "serial")

    def test_other_models_are_unaffected_by_the_switch(self):
        from backends import model_connection
        with mock.patch.object(config, "SCU_LAN", True):
            self.assertEqual(model_connection("ic7300")["transport"], "serial")


if __name__ == "__main__":
    unittest.main()
