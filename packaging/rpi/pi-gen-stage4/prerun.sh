#!/bin/bash -e
# pi-gen stage prerun: carry the previous stage's rootfs into this stage.
# (Standard pi-gen helper; without it the stage has no rootfs to chroot into.)
if [ ! -d "$ROOTFS_DIR" ]; then
    copy_previous
fi
