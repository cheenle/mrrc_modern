#!/bin/bash
# ===========================================================================
#  MRRC Modern - install an already-built source tree over an existing install
#
#  What this is for: a box whose build predates 2026-10-18 has no self-update
#  API (`/api/update/apply`), and its own `mrrc-update` (up to 1.25.5) syncs
#  with `rsync --delete` — which wipes `env/`, `profiles/` and the recordings.
#  So neither the in-browser card nor that script can be used as-is.
#
#  What it does: unpack the published source tarball (latest.json -> boxLine),
#  back up the current code, copy the new tree over it **without deleting
#  anything**, run pip if the install has a venv, restart, print the version.
#
#  It is the shell twin of `box_update.apply()` — same keep-list, same "no
#  delete", same stamps (`version.txt` / `fixpack.txt` come from the tarball).
#  Nothing here touches config, recordings, memory channels or the tunnel.
#
#  usage:  mrrc-remote-update.sh <tarball> [home] [user]
#          bash mrrc-remote-update.sh /tmp/src.tgz /opt/mrrc_modern mrrc
#
#  env:    MRRC_RESTART_CMD   override the restart (tests pass /bin/true)
# ===========================================================================
set -euo pipefail

PKG="${1:?usage: mrrc-remote-update.sh <tarball> [home] [user]}"
TARGET="${2:-/opt/mrrc_modern}"
RUN_USER="${3:-mrrc}"

#: Same list as box_update.KEEP: runtime state that must never be overwritten
#: by a release (a package should not contain them either — stage-tree.sh drops
#: them — but this is the second guard, and it is the one that stays true if
#: somebody ever publishes a hand-made tarball).
EXCLUDES=(--exclude=./venv --exclude=./logs --exclude=./recordings --exclude=./.local
	--exclude=./env --exclude=./fleet --exclude=./data --exclude=./ft8-slots)

say() { printf '==> %s\n' "$*"; }
die() {
	printf 'ERROR: %s\n' "$*" >&2
	exit 1
}

[ -f "$PKG" ] || die "no such package: $PKG"
[ -f "$TARGET/server.py" ] || die "$TARGET does not look like an MRRC install (no server.py)"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/mrrc-update.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

say "unpack $PKG"
tar -xzf "$PKG" -C "$WORK"

# The tarball may carry the tree at the root or under one directory.
TREE="$WORK"
if [ ! -f "$TREE/server.py" ]; then
	for candidate in "$WORK"/*/; do
		if [ -f "${candidate}server.py" ]; then
			TREE="${candidate%/}"
			break
		fi
	done
fi
[ -f "$TREE/server.py" ] || die "the package has no server.py — not an MRRC tree"

NEW_VER="$(head -1 "$TREE/version.txt" 2>/dev/null | tr -d '[:space:]' || true)"
NEW_FP="$(head -1 "$TREE/fixpack.txt" 2>/dev/null | tr -d '[:space:]' || true)"
[ -n "$NEW_VER" ] || die "the package has no version.txt — refusing to install an unstamped tree"
OLD_VER="$(head -1 "$TARGET/version.txt" 2>/dev/null | tr -d '[:space:]' || true)"
say "version ${OLD_VER:-?} -> $NEW_VER${NEW_FP:+ ($NEW_FP)}"

# Back up the code only. Excluding .local also keeps the backup out of its own
# archive: that is where it is written.
BACKUP_DIR="$TARGET/.local/share/MRRC-Modern/update"
mkdir -p "$BACKUP_DIR"
BACKUP="$BACKUP_DIR/rollback-manual-${OLD_VER:-unknown}-$(date +%Y%m%d-%H%M%S).tar.gz"
say "backup -> $BACKUP"
# shellcheck disable=SC2086  # $KEEP is a space-separated list on purpose
tar -czf "$BACKUP" -C "$TARGET" \
	--exclude=./venv --exclude=./logs --exclude=./recordings --exclude=./.local \
	--exclude=./env --exclude=./fleet --exclude=./data --exclude=./ft8-slots \
	--exclude=./__pycache__ .

say "copy the new tree over (nothing is deleted)"
tar -cf - -C "$TREE" . | tar -xf - -C "$TARGET" --no-same-owner "${EXCLUDES[@]}"

if [ "$(id -u)" = "0" ]; then
	chown -R "$RUN_USER:$RUN_USER" "$TARGET" 2>/dev/null || true
fi

if [ -x "$TARGET/venv/bin/pip" ] && [ -f "$TARGET/requirements.txt" ]; then
	say "python dependencies"
	if ! "$TARGET/venv/bin/pip" install --upgrade -q -r "$TARGET/requirements.txt"; then
		die "pip failed. The service was NOT restarted, so the old code is still running in memory;
     fix the dependency problem and run this again."
	fi
fi

say "restart"
if [ -n "${MRRC_RESTART_CMD:-}" ]; then
	bash -c "$MRRC_RESTART_CMD"
elif [ "$(id -u)" = "0" ]; then
	systemctl restart mrrc-modern
else
	# No root: the unit restarts the process on a non-zero exit (Restart=on-failure),
	# which is exactly what `box_update`'s own watchdog does.
	pkill -f 'venv/bin/python server.py' || true
fi

say "done: ${OLD_VER:-?} -> $NEW_VER${NEW_FP:+ ($NEW_FP)}"
echo "    backup kept at: $BACKUP"
echo "    logs:           $TARGET/logs/server-stdout.log"
