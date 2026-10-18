"""静态资源的两层缓存契约（2026-10-17，规格 §3.3）。

实例侧要发三件事：分类的 `Cache-Control`、条件请求 304、有扩展名的缺失文件 404。

前景：实例此前只发 `ETag`/`Last-Modified`、**不实现条件 GET** —— 实测带
`If-None-Match` 仍返回 200 与完整 534 KB（`modules/opus_wasm.js`），于是每次重新加载
都等于重新穿一遍隧道；hub 边缘又只压 `text/html`，JS/CSS 原样过隧道。hub 侧已经
按同一套值加了 gzip 与边缘缓存（`../mrrc_hub/deploy/nginx/`）；这里钉的是"实例自己也对"，
于是局域网直连、B2/B3 退化路径、别的反代都拿到同样的语义。

为什么值得测：这三件事的失败模式都不报错 —— 缓存头写错只是"每次都慢"，304 写错
只是"每次都全量传"，回落写错只是"白拉 40 KB 的 HTML 当图片"。
"""
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request

import server


def _request(path: str, **headers: str) -> Request:
    raw = [(k.replace("_", "-").lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
        "query_string": b"", "headers": raw, "scheme": "http",
        "server": ("testserver", 80), "client": ("127.0.0.1", 12345), "root_path": "",
    }
    return Request(scope)


def _serve(path: str, **headers: str):
    """绕过鉴权直接取文件响应（鉴权顺序由最后那个测试单独钉）。"""
    with patch.object(server, "_verify_auth", return_value=True):
        return asyncio.run(server.serve_static(path, _request("/" + path, **headers)))


class CacheControlTests(unittest.TestCase):
    def test_documents_revalidate_every_time(self):
        """HTML/JSON/入口脚本是"部署后必须立刻可见"的那一类。"""
        for path in ("", "index.html", "settings.html", "manifest.json", "sw.js"):
            self.assertEqual(_serve(path).headers["cache-control"], "no-cache", path)

    def test_assets_are_cached_for_an_hour(self):
        for path in ("ft710.css", "ft710_main.js", "modules/opus_wasm.js", "audio/cq.wav"):
            self.assertEqual(_serve(path).headers["cache-control"],
                             "public, max-age=3600", path)

    def test_helper_covers_types_without_a_file_on_disk(self):
        """判定按扩展名，不按文件是否存在 —— 否则新增的字体/图标会静默落到默认分支。"""
        for name in ("x.woff2", "x.woff", "x.svg", "x.wasm", "x.mp3", "x.webp"):
            self.assertEqual(server._cache_control_for(Path(name)), "public, max-age=3600", name)
        for name in ("x.html", "x.htm", "x.json"):
            self.assertEqual(server._cache_control_for(Path(name)), "no-cache", name)

    def test_no_immutable_while_versions_are_bumped_by_hand(self):
        """`?v=` 是手工 bump 的：immutable 会把"漏 bump"的代价从 1 小时放大到一年。"""
        self.assertNotIn("immutable", _serve("ft710.css").headers["cache-control"])


class ConditionalRequestTests(unittest.TestCase):
    def test_matching_etag_answers_304_without_a_body(self):
        first = _serve("ft710.css")
        again = _serve("ft710.css", if_none_match=first.headers["etag"])
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.body, b"")
        self.assertEqual(again.headers["etag"], first.headers["etag"])
        self.assertEqual(again.headers["cache-control"], "public, max-age=3600")

    def test_a_stale_etag_still_gets_the_file(self):
        self.assertEqual(_serve("ft710.css", if_none_match='"deadbeef"').status_code, 200)

    def test_a_weak_etag_still_matches(self):
        """浏览器有时发 `W/"…"`（弱校验）：字节未变时不该再传一次 534 KB。"""
        etag = _serve("ft710.css").headers["etag"]
        self.assertEqual(_serve("ft710.css", if_none_match=f"W/{etag}").status_code, 304)

    def test_if_modified_since_at_or_after_mtime_answers_304(self):
        first = _serve("ft710.css")
        again = _serve("ft710.css", if_modified_since=first.headers["last-modified"])
        self.assertEqual(again.status_code, 304)

    def test_unparseable_if_modified_since_is_ignored(self):
        self.assertEqual(_serve("ft710.css", if_modified_since="not-a-date").status_code, 200)

    def test_if_none_match_wins_over_if_modified_since(self):
        """RFC 7232：带了 If-None-Match 就不再按日期判断，否则新旧校验器会互相打脸。"""
        first = _serve("ft710.css")
        response = _serve("ft710.css", if_none_match='"deadbeef"',
                          if_modified_since=first.headers["last-modified"])
        self.assertEqual(response.status_code, 200)


class MissingAssetTests(unittest.TestCase):
    def test_a_missing_file_with_an_extension_is_404_not_index(self):
        """`/favicon.png` 不存在：旧行为回落 40 KB 的 index.html，浏览器当图片收下。"""
        for path in ("favicon.png", "favicon.ico", "nope.js", "css/gone.css"):
            self.assertEqual(_serve(path).status_code, 404, path)

    def test_extension_less_paths_still_fall_back_to_index(self):
        for path in ("some/spa/route", "settings", "ft8"):
            response = _serve(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.media_type, "text/html", path)
            self.assertEqual(response.headers["cache-control"], "no-cache", path)

    def test_traversal_is_still_refused_before_the_extension_rule(self):
        """I8 的路径穿越判定先于回落规则：穿越返回 404，不去看扩展名。"""
        self.assertEqual(_serve("../server.py").status_code, 404)


class PageRouteTests(unittest.TestCase):
    """`/listen` 与 `/settings` 不经 serve_static —— 它们各自也要发文档类缓存头。

    `/setup` 是例外且必须保持例外：那个页面会显示自动生成的口令横幅，它发 `no-store`。
    """

    def test_page_routes_revalidate(self):
        for route, path in ((server.listen_page, "/listen"), (server.settings_page, "/settings")):
            with self.subTest(path=path):
                response = asyncio.run(route(_request(path)))
                self.assertEqual(response.status_code, 200, path)
                self.assertEqual(response.headers["cache-control"], "no-cache", path)

    def test_the_onboarding_page_still_refuses_to_be_cached_at_all(self):
        with patch.object(server, "_verify_auth", return_value=True):
            with patch.object(server, "_first_window_open", return_value=True):
                response = asyncio.run(server.setup_page(_request("/setup")))
        if response.status_code == 200:
            self.assertEqual(response.headers["cache-control"], "no-store")
        else:                      # 已配置的盒子会 302 去 /settings —— 那也是对的
            self.assertIn(response.status_code, (302, 307))


class AuthOrderTests(unittest.TestCase):
    def test_static_assets_still_need_auth(self):
        """缓存头不得改变鉴权顺序：没带令牌取资源仍然是跳登录页。"""
        response = asyncio.run(server.serve_static("ft710.css", _request("/ft710.css")))
        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/login")


if __name__ == "__main__":
    unittest.main()
