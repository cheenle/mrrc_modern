#!/bin/bash -e
# Runs INSIDE the Armbian rootfs (chroot), as root.
#
# Every step is idempotent: a build that fails halfway can be re-run without
# starting from the downloaded image again.
#
# What is deliberately NOT here: anything needing hardware. The web password,
# the serial port, the USB sound card and the self-signed certificate are all
# resolved on the box's first boot by mrrc-firstboot (design §3.3). Baking a
# guess in would produce a configuration that looks usable and cannot work.
set -euo pipefail

REPO_SRC="${REPO_SRC:-/src}"
MRRC_HOME=/opt/mrrc_modern
MRRC_USER=mrrc
VENV="$MRRC_HOME/venv"
ENV_DIR="$MRRC_HOME/env"
ENV_FILE="$ENV_DIR/mrrc.env"

log() { printf '\n=== %s ===\n' "$*"; }

log "user and groups"
if ! id -u "$MRRC_USER" >/dev/null 2>&1; then
  adduser --system --group --home "$MRRC_HOME" --shell /bin/bash "$MRRC_USER"
fi
# dialout: /dev/ttyUSB* and /dev/ttyACM*; audio: the radio's USB sound card.
for g in dialout audio; do
  getent group "$g" >/dev/null 2>&1 || groupadd --system "$g"
  adduser "$MRRC_USER" "$g" >/dev/null
done

log "source tree (the Pi builder's exclusion list, kept byte-identical)"
# packaging/ is excluded, so the profiles are copied separately below — they
# are deployment data that happens to live under packaging/.
mkdir -p "$MRRC_HOME"
rsync -a "$REPO_SRC/" "$MRRC_HOME/" \
  --exclude ".git/" --exclude "venv/" --exclude ".venv/" --exclude "dist/" \
  --exclude "build/" --exclude "logs/" --exclude "certs/" --exclude "promo/" \
  --exclude "FT710Mobile/" --exclude "FT710Android/" --exclude "website/" \
  --exclude "tests/" --exclude ".agents/" --exclude "docs/" --exclude "SDD/" \
  --exclude "packaging/" --exclude "macos/" --exclude "windows/" \
  --exclude "__pycache__/" --exclude "*.pyc" --exclude ".DS_Store" \
  --exclude "atr1000_tuner.json"

log "radio profiles"
mkdir -p "$MRRC_HOME/profiles"
cp "$REPO_SRC"/packaging/box/profiles/*.env "$MRRC_HOME/profiles/"

log "build prerequisites the base image does not carry"
# The design assumed this image already had python3-venv, pip, the PortAudio
# and ALSA headers and libopus0, with apt adding "approximately nothing".
# Measured on the pinned image, every one of them is absent (only libasound2,
# gcc, make, rsync, git, curl and sudo are there).
#
# install.sh cannot repair this itself: it creates the virtualenv in STEP 2 and
# installs system packages in STEP 3, so a missing ensurepip is fatal before apt
# ever runs — and a venv created without it has no pip, which fails the
# dependency install further down and ships an image that cannot start.
#
# libopus0 is the one that is easy to miss and expensive to miss: without it the
# server silently falls back to PCM instead of Opus.
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  python3.11-venv python3-dev \
  portaudio19-dev libportaudio2 libasound2-dev \
  libopus0 libopus-dev

log "python environment (install.sh owns the dependency list)"
cd "$MRRC_HOME"
./install.sh --yes --install-service --no-scope || {
  echo "box-overlay: install.sh failed" >&2; exit 1;
}
# install.sh writes an env file next to the source tree; the unit reads
# $ENV_FILE instead, so move it there rather than maintaining two env files.
if [ -f "$MRRC_HOME/.env" ] && [ ! -f "$ENV_FILE" ]; then
  mkdir -p "$ENV_DIR"
  mv "$MRRC_HOME/.env" "$ENV_FILE"
fi
chown -R "$MRRC_USER:$MRRC_USER" "$MRRC_HOME"
chmod 640 "$ENV_FILE" 2>/dev/null || true

log "FTDI scope libraries (aarch64, vendored in the repo)"
install -d "$MRRC_HOME/vendor/ftdi"
install -m 0755 "$REPO_SRC/vendor/ftdi/libft4222.so" "$MRRC_HOME/vendor/ftdi/libft4222.so"
# The second name is the same ELF: FTDI's Linux libft4222 links D2XX in
# statically and re-exports its symbols, so both of find_ftdi_libraries()'s
# slots can be this one file. See packaging/box/vendor-ftdi.md.
ln -sf libft4222.so "$MRRC_HOME/vendor/ftdi/libftd2xx.so"

log "frpc (Cloud Hub tunnel client)"
install -d "$MRRC_HOME/fleet"
bash "$REPO_SRC/packaging/box/fetch-frpc.sh" "$MRRC_HOME/fleet"

log "mrrc-radio, the updater, and the password helper"
install -m 0755 "$MRRC_HOME/linux/mrrc_radio.py" /usr/local/bin/mrrc-radio
install -m 0755 "$MRRC_HOME/linux/mrrc_update.sh" /usr/local/bin/mrrc-update
# The Pi image's helper, unchanged: the web password lives in a 0640 root file
# and this is how an operator reads it back after the console banner is gone.
install -m 0755 "$REPO_SRC/packaging/rpi/pi-gen-stage4/01-deploy-mrrc/files/usr/local/bin/mrrc-show-password" \
  /usr/local/bin/mrrc-show-password

log "systemd units"
cat > /etc/systemd/system/mrrc-modern.service <<'UNIT'
[Unit]
Description=MRRC Modern web control server
After=network-online.target
Wants=network-online.target

[Service]
User=mrrc
Group=mrrc
WorkingDirectory=/opt/mrrc_modern
EnvironmentFile=/opt/mrrc_modern/env/mrrc.env
# Same file, so Cloud Hub's connect() writes where the service will read
# (design D-11): it persists the certificate paths and the TX heartbeat, and a
# split between these two lines is silent — the UI reports 已连接 while upstream
# TLS verification fails.
Environment=MRRC_CONFIG_FILE=/opt/mrrc_modern/env/mrrc.env
ExecStart=/opt/mrrc_modern/venv/bin/python server.py
Restart=on-failure
RestartSec=3
# The default KillMode=control-group also stops the frpc child. That matters
# on Linux: frpc's stale-process sweep in cloud_hub.py is Windows-only, and a
# leftover frpc holds the proxy name so the next start cannot register it.
StandardOutput=append:/opt/mrrc_modern/logs/server-stdout.log
StandardError=append:/opt/mrrc_modern/logs/server-stdout.log

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/mrrc-firstboot.service <<'UNIT'
[Unit]
Description=MRRC Modern first-boot auto-configuration
After=network-online.target
Wants=network-online.target
ConditionPathExists=!/var/lib/mrrc/firstboot-done

[Service]
Type=oneshot
ExecStart=/opt/mrrc_modern/venv/bin/python /opt/mrrc_modern/linux/firstboot_wrapper.py
StandardOutput=console
StandardError=journal
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

mkdir -p "$MRRC_HOME/linux" "$MRRC_HOME/logs" /var/lib/mrrc/certs
install -m 0755 "$REPO_SRC/packaging/box/firstboot_wrapper.py" \
  "$MRRC_HOME/linux/firstboot_wrapper.py"
chown -R "$MRRC_USER:$MRRC_USER" /var/lib/mrrc "$MRRC_HOME/logs"

log "version stamp"
# version.txt is what the support bundle manifest and the upgrade channel read
# out of an installed tree, so an image without it fails those two things while
# still looking complete. build-image.sh passes the value it read from the
# CHANGELOG; the fallback keeps this script usable on its own.
version="${MRRC_VERSION:-$(grep -m1 -oE '## \[v[0-9]+\.[0-9]+\.[0-9]+\]' "$REPO_SRC/CHANGELOG.md" \
  | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+')}"
: "${version:?Could not read a version from CHANGELOG.md}"
printf '%s\n' "${version#v}" > "$MRRC_HOME/version.txt"
cp "$MRRC_HOME/version.txt" "$MRRC_HOME/VERSION"
chown "$MRRC_USER:$MRRC_USER" "$MRRC_HOME/version.txt" "$MRRC_HOME/VERSION"
echo "version: ${version#v}"

log "headless defaults (only keys that are not already set)"
touch "$ENV_FILE"
grep -q '^MRRC_WEB_HOST='          "$ENV_FILE" || echo 'MRRC_WEB_HOST=0.0.0.0'      >> "$ENV_FILE"
grep -q '^MRRC_WEB_PORT='          "$ENV_FILE" || echo 'MRRC_WEB_PORT=8888'         >> "$ENV_FILE"
# Second PTT defence (design D-4): connect() writes the 5 s heartbeat gate; the
# duration ceiling is off by default and has to be asked for.
grep -q '^MRRC_PTT_MAX_TX_SECONDS=' "$ENV_FILE" || echo 'MRRC_PTT_MAX_TX_SECONDS=120' >> "$ENV_FILE"
grep -q '^MRRC_SSL_CERT='          "$ENV_FILE" || echo 'MRRC_SSL_CERT=/var/lib/mrrc/certs/server.crt' >> "$ENV_FILE"
grep -q '^MRRC_SSL_KEY='           "$ENV_FILE" || echo 'MRRC_SSL_KEY=/var/lib/mrrc/certs/server.key'  >> "$ENV_FILE"
chown "$MRRC_USER:$MRRC_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

log "enable services"
systemctl enable mrrc-firstboot.service mrrc-modern.service
# Amlogic vendor images ship a getty on a vendor-only FIQ console; the mainline
# kernel does not have it and the unit would wait 90 s per boot.
systemctl mask serial-getty@ttyFIQ0.service || true

log "reclaim space (the rootfs is a 3 GB partition with ~960 MB free)"
apt-get clean
rm -rf /var/lib/apt/lists/* "$MRRC_HOME/venv/.cache" /root/.cache
# /tmp held ~12 MiB of install.sh validation scratch when this build first ran,
# and nothing else clears it, so it would otherwise ship inside the image.
rm -rf /tmp/* /tmp/.[!.]* 2>/dev/null || true
find "$MRRC_HOME" -name '__pycache__' -type d -prune -exec rm -rf {} +

log "overlay done"
du -sh "$MRRC_HOME"
