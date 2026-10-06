#!/usr/bin/env bash
# Fetch frpc for the Cloud Hub tunnel, pinned to the hub's frps version.
#
# Version pinning is not cosmetic: frp refuses a handshake when the client is
# newer than the server, and the failure surfaces as a tunnel that never comes
# up rather than as a version complaint from this script.
#
# The checksum comes from frp's own release checksums file — the URL carries
# the version, so this stays deterministic without a hash we would have to
# remember to update.
set -euo pipefail

FRP_VERSION="${MRRC_FRP_VERSION:-0.71.0}"
PLATFORM="${MRRC_FRP_PLATFORM:-linux_arm64}"
DEST="${1:?usage: fetch-frpc.sh <dest-dir>}"

BASE="${MRRC_FRP_BASE:-https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}}"
TARBALL="frp_${FRP_VERSION}_${PLATFORM}.tar.gz"
SUMS="frp_sha256_checksums.txt"

sha256_of() {  # macOS has shasum, Debian has sha256sum
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

mkdir -p "$DEST"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "frpc: fetching ${TARBALL} (frp ${FRP_VERSION})"
curl -fsSL --retry 3 -o "$tmp/$TARBALL" "$BASE/$TARBALL"
curl -fsSL --retry 3 -o "$tmp/$SUMS"    "$BASE/$SUMS"

want="$(awk -v f="$TARBALL" '$2 == f {print $1}' "$tmp/$SUMS" | head -1)"
[ -n "$want" ] || { echo "frpc: $SUMS has no entry for $TARBALL — refusing" >&2; exit 1; }

got="$(sha256_of "$tmp/$TARBALL")"
if [ "$got" != "$want" ]; then
  echo "frpc: checksum mismatch for $TARBALL" >&2
  echo "      want $want" >&2
  echo "      got  $got" >&2
  exit 1
fi

tar xzf "$tmp/$TARBALL" -C "$tmp"
bin="$(find "$tmp" -type f -name frpc | head -1)"
[ -n "$bin" ] || { echo "frpc: nothing named frpc inside $TARBALL" >&2; exit 1; }

install -m 0755 "$bin" "$DEST/frpc"
# The checksum above is the real gate; printing the version is a convenience.
# On a build host that cannot execute a Linux ELF (macOS) it must not fail the
# build, so say that instead of pretending.
version="$("$DEST/frpc" --version 2>/dev/null | head -1 || true)"
[ -n "$version" ] || version="(not runnable on this host)"
echo "frpc: installed $DEST/frpc — $version"
