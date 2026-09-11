#!/usr/bin/env bash
# Verify the built rpi64 image: xz integrity, size bounds, debugfs spot-checks.
# The .img is an MBR disk image (p1 = boot FAT32, p2 = root ext4) — partition 2
# is read via debugfs' "?offset=" file syntax.
# Requires: e2fsprogs (brew install e2fsprogs — debugfs at e2fsprogs/sbin).
set -euo pipefail
IMG_XZ="${1:?usage: verify-image.sh <img.xz>}"
DEBUGFS="$(brew --prefix e2fsprogs 2>/dev/null)/sbin/debugfs"
[ -x "$DEBUGFS" ] || DEBUGFS=debugfs

echo "==> xz integrity"
xz -t "$IMG_XZ"
BYTES=$(stat -f %z "$IMG_XZ")
if ((BYTES < 200000000 || BYTES > 2000000000)); then
	echo "ERROR: image size ${BYTES} outside 0.2-2.0GB bounds"
	exit 1
fi
echo "==> size OK (${BYTES} bytes)"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "==> decompress (for debugfs)"
xz -dc "$IMG_XZ" >"$TMP/root.img"

# MBR: partition-2 entry at 0x1BE+0x10; LBA start = bytes [8..11] little-endian.
P2_LBA=$(
	"$DEBUGFS" -R "quit" "$TMP/root.img" 2>/dev/null
	python3 - "$TMP/root.img" <<'PY'
import struct, sys
with open(sys.argv[1], "rb") as f:
    mbr = f.read(512)
lba = struct.unpack("<I", mbr[0x1CE + 8:0x1CE + 12])[0]
print(lba * 512)
PY
)
echo "==> rootfs partition offset: ${P2_LBA} bytes"
DEV="${TMP}/root.img?offset=${P2_LBA}"

for f in /opt/mrrc_modern/VERSION /etc/systemd/system/mrrc-modern.service \
	/etc/systemd/system/mrrc-firstboot.service /usr/local/bin/mrrc-show-password; do
	echo "--- $f ---"
	"$DEBUGFS" -R "cat $f" "$DEV" 2>/dev/null | head -6
done
echo "==> venv python present:"
"$DEBUGFS" -R "stat /opt/mrrc_modern/venv/bin/python3" "$DEV" 2>/dev/null | head -2
echo "==> VERIFY_OK"
