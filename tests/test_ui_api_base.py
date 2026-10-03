"""`FT710Settings.url()` must address the API at the root, whatever route the page is on.

Reported 2026-10-03: clicking 刷新状态 in the cloud panel raised
`Uncaught (in promise) SyntaxError`. The server side was fine — the same endpoint answers
200 application/json to a logged-in session (measured on the machine). The response the
browser got was the whole index.html (200 text/html, 36483 bytes, exactly the size of
index.html), so JSON.parse was always going to fail.

That happens whenever the page is not sitting on a directory: settings_manager.js computed
the API base from the current path's last segment and a **bare route was treated as a
directory**, so at /login every endpoint became /login/api/... — and the SPA fallback
answers index.html for any path. The comment above the function says a bare route "resolves
to the same base" as `/`; the code did the opposite. Worse, the login page's `next=`
redirect can park a browser on /login/api/cloud/state, after which every call nests one
level deeper and the app never recovers.

These tests execute the real `basePath()` out of the shipped file under node rather than a
transcription of it — a transcription would have agreed with whatever the author believed.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_JS = ROOT / "static" / "modules" / "settings_manager.js"
NODE = shutil.which("node")

# pathname -> the API base the page must use
EXPECTED = {
    "/": "",
    "/index.html": "",
    "/login": "",
    # The login page's next= redirect can leave a browser here; it must not spiral.
    "/login/api/cloud/state": "",
    "/whatever": "",
    # A reverse proxy serves the app under a prefix — that is the only reason for a base,
    # and it always shows up as a directory in the address bar.
    "/mrrc/": "/mrrc",
    "/mrrc/index.html": "/mrrc",
}


def _extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for i in range(brace, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError("unbalanced braces while extracting " + name)


def _base_path(pathname: str) -> str:
    assert NODE, "node must be on PATH to execute the real function"
    function = _extract_function(SETTINGS_JS.read_text(encoding="utf-8"), "basePath")
    script = ("var window = {location: {pathname: %s}};\n%s\n"
              "process.stdout.write(basePath());" % (json.dumps(pathname), function))
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        raise AssertionError(f"node failed for {pathname!r}: {done.stderr}")
    return done.stdout


class ApiBaseTests(unittest.TestCase):
    def test_the_function_is_still_there_to_test(self):
        """src 一旦被改名/内联，下面的用例会全部静默跳过——这条先把它钉住。"""
        source = SETTINGS_JS.read_text(encoding="utf-8")
        self.assertIn("function basePath(", source)
        self.assertIn("FT710Settings", source)

    @unittest.skipUnless(NODE, "node 不在 PATH 上，跳过执行真实函数的用例")
    def test_every_route_addresses_the_api_at_the_root(self):
        wrong = {}
        for pathname, want in EXPECTED.items():
            got = _base_path(pathname)
            if got != want:
                wrong[pathname] = (got, want)
        self.assertEqual(
            wrong, {},
            "API 基址算错了；例如页在 /login 时 url('/api/cloud/state') 会变成 "
            "/login/api/cloud/state，而服务端对任何路径都用 index.html 作 SPA 回退，"
            "于是 JSON.parse 拿到 HTML —— 就是那句 Uncaught (in promise) SyntaxError")

    @unittest.skipUnless(NODE, "node 不在 PATH 上")
    def test_the_api_url_never_carries_the_page_route(self):
        """更本质的说法：API 地址里不该出现页面自己的路由段。"""
        for pathname in EXPECTED:
            for route in ("login", "whatever"):
                if route not in pathname:
                    continue
                url = _base_path(pathname) + "/api/cloud/state"
                self.assertFalse(url.startswith("/" + route + "/"),
                                 f"页在 {pathname} 时 API 被打到 {url}")


if __name__ == "__main__":
    unittest.main()
