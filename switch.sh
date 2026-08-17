#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# switch.sh — mrrc_modern ⇄ mrrc_ft8 一键切换
# ═══════════════════════════════════════════════════════════════════════
# 两个程序共用电台的 USB 音频与 CAT/CI-V 设备，同一时间只能运行一个。
# 本脚本检测当前哪个在运行：停掉在跑的，启动没跑的。
#
# 用法:
#   ./switch.sh           切换到当前没在运行的那个程序
#   ./switch.sh mrrc      强制切换到 mrrc_modern（本目录 restart.sh）
#   ./switch.sh ft8       强制切换到 mrrc_ft8（../ft8/restart.sh）
#   ./switch.sh status    只显示当前运行状态，不做切换
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MRRC_DIR="$HERE"
FT8_DIR="$HERE/../ft8"

FT8_PORT=8000
MRRC_PORT="${MRRC_WEB_PORT:-${FT710_WEB_PORT:-8888}}"
# rigctld 端口需与 ../ft8/restart.sh 的 MRRC_FT8_RIGCTLD_PORT 一致
RIGCTLD_PORT="${MRRC_FT8_RIGCTLD_PORT:-4532}"
# 停掉一个程序后，CoreAudio 需要时间释放 USB 音频设备，立刻启动另一个
# 会拿到降级的音频流且无法恢复（测量说明见 ../ft8/restart.sh 注释）。
AUDIO_SETTLE="${SWITCH_SETTLE:-8}"

if [ -f "$MRRC_DIR/.env" ]; then
	p="$(grep -E '^(MRRC|FT710)_WEB_PORT=' "$MRRC_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2 || true)"
	if [ -n "$p" ]; then MRRC_PORT="$p"; fi
	sp="$(grep -E '^(MRRC|FT710)_SERIAL_PORT=' "$MRRC_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2 || true)"
	if [ -n "$sp" ]; then MRRC_SERIAL="$sp"; fi
fi

# 电台 CAT/CI-V 串口（与 ../ft8/restart.sh 的 RIG_DEVICE 必须一致；唯一 owner 是
# 当前运行侧的程序。switch.sh 用它做"残留进程"检测的语义兜底——见 mrrc_running）
MRRC_SERIAL="${MRRC_SERIAL:-${FT710_SERIAL:-/dev/cu.SLAB_USBtoUART}}"

usage() {
	cat <<'EOF'
用法: ./switch.sh [mrrc|ft8|status]

  (无参数)    切换到当前没在运行的那个程序
  mrrc       强制切换到 mrrc_modern
  ft8        强制切换到 mrrc_ft8
  status     只显示当前运行状态，不做切换

环境变量:
  SWITCH_SETTLE   切换时等待 USB 音频设备释放的秒数（默认 8）
EOF
	exit "${1:-0}"
}

# ─── 运行状态检测 ────────────────────────────────────────────────────
# 与 ../ft8/restart.sh 相同的判定方式：8000 端口监听者 + server.main 进程
ft8_pids() {
	{
		lsof -iTCP:"$FT8_PORT" -sTCP:LISTEN -t 2>/dev/null || true
		pgrep -f "server\.main" 2>/dev/null || true
	} | sort -u
}

ft8_running() {
	[ -n "$(ft8_pids)" ]
}

# rigctld（Hamlib CAT 守护，FT8 串口唯一 owner）：进程名 + 监听端口双保险
rigctld_pids() {
	{
		pgrep -x rigctld 2>/dev/null || true
		lsof -iTCP:"$RIGCTLD_PORT" -sTCP:LISTEN -t 2>/dev/null || true
	} | sort -u
}

rigctld_running() {
	[ -n "$(rigctld_pids)" ]
}

# PID 文件存活、8888 端口监听、server.py 进程，三者任一成立即视为在运行
mrrc_running() {
	local pid pid_file="$MRRC_DIR/.mrrc-server.pid" rigpid
	if [ -f "$pid_file" ]; then
		pid="$(cat "$pid_file" 2>/dev/null || true)"
		if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
			return 0
		fi
	fi
	if [ -n "$(lsof -iTCP:"$MRRC_PORT" -sTCP:LISTEN -t 2>/dev/null || true)" ]; then
		return 0
	fi
	# 大小写不敏感：MacPorts 解释器命令行是 "Python server.py"（大写 P），
	# 小写正则 "python.*server\.py" 匹配不到 → 误判未运行 → 切换漏停残留进程
	# （现场 2026-08-07：残留 server.py 与 rigctld 抢串口 4 小时）。
	if [ -n "$(pgrep -if "server\.py" 2>/dev/null || true)" ]; then
		return 0
	fi
	# 串口语义兜底：CAT/CI-V 串口被非 rigctld 进程持有 = 有残留程序在占用电台。
	# 切换前必须清理，否则 ft8 侧 rigctld 一启动就与之争抢（AD-008）。
	if command -v lsof >/dev/null 2>&1; then
		rigpid="$(pgrep -x rigctld 2>/dev/null || true)"
		for hpid in $(lsof -t "$MRRC_SERIAL" 2>/dev/null || true); do
			if [ -z "$rigpid" ] || [ "$hpid" != "$rigpid" ]; then
				return 0
			fi
		done
	fi
	return 1
}

show_status() {
	if ft8_running; then
		echo "mrrc_ft8    (port $FT8_PORT):  运行中  PID: $(ft8_pids | tr '\n' ' ')"
	else
		echo "mrrc_ft8    (port $FT8_PORT):  未运行"
	fi
	if rigctld_running; then
		echo "rigctld     (port $RIGCTLD_PORT):  运行中  PID: $(rigctld_pids | tr '\n' ' ')"
	else
		echo "rigctld     (port $RIGCTLD_PORT):  未运行"
	fi
	if mrrc_running; then
		echo "mrrc_modern (port $MRRC_PORT):  运行中"
	else
		echo "mrrc_modern (port $MRRC_PORT):  未运行"
	fi
}

# ─── 停止（ft8 没有 stop.sh，清理逻辑镜像自 ../ft8/restart.sh）────────
stop_ft8() {
	local pids
	pids="$(ft8_pids)"
	if [ -z "$pids" ]; then
		echo "mrrc_ft8 未在运行"
	else
		echo "停止 mrrc_ft8 (PID: $(echo "$pids" | tr '\n' ' '))..."
		echo "$pids" | xargs kill 2>/dev/null || true
		for _ in $(seq 1 20); do
			pids="$(ft8_pids)"
			if [ -z "$pids" ]; then break; fi
			sleep 0.5
		done
		if [ -n "$pids" ]; then
			echo "  强制结束..."
			echo "$pids" | xargs kill -9 2>/dev/null || true
			for _ in $(seq 1 10); do
				pids="$(ft8_pids)"
				if [ -z "$pids" ]; then break; fi
				sleep 0.5
			done
		fi
		if [ -n "$(ft8_pids)" ]; then
			echo "✗ 无法停止 mrrc_ft8，请手动检查" >&2
			return 1
		fi
		echo "✓ mrrc_ft8 已停止"
	fi

	# rigctld 也一并停掉：释放 CAT 串口给 mrrc_ft710（AD-008 串口唯一 owner）
	local rpids
	rpids="$(rigctld_pids)"
	if [ -z "$rpids" ]; then
		echo "rigctld 未在运行"
		return 0
	fi
	echo "停止 rigctld (PID: $(echo "$rpids" | tr '\n' ' '))..."
	echo "$rpids" | xargs kill 2>/dev/null || true
	for _ in $(seq 1 20); do
		rpids="$(rigctld_pids)"
		if [ -z "$rpids" ]; then break; fi
		sleep 0.5
	done
	if [ -n "$rpids" ]; then
		echo "  强制结束 rigctld..."
		echo "$rpids" | xargs kill -9 2>/dev/null || true
		for _ in $(seq 1 10); do
			rpids="$(rigctld_pids)"
			if [ -z "$rpids" ]; then break; fi
			sleep 0.5
		done
	fi
	if [ -n "$(rigctld_pids)" ]; then
		echo "✗ 无法停止 rigctld，请手动检查" >&2
		return 1
	fi
	echo "✓ rigctld 已停止"
}

stop_mrrc() {
	"$MRRC_DIR/stop.sh"
}

# ─── 切换 ────────────────────────────────────────────────────────────
switch_to_mrrc() {
	stop_ft8
	echo "等待 ${AUDIO_SETTLE}s 让 USB 音频设备释放..."
	sleep "$AUDIO_SETTLE"
	echo ""
	echo "═══ 启动 mrrc_modern（$MRRC_DIR/restart.sh）═══"
	"$MRRC_DIR/restart.sh"
}

switch_to_ft8() {
	if mrrc_running; then
		stop_mrrc
	else
		echo "mrrc_modern 未在运行"
	fi
	echo ""
	# ft8 的 restart.sh 自带 8s 音频设备释放等待，无需额外 sleep
	echo "═══ 启动 mrrc_ft8（$FT8_DIR/restart.sh）═══"
	"$FT8_DIR/restart.sh"
}

# ─── 主流程 ──────────────────────────────────────────────────────────
[ -d "$FT8_DIR" ] || {
	echo "✗ 找不到 $FT8_DIR" >&2
	exit 1
}
FT8_DIR="$(cd "$FT8_DIR" && pwd)"

TARGET="${1:-}"
case "$TARGET" in
status)
	show_status
	exit 0
	;;
-h | --help | help) usage 0 ;;
mrrc_ft8 | ft8) TARGET=ft8 ;;
mrrc | mrrc_modern | ft710) TARGET=mrrc ;;
"") ;;
*)
	echo "✗ 未知参数: $TARGET" >&2
	usage 1
	;;
esac

F8_UP=false
MRRC_UP=false
if ft8_running; then F8_UP=true; fi
if mrrc_running; then MRRC_UP=true; fi

echo "当前状态: mrrc_modern=$MRRC_UP  mrrc_ft8=$F8_UP"

if $F8_UP && $MRRC_UP; then
	echo "✗ 两个程序都在运行（异常状态），请手动停掉一个后重试：" >&2
	echo "  停 mrrc: $MRRC_DIR/stop.sh" >&2
	echo "  停 ft8:   lsof -iTCP:$FT8_PORT -sTCP:LISTEN -t | xargs kill" >&2
	exit 1
fi

if [ -z "$TARGET" ]; then
	if $F8_UP; then
		TARGET=mrrc
	elif $MRRC_UP; then
		TARGET=ft8
	else
		TARGET=mrrc
		echo "两个程序都未运行，默认启动 mrrc_modern（如需 ft8: ./switch.sh ft8）"
	fi
else
	if [ "$TARGET" = mrrc ] && $MRRC_UP; then
		echo "mrrc_modern 已在运行，无需切换"
		exit 0
	fi
	if [ "$TARGET" = ft8 ] && $F8_UP; then
		echo "mrrc_ft8 已在运行，无需切换"
		exit 0
	fi
fi

echo "切换到: $TARGET"
if [ "$TARGET" = mrrc ]; then
	switch_to_mrrc
else
	switch_to_ft8
fi
