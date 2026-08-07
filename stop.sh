#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# MRRC FT-710 — Stop Server
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

PID_FILE="$ROOT_DIR/.ft710-server.pid"
LOG_FILE="$ROOT_DIR/logs/ft710-server.log"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

# ── Source .env for port ─────────────────────────────────────────────
PORT="${FT710_WEB_PORT:-8888}"
if [ -f "$ROOT_DIR/.env" ]; then
  set -a; source "$ROOT_DIR/.env"; set +a
  PORT="${FT710_WEB_PORT:-$PORT}"
fi

stopped_cleanly=false

# ═══════════════════════════════════════════════════════════════════════
# 1. Stop by PID file
# ═══════════════════════════════════════════════════════════════════════
if [ -f "$PID_FILE" ]; then
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo -n "Stopping server (pid $pid)... "
    kill "$pid" 2>/dev/null || true

    # Graceful shutdown: wait up to 5 seconds
    for _ in $(seq 1 20); do
      if ! kill -0 "$pid" 2>/dev/null; then
        echo -e "${GREEN}stopped${NC}"
        stopped_cleanly=true
        break
      fi
      sleep 0.25
    done

    # Force kill if still alive
    if ! $stopped_cleanly; then
      echo -n "force-killing... "
      kill -9 "$pid" 2>/dev/null || true
      sleep 0.5
      if kill -0 "$pid" 2>/dev/null; then
        echo -e "${RED}FAILED — process $pid is unkillable${NC}"
      else
        echo -e "${YELLOW}force-stopped${NC}"
        stopped_cleanly=true
      fi
    fi
  else
    echo "Stale PID file (pid $pid is dead)"
  fi
  rm -f "$PID_FILE"
else
  echo "No PID file found"
fi

# ═══════════════════════════════════════════════════════════════════════
# 2. Kill scope_pipe subprocess (orphaned by server)
# ═══════════════════════════════════════════════════════════════════════
scope_pids=$(pgrep -f "scope_pipe.py" 2>/dev/null || true)
if [ -n "$scope_pids" ]; then
  echo -n "Stopping scope_pipe... "
  echo "$scope_pids" | xargs kill 2>/dev/null || true
  sleep 0.3
  scope_pids=$(pgrep -f "scope_pipe.py" 2>/dev/null || true)
  [ -n "$scope_pids" ] && echo "$scope_pids" | xargs kill -9 2>/dev/null || true
  echo -e "${GREEN}done${NC}"
fi

# ═══════════════════════════════════════════════════════════════════════
# 3. Clean up port listeners (any leftover uvicorn/stale socket)
# ═══════════════════════════════════════════════════════════════════════
if command -v lsof &>/dev/null; then
  port_pid=$(lsof -ti ":$PORT" -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$port_pid" ]; then
    echo -n "Freeing port $PORT (pid $port_pid)... "
    kill "$port_pid" 2>/dev/null || true
    sleep 0.5
    if lsof -ti ":$PORT" -sTCP:LISTEN &>/dev/null 2>&1; then
      kill -9 "$port_pid" 2>/dev/null || true
      sleep 0.5
      if lsof -ti ":$PORT" -sTCP:LISTEN &>/dev/null 2>&1; then
        echo -e "${RED}FAILED — port $PORT is stuck (reboot may be needed)${NC}"
      else
        echo -e "${YELLOW}force-freed${NC}"
      fi
    else
      echo -e "${GREEN}freed${NC}"
    fi
  fi
fi

# ═══════════════════════════════════════════════════════════════════════
# 4. Final verification
# ═══════════════════════════════════════════════════════════════════════
# 大小写不敏感：MacPorts 解释器命令行是 "Python server.py"（大写 P），
# 小写正则 "python.*server.py" 匹配不到 → 残留进程漏检（现场 2026-08-07）。
remaining=$(pgrep -if "server\.py" 2>/dev/null || true)
if [ -n "$remaining" ]; then
  echo -e "${YELLOW}⚠ Lingering python processes: $remaining${NC}"
else
  echo -e "${GREEN}✓ FT-710 server fully stopped${NC}"
fi

# ═══════════════════════════════════════════════════════════════════════
# 5. Serial port holders — AD-008 串口唯一 owner 兜底
# ═══════════════════════════════════════════════════════════════════════
# 手动/非 start.sh 启动的 server 进程可能不写 pid 文件、不监听 WEB 端口，
# 但一定会持有 CAT 串口（.env 的 FT710_SERIAL_PORT）。任何非 rigctld 的
# 持有者都是残留冲突进程，直接释放——避免切换后 ft8 侧 rigctld 与其争抢。
SERIAL_DEV="${FT710_SERIAL_PORT:-/dev/cu.SLAB_USBtoUART}"
rigctld_pid=$(pgrep -x rigctld 2>/dev/null || true)
holders=$(lsof -t "$SERIAL_DEV" 2>/dev/null || true)
for hpid in $holders; do
  if [ -n "$rigctld_pid" ] && [ "$hpid" = "$rigctld_pid" ]; then
    continue  # rigctld 归 ft8 侧管理，不在本脚本职责内
  fi
  echo -n "Releasing serial port holder (pid $hpid)... "
  kill "$hpid" 2>/dev/null || true
  sleep 0.3
  if kill -0 "$hpid" 2>/dev/null; then
    kill -9 "$hpid" 2>/dev/null || true
    sleep 0.3
    echo -e "${YELLOW}force-stopped${NC}"
  else
    echo -e "${GREEN}stopped${NC}"
  fi
done
if [ -n "$holders" ]; then
  echo -e "${GREEN}✓ Serial port released${NC}"
fi
