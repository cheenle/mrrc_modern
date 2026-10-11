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
# detected serial port and a password it generated there. On a device both are
# first-boot facts (design D-3; the web password is generated on the device and
# read back with mrrc-show-password) — strip them so first_run does its job
# instead of trusting the build machine.
sed -i -e '/^#\?MRRC_SERIAL_PORT=/d' -e '/^#\?MRRC_WEB_PASSWORD=/d' "$ENV_FILE"
chown -R "$MRRC_USER:$MRRC_USER" "$MRRC_HOME"
chmod 640 "$ENV_FILE" 2>/dev/null || true

log "FTDI scope libraries (aarch64, vendored in the repo)"
install -d "$MRRC_HOME/vendor/ftdi"
install -m 0755 "$MRRC_HOME/vendor/ftdi/libft4222.so" "$MRRC_HOME/vendor/ftdi/libft4222.so"
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
cat > /etc/udev/rules.d/99-mrrc-ftdi.rules <<'RULE'
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
systemctl enable mrrc-firstboot.service mrrc-modern.service

log "import gate: the entry points the units run must import from THIS tree"
# The two failure modes this catches both shipped once (2026-10-10, the 1.25.4
# box image): server.py imported macos.first_run, which the exclusion list drops
# on purpose, and the installed mrrc-radio resolved its tree from __file__. Both
# die before any log line and neither is visible to a syntax check. Runs before
# the reclaim step so a stray __pycache__ is cleaned up with the rest.
(
	cd "$MRRC_HOME"
	PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" -c "import server; import sys; sys.path.insert(0, '$MRRC_HOME'); sys.path.insert(0, '$MRRC_HOME/linux'); import net_wifi, box_ops; import first_run, mrrc_radio"
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
