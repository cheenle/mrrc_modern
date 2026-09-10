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
# Overlay the ENTIRE staged files/ tree (etc/ systemd+motd+helper, usr/ helper,
# opt/ code) onto the rootfs. This pi-gen version has no automatic files/ copy.
[ -f "01-deploy-mrrc/files/opt/mrrc_modern/requirements.txt" ] || { echo "FATAL: code tree missing at $PWD"; exit 1; }
cp -a 01-deploy-mrrc/files/. "$ROOTFS_DIR/"
[ -f "$ROOTFS_DIR/opt/mrrc_modern/requirements.txt" ] || { echo "FATAL: code copy failed"; exit 1; }
[ -f "$ROOTFS_DIR/usr/local/bin/mrrc-show-password" ] || { echo "FATAL: helper copy failed"; exit 1; }
echo "prerun: files overlay applied ($(ls "$ROOTFS_DIR/opt/mrrc_modern" | wc -l) code entries)"
