#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# MRRC Modern — restart（macOS / 开发机）
# ═══════════════════════════════════════════════════════════════════════
# 源码运行场景：停 → 起（stop.sh + start.sh，参数原样透传，例如 --foreground）。
# Linux 盒子（systemd 管理）请用 restart_linux.sh。
#
# 本脚本会在检测到 systemd 单元 mrrc-modern.service 时**拒绝执行** ——
# 在盒子上用 stop.sh/start.sh 会与 systemd 打架并以 root 起一份读不到
# TLS 私钥的副本（事故：2026-10-11 21:07 掉线）。
#
# 用法：
#   ./restart_mac.sh                停止 → 启动 → 检查 Web 是否在应答
#   ./restart_mac.sh --foreground   前台运行（其余参数透传给 start.sh）
#   PORT=8443 ./restart_mac.sh      只影响健康检查用的端口
#
# 退出码：0=已启动并在应答 · 2=进程在跑但 Web 无应答 · 1=启动失败
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

WAIT_WEB=${WAIT_WEB:-20}

if [ -t 1 ]; then
	C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_BAD=$'\033[31m'; C_END=$'\033[0m'
else
	C_OK=''; C_WARN=''; C_BAD=''; C_END=''
fi
say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_END" "$*"; }
warn() { printf '  %s⚠%s %s\n' "$C_WARN" "$C_END" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$C_BAD" "$C_END" "$*"; }

# ── 守卫：Linux 盒子上必须走 systemd ───────────────────────────────────
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files mrrc-modern.service >/dev/null 2>&1; then
	bad "本机由 systemd 管理（mrrc-modern.service）：请用 ./restart_linux.sh"
	say "  stop.sh/start.sh 是开发机脚本；在盒子上直接跑会让服务与 systemd 冲突。"
	exit 1
fi

env_val() {   # 依次找 仓库 .env → macOS 应用 env；不 source（值里可能含空格）
	local f
	for f in "$ROOT_DIR/.env" "$HOME/Library/Application Support/MRRC-Modern/mrrc_modern.env"; do
		[ -r "$f" ] || continue
		local v
		v=$(sed -n "s/^[[:space:]]*$1=//p" "$f" | tail -1 | sed -e 's/^"//' -e 's/"$//')
		if [ -n "$v" ]; then printf '%s' "$v"; return 0; fi
	done
	return 0
}

web_code() {
	local c
	c=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 3 "https://127.0.0.1:$PORT/login" 2>/dev/null || echo 000)
	if [ "$c" = "000" ]; then
		c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT/login" 2>/dev/null || echo 000)
	fi
	printf '%s' "$c"
}

served_cn() {
	echo | openssl s_client -connect "127.0.0.1:$PORT" 2>/dev/null \
		| openssl x509 -noout -subject 2>/dev/null \
		| sed -n 's/.*CN *= *//p' | head -1
}

PORT=${PORT:-$(env_val MRRC_WEB_PORT)}
PORT=${PORT:-8888}
LABEL=$(env_val MRRC_CLOUD_LABEL)
EXPECT_CN="${LABEL:+$LABEL.mrrc.vlsc.net}"

# 前台运行没有"起来后再检查"这一步，直接交给 start.sh
for arg in "$@"; do
	if [ "$arg" = "--foreground" ] || [ "$arg" = "-f" ]; then
		./stop.sh
		exec ./start.sh "$@"
	fi
done

say "▶ stop.sh"
./stop.sh
say "▶ start.sh $*"
./start.sh "$@"

code=000
deadline=$((SECONDS + WAIT_WEB))
while [ "$SECONDS" -lt "$deadline" ]; do
	code=$(web_code)
	[ "$code" != "000" ] && break
	sleep 1
done

say ""
say "重启后状态："
if [ -f "$ROOT_DIR/.ft710-server.pid" ]; then
	say "  进程      : pid=$(cat "$ROOT_DIR/.ft710-server.pid")  已运行 $(ps -o etime= -p "$(cat "$ROOT_DIR/.ft710-server.pid")" 2>/dev/null | tr -d ' ' || echo '?')"
fi
say "  Web       : https://127.0.0.1:$PORT/login → $code"
cn=$(served_cn || true)
if [ -n "$cn" ]; then
	if [ -n "$EXPECT_CN" ] && [ "$cn" != "$EXPECT_CN" ]; then
		warn "证书 CN=$cn，与 Cloud Hub 标签期望的 $EXPECT_CN 不一致（hub 会 502；重启进程即可重载证书）"
	else
		ok "证书 CN   : $cn"
	fi
fi
say "  日志      : $ROOT_DIR/logs/ft710-server.log"

if [ "$code" = "000" ]; then
	say ""
	bad "${WAIT_WEB}s 内 Web 无应答（端口 $PORT）"
	say "  排查：tail -40 $ROOT_DIR/logs/ft710-server.log"
	exit 2
fi
say ""
ok "重启完成，Web 在应答"
