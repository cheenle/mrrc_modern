"""The two pages, and the one rule they share.

`/setup` and `/settings` are separate HTML by decision (the onboarding page has
to work with no session at all, and the settings page must never be reachable
without one). They share a backend and a client script — this file keeps the
backend from growing a second implementation for one of them, and keeps the
onboarding page from acquiring anything a passwordless caller should not read.
"""
from __future__ import annotations

import asyncio
import re
import unittest
from pathlib import Path

import net_wifi
import server
from tests.test_server_box_window import FakeRequest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def run(coro):
    return asyncio.run(coro)


class SourceTests(unittest.TestCase):
    def test_both_pages_load_the_shared_client(self):
        for name in ("setup.html", "settings.html"):
            text = (STATIC / name).read_text(encoding="utf-8")
            with self.subTest(page=name):
                self.assertIn("box_api.js", text)

    def test_the_shared_client_is_not_versionless(self):
        """sw.js is cache-first, and a script served under an unchanged key is
        corrected on disk while the browser runs the old one."""
        for name in ("setup.html", "settings.html"):
            text = (STATIC / name).read_text(encoding="utf-8")
            with self.subTest(page=name):
                self.assertRegex(text, r"box_api\.js\?v=\d+")

    def test_the_onboarding_page_reads_only_these_three_endpoints(self):
        """spec §5.3. Two of the three are in the window; the third —
        /api/box/wifi — is not, and is reachable only because saving the
        password signs a session on the spot. So the Wi-Fi card must stay locked
        until that write succeeds, which is what `Box.disabled('#scan-btn',
        false)` inside `unlockWifi()` is for.

        Nothing else: the safety fields and the service list are not things an
        open page gets to enumerate."""
        text = (STATIC / "setup.html").read_text(encoding="utf-8")
        called = set(re.findall(r"Box\.api\('([^']+)'", text))
        self.assertEqual(called, {"/api/box/onboarding", "/api/box/password",
                                  "/api/box/wifi"})

    def test_the_settings_page_does_not_read_the_onboarding_endpoint(self):
        """That endpoint is the one thing a passwordless caller may reach. A page
        that already has a session has no business reading it."""
        text = (STATIC / "settings.html").read_text(encoding="utf-8")
        self.assertNotIn("/api/box/onboarding", text)

    def test_the_reboot_button_carries_its_confirmation(self):
        """A box reboot drops every connection and takes a minute to come back,
        and it sits directly above a button that restarts the service instead.
        Two buttons that read almost the same, doing very different things — so
        the page asks twice, and the second ask is the one that reaches the
        server (which checks for the literal string)."""
        text = (STATIC / "settings.html").read_text(encoding="utf-8")
        self.assertIn("confirm: 'reboot'", text)
        self.assertIn("window.confirm", text)

    def test_the_onboarding_page_warns_about_a_shared_network(self):
        """spec §5.4: the window's predicate is weaker than the retired
        hotspot's, and the honest mitigation is to say so where the operator can
        read it before they rely on it."""
        text = (STATIC / "setup.html").read_text(encoding="utf-8")
        self.assertIn("信任的网络", text)


class SettingsPageDeviceKeyTests(unittest.TestCase):
    """The page and `net_wifi.devices()` must agree on a device row's keys.

    The page once read `device.interface` while the producer emits the key
    `device`, so every adapter row rendered literally `网卡 undefined: connected
    → …`. Nothing caught it, because the page's keys and the producer's keys
    were never compared. This parses the `device.<key>` reads out of the page's
    own source and asserts each is a key `net_wifi.devices()` actually emits —
    an invariant over both sides, so a rename on either side fails here.
    """

    def emitted_keys(self):
        def runner(argv):
            return net_wifi.NmResult(
                0, "eth0:ethernet:connected:有线连接 2\n"
                   "wlan0:wifi:disconnected:\n", "")
        rows = net_wifi.devices(runner)
        self.assertTrue(rows, "the canned listing produced no device rows")
        return set(rows[0])

    def test_the_page_reads_only_keys_the_producer_emits(self):
        text = (STATIC / "settings.html").read_text(encoding="utf-8")
        read = set(re.findall(r"\bdevice\.([A-Za-z_$][\w$]*)", text))
        self.assertTrue(read, "the page reads no device fields at all")
        self.assertEqual(read - self.emitted_keys(), set())

    def test_the_error_was_the_interface_key_not_the_device_key(self):
        """Pin the exact regression: the render now reads `device.device`."""
        text = (STATIC / "settings.html").read_text(encoding="utf-8")
        self.assertIn("device.device", text)
        self.assertNotIn("device.interface", text)


class UnauthenticatedClientScriptTests(unittest.TestCase):
    """R-at. /setup renders before any session exists and loads its client with
    ``<script src="box_api.js">``, which the browser resolves — from /setup — to
    ``GET /box_api.js``. Two gates stand between that request and the script:
    the auth middleware's ``PUBLIC_PATHS`` set and ``serve_static``'s own
    exemption tuple. An unauthenticated caller must pass *both*, or the browser
    is handed the login page's HTML where it expected JavaScript: ``window.Box``
    is never defined, the inline handler throws, and every control on the
    onboarding page is dead — including the password form, which is the only
    thing that closes the first window.

    These call the real handlers rather than asserting a string is in a set,
    because the failure mode is a redirect and only running the code shows it.
    """

    def unauthenticated(self):
        # client=None, no cookies/headers/query_params — a caller who has never
        # logged in.
        return FakeRequest(path="/box_api.js", client=None)

    def test_the_middleware_passes_the_client_script_through(self):
        async def call_next(request):
            return "served"
        response = run(server.auth_middleware(self.unauthenticated(), call_next))
        self.assertEqual(response, "served")

    def test_serve_static_serves_the_client_script_without_a_session(self):
        response = run(server.serve_static("box_api.js", self.unauthenticated()))
        self.assertEqual(getattr(response, "status_code", 200), 200)
        self.assertEqual(str(response.media_type), "application/javascript")


class RouteTests(unittest.TestCase):
    def test_the_settings_page_is_served(self):
        response = run(server.settings_page(None))
        self.assertEqual(getattr(response, "status_code", 200), 200)

    def test_both_page_routes_sit_above_the_spa_fallback(self):
        """A GET route registered below the catch-all is answered with
        index.html instead of its own handler — that is how /api/cloud/state came
        back as a web page in v1.24.0."""
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        fallback = source.index('app.add_api_route("/{path:path}", serve_static')
        for route in ('@app.get("/setup"', '@app.get("/settings"'):
            with self.subTest(route=route):
                self.assertLess(source.index(route), fallback)

    def test_no_document_names_the_retired_manage_page(self):
        for relative in ("server.py", "static/index.html", "static/listen.html"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(path=relative):
                self.assertNotIn('"/manage"', text)
                self.assertNotIn("manage.html", text)


class SettingsWifiPanelTests(unittest.TestCase):
    """手输 SSID 与“输入时明文”的形态 —— 以及它没有变成回显。"""

    def setUp(self):
        self.text = (STATIC / "settings.html").read_text(encoding="utf-8")

    def test_the_ssid_can_be_typed_by_hand(self):
        # 隐藏网络与“扫描返回空表”都只能手输；点扫描结果只是把它**填入**这个框。
        self.assertIn('id="ssid-input"', self.text)
        self.assertIn("document.querySelector('#ssid-input').value", self.text)

    def test_the_password_is_masked_until_the_operator_asks(self):
        self.assertIn('id="psk" type="password"', self.text)
        self.assertIn("field.type = shown ? 'password' : 'text'", self.text)

    def test_an_empty_scan_explains_itself(self):
        """空白列表看起来像“没点对”——它其实是芯片刚重启或网络是隐藏的。"""
        self.assertIn("没有扫到任何 Wi-Fi", self.text)


class SettingsPageMarkupTests(unittest.TestCase):
    """本页不再用 innerHTML 拼或清 DOM（2026-10-17 收敛后钉住）。"""

    def test_no_markup_is_built_with_innerHTML(self):
        text = (STATIC / "settings.html").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", text)


class WifiSecretNeverComesBackTests(unittest.TestCase):
    """NFR-068 / AD-021：Wi-Fi 密码只进 nmcli 的 argv，不回显、不落盘。"""

    PAGES = ("settings.html", "setup.html", "box_api.js", "sw.js")
    MODULES = ("net_wifi.py", "box_ops.py", "server.py")

    def test_no_page_reads_a_stored_psk_back(self):
        for name in self.PAGES:
            text = (STATIC / name).read_text(encoding="utf-8")
            with self.subTest(page=name):
                self.assertNotIn("show-secrets", text)
                self.assertNotIn("wireless-security.psk", text)

    def test_the_server_never_asks_nm_for_a_secret(self):
        """NFR-068 / AD-021：Wi-Fi 密码只被**写入** profile，绝不读回来。

        写入只有一处：`net_wifi.connect_wifi` 的 join argv（`connection
        modify … psk`）。读回来的形状有四种，四种都在这里被拒：
        `--show-secrets`、`-g`/`--get-values` 查 psk 字段、`connection export`、
        以及直接读 NetworkManager 的 keyfile 目录。

        2026-10-17 这条守卫在原位红过一次：join 从 `device wifi connect …
        password` 改成「写 profile 再 up」（AP 不在扫描列表里也得能配），
        于是一个**合法的写入**撞上了原先按字面量切的断言。按形状钉住
        “读”，比按字面量封死整个 key 更接近要保护的东西。
        """
        for name in self.MODULES:
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertNotIn("show-secrets", text)
                self.assertNotIn("connection export", text)
                self.assertNotIn("system-connections", text)
                self.assertIsNone(
                    re.search(r"(?:-g|--get-values)[^\n]*psk", text,
                              re.IGNORECASE),
                    f"{name} queries nmcli for the psk")

        # 密码只在 join 里写一次，且必须与它所属的安全类型同处一行上下文。
        source = (ROOT / "net_wifi.py").read_text(encoding="utf-8")
        mentions = [line.strip() for line in source.splitlines()
                    if "wireless-security.psk" in line]
        self.assertEqual(len(mentions), 1, mentions)
        self.assertIn('"wpa-psk"', source)


class CloudPanelTests(unittest.TestCase):
    """公网入口面板只允许用既有的三个端点（不新增写入路径）。"""

    def setUp(self):
        self.text = (STATIC / "settings.html").read_text(encoding="utf-8")

    def test_the_panel_uses_exactly_the_three_existing_endpoints(self):
        used = set(re.findall(r"/api/cloud/[a-z_]+", self.text))
        self.assertEqual(used, {"/api/cloud/state", "/api/cloud/apply",
                                "/api/cloud/refresh"})

    def test_a_box_that_already_has_a_token_does_not_apply_again(self):
        """门户对同一呼号的重复申请是查重拒绍的；重提只会把“已存在申请”糊到脸上。"""
        self.assertIn("if (cloud.has_token) return refreshCloud();", self.text)

    def test_the_callsign_field_says_what_changing_it_means(self):
        self.assertIn("旧入口不会自动迁移", self.text)

    def test_the_panel_says_whether_the_tunnel_is_running(self):
        """「已接通」只看配置里有没有入口字符串，不看隧道真的在跑。

        2026-10-22 现场：公网入口连续四天 502，而盒子的这个面板写着「已接通」——
        两条互相矛盾的信息摆在同一个屏幕上。服务端早就把 `tunnel_running` /
        `tunnel_error` 发出来了（桌面端对话框 `modules/cloud_hub.js` 一直在渲染），
        唯独盒子设置页只在 `tunnel_error` 非空时才提隧道，于是「隧道没起来而 error 为空」
        （恰恰是 frpc 缺失那种）又变成沉默。
        """
        self.assertIn("cloud.tunnel_running", self.text)
        self.assertNotIn("if (cloud.tunnel_error) kv(", self.text,
                         "只在出错时才显示隧道 —— 那正是沉默的写法")
        self.assertIn("未运行", self.text)


if __name__ == "__main__":
    unittest.main()

class BtPairEndpointTests(unittest.TestCase):
    """配对端点必须把原因交给操作员，并且把「按键可用」当作成功判据。

    现场（2026-10-18）：网页点「配对」失败，页面只显示"配不上"；真正的原因
    （遥控器没进配对模式 → bluez `Device ... not available`）被这里重新取的
    `state()` 覆盖掉了。一个说不出下一步的失败提示等于没有提示。
    """

    def _post(self, pair_result, state_result):
        import asyncio
        from types import SimpleNamespace
        from unittest import mock
        import server

        req = SimpleNamespace(headers={}, cookies={"mrrc_auth": "t"},
                              client=SimpleNamespace(host="127.0.0.1"),
                              url=SimpleNamespace(path="/api/box/bt/pair", query=""))

        async def body():
            return {"address": "0C:F3:CE:B0:88:75"}

        req.json = body
        with mock.patch.object(server, "_verify_auth", return_value=True), \
             mock.patch.object(server.net_bt, "pair", return_value=pair_result), \
             mock.patch.object(server.net_bt, "state", return_value=dict(state_result)):
            return asyncio.run(server.api_box_bt_pair(req))

    def test_error_from_pair_is_surfaced(self):
        r = self._post({"error": "遥控器没有在广播：按住「OK + 返回」约 5 秒再点一次。"},
                       {"paired": False, "keys_work": False})
        import json
        payload = json.loads(r.body)
        self.assertIn("OK + 返回", payload["error"])
        self.assertFalse(payload["ok"])
        self.assertEqual(r.status_code, 400)

    def test_paired_but_dead_keys_is_not_ok(self):
        r = self._post({"error": "已配对，但按键还没反应——点「重配」再试一次"},
                       {"paired": True, "keys_work": False})
        import json
        payload = json.loads(r.body)
        self.assertFalse(payload["ok"])
        self.assertIn("重配", payload["error"])

    def test_working_remote_is_ok(self):
        r = self._post({}, {"paired": True, "keys_work": True})
        import json
        self.assertTrue(json.loads(r.body)["ok"])
        self.assertEqual(r.status_code, 200)

