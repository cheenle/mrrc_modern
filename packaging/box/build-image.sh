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

# version.txt is not a source file: it is generated from the CHANGELOG's top
# version, the same way packaging/rpi and packaging/macos do it, and the Pi
# builder's comment says why it matters — the support bundle manifest and the
# upgrade channel read it out of the installed tree.
VERSION="$(grep -m1 -oE '## \[v[0-9]+\.[0-9]+\.[0-9]+\]' "$REPO/CHANGELOG.md" \
  | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+')"
: "${VERSION:?Could not read a version from CHANGELOG.md}"
OUT_IMG="MRRC-Modern-${VERSION#v}-w103d.img"

# Where the build's apt fetches from. The base image points at deb.debian.org,
# which measured 65 KB/s from a container on this network (290 KB/s from the
# host) while domestic mirrors managed 700-830 KB/s for the same file — the
# difference between a ~20 minute wait and a ~2 minute one for the ~80 MiB of
# packages the overlay installs.
#
# This is a mirror swap only: the URIs change, the Signed-By keyring does not,
# because the mirror is the same archive.
#
# Set MRRC_BOX_APT_MIRROR= (empty) to leave the base image's sources alone.
APT_MIRROR="${MRRC_BOX_APT_MIRROR-https://mirrors.tuna.tsinghua.edu.cn}"

# Where the build's pip fetches from, for the same reason one layer up: PyPI
# measured 37 KB/s here against 2.9 MB/s from a domestic mirror, slow enough
# that the dependency install sat with an established connection and no
# progress for seven minutes and had to be killed.
#
# This one is build-time only — the virtualenv is baked into the image, so the
# box never runs pip. Set MRRC_BOX_PIP_INDEX= to use PyPI itself.
PIP_INDEX="${MRRC_BOX_PIP_INDEX-https://pypi.tuna.tsinghua.edu.cn/simple}"

# Where the build fetches the frp release from, for the same reason a third
# time: github.com served 33 KB/s here and truncated the transfer mid-stream,
# against 2.3 MB/s through a mirror. fetch-frpc.sh still verifies frp's own
# checksum, so a mirror serving something else is rejected rather than
# installed. Set MRRC_BOX_FRP_PROXY= to fetch from github.com directly.
FRP_PROXY="${MRRC_BOX_FRP_PROXY-https://gh-proxy.com/}"

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
#
# There is deliberately no apt-get here. Everything the host side runs —
# losetup, mount, mknod, df, awk, chroot, sync — is in the base image; the
# tools the overlay needs (rsync, curl, mkfs) are used inside the chroot,
# where the rootfs already has them. Installing them here instead cost an
# `apt-get update` against a mirror that is sometimes slow enough to stall the
# build for minutes, for packages that were never invoked.
docker run --rm --platform linux/arm64 --privileged \
  -v "$REPO":/src:ro \
  -v "$WORK":/work \
  -e MRRC_BOX_APT_MIRROR="$APT_MIRROR" \
  -e MRRC_BOX_PIP_INDEX="$PIP_INDEX" \
  -e MRRC_BOX_FRP_PROXY="$FRP_PROXY" \
  -e MRRC_VERSION="$VERSION" \
  debian:bookworm bash -euo pipefail -c '
    img=/work/w103d.img
    loop="$(losetup --show -fP "$img")"
    trap "umount -R /mnt 2>/dev/null || true; losetup -d $loop 2>/dev/null || true" EXIT

    # The kernel scans the partitions, but the /dev nodes are udev work and a
    # container has none. Without them the mounts below fail.
    . /src/packaging/box/part-nodes.sh
    part_nodes "$loop"
    [ -b "${loop}p2" ] || { echo "✗ ${loop}p2 missing after the -P scan" >&2; exit 1; }

    # MBR: p1 is the FAT boot partition, p2 the ext4 rootfs.
    mkdir -p /mnt
    mount "${loop}p2" /mnt
    mount "${loop}p1" /mnt/boot

    # Point the rootfs apt at a mirror that can actually serve it (see
    # APT_MIRROR). Only the Debian archive and its security suite are rewritten;
    # the Armbian sources are left where they are, since that is where the
    # kernel packages come from. The rewrite stays in the image, so a deployed
    # box also gets the faster mirror — set MRRC_BOX_APT_MIRROR= to skip it.
    mirror="${MRRC_BOX_APT_MIRROR:-}"
    if [ -n "$mirror" ]; then
      for f in /mnt/etc/apt/sources.list /mnt/etc/apt/sources.list.d/*.sources; do
        [ -f "$f" ] || continue
        sed -i -e "s|https\?://deb\.debian\.org|$mirror|g" \
               -e "s|https\?://security\.debian\.org|$mirror/debian-security|g" "$f"
      done
      echo "==> apt pointed at $mirror"
    fi

    free_mb="$(df -Pm /mnt | awk "NR==2 {print \$4}")"
    echo "rootfs free before overlay: ${free_mb} MiB"
    if [ "$free_mb" -lt 400 ]; then
      echo "rootfs too small: ${free_mb} MiB < 400 MiB — trim the payload or grow p2" >&2
      exit 1
    fi

    for d in dev proc sys; do mount --bind /$d /mnt/$d; done

    # chroot changes the root, so the container /src is not reachable from
    # inside it: a chrooted /src resolves to /mnt/src, and the overlay script
    # then fails with a bare "No such file or directory" for a path that plainly
    # exists on this side. Bind it in, and keep it read-only.
    mkdir -p /mnt/src
    mount --bind /src /mnt/src
    mount -o remount,ro,bind /mnt/src

    # install.sh runs apt and pip inside the chroot, where systemd-resolved is
    # not running: the rootfs resolv.conf is a symlink to a socket that does not
    # exist, so every lookup fails and the build dies in apt. Lend it the
    # container resolver.
    rm -f /mnt/etc/resolv.conf
    cp /etc/resolv.conf /mnt/etc/resolv.conf

    # chroot keeps this shell environment, so exporting the index here is how
    # install.sh gets it. pip reads PIP_INDEX_URL itself; nothing in the repo
    # has to know about the mirror.
    if [ -n "${MRRC_BOX_PIP_INDEX:-}" ]; then
      export PIP_INDEX_URL="$MRRC_BOX_PIP_INDEX"
      echo "==> pip index: $MRRC_BOX_PIP_INDEX"
    fi

    # Same channel to fetch-frpc.sh, which owns the version and the GitHub path.
    if [ -n "${MRRC_BOX_FRP_PROXY:-}" ]; then
      export MRRC_FRP_PROXY="$MRRC_BOX_FRP_PROXY"
      echo "==> frp via: $MRRC_BOX_FRP_PROXY"
    fi

    chroot /mnt /bin/bash -c "REPO_SRC=/src /src/packaging/box/box-overlay.sh"

    umount /mnt/src
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
