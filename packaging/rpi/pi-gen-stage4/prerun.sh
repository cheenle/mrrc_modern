#!/bin/bash -e
# pi-gen stage prerun: carry the previous stage's rootfs into this stage.
# (Standard pi-gen helper; without it the stage has no rootfs to chroot into.)
if [ ! -d "$ROOTFS_DIR" ]; then
    copy_previous
fi

# Inject the MRRC Modern code tree (staged by build-image.sh under
# 01-deploy-mrrc/files/opt/mrrc_modern). This pi-gen version has no
# automatic files/ copy, so prerun does it explicitly.
mkdir -p "$ROOTFS_DIR/opt/mrrc_modern"
cp -a "$STAGE_DIR"/01-deploy-mrrc/files/opt/mrrc_modern/. "$ROOTFS_DIR/opt/mrrc_modern/"
