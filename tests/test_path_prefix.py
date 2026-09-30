"""Raised-path contract: nothing local may escape the path prefix.

An instance can be served under a sub-path (https://edge/mrrc_modern/<name>/...) so
that an edge can front it on a standard port with a certificate it already has.
That only works while every local URL is built relative to the prefix; one
absolute `/WSradio` or `/api/...` silently reaches the edge instead of the
instance, and the failure looks like "the console is blank", not like a URL bug.

Hardware-free: source assertions over the frontend, which is how this repo guards
frontend contracts.
"""
import re
import unittest
from pathlib import Path

INDEX = Path("static/index.html").read_text(encoding="utf-8")
MAIN = Path("static/ft710_main.js").read_text(encoding="utf-8")
SETTINGS = Path("static/modules/settings_manager.js").read_text(encoding="utf-8")
SW = Path("static/sw.js").read_text(encoding="utf-8")
ATR = Path("static/modules/atr1000.js").read_text(encoding="utf-8")

TAG_RE = re.compile(r"<(script|link)\b[^>]*>")


class AssetTagTests(unittest.TestCase):
    def test_no_local_asset_is_referenced_by_absolute_path(self):
        for tag in TAG_RE.findall(INDEX):
            for attr, val in re.findall(r'(src|href)="([^"]*)"', tag):
                if val.startswith(("http://", "https://", "//", "data:", "#", "")):
                    continue
                self.assertFalse(val.startswith("/"), f"absolute asset in {tag!r}")


class InlineScriptTests(unittest.TestCase):
    def test_inline_api_calls_go_through_the_base_path(self):
        self.assertNotIn('fetch("/api/', INDEX)
        self.assertIn("function apiUrl(path)", INDEX)
        self.assertIn("FT710Settings.url(path)", INDEX)

    def test_no_test_asserts_an_absolute_asset_for_the_document(self):
        """index.html/listen.html are relative now; sw.js precache stays absolute."""
        for module in (Path("tests/test_server_ws_protocol.py"),
                       Path("tests/test_ws_token_transport.py")):
            for line in module.read_text(encoding="utf-8").splitlines():
                if "assertIn(" not in line or "sw" in line.lower():
                    continue
                m = re.search(r"assertIn\(\s*[\"\'](/[\w./-]+\.(?:js|css|png|json)[^\"\']*)", line)
                self.assertIsNone(m, f"{module.name}: absolute document asset pinned: {line.strip()}")


class BuilderTests(unittest.TestCase):
    def test_url_builders_include_the_base_path(self):
        for fn in ("wsUrlWithAuth", "staticUrlWithAuth"):
            body = MAIN.split(f"function {fn}(path)", 1)[1].split("}", 1)[0]
            self.assertIn("basePath() + path", body, fn)

    def test_base_path_helper_degrades_to_root(self):
        self.assertIn("window.FT710Settings && FT710Settings.basePath", MAIN)

    def test_websocket_call_sites_use_the_builders(self):
        self.assertEqual(len(re.findall(r'wsUrlWithAuth\("/WS', MAIN)), 4)
        self.assertIn("FT710Settings.basePath ?", ATR)          # /WSatr1000 is built inline


class SettingsExportTests(unittest.TestCase):
    def test_base_path_is_exported_with_a_documented_derivation(self):
        self.assertIn("basePath: basePath", SETTINGS)
        self.assertIn("url: url", SETTINGS)
        self.assertIn("function basePath()", SETTINGS)


class ServiceWorkerTests(unittest.TestCase):
    def test_sw_precache_entries_stay_absolute(self):
        """A sweep that relativises everything would break the worker: its asset
        list is not resolved against a document."""
        self.assertIn("'/manifest.json'", SW)
        self.assertIn("'/',", SW)


if __name__ == "__main__":
    unittest.main()
