"""The FT8 page and every module it loads must ship together."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STATIC = ROOT / "static"


def test_ft8_page_exists():
    assert (STATIC / "ft8.html").is_file()


def test_page_references_resolve():
    """Local refs resolve; they are relative by the platform's path-proxy rule."""

    html = (STATIC / "ft8.html").read_text()
    refs = [r for r in re.findall(r'(?:src|href)="([^"]+)"', html)
            if not r.startswith(("http", "#", "/"))]
    assert refs, "page references no assets?"
    missing = [r for r in refs if not (STATIC / r.split("?", 1)[0]).is_file()]
    assert missing == []


def test_module_imports_resolve():
    """Every relative import in the FT8 modules points at a shipped file."""

    missing: list[str] = []
    for js in sorted((STATIC / "ft8").glob("*.js")):
        for target in re.findall(r'from\s+"(\./[^"]+)"', js.read_text()):
            if not (STATIC / "ft8" / target[2:]).is_file():
                missing.append(f"{js.name} -> {target}")
    assert missing == []


DYNAMIC_IDS = {
    # Rendered by the settings drawer into #drawer-content (settings.js).
    "st-call", "st-grid", "st-mode", "st-audio", "st-save", "st-msg", "st-always",
}


def test_dom_ids_used_by_js_exist_in_the_page():
    html = (STATIC / "ft8.html").read_text()
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    used: set[str] = set()
    for js in (STATIC / "ft8").glob("*.js"):
        used |= set(re.findall(r'getElementById\("([^"]+)"\)', js.read_text()))
    assert sorted(used - html_ids - DYNAMIC_IDS) == []


def test_ft8_page_declares_the_engine_stats_element() -> None:
    """状态栏必须有承载升级率的元素，否则渲染代码无处可写。"""

    html = (ROOT / "static" / "ft8.html").read_text(encoding="utf-8")
    assert 'id="engine-stats"' in html


def test_ft8_api_client_exposes_engine_stats() -> None:
    """页面通过 `api.engineStats()` 取数 —— 方法名是跨文件的契约。"""

    js = (ROOT / "static" / "ft8" / "api.js").read_text(encoding="utf-8")
    assert "engineStats:" in js
    assert '"/engine/stats"' in js


def test_ft8_main_polls_the_engine_stats_endpoint() -> None:
    main = (ROOT / "static" / "ft8" / "main.js").read_text(encoding="utf-8")
    assert "engineStats()" in main
