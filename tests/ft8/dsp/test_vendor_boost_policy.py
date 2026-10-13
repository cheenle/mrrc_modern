"""The vendored Boost header closure must be complete and unmodified.

WSJT-X's crc13.cpp / crc14.cpp include <boost/crc.hpp> and <boost/config.hpp>;
Debian ships those only inside libboost1.74-dev (138 MB of headers, which both
appliance images would carry and every macOS/Windows build machine would have to
install).  The closure is vendored under ft8/dsp/vendor-boost/ instead, and this
module is what keeps "vendored" from decaying into "half of it, on one machine".

Three failure modes, each of which produced a green tree before this test:

1. A file dropped from vendor-boost — the build then fails on the *device*
   during a 65-minute image build, or on the Windows VM after a 40-minute tarball
   round trip.  Caught here by walking the include graph.
2. A vendored file edited by hand — the tree silently diverges from upstream and
   the next person's re-extraction silently reverts the fix.  Caught by the
   SHA-256 manifest.
3. `find_package(Boost)` creeping back into CMakeLists — the dependency returns
   to all four platforms with nothing else failing.  Caught by the wiring check.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
VENDOR = ROOT / "ft8" / "dsp" / "vendor-boost"
MANIFEST = ROOT / "ft8" / "dsp" / "vendor-boost.sha256"
CMAKE = ROOT / "ft8" / "dsp" / "CMakeLists.txt"

#: The two headers crc13.cpp / crc14.cpp actually include — the roots the
#: extraction script starts from.
ROOTS = ("boost/crc.hpp", "boost/config.hpp")

_INCLUDE = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', re.M)


def _manifest() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        entries[name.strip()] = digest
    return entries


def test_manifest_covers_every_vendored_file():
    listed = _manifest()
    on_disk = {
        str(p.relative_to(VENDOR))
        for p in VENDOR.rglob("*")
        if p.is_file()
    }
    assert set(listed) == on_disk, (
        "vendor-boost.sha256 and the tree disagree — re-run "
        "tools/ft8/vendor_boost_headers.sh and commit the result"
    )


def test_every_vendored_file_matches_its_digest():
    for name, digest in _manifest().items():
        path = VENDOR / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == digest, (
            f"{name} was edited in place; the vendored tree must stay byte-identical "
            "to upstream (or be re-extracted and its manifest regenerated)"
        )


def test_the_boost_license_travels_with_the_headers():
    """Boost's licence requires the notice in source distributions."""
    license_file = VENDOR / "LICENSE_1_0.txt"
    assert license_file.is_file()
    assert "Boost Software License" in license_file.read_text(encoding="utf-8")


def test_include_closure_is_complete():
    """Every `boost/...` include inside the tree must resolve inside the tree.

    This is the completeness property that matters: the compiler follows the
    same graph, and a missing node is a build failure on whichever platform
    happens to configure next.
    """
    missing: list[str] = []
    for path in VENDOR.rglob("*"):
        if not path.is_file() or path.suffix not in (".hpp", ".h"):
            continue
        for target in _INCLUDE.findall(path.read_text(errors="replace")):
            if not target.startswith("boost/"):
                continue
            if not (VENDOR / target).is_file():
                missing.append(f"{path.relative_to(VENDOR)} -> {target}")
    assert not missing, "incomplete vendored Boost closure:\n" + "\n".join(missing)


@pytest.mark.parametrize("root", ROOTS)
def test_the_two_roots_exist(root: str):
    assert (VENDOR / root).is_file()


def test_cmakelists_no_longer_needs_a_boost_installation():
    text = CMAKE.read_text(encoding="utf-8")
    assert "find_package(Boost" not in text, (
        "find_package(Boost) is back: that reinstates a 138 MB header package on "
        "both image lines and on both desktop build machines"
    )
    assert "Boost_INCLUDE_DIRS" not in text
    assert "vendor-boost" in text and "FT8_BOOST_INCLUDE" in text
