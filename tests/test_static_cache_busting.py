"""A changed static file only reaches a user if its cache key moves.

sw.js is cache-first for everything and precaches an explicit list. It re-installs only when
sw.js itself changes byte-for-byte, so a module shipped under an unchanged `?v=` keeps being
served out of the previous cache — the correction sits on disk while the browser runs the old
code, and no server-side check can see it.

Measured 2026-10-03: settings_manager.js (the API base computation) and cloud_hub.js (the
dialog's unreadable error) were both corrected in one sitting. Either one shipped without its
`?v=` bump would have left the reported fault exactly as reported.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "static" / "index.html"
SW = ROOT / "static" / "sw.js"

# Cached by the worker but not referenced by index.html, so there is nothing to compare.
SKIP = {"/", "/index.html", "/manifest.json"}


def _page_references() -> dict:
    """path (without query) -> full reference, for every script/link the page loads."""
    html = INDEX.read_text(encoding="utf-8")
    out = {}
    for match in re.finditer(r'(?:src|href)="([^"]+)"', html):
        ref = match.group(1)
        if ref.startswith(("http:", "https:", "//", "#", "data:")):
            continue
        path, _, query = ref.partition("?")
        if path.startswith("/"):
            path = path[1:]
        out[path] = query
    return out


def _precached() -> dict:
    """path (without query) -> query, for every entry of sw.js's ASSETS list."""
    source = SW.read_text(encoding="utf-8")
    body = source[source.index("const ASSETS = ["):source.index("];", source.index("const ASSETS = ["))]
    out = {}
    for match in re.finditer(r"'([^']+)'", body):
        ref = match.group(1)
        if ref in SKIP:
            continue
        path, _, query = ref.partition("?")
        if path.startswith("/"):
            path = path[1:]
        out[path] = query
    return out


class StaticCacheKeyTests(unittest.TestCase):
    def test_the_lists_do_not_silently_become_empty(self):
        page, cached = _page_references(), _precached()
        for name in ("modules/settings_manager.js", "modules/cloud_hub.js"):
            self.assertIn(name, cached, f"sw.js 不再预缓存 {name} —— 下面的断言会空跑")
            self.assertIn(name, page, f"index.html 不再引用 {name}")

    def test_a_page_reference_and_its_precached_entry_carry_the_same_version(self):
        page, cached = _page_references(), _precached()
        mismatch = {
            name: (page[name], cached[name])
            for name in cached
            if name in page and page[name] != cached[name]
        }
        self.assertEqual(
            mismatch, {},
            "index.html 与 sw.js 的版本号不一致（格式：文件 -> (页面, 预缓存)）："
            "sw.js 是全量 cache-first，键没变就永远发不出新文件；"
            "在一起改代码时必须一起升 ?v= 与 CACHE")

    def test_the_cache_name_carries_a_version(self):
        source = SW.read_text(encoding="utf-8")
        match = re.search(r"const CACHE = '([^']+)'", source)
        self.assertIsNotNone(match, "sw.js 里找不到 CACHE 常量")
        assert match is not None
        self.assertRegex(match.group(1), r".+-v\d+$",
                         f"CACHE={match.group(1)!r} 没有版本后缀，升级时无法淘汰旧缓存")


if __name__ == "__main__":
    unittest.main()
