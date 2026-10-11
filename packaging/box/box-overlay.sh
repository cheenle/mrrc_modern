#!/bin/bash -e
# W103D (ZTE, Amlogic S905L3A, Armbian) platform shell.
#
# It creates the conditions the shared service layer assumes, then hands over:
# the account, the board's WiFi firmware, the two distro masks, and the assets.
# Everything else — the code tree, the units, the CLIs, the env defaults, the
# import gate, the reclaim — is packaging/common/mrrc-service-layer.sh, because
# the Raspberry Pi image installs the exact same server and two implementations
# is how they drift (design doc, 2026-10-11).
set -euo pipefail

REPO_SRC="${REPO_SRC:-/src}"
MRRC_HOME=/opt/mrrc_modern
MRRC_USER=mrrc
MRRC_ASSETS=/opt/mrrc-build

# The two platform facts the shared layer takes as parameters. Both are stated
# here rather than left to its defaults, because a silent default is exactly how
# the box and the Pi drift apart again: the Pi passes :: (dual stack) and
# /boot/firmware, and tests/test_packaging_parity.py asserts these two — and only
# these two — are what differs between the images.
export MRRC_WEB_HOST=0.0.0.0
export MRRC_BOOT_DIR=/boot
export MRRC_ASSETS
export REPO_SRC

log() { printf '\n=== %s ===\n' "$*"; }

log "user and groups"
if ! id -u "$MRRC_USER" >/dev/null 2>&1; then
	adduser --system --group --home "$MRRC_HOME" --shell /bin/bash "$MRRC_USER"
fi
# dialout: /dev/ttyUSB* and /dev/ttyACM*; audio: the radio's USB sound card.
for g in dialout audio; do
	getent group "$g" >/dev/null 2>&1 || groupadd --system "$g"
	adduser "$MRRC_USER" "$g" >/dev/null
done

log "stage the code tree (the one exclusion list)"
mkdir -p "$MRRC_HOME" "$MRRC_ASSETS"
bash "$REPO_SRC/packaging/common/stage-tree.sh" "$REPO_SRC" "$MRRC_HOME"
# The service layer's own assets travel separately, because the exclusion list
# drops packaging/ from the installed tree — and they land at the same path the
# Pi builder stages them to, so the layer has one asset path to know about.
cp -a "$REPO_SRC/packaging/common/." "$MRRC_ASSETS/"

log "MT7663S WiFi N9 firmware (vendored factory build)"
# The base image's linux-firmware N9 is the 2020 build, which associates and then
# progressively dies on this board (scan wedges, auth timeouts, MCU mailbox -110
# after ~15 min). The factory 2021 build (7663mp1827) is the one the kernel port
# was validated against. See packaging/box/vendor-mt7663.md.
install -m 0644 "$REPO_SRC/vendor/mediatek/mt7663_n9_v3.bin" \
	/lib/firmware/mediatek/mt7663_n9_v3.bin
echo "223f73f17f0f986dc4e7167daa6eef14ffb41c713f22d70f9645eb049bdec80a  /lib/firmware/mediatek/mt7663_n9_v3.bin" | sha256sum -c
# A linux-firmware package upgrade would put the broken build back; the diversion
# sends package writes to .distrib instead of over our file.
dpkg-divert --add --no-rename /lib/firmware/mediatek/mt7663_n9_v3.bin

log "distro masks"
# Debian's own dnsmasq.service is enabled by the package and collides with
# systemd-resolved for port 53 — a failed unit on every fresh box, and harmless
# only by luck. Nothing here runs a standalone dnsmasq any more (the hotspot's
# shared mode is gone), but the daemon package can still be pulled in by another
# dependency, so the mask stays as the second line of defence.
systemctl mask dnsmasq.service || true
# Amlogic vendor images ship a getty on a vendor-only FIQ console; the mainline
# kernel does not have it and the unit would wait 90 s per boot.
systemctl mask serial-getty@ttyFIQ0.service || true

log "hand over to the shared service layer"
exec bash "$MRRC_ASSETS/mrrc-service-layer.sh"
