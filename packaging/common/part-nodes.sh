#!/usr/bin/env bash
# Materialise the partition device nodes for a loop device, inside a container.
#
# `losetup -P` asks the kernel to scan the partition table, but the kernel only
# publishes entries under /sys/class/block/loopNpM — creating the /dev nodes is
# udev's job, and a container has no udev (Docker mounts a tmpfs over /dev, so
# devtmpfs entries are hidden and new ones cannot appear).
#
# Without this, ${loop}p2 does not exist and every mount of a partitioned image
# dies with:
#
#     mount: /mnt: special device /dev/loop1p2 does not exist.
#
# sysfs carries each partition's major:minor, so mint the nodes from it.
#
# Usage:  . part-nodes.sh ; part_nodes /dev/loop1
#
# Note this is specific to attaching an *image file*. Mounting an already
# attached block device with a real partition table is unaffected, which is why
# a partitionless loop test passes while this is broken.
part_nodes() {
  local loop="$1" part name major_minor
  for part in /sys/class/block/"$(basename "$loop")"p*; do
    [ -e "$part" ] || continue
    name="$(basename "$part")"
    major_minor="$(cat "$part/dev")"
    mknod "/dev/$name" b "${major_minor%:*}" "${major_minor#*:}"
  done
}
