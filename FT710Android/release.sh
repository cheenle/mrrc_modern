#!/usr/bin/env bash
# MRRC Modern Android 发布脚本：构建 → 签名核对 → 站点上传 → 线上 SHA-256 复核
# 用法: ./release.sh [--version X.Y.Z] [--dry-run] [--skip-tests]
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
WEBSITE_DIR="${WEBSITE_DIR:-$HOME/HAM/website/mrrc_modern}"
REMOTE_USER="cheenle"
REMOTE_HOST="www.vlsc.net"
REMOTE_DOWNLOADS="/var/www/vlsc.net/mrrc_modern/downloads"

VERSION=""; DRY_RUN=0; SKIP_TESTS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-tests) SKIP_TESTS=1; shift ;;
    *) echo "unknown arg: $1"; exit 2 ;;
  esac
done

: "${JAVA_HOME:=$(/usr/libexec/java_home -v 17)}"; export JAVA_HOME
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
[ -d "$ANDROID_HOME" ] || { echo "ANDROID_HOME not found"; exit 1; }
[ -f "$APP_DIR/keystore.properties" ] || { echo "keystore.properties missing (BUILD_GUIDE.md)"; exit 1; }
[ -d "$WEBSITE_DIR" ] || { echo "website dir not found: $WEBSITE_DIR"; exit 1; }

if [ -z "$VERSION" ]; then
  VERSION=$(grep -oE 'versionName = "[0-9]+\.[0-9]+\.[0-9]+"' "$APP_DIR/app/build.gradle.kts" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
fi
[ -n "$VERSION" ] || { echo "cannot determine version"; exit 1; }
echo "== MRRC Modern Android v$VERSION =="

cd "$APP_DIR"
if [ "$SKIP_TESTS" = 1 ]; then ./gradlew lintDebug assembleRelease; else ./gradlew test lintDebug assembleRelease; fi

APK="$APP_DIR/app/build/outputs/apk/release/app-release.apk"
[ -f "$APK" ] || { echo "APK missing: $APK"; exit 1; }

APKSIGNER="$ANDROID_HOME/build-tools/35.0.0/apksigner"
[ -x "$APKSIGNER" ] || APKSIGNER=$(find "$ANDROID_HOME/build-tools" -name apksigner | sort | tail -1)
echo "-- signature --"; "$APKSIGNER" verify --print-certs "$APK" | head -6

VERSIONED="MRRC-Modern-v${VERSION}-Android.apk"
STABLE="MRRC-Modern-Android.apk"
mkdir -p "$APP_DIR/dist"
cp "$APK" "$APP_DIR/dist/$VERSIONED"
cp "$APK" "$APP_DIR/dist/$STABLE"
SIZE=$(stat -f%z "$APP_DIR/dist/$VERSIONED")
SHA=$(shasum -a 256 "$APP_DIR/dist/$VERSIONED" | awk '{print $1}')
echo "-- artifact: $VERSIONED · $SIZE bytes · $SHA --"

mkdir -p "$WEBSITE_DIR/downloads"
cp "$APP_DIR/dist/$VERSIONED" "$WEBSITE_DIR/downloads/$VERSIONED"
cp "$APP_DIR/dist/$STABLE" "$WEBSITE_DIR/downloads/$STABLE"

update_block() {  # $1=页面文件 $2=下载链接前缀 $3=语言(zh|en)
  python3 - "$1" "$2" "$3" "$VERSION" "$SIZE" "$SHA" <<'PY'
import re, sys
page, prefix, lang, version, size, sha = sys.argv[1:7]
html = open(page, encoding="utf-8").read()
mb = f"{int(size) / 1048576:.1f} MB"
if lang == "zh":
    label = f"下载 APK v{version}（Android 8.0+）"
    note = (f"MRRC-Modern-v{version}-Android.apk · {mb} · SHA-256 <code>{sha}</code><br>"
            "安装：系统设置允许「安装未知应用」后点开 APK。")
else:
    label = f"Download APK v{version} (Android 8.0+)"
    note = (f"MRRC-Modern-v{version}-Android.apk · {mb} · SHA-256 <code>{sha}</code><br>"
            "Install: allow “install unknown apps”, then open the APK.")
block = (
    "<!-- android-download:start -->\n"
    f'<p><a class="btn btn-primary btn-large" href="{prefix}MRRC-Modern-Android.apk">'
    f"{label}</a></p>\n"
    f'<p style="color: var(--scope-text-muted); font-size: 0.85rem; margin-top: .5rem;">'
    f"{note}</p>\n"
    "<!-- android-download:end -->"
)
pattern = re.compile(r"<!-- android-download:start -->.*?<!-- android-download:end -->", re.S)
if not pattern.search(html):
    sys.exit(f"marker block not found in {page}")
open(page, "w", encoding="utf-8").write(pattern.sub(block, html))
print(f"updated {page}")
PY
}
update_block "$WEBSITE_DIR/zh/index.html" "../downloads/" zh
update_block "$WEBSITE_DIR/index.html" "downloads/" en

if git -C "$WEBSITE_DIR" diff --quiet -- zh/index.html index.html; then :; else
  git -C "$WEBSITE_DIR" add zh/index.html index.html
  git -C "$WEBSITE_DIR" commit -m "site(mrrc_modern): Android APK v$VERSION download card"
fi

if [ "$DRY_RUN" = 1 ]; then echo "-- dry run: 跳过上传 / 部署 / 线上复核 --"; exit 0; fi

scp "$APP_DIR/dist/$VERSIONED" "$APP_DIR/dist/$STABLE" "$REMOTE_USER@$REMOTE_HOST:~/"
ssh "$REMOTE_USER@$REMOTE_HOST" "sudo mv ~/$VERSIONED ~/$STABLE $REMOTE_DOWNLOADS/ && sudo chown www-data:www-data $REMOTE_DOWNLOADS/$VERSIONED $REMOTE_DOWNLOADS/$STABLE && sudo chmod 644 $REMOTE_DOWNLOADS/$VERSIONED $REMOTE_DOWNLOADS/$STABLE"
printf 'y\n' | "$WEBSITE_DIR/deploy.sh"

REMOTE_SHA=$(ssh "$REMOTE_USER@$REMOTE_HOST" "sha256sum $REMOTE_DOWNLOADS/$VERSIONED" | awk '{print $1}')
[ "$REMOTE_SHA" = "$SHA" ] || { echo "SHA mismatch: local=$SHA remote=$REMOTE_SHA"; exit 1; }
curl -fsSI "https://$REMOTE_HOST/mrrc_modern/downloads/$VERSIONED" | head -3
TMP=$(mktemp -d)/"$VERSIONED"
curl -fsS -o "$TMP" "https://$REMOTE_HOST/mrrc_modern/downloads/$VERSIONED"
[ "$(shasum -a 256 "$TMP" | awk '{print $1}')" = "$SHA" ] || { echo "downloaded file hash mismatch"; exit 1; }
echo "== v$VERSION published & verified =="
