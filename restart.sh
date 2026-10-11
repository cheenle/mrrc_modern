#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# MRRC Modern — restart（平台分发）
# ═══════════════════════════════════════════════════════════════════════
# Linux 盒子 → restart_linux.sh（systemd 驱动 + 健康验证）
# macOS/开发机 → restart_mac.sh（stop.sh + start.sh）
#
# 保留本文件名是因为文档与使用习惯都用 ./restart.sh。
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$(uname -s)" in
Linux) exec "$DIR/restart_linux.sh" "$@" ;;
Darwin) exec "$DIR/restart_mac.sh" "$@" ;;
*)
	echo "未知平台 $(uname -s)：Linux 用 ./restart_linux.sh，macOS 用 ./restart_mac.sh" >&2
	exit 1
	;;
esac
