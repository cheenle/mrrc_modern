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
    missing = [r for r in refs if not (STATIC / r).is_file()]
    assert missing == []


def test_module_imports_resolve():
    """Every relative import in the FT8 modules points at a shipped file."""

    missing: list[str] = []
    for js in sorted((STATIC / "ft8").glob("*.js")):
        for target in re.findall(r'from\s+"(\./[^"]+)"', js.read_text()):
            if not (STATIC / "ft8" / target[2:]).is_file():
                missing.append(f"{js.name} -> {target}")
    assert missing == []


def test_dom_ids_used_by_js_exist_in_the_page():
    html = (STATIC / "ft8.html").read_text()
    html_ids = set(re.findall(r'id="([^"]+)"', html))
    used: set[str] = set()
    for js in (STATIC / "ft8").glob("*.js"):
        used |= set(re.findall(r'getElementById\("([^"]+)"\)', js.read_text()))
    assert sorted(used - html_ids) == []
