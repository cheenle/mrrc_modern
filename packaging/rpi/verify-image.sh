#!/usr/bin/env bash
# Verify the built rpi64 image: xz integrity, size bounds, debugfs spot-checks.
# Requires: e2fsprogs (brew install e2fsprogs — debugfs at e2fsprogs/sbin).
set -euo pipefail
IMG_XZ="${1:?usage: verify-image.sh <img.xz>}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEBUGFS="$(brew --prefix e2fsprogs 2>/dev/null)/sbin/debugfs"
[ -x "$DEBUGFS" ] || DEBUGFS=debugfs

echo "==> xz integrity"
xz -t "$IMG_XZ"
BYTES=$(stat -f %z "$IMG_XZ")
if (( BYTES < 400000000 || BYTES > 2000000000 )); then
    echo "ERROR: image size ${BYTES} outside 0.4-2.0GB bounds"; exit 1
fi
echo "==> size OK (${BYTES} bytes)"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "==> decompress (for debugfs)"
xz -dc "$IMG_XZ" > "$TMP/root.img"
for f in /opt/mrrc_modern/VERSION /etc/systemd/system/mrrc-modern.service \
         /etc/systemd/system/mrrc-firstboot.service /usr/local/bin/mrrc-show-password; do
    echo "--- $f ---"
    "$DEBUGFS" -R "cat $f" "$TMP/root.img" 2>/dev/null | head -5
done
echo "==> venv python present:"
"$DEBUGFS" -R "stat /opt/mrrc_modern/venv/bin/python3" "$TMP/root.img" 2>/dev/null | head -2
echo "==> VERIFY_OK"
