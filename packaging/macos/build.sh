#!/usr/bin/env bash
# Build the macOS arm64 release of MRRC FT-710 into dist/macos/.
# Bash mirror of packaging/windows/build.ps1. Run on the developer's Mac.
#
# Prerequisites (see mac_pack.md):
#   - Xcode Command Line Tools (codesign, hdiutil)
#   - brew install portaudio   (for the pyaudio wheel)
#   - a venv with requirements.txt + packaging/macos/requirements-build.txt
#
# Usage:
#   source .venv/bin/activate
#   pip install -r packaging/macos/requirements-build.txt
#   packaging/macos/build.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DIST_ROOT="$REPO_ROOT/dist/macos"
PYI_ROOT="$DIST_ROOT/_pyinstaller"
APP_BUNDLE="$DIST_ROOT/MRRC-FT710.app"
APP_MACOS="$APP_BUNDLE/Contents/MacOS"
BUILD_WORK="$REPO_ROOT/build/pyinstaller"

cd "$REPO_ROOT"

# ---- version (single source of truth: top CHANGELOG entry) ---------------
VERSION="$(grep -m1 -oE '## \[v[0-9]+\.[0-9]+\.[0-9]+\]' CHANGELOG.md | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' || true)"
: "${VERSION:?Could not extract version from CHANGELOG.md (expected a '## [vX.Y.Z]' heading)}"
export MRRC_VERSION="$VERSION"
echo "==> Building MRRC FT-710 ${VERSION} (arm64) for macOS"

PYBIN="${PYTHON:-python3}"

# ---- Step 0: syntax check -------------------------------------------------
echo "==> Syntax check"
PYFILES=("$REPO_ROOT"/*.py)
"$PYBIN" -m py_compile "${PYFILES[@]}"

# ---- Step 1: tests (must be green — same gate as the Windows build) ------
echo "==> Tests"
"$PYBIN" -m unittest discover -s tests

# ---- Step 2: FTDI dylib check (warn only; S-meter fallback is fine) -------
FT4222="$REPO_ROOT/vendor/ftdi/macos/libft4222.dylib"
FTD2XX="$REPO_ROOT/vendor/ftdi/macos/libftd2xx.dylib"
if [[ ! -f "$FT4222" || ! -f "$FTD2XX" ]]; then
  echo "WARNING: macOS FTDI dylibs missing under vendor/ftdi/macos/." >&2
  echo "         The app will build, but FT4222 true spectrum will fall back to S-meter." >&2
  echo "         Missing: ${FT4222##*/}, ${FTD2XX##*/}" >&2
fi

# ---- Step 3: PyInstaller x3 ---------------------------------------------
echo "==> PyInstaller"
rm -rf "$PYI_ROOT" "$BUILD_WORK"
"$PYBIN" -m PyInstaller packaging/pyinstaller/scope_pipe.spec \
    --noconfirm --distpath "$PYI_ROOT" --workpath "$BUILD_WORK"
"$PYBIN" -m PyInstaller packaging/pyinstaller/ft710_server.spec \
    --noconfirm --distpath "$PYI_ROOT" --workpath "$BUILD_WORK"
"$PYBIN" -m PyInstaller packaging/macos/ft710_launcher.spec \
    --noconfirm --distpath "$PYI_ROOT" --workpath "$BUILD_WORK"

# ---- Step 4: assemble .app (hand-built; no PyInstaller BUNDLE) ----------
echo "==> Assemble .app"
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_MACOS" "$APP_BUNDLE/Contents/Resources"
# Info.plist with the CHANGELOG version injected.
sed "s/__VERSION__/${VERSION}/" "$SCRIPT_DIR/Info.plist" \
    > "$APP_BUNDLE/Contents/Info.plist"

# launcher onefile exe (the entry point named by CFBundleExecutable)
cp "$PYI_ROOT/MRRC-FT710" "$APP_MACOS/MRRC-FT710"
# server onedir (ft710-server + _internal/) -> Contents/MacOS/
cp -R "$PYI_ROOT/ft710-server/." "$APP_MACOS/"
# scope_pipe onefile -> Contents/MacOS/
cp "$PYI_ROOT/scope_pipe" "$APP_MACOS/scope_pipe"

# runtime files the launcher reads from app_dir()
mkdir -p "$APP_MACOS/macos"
cp "$REPO_ROOT/macos/default.env" "$APP_MACOS/macos/default.env"
cp "$REPO_ROOT/mem_channels.json" "$APP_MACOS/mem_channels.json"

# optional FTDI dylibs
if [[ -d "$REPO_ROOT/vendor/ftdi/macos" ]]; then
  mkdir -p "$APP_MACOS/vendor/ftdi"
  cp -R "$REPO_ROOT/vendor/ftdi/macos" "$APP_MACOS/vendor/ftdi/macos"
fi

# strip stale bytecode caches
find "$APP_BUNDLE" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$APP_BUNDLE" -name "*.pyc" -delete 2>/dev/null || true

# ---- Step 5: ad-hoc codesign (no Developer ID -> Gatekeeper warns once) -
echo "==> Codesign (ad-hoc)"
# Sign leaves first, then the bundle root. We deliberately avoid --deep: it
# recurses into _internal and tries to sign .dist-info dirs as bundles, which
# fails ("bundle format unrecognized"). Ad-hoc signing without hardened runtime
# does not enforce library validation, so signing the shared libs + the main
# exes + the root is enough to avoid the "damaged" Gatekeeper message; users
# still right-click -> Open once (no Developer ID = "unidentified developer").
find "$APP_BUNDLE" -type f \( -name "*.dylib" -o -name "*.so" \) \
    -exec codesign --force --sign - {} + 2>/dev/null || true
for exe in "$APP_MACOS/MRRC-FT710" "$APP_MACOS/ft710-server" "$APP_MACOS/scope_pipe"; do
  [[ -f "$exe" ]] && codesign --force --sign - "$exe" 2>/dev/null || true
done
codesign --force --sign - "$APP_BUNDLE" 2>/dev/null \
  || echo "WARNING: ad-hoc codesign reported errors (non-fatal; right-click -> Open still works)." >&2
codesign --verify --verbose=2 "$APP_BUNDLE" 2>&1 || true

# ---- Step 6: .dmg -------------------------------------------------------
echo "==> DMG"
DMG="$DIST_ROOT/MRRC-FT710-${VERSION}-arm64.dmg"
rm -f "$DMG"
hdiutil create -volname "MRRC FT-710" -fs HFS+ -format UDZO \
    -srcfolder "$APP_BUNDLE" "$DMG"

# ---- Step 7: checksums (for the website download table) -----------------
echo
echo "==> Done"
echo "App:  $APP_BUNDLE"
echo "DMG:  $DMG"
echo "Size: $(du -h "$DMG" | cut -f1)"
echo -n "MD5:    "; md5 -q "$DMG"
echo -n "SHA256: "; shasum -a 256 "$DMG" | cut -d' ' -f1
