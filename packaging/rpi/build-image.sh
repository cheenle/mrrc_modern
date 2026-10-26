#!/usr/bin/env bash
# Build the MRRC Modern Raspberry Pi image (rpi64) via pi-gen in Docker.
# Prereqs: Docker Desktop running (Apple Silicon = native aarch64), >=20GB free.
# Usage: packaging/rpi/build-image.sh          (version from CHANGELOG top entry)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
PIGEN_REF="2026-06-18-raspios-bookworm-arm64"
WORK="${MRRC_PI_WORK:-$REPO_ROOT/build/pi-gen}"
OUT="${MRRC_PI_OUT:-$REPO_ROOT/dist/rpi}"

# Where the build's apt fetches from. pi-gen has no mirror switch at all: stage0
# debootstraps from deb.debian.org, which measured 65 KB/s from a container on
# this network while domestic mirrors managed 700-830 KB/s for the same files.
# Four upstream points are rewritten in the clone, all of them ordinary files:
# the debootstrap URL, the Debian sources list, the Raspberry Pi archive, and the
# container image's own apt (which runs first and is the one that would otherwise
# stall before anything else starts). Set MRRC_PI_APT_MIRROR= (empty) to use
# upstream.
APT_MIRROR="${MRRC_PI_APT_MIRROR-https://mirrors.tuna.tsinghua.edu.cn}"
# PyPI measured 37 KB/s here against 2.9 MB/s domestically. The virtualenv ships
# inside the image, so the device itself never runs pip.
PIP_INDEX="${MRRC_PI_PIP_INDEX-https://pypi.tuna.tsinghua.edu.cn/simple}"
# github.com served 33 KB/s here and truncated transfers mid-stream; a list,
# because these proxies go down — the empty entry at the end means GitHub itself
# is the last resort. fetch-frpc.sh verifies frp's own checksum per source, so a
# mirror can accelerate the download but cannot change what gets installed.
FRP_PROXY="${MRRC_PI_FRP_PROXY-https://gh-proxy.com/,https://ghfast.top/,}"

# 无线电管制域。**不是偏好设置**：pi-gen 的 stage2/02-net-tweaks/01-run.sh 是
#   if [ -v WPA_COUNTRY ]; then raspi-config nonint do_wifi_country "$WPA_COUNTRY"
#   elif [ -d /var/lib/NetworkManager ]; then echo WirelessEnabled=false > …state
# 而 build.sh 对未设变量只做 `export WPA_COUNTRY`（不赋值），`[ -v ]` 判的是
# "有没有值" —— 实测为假。于是**不设这个键的镜像出厂 Wi-Fi 是关的**：/settings
# 的 Wi-Fi 面板扫不到任何网络，/boot/firmware/mrrc-wifi.txt 预置也连不上。
# 默认 CN：2.4G 1–13 与 5G 149–165，最不容易把客户的 AP 排除在外。
WPA_COUNTRY_CFG="${MRRC_PI_WPA_COUNTRY-CN}"

# 版本号的唯一读法（spec 2026-10-27 §4.0）。不要再在这里写正则：镜像线标题是
# `## [镜像线 vX.Y.Z · FPn]`，只认 `## [v…]` 的写法会拿回桌面线的版本号。
VERSION="$(sh "$REPO_ROOT/packaging/common/version_of.sh" "$REPO_ROOT/CHANGELOG.md")"
: "${VERSION:?Could not read the version from CHANGELOG.md}"
echo "==> Building MRRC Modern ${VERSION} rpi64 image"
echo "==> WLAN country: ${WPA_COUNTRY_CFG}"

# ── preflight ──
docker info >/dev/null 2>&1 || {
	echo "ERROR: Docker daemon not running (open -a Docker)"
	exit 1
}
# Free space is checked on the WORK volume (pi-gen scratch, image and xz all
# land there; MRRC_PI_WORK can point at an external disk).
mkdir -p "$(dirname "$WORK")"
FREE_GB=$(df -Pk "$(dirname "$WORK")" | awk 'NR==2 {print int($4/1048576)}')
((FREE_GB >= 20)) || {
	echo "ERROR: need >=20GB free on $(dirname "$WORK"), have ${FREE_GB}GB"
	exit 1
}

# ── pi-gen clone (pinned; skip when the right ref is already in place —
#    allows pre-seeding the clone on hosts where GitHub is unreachable) ──
if [ -d "$WORK/.git" ] &&
	[ "$(git -c safe.directory="$WORK" -C "$WORK" describe --tags 2>/dev/null)" = "$PIGEN_REF" ]; then
	echo "==> pi-gen $PIGEN_REF already present, skipping clone"
else
	rm -rf "$WORK"
	git clone --depth 1 --branch "$PIGEN_REF" https://github.com/RPi-Distro/pi-gen.git "$WORK"
fi

# ── mirror rewrites (idempotent) ──
# A substring swap per file rather than a sources.list generator: the pinned ref
# is what makes these strings stable, and the patch says so when it finds the
# file already pointing elsewhere. python3 rather than sed -i because macOS sed
# and GNU sed disagree about in-place edits and about the escaping of a nested
# quote, and this repo already requires python3 elsewhere on the build host.
patch_text() { # patch_text <file> <from> <to>
	python3 - "$1" "$2" "$3" <<'PY'
import pathlib, sys
path, src, dst = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text(encoding="utf-8")
if src not in text:
    print(f"    {path}: already on {dst}" if dst in text else f"    {path}: {src!r} not found")
else:
    path.write_text(text.replace(src, dst), encoding="utf-8")
    print(f"    {path}: {src} -> {dst}")
PY
}

if [ -n "$APT_MIRROR" ]; then
	MIRROR_HOST="${APT_MIRROR#https://}"
	MIRROR_HOST="${MIRROR_HOST#http://}"
	MIRROR_HOST="${MIRROR_HOST%/}"
	echo "==> apt mirror: $MIRROR_HOST"
	patch_text "$WORK/stage0/prerun.sh" "http://deb.debian.org/debian/" "http://$MIRROR_HOST/debian/"
	patch_text "$WORK/stage0/00-configure-apt/files/sources.list" "deb.debian.org" "$MIRROR_HOST"
	# archive.raspberrypi.com/debian is the Raspberry Pi archive; the mirror
	# serves it under /raspberrypi/ and without the /debian suffix.
	patch_text "$WORK/stage0/00-configure-apt/files/raspi.list" "archive.raspberrypi.com/debian" "$MIRROR_HOST/raspberrypi"
	# The container image's own apt is the fourth upstream point and the first
	# one to run: without it the very first build sits in
	# `apt-get install git parted …` at 65 KB/s. Cached as a layer afterwards.
	#
	# Both layouts are covered on purpose: debian:bullseye (the base this
	# Dockerfile declares) has no /etc/apt/sources.list at all — it ships the
	# deb822 /etc/apt/sources.list.d/debian.sources, so a sed keyed on the
	# classic path fails the whole image build in seconds (measured
	# 2026-10-11: `sed: can't read /etc/apt/sources.list`). Which of the two
	# exists is a property of the tag, not of this repository.
	patch_text "$WORK/Dockerfile" "RUN apt-get -y update" \
		"RUN for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do if [ -f \"\$f\" ]; then sed -i \"s|deb.debian.org|$MIRROR_HOST|g\" \"\$f\"; fi; done; apt-get -y update"
fi

# ── stage: copy ours in, stage the code tree the ONE way ──
rsync -a --delete "$REPO_ROOT/packaging/rpi/pi-gen-stage4/" "$WORK/rpi-stage4/"
STAGE_FILES="$WORK/rpi-stage4/01-deploy-mrrc/files"
mkdir -p "$STAGE_FILES/opt/mrrc_modern" "$STAGE_FILES/opt/mrrc-build"
bash "$REPO_ROOT/packaging/common/stage-tree.sh" "$REPO_ROOT" "$STAGE_FILES/opt/mrrc_modern"
# The service layer's own assets, at the path it knows. packaging/ is dropped
# from the installed tree by the exclusion list, so this is a second copy on
# purpose — and it is not /tmp: pi-gen's on_chroot mounts a tmpfs over the
# rootfs's /tmp, which would hide anything staged there.
rsync -a "$REPO_ROOT/packaging/common/" "$STAGE_FILES/opt/mrrc-build/"

# pi-gen's chroot does not inherit this shell's environment, so every platform
# parameter travels in a file the stage script sources. Generated, not templated.
cat >"$STAGE_FILES/opt/mrrc-build/build.env" <<EOF
# Generated by packaging/rpi/build-image.sh — build-time only, reclaimed by the
# service layer before the image is assembled, never part of the artifact.
MRRC_VERSION='${VERSION}'
MRRC_WEB_HOST='::'
MRRC_BOOT_DIR='/boot/firmware'
PIP_INDEX_URL='${PIP_INDEX}'
MRRC_FRP_PROXY='${FRP_PROXY}'
EOF

# version.txt is the cross-platform name (support bundle manifest + the upgrade
# channel read it); VERSION is kept for operators upgrading in place.
printf '%s\n' "${VERSION#v}" >"$STAGE_FILES/opt/mrrc_modern/version.txt"
cp "$STAGE_FILES/opt/mrrc_modern/version.txt" "$STAGE_FILES/opt/mrrc_modern/VERSION"

# ── export markers ──
# Modern pi-gen: a stage exports the final image when it contains an
# EXPORT_IMAGE file (assembly logic lives in the top-level export-image/).
# Skip stage2's bare-Lite export; export once, after stage4 content lands.
touch "$WORK/stage2/SKIP_IMAGES"
touch "$WORK/rpi-stage4/EXPORT_IMAGE"

# ── pi-gen config ──
# TARGET_HOSTNAME is MRRC-Modern rather than pi-gen's default raspberrypi: the
# settings page's identity field already defaults to that name (box_ops), so a
# different actual hostname would be a silent divergence between what the box
# calls itself and what mDNS answers to.
cat >"$WORK/config" <<EOF
# `v` 前缀是发布规范（.agents/skills/dual-platform-release/release-artifacts.json
# 与站上的 `MRRC-Modern-v<ver>-rpi64.img.xz`）；version_of.sh 给的是不带 v 的版本号，
# 所以在这里加回去 —— 少了它，产物名与下载卡的匹配规则对不上。
IMG_NAME="MRRC-Modern-v${VERSION#v}-rpi64"
RELEASE=bookworm
TARGET_ARCH=64
TARGET_HOSTNAME=MRRC-Modern
WPA_COUNTRY='${WPA_COUNTRY_CFG}'
FIRST_USER_NAME=mrrc
FIRST_USER_PASS=mrrc
ENABLE_SSH=1
DEPLOY_COMPRESSION=none
STAGE_LIST="stage0 stage1 stage2 rpi-stage4"
EOF

cd "$WORK"
# Mount the host pi-gen dir into the container so work/ + deploy/ (multi-GB)
# live on the WORK volume instead of Docker.raw.
export PIGEN_DOCKER_OPTS="-v $WORK:/pi-gen"
# PRESERVE_CONTAINER=1 keeps the pigen_work container on failure. Resume with:
#   CONTINUE=1 IGNORE_FILE_CHANGES=1 ./build-docker.sh   (after a failure)
IGNORE_FILE_CHANGES=1 PRESERVE_CONTAINER=1 ./build-docker.sh

# ── artifact ──
mkdir -p "$OUT"
IMG=$(ls "$WORK/deploy/"*.img | head -1)
XZ_OUT="$OUT/MRRC-Modern-v${VERSION#v}-rpi64.img.xz"
xz -T0 -9 -c "$IMG" >"$XZ_OUT"
rm -f "$IMG"
echo "==> Done: $XZ_OUT"
if command -v sha256sum >/dev/null 2>&1; then sha256sum "$XZ_OUT"; else shasum -a 256 "$XZ_OUT"; fi
