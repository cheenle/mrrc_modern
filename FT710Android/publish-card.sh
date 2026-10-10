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
# 站点树（deploy.sh 的取源）。~/HAM/website/mrrc_modern 是**指向仓库 website/ 的符号链接**，
# 所以它里面的 index.html 就是 git 跟踪的那一份。
WEBSITE_DIR="${WEBSITE_DIR:-$HOME/HAM/website/mrrc_modern}"

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

# ── 0) 稳定别名自愈 ────────────────────────────────────────────────────
# 站点树与 Windows/macOS 发布波共用：对方的全站部署会**删掉** MRRC-Modern-Android.apk
# （APK 在 .gitignore 里，不在他们的 checkout 中），并把页面卡片打回他们那份的旧版本号。
# 2026-10-07 23:39 实测发生过：下载链接变 404、卡片回到 v1.1.17，而版本化 APK 仍在。
# 所以每次改卡片前先确认别名在且内容正确，缺了就从服务器上的版本化文件复制回来
# （比重传 9MB 快，且保住已公布过的 SHA）。
REMOTE_DL="$REMOTE_ROOT/downloads"
VERSIONED="MRRC-Modern-v${VERSION}-Android.apk"
STABLE="MRRC-Modern-Android.apk"
ALIAS_SHA=$(ssh -o ConnectTimeout=10 "$REMOTE_USER@$REMOTE_HOST" \
  "sha256sum $REMOTE_DL/$STABLE 2>/dev/null | awk '{print \$1}'" 2>/dev/null || true)
if [ "$ALIAS_SHA" != "$SHA" ]; then
  echo "-- 稳定别名缺失或不符（线上 ${ALIAS_SHA:-无}，应为 ${SHA:0:16}…）→ 修复"
  VER_SHA=$(ssh -o ConnectTimeout=10 "$REMOTE_USER@$REMOTE_HOST" \
    "sha256sum $REMOTE_DL/$VERSIONED 2>/dev/null | awk '{print \$1}'" 2>/dev/null || true)
  if [ "$VER_SHA" = "$SHA" ]; then
    ssh "$REMOTE_USER@$REMOTE_HOST" \
      "cd $REMOTE_DL && sudo cp -p $VERSIONED $STABLE && sudo chown www-data:www-data $STABLE && sudo chmod 644 $STABLE"
    echo "   已从服务器上的 $VERSIONED 复制回别名"
  else
    # 版本化文件也不在或不对 → 从本地 dist 重传两份（release.sh 会在 dist/ 里同时生成两个名字）
    [ -f "$APP_DIR/dist/$STABLE" ] || cp "$APK" "$APP_DIR/dist/$STABLE"
    scp "$APK" "$APP_DIR/dist/$STABLE" "$REMOTE_USER@$REMOTE_HOST:~/"
    ssh "$REMOTE_USER@$REMOTE_HOST" \
      "sudo mv ~/$VERSIONED ~/$STABLE $REMOTE_DL/ && sudo chown www-data:www-data $REMOTE_DL/$VERSIONED $REMOTE_DL/$STABLE && sudo chmod 644 $REMOTE_DL/$VERSIONED $REMOTE_DL/$STABLE"
    echo "   已从本地 dist 重新上传（版本化 + 别名）"
  fi
fi

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

# ── 3) 回写仓库里的卡片（否则地雷会在下一次发布时重新埋上）────────────
# 本脚本原本只补**线上**页面（故意的：避开与 Windows/macOS 发布波共用站点树的冲突），
# 但站点树就是仓库的 website/ ⇒ 仓库里的 Android 卡片永远停在上一次有人手写的那个版本。
# deploy.sh 的 tar 是 --overwrite 且 HTML 在包里，所以**任何一次全站部署都会把线上
# 卡片打回仓库里那个旧版本**（2026-10-07 23:39 实测：卡片回到 v1.1.17）。
# 所以线上核完就把仓库这一半补齐，让下一次 deploy 对 Android 变成无操作。
# 用与上面 patch() 逐字相同的块构造，所以两边收敛到字节一致。
if [ -f "$WEBSITE_DIR/index.html" ] && [ -f "$WEBSITE_DIR/zh/index.html" ]; then
  python3 - "$WEBSITE_DIR" "$VERSION" "$SIZE" "$SHA" <<'PY'
import re, sys
site, version, size, sha = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
mb = f"{size / 1048576:.1f} MB"

def patch(path, prefix, label, install):
    note = (f"MRRC-Modern-v{version}-Android.apk · {mb} · SHA-256 <code>{sha}</code><br>{install}")
    block = (
        "<!-- android-download:start -->\n"
        f'<p><a class="btn btn-primary btn-large" href="{prefix}MRRC-Modern-Android.apk">'
        f"{label}</a></p>\n"
        f'<p style="color: var(--scope-text-muted); font-size: 0.85rem; margin-top: .5rem;">'
        f"{note}</p>\n"
        "<!-- android-download:end -->"
    )
    html = open(path, encoding="utf-8").read()
    pat = re.compile(r"<!-- android-download:start -->.*?<!-- android-download:end -->", re.S)
    if not pat.search(html):
        sys.exit(f"marker block not found in {path}")
    html = pat.sub(lambda _: block, html)   # lambda：不让 re 解释 SHA/文本里的反斜杠
    hero = re.compile(r'(<i class="fab fa-android"></i>\s*[^<]*?v)\d+\.\d+\.\d+')
    if not hero.search(html):
        sys.exit(f"hero android button not found in {path}")
    html = hero.sub(lambda m: m.group(1) + version, html)
    open(path, "w", encoding="utf-8").write(html)
    print(f"   repo synced: {path}")

patch(f"{site}/index.html", "downloads/", f"Download APK v{version} (Android 8.0+)",
      "Install: allow \u201cinstall unknown apps\u201d, then open the APK.")
patch(f"{site}/zh/index.html", "../downloads/", f"\u4e0b\u8f7d APK v{version}\uff08Android 8.0+\uff09",
      "\u5b89\u88c5\uff1a\u7cfb\u7edf\u8bbe\u7f6e\u5141\u8bb8\u300c\u5b89\u88c5\u672a\u77e5\u5e94\u7528\u300d\u540e\u70b9\u5f00 APK\u3002")
PY
  echo "-- 仓库里的两页 Android 卡片已追平 v${VERSION}（记得在主仓提交这两页；"
  echo "   不提交也不会影响线上，但下次从干净 checkout 部署就又会降级）--"
else
  echo "-- 找不到站点树 ${WEBSITE_DIR}，跳过仓库回写（线上已正确，不影响下载）--"
fi
