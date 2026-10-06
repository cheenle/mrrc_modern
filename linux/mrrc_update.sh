#!/bin/bash -e
# Update MRRC Modern in place, without reflashing the image.
#
# Deliberately the smallest thing that works: fetch the tree, install whatever
# requirements.txt now needs, restart. There is no rollback, because there is
# no verified way to decide a rollback is warranted — upgrade_core.py's
# automatic path (slice 2) is explicitly not started, and inventing one here
# would be a second, untested state machine over the same files.
set -euo pipefail

MRRC_HOME="${MRRC_HOME:-/opt/mrrc_modern}"
MRRC_USER="${MRRC_USER:-mrrc}"
SRC="${MRRC_UPDATE_SRC:-}"          # git URL, or a local directory to rsync from
BRANCH="${MRRC_UPDATE_BRANCH:-main}"

[ -n "$SRC" ] || { echo "set MRRC_UPDATE_SRC to a git URL or a local directory" >&2; exit 1; }

echo "==> fetching from $SRC"
if [ -d "$SRC" ]; then
  rsync -a --delete "$SRC/" "$MRRC_HOME/" \
    --exclude ".git/" --exclude "venv/" --exclude ".venv/" --exclude "dist/" \
    --exclude "build/" --exclude "logs/" --exclude "certs/" --exclude "promo/" \
    --exclude "FT710Mobile/" --exclude "FT710Android/" --exclude "website/" \
    --exclude "tests/" --exclude ".agents/" --exclude "docs/" --exclude "SDD/" \
    --exclude "packaging/" --exclude "macos/" --exclude "windows/" \
    --exclude "__pycache__/" --exclude "*.pyc" --exclude ".DS_Store" \
    --exclude "atr1000_tuner.json"
else
  git -C "$MRRC_HOME" fetch --depth 1 origin "$BRANCH"
  git -C "$MRRC_HOME" checkout -f FETCH_HEAD
fi

echo "==> python dependencies"
"$MRRC_HOME/venv/bin/pip" install --upgrade -q -r "$MRRC_HOME/requirements.txt"

chown -R "$MRRC_USER:$MRRC_USER" "$MRRC_HOME"
echo "==> restarting"
systemctl restart mrrc-modern
systemctl --no-pager --lines=5 status mrrc-modern || true
echo "==> done; env file left untouched at $MRRC_HOME/env/mrrc.env"
