#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# MRRC Modern — Start Server
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

VENV_PY="$ROOT_DIR/venv/bin/python"
PID_FILE="$ROOT_DIR/.ft710-server.pid"
LOG_DIR="$ROOT_DIR/logs"
LOG_FILE="$LOG_DIR/ft710-server.log"
MAX_LOG_SIZE_MB=50

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

# ── Help ──────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
Usage: $0 [OPTIONS]

Options:
  -f, --foreground   Run in foreground (no nohup, logs to stdout)
  -p, --port PORT    Override web port (env: MRRC_WEB_PORT)
  -s, --serial PATH  Override CAT/CI-V serial port (env: MRRC_SERIAL_PORT)
  -h, --help         Show this help
EOF
  exit 0
}

# ── Parse args ────────────────────────────────────────────────────────
FOREGROUND=false
while [ $# -gt 0 ]; do
  case "$1" in
    -f|--foreground) FOREGROUND=true ;;
    -p|--port) export MRRC_WEB_PORT="$2"; shift ;;
    -s|--serial) export MRRC_SERIAL_PORT="$2"; shift ;;
    -h|--help) usage ;;
    *) echo -e "${RED}Unknown: $1${NC}"; usage ;;
  esac
  shift
done

# ── Source .env (lowest priority — CLI args override) ─────────────────
if [ -f "$ROOT_DIR/.env" ]; then
  set -a; source "$ROOT_DIR/.env"; set +a
fi

# ═══════════════════════════════════════════════════════════════════════
# 1. Pre-flight checks
# ═══════════════════════════════════════════════════════════════════════

# 1a. Python venv
if [ ! -x "$VENV_PY" ]; then
  echo -e "${RED}✗ venv not found at $VENV_PY${NC}"
  echo "  Run: ./install.sh"
  exit 1
fi

# 1b. Quick syntax check
if ! "$VENV_PY" -c "import py_compile; py_compile.compile('$ROOT_DIR/server.py', doraise=True)" 2>/dev/null; then
  echo -e "${RED}✗ server.py has syntax errors — fix before starting${NC}"
  exit 1
fi

# 1c. Critical imports
if ! "$VENV_PY" -c "import fastapi, uvicorn, serial" 2>/dev/null; then
  echo -e "${RED}✗ Missing critical Python packages${NC}"
  echo "  Run: venv/bin/pip install -r requirements.txt"
  exit 1
fi

# 1d. Already running?
if [ -f "$PID_FILE" ]; then
  old_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
    echo -e "${YELLOW}Server already running: pid $old_pid${NC}"
    echo "  Log: $LOG_FILE"
    echo "  Stop: ./stop.sh"
    exit 0
  fi
  echo -e "${YELLOW}Cleaning stale PID file (pid $old_pid is dead)${NC}"
  rm -f "$PID_FILE"
fi

# 1e. Port check
PORT="${MRRC_WEB_PORT:-${FT710_WEB_PORT:-8888}}"
if command -v lsof &>/dev/null; then
  port_pid=$(lsof -ti ":$PORT" -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$port_pid" ]; then
    echo -e "${YELLOW}⚠ Port $PORT is in use by pid $port_pid${NC}"
    if kill -0 "$port_pid" 2>/dev/null; then
      echo "  That process is still alive — trying ./stop.sh first"
      "$ROOT_DIR/stop.sh" 2>/dev/null || true
      sleep 1
      if lsof -ti ":$PORT" -sTCP:LISTEN &>/dev/null 2>&1; then
        echo -e "${RED}✗ Port $PORT still busy after stop attempt${NC}"
        echo "  Manually: lsof -ti :$PORT | xargs kill"
        exit 1
      fi
    else
      echo "  Stale listener — continuing"
    fi
  fi
fi

# 1f. Serial port check
SERIAL_PORT="${MRRC_SERIAL_PORT:-${FT710_SERIAL_PORT:-}}"
if [ -n "${SERIAL_PORT:-}" ]; then
  if [ ! -e "$SERIAL_PORT" ]; then
    echo -e "${YELLOW}⚠ Serial port not found: $SERIAL_PORT${NC}"
    echo "  Server will start but radio control won't work"
    echo "  Available serial ports:"
    ls /dev/cu.* 2>/dev/null | grep -i usb || ls /dev/ttyUSB* 2>/dev/null || echo "  (none found)"
    echo ""
  elif [ ! -r "$SERIAL_PORT" ] || [ ! -w "$SERIAL_PORT" ]; then
    echo -e "${YELLOW}⚠ Serial port exists but may not be accessible: $SERIAL_PORT${NC}"
  fi
fi

# 1g. Log rotation
mkdir -p "$LOG_DIR"
if [ -f "$LOG_FILE" ]; then
  log_size=$(du -m "$LOG_FILE" 2>/dev/null | cut -f1 || echo 0)
  if [ "$log_size" -gt "$MAX_LOG_SIZE_MB" ]; then
    mv "$LOG_FILE" "$LOG_FILE.1"
    echo "Log rotated (was ${log_size}MB)"
  fi
fi

# ═══════════════════════════════════════════════════════════════════════
# 2. Start
# ═══════════════════════════════════════════════════════════════════════

echo -e "${CYAN}Starting MRRC Modern server...${NC}"
echo "  Serial: ${SERIAL_PORT:-auto}"
echo "  Port:   $PORT"
echo "  Log:    $LOG_FILE"

if $FOREGROUND; then
  echo -e "${GREEN}Running in foreground (Ctrl-C to stop)${NC}"
  exec "$VENV_PY" server.py
else
  nohup "$VENV_PY" server.py >>"$LOG_FILE" 2>&1 &
  pid="$!"
  echo "$pid" > "$PID_FILE"

  # Wait for startup
  for i in $(seq 1 15); do
    sleep 0.3
    if ! kill -0 "$pid" 2>/dev/null; then
      echo -e "${RED}✗ Server crashed on startup${NC}"
      echo "  Last 20 lines of log:"
      tail -20 "$LOG_FILE"
      rm -f "$PID_FILE"
      exit 1
    fi
    # Check if uvicorn is listening
    if grep -q "Uvicorn running on" "$LOG_FILE" 2>/dev/null; then
      echo -e "${GREEN}✓ Server started: pid $pid${NC}"
      echo "  URL:  http://localhost:$PORT"
      echo "  Log:  $LOG_FILE"
      echo "  Stop: ./stop.sh"
      exit 0
    fi
  done

  # Timed out waiting for ready — check if still alive
  if kill -0 "$pid" 2>/dev/null; then
    echo -e "${YELLOW}Server process is running (pid $pid) but startup message not seen yet${NC}"
    echo "  Check: tail -f $LOG_FILE"
    echo "  Stop:  ./stop.sh"
  else
    echo -e "${RED}✗ Server failed to start${NC}"
    tail -20 "$LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
  fi
fi
