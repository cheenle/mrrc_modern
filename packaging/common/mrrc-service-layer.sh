#!/bin/bash -e
# The MRRC Modern service layer: everything that turns a bare rootfs into an
# appliance, for both image lines. Runs INSIDE the target rootfs as root.
#
# Every step is idempotent: a build that failed halfway can be re-run.
#
# Inputs (environment; each has a default so this script is runnable by hand):
#   MRRC_ASSETS       this layer's own assets          (default /opt/mrrc-build)
#   MRRC_HOME         install location                 (default /opt/mrrc_modern)
#   MRRC_USER         service account, must already exist (default mrrc)
#   MRRC_VERSION      version.txt / VERSION, or a CHANGELOG.md to read it from
#   MRRC_WEB_HOST     bind address written to the env file (default 0.0.0.0; the
#                     Pi passes :: — the one bind divergence, asserted in
#                     tests/test_packaging_parity.py)
#   MRRC_BOOT_DIR     boot partition a preseed mrrc.env may sit on (default /boot;
#                     the Pi passes /boot/firmware). Written into the unit; only
#                     firstboot_wrapper.py reads it.
#   REPO_SRC          the tree the caller staged from — used ONLY to find
#                     CHANGELOG.md when MRRC_VERSION is unset (default /src)
#   PIP_INDEX_URL     honoured by pip itself; each builder exports it
#
# Deliberately NOT here: anything needing hardware. The web password, the serial
# port, the USB sound card and the self-signed certificate are all resolved on
# the device's first boot (design D-3). Baking a guess in produces a
# configuration that looks usable and cannot work.
#
# Deliberately NOT here either: the board- and distro-specific bits (the W103D's
# MT7663S firmware, its FIQ/dnsmasq masks, whether the account comes from adduser
# or from pi-gen's FIRST_USER_NAME, that the Pi installs e2fsprogs through
# pi-gen's own package list). Those live in the platform shells, which call this.
set -euo pipefail

MRRC_ASSETS="${MRRC_ASSETS:-/opt/mrrc-build}"
MRRC_HOME="${MRRC_HOME:-/opt/mrrc_modern}"
MRRC_USER="${MRRC_USER:-mrrc}"
MRRC_WEB_HOST="${MRRC_WEB_HOST:-0.0.0.0}"
MRRC_BOOT_DIR="${MRRC_BOOT_DIR:-/boot}"
REPO_SRC="${REPO_SRC:-/src}"
VENV="$MRRC_HOME/venv"
ENV_DIR="$MRRC_HOME/env"
ENV_FILE="$ENV_DIR/mrrc.env"

log() { printf '\n=== %s ===\n' "$*"; }

log "preconditions"
id -u "$MRRC_USER" >/dev/null 2>&1 || {
	echo "mrrc-service-layer: no '$MRRC_USER' account. The platform shell creates" \
		"it (adduser --system on the box, pi-gen's FIRST_USER_NAME on the Pi)." >&2
	exit 1
}
# The tree is staged by the caller (stage-tree.sh); this is the check that turns
# "the tree was not staged" into a build-time error rather than a boot-time
# ModuleNotFoundError.
[ -f "$MRRC_HOME/requirements.txt" ] || {
	echo "mrrc-service-layer: no code tree at $MRRC_HOME — the caller must stage it" \
		"with packaging/common/stage-tree.sh first" >&2
	exit 1
}
[ -f "$MRRC_ASSETS/packages.txt" ] || {
	echo "mrrc-service-layer: assets missing at $MRRC_ASSETS (packages.txt)" >&2
	exit 1
}

log "system packages"
apt-get update -qq
# Word splitting is the point: packages.txt is one name per line and the list
# goes to apt as a single transaction.
APT_PACKAGES="$(grep -vE '^[[:space:]]*(#|$)' "$MRRC_ASSETS/packages.txt" | tr '\n' ' ')"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends $APT_PACKAGES

log "radio profiles"
# Deployment data that happens to live under packaging/: the model switcher
# (mrrc-radio) reads <MRRC_HOME>/profiles, and both images get the same eleven.
# The exclusion list drops packaging/ from the staged tree, so these come from
# the assets directory instead.
mkdir -p "$MRRC_HOME/profiles"
cp "$MRRC_ASSETS"/profiles/*.env "$MRRC_HOME/profiles/"

log "python environment (install.sh owns the dependency list)"
cd "$MRRC_HOME"
./install.sh --yes --install-service --no-scope || {
	echo "mrrc-service-layer: install.sh failed" >&2
	exit 1
}
# install.sh writes an env file next to the source tree; the unit reads
# $ENV_FILE instead, so move it there rather than maintaining two env files.
if [ -f "$MRRC_HOME/.env" ] && [ ! -f "$ENV_FILE" ]; then
	mkdir -p "$ENV_DIR"
	mv "$MRRC_HOME/.env" "$ENV_FILE"
fi
# install.sh's template is written on the *build* host: it carries that machine's
# detected serial port, a password it generated there, and a hard-coded
# `MRRC_WEB_HOST=0.0.0.0`. On a device all three are wrong or premature (design
# D-3; the web password is generated on the device and read back with
# mrrc-show-password), and the bind address is a platform parameter — leaving the
# template's value in place silently overrode it, which is how the first
# refactored Pi image ended up IPv4-only while its own unit said `::` (caught by
# the four-step artifact verification, 2026-10-11). Strip them so the defaults
# block below is the only writer.
#
# Note for a re-run against a live box: this resets an operator's own
# MRRC_WEB_HOST to the platform default. That is the same deal the other two
# keys already make, and this script is a build-time tool rather than an updater
# (linux/mrrc_update.sh stages the tree and never runs it).
sed -i -e '/^#\?MRRC_SERIAL_PORT=/d' -e '/^#\?MRRC_WEB_PASSWORD=/d' \
	-e '/^#\?MRRC_WEB_HOST=/d' "$ENV_FILE"

log "FT8 DSP library (native, built from the tree that was just staged)"
# The FT8 work mode reaches libwsjt_core.so through ctypes, and this is where
# an image gets it.  Building here instead of shipping a prebuilt .so keeps the
# library provably in step with the sources: the tree is right there, and the
# alternative — a binary committed under vendor/ — is a second thing to keep
# up to date by hand.
#
# build.sh is idempotent by source digest, so the same call is also what
# linux/mrrc_update.sh makes after an update (no-op unless ft8/dsp moved).
# Measured 2026-10-13: 7.7 s with 9 jobs on the build host, ~2 minutes on the
# W103D itself (docs/ft8-m1-baseline.md).
bash "$MRRC_HOME/ft8/dsp/build.sh"
[ -f "$MRRC_HOME/ft8/dsp/build/libwsjt_core.so" ] || {
	echo "mrrc-service-layer: ft8/dsp/build.sh produced no libwsjt_core.so —" \
		"the FT8 work mode would come up as \"library missing\" on this image" >&2
	exit 1
}

chown -R "$MRRC_USER:$MRRC_USER" "$MRRC_HOME"
chmod 640 "$ENV_FILE" 2>/dev/null || true

log "FTDI scope libraries (aarch64, vendored in the repo)"
# The staged tree already carries them (vendor/ is not excluded) with the mode
# git recorded, so this is a check plus a mode, not a copy: `install SRC DST`
# with SRC == DST fails outright ("are the same file"), which is what a first
# run of this layer did on 2026-10-11 — the file it was copying had already
# been staged into place. The check is the part that matters: a repo without
# the vendored pair must fail the build, not ship an image whose real FFT
# silently falls back to the S-meter.
[ -f "$MRRC_HOME/vendor/ftdi/libft4222.so" ] || {
	echo "mrrc-service-layer: vendor/ftdi/libft4222.so missing from the tree — the" \
		"FT-710's real FFT needs it (see packaging/box/vendor-ftdi.md)" >&2
	exit 1
}
chmod 0755 "$MRRC_HOME/vendor/ftdi/libft4222.so"
# The second name is the same ELF: FTDI's Linux libft4222 links D2XX in
# statically and re-exports its symbols, so both of find_ftdi_libraries()'s slots
# can be this one file. See packaging/box/vendor-ftdi.md.
ln -sf libft4222.so "$MRRC_HOME/vendor/ftdi/libftd2xx.so"

log "udev rule: the FT4222 scope interface belongs to the service user"
# libft4222 opens the USB device directly — the FT4222 has no /dev node of its
# own — and it does so as `mrrc`. Without this rule the node is root:root 0664,
# every scope open fails with FTDI error 2 ("device not opened") and the real FFT
# never starts, while CAT and audio keep working: the most confusing half-failure
# this feature can have (found on the first real box, 2026-10-11 — root ran the
# scope pipe fine, mrrc could not). dialout is the group both images put mrrc in
# for ttyUSB*. The Pi needs this rule for the same reason the box does: the same
# FT4222 hangs off its USB port.
cat >/etc/udev/rules.d/99-mrrc-ftdi.rules <<'RULE'
SUBSYSTEM=="usb", ATTR{idVendor}=="0403", ATTR{idProduct}=="601c", MODE="0660", GROUP="dialout"
RULE

log "frpc (Cloud Hub tunnel client)"
install -d "$MRRC_HOME/fleet"
bash "$MRRC_ASSETS/fetch-frpc.sh" "$MRRC_HOME/fleet"

log "mrrc-radio, the updater, the password helper and the login banner"
install -m 0755 "$MRRC_HOME/linux/mrrc_update.sh" /usr/local/bin/mrrc-update
# mrrc-radio is a wrapper, not a copy of the module. linux/mrrc_radio.py has no
# shebang (it is a module the tests import, and the interpreter that has pyserial
# and the backend package is the venv, not the system python3), so installing the
# .py itself as /usr/local/bin/mrrc-radio handed a Python file to /bin/sh —
# "Syntax error: ( unexpected" on every invocation. A copy would also go stale
# the moment mrrc-update replaced the tree, since it does not re-install
# /usr/local/bin. The tree stays the single source.
cat >/usr/local/bin/mrrc-radio <<'WRAPPER'
#!/bin/sh
exec /opt/mrrc_modern/venv/bin/python /opt/mrrc_modern/linux/mrrc_radio.py "$@"
WRAPPER
chmod 0755 /usr/local/bin/mrrc-radio
install -m 0755 "$MRRC_ASSETS/mrrc-show-password" /usr/local/bin/mrrc-show-password
install -d /etc/update-motd.d
install -m 0755 "$MRRC_ASSETS/motd-10-mrrc" /etc/update-motd.d/10-mrrc

log "polkit: let the server user reconfigure NetworkManager (design §7.2)"
# /etc/polkit-1/rules.d/ beats /usr/share/polkit-1/rules.d/, and a distro
# upgrade package does not overwrite what is written there.
install -d -m 0755 /etc/polkit-1/rules.d
install -m 0644 "$MRRC_ASSETS/49-mrrc-nm.rules" \
	/etc/polkit-1/rules.d/49-mrrc-nm.rules

log "systemd units"
cat >/etc/systemd/system/mrrc-modern.service <<UNIT
[Unit]
Description=MRRC Modern web control server
After=network-online.target
Wants=network-online.target

[Service]
User=$MRRC_USER
Group=$MRRC_USER
WorkingDirectory=$MRRC_HOME
EnvironmentFile=$ENV_FILE
# Same file, so Cloud Hub's connect() writes where the service will read
# (design D-11): it persists the certificate paths and the TX heartbeat, and a
# split between these two lines is silent — the UI reports 已连接 while upstream
# TLS verification fails.
Environment=MRRC_CONFIG_FILE=$ENV_FILE
ExecStart=$VENV/bin/python server.py
Restart=on-failure
RestartSec=3
# The default KillMode=control-group also stops the frpc child. That matters on
# Linux: frpc's stale-process sweep in cloud_hub.py is Windows-only, and a
# leftover frpc holds the proxy name so the next start cannot register it.
StandardOutput=append:$MRRC_HOME/logs/server-stdout.log
StandardError=append:$MRRC_HOME/logs/server-stdout.log

[Install]
WantedBy=multi-user.target
UNIT

cat >/etc/systemd/system/mrrc-firstboot.service <<UNIT
[Unit]
Description=MRRC Modern first-boot auto-configuration
After=network-online.target
Wants=network-online.target
ConditionPathExists=!/var/lib/mrrc/firstboot-done

[Service]
Type=oneshot
# The two platform facts firstboot_wrapper.py cannot guess: where a preseed
# mrrc.env would be, and which address the server binds.
Environment=MRRC_BOOT_DIR=$MRRC_BOOT_DIR
Environment=MRRC_WEB_HOST=$MRRC_WEB_HOST
ExecStart=$VENV/bin/python $MRRC_HOME/linux/firstboot_wrapper.py
StandardOutput=journal+console
StandardError=journal
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

cat >/etc/systemd/system/mrrc-box-helper.service <<'UNIT'
[Unit]
Description=MRRC Modern box helper (the four system verbs the web pages may ask for)
# NetworkManager first only because the pages that use this helper also use NM:
# nothing here talks to it. No network-online.target — this must be up when
# there is no network, which is exactly when an operator is configuring one.
After=NetworkManager.service

[Service]
Type=simple
# Root for hostnamectl / timedatectl / systemctl reboot. Group=mrrc is what makes
# systemd create RuntimeDirectory=/run/mrrc owned by root:mrrc, so the socket
# inside it can be 0660 root:mrrc without this script chowning anything — /run is
# tmpfs, so the directory is recreated clean at every boot.
User=root
Group=mrrc
RuntimeDirectory=mrrc
RuntimeDirectoryMode=0770
ExecStart=/opt/mrrc_modern/venv/bin/python /opt/mrrc_modern/linux/box_helper.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=mrrc-box-helper

[Install]
WantedBy=multi-user.target
UNIT

# 未加引号的 heredoc：下面两处要把 $MRRC_BOOT_DIR 插进去。这是**有意差异 ②**的一部分
# ——Armbian 把启动分区挂在 /boot，Raspberry Pi OS 挂在 /boot/firmware。写死路径的后果不是
# 报错，而是 ConditionPathExists 永不成立、这条能力在 Pi 上**静默哑掉**（2026-10-12 抓到的）。
cat >/etc/systemd/system/mrrc-wifi-preseed.service <<UNIT
[Unit]
Description=MRRC Modern Wi-Fi preseed ($MRRC_BOOT_DIR/mrrc-wifi.txt, applied once)
# NetworkManager, not network-online.target: joining this Wi-Fi is what brings
# the network online, so waiting for it would be a circle (design §9.2, R-8).
After=NetworkManager.service
Wants=NetworkManager.service
ConditionPathExists=$MRRC_BOOT_DIR/mrrc-wifi.txt
# Before firstboot, which *does* need the network: it is what fetches nothing
# today but is ordered on network-online.target, and it is the unit that decides
# whether this box has ever been configured.
Before=mrrc-firstboot.service

[Service]
Type=oneshot
# Root because it is the only thing running this early and nmcli's system
# connections are root's until the polkit rule is loaded. The file is deleted on
# success, so a second boot runs nothing at all.
# Same platform fact the ConditionPathExists above uses, handed to the script so
# the two cannot disagree about where the file is.
Environment=MRRC_BOOT_DIR=$MRRC_BOOT_DIR
ExecStart=/opt/mrrc_modern/venv/bin/python /opt/mrrc_modern/linux/wifi_preseed.py
StandardOutput=journal+console
StandardError=journal+console
SyslogIdentifier=mrrc-wifi-preseed

[Install]
WantedBy=multi-user.target
UNIT

mkdir -p "$MRRC_HOME/linux" "$MRRC_HOME/logs" /var/lib/mrrc/certs
install -m 0755 "$MRRC_ASSETS/firstboot_wrapper.py" \
	"$MRRC_HOME/linux/firstboot_wrapper.py"
chown -R "$MRRC_USER:$MRRC_USER" /var/lib/mrrc "$MRRC_HOME/logs"

log "version stamp"
# version.txt is what the support bundle manifest and the upgrade channel read
# out of an installed tree, so an image without it fails those two things while
# still looking complete. The Pi builder passes the value it read from the
# CHANGELOG in build.env; the fallback below keeps this script usable on its own
# (the installed tree has no CHANGELOG.md — the exclusion list drops /*.md).
version="${MRRC_VERSION:-}"
if [ -z "$version" ] && [ -f "$REPO_SRC/CHANGELOG.md" ]; then
	version="$(grep -m1 -oE '## \[v[0-9]+\.[0-9]+\.[0-9]+\]' "$REPO_SRC/CHANGELOG.md" |
		grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+')"
fi
: "${version:?Could not determine a version (set MRRC_VERSION or ship CHANGELOG.md)}"
printf '%s\n' "${version#v}" >"$MRRC_HOME/version.txt"
cp "$MRRC_HOME/version.txt" "$MRRC_HOME/VERSION"
chown "$MRRC_USER:$MRRC_USER" "$MRRC_HOME/version.txt" "$MRRC_HOME/VERSION"
echo "version: ${version#v}"

log "box identity: the image must not ship as 'armbian' (design §8)"
# static — not --pretty: the pretty hostname is the display name, the static one
# is what avahi advertises and what DHCP option 12 carries, and only the static
# one needs to be a legal hostname.
#
# Write the file first: the box builds this in a chroot with systemd not running
# (build-image.sh), where hostnamectl is a D-Bus client and fails. The
# hostnamectl line stays for live systems, where it also updates the pretty and
# transient names.
echo "MRRC-Modern" >/etc/hostname
hostnamectl set-hostname --static MRRC-Modern || true
# hostnamectl stopped touching /etc/hosts in systemd 240. Debian's own image
# carries a 127.0.1.1 line naming the old hostname, and `hostname -f` (which
# sudo and some mailers use) goes through it.
if grep -q '^127\.0\.1\.1' /etc/hosts; then
	sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tMRRC-Modern/' /etc/hosts
else
	printf '127.0.1.1\tMRRC-Modern\n' >>/etc/hosts
fi

log "headless defaults (only keys that are not already set)"
touch "$ENV_FILE"
grep -q '^MRRC_WEB_HOST=' "$ENV_FILE" || echo "MRRC_WEB_HOST=$MRRC_WEB_HOST" >>"$ENV_FILE"
grep -q '^MRRC_WEB_PORT=' "$ENV_FILE" || echo 'MRRC_WEB_PORT=8888' >>"$ENV_FILE"
# Second PTT defence (design D-4): connect() writes the 5 s heartbeat gate; the
# duration ceiling is off by default and has to be asked for.
grep -q '^MRRC_PTT_MAX_TX_SECONDS=' "$ENV_FILE" || echo 'MRRC_PTT_MAX_TX_SECONDS=120' >>"$ENV_FILE"
# The radio's USB-audio capture needs a deeper host buffer than the desktops:
# ALSA hands a blocking stream ~2 periods of 20 ms, and an overflow stops the
# capture interface silently (measured on the first real box, 2026-10-11). The
# code default is the same value on Linux; writing it here makes the knob visible
# in the env file an operator actually edits.
grep -q '^MRRC_RX_BUFFER_MS=' "$ENV_FILE" || echo 'MRRC_RX_BUFFER_MS=100' >>"$ENV_FILE"
grep -q '^MRRC_SSL_CERT=' "$ENV_FILE" || echo 'MRRC_SSL_CERT=/var/lib/mrrc/certs/server.crt' >>"$ENV_FILE"
grep -q '^MRRC_SSL_KEY=' "$ENV_FILE" || echo 'MRRC_SSL_KEY=/var/lib/mrrc/certs/server.key' >>"$ENV_FILE"
chown "$MRRC_USER:$MRRC_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

log "enable services"
systemctl enable mrrc-box-helper.service mrrc-wifi-preseed.service mrrc-firstboot.service mrrc-modern.service
# avahi-daemon is what answers <hostname>.local. It ships enabled in the
# package, but this image masks units by default in several places, so say it
# out loud rather than trusting the package's postinst (design §8).
systemctl enable avahi-daemon.service || true

log "import gate: the entry points the units run must import from THIS tree"
# The two failure modes this catches both shipped once (2026-10-10, the 1.25.4
# box image): server.py imported macos.first_run, which the exclusion list drops
# on purpose, and the installed mrrc-radio resolved its tree from __file__. Both
# die before any log line and neither is visible to a syntax check. Runs before
# the reclaim step so a stray __pycache__ is cleaned up with the rest.
(
	cd "$MRRC_HOME"
	PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" -c "import server; import sys; sys.path.insert(0, '$MRRC_HOME'); sys.path.insert(0, '$MRRC_HOME/linux'); import net_wifi, box_ops; import first_run, mrrc_radio, wifi_preseed, box_helper"
) || {
	echo "mrrc-service-layer: an image entry point cannot import from this tree" >&2
	exit 1
}
echo "entry points import OK"

log "reclaim space"
apt-get clean
rm -rf /var/lib/apt/lists/* "$VENV/.cache" /root/.cache
# install.sh leaves validation scratch in /tmp, and the assets directory is a
# build-only copy: neither may ship. (On the Pi /tmp is a tmpfs during the build
# anyway, but /tmp is a real directory on the box, and MRRC_ASSETS is real on
# both — a leftover there would ship the overlay's own source.)
rm -rf /tmp/* /tmp/.[!.]* 2>/dev/null || true
rm -rf "$MRRC_ASSETS"
find "$MRRC_HOME" -name '__pycache__' -type d -prune -exec rm -rf {} +

log "overlay done"
du -sh "$MRRC_HOME"
