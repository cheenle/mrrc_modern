"""应用侧全链路：HTTP 门户 → 协议函数 → 隧道配置 → 真 SSH 会话。

只有两样东西是假的，而且都是有意的：

* **frpc 本身**（这里没有隧道可跑）：`set_help_proxy` 写的是真配置、POST 的是一个假
  admin API，所以"加代理 → reload → 删代理 → reload"这条路径是整条跑过的；
* **门户**：一个三端点的假 HTTP 服务，但它说的是真协议（字段名、语义、stop 回读）。

其余全是真的：真 paramiko 客户端、真口令认证、真 shell 通道、真的"关闭后端口不再接受连接"。
这一条把真机那一步（需要先部署门户）之外的所有环节钉住了。
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cloud_hub
import help_session


class _FakePortal(BaseHTTPRequestHandler):
    """三个端点，按设计文档的字段表说话。"""

    calls: list = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        # 门户说的是 form-encoded（cloud_hub._post 就是这么发的，真实的 portal 也这么收），
        # 第一版假门户按 JSON 解析，于是每次请求都在服务端 JSONDecodeError、连接被关。
        raw = self.rfile.read(length).decode("utf-8")
        body = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}
        type(self).calls.append((self.path, body))
        if self.path == "/help/request":
            reply = {"port": 18877, "expires_at": 0}
        elif self.path == "/help/heartbeat":
            reply = {"stop": False, "reason": ""}
        else:
            reply = {"ok": True}
        payload = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):    # noqa: A002 — 基类就是这么命名的
        pass


class _FakeFrpcAdmin(BaseHTTPRequestHandler):
    """只认 POST /api/reload，并把看见的 Basic 认证记下来。"""

    seen: list = []

    def do_GET(self):
        """frpc's admin API wants GET; POST gets 405 (measured on 0.71.0, 2026-10-12)."""
        type(self).seen.append((self.path, self.headers.get("Authorization", "")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):
        self.send_response(405)
        self.end_headers()

    def log_message(self, format, *args):    # noqa: A002 — 基类就是这么命名的
        pass


def _serve(handler_cls):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        _FakePortal.calls = []
        _FakeFrpcAdmin.seen = []
        self.portal_srv = _serve(_FakePortal)
        self.portal = f"http://127.0.0.1:{self.portal_srv.server_address[1]}"
        self.admin_srv = _serve(_FakeFrpcAdmin)
        self.admin_port = self.admin_srv.server_address[1]
        self.tmp = tempfile.TemporaryDirectory()
        self.conf = Path(self.tmp.name) / "frpc-bg7ifp.toml"
        cloud_hub._write_frpc_config(self.conf, cloud_hub.frpc_config_text(
            "bg7ifp", "hub-token", 8888, 18802, log_file="",
            admin_port=self.admin_port, admin_password="admin-pw"))
        self.callsign, self.token = "BG7IFP", "req-token"

    def tearDown(self):
        for srv in (self.portal_srv, self.admin_srv):
            srv.shutdown()
            srv.server_close()          # shutdown() 只停循环，监听套接字要自己关
        self.tmp.cleanup()

    def _close_portal(self) -> None:
        """门户那边的收尾：返回值没人要，这里显式丢掉（签名说的是 -> None）。"""
        cloud_hub.help_close(self.portal, self.callsign, self.token)

    def _session(self):
        return help_session.build_session(
            portal_opener=lambda minutes: int(cloud_hub.help_request(
                self.portal, self.callsign, self.token, minutes,
                username="help-ab12")["port"]),
            tunnel=lambda lp, rp, add: cloud_hub.set_help_proxy(
                self.conf, help_local_port=lp, help_remote_port=rp) if add
            else cloud_hub.set_help_proxy(self.conf, remove=True),
            portal_closer=self._close_portal,
            portal_beat=lambda port, conns: cloud_hub.help_heartbeat(
                self.portal, self.callsign, self.token, port, conns))

    def test_the_whole_app_side_of_a_help_window(self):
        import socket
        session = self._session()
        status = session.enable(minutes=30, username="zhang-san", password="pw-1234567890")
        try:
            # ① 门户收到了申请，字段就是设计里那几个，没有口令
            routes = [c[0] for c in _FakePortal.calls]
            self.assertIn("/help/request", routes)
            request_body = [c[1] for c in _FakePortal.calls if c[0] == "/help/request"][0]
            self.assertEqual(request_body["callsign"], "BG7IFP")
            self.assertEqual(request_body["username"], "help-ab12")
            self.assertNotIn("password", request_body)

            # ② 隧道配置里多了一个按实例命名的代理，主代理还在；reload 真的被调了
            text = self.conf.read_text(encoding="ascii")
            self.assertIn('name = "mrrc-help-bg7ifp"', text)
            self.assertIn('name = "bg7ifp"', text)
            self.assertTrue(_FakeFrpcAdmin.seen, "没有向 frpc 管理端发过 reload")
            self.assertTrue(_FakeFrpcAdmin.seen[0][1].startswith("Basic "),
                            "reload 没带 Basic 认证")

            # ③ 真客户端用真口令连进来，敲一条命令
            import paramiko
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect("127.0.0.1", port=session.local_port,
                           username=status["username"], password="pw-1234567890",
                           allow_agent=False, look_for_keys=False, timeout=5)
            try:
                transport = client.get_transport()
                assert transport is not None, "连接成功却没有 transport"
                chan = transport.open_session()
                chan.exec_command(help_session.probe_command())
                self.assertIn(help_session.PROBE_MARK, chan.makefile().read().decode())
            finally:
                client.close()

            # ④ 错口令连不进来
            with self.assertRaises(paramiko.AuthenticationException):
                bad = paramiko.SSHClient()
                bad.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                bad.connect("127.0.0.1", port=session.local_port,
                            username=status["username"], password="nope-nope-nope",
                            allow_agent=False, look_for_keys=False, timeout=5)
        finally:
            port_before = session.local_port
            session.close("user")

        # ⑤ 关闭之后：代理被摘掉、门户收到 close、端口不再接受连接
        text = self.conf.read_text(encoding="ascii")
        self.assertNotIn("mrrc-help-", text)
        self.assertIn('name = "bg7ifp"', text, "主代理被误删")
        self.assertIn("/help/close", [c[0] for c in _FakePortal.calls])
        self.assertEqual(session.password_for_display(), "")
        probe = socket.socket()
        try:
            self.assertRaises(OSError, probe.connect, ("127.0.0.1", port_before))
        finally:
            probe.close()


if __name__ == "__main__":
    unittest.main()
