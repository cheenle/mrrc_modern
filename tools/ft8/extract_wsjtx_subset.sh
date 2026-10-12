#!/usr/bin/env bash
# Extract the WSJT-X FT8 source subset used by ft8/dsp into ft8/dsp/vendor/.
#
# The file list is NOT duplicated here.  It is derived from the CMake source
# lists in ft8/dsp/cmake/ (the files the build actually compiles) plus the
# recursive Fortran `include` closure of those sources and of the patched
# copies in ft8/dsp/patched/.
#
# usage: extract_wsjtx_subset.sh <wsjtx-checkout-root> [out-dir]
set -euo pipefail

WSJTX="${1:?usage: extract_wsjtx_subset.sh <wsjtx-checkout-root> [out-dir]}"
OUT="${2:-ft8/dsp/vendor}"
LIB="$WSJTX/lib"
[ -d "$LIB" ] || { echo "no lib/ under $WSJTX" >&2; exit 1; }

python3 - "$WSJTX" "$OUT" <<'PY'
import re
import shutil
import sys
from pathlib import Path

wsjtx, out = Path(sys.argv[1]), Path(sys.argv[2])
lib = wsjtx / "lib"
cmake_files = [Path("ft8/dsp/cmake/standard-ft8.cmake"),
               Path("ft8/dsp/cmake/improved-ft8.cmake")]
patched = Path("ft8/dsp/patched")

# 1. Sources referenced by the build's CMake lists (the single source of truth).
refs = set()
for cm in cmake_files:
    text = cm.read_text()
    refs.update(m.group(1) for m in re.finditer(r"\$\{WSJTX_LIB\}/([^\s)]+)", text))

# 2. Recursive include closure, resolved against the compiler's -I paths
#    (${WSJTX_LIB}, ${WSJTX_LIB}/ft8, ${WSJTX_LIB}/ft8var are on the include path).
include_re = re.compile(r"^\s*include\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
search_dirs = [lib, lib / "ft8", lib / "ft8var"]
seen: set[str] = set()
# Closure starts: the CMake-referenced sources AND the patched copies (they
# compile too, so their includes must be vendored as well).
queue: list[tuple[str, object]] = [(str(lib / rel), rel) for rel in sorted(refs)]
queue += [(str(p), str(p)) for p in sorted(patched.glob("*.f90"))]
while queue:
    src_path, label = queue.pop()
    src = Path(src_path)
    if not src.is_file():
        raise SystemExit(f"referenced source missing: {src}")
    for line in src.read_text(errors="replace").splitlines():
        m = include_re.match(line)
        if not m:
            continue
        name = m.group(1)
        found = False
        for d in search_dirs:
            candidate = d / name
            if candidate.is_file():
                rel_inc = str(candidate.relative_to(lib))
                if rel_inc not in seen:
                    seen.add(rel_inc)
                    queue.append((str(lib / rel_inc), rel_inc))
                found = True
                break
        if not found:
            raise SystemExit(f"include {name!r} of {label} not found under {lib}")

for rel in sorted(refs | seen):
    src, dst = lib / rel, out / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"vendor/{rel}")
PY

# 3. Digest over the extracted tree (sorted, path-relative -> stable).
python3 - "$OUT" <<'PY'
import hashlib
import sys
from pathlib import Path

out = Path(sys.argv[1])
rows = []
for p in sorted(out.rglob("*")):
    if p.is_file():
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        rows.append(f"{digest}  {p.relative_to(out)}")
(out.parent / "vendor.sha256").write_text("\n".join(rows) + "\n")
print(f"vendor.sha256: {len(rows)} files")
PY
