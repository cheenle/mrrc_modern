"""`/Nano` 的路由与四道守卫（spec §4.7）。不碰硬件：设备对象是假的。

守卫顺序是被断言的东西：开关/角色 → 发射中 → 繁忙 → 参数。顺序错了会让
"电台在发射"被 400 掩盖，而这正是最危险的输入路径。
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import server
from nano import Sweep


class FakeRequest:
    def __init__(self, cookies=None, body=None):
        self.url = SimpleNamespace(path="/api/nano/scan", query="")
        self.method = "POST"
        self.headers = {}
        self.cookies = cookies or {}
        self.query_params = {}
        self._body = body or {}
        self.client = SimpleNamespace(host="127.0.0.1")

    async def json(self):
        return self._body


class BusyLock:
    """一个"别人正拿着"的锁：进不去，也不会真进。"""
    def locked(self):
        return True

    async def __aenter__(self):
        raise AssertionError("handler must not enter the lock while busy")

    async def __aexit__(self, *exc):
        return False


class FakeDevice:
    def __init__(self):
        self.port = "/dev/fake"
        self.sent = []
        self.restored = None

    def info(self):
        return SimpleNamespace(board="NanoVNA-H 4", version="1.2.14", build="",
                               platform="", points_max=401)

    def vbat_mv(self):
        return 3627

    def cal_state(self):
        return "LOAD"

    def read_settings(self):
        return {"smooth": 0, "power": 2}

    def scan(self, start, stop, points):
        n = points
        return Sweep(freqs=[start + i for i in range(n)],
                     s11=[(0.0, 0.0)] * n, elapsed_ms=42)

    def restore_sweep(self, start, stop, points):
        self.restored = (start, stop, points)
        return True

    def set_setting(self, name, value):
        if name == "tcxo":
            raise ValueError("setting not allowed: 'tcxo'")
        self.sent.append((name, value))

    def save_cal(self, slot):
        self.sent.append(("save", slot))

    def recall_cal(self, slot):
        self.sent.append(("recall", slot))


class RoleGateTest(unittest.TestCase):
    def setUp(self):
        self.token = "nano-test-token"
        server._auth_tokens.add(self.token)
        self.req = FakeRequest(cookies={"mrrc_auth": self.token})

    def tearDown(self):
        server._auth_tokens.discard(self.token)
        server._listen_tokens.discard(self.token)
        server._sdr_tokens.discard(self.token)

    def test_operator_is_allowed(self):
        self.assertIsNone(server._nano_forbidden(self.req))

    def test_listen_session_is_forbidden(self):
        server._listen_tokens.add(self.token)
        self.assertEqual(server._nano_forbidden(self.req)[0], 403)

    def test_sdr_session_is_forbidden(self):
        server._sdr_tokens.add(self.token)
        sdr_req = FakeRequest(cookies={"mrrc_sdr": self.token})
        self.assertEqual(server._nano_forbidden(sdr_req)[0], 403)

    def test_disabled_feature_looks_not_found(self):
        with mock.patch.object(server, "NANO_ENABLED", False):
            self.assertEqual(server._nano_forbidden(self.req)[0], 404)

    def test_page_refuses_listen_session(self):
        server._listen_tokens.add(self.token)
        with self.assertRaises(server.HTTPException) as ctx:
            asyncio.run(server.nano_page(self.req))
        self.assertEqual(ctx.exception.status_code, 403)


class ScanGuardTest(unittest.TestCase):
    def setUp(self):
        self.token = "nano-scan-token"
        server._auth_tokens.add(self.token)
        self.req = FakeRequest(cookies={"mrrc_auth": self.token},
                               body={"start": 1_000_000, "stop": 54_000_000, "points": 401})
        self.addCleanup(lambda: server._auth_tokens.discard(self.token))
        self.addCleanup(lambda: setattr(server, "_nano_lock", asyncio.Lock()))

    def _post(self, **patch):
        with mock.patch.multiple(server, **patch):
            return asyncio.run(server.api_nano_scan(self.req))

    def test_transmitting_wins_over_everything(self):
        r = self._post(_nano_tx_active=mock.MagicMock(return_value=True))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(json.loads(r.body)["error"], "tx_active")

    def test_busy_returns_409_without_blocking(self):
        r = self._post(_nano_tx_active=mock.MagicMock(return_value=False),
                       _nano_lock=BusyLock())
        self.assertEqual(r.status_code, 409)
        self.assertEqual(json.loads(r.body)["error"], "busy")

    def test_bad_range_is_rejected_before_touching_the_device(self):
        self.req._body = {"start": 54_000_000, "stop": 1_000_000, "points": 401}
        with mock.patch.object(server, "_nano_tx_active", return_value=False), \
             mock.patch.object(server, "_nano_require_device") as ensure:
            r = asyncio.run(server.api_nano_scan(self.req))
        self.assertEqual(r.status_code, 400)
        ensure.assert_not_called()

    def test_happy_path_returns_physics_and_restores_device(self):
        dev = FakeDevice()
        with mock.patch.object(server, "_nano_tx_active", return_value=False), \
             mock.patch.object(server, "_nano_require_device", return_value=dev), \
             mock.patch.object(server, "_nano_capture_state", return_value={}), \
             mock.patch.object(server, "_nano_read_state",
                               return_value={"start_hz": 50_000, "stop_hz": 900_000_000,
                                             "points": 101}):
            r = asyncio.run(server.api_nano_scan(self.req))
        self.assertEqual(r.status_code, 200)
        payload = json.loads(r.body)
        self.assertEqual(len(payload["freqs"]), 401)
        self.assertEqual(payload["swr"][0], 1.0)          # 0+0j 是匹配负载
        self.assertTrue(payload["restored"])
        self.assertEqual(dev.restored, (50_000, 900_000_000, 101))

    def test_device_failure_is_503_and_drops_the_handle(self):
        with mock.patch.object(server, "_nano_tx_active", return_value=False), \
             mock.patch.object(server, "_nano_require_device",
                               side_effect=server.NanoError("gone")), \
             mock.patch.object(server, "_nano_forget") as forget:
            r = asyncio.run(server.api_nano_scan(self.req))
        self.assertEqual(r.status_code, 503)
        forget.assert_called_once()


class SettingsAndCalTest(unittest.TestCase):
    def setUp(self):
        self.token = "nano-set-token"
        server._auth_tokens.add(self.token)
        self.addCleanup(lambda: server._auth_tokens.discard(self.token))
        self.addCleanup(lambda: setattr(server, "_nano_lock", asyncio.Lock()))

    def _req(self, body):
        return FakeRequest(cookies={"mrrc_auth": self.token}, body=body)

    def test_whitelist_rejects_tcxo(self):
        dev = FakeDevice()
        with mock.patch.object(server, "_nano_tx_active", return_value=False), \
             mock.patch.object(server, "_nano_require_device", return_value=dev):
            r = asyncio.run(server.api_nano_settings(
                self._req({"name": "tcxo", "value": 26000000})))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(json.loads(r.body)["error"], "bad_setting")

    def test_settings_applied(self):
        dev = FakeDevice()
        with mock.patch.object(server, "_nano_tx_active", return_value=False), \
             mock.patch.object(server, "_nano_require_device", return_value=dev):
            r = asyncio.run(server.api_nano_settings(
                self._req({"name": "smooth", "value": 3})))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(dev.sent, [("smooth", 3)])

    def test_cal_save_requires_typed_confirmation(self):
        with mock.patch.object(server, "_nano_tx_active", return_value=False):
            r = asyncio.run(server.api_nano_cal(
                self._req({"slot": 1, "action": "save"})))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(json.loads(r.body)["error"], "confirm_required")

    def test_cal_recall_needs_no_confirmation(self):
        dev = FakeDevice()
        with mock.patch.object(server, "_nano_tx_active", return_value=False), \
             mock.patch.object(server, "_nano_require_device", return_value=dev):
            r = asyncio.run(server.api_nano_cal(
                self._req({"slot": 1, "action": "recall"})))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(dev.sent, [("recall", 1)])


class SweepStoreTest(unittest.TestCase):
    def setUp(self):
        self.token = "nano-store-token"
        server._auth_tokens.add(self.token)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(server, "NANO_DIR", self.dir),
            mock.patch.object(server, "NANO_SWEEPS_DIR", self.dir / "sweeps"),
            mock.patch.object(server, "NANO_STATE_FILE", self.dir / "state.json"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.addCleanup(lambda: server._auth_tokens.discard(self.token))
        self.req = FakeRequest(cookies={"mrrc_auth": self.token})

    def test_save_list_get_delete_roundtrip(self):
        dev = FakeDevice()
        server._nano_last_sweep = {"freqs": [1, 2, 3], "s11": [(0.0, 0.0)] * 3,
                                  "start_hz": 1_000_000, "stop_hz": 3_000_000,
                                  "points": 3}
        with mock.patch.object(server, "_nano_device", dev):
            saved = asyncio.run(server.api_nano_save_sweep(
                FakeRequest(cookies={"mrrc_auth": self.token},
                            body={"label": "40m 雨前", "note": "湿"})))
        self.assertEqual(saved.status_code, 200)
        sid = json.loads(saved.body)["id"]

        listing = asyncio.run(server.api_nano_sweeps(self.req))
        items = json.loads(listing.body)["sweeps"]
        self.assertEqual([i["id"] for i in items], [sid])
        self.assertEqual(items[0]["label"], "40m 雨前")

        got = asyncio.run(server.api_nano_get_sweep(sid, self.req))
        payload = json.loads(got.body)
        self.assertEqual(payload["points"], 3)
        self.assertEqual(payload["swr"], [1.0, 1.0, 1.0])

        csv_out = asyncio.run(server.api_nano_get_sweep(sid, self.req, format="csv"))
        self.assertIn("freq_hz,s11_re,s11_im,swr,r_ohm,x_ohm", csv_out.body.decode())

        s1p_out = asyncio.run(server.api_nano_get_sweep(sid, self.req, format="s1p"))
        self.assertIn("# Hz S RI R 50", s1p_out.body.decode())

        gone = asyncio.run(server.api_nano_delete_sweep(sid, self.req))
        self.assertEqual(gone.status_code, 200)
        self.assertEqual(asyncio.run(server.api_nano_sweeps(self.req)).status_code, 200)

    def test_path_traversal_id_is_refused(self):
        with self.assertRaises(server.HTTPException) as ctx:
            asyncio.run(server.api_nano_get_sweep("../../etc/passwd", self.req))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_save_without_a_sweep_is_409(self):
        server._nano_last_sweep = None
        r = asyncio.run(server.api_nano_save_sweep(self.req))
        self.assertEqual(r.status_code, 409)


class PageAssetsTest(unittest.TestCase):
    """前缀安全：接口地址只能从页面自己的 location 推出来（tests/test_path_prefix.py 的规矩）。"""

    def test_js_never_hardcodes_an_absolute_api_url(self):
        js = (Path(server.__file__).parent / "static" / "nano.js").read_text(encoding="utf-8")
        self.assertNotIn("fetch('/api", js)
        self.assertNotIn('fetch("/api', js)
        self.assertIn("location.pathname.lastIndexOf('/Nano')", js)

    def test_page_loads_its_own_script_and_never_the_radio_monoliths(self):
        html = (Path(server.__file__).parent / "static" / "nano.html").read_text(encoding="utf-8")
        self.assertIn("nano.js?v=", html)
        self.assertNotIn("ft710_main.js", html)
        self.assertNotIn("ft710_ui.js", html)


class FrequencyRebuildTest(unittest.TestCase):
    def test_linear_rebuild_matches_device_grid(self):
        freqs = server._nano_sweep_freqs({"start_hz": 50_000, "stop_hz": 900_000_000,
                                          "points": 101})
        self.assertEqual(freqs[0], 50_000)
        self.assertEqual(freqs[-1], 900_000_000)
        self.assertEqual(len(freqs), 101)

    def test_explicit_freqs_win(self):
        self.assertEqual(server._nano_sweep_freqs({"freqs": [1, 2]}), [1, 2])


if __name__ == "__main__":
    unittest.main()
