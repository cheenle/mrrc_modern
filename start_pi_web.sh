#!/bin/bash
# Start pi-web with password-protected reverse proxy
#
# Usage:
#   PI_WEB_PASSWORD=mysecret ./start_pi_web.sh
#   PI_WEB_PASSWORD=mysecret PI_WEB_PORT=8443 ./start_pi_web.sh
#
# Set PI_WEB_PASSWORD in your environment or .env file.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PI_WEB_PORT="${PI_WEB_PORT:-8443}"
PI_WEB_BACKEND_PORT="${PI_WEB_BACKEND_PORT:-30141}"

# Source .env if present
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a; source "$SCRIPT_DIR/.env"; set +a
fi

if [ -z "$PI_WEB_PASSWORD" ]; then
    echo "ERROR: PI_WEB_PASSWORD is required."
    echo "  PI_WEB_PASSWORD=mysecret ./start_pi_web.sh"
    exit 1
fi

echo "=== pi-web auth proxy ==="
echo "Public URL:  http://localhost:${PI_WEB_PORT}"
echo "Backend:     http://127.0.0.1:${PI_WEB_BACKEND_PORT}"
echo "User:        ${PI_WEB_USER:-admin}"
echo ""

# Step 1: start pi-web in background (if not already running)
PI_WEB_PID=""
if ! lsof -i :${PI_WEB_BACKEND_PORT} -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "[1/2] Starting pi-web on port ${PI_WEB_BACKEND_PORT}..."
    PORT=$PI_WEB_BACKEND_PORT PI_WEB_NO_OPEN=1 pi-web &
    PI_WEB_PID=$!
    sleep 3
else
    echo "[1/2] pi-web already running on port ${PI_WEB_BACKEND_PORT}"
fi

# Step 2: start the auth proxy
echo "[2/2] Starting auth proxy on port ${PI_WEB_PORT}..."
echo ""

# Trap to clean up on exit
cleanup() {
    echo ""
    echo "Shutting down..."
    if [ -n "$PI_WEB_PID" ] && kill -0 "$PI_WEB_PID" 2>/dev/null; then
        kill "$PI_WEB_PID" 2>/dev/null
    fi
    exit 0
}
trap cleanup INT TERM

# Run the proxy (blocking)
PI_WEB_BACKEND="http://127.0.0.1:${PI_WEB_BACKEND_PORT}" \
    python3 "$SCRIPT_DIR/pi_web_proxy.py"

# Cleanup if proxy exits
cleanup
