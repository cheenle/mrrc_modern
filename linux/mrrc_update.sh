#!/bin/bash -e
# Update MRRC Modern in place, without reflashing the image.
#
# Deliberately the smallest thing that works: fetch the tree, install whatever
# requirements.txt now needs, restart. There is no rollback, because there is no
# verified way to decide a rollback is warranted — upgrade_core.py's automatic
# path (slice 2) is explicitly not started, and inventing one here would be a
# second, untested state machine over the same files.
#
# The file list is not here either: the fetched tree carries
# packaging/common/stage-tree.sh, the one place the exclusion list lives, and
# this script calls it. Using the *fetched* copy is also the only version that is
# right — the release being installed is the one that knows what it ships.
#
# One deliberate behaviour change against the pre-2026-10-11 version: nothing is
# deleted any more. That version passed --delete to rsync, so a file dropped from
# the tree was also dropped on every box; de-duplicating the list does not get to
# smuggle that decision in with it. Removing stale files needs its own evaluation
# (design doc §4.4), so this updater only adds and overwrites for now.
set -euo pipefail

MRRC_HOME="${MRRC_HOME:-/opt/mrrc_modern}"
MRRC_USER="${MRRC_USER:-mrrc}"
SRC="${MRRC_UPDATE_SRC:-}" # git URL, or a local directory to rsync from
BRANCH="${MRRC_UPDATE_BRANCH:-main}"
CLONE=""

cleanup() {
	[ -n "$CLONE" ] && rm -rf "$CLONE"
	return 0
}
trap cleanup EXIT

[ -n "$SRC" ] || {
	echo "set MRRC_UPDATE_SRC to a git URL or a local directory" >&2
	exit 1
}

if [ -d "$SRC" ]; then
	# `cd … && pwd` rather than readlink -f: this runs on the device, the path
	# has to be absolute for rsync's root-anchored excludes to mean anything,
	# and a relative MRRC_UPDATE_SRC is the natural thing for an operator to
	# type when they are already standing in the tree.
	SRC_TREE="$(cd "$SRC" && pwd)"
	echo "==> updating from the local tree $SRC_TREE"
else
	echo "==> cloning $SRC ($BRANCH)"
	CLONE="$(mktemp -d)"
	SRC_TREE="$CLONE/tree"
	# The previous version ran `git -C "$MRRC_HOME" fetch`, and /opt/mrrc_modern
	# is not a git checkout — that branch could never have worked.
	git clone --depth 1 --branch "$BRANCH" "$SRC" "$SRC_TREE"
fi

STAGER="$SRC_TREE/packaging/common/stage-tree.sh"
[ -f "$STAGER" ] || {
	echo "mrrc-update: $SRC_TREE has no packaging/common/stage-tree.sh." >&2
	echo "That tree predates the 2026-10-11 packaging change, so this updater" >&2
	echo "cannot know which files it is supposed to install. Use $BRANCH or later." >&2
	exit 1
}

bash "$STAGER" "$SRC_TREE" "$MRRC_HOME"

echo "==> python dependencies"
"$MRRC_HOME/venv/bin/pip" install --upgrade -q -r "$MRRC_HOME/requirements.txt"

echo "==> FT8 DSP library (rebuild only when ft8/dsp changed)"
# ft8/dsp/build/ is a build product and excluded from the staged tree, so an
# update that changes the DSP sources would otherwise pair the new Python with
# the previous .so.  The ABI is hard-checked at load (an old library refuses to
# start, loudly), but anything short of an ABI bump would be a silent mismatch
# — and "the box silently runs two versions of one decoder" is exactly the
# failure docs/ft8-w103d-decode-perf-analysis.md §5 warned about.  build.sh
# stores a digest of its sources and returns immediately when nothing moved,
# so the common case costs milliseconds.
#
# Deliberately NOT fatal: images released before 1.27.0 have no compiler, and
# an update must not leave a working SSB box half-updated because its FT8
# toolchain is missing.  A real failure is reported and FT8 alone degrades.
echo "==> version stamp"
# 版本号必须跟着树走。它不写的话，热升级后的盒子会一直声称自己跑着旧版本 —— 诊断包
# 的 manifest、`/api/update/check` 的“当前版本”与 ☰ 菜单页脚读的都是这个文件，
# 于是「升级了但行为没变」这类报告根本无从分辨（2026-10-17 实测：1.25.5 的树被升到
# 1.28.1 之后，version.txt 仍然写着 1.25.5）。
#
# 取值与镜像构建同一条规则：CHANGELOG 顶部的第一个 `## [vX.Y.Z]`。它是 `[Unreleased]`
# 之后的第一条，也就是即将发/刚发的那个版本号。
# 三个来源，按可信度排：显式给的值 → 源树的 CHANGELOG → 源树的 version.txt。
# 第二个在**喂一棵已 stage 过的树**时不存在（`stage-tree.sh` 的 `/*.md` 排除），
# 而那是把本地目录当 MRRC_UPDATE_SRC 时最自然的做法 —— 2026-10-17 就这么吃过一次：
# 升级成功，版本号仍写 1.25.5。
if [ -n "${MRRC_UPDATE_VERSION:-}" ]; then
	version="$MRRC_UPDATE_VERSION"
elif [ -f "$SRC_TREE/CHANGELOG.md" ]; then
	version="$(grep -m1 -oE '## \[v[0-9]+\.[0-9]+\.[0-9]+\]' "$SRC_TREE/CHANGELOG.md" \
		2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)"
elif [ -s "$SRC_TREE/version.txt" ]; then
	version="$(head -1 "$SRC_TREE/version.txt" | tr -d '[:space:]')"
else
	version=""
fi
if [ -n "$version" ]; then
	printf '%s\n' "$version" >"$MRRC_HOME/version.txt"
	cp "$MRRC_HOME/version.txt" "$MRRC_HOME/VERSION"
	chown "$MRRC_USER:$MRRC_USER" "$MRRC_HOME/version.txt" "$MRRC_HOME/VERSION"
	echo "    $version"
else
	echo "WARNING: 源树里没有 CHANGELOG.md 的版本行；version.txt 仍是" \
		"$(cat "$MRRC_HOME/version.txt" 2>/dev/null || echo '?')，升级后的盒子会报错版本" >&2
fi

if ! bash "$MRRC_HOME/ft8/dsp/build.sh"; then
	echo "WARNING: the FT8 DSP library did not rebuild; FT8 mode will report" >&2
	echo "         a missing library until it is built. Everything else is fine." >&2
fi

chown -R "$MRRC_USER:$MRRC_USER" "$MRRC_HOME"
echo "==> restarting"
systemctl restart mrrc-modern
systemctl --no-pager --lines=5 status mrrc-modern || true
echo "==> done; env file left untouched at $MRRC_HOME/env/mrrc.env"
