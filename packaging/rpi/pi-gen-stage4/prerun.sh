#!/bin/bash -e
# pi-gen stage prerun: carry the previous stage's rootfs into this stage, then
# overlay this stage's files/ tree onto it. This pi-gen version has no automatic
# files/ copy, and cwd is the stage dir (build.sh pushd's before calling prerun).
if [ ! -d "$ROOTFS_DIR" ]; then
	copy_previous
fi

cp -a 01-deploy-mrrc/files/. "$ROOTFS_DIR/"

# What build-image.sh must have staged. Checked here rather than assumed: a
# missing code tree fails at first boot with an ImportError, and a missing asset
# directory fails two steps later inside the service layer with a message about
# the wrong thing.
for f in opt/mrrc_modern/requirements.txt opt/mrrc_modern/version.txt \
         opt/mrrc-build/mrrc-service-layer.sh opt/mrrc-build/build.env \
         opt/mrrc-build/packages.txt; do
	[ -e "$ROOTFS_DIR/$f" ] || {
		echo "FATAL: $f missing — packaging/rpi/build-image.sh stages it" >&2
		exit 1
	}
done
echo "prerun: staged $(find "$ROOTFS_DIR/opt/mrrc_modern" -type f | wc -l) code files"
