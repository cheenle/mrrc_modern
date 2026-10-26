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

# Arguments. --preflash is the ship-ready variant (design 2026-10-17): fixed
# factory credentials plus the system half (time zone/locale) baked in. Off by
# default — the default image keeps generating a per-box password on first boot,
# and tests/test_preflash.py pins that default rather than trusting this comment.
CHECK_ONLY=0
PREFLASH=0
for arg in "$@"; do
	case "$arg" in
	--check) CHECK_ONLY=1 ;;
	--preflash) PREFLASH=1 ;;
	-*)
		echo "unknown option: $arg" >&2
		exit 2
		;;
	*)
		echo "unknown argument: $arg" >&2
		exit 2
		;;
	esac
done

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
# 镜像线优先：`## [镜像线 vX.Y.Z]` 是盒子/树莓派这条线的版本，桌面安装包线另有编号
# （见 CHANGELOG 顶部说明）。没有镜像线标题时才退回桌面标题。
# 修复包编号（2026-10-22 起）就挂在标题里：`## [镜像线 v1.28.5 · FP1]`。正则必须把
# 可选后缀吃掉 —— 否则 `grep -m1` 会跳过带 FP 的条目，落回上一条**没有 FP 的旧标题**，
# 打出来的镜像自称一个更早的版本（实测：1.28.6 的树，镜像写成 1.28.4）。
VERSION="$(grep -m1 -oE '## \[镜像线 v[0-9]+\.[0-9]+\.[0-9]+( · FP[0-9]+)?\]' "$REPO/CHANGELOG.md" |
	grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
if [ -z "$VERSION" ]; then
	VERSION="$(grep -m1 -oE '## \[v[0-9]+\.[0-9]+\.[0-9]+\]' "$REPO/CHANGELOG.md" |
		grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+')"
fi
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
# against 2.3 MB/s through a mirror. A list, because these proxies go down — the
# empty entry at the end means GitHub itself is the last resort. fetch-frpc.sh
# still verifies frp's own checksum per source, so a mirror can accelerate the
# download but cannot change what gets installed.
FRP_PROXY="${MRRC_BOX_FRP_PROXY-https://gh-proxy.com/,https://ghfast.top/,}"

sha256_of() {
	if command -v sha256sum >/dev/null 2>&1; then
		sha256sum "$1" | cut -d' ' -f1
	else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

check() {
	local ok=0
	if ! docker info >/dev/null 2>&1; then
		echo "✗ Docker daemon is not running (macOS cannot loop-mount ext4 without it)" >&2
		ok=1
	fi
	[ -f "$REPO/vendor/ftdi/libft4222.so" ] || {
		echo "✗ vendor/ftdi/libft4222.so missing" >&2
		ok=1
	}
	[ -L "$REPO/vendor/ftdi/libftd2xx.so" ] || {
		echo "✗ vendor/ftdi/libftd2xx.so is not a symlink" >&2
		ok=1
	}
	[ -f "$REPO/packaging/common/profiles/ft710.env" ] || {
		echo "✗ packaging/common/profiles/ missing" >&2
		ok=1
	}
	command -v rsync >/dev/null || {
		echo "✗ rsync missing" >&2
		ok=1
	}
	command -v curl >/dev/null || {
		echo "✗ curl missing" >&2
		ok=1
	}
	return $ok
}

if [ "$CHECK_ONLY" = "1" ]; then
	if check; then
		echo "✓ prerequisites ok"
		exit 0
	else exit 1; fi
fi

check || {
	echo "prerequisites failed; fix the above first" >&2
	exit 1
}

mkdir -p "$WORK" "$OUT"

if [ ! -f "$WORK/$IMG_NAME" ]; then
	echo "==> fetching $IMG_NAME"
	curl -fL --retry 3 -o "$WORK/$IMG_NAME.part" "$IMG_URL"
	mv "$WORK/$IMG_NAME.part" "$WORK/$IMG_NAME"
fi

echo "==> verifying SHA-256"
got="$(sha256_of "$WORK/$IMG_NAME")"
[ "$got" = "$IMG_SHA" ] || {
	echo "✗ $IMG_NAME sha256 $got != $IMG_SHA" >&2
	exit 1
}
echo "    ok"

# $WORK/w103d.img is not a scratch file: it is the tree the *previous* build
# left behind, with its packages already installed. Reusing it silently stacks a
# second overlay on the first, and pip only ever adds — 2026-10-26 that produced
# an image carrying BOTH `scipy-1.14.1.dist-info` and `scipy-1.17.1.dist-info`,
# so the 1.17.1 native modules were still on disk next to the downgraded ones.
# Refuse rather than guess: a fresh unpack costs three minutes, a shipped image
# with the wrong scipy bricked every box flashed with it (SIGSEGV in
# `import scipy.signal`, service restart-looping, no web UI).
if [ -f "$WORK/w103d.img" ]; then
	if [ "${MRRC_BOX_REUSE_TREE:-0}" = "1" ]; then
		echo "==> reusing the existing tree $WORK/w103d.img (MRRC_BOX_REUSE_TREE=1)"
		echo "    note: pip only adds packages, it never removes the previous overlay's"
	else
		{
			echo "✗ $WORK/w103d.img already exists — that is the tree a PREVIOUS build"
			echo "  overlaid, not a pristine unpack. Stacking on it leaves the old"
			echo "  site-packages in place (measured 2026-10-26: two scipy dist-infos in"
			echo "  one image, and the box crash-looped on import)."
			echo
			echo "  Start clean:      rm -f $WORK/w103d.img   then run this again"
			echo "  Really reuse it:  MRRC_BOX_REUSE_TREE=1 $0 $*"
		} >&2
		exit 1
	fi
else
	echo "==> decompressing"
	gunzip -c "$WORK/$IMG_NAME" >"$WORK/w103d.img"
fi

echo "==> building inside a privileged arm64 container"
if [ "$PREFLASH" = "1" ]; then
	echo "==> preflash: factory credentials will be baked in (root/user/web + TZ/locale)"
fi
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
	-e MRRC_PREFLASH="$PREFLASH" \
	debian:bookworm bash -euo pipefail -c '
    img=/work/w103d.img
    loop="$(losetup --show -fP "$img")"
    trap "umount -R /mnt 2>/dev/null || true; losetup -d $loop 2>/dev/null || true" EXIT

    # sourcing part-nodes.sh, which lives in packaging/common/ — the Pi image
    # verification container needs the same mknod trick (a container has no udev,
    # so `losetup -P` produces partitions the kernel knows about and /dev does not).
    # Keep prose in here free of apostrophes: this whole block is one single-quoted
    # argument, and one of them ends it (see ShellHygieneTests.test_shell_scripts_parse).
    . /src/packaging/common/part-nodes.sh
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

    # Ship-ready variant. One flag, one writer: the shared service layer acts on
    # it only when it is exactly 1, and preflash.sh refuses to write anything
    # otherwise (both are pinned by tests/test_preflash.py).
    export MRRC_PREFLASH="${MRRC_PREFLASH:-0}"
    echo "==> preflash: $MRRC_PREFLASH"

    chroot /mnt /bin/bash -c "REPO_SRC=/src /src/packaging/box/box-overlay.sh"

    # Put the resolver back before the image is shipped. The block above hands the
    # chroot the resolv.conf of the build container so apt/pip can resolve; the base image ships
    # `/etc/resolv.conf -> /run/systemd/resolve/stub-resolv.conf` instead, and that
    # symlink is what makes systemd-resolved (which NM feeds) the resolver glibc
    # actually talks to. Leaving the copy behind ships a *foreign* file: measured on a
    # delivered box 2026-10-25, `resolvectl status` said `resolv.conf mode: foreign`,
    # the file held one server while `resolvectl dns wlan0` already had three - so
    # every `ipv4.dns` an operator or the product writes is inert, and a customer whose
    # LAN is not the build LAN gets a dead nameserver and 5 s timeouts.
    if [ -e /mnt/usr/lib/systemd/systemd-resolved ]; then
      rm -f /mnt/etc/resolv.conf
      ln -s ../run/systemd/resolve/stub-resolv.conf /mnt/etc/resolv.conf
      echo "==> resolv.conf restored to the systemd-resolved stub symlink"
    else
      echo "==> no systemd-resolved in this base image — leaving resolv.conf alone" >&2
    fi

    umount /mnt/src
    for d in dev proc sys; do umount /mnt/$d; done

    sync
    umount /mnt/boot
    umount /mnt
    losetup -d "$loop"
    echo "==> overlay applied"
  '

echo "==> compressing"
gzip -9 -c "$WORK/w103d.img" >"$OUT/$OUT_IMG.gz"
echo "==> artifact: $OUT/$OUT_IMG.gz"
printf '    sha256:  '
sha256_of "$OUT/$OUT_IMG.gz"
