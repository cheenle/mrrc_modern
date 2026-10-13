#!/usr/bin/env python3
"""Collect the FT8 DSP library's dependencies into one directory for bundling.

The macOS dylib is built by MacPorts' gfortran, so its load commands point at
``/opt/local/lib/...`` — FFTW, libgfortran, libgomp, libquadmath, all of which
exist only on this build host.  A user's Mac has none of them, and the app is
supposed to load the library through ctypes, so every one of those references
has to become ``@loader_path/<name>`` with the file itself sitting next to
``libwsjt_core.dylib``.

This used to be a shell one-liner's worth of work in the release checklists and
it is not: the closure is transitive (libgfortran pulls libquadmath, libgomp
pulls libgcc_s), and ``install_name_tool`` has to rewrite both the reference and
the referenced library's own ``-id`` or the members cannot find each other.

The interesting logic — parsing ``otool -L`` and deciding what counts as
"system" — is separated from the subprocess calls so it can be unit tested
without a Mac: see tests/test_ft8_macos_bundle.py.

usage: ft8_bundle.py --library <built dylib> --dest <bundle dir>
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path


#: Prefixes that ship with macOS.  Everything else has to travel with the app.
SYSTEM_PREFIXES = ("/usr/lib/", "/System/", "/Library/Apple/")

#: `otool -L` line: "\t<path> (compatibility version ...)".
_LOAD_LINE = re.compile(r"^\s+(\S+)\s+\(compatibility version")


def parse_otool(text: str) -> list[str]:
    """Return the install names a ``otool -L`` output lists.

    The path may contain spaces (a build tree under "/Users/…/My Project" does),
    so the line is matched as everything up to the " (compatibility version"
    marker rather than by splitting on whitespace.
    """
    names: list[str] = []
    for line in text.splitlines():
        match = _LOAD_LINE.match(line)
        if not match:
            continue
        names.append(match.group(1))
    return names


def is_system_path(path: str) -> bool:
    """Whether a load command points at a library macOS itself provides."""
    return path.startswith(SYSTEM_PREFIXES) or path.startswith("@")


def collect_closure(library: Path, run) -> dict[str, Path]:
    """Map every non-system dependency (transitively) to its file on disk."""
    found: dict[str, Path] = {}
    queue = [library]
    seen_files = {library.resolve()}
    while queue:
        current = queue.pop()
        for name in parse_otool(run(["otool", "-L", str(current)])):
            if is_system_path(name):
                continue
            if name in found:
                continue
            source = Path(name)
            if not source.is_file():
                raise SystemExit(f"{current.name} references a missing library: {name}")
            found[name] = source
            if source.resolve() not in seen_files:
                seen_files.add(source.resolve())
                queue.append(source)
    return found


def rewrite(dest: Path, library: Path, dependencies: dict[str, Path], run) -> None:
    """Copy library + closure into ``dest`` and point every load command at it."""
    dest.mkdir(parents=True, exist_ok=True)
    for name, source in dependencies.items():
        target = dest / Path(name).name
        if not target.exists():
            shutil.copy2(source, target)
    bundled_library = dest / library.name
    if bundled_library != library:
        shutil.copy2(library, bundled_library)

    # -id first: a library that names itself /opt/local/... is found by its
    # dependents through that name, so changing only the references leaves the
    # graph half-rewritten and dlopen fails on the second hop.
    for path in [bundled_library, *(dest / Path(n).name for n in dependencies)]:
        run(["install_name_tool", "-id", f"@loader_path/{path.name}", str(path)])
    for name in dependencies:
        for path in [bundled_library, *(dest / Path(n).name for n in dependencies)]:
            run(["install_name_tool", "-change", name,
                 f"@loader_path/{Path(name).name}", str(path)])


def check(dest: Path, run) -> list[str]:
    """Return every load command that still points outside the bundle."""
    bad: list[str] = []
    for path in sorted(dest.iterdir()):
        for name in parse_otool(run(["otool", "-L", str(path)])):
            if not is_system_path(name) and not name.startswith("@loader_path/"):
                bad.append(f"{path.name}: {name}")
    return bad


def _run(command: list[str]) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"{' '.join(command)} failed: {completed.stderr.strip()}")
    return completed.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    args = parser.parse_args(argv)

    if not args.library.is_file():
        raise SystemExit(f"no such library: {args.library}")
    dependencies = collect_closure(args.library, _run)
    rewrite(args.dest, args.library, dependencies, _run)
    bad = check(args.dest, _run)
    if bad:
        # Hard failure, not a warning: a bundle that ships this is an app whose
        # FT8 mode dies with "image not found" on a machine that has no
        # /opt/local — and nothing else in the build would have noticed.
        print("FT8 bundle still references libraries outside the bundle:",
              file=sys.stderr)
        for line in bad:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"ft8 bundle: {args.library.name} + {len(dependencies)} dependencies "
          f"in {args.dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
