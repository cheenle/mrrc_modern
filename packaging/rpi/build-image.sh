#!/usr/bin/env bash
# Build the MRRC Modern Raspberry Pi image (rpi64) via pi-gen in Docker.
# Prereqs: Docker Desktop running (Apple Silicon = native aarch64), >=20GB free.
# Usage: packaging/rpi/build-image.sh          (version from CHANGELOG top entry)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PIGEN_REF="2026-06-18-raspios-bookworm-arm64"
WORK="${MRRC_PI_WORK:-$REPO_ROOT/build/pi-gen}"
OUT="${MRRC_PI_OUT:-$REPO_ROOT/dist/rpi}"

VERSION="$(grep -m1 -oE '## \[v[0-9]+\.[0-9]+\.[0-9]+\]' "$REPO_ROOT/CHANGELOG.md" | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' )"
: "${VERSION:?Could not read version from CHANGELOG.md}"
echo "==> Building MRRC Modern ${VERSION} rpi64 image"

# ── preflight ──
docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon not running (open -a Docker)"; exit 1; }
FREE_GB=$(df -Pk "$REPO_ROOT" | awk 'NR==2 {print int($4/1048576)}')
(( FREE_GB >= 20 )) || { echo "ERROR: need >=20GB free, have ${FREE_GB}GB"; exit 1; }

# ── pi-gen clone (pinned; skip when the right ref is already in place —
#    allows pre-seeding the clone on hosts where GitHub is unreachable) ──
if [ -d "$WORK/.git" ] && \
   [ "$(git -c safe.directory="$WORK" -C "$WORK" describe --tags 2>/dev/null)" = "$PIGEN_REF" ]; then
    echo "==> pi-gen $PIGEN_REF already present, skipping clone"
else
    rm -rf "$WORK"
    git clone --depth 1 --branch "$PIGEN_REF" https://github.com/RPi-Distro/pi-gen.git "$WORK"
fi

# ── stage: copy ours in, inject code tree ──
rsync -a --delete "$REPO_ROOT/packaging/rpi/pi-gen-stage4/" "$WORK/rpi-stage4/"
STAGE_FILES="$WORK/rpi-stage4/01-deploy-mrrc/files"
mkdir -p "$STAGE_FILES/opt/mrrc_modern"
rsync -a "$REPO_ROOT/" "$STAGE_FILES/opt/mrrc_modern/" \
  --exclude ".git/" --exclude "venv/" --exclude ".venv/" --exclude "dist/" \
  --exclude "build/" --exclude "logs/" --exclude "certs/" --exclude "promo/" \
  --exclude "FT710Mobile/" --exclude "FT710Android/" --exclude "website/" \
  --exclude "tests/" --exclude ".agents/" --exclude "docs/" --exclude "SDD/" \
  --exclude "packaging/" --exclude "macos/" --exclude "windows/" \
  --exclude "__pycache__/" --exclude "*.pyc" --exclude ".DS_Store" \
  --exclude "atr1000_tuner.json"
printf '%s\n' "${VERSION#v}" > "$STAGE_FILES/opt/mrrc_modern/VERSION"
mkdir -p "$STAGE_FILES/opt/mrrc_modern/vendor/ftdi"

# ── pi-gen config ──
cat > "$WORK/config" <<EOF
IMG_NAME="MRRC-Modern-${VERSION}-rpi64"
RELEASE=bookworm
TARGET_ARCH=64
FIRST_USER_NAME=mrrc
FIRST_USER_PASS=mrrc
ENABLE_SSH=1
DEPLOY_COMPRESSION=none
STAGE_LIST="stage0 stage1 stage2 rpi-stage4"
EOF

cd "$WORK"
IGNORE_FILE_CHANGES=1 PRESERVE_CONTAINER=0 ./build-docker.sh

# ── artifact ──
mkdir -p "$OUT"
IMG=$(ls "$WORK/deploy/"*.img | head -1)
XZ_OUT="$OUT/MRRC-Modern-${VERSION}-rpi64.img.xz"
xz -T0 -9 -c "$IMG" > "$XZ_OUT"
rm -f "$IMG"
echo "==> Done: $XZ_OUT"
if command -v sha256sum >/dev/null 2>&1; then sha256sum "$XZ_OUT"; else shasum -a 256 "$XZ_OUT"; fi
