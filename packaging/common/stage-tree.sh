#!/bin/bash
# Stage the repository tree into an image's /opt/mrrc_modern — the ONE place the
# exclusion list lives.
#
# Three callers, one list:
#   * packaging/rpi/build-image.sh    on the build host (macOS), staging into pi-gen
#   * packaging/box/box-overlay.sh    inside the Armbian chroot, /src → /opt/mrrc_modern
#   * linux/mrrc_update.sh            on the box, using the copy in the tree it fetched
#
# Two of them disagreeing is how a file ends up in one image and missing from the
# other, and the symptom is a runtime ImportError rather than a build error, so
# tests/test_packaging_parity.py fails the build when this list grows a second home.
#
# bash 3.2 on purpose: the Pi builder runs this with macOS's /bin/bash.
#
# usage: stage-tree.sh <src> <dst>
set -euo pipefail

SRC="${1:?usage: stage-tree.sh <src> <dst>}"
DST="${2:?usage: stage-tree.sh <src> <dst>}"
[ -d "$SRC" ] || {
	echo "stage-tree: no such source tree: $SRC" >&2
	exit 1
}
mkdir -p "$DST"

# Deliberately no --delete: image builds stage into an empty directory, and the
# in-place updater's --delete is a separate decision (see the note in
# linux/mrrc_update.sh) rather than something to smuggle in with this extraction.
#
# Why the prose lives here and not inside the argument list: a `#` on a line that
# continues an argument list ENDS THE COMMAND, so every option after it becomes a
# new command called --exclude. Three builders shipped that on 2026-10-11 and no
# test could see it (bash -n is a parse-time check and this is runtime semantics;
# a regex over the text finds every --exclude whether or not bash ever passes it
# on). ShellHygieneTests.test_no_comment_inside_a_continued_argument_list is the
# rule that keeps it from coming back.
#
# `packaging/` is excluded — the service layer's own assets (profiles,
# fetch-frpc.sh, firstboot_wrapper.py, the units' source) are staged separately
# by each builder into /opt/mrrc-build.
#
# The root-anchored (/x) rules match only the repo root: per-box state (instance
# identity, recordings, operator files, the Cloud Hub's fleet/ directory) and
# dev-only material must never be shipped by an image, nor overwritten by an
# update.  /draft/ joined the list on 2026-10-13 with the Rust rewrite draft
# workstream: a crate skeleton plus a draft SDD that nothing in the product
# imports, and without this rule it would be staged into both images. /fleet/ joined the list on 2026-10-11 after a build staged the
# developer's own 14.8 MB frpc from the repo root into the image before the real
# fetch replaced it — harmless in the end, but it is exactly the kind of
# machine state this list exists to keep out.
#
# Five more joined on 2026-10-14 (auth-tokens.json, mrrc_modern.env,
# recordings.json, ft8/settings.json, /env/), and two of them are the reason the
# 2026-10-14 `qso.db` rule was not enough on its own. `_runtime_dir()` is the
# *install directory* in a packaged build, so every file server.py writes at
# runtime lands in $MRRC_HOME — the same place this script stages into. A
# developer's working tree therefore holds that developer's live state:
# auth-tokens.json is a 30-day session credential (staging it logs every
# operator out and makes the developer's cookies valid on a box that can key a
# transmitter), and mrrc_modern.env carries MRRC_WEB_PASSWORD — the only door
# when bound to 0.0.0.0 — and MRRC_CLOUD_TOKEN. That second one reaches past the
# updater to the *image* builders, so a secret in the build tree ships inside the
# artifact. All four verified present in a real working tree before the rule
# landed; the guard that runs the stager rather than reading this text is
# ExclusionListTests.test_runtime_state_is_never_staged.
#
# 2026-10-17: that rule matched the exact name only, and the working tree's copy
# had been renamed to auth-tokens.json.invalidated-<ts> (what the runtime writes
# when it retires an unreadable store), so it was baked into the v1.28.1 box
# image -- caught by step 2 of the artifact verification, not by any guard.
# The rule is now auth-tokens.json* and the stager test carries that real name.
rsync -a "$SRC/" "$DST/" \
	--exclude ".git/" --exclude "venv/" --exclude ".venv*/" --exclude "dist/" \
	--exclude "build/" --exclude "logs/" --exclude "certs/" --exclude "promo/" \
	--exclude "FT710Mobile/" --exclude "FT710Android/" --exclude "website/" \
	--exclude "tests/" --exclude ".agents/" --exclude "docs/" --exclude "SDD/" \
	--exclude "packaging/" --exclude "macos/" --exclude "windows/" \
	--exclude "__pycache__/" --exclude "*.pyc" --exclude ".DS_Store" \
	--exclude "atr1000_tuner.json" --exclude "qso.db" \
	--exclude "auth-tokens.json*" --exclude "mrrc_modern.env" \
	--exclude "recordings.json" --exclude "ft8/settings.json" \
	--exclude "/env/" \
	--exclude "/fleet/" \
	--exclude "/draft/" \
	--exclude "/.local/" --exclude "/.env" --exclude "/recordings/" \
	--exclude "/mem_channels.json" --exclude "/.worktrees/" --exclude "/.superpowers/" \
	--exclude "/.agnes/" --exclude "/.claude/" --exclude "/.pytest_cache/" \
	--exclude "/.ruff_cache/" --exclude "/dev_tools/" --exclude "/tools/" \
	--exclude "/support-out/" --exclude "/lib/" --exclude "/biome.json" \
	--exclude "/build_mrrc_v1101.ps1" --exclude "/*.md" --exclude "/*.pdf" \
	--exclude "/*.jpg" --exclude "/*.JPG" --exclude "/*.PNG" --exclude "/*.m4a"

echo "stage-tree: $SRC → $DST ($(find "$DST" -type f | wc -l | tr -d ' ') files)"
