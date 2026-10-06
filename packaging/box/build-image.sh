#!/usr/bin/env bash
# Build MRRC-Modern-<ver>-w103d.img.gz from ophub's Armbian image.
#
# Runs on macOS (Apple Silicon). The image is arm64 and so is this host, so the
# chroot needs no qemu — the same property that makes packaging/rpi/build-image.sh
# work. Docker must be running: macOS cannot loop-mount ext4 without it.
#
#   ./build-image.sh            # full build
#   ./build-image.sh --check    # prerequisites only, changes nothing
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="${MRRC_BOX_WORK:-$REPO/build/w103d}"
OUT="$REPO/dist/w103d"

# Pinned by SHA-256. The four markers in the name are load-bearing:
#   amlogic            platform
#   s905l3a-w103d      the board config; the generic s905l3a image has no DTBs
#                      or kernel patches for this box, i.e. no MT7663S WiFi
#   bookworm           Python 3.11
#   6.18.54            the kernel series carrying the W103D patches + N9 firmware
IMG_NAME="Armbian_26.11.0_amlogic_s905l3a-w103d_bookworm_6.18.54_server_2026.10.01.img.gz"
IMG_SHA="998d5244ac2274077c091050b9db620c22a8412989766fbd728b045a0fed1ae9"
IMG_TAG="Armbian_bookworm_arm64_server_2026.10"
IMG_URL="https://github.com/ophub/amlogic-s9xxx-armbian/releases/download/$IMG_TAG/$IMG_NAME"

VERSION="$(cat "$REPO/version.txt" 2>/dev/null || echo dev)"
OUT_IMG="MRRC-Modern-${VERSION}-w103d.img"

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

check() {
  local ok=0
  if ! docker info >/dev/null 2>&1; then
    echo "✗ Docker daemon is not running (macOS cannot loop-mount ext4 without it)" >&2
    ok=1
  fi
  [ -f "$REPO/vendor/ftdi/libft4222.so" ] || {
    echo "✗ vendor/ftdi/libft4222.so missing" >&2; ok=1; }
  [ -L "$REPO/vendor/ftdi/libftd2xx.so" ] || {
    echo "✗ vendor/ftdi/libftd2xx.so is not a symlink" >&2; ok=1; }
  [ -f "$REPO/packaging/box/profiles/ft710.env" ] || {
    echo "✗ packaging/box/profiles/ missing" >&2; ok=1; }
  command -v rsync >/dev/null || { echo "✗ rsync missing" >&2; ok=1; }
  command -v curl  >/dev/null || { echo "✗ curl missing" >&2; ok=1; }
  return $ok
}

if [ "${1:-}" = "--check" ]; then
  if check; then echo "✓ prerequisites ok"; exit 0; else exit 1; fi
fi

check || { echo "prerequisites failed; fix the above first" >&2; exit 1; }

mkdir -p "$WORK" "$OUT"

if [ ! -f "$WORK/$IMG_NAME" ]; then
  echo "==> fetching $IMG_NAME"
  curl -fL --retry 3 -o "$WORK/$IMG_NAME.part" "$IMG_URL"
  mv "$WORK/$IMG_NAME.part" "$WORK/$IMG_NAME"
fi

echo "==> verifying SHA-256"
got="$(sha256_of "$WORK/$IMG_NAME")"
[ "$got" = "$IMG_SHA" ] || {
  echo "✗ $IMG_NAME sha256 $got != $IMG_SHA" >&2; exit 1; }
echo "    ok"

if [ ! -f "$WORK/w103d.img" ]; then
  echo "==> decompressing"
  gunzip -c "$WORK/$IMG_NAME" > "$WORK/w103d.img"
fi

echo "==> building inside a privileged arm64 container"
# --privileged is for losetup/mount; /dev is needed for the loop devices.
# The container only mounts, overlays and unmounts. Compression happens on the
# host afterwards, which avoids copying a 3.4 GB image inside the work volume.
docker run --rm --platform linux/arm64 --privileged \
  -v "$REPO":/src:ro \
  -v "$WORK":/work \
  debian:bookworm bash -euo pipefail -c '
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
      e2fsprogs util-linux rsync curl ca-certificates >/dev/null

    img=/work/w103d.img
    loop="$(losetup --show -fP "$img")"
    trap "umount -R /mnt 2>/dev/null || true; losetup -d $loop 2>/dev/null || true" EXIT

    # MBR: p1 is the FAT boot partition, p2 the ext4 rootfs.
    mkdir -p /mnt
    mount "${loop}p2" /mnt
    mount "${loop}p1" /mnt/boot

    free_mb="$(df -Pm /mnt | awk "NR==2 {print \$4}")"
    echo "rootfs free before overlay: ${free_mb} MiB"
    if [ "$free_mb" -lt 400 ]; then
      echo "rootfs too small: ${free_mb} MiB < 400 MiB — trim the payload or grow p2" >&2
      exit 1
    fi

    for d in dev proc sys; do mount --bind /$d /mnt/$d; done
    chroot /mnt /bin/bash -c "REPO_SRC=/src /src/packaging/box/box-overlay.sh"
    for d in dev proc sys; do umount /mnt/$d; done

    sync
    umount /mnt/boot
    umount /mnt
    losetup -d "$loop"
    echo "==> overlay applied"
  '

echo "==> compressing"
gzip -9 -c "$WORK/w103d.img" > "$OUT/$OUT_IMG.gz"
echo "==> artifact: $OUT/$OUT_IMG.gz"
printf '    sha256:  '; sha256_of "$OUT/$OUT_IMG.gz"
