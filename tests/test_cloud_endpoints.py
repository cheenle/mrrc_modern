"""Regression coverage for the Cloud Hub endpoints in ``server.py``.

On 2026-10-01 a packaged v1.24.0 instance answered the settings dialog's
云端申请 with ``500 Internal Server Error`` and this traceback::

    File "server.py", line 4035, in api_cloud_apply
    File "server.py", line 3987, in _cloud_portal
    NameError: name 'cloud_hub' is not defined

Two independent breakages produced it: ``server.py`` called ``cloud_hub.*``
without ever importing the module, and the refresh endpoint asked ``config``
for ``WEB_PORT`` while this module only binds the bare name
(``from config import ..., WEB_PORT, ...``). Both slipped past
``test_cloud_hub.py`` because that file exercises the module in isolation -
``import cloud_hub`` at the top of a test is exactly what masked the missing
import in the server. These tests drive the HTTP endpoints themselves, so a
missing binding can no longer hide behind a passing unit test.
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cloud_hub
import server


class _Request:
    """Only what the endpoints touch: auth and the JSON body (auth is patched)."""

    def __init__(self, body=None):
        self._body = body or {}
        self.headers = {"cookie": f"{server.AUTH_COOKIE}=test-token"}

    async def json(self):
        return self._body


class CloudApplyEndpointTests(unittest.TestCase):
    """POST /api/cloud/apply must reach the portal and persist the request token."""

    def test_apply_uses_the_cloud_hub_module_and_keeps_the_token(self):
        request = _Request({"callsign": "bg9zzz", "contact": "op@example.com"})
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "mrrc_modern.env"
            with patch.object(server, "_verify_auth", return_value=True), \
                 patch.object(server, "_config_file_path", return_value=env), \
                 patch.object(cloud_hub, "apply",
                              return_value={"request_token": "req-token",
                                            "status": "applied"}) as apply_mock, \
                 patch.object(server, "_cloud_start_autoconnect") as start_mock:
                reply = asyncio.run(server.api_cloud_apply(request))
                # 提交之后就必须自己看着，而不是等谁把对话框开着（见 CloudAutoconnectTests）。
                # 这里替换掉它，否则测试进程会真的起一个轮询线程去打生产 hub。
                start_mock.assert_called_once()

            payload = json.loads(bytes(reply.body))
            self.assertEqual(reply.status_code, 200)
            self.assertTrue(payload["submitted"])
            self.assertEqual(payload["callsign"], "BG9ZZZ")
            apply_mock.assert_called_once_with(cloud_hub.PORTAL_DEFAULT, "BG9ZZZ",
                                               "op@example.com", "mrrc_modern")
            written = env.read_text(encoding="utf-8")
            self.assertIn("MRRC_CLOUD_TOKEN=req-token", written)
            self.assertIn("MRRC_CLOUD_CALLSIGN=BG9ZZZ", written)


class CloudRefreshEndpointTests(unittest.TestCase):
    """POST /api/cloud/refresh must connect an approved instance using the app's port."""

    def test_refresh_connects_with_the_real_web_port_and_records_the_entry(self):
        request = _Request()
        grant = {"connected": True, "status": "granted", "label": "bg9zzz",
                 "port": 18899, "fqdn": "bg9zzz.mrrc.vlsc.net",
                 "entry": "https://bg9zzz.mrrc.vlsc.net/",
                 "cert": "C:/certs/bg9zzz.crt", "tunnel_config": "C:/fleet/frpc-bg9zzz.toml",
                 "tunnel_started": True}
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "mrrc_modern.env"
            env.write_text("MRRC_CLOUD_CALLSIGN=BG9ZZZ\nMRRC_CLOUD_TOKEN=req-token\n",
                           encoding="utf-8")
            with patch.object(server, "_verify_auth", return_value=True), \
                 patch.object(server, "_config_file_path", return_value=env), \
                 patch.object(cloud_hub, "status", return_value={"status": "granted"}), \
                 patch.object(cloud_hub, "connect", return_value=grant) as connect_mock:
                reply = asyncio.run(server.api_cloud_refresh(request))

            payload = json.loads(bytes(reply.body))
            self.assertEqual(reply.status_code, 200)
            self.assertTrue(payload["connected"])
            # server.py imports WEB_PORT as a bare name; asking for config.WEB_PORT
            # raises NameError and turns an approved instance into a 500.
            self.assertEqual(connect_mock.call_args.kwargs["local_port"], server.WEB_PORT)
            # The entry is written to the launcher's config file so the tunnel
            # survives the next restart.
            written = env.read_text(encoding="utf-8")
            self.assertIn("MRRC_CLOUD_LABEL=bg9zzz", written)


class ServerRouteOrderTests(unittest.TestCase):
    """The SPA fallback must not swallow API routes.

    ``serve_static`` matches *every* GET path. In v1.24.0 it was registered (by decorator, in the
    middle of the module) before the Cloud Hub endpoints, so a frozen build answered
    ``GET /api/cloud/state`` with 200 + index.html - the settings dialog could never read its own
    state, while the POST endpoints beside it reached their handlers and 500'd for the missing
    import. Route order is the only thing that keeps this honest, so it is asserted here.
    """

    @staticmethod
    def _paths() -> list[str]:
        return [getattr(route, "path", "") for route in server.app.router.routes]

    def test_the_state_route_is_registered_before_the_spa_fallback(self):
        paths = self._paths()
        self.assertIn("/api/cloud/state", paths)
        self.assertLess(paths.index("/api/cloud/state"), paths.index("/{path:path}"))

    def test_no_api_route_is_registered_after_the_spa_fallback(self):
        paths = self._paths()
        catch_all = paths.index("/{path:path}")
        shadowed = [p for p in paths[catch_all + 1:] if p.startswith("/api/")]
        self.assertEqual(shadowed, [],
                         "these API routes would be answered with index.html: " + repr(shadowed))



class CertificateReloadClockTests(unittest.TestCase):
    """_cert_reload_required 必须比较**同一个时钟**。

    它要回答的是"正在被服务的那张证书，是不是磁盘上这一张"。原先拿 st_mtime（epoch 秒，
    ≈1.79e9）去比 time.monotonic()（开机以来秒数，≈3.8e5），于是**任何已配置的证书都被
    判成"进程启动后才写的"** —— 三天前的证书也一样（2026-10-03 实测）。

    在只有人工点"重启"按钮时，后果只是那句提示永远消不掉。但接入一旦自己重启，它就会
    变成**无限重启循环**：每次重启后判断依旧为真。所以这条先修。
    """

    def test_a_certificate_older_than_this_process_needs_no_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "fullchain.pem"
            cert.write_text("dummy", encoding="utf-8")
            old = time.time() - 3600                      # 一小时前，早于进程启动
            os.utime(cert, (old, old))
            with patch.dict(os.environ, {"MRRC_SSL_CERT": str(cert)}):
                self.assertFalse(server._cert_reload_required(),
                                 "一小时前的证书被当成刚写入的 —— 又把 epoch 和 monotonic 比了")

    def test_a_certificate_written_after_start_asks_for_a_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "fullchain.pem"
            cert.write_text("dummy", encoding="utf-8")
            with patch.object(server, "_PROCESS_STARTED_AT", time.time() - 60), \
                 patch.dict(os.environ, {"MRRC_SSL_CERT": str(cert)}):
                self.assertTrue(server._cert_reload_required())


class CertificateReloadSelectionTests(unittest.TestCase):
    """登记写进来的证书与进程正在服务的那张**不是同一个文件**时，必须重启。

    2026-10-03 实测：BG6LH 登记成功后 /api/cloud/refresh 返回 cert_reload_required=false，
    而配置文件里已经是 certs\\fullchain.pem、进程服务的却是启动时的 certs\\server.crt。

    原因是这个判据读的两处都不是"配置现在要求的那张"：
      - `_cloud_settings()` 的键白名单 ``_CLOUD_KEYS`` 里**没有** MRRC_SSL_CERT，那一项恒为 None；
      - 另一处是 ``os.environ`` —— 而环境是**进程启动那一刻**的，登记却发生在启动之后。

    后果不是少一句提示：入口会一直 502（hub 拿登记的那张证书校验上游），界面不给重启按钮，
    无人值守的自动接入也不会重启 —— "接入成功"和"入口能用"之间那道缝正好落在这里。
    """

    def _config(self, tmp, value):
        env = Path(tmp) / "mrrc_modern.env"
        env.write_text(f"MRRC_SSL_CERT={value}\n" if value else "MRRC_WEB_PORT=8888\n",
                       encoding="utf-8")
        return env

    def test_serving_a_different_file_than_the_config_asks_for_asks_for_a_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            enrolled = Path(tmp) / "fullchain.pem"
            enrolled.write_text("enrolled", encoding="utf-8")
            old = time.time() - 3600
            os.utime(enrolled, (old, old))          # 文件旧也没关系：服务的是**另一个**文件
            env = self._config(tmp, enrolled)
            with patch.object(server, "_config_file_path", return_value=env):
                server._record_serving_cert({"ssl_certfile": str(Path(tmp) / "server.crt")})
                self.assertTrue(server._cert_reload_required(),
                                "登记的那张证书没被服务，却不要求重启 —— 入口会一直 502")

    def test_serving_the_same_file_older_than_this_process_needs_no_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = Path(tmp) / "fullchain.pem"
            cert.write_text("served", encoding="utf-8")
            old = time.time() - 3600
            os.utime(cert, (old, old))
            env = self._config(tmp, cert)
            with patch.object(server, "_config_file_path", return_value=env):
                server._record_serving_cert({"ssl_certfile": str(cert)})
                self.assertFalse(server._cert_reload_required())

    def test_no_configured_certificate_means_nothing_to_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._config(tmp, "")
            with patch.object(server, "_config_file_path", return_value=env), \
                 patch.dict(os.environ, {"MRRC_SSL_CERT": ""}):
                server._record_serving_cert({})
                self.assertFalse(server._cert_reload_required())

    def test_the_serving_path_is_recorded_at_startup(self):
        """实现了却没人调用 —— 这个仓库已经栽过一次（_cloud_start_tunnel 只有一个调用点）。"""
        source = Path(server.__file__).read_text(encoding="utf-8")
        self.assertIn("_record_serving_cert(ssl_kwargs)", source)


class CloudAutoconnectTests(unittest.TestCase):
    """批准必须能自己走到实例上，**不需要有人开着对话框**。

    2026-10-03 实测：BG6LH 于 17:45:04 提交申请、运维于 17:46:01 批准；hub 的 nginx 访问
    日志显示此后客户端**再也没有发过 /status**（当天最后一条 /status 是 10:37，来自另一台
    机器）。于是应用一直停在那句"已提交申请"。原因是当时唯一的轮询循环住在对话框里，
    `open()` 才 setInterval、`close()` 就 clearInterval —— 而租户不会把那个对话框开上好几天。

    所以轮询要搬到服务端：只要本机有申请令牌且尚未接入，就自己去问，批准后自己完成登记
    （签证书 / 登记 / 起隧道），并把结果留在 /api/cloud/state 里让人看得见。
    """

    APPS = {"MRRC_CLOUD_CALLSIGN": "BG6LH", "MRRC_CLOUD_TOKEN": "req-token"}

    GRANT = {"connected": True, "status": "granted", "label": "bg6lh", "port": 18806,
             "fqdn": "bg6lh.mrrc.vlsc.net", "entry": "https://bg6lh.mrrc.vlsc.net/",
             "cert": "C:/certs/bg6lh.crt", "tunnel_config": "C:/fleet/frpc-bg6lh.toml",
             "tunnel_started": True}

    def _config(self, tmp, **overrides):
        env = Path(tmp) / "mrrc_modern.env"
        values = dict(self.APPS, **overrides)
        env.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
        return env

    def test_polls_the_hub_and_connects_when_the_operator_granted(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._config(tmp)
            with patch.object(server, "_config_file_path", return_value=env), \
                 patch.object(server, "_cert_reload_required", return_value=False), \
                 patch.object(cloud_hub, "status",
                              return_value={"status": "granted"}) as status_mock, \
                 patch.object(cloud_hub, "connect", return_value=self.GRANT) as connect_mock, \
                 patch.object(server, "_cloud_restart_now") as restart_mock:
                result = server._cloud_autoconnect_once()

            self.assertEqual(status_mock.call_count, 1, "没有去问 hub")
            self.assertEqual(connect_mock.call_count, 1, "批准了却没有接入")
            self.assertEqual(connect_mock.call_args.kwargs["local_port"], server.WEB_PORT)
            written = env.read_text(encoding="utf-8")
            self.assertIn("MRRC_CLOUD_LABEL=bg6lh", written)
            self.assertIn("MRRC_CLOUD_ENTRY=https://bg6lh.mrrc.vlsc.net/", written)
            self.assertEqual(result["status"], "connected")
            restart_mock.assert_not_called()

    def test_without_an_application_it_sends_no_traffic(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "mrrc_modern.env"
            env.write_text("MRRC_WEB_PORT=8888\n", encoding="utf-8")
            with patch.object(server, "_config_file_path", return_value=env), \
                 patch.object(cloud_hub, "status") as status_mock:
                result = server._cloud_autoconnect_once()
            status_mock.assert_not_called()
            self.assertEqual(result["status"], "idle")

    def test_an_instance_already_connected_does_not_poll_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._config(tmp, MRRC_CLOUD_LABEL="bg6lh")
            with patch.object(server, "_config_file_path", return_value=env), \
                 patch.object(cloud_hub, "status") as status_mock:
                result = server._cloud_autoconnect_once()
            status_mock.assert_not_called()
            self.assertEqual(result["status"], "connected")

    def test_a_pending_application_is_asked_about_but_not_connected(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = self._config(tmp)
            with patch.object(server, "_config_file_path", return_value=env), \
                 patch.object(cloud_hub, "status", return_value={"status": "applied"}), \
                 patch.object(cloud_hub, "connect") as connect_mock:
                result = server._cloud_autoconnect_once()
            connect_mock.assert_not_called()
            self.assertEqual(result["status"], "applied")

    def test_an_unreachable_hub_is_recorded_rather_than_raised(self):
        """轮询线程里抛异常会静默杀死轮询 —— 证据要留下，进程要活着。"""
        with tempfile.TemporaryDirectory() as tmp:
            env = self._config(tmp)
            with patch.object(server, "_config_file_path", return_value=env), \
                 patch.object(cloud_hub, "status",
                              side_effect=cloud_hub.CloudHubError("portal did not answer")):
                result = server._cloud_autoconnect_once()          # 不得抛出
            self.assertEqual(result["status"], "unreachable")
            self.assertIn("portal did not answer", result["error"])

    def test_a_stale_certificate_restarts_so_the_entry_serves_the_enrolled_one(self):
        """登记时签的证书不是进程正在服务的那张，入口会 502 —— 无人值守就必须自己重启。"""
        with tempfile.TemporaryDirectory() as tmp:
            env = self._config(tmp)
            with patch.object(server, "_config_file_path", return_value=env), \
                 patch.object(server, "_cert_reload_required", return_value=True), \
                 patch.object(cloud_hub, "status", return_value={"status": "granted"}), \
                 patch.object(cloud_hub, "connect", return_value=self.GRANT), \
                 patch.object(server, "_cloud_restart_now") as restart_mock:
                result = server._cloud_autoconnect_once()
            self.assertEqual(restart_mock.call_count, 1, "新证书没被服务，却没有重启")
            self.assertEqual(result["status"], "restarting")

    def test_the_poller_steps_aside_while_the_dialog_is_enrolling(self):
        """两边做的是同一件事：各签一张证书、各 POST 一次 /enroll、各写一份 frpc 配置。

        轮询线程的加入让这张竞态第一次真的可达，所以必须有一方让路，而且让路不能假装成功。
        """
        with tempfile.TemporaryDirectory() as tmp:
            env = self._config(tmp)
            with patch.object(server, "_config_file_path", return_value=env), \
                 patch.object(cloud_hub, "status", return_value={"status": "granted"}), \
                 patch.object(cloud_hub, "connect") as connect_mock:
                server._cloud_connect_lock.acquire()          # 假装对话框此刻正在接入
                try:
                    result = server._cloud_autoconnect_once()
                finally:
                    server._cloud_connect_lock.release()
            connect_mock.assert_not_called()
            self.assertEqual(result["status"], "busy")

    def test_the_poller_only_starts_when_there_is_an_application_to_follow(self):
        with tempfile.TemporaryDirectory() as tmp:
            server._cloud_autoconnect_thread = None
            env = Path(tmp) / "mrrc_modern.env"
            env.write_text("MRRC_WEB_PORT=8888\n", encoding="utf-8")
            with patch.object(server, "_config_file_path", return_value=env):
                self.assertFalse(server._cloud_start_autoconnect(9999))
            self.assertIsNone(server._cloud_autoconnect_thread,
                              "没有申请也起了轮询线程")

            env = self._config(tmp, MRRC_CLOUD_LABEL="bg6lh")     # 已接入，没什么可等的
            with patch.object(server, "_config_file_path", return_value=env):
                self.assertFalse(server._cloud_start_autoconnect(9999))
            self.assertIsNone(server._cloud_autoconnect_thread)

    def test_the_state_endpoint_reports_the_poller_so_a_stuck_client_is_visible(self):
        """没有这条，"客户端根本没在问"和"问了没批"在界面上长得一模一样。"""
        server._note_autoconnect("applied", "")
        request = _Request()
        with patch.object(server, "_verify_auth", return_value=True), \
             patch.object(server, "_cloud_start_tunnel"):
            reply = asyncio.run(server.api_cloud_state(request))
        payload = json.loads(bytes(reply.body))
        self.assertIn("autoconnect", payload)
        self.assertEqual(payload["autoconnect"]["status"], "applied")

if __name__ == "__main__":
    unittest.main()
