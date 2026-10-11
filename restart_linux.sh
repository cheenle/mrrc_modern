#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# MRRC Modern — restart（Linux 盒子 / systemd）
# ═══════════════════════════════════════════════════════════════════════
# 只做一件事：让 systemd 重启 mrrc-modern，并**验证**重启后真的可用
# （Web / 证书 / 隧道 frpc）。macOS 与开发机请用 restart_mac.sh。
#
# 为什么要有这个文件：原先的 restart.sh 是 stop.sh + start.sh 的薄壳
# （macOS 开发机时代的脚本）。在 systemd 管理的盒子上那对脚本会打架：
#   · stop.sh 干净地杀掉单元进程（systemd 认为是一次正常停止，不拉起）；
#   · start.sh 随后以 **root** 起一份 nohup 副本 —— 读的是 macOS 时代的
#     .env（日志里在开 /dev/cu.usbserial-*）、读不到 mrrc:mrrc 0600 的
#     TLS 私钥，还把 logs/ 与 __pycache__/ 写成 root 属主（令真正的服务
#     再也写不进去）；
#   · 副本一退，盒子就一直下线。
#   （事故：2026-10-11 21:07 掉线，靠 systemctl start 才恢复。）
#
# 用法：
#   ./restart_linux.sh              重启 + 等健康（推荐）
#   ./restart_linux.sh status       只看状态，不重启
#   ./restart_linux.sh start|stop   仅控制单元，不做健康等待
#   ./restart_linux.sh --no-wait    重启但不等待
#   ./restart_linux.sh help         显示本说明
#
# 退出码：0=健康 · 2=Web 可用但隧道 frpc 没起来（公网入口会 502）· 1=失败/不健康
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/mrrc_modern}
UNIT=${UNIT:-mrrc-modern}
ENV_FILE="$APP_DIR/env/mrrc.env"
WAIT_WEB=${WAIT_WEB:-30}
WAIT_TUNNEL=${WAIT_TUNNEL:-25}

if [ -t 1 ]; then
	C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_BAD=$'\033[31m'; C_END=$'\033[0m'
else
	C_OK=''; C_WARN=''; C_BAD=''; C_END=''
fi
say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_END" "$*"; }
warn() { printf '  %s⚠%s %s\n' "$C_WARN" "$C_END" "$*"; }
bad()  { printf '  %s✗%s %s\n' "$C_BAD" "$C_END" "$*"; }

env_val() {   # 不 source（值里含空格等特殊字符，见 docs 里的 "USB Audio Device"）
	[ -r "$ENV_FILE" ] || return 0
	sed -n "s/^[[:space:]]*$1=//p" "$ENV_FILE" | tail -1 | sed -e 's/^"//' -e 's/"$//'
}

# ── 平台 / 单元前置检查 ────────────────────────────────────────────────
if [ "$(uname -s)" != "Linux" ]; then
	bad "本脚本只用于 Linux（当前平台 $(uname -s)）：macOS 与开发机请用 ./restart_mac.sh"
	exit 1
fi
if ! command -v systemctl >/dev/null 2>&1 || ! systemctl list-unit-files "$UNIT.service" >/dev/null 2>&1; then
	bad "没有 systemd 单元 $UNIT.service：这台机器不是盒子（开发机）→ 请用 ./restart_mac.sh"
	exit 1
fi

need_root() {
	[ "$(id -u)" -eq 0 ] && return 0
	if command -v sudo >/dev/null 2>&1; then
		exec sudo -- "$(readlink -f "${BASH_SOURCE[0]}")" "$@"
	fi
	bad "需要 root 权限（systemctl 与读取 $ENV_FILE）"
	exit 1
}

web_code() {
	local c
	c=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 3 "https://127.0.0.1:$PORT/login" 2>/dev/null || echo 000)
	if [ "$c" = "000" ]; then
		c=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$PORT/login" 2>/dev/null || echo 000)
	fi
	printf '%s' "$c"
}

# 200/302=正常 · 401/403=需要登录（也算是"在服务"）· 5xx/000=不健康
web_ok() {
	case "$1" in 200 | 302 | 401 | 403) return 0 ;; *) return 1 ;; esac
}

served_cn() {
	echo | openssl s_client -connect "127.0.0.1:$PORT" 2>/dev/null \
		| openssl x509 -noout -subject 2>/dev/null \
		| sed -n 's/.*CN *= *//p' | head -1
}

tunnel_pid() { pgrep -x frpc 2>/dev/null | head -1 || true; }

# 释放 systemd 之外的 server.py 残留（root 手工启动的副本会抢 Web 端口与 CAT 串口）
release_rogue_servers() {
	local mainpid pids p comm
	mainpid=$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null || echo 0)
	pids=$(pgrep -f 'server\.py' 2>/dev/null || true)
	for p in $pids; do
		[ "$p" = "$mainpid" ] && continue
		comm=$(ps -o comm= -p "$p" 2>/dev/null | tr -d ' ' || true)
		case "$comm" in python | python3 | python3.*) ;; *) continue ;; esac
		warn "发现 systemd 之外的 server.py 残留 pid=$p（用户 $(ps -o user= -p "$p" 2>/dev/null | tr -d ' ')）→ 停止它"
		kill "$p" 2>/dev/null || true
		sleep 1
		kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null || true
	done
}

print_status() {
	local mp cn tp
	say "  单元      : $UNIT = $(systemctl is-active "$UNIT") / $(systemctl show -p SubState --value "$UNIT")"
	mp=$(systemctl show -p MainPID --value "$UNIT" 2>/dev/null || echo 0)
	if [ "$mp" != "0" ]; then
		say "  主进程    : pid=$mp  已运行 $(ps -o etime= -p "$mp" 2>/dev/null | tr -d ' ')"
	fi
	say "  Web       : https://127.0.0.1:$PORT/login → $(web_code)"
	cn=$(served_cn || true)
	if [ -n "$cn" ]; then
		if [ -n "$EXPECT_CN" ] && [ "$cn" != "$EXPECT_CN" ]; then
			warn "证书 CN=$cn，与 Cloud Hub 标签期望的 $EXPECT_CN 不一致（hub 会 502；重启本服务即可重载证书）"
		else
			ok "证书 CN   : $cn"
		fi
	else
		warn "证书      : 取不到（Web 未监听 TLS？）"
	fi
	tp=$(tunnel_pid)
	if [ -n "$tp" ]; then
		ok "隧道 frpc : pid=$tp  已运行 $(ps -o etime= -p "$tp" 2>/dev/null | tr -d ' ')${LABEL:+（label=$LABEL）}"
	elif [ -n "$LABEL" ]; then
		warn "隧道 frpc : 未运行（label=$LABEL → 公网入口不可用）"
	else
		say "  隧道 frpc : 未配置（无 MRRC_CLOUD_LABEL）"
	fi
	if [ -e /dev/ttyUSB0 ]; then ok "CAT 串口  : /dev/ttyUSB0"; else warn "CAT 串口  : /dev/ttyUSB0 不存在"; fi
	say "  日志      : $APP_DIR/logs/server-stdout.log（活）· $APP_DIR/logs/ft710-server.log（start.sh 残留，可删）"
}

# ── 参数 ───────────────────────────────────────────────────────────────
NOWAIT=0
if [ "${1:-}" = "--no-wait" ]; then NOWAIT=1; shift; fi
CMD=${1:-restart}
PORT=$(env_val MRRC_WEB_PORT); PORT=${PORT:-8888}
LABEL=$(env_val MRRC_CLOUD_LABEL)
EXPECT_CN="${LABEL:+$LABEL.mrrc.vlsc.net}"

case "$CMD" in
status)
	need_root "$@"
	print_status
	;;
start | stop)
	need_root "$@"
	systemctl "$CMD" "$UNIT"
	say "  单元 → $(systemctl is-active "$UNIT")"
	;;
restart)
	need_root "$@"
	release_rogue_servers
	say "▶ systemctl restart $UNIT"
	systemctl restart "$UNIT"
	if [ "$NOWAIT" = "1" ]; then
		say "  （--no-wait：已发出重启，不做健康等待）"
		exit 0
	fi
	code=000
	deadline=$((SECONDS + WAIT_WEB))
	while [ "$SECONDS" -lt "$deadline" ]; do
		code=$(web_code)
		if web_ok "$code"; then break; fi
		sleep 1
	done
	say ""
	say "重启后状态："
	print_status
	if ! web_ok "$code"; then
		say ""
		bad "重启后 ${WAIT_WEB}s 内 Web 未就绪（最后状态码 $code）"
		say "  排查：journalctl -u $UNIT -n 40 --no-pager"
		say "        tail -40 $APP_DIR/logs/server-stdout.log   # systemd 的 stdout，真正的活日志"
		exit 1
	fi
	if [ -n "$LABEL" ] && [ -z "$(tunnel_pid)" ]; then
		deadline=$((SECONDS + WAIT_TUNNEL))
		while [ "$SECONDS" -lt "$deadline" ]; do
			[ -n "$(tunnel_pid)" ] && break
			sleep 1
		done
		if [ -z "$(tunnel_pid)" ]; then
			say ""
			bad "Web 已就绪，但 ${WAIT_TUNNEL}s 内隧道 frpc 未起来（label=$LABEL → 公网入口 502）"
			say "  排查：$APP_DIR/fleet/frpc 是否存在且可执行；grep MRRC_CLOUD $ENV_FILE"
			exit 2
		fi
	fi
	say ""
	ok "重启完成，服务健康"
	;;
*)
	sed -n '2,30p' "$(readlink -f "${BASH_SOURCE[0]}")"
	exit 1
	;;
esac
