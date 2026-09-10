#!/bin/bash -e
# pi-gen stage prerun: carry the previous stage's rootfs into this stage.
# (Standard pi-gen helper; without it the stage has no rootfs to chroot into.)
if [ ! -d "$ROOTFS_DIR" ]; then
    copy_previous
fi

# Inject the MRRC Modern code tree (staged by build-image.sh under
# 01-deploy-mrrc/files/opt/mrrc_modern). This pi-gen version has no
# automatic files/ copy, so prerun does it explicitly. cwd is the stage
# dir (build.sh pushd's before calling prerun) — use relative paths.
CODE_SRC="01-deploy-mrrc/files/opt/mrrc_modern"
[ -f "$CODE_SRC/requirements.txt" ] || { echo "FATAL: code tree missing at $PWD/$CODE_SRC"; exit 1; }
mkdir -p "$ROOTFS_DIR/opt/mrrc_modern"
cp -a "$CODE_SRC"/. "$ROOTFS_DIR/opt/mrrc_modern/"
[ -f "$ROOTFS_DIR/opt/mrrc_modern/requirements.txt" ] || { echo "FATAL: code copy failed"; exit 1; }
echo "prerun: code tree injected ($(ls "$ROOTFS_DIR/opt/mrrc_modern" | wc -l) entries)"
