#!/bin/bash
# Put /etc/resolv.conf back on the systemd-resolved stub symlink.
#
# Why this exists: `packaging/box/build-image.sh` hands the chroot the *build
# container's* /etc/resolv.conf so apt/pip can resolve while the image is being
# assembled. The base image ships `/etc/resolv.conf -> ../run/systemd/resolve/
# stub-resolv.conf` instead, and that symlink is what makes systemd-resolved the
# resolver glibc actually talks to (NetworkManager feeds resolved; resolved writes
# the stub). A box built before 2026-10-25 keeps the copy, so it ships with a
# *build machine* address baked in.
#
# Measured 2026-10-25 on a delivered box (MRRC-BG1SB, image built 2026-10-11):
#
#   -rw-r--r-- 1 root root 97 10月11日 11:58 /etc/resolv.conf
#   # DNS requests are forwarded to the host. DHCP DNS options are ignored.
#   nameserver 192.168.65.7          <- Docker Desktop's internal DNS
#
# 192.168.65.7 exists on no customer LAN. glibc sends every lookup there, waits
# its 5 s timeout and fails: `ping <ip>` works, `ping <name>` does not, and NTP,
# the portal, the update check and the support upload all fail for the same
# reason. `resolvectl status` reports `resolv.conf mode: foreign` - resolved is
# running and already has the right servers (from DHCP), it simply is not the
# file glibc reads.
#
# Safe to run any number of times, and safe to run on a box that is already
# correct: a symlink is left alone, and the original file is backed up once.
#
#   sudo bash /opt/mrrc_modern/linux/fix_resolv_conf.sh
#   sudo bash ... --dry-run        # 只说会做什么
#
# Called by linux/mrrc_update.sh, which runs as root on every in-place upgrade -
# that is how boxes already in the field get the repair without a reflash.
set -uo pipefail

CONF="${MRRC_RESOLV_CONF:-/etc/resolv.conf}"
STUB="${MRRC_RESOLV_STUB:-/run/systemd/resolve/stub-resolv.conf}"
BACKUP="${MRRC_RESOLV_BACKUP:-$CONF.mrrc-bak}"
#: The unit file, not the socket: /run only exists while the daemon runs, and a
#: box mid-upgrade may well be in a state where it has not started yet.
RESOLVED_UNIT="${MRRC_RESOLVED_UNIT:-}"
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1
#: 重启 resolved 会短暂影响本机解析；测试与容器里用 0 关掉（见
#: tests/test_fix_resolv_conf.py）。真实盒子上必须开着，否则全局里那条
#: 死地址还在。
RESTART="${MRRC_RESOLV_RESTART:-1}"

if [ -z "$RESOLVED_UNIT" ]; then
	for candidate in /usr/lib/systemd/systemd-resolved /lib/systemd/systemd-resolved; do
		[ -e "$candidate" ] && RESOLVED_UNIT="$candidate" && break
	done
fi

# No systemd-resolved means this box resolves some other way (a static file IS
# its configuration, or another agent writes it). Nothing to repair, and
# replacing it would break DNS outright.
if [ -z "$RESOLVED_UNIT" ] || [ ! -e "$RESOLVED_UNIT" ]; then
	echo "fix_resolv_conf: no systemd-resolved here — $CONF left alone"
	exit 0
fi

# ── 这台机器用不用 systemd-resolved ──────────────────────────────────────
# /run 是 tmpfs，stub 只在 resolved 真跑着时才存在，所以"stub 不在"同时包含两种
# 完全不同的机器：resolved 装了但没起来（真问题），与这台机器根本不走 resolved
# （树莓派：NetworkManager 直接写 /etc/resolv.conf，pi-gen 的导出阶段还写死过
# nameserver 8.8.8.8）。把后者判成坏的，就是让一台 DNS 正常的机器去修 DNS。
# MRRC_RESOLVED_STATE=active|enabled|absent|unknown 供测试注入（spec 2026-10-27 §4.2）。
STATE="${MRRC_RESOLVED_STATE:-}"
if [ -z "$STATE" ]; then
	if ! command -v systemctl >/dev/null 2>&1; then
		STATE=unknown
	elif systemctl is-active --quiet systemd-resolved 2>/dev/null; then
		STATE=active
	elif systemctl is-enabled --quiet systemd-resolved 2>/dev/null; then
		STATE=enabled
	else
		STATE=absent
	fi
fi

if [ -L "$CONF" ]; then
	echo "fix_resolv_conf: $CONF is already a symlink ($(readlink "$CONF")) — nothing to do"
	exit 0
fi

if [ ! -f "$CONF" ]; then
	echo "fix_resolv_conf: $CONF is missing — leaving it to the resolver"
	exit 0
fi

if [ ! -e "$STUB" ]; then
	if [ "$STATE" = "absent" ]; then
		echo "fix_resolv_conf: 这台机器不用 systemd-resolved（$STUB 不存在，且未启用）"
		echo "fix_resolv_conf: $CONF 由 NetworkManager 管，无需修复"
		exit 0
	fi
	echo "fix_resolv_conf: $STUB does not exist (resolved: $STATE) — $CONF left alone" >&2
	exit 1
fi

squashed="$(tr '\n' ' ' <"$CONF" | tr -s ' ')"
echo "fix_resolv_conf: $CONF is a static file: $squashed"
if [ "$DRY_RUN" = "1" ]; then
	echo "fix_resolv_conf: would back it up to $BACKUP and point $CONF at $STUB"
	exit 0
fi

if [ ! -e "$BACKUP" ]; then
	cp -a "$CONF" "$BACKUP" || {
		echo "fix_resolv_conf: could not write $BACKUP — not touching $CONF" >&2
		exit 1
	}
	echo "fix_resolv_conf: backup written to $BACKUP"
fi

# Relative, the way the distribution ships it: an absolute link is identical in
# effect but shows up as a diff in every `dpkg -V`-style check.
ln -sfn ../run/systemd/resolve/stub-resolv.conf "$CONF" || {
	echo "fix_resolv_conf: could not replace $CONF" >&2
	exit 1
}
echo "fix_resolv_conf: $CONF -> $(readlink "$CONF")"

# Resolved imported the *old* file's nameservers into its **global** list while it
# sat in "foreign" mode, and pointing resolv.conf at the stub does not clear them:
# measured 2026-10-25 on MRRC-BG1SB right after the symlink switch -
# `resolvectl status` still said `Global: 192.168.65.7` while `wlan0` had the
# router. A global server is consulted for queries the link scope does not claim,
# so the dead address has to go. Only a restart re-reads the file; flush-caches
# just drops its answer cache.
if [ "$RESTART" != "0" ] && command -v systemctl >/dev/null 2>&1 &&
	command -v resolvectl >/dev/null 2>&1; then
	resolvectl flush-caches >/dev/null 2>&1 || true
	if systemctl is-active --quiet systemd-resolved 2>/dev/null; then
		echo "fix_resolv_conf: restarting systemd-resolved so the old global server is dropped"
		systemctl try-restart systemd-resolved >/dev/null 2>&1 || true
		sleep 1
	fi
fi

echo "fix_resolv_conf: done — check with: resolvectl status | head -3; getent hosts portal.mrrc.vlsc.net"
