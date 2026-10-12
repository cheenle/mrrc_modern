#!/usr/bin/env bash
# Build libwsjt_core for the host platform.  Idempotent: skips when the build
# inputs are unchanged and the library already exists.  This is the script the
# box runs after mrrc-update (design §5 step 4).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="$HERE/build"
DIGEST_FILE="$BUILD/.source-digest"
JOBS="${FT8_BUILD_JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 2)}"

digest() {
  python3 - "$HERE" "$BUILD" <<'PY'
import hashlib, sys
from pathlib import Path
here, build = Path(sys.argv[1]), Path(sys.argv[2])
h = hashlib.sha256()
exts = {".f90", ".c", ".h", ".cmake", ".txt"}
for p in sorted(here.rglob("*")):
    if build in p.parents or not p.is_file() or p.suffix not in exts:
        continue
    h.update(str(p.relative_to(here)).encode())
    h.update(p.read_bytes())
print(h.hexdigest())
PY
}

# A build/ dir synced from another machine (or moved tree) carries a
# CMakeCache pinned to the old source path; cmake then refuses to configure.
# The digest check cannot see this (CMakeCache.txt is not a build input), so
# detect it explicitly and start clean.
if [ -f "$BUILD/CMakeCache.txt" ] && \
   ! grep -q "^CMAKE_HOME_DIRECTORY:INTERNAL=$HERE\$" "$BUILD/CMakeCache.txt"; then
  echo "ft8/dsp: stale build dir from another path — rebuilding from scratch"
  rm -rf "$BUILD"
fi

LIB="$BUILD/libwsjt_core.so"
[ "$(uname -s)" = "Darwin" ] && LIB="$BUILD/libwsjt_core.dylib"

if [ -f "$LIB" ] && [ -f "$DIGEST_FILE" ] && [ "$(cat "$DIGEST_FILE")" = "$(digest)" ]; then
  echo "ft8/dsp: build up to date ($LIB)"
  exit 0
fi

FORTRAN="${FT8_FORTRAN_COMPILER:-}"
if [ -z "$FORTRAN" ]; then
  for c in gfortran gfortran-mp-13 gfortran-13 gfortran-12; do
    command -v "$c" >/dev/null && { FORTRAN="$c"; break; }
  done
fi
[ -n "$FORTRAN" ] || { echo "ft8/dsp: no gfortran found (apt install gfortran libfftw3-dev libboost-dev cmake)" >&2; exit 1; }

echo "ft8/dsp: building with $FORTRAN ($JOBS jobs)"
cmake -S "$HERE" -B "$BUILD" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_Fortran_COMPILER="$FORTRAN" >/dev/null
cmake --build "$BUILD" -j "$JOBS"
digest > "$DIGEST_FILE"
echo "ft8/dsp: built $LIB"
