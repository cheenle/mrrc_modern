"""`MRRC_SCU_LAN=1` wiring: the switch, the missing-key path, the session.

The claim these tests pin is narrow and load-bearing: turning the switch on
replaces the **transport under an unchanged CAT core**, so the radio keeps its
profile tables, its poll items and its PTT rules while the bytes travel over
UDP.  That is the whole reason this is a network transport rather than a new
radio family (design record §1), and it is what a later "why does the FTDX101
no longer have filters" regression would have broken.
"""
import unittest
from unittest import mock

import config
from backends.yaesu import backend as backend_module
from backends.yaesu.backend import FTDX101MPBackend


class StubClient:
    """The `ScuLanClient` surface the backend drives, without sockets."""

    instances: list = []

    def __init__(self, host, username, password, *, base_port=50000):
        self.host = host
        self.username = username
        self.password = password
        self.base_port = base_port
        self.session_id = 0x28
        self.auth_failed = False
        self.on_cat = None
        self.started = 0
        self.stopped = 0
        self.connected_answer = True
        StubClient.instances.append(self)

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    async def wait_until_connected(self, timeout: float = 10.0) -> bool:
        return self.connected_answer

    def send_cat_threadsafe(self, command: str) -> bool:
        return True

    def snapshot(self) -> dict:
        return {"host": self.host, "connected": self.connected_answer}


class WiringTests(unittest.TestCase):
    """Net mode off (the default) must be byte-for-byte the old wiring."""

    def test_the_default_wiring_has_no_network_transport(self):
        with mock.patch.object(config, "SCU_LAN", False):
            radio = FTDX101MPBackend("/dev/ttyUSB0", 38400)
        self.assertIsNone(radio.network_client())
        self.assertIsNone(radio.network_state())
        self.assertIsNone(radio._cat._transport)
        self.assertEqual(radio.capabilities.scope_type, "none")

    def test_the_switch_builds_the_tunnel_and_reports_a_network_scope(self):
        with mock.patch.object(config, "SCU_LAN", True), \
                mock.patch.object(config, "SCU_LAN_HOST", "192.168.1.50"), \
                mock.patch.object(config, "SCU_LAN_USER", "defaultuser"), \
                mock.patch.object(config, "SCU_LAN_PASS", "secret"), \
                mock.patch.object(config, "SCU_LAN_PORT", 50000):
            radio = FTDX101MPBackend("/dev/ttyUSB0", 38400)
        client = radio.network_client()
        self.assertIsNotNone(client)
        assert client is not None
        self.assertEqual((client.host, client.username, client.password),
                         ("192.168.1.50", "defaultuser", "secret"))
        self.assertIsNotNone(radio._cat._transport)
        self.assertEqual(radio.capabilities.scope_type, "scu_lan")
        state = radio.network_state()
        assert state is not None
        self.assertEqual(state["host"], "192.168.1.50")
        self.assertFalse(state["connected"], "no session runs in this test")

    def test_a_missing_credential_is_named_and_the_serial_path_is_kept(self):
        with mock.patch.object(config, "SCU_LAN", True), \
                mock.patch.object(config, "SCU_LAN_HOST", "192.168.1.50"), \
                mock.patch.object(config, "SCU_LAN_USER", "defaultuser"), \
                mock.patch.object(config, "SCU_LAN_PASS", ""):
            with self.assertLogs("backends.yaesu.backend", "WARNING") as logs:
                radio = FTDX101MPBackend("/dev/ttyUSB0", 38400)
        self.assertIsNone(radio.network_client())
        self.assertIsNone(radio._cat._transport)
        self.assertTrue(any("MRRC_SCU_LAN_PASS" in line for line in logs.output),
                        "the operator has to be told which key is missing")
        self.assertEqual(radio.capabilities.scope_type, "none")


class SessionWiringTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        StubClient.instances = []
        self._patches = [
            mock.patch.object(config, "SCU_LAN", True),
            mock.patch.object(config, "SCU_LAN_HOST", "192.168.1.50"),
            mock.patch.object(config, "SCU_LAN_USER", "defaultuser"),
            mock.patch.object(config, "SCU_LAN_PASS", "secret"),
            mock.patch("backends.yaesu.scu_lan.client.ScuLanClient",
                       StubClient),
            mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True, create=True),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    async def test_connect_starts_the_session_and_adopts_the_tunnel(self):
        radio = FTDX101MPBackend("/dev/ttyUSB0", 38400)
        client = radio.network_client()
        assert client is not None
        transport = radio._cat._transport
        # `connect` also runs the read-only `ID;` identity check, which the
        # stub's transport answers by simply accepting the write.
        await radio.connect()
        self.assertEqual(client.started, 1)
        self.assertIs(radio._cat._ser, transport)
        assert radio._cat._ser is not None
        self.assertTrue(radio._cat._ser.is_open)

    async def test_the_cat_rate_ladder_is_skipped_on_a_tunnel(self):
        """There is no baud rate on UDP; probing one would be noise."""
        radio = FTDX101MPBackend("/dev/ttyUSB0", 38400)
        with mock.patch.object(radio._cat, "_adopt_cat_rate",
                               side_effect=AssertionError("rate ladder ran")):
            await radio.connect()

    async def test_connecting_twice_is_safe(self):
        """The backend calls `start()` on every connect; the client is the
        idle-safe one (`if self._tasks: return`), and that is asserted in the
        client's own tests."""
        radio = FTDX101MPBackend("/dev/ttyUSB0", 38400)
        await radio.connect()
        await radio.connect()
        client = radio.network_client()
        assert client is not None
        self.assertEqual(client.started, 2, "the client, not the backend, dedupes")

    async def test_disconnect_stops_the_session(self):
        radio = FTDX101MPBackend("/dev/ttyUSB0", 38400)
        await radio.connect()
        await radio.disconnect()
        client = radio.network_client()
        assert client is not None
        self.assertEqual(client.stopped, 1)

    async def test_reconnect_waits_for_the_session_instead_of_reopening(self):
        radio = FTDX101MPBackend("/dev/ttyUSB0", 38400)
        with mock.patch.object(radio._cat, "reconnect_loop",
                               side_effect=AssertionError("serial reopen")):
            self.assertTrue(await radio.reconnect_loop())

    async def test_a_session_that_never_comes_up_is_a_warning_not_a_crash(self):
        radio = FTDX101MPBackend("/dev/ttyUSB0", 38400)
        client = radio.network_client()
        assert client is not None
        client.connected_answer = False
        with self.assertLogs("backends.yaesu.backend", "WARNING") as logs:
            await radio.connect()
        self.assertTrue(any("SCU-LAN10 session not up" in line
                            for line in logs.output))


class NetworkSurfaceTests(unittest.TestCase):
    """Scope/audio/TX gate on the network wiring."""

    def setUp(self) -> None:
        # The gate warning is one-per-process; reset it so each test sees its own.
        backend_module._SCU_TX_GATE_WARNED = False
        with mock.patch.object(config, "SCU_LAN", True), \
                mock.patch.object(config, "SCU_LAN_HOST", "10.0.0.9"), \
                mock.patch.object(config, "SCU_LAN_USER", "u"), \
                mock.patch.object(config, "SCU_LAN_PASS", "p"):
            self.radio = FTDX101MPBackend("/dev/ttyUSB0", 38400)

    def test_the_span_table_comes_from_the_ss_codes(self):
        caps = self.radio.capabilities
        self.assertEqual(caps.scope_type, "scu_lan")
        spans = caps.scope_spans
        self.assertEqual(spans[0], 1000)
        self.assertEqual(spans[4], 50000)
        self.assertEqual(spans[8], 1000000)
        self.assertEqual(len(spans), 9, "SS codes 0-8")

    def test_transmit_is_refused_while_tx_audio_does_not_exist(self):
        with self.assertLogs("backends.yaesu.backend", "WARNING") as logs:
            self.assertTrue(self.radio.capabilities.tx_gated)
            self.assertFalse(self.radio._tx_allowed())
        self.assertTrue(any("transmit refused" in line for line in logs.output))

    def test_the_scope_producer_runs_on_the_session_and_keeps_running(self):
        producer = self.radio.create_scope_producer(scope_handler=None)
        assert producer is not None
        self.assertTrue(producer.keeps_running())
        self.assertIs(producer._client, self.radio.network_client())

    def test_the_audio_handler_reads_rx_from_the_unit(self):
        handler = self.radio.create_audio_handler()
        assert handler is not None
        self.assertFalse(handler.tx_audio_available)
        self.assertEqual(handler.device_rates, {"rx": 48000, "tx": 48000})

    def test_network_state_carries_the_scope_and_tx_audio_flags(self):
        self.radio.create_scope_producer(scope_handler=None)
        state = self.radio.network_state()
        assert state is not None
        self.assertIn("scope", state)
        self.assertFalse(state["tx_audio"])


class SerialSurfaceTests(unittest.TestCase):
    def test_nothing_is_created_on_the_serial_wiring(self):
        with mock.patch.object(config, "SCU_LAN", False), \
                mock.patch.object(config, "ALLOW_UNVERIFIED_TX", True,
                                  create=True):
            radio = FTDX101MPBackend("/dev/ttyUSB0", 38400)
            self.assertIsNone(radio.create_scope_producer(scope_handler=None))
            self.assertIsNone(radio.create_audio_handler())
            self.assertIsNone(radio.network_state())
            self.assertFalse(radio.capabilities.tx_gated,
                             "serial wiring keeps the profile's own gate")


class CapabilityTests(unittest.TestCase):
    def test_the_switched_on_backend_still_reports_the_profile_tables(self):
        """The reason this is a transport, not a new family."""
        with mock.patch.object(config, "SCU_LAN", True), \
                mock.patch.object(config, "SCU_LAN_HOST", "10.0.0.9"), \
                mock.patch.object(config, "SCU_LAN_USER", "u"), \
                mock.patch.object(config, "SCU_LAN_PASS", "p"):
            radio = FTDX101MPBackend("/dev/ttyUSB0", 38400)
        caps = radio.capabilities
        self.assertEqual(caps.model_name, "ftdx101mp")
        self.assertTrue(radio.bands)
        self.assertTrue(radio.ui_modes)
        self.assertTrue(radio.filter_tables()["voice"],
                        "profile filter widths must survive the switch")
        self.assertIs(backend_module.FTDX101MPBackend, type(radio))


if __name__ == "__main__":
    unittest.main()
