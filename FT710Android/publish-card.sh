#!/usr/bin/env bash
# 只更新线上 Android 下载卡片（en/zh 的 <!-- android-download --> 标记块 + hero 安卓按钮版本号）。
#
# 为什么这么做：站点页面同时承载 Windows/macOS 卡片，而全站 deploy.sh 会把另一条
# 发布波的内容回退（2026-10-05 实测）。本脚本抓**线上当前页面**，只在本地补 Android
# 相关行，再原样传回——除 Android 行外逐字节不变，不跑 deploy.sh、不改任何其他内容。
#
# 用法: ./publish-card.sh [--version X.Y.Z] [--dry-run]
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE_USER="cheenle"
REMOTE_HOST="www.vlsc.net"
REMOTE_ROOT="/var/www/vlsc.net/mrrc_modern"
BASE_URL="https://www.vlsc.net/mrrc_modern"

VERSION=""; DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1"; exit 2 ;;
  esac
done

if [ -z "$VERSION" ]; then
  VERSION=$(grep -oE 'versionName = "[0-9]+\.[0-9]+\.[0-9]+"' "$APP_DIR/app/build.gradle.kts" \
            | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
fi
[ -n "$VERSION" ] || { echo "cannot determine version"; exit 1; }

APK="$APP_DIR/dist/MRRC-Modern-v${VERSION}-Android.apk"
[ -f "$APK" ] || { echo "artifact missing: $APK (先跑 release.sh --apk-only)"; exit 1; }
SIZE=$(stat -f%z "$APK")
SHA=$(shasum -a 256 "$APK" | awk '{print $1}')

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
echo "== Android 卡片 v$VERSION · $(printf '%s' "$SIZE") bytes · $SHA =="
curl -fsS -o "$TMP/index.html" "$BASE_URL/index.html"
curl -fsS -o "$TMP/zh.html"    "$BASE_URL/zh/index.html"

python3 - "$TMP" "$VERSION" "$SIZE" "$SHA" <<'PY'
import re, sys
tmp, version, size, sha = sys.argv[1:5]
mb = f"{int(size) / 1048576:.1f} MB"

def patch(path, prefix, label, note):
    html = open(path, encoding="utf-8").read()
    block = (
        "<!-- android-download:start -->\n"
        f'<p><a class="btn btn-primary btn-large" href="{prefix}MRRC-Modern-Android.apk">'
        f"{label}</a></p>\n"
        f'<p style="color: var(--scope-text-muted); font-size: 0.85rem; margin-top: .5rem;">'
        f"MRRC-Modern-v{version}-Android.apk · {mb} · SHA-256 <code>{sha}</code><br>{note}</p>\n"
        "<!-- android-download:end -->"
    )
    pat = re.compile(r"<!-- android-download:start -->.*?<!-- android-download:end -->", re.S)
    if not pat.search(html):
        sys.exit(f"marker block not found in {path}")
    html = pat.sub(block, html)
    hero = re.compile(r'(<i class="fab fa-android"></i>\s*[^<]*?v)\d+\.\d+\.\d+')
    if not hero.search(html):
        sys.exit(f"hero android button not found in {path}")
    html = hero.sub(lambda m: m.group(1) + version, html)
    open(path, "w", encoding="utf-8").write(html)

patch(f"{tmp}/index.html", "downloads/", f"Download APK v{version} (Android 8.0+)",
      "Install: allow “install unknown apps”, then open the APK.")
patch(f"{tmp}/zh.html", "../downloads/", f"下载 APK v{version}（Android 8.0+）",
      "安装：系统设置允许「安装未知应用」后点开 APK。")
print("patched (只有 Android 行被改写)")
PY

if [ "$DRY_RUN" = 1 ]; then
  echo "-- dry-run: 不传服务器 --"; exit 0
fi

scp "$TMP/index.html" "$TMP/zh.html" "$REMOTE_USER@$REMOTE_HOST:~/"
ssh "$REMOTE_USER@$REMOTE_HOST" \
  "sudo mv ~/index.html $REMOTE_ROOT/index.html && sudo mv ~/zh.html $REMOTE_ROOT/zh/index.html && sudo chown www-data:www-data $REMOTE_ROOT/index.html $REMOTE_ROOT/zh/index.html && sudo chmod 644 $REMOTE_ROOT/index.html $REMOTE_ROOT/zh/index.html && sudo systemctl reload nginx"
# 为什么要 reload：nginx.conf 开了 open_file_cache（valid 60s / inactive 30s），mv 换文件后
# nginx 会继续用旧 inode 服务最长 60 秒（2026-10-05 实测：文件已是 v1.1.5，外网仍回 v1.1.4）。

# 复核：两页在线内容与补丁后逐字节一致；APK 线上 SHA 与卡片一致。
# 注意：公网链路可能存在短时缓存（2026-10-05 实测：刚上传后复查到 15:25 的旧响应）——
# 用时间戳查询串绕开缓存键，并重试。
fetch_fresh() {  # $1=url $2=输出文件
  local url="$1" out="$2" i
  for i in 1 2 3 4 5; do
    curl -fsS "${url}?verify=$(date +%s)-$i" -o "$out" || true
    grep -q "android-download" "$out" && return 0
    sleep 2
  done
  echo "fetch failed after retries: $url"; return 1
}

fetch_fresh "$BASE_URL/index.html" "$TMP/live-en.html"
fetch_fresh "$BASE_URL/zh/index.html" "$TMP/live-zh.html"
diff -q "$TMP/index.html" "$TMP/live-en.html" >/dev/null || { echo "en page mismatch"; exit 1; }
diff -q "$TMP/zh.html" "$TMP/live-zh.html" >/dev/null || { echo "zh page mismatch"; exit 1; }
LIVE_SHA=$(curl -fsS "$BASE_URL/downloads/MRRC-Modern-Android.apk?verify=$(date +%s)" | shasum -a 256 | awk '{print $1}')
[ "$LIVE_SHA" = "$SHA" ] || { echo "APK sha mismatch: card=$SHA live=$LIVE_SHA"; exit 1; }
echo "== Android 卡片 v$VERSION 已上线，且 APK 线上 SHA 与卡片一致（其他内容未动）=="
