#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# MRRC Modern — Restart Server
# ═══════════════════════════════════════════════════════════════════════
# Thin wrapper: stop → start.  All logic lives in stop.sh / start.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Pass any arguments through to start.sh  (e.g. --foreground, -p PORT)
"$SCRIPT_DIR/stop.sh"
echo ""
exec "$SCRIPT_DIR/start.sh" "$@"
