#!/usr/bin/env bash
# FT-710 电源控制便捷脚本 —— 包装 ft710_power.py (直接串口 CAT, 不经服务器/WSS)
#
# 用法:  ./power.sh <on|off|status|cycle>
#   on       开机 (PS1; + FA 回读验证, 最多 3 次重试)
#   off      关机 (PS0; 连发两次)
#   status   查询电源状态
#   cycle    断电 8 秒后重新上电
#
# 注意: 串口被服务器(server.py)占用时直接发命令会抢答,
#       建议先 ./stop.sh 停服务器再执行。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="$ROOT_DIR/venv/bin/python"
POWER_PY="$ROOT_DIR/ft710_power.py"

if [ $# -lt 1 ]; then
    echo "用法: $0 <on|off|status|cycle>" >&2
    exit 2
fi

case "$1" in
    on|off|status|cycle) ;;
    *)
        echo "✗ 未知参数: $1 (应为 on|off|status|cycle)" >&2
        exit 2
        ;;
esac

if [ ! -x "$VENV_PY" ]; then
    echo "✗ 找不到 venv python: $VENV_PY" >&2
    exit 1
fi
if [ ! -f "$POWER_PY" ]; then
    echo "✗ 找不到电源脚本: $POWER_PY" >&2
    exit 1
fi

exec "$VENV_PY" "$POWER_PY" "$@"
