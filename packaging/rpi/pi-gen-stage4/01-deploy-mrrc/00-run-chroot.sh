#!/bin/bash -e
# Runs INSIDE the arm64 rootfs. pi-gen feeds this file to
# `capsh --chroot=… -- -e`, which execs /bin/bash -e with the script on stdin —
# so the shebang above is a comment, $BASH_SOURCE is not a path, and `set -u` is
# unsafe (a chroot has no login environment: `USER` and friends are simply absent).
#
# Everything that is not Raspberry-Pi-specific lives in the shared service layer;
# this file only creates the conditions that layer assumes.
set -eo pipefail

MRRC_HOME=/opt/mrrc_modern
ASSETS=/opt/mrrc-build

[ -x "$ASSETS/mrrc-service-layer.sh" ] || {
	echo "FATAL: the shared service layer is not staged at $ASSETS" >&2
	exit 1
}
[ -f "$ASSETS/build.env" ] || { echo "FATAL: $ASSETS/build.env is missing" >&2; exit 1; }

# Version, bind address, boot partition and the three mirrors: all generated on
# the host by build-image.sh, because pi-gen's chroot does not inherit the host
# environment. `set -a` exports them for pip and fetch-frpc.sh as well.
set -a
. "$ASSETS/build.env"
set +a

# pi-gen's FIRST_USER_NAME=mrrc created the account; the service layer assumes it
# exists and never creates it. dialout: /dev/ttyUSB*/ttyACM*. audio: the radio's
# USB sound card.
usermod -aG dialout,audio mrrc
chown -R mrrc:mrrc "$MRRC_HOME"
systemctl enable ssh

exec bash "$ASSETS/mrrc-service-layer.sh"
