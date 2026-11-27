#!/usr/bin/env python3
"""Verify (or refresh) the NVS reference hash against ESP-IDF's own generator.

`tests/test_passport_flash.py::NvsImageTests` pins a SHA-256 of the NVS image
that our encoder builds for a fixed set of provisioning values, and the pin is
only meaningful if that image is byte-identical to what **ESP-IDF itself**
produces.  The test cannot call ESP-IDF (it must run with no ESP toolchain), so
the comparison happens here instead — by hand, once per change to `REFERENCE`.

    ./.venv/bin/python dev_tools/verify_nvs_reference.py           # verify
    ./.venv/bin/python dev_tools/verify_nvs_reference.py --update  # rewrite the pin

What this catches, and why it is worth a separate tool:

* a wrong entry type.  `port` must be written as I32 (`0x14`); an I8 (`0x11`)
  is accepted by the flash and then `nvs_get_i32()` fails on the device, which
  silently falls back to the compiled-in 443 — measured 2026-11-27 with
  tcpdump while the page reported success.
* a wrong page header, entry CRC or span.  None of them throws at flash time;
  NVS just reports "no free pages" and the firmware boots with its defaults,
  i.e. the Wi-Fi that was just provisioned quietly disappears.

Requires the ESP-IDF generator as a package (it is development-only):

    pip install -r requirements-dev.txt     # includes esp-idf-nvs-partition-gen

This script reads `REFERENCE` out of the test module with `ast` rather than
importing it, so running it does not depend on the test suite's import graph.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import io
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
TEST_FILE = REPO / "tests" / "test_passport_flash.py"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import passport_flash as pf                                              # noqa: E402


def read_reference() -> tuple[dict, str]:
    """`(REFERENCE, REFERENCE_SHA)` straight out of the test module."""
    tree = ast.parse(TEST_FILE.read_text(encoding="utf-8"))
    reference: dict = {}
    sha = ""
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name == "REFERENCE":
                reference = ast.literal_eval(node.value)
            elif name == "REFERENCE_SHA":
                sha = ast.literal_eval(node.value)
    if not reference or not sha:
        raise SystemExit(f"could not find REFERENCE / REFERENCE_SHA in {TEST_FILE}")
    return reference, sha


def write_csv(values: dict, path: pathlib.Path) -> None:
    """The generator's CSV, in the same order `build_nvs_image` emits entries.

    Order matters: NVS writes entries sequentially and the pinned hash is over
    the resulting bytes, so a reordered CSV is a different image even though the
    key/value set is the same.
    """
    lines = ["key,type,encoding,value", f"{pf.CFG_NAMESPACE},namespace,,"]
    for key in pf.CFG_KEYS:
        lines.append(f"{key},data,string,{values[key]}")
    try:
        port = int(values[pf.CFG_PORT_KEY])
    except KeyError:
        raise SystemExit(f"REFERENCE has no {pf.CFG_PORT_KEY!r} key")
    except (TypeError, ValueError):
        raise SystemExit(f"REFERENCE[{pf.CFG_PORT_KEY!r}] is not an integer: "
                         f"{values[pf.CFG_PORT_KEY]!r}")
    lines.append(f"{pf.CFG_PORT_KEY},data,i32,{port}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def esp_idf_image(values: dict, workdir: pathlib.Path) -> bytes:
    """Generate the same partition with ESP-IDF's generator and return its bytes."""
    try:
        from esp_idf_nvs_partition_gen import nvs_partition_gen as gen
    except ImportError:
        raise SystemExit(
            "esp-idf-nvs-partition-gen is not installed — this is a development\n"
            "tool: pip install -r requirements-dev.txt")

    csv_path = workdir / "reference.csv"
    bin_path = workdir / "reference.bin"
    write_csv(values, csv_path)

    class Args:                       # what generate() expects from argparse
        input = str(csv_path)
        output = str(bin_path)
        size = hex(pf.NVS_SIZE)
        version = 2
        outdir = str(workdir)

    # The generator narrates everything it does; keep the tool's own output
    # readable and let its text be noise unless it raises.
    with contextlib.redirect_stdout(io.StringIO()):
        gen.generate(Args())
    return bin_path.read_bytes()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true",
                        help="rewrite REFERENCE_SHA in the test module")
    args = parser.parse_args()

    reference, pinned = read_reference()
    ours = pf.build_nvs_image(reference)

    with tempfile.TemporaryDirectory() as tmp:
        official = esp_idf_image(reference, pathlib.Path(tmp))

    official_sha = hashlib.sha256(official).hexdigest()
    ours_sha = hashlib.sha256(ours).hexdigest()

    print(f"values      {sorted(reference)}")
    print(f"size        official={len(official)}  ours={len(ours)}")
    print(f"sha256      official={official_sha}")
    print(f"            ours    ={ours_sha}")

    if official != ours:
        for i, (a, b) in enumerate(zip(official, ours)):
            if a != b:
                print(f"FIRST DIFF  offset 0x{i:x}: official=0x{a:02x} ours=0x{b:02x}")
                break
        print("RESULT      MISMATCH — our encoder drifted from ESP-IDF's generator")
        return 1

    print("RESULT      byte-identical")

    if pinned == official_sha:
        print(f"PIN         up to date ({pinned[:16]}…)")
        return 0

    if not args.update:
        print(f"PIN         STALE — test says {pinned[:16]}…, generator says "
              f"{official_sha[:16]}…; re-run with --update")
        return 1

    text = TEST_FILE.read_text(encoding="utf-8")
    old = f'REFERENCE_SHA = "{pinned}"'
    if old not in text:
        print(f"PIN         could not rewrite: {old!r} not found verbatim")
        return 1
    TEST_FILE.write_text(
        text.replace(old, f'REFERENCE_SHA = "{official_sha}"', 1), encoding="utf-8")
    print(f"PIN         updated to {official_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
