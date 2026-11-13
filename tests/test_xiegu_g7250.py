"""Xiegu G7250 backend.

Hardware-independent coverage for the first non-Icom/non-Yaesu family:

- registry/profile/capability wiring (a registered model that no dialog can
  reach was a real bug class on the Icom round);
- the CI-V address probe — the G7250's address is undocumented, so "answered
  on the second candidate" and "answered nowhere" both have to behave;
- the **CI-V 0x27** scope geometry the vendor rig file documents
  (475 bins / 11 segments / amp 160), which is what replaced the raw-IQ
  producer an earlier revision assumed — see docs/g7250-wfview-analysis.md;
- the TX gate (unverified model must not key).

The retired LAN-IQ modules are gone; the protocol facts that matter live in the
analysis document, not in dead code.
"""
import asyncio
import os
import unittest
from unittest import mock

import config
from backends import create_backend, known_models, model_verification
from backends.xiegu import xiegu_profiles
from backends.xiegu.backend import G7250Backend


def _backend(**kwargs) -> G7250Backend:
    backend = create_backend("g7250", "/dev/null", **kwargs)
    assert isinstance(backend, G7250Backend)
    return backend


class ProfileAndRegistryTests(unittest.TestCase):
    def test_registered_and_reachable(self):
        self.assertIn("g7250", known_models())
        self.assertEqual(model_verification()["g7250"]["name"], "Xiegu G7250")
        self.assertFalse(model_verification()["g7250"]["verified"])

    def test_profile_is_xiegu_shaped(self):
        p = xiegu_profiles.get_profile("g7250")
        self.assertEqual(p.model_key, "g7250")
        self.assertEqual(p.civ_addr, xiegu_profiles.DEFAULT_CIV_ADDRESS)
        # No documented 1A 05 item: the empty payload is what makes
        # CivController skip the transceive step instead of guessing.
        self.assertEqual(p.transceive_item, b"")
        self.assertFalse(p.verified)
        self.assertTrue(p.has_atu)
        self.assertEqual(p.filter_model, "fil123")

    def test_bands_and_modes_match_the_radio(self):
        p = xiegu_profiles.get_profile("g7250")
        names = [b["name"] for b in p.bands]
        self.assertEqual(names[0], "160m")
        self.assertEqual(names[-1], "6m")
        # The G7250 is HF + 6 m only: no 4 m / 70 cm rows inherited.
        self.assertNotIn("70cm", names)
        self.assertNotIn("2m", names)
        self.assertEqual(set(p.mode_num_to_name.values()),
                         {"LSB", "USB", "AM", "CW-U", "CW-L", "FM"})
        self.assertNotIn("RTTY-L", p.mode_name_to_num)

    def test_default_baud_is_115200(self):
        self.assertEqual(config.default_baud_for("g7250"), 115200)


class CapabilityTests(unittest.TestCase):
    def test_spectrum_is_ci_v_not_lan_iq(self):
        """厂商 rig 文件：HasSpectrum=true / 475 / 160；分段数实测是 1。

        这套几何与 IC-7300 的 0x27 一致，也是我们 CI-V scope 生产者期望的形状
        —— 频谱走 CAT，与网口无关。**例外是分段数**：rig 文件写 11（USB 串口
        逐段投递的形状），网口实测是一帧 497 字节装下整条 475 点扫频、
        seq_max=1（2026-11-13 现场探针，约 8.5 帧/秒）。
        """
        backend = _backend()
        caps = backend.capabilities
        self.assertEqual(caps.scope_type, "civ27")
        self.assertEqual(caps.default_baud, 115200)
        p = xiegu_profiles.get_profile("g7250")
        self.assertEqual((p.scope_bins, p.scope_amp_max), (475, 160))
        self.assertEqual(p.scope_seq_max, 1,
                         "LAN 投递是整帧，不是 11 段；写错只会每次会话多一条误报警告")

    def test_the_scope_producer_is_the_shared_ci_v_one(self):
        from backends.ic7300.civ_scope import CivScopeProducer
        producer = _backend().create_scope_producer(None, None)
        self.assertIsInstance(producer, CivScopeProducer)

    def test_the_vendor_table_corrects_two_capability_flags(self):
        """`16 41` 自动陷波、`15 15/16` Vd/Id —— 共享默认是 False。"""
        caps = _backend().capabilities
        self.assertTrue(caps.has_auto_notch)
        self.assertTrue(caps.has_vd_id_meters)

    def test_scope_on_is_polled_again(self):
        """没有 0x27 的前提没了：scope_on 必须回到轮询表。"""
        fields = [f for f, _ in _backend().settings_poll_items()]
        self.assertIn("scope_on", fields)
        self.assertIn("filter_width", fields)

    def test_unverified_model_is_tx_gated(self):
        backend = _backend()
        self.assertFalse(backend.capabilities.verified)
        self.assertTrue(backend.capabilities.tx_gated)

    def test_refused_ptt_never_writes(self):
        backend = _backend()
        backend._civ.send_set_command = mock.AsyncMock(return_value=True)
        with mock.patch.object(config, "ALLOW_UNVERIFIED_TX", False, create=True):
            self.assertFalse(asyncio.run(backend.set_ptt(True)))
        backend._civ.send_set_command.assert_not_called()

    def test_ui_modes_have_no_rtty(self):
        self.assertEqual(_backend().ui_modes, ["LSB", "USB", "CW-U", "AM", "FM"])

    def test_a_stale_lan_address_is_named_and_ignored(self):
        """老 env 里的 MRRC_G7250_LAN_IP 不再悄悄失效，而是点名一次。"""
        with mock.patch.object(config, "G7250_LAN_IP", "192.168.110.199"):
            backend = _backend()
            with self.assertLogs("xiegu.backend", level="WARNING") as logs:
                backend._warn_about_the_lan_hint_once()
                backend._warn_about_the_lan_hint_once()
        hits = [line for line in logs.output if "MRRC_G7250_LAN_IP" in line]
        self.assertEqual(len(hits), 1, logs.output)


class _FakeController:
    """Stand-in for CivController: remembers the address, answers or not."""

    instances: list = []

    def __init__(self, port, baudrate, civ_addr, **kwargs):
        self.port, self.baudrate, self.civ_addr = port, baudrate, civ_addr
        self.transceive_cmd = kwargs.get("transceive_cmd")
        self.connected = False
        self.connect_calls = 0
        _FakeController.instances.append(self)

    async def connect(self):
        self.connected = True
        self.connect_calls += 1
        return True

    async def disconnect(self):
        self.connected = False

    async def get_frequency(self, vfo="A", timeout=None):
        if self.civ_addr == getattr(_FakeController, "answering_addr", None):
            return 14_074_000
        raise TimeoutError("no answer")

    async def send_set_command(self, cmd):
        return True


class AddressProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _FakeController.instances = []
        self._real = G7250Backend.__dict__.get("_make_controller")
        G7250Backend._make_controller = (
            lambda self, addr: _FakeController(self._port, self._baud, addr,
                                               transceive_cmd=b""))

    def tearDown(self):
        if self._real is None:
            del G7250Backend._make_controller
        else:                                              # pragma: no cover
            G7250Backend._make_controller = self._real

    async def test_second_candidate_is_adopted(self):
        _FakeController.answering_addr = 0x88
        backend = _backend()
        self.assertTrue(await backend.connect())
        self.assertEqual(backend.resolved_civ_addr, 0x88)
        # 0xA4 was tried first (and its controller was disconnected again).
        tried = [c.civ_addr for c in _FakeController.instances]
        self.assertEqual(tried, [0xA4, 0x88])
        self.assertFalse(_FakeController.instances[0].connected)

    async def test_no_answer_reports_every_candidate(self):
        _FakeController.answering_addr = None
        backend = _backend()
        self.assertFalse(await backend.connect())
        self.assertIsNone(backend.resolved_civ_addr)
        self.assertEqual([c.civ_addr for c in _FakeController.instances],
                         list(xiegu_profiles.CANDIDATE_CIV_ADDRESSES))
        self.assertTrue(all(not c.connected for c in _FakeController.instances))

    async def test_pinned_address_is_the_only_attempt(self):
        _FakeController.answering_addr = 0x70
        with mock.patch.object(config, "G7250_CIV_ADDR", "0x70"):
            backend = _backend()
            self.assertTrue(await backend.connect())
        self.assertEqual([c.civ_addr for c in _FakeController.instances], [0x70])


if __name__ == "__main__":
    unittest.main()


class _FakeSoundCard:
    """Stands in for the PyAudio handler (the TX half of the adapter)."""

    def __init__(self, *_a, **_kw):
        self.calls = []

    def start_tx(self):
        return True

    def stop_tx(self, *_a, **_kw):
        pass

    def feed_tx_audio(self, pcm):
        pass

    def write_tx_chunk(self):
        pass

    def tx_stats(self):
        return {"written": 0, "write_err": 0, "peak": 0, "queued": 0,
                "queue_drops": 0}

    def tx_queue_frames(self):
        return 0

    def has_pending_tx_audio(self):
        return False

    @property
    def device_rates(self):
        return {"rx": 48000, "tx": 48000}

    def close(self):
        pass


class LanCatWiringTests(unittest.TestCase):
    """`MRRC_G7250_NET=1` 时 CAT 也走网口隧道（0xA0）。

    现场实测（2026-11-12）：USB 侧 `0x27` 永不应答，而网络隧道 3 秒回 60 段
    瀑布。所以「CAT 走网口」不是优化，是这个机型拿到真频谱的唯一路径 ——
    失败时必须退回串口并说清楚原因，不能让操作员面对一条空瀑布。
    """

    def _backend(self, **env):
        from unittest import mock
        patches = {k: mock.patch.object(config, k, v) for k, v in env.items()}
        for p in patches.values():
            p.start()
            self.addCleanup(p.stop)
        return _backend()

    def test_off_by_default(self):
        backend = self._backend()
        self.assertIsNone(backend.network_client())

    def test_missing_credentials_name_the_key(self):
        from unittest import mock
        with mock.patch.object(config, "G7250_NET", True), \
                mock.patch.object(config, "G7250_NET_HOST", "192.168.110.199"), \
                mock.patch.object(config, "G7250_NET_USER", "G7250"), \
                mock.patch.object(config, "G7250_NET_PASS", ""):
            with self.assertLogs("xiegu.backend", level="WARNING") as logs:
                backend = _backend()
                self.assertIsNone(backend.network_client())
        self.assertTrue(any("MRRC_G7250_NET_PASS" in line for line in logs.output),
                        logs.output)

    def test_one_switch_covers_cat_and_audio(self):
        backend = self._backend(G7250_NET=True, G7250_NET_HOST="10.0.0.9",
                                G7250_NET_USER="u", G7250_NET_PASS="p")
        client = backend.network_client()
        self.assertIsNotNone(client)
        # the same client serves both halves: one session, three sockets
        self.assertIs(backend.network_client(), client)

    def test_the_controller_gets_the_network_transport(self):
        from backends.icom_net.transport import NetworkCivTransport
        backend = self._backend(G7250_NET=True, G7250_NET_CIV=True,
                                G7250_NET_HOST="10.0.0.9",
                                G7250_NET_USER="u", G7250_NET_PASS="p")
        backend.network_client()
        controller = backend._make_controller(0xA0)
        self.assertIsInstance(controller._transport, NetworkCivTransport)
        self.assertEqual(controller.civ_addr, 0xA0)

    def test_serial_keeps_its_transport_when_the_lan_path_is_off(self):
        backend = self._backend(G7250_NET=False)
        controller = backend._make_controller(0xA4)
        self.assertIsNone(controller._transport)


class NetworkAudioWiringTests(unittest.TestCase):
    """`MRRC_G7250_NET_AUDIO=1` 时才把网口音频交给服务端。

    现场事实：这台电台的内置 USB 声卡采集三天都是 −70 dBFS，而 wfview 从网口
    听得很好。所以「RX 音频走网络」是一个显式选项，缺参数时**退回声卡**并点名
    缺哪几项 —— 而不是静悄悄换一条没人验证过的路。
    """

    def _backend_with(self, **env):
        from unittest import mock
        patches = [mock.patch.object(config, k, v) for k, v in env.items()]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        return _backend()

    def test_off_by_default(self):
        self.assertIsNone(_backend().create_audio_handler())

    def test_on_without_credentials_falls_back_and_says_so(self):
        from unittest import mock
        with mock.patch.object(config, "G7250_NET_AUDIO", True), \
                mock.patch.object(config, "G7250_NET_HOST", "192.168.110.199"), \
                mock.patch.object(config, "G7250_NET_USER", ""), \
                mock.patch.object(config, "G7250_NET_PASS", ""):
            with self.assertLogs("xiegu.backend", level="WARNING") as logs:
                handler = _backend().create_audio_handler()
        self.assertIsNone(handler)
        self.assertTrue(any("MRRC_G7250_NET_USER" in line for line in logs.output),
                        logs.output)

    def test_on_with_credentials_builds_the_network_adapter(self):
        from unittest import mock
        from backends.icom_net.audio import IcomNetAudioHandler
        with mock.patch.object(config, "G7250_NET_AUDIO", True), \
                mock.patch.object(config, "G7250_NET_HOST", "192.168.110.199"), \
                mock.patch.object(config, "G7250_NET_USER", "G7250"), \
                mock.patch.object(config, "G7250_NET_PASS", "beta_tester"), \
                mock.patch("audio_handler.AudioHandler", _FakeSoundCard):
            backend = _backend()
            handler = backend.create_audio_handler()
        self.assertIsInstance(handler, IcomNetAudioHandler)
        self.assertEqual(backend._net_client.host, "192.168.110.199")
        self.assertEqual(backend._net_client.rx_sample, 16000)

    def test_the_host_falls_back_to_the_lan_address(self):
        from unittest import mock
        with mock.patch.object(config, "G7250_NET_AUDIO", True), \
                mock.patch.object(config, "G7250_NET_HOST", ""), \
                mock.patch.object(config, "G7250_LAN_IP", "192.168.110.199"), \
                mock.patch.object(config, "G7250_NET_USER", "G7250"), \
                mock.patch.object(config, "G7250_NET_PASS", "beta_tester"), \
                mock.patch("audio_handler.AudioHandler", _FakeSoundCard):
            backend = _backend()
            handler = backend.create_audio_handler()
        self.assertIsNotNone(handler)
        self.assertEqual(backend._net_client.host, "192.168.110.199")


class ServerCatPathTests(unittest.IsolatedAsyncioTestCase):
    """`server.py` 从不调用 `backend.connect()`，它拿 `backend.cat` 再 connect。

    这是 1.28.53 现场失败的真因：网口分支写在 `G7250Backend.connect()` 里，
    而启动路径根本不经过它 —— 日志里 COM3 在 44.698 就打开了，网口客户端
    到 48.199 才由音频处理器创建，CAT 永远留在串口上（瀑布只能是合成曲线）。
    所以 `cat` 必须返回一个**稳定的代理**：connect 时选路，其余调用转发给
    当前活着的那个 CivController。
    """

    async def test_cat_connect_falls_back_to_serial_when_lan_fails(self):
        from unittest import mock
        seen = []

        class Fake:
            def __init__(self, port, baud, addr, **_kw):
                self.civ_addr, self.connected = addr, False
                seen.append(addr)

            async def connect(self):
                self.connected = True
                return True

            async def disconnect(self):
                self.connected = False

            async def get_frequency(self, timeout=1):
                return 14000000 if self.civ_addr == 0xA4 else None

        with mock.patch.object(config, "G7250_NET", True), \
                mock.patch.object(config, "G7250_NET_HOST", "10.0.0.9"), \
                mock.patch.object(config, "G7250_NET_USER", "u"), \
                mock.patch.object(config, "G7250_NET_PASS", "p"), \
                mock.patch.object(G7250Backend, "_make_controller",
                                  lambda self, addr, **kw: Fake(self._port, self._baud, addr)), \
                mock.patch.object(G7250Backend, "_connect_network",
                                  lambda self: _false()):
            backend = _backend()
            cat = backend.cat                      # what server.py grabs
            # The failed LAN attempt rebuilds a serial controller before the
            # probe loop runs, so the loop's own first candidate is the one that
            # answers; assert on the address rather than the call count.
            self.assertTrue(await cat.connect())
            self.assertEqual(set(seen), {0xA4})
            self.assertTrue(cat.connected)         # delegation reaches the live one

    async def test_the_proxy_delegates_to_whatever_controller_is_live(self):
        backend = _backend()
        cat = backend.cat
        self.assertIs(cat._backend, backend)
        self.assertEqual(cat.connected, backend._civ.connected)


async def _false():
    return False
