#!/usr/bin/env bash
# 从钉住的 ft8_lib 检出复制快路径所需的最小 C 子集到 ft8/dsp/vendor-ft8lib/，
# 并生成 SHA-256 清单。用法: tools/ft8/vendor_ft8lib.sh <上游检出目录>
set -euo pipefail
UPSTREAM="${1:?usage: vendor_ft8lib.sh <ft8_lib checkout>}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="$REPO_ROOT/ft8/dsp/vendor-ft8lib"

mkdir -p "$DEST/ft8" "$DEST/common" "$DEST/fft"
cp "$UPSTREAM"/ft8/{constants,crc,decode,encode,ldpc,message,text}.c "$DEST/ft8/"
cp "$UPSTREAM"/ft8/{constants,crc,decode,encode,ldpc,message,text}.h "$DEST/ft8/"
cp "$UPSTREAM"/ft8/debug.h "$DEST/ft8/"
# common/ 只有 monitor.c（common.h 是纯头文件，上游没有 common.c）。
cp "$UPSTREAM"/common/monitor.c "$DEST/common/"
cp "$UPSTREAM"/common/monitor.h "$DEST/common/"
cp "$UPSTREAM"/common/common.h "$DEST/common/"
cp "$UPSTREAM"/fft/{kiss_fft,kiss_fftr}.c "$DEST/fft/"
cp "$UPSTREAM"/fft/{kiss_fft,kiss_fftr}.h "$DEST/fft/"
cp "$UPSTREAM"/fft/_kiss_fft_guts.h "$DEST/fft/"

cd "$DEST"
if command -v sha256sum >/dev/null 2>&1; then
  HASH=(sha256sum)
else
  HASH=(shasum -a 256)
fi
find . -type f \( -name '*.c' -o -name '*.h' \) | sed 's|^\./||' | sort \
  | xargs "${HASH[@]}" > "$DEST.sha256"
echo "wrote $DEST and $DEST.sha256"
