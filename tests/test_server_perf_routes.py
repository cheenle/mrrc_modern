"""`/api/perf` 与 `/Perf` 的守卫与形状。

两件被断言的事：

  * 只听会话与 SDR 会话看不到这台盒子的内部数字（进程、温度、磁盘）——
    listen 页面是给陌生人的，服务端才是唯一把关的地方；
  * 返回的形状稳定：页面靠 `host` / `service` / `verdict` 三段画图，缺一段就是
    一张空白的卡片，而这正是「点进去一片空白」类反馈的来源。
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import host_perf
import server


class FakeRequest:
    def __init__(self, token="good"):
        self.url = SimpleNamespace(path="/api/perf", query="")
        self.method = "GET"
        self.headers = {"authorization": "Bearer " + token} if token else {}
        self.cookies = {}
        self.query_params = {}
        self.client = SimpleNamespace(host="127.0.0.1")


def run(coro):
    return asyncio.run(coro)


class PerfRouteTests(unittest.TestCase):
    def setUp(self):
        self._tokens = set(server._auth_tokens)
        server._auth_tokens.add("good")
        server._listen_tokens.clear()
        server._sdr_tokens.clear()

    def tearDown(self):
        server._auth_tokens.clear()
        server._auth_tokens.update(self._tokens)
        server._listen_tokens.clear()
        server._sdr_tokens.clear()

    def test_a_full_session_gets_a_snapshot(self):
        body = run(server.api_perf(FakeRequest("good"))).body
        data = __import__("json").loads(body)
        for key in ("host", "service", "verdict"):
            with self.subTest(key=key):
                self.assertIn(key, data)
        self.assertIn("event_loop_lag_ms", data["service"])
        self.assertIn("level", data["verdict"])

    def test_a_listen_session_is_refused(self):
        # listen 令牌本身是**有效**会话（登录过），被拒的是它的角色
        server._auth_tokens.add("listen-token")
        server._listen_tokens.add("listen-token")
        response = run(server.api_perf(FakeRequest("listen-token")))
        self.assertEqual(response.status_code, 403)

    def test_an_sdr_session_is_refused(self):
        server._auth_tokens.add("sdr-token")
        server._sdr_tokens.add("sdr-token")
        response = run(server.api_perf(FakeRequest("sdr-token")))
        self.assertEqual(response.status_code, 403)

    def test_the_page_is_served_for_a_full_session(self):
        with mock.patch.object(server, "STATIC_DIR", server.STATIC_DIR):
            response = run(server.perf_page(FakeRequest("good")))
        self.assertEqual(response.status_code, 200)

    def test_an_unauthenticated_request_is_refused(self):
        """中间件之外再挡一次：这一页的数字是盒子的内部状态。"""
        response = run(server.api_perf(FakeRequest("")))
        self.assertEqual(response.status_code, 401)


class ServiceBlockTests(unittest.TestCase):
    def test_the_service_block_survives_a_dead_audio_engine(self):
        """音频没起来时这一页仍然要能看 —— 它正是「为什么卡」的时候被打开的。"""
        class Boom:
            def tx_stats(self):
                raise RuntimeError("no sound card")

        with mock.patch.object(server, "audio", Boom()):
            block = server._perf_service_block()
        self.assertIn("event_loop_lag_ms", block)
        self.assertEqual(block["audio"], {})


class VerdictWiringTests(unittest.TestCase):
    def test_the_verdict_comes_from_host_perf_not_the_route(self):
        """判定只有一处定义（host_perf.verdict），路由不许再造一套阈值。"""
        snapshot = {"available": True, "cpus": 4, "load_per_core": 0.1,
                    "cpu": {"busy_pct": 5.0, "iowait_pct": 0.0},
                    "disk": {"busy_pct": 1.0}, "mem": {"used_pct": 20.0}}
        self.assertEqual(host_perf.verdict(snapshot)["level"], "ok")


if __name__ == "__main__":
    unittest.main()
