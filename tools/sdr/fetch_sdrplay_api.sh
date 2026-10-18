#!/bin/sh
# Fetch the SDRplay Hardware API (3.15) into vendor/sdrplay/ for the image build.
#
# Why a separate step and not a build-time download: the two image lines build
# in a chroot/container where sdrplay.com may be unreachable, and an image that
# silently ships without the API puts a "device not found" on a customer's
# screen.  So the tarball is fetched once, verified, and the service layer
# copies from vendor/ — same shape as the FT8 vendor subsets.
#
# Licence: libsdrplay_api / sdrplay_apiService are closed-source SDRplay
# binaries, free to use with SDRplay hardware.  sdrplay_license.txt ships with
# the image next to them (docs/sdr_receiver.md §7).
#
# Usage: sh tools/sdr/fetch_sdrplay_api.sh [--check]
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
DEST="$ROOT/vendor/sdrplay"
URL="https://sdrplay.com/download/hardware-api-linux/?wpdmdl=1906"
SHA256="3a97ca764263bbe76fb0f2220e6408942357e8864c19e1408a6d6987af382fe3"
BYTES="511429"

sha_of() {
	if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
	else shasum -a 256 "$1" | awk '{print $1}'; fi
}

if [ "${1:-}" = "--check" ]; then
	[ -f "$DEST/libsdrplay_api.so.3.15" ] || { echo "vendor/sdrplay missing" >&2; exit 1; }
	echo "vendor/sdrplay present: $(ls "$DEST")"
	exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "==> downloading $URL"
curl -fsSL --retry 3 -A "Mozilla/5.0" "$URL" -o "$tmp/api.bin"
got=$(sha_of "$tmp/api.bin")
size=$(wc -c < "$tmp/api.bin" | tr -d ' ')
if [ "$got" != "$SHA256" ] || [ "$size" != "$BYTES" ]; then
	echo "mrrc: SDRplay API changed upstream (size $size, sha $got);" \
	     "expected $BYTES / $SHA256 — review and bump this script" >&2
	exit 1
fi

# Makeself archive: extract without running the vendor installer (which is
# interactive and wants to write system paths).
sh "$tmp/api.bin" --noexec --target "$tmp/api" >/dev/null

mkdir -p "$DEST/arm64" "$DEST/inc"
install -m 644 "$tmp/api/arm64/libsdrplay_api.so.3.15" "$DEST/arm64/"
install -m 755 "$tmp/api/arm64/sdrplay_apiService"      "$DEST/arm64/"
install -m 644 "$tmp/api"/inc/sdrplay_api*.h            "$DEST/inc/"
install -m 644 "$tmp/api/sdrplay_license.txt"           "$DEST/"

echo "==> vendored into vendor/sdrplay (API 3.15, arm64)"
ls -l "$DEST/arm64"
