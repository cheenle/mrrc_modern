#!/bin/sh
# 版本号的唯一读法。usage: version_of.sh <CHANGELOG.md>  →  X.Y.Z（无标题则退出 1）
#
# 镜像线标题优先（`## [镜像线 vX.Y.Z · FPn]`），否则桌面线（`## [vX.Y.Z]`）。
# 这不是偏好，是 2026-10-19 之后的事实：两条线各自编号，镜像线标题永远带
# `镜像线 ` 前缀，所以只认 `## [v…]` 的正则会一路滑到桌面线的最新一条 ——
# 2026-10-27 实测：当前 CHANGELOG 下它返回 1.28.6，而镜像线是 1.28.11。
#
# 规则与 dev_tools/make_box_source.py::version_of() 必须一字不差；
# tests/test_release_artifacts.py::VersionOfScriptTests 把两个实现的输出钉在一起。
#
# 三处调用：packaging/rpi/build-image.sh（宿主）、
# packaging/common/mrrc-service-layer.sh（pi-gen chroot / 盒子 chroot）、
# linux/mrrc_update.sh（设备）。所以是 CLI 而不是被 source 的函数。
set -u
file="${1:-}"
[ -n "$file" ] && [ -f "$file" ] || {
	echo "version_of: no such CHANGELOG: ${file:-<none>}" >&2
	exit 1
}
# 两条独立的正则，而不是一条合并的 sed："文件里第一条匹配"不能决定答案 ——
# 镜像线的标题在文件里可能排在若干桌面线条目之后。
v=$(grep -m1 -oE '## \[镜像线 v[0-9]+\.[0-9]+\.[0-9]+' "$file" |
	grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
[ -n "$v" ] || v=$(grep -m1 -oE '## \[v[0-9]+\.[0-9]+\.[0-9]+\]' "$file" |
	grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
[ -n "$v" ] || {
	echo "version_of: no '## [镜像线 vX.Y.Z' or '## [vX.Y.Z]' heading in $file" >&2
	exit 1
}
printf '%s\n' "$v"
