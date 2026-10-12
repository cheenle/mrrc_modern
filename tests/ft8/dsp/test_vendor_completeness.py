"""The vendored WSJT-X subset must cover everything the DSP build compiles."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DSP = ROOT / "ft8" / "dsp"
VENDOR = DSP / "vendor"

INCLUDE_RE = re.compile(r"^\s*include\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)


def _cmake_refs() -> set[str]:
    refs: set[str] = set()
    for cm in sorted((DSP / "cmake").glob("*.cmake")):
        refs.update(m.group(1) for m in re.finditer(r"\$\{WSJTX_LIB\}/([^\s)]+)", cm.read_text()))
    return refs


def test_cmake_source_lists_exist_and_are_not_empty() -> None:
    """Guard against a vacuous pass when the CMake lists are missing."""

    refs = _cmake_refs()
    assert refs, "no ${WSJTX_LIB}/ references found under ft8/dsp/cmake/"


def test_every_cmake_referenced_source_is_vendored() -> None:
    missing = sorted(r for r in _cmake_refs() if not (VENDOR / r).is_file())
    assert missing == []


def test_include_closure_is_vendored() -> None:
    search_dirs = [VENDOR, VENDOR / "ft8", VENDOR / "ft8var"]
    queue = [(VENDOR / rel, rel) for rel in sorted(_cmake_refs())]
    queue += [(p, str(p)) for p in sorted((DSP / "patched").glob("*.f90"))]
    seen: set[str] = set()
    while queue:
        src, label = queue.pop()
        assert src.is_file(), f"missing source {label}"
        for line in src.read_text(errors="replace").splitlines():
            m = INCLUDE_RE.match(line)
            if not m:
                continue
            name = m.group(1)
            hit = next((d / name for d in search_dirs if (d / name).is_file()), None)
            assert hit is not None, f"include {name!r} of {label} not vendored"
            rel_inc = str(hit.relative_to(VENDOR))
            if rel_inc not in seen:
                seen.add(rel_inc)
                queue.append((VENDOR / rel_inc, rel_inc))


def test_vendor_digest_matches_tree() -> None:
    rows = []
    for p in sorted(VENDOR.rglob("*")):
        if p.is_file():
            rows.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(VENDOR)}")
    assert (DSP / "vendor.sha256").read_text() == "\n".join(rows) + "\n"
