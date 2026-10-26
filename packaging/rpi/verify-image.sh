#!/usr/bin/env bash
# Four-step verification of a built rpi64 image, against the REAL artifact.
#
# Same recipe as docs/w103d_pack.md: the .img is an MBR disk with p1 = boot
# FAT32 and p2 = root ext4, macOS cannot loop-mount ext4, and the image is arm64
# like this host, so a privileged arm64 container can mount it and even chroot
# into it natively. util-linux plus coreutils is all it needs — no apt, which
# matters because apt is the slowest thing on this network.
#
#   1. code identity — the image's tree is this checkout's tree
#   2. installed     — profiles, CLIs, frpc, FTDI libs, account, no leftovers
#   3. D-11 wiring   — EnvironmentFile and MRRC_CONFIG_FILE are the same file
#   4. it runs       — chroot the artifact and import every dependency + libopus
#
# The inner script travels as a file rather than as a `bash -c '…'` argument:
# one apostrophe anywhere in the body (a word like "image's") closes that quoted
# string and the rest of the script is never parsed — shipped once on
# 2026-10-11, in packaging/box/build-image.sh.
#
# usage: packaging/rpi/verify-image.sh dist/rpi/MRRC-Modern-v1.25.5-rpi64.img.xz
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
IMG_XZ="${1:?usage: verify-image.sh <img.xz>}"
[ -f "$IMG_XZ" ] || {
	echo "no such image: $IMG_XZ" >&2
	exit 1
}
IMG_XZ="$(cd "$(dirname "$IMG_XZ")" && pwd)/$(basename "$IMG_XZ")"

# Decompress on the host, not in the container: the artifact is .xz (the box's is
# .gz, which is where this recipe came from), the slim debian image cannot be
# assumed to carry xz-utils, and a plain file mount keeps the inner script free
# of archive formats. It also keeps the glob out of the container: the directory
# holds every image ever built here, and a wildcard there picked up the previous
# release and tried to verify that one too.
WORK_DIR="$(mktemp -d -t mrrc-verify-XXXXXX)"
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

echo "==> xz integrity"
xz -t "$IMG_XZ"
BYTES=$(stat -f %z "$IMG_XZ" 2>/dev/null || stat -c %s "$IMG_XZ")
((BYTES > 200000000 && BYTES < 2000000000)) || {
	echo "ERROR: image size ${BYTES} outside 0.2-2.0GB" >&2
	exit 1
}
echo "    ${BYTES} bytes"

echo "==> decompress"
xz -dc "$IMG_XZ" >"$WORK_DIR/image.img"

INNER="$WORK_DIR/verify.sh"
cat >"$INNER" <<'INNER_SCRIPT'
#!/bin/bash -euo pipefail
bad=0
ok()   { echo "  OK   $*"; }
fail() { echo "  FAIL $*"; bad=1; }

echo "== mount the artifact"
loop=$(losetup --show -fP /image/image.img)
. /common/part-nodes.sh
part_nodes "$loop"
[ -b "${loop}p2" ] || { echo "no ${loop}p2 after the -P scan"; exit 1; }
mkdir -p /mnt
mount -o ro "${loop}p2" /mnt

echo "== step 1: code identity (artifact vs this checkout)"
# `file` is not in the slim debian:bookworm image, and the working tree can move
# while a build runs, so the ELF header is read directly and a mismatch names
# both hashes for a human to settle.
for f in server.py requirements.txt static/ft710_ui.js scope_handler.py; do
	a=$(sha256sum "/mnt/opt/mrrc_modern/$f" | cut -d" " -f1)
	b=$(sha256sum "/src/$f" | cut -d" " -f1)
	[ "$a" = "$b" ] && ok "$f identical to the checkout" \
		|| fail "$f differs: artifact=$a checkout=$b"
done

echo "== step 2: what got installed"
n=$(find /mnt/opt/mrrc_modern/profiles -name "*.env" 2>/dev/null | wc -l)
[ "$n" = 11 ] && ok "11 radio profiles" || fail "profiles: $n"
for f in mrrc-update mrrc-radio mrrc-show-password; do
	[ -e "/mnt/usr/local/bin/$f" ] && ok "/usr/local/bin/$f" || fail "/usr/local/bin/$f missing"
done
[ -x /mnt/etc/update-motd.d/10-mrrc ] && ok "login banner" || fail "10-mrrc missing"
# e_machine sits at offset 18, two bytes little-endian; 183 = AArch64.
machine=$(od -An -tu1 -j18 -N2 /mnt/opt/mrrc_modern/fleet/frpc 2>/dev/null | tr -s " " | sed "s/^ //;s/ $//")
[ "$machine" = "183 0" ] && ok "frpc is an aarch64 ELF (e_machine=183)" || fail "frpc e_machine=$machine"
[ -f /mnt/opt/mrrc_modern/vendor/ftdi/libft4222.so ] && ok "FTDI libft4222.so" || fail "FTDI library missing"
[ -L /mnt/opt/mrrc_modern/vendor/ftdi/libftd2xx.so ] && ok "libftd2xx.so symlink" || fail "libftd2xx.so is not a symlink"
[ -f /mnt/etc/udev/rules.d/99-mrrc-ftdi.rules ] && ok "FT4222 udev rule" || fail "99-mrrc-ftdi.rules missing"
grep -q "^mrrc:" /mnt/etc/passwd && ok "mrrc account" || fail "mrrc account missing"
v=$(cat /mnt/opt/mrrc_modern/version.txt 2>/dev/null)
[ -n "$v" ] && ok "version.txt = $v" || fail "version.txt missing"
# 版本号必须来自**镜像线**（spec 2026-10-27 §2.4）：只认 `## [vX.Y.Z]` 的正则
# 会一路滑到桌面线的最新一条，产出自我报错版本号的镜像。
if want=$(sh /src/packaging/common/version_of.sh /src/CHANGELOG.md 2>/dev/null); then
	[ "$want" = "$v" ] && ok "version.txt matches the image line ($want)" \
		|| fail "version.txt=$v but CHANGELOG 镜像线=$want"
else
	fail "version_of.sh could not read /src/CHANGELOG.md"
fi
# 一键升级与提权通道：盒子上早就有，树莓派从来没验过（spec §4.3）。
grep -q "fix_resolv_conf.sh" /mnt/usr/local/bin/mrrc-update \
	&& ok "mrrc-update carries the resolver repair" \
	|| fail "mrrc-update does not call fix_resolv_conf.sh"
[ -f /mnt/etc/systemd/system/mrrc-box-helper.service ] \
	&& ok "mrrc-box-helper.service" || fail "mrrc-box-helper.service missing"
[ -L /mnt/etc/systemd/system/multi-user.target.wants/mrrc-box-helper.service ] \
	&& ok "box helper enabled" || fail "box helper is not enabled"
# 出厂 Wi-Fi 必须是开的：pi-gen 在 WPA_COUNTRY 缺失时写这一行（spec §2.1）。
nmstate=/mnt/var/lib/NetworkManager/NetworkManager.state
if [ -f "$nmstate" ] && grep -q "^WirelessEnabled=false" "$nmstate"; then
	fail "Wi-Fi ships soft-disabled (WPA_COUNTRY was not set at build time)"
else
	ok "Wi-Fi not soft-disabled"
fi
# 解析链：镜像不得带着构建机的地址出门（Armbian 2026-10-25 那个形态）。
if grep -qE "192\.168\.65\.|DNS requests are forwarded to the host" /mnt/etc/resolv.conf 2>/dev/null; then
	fail "/etc/resolv.conf carries the build host's resolver"
else
	ok "resolv.conf is not a build-host leftover"
fi
[ -f /mnt/opt/mrrc_modern/linux/firstboot_wrapper.py ] && ok "firstboot wrapper" || fail "firstboot wrapper missing"
[ ! -e /mnt/opt/mrrc-build ] && ok "no /opt/mrrc-build" || fail "/opt/mrrc-build shipped"
[ -z "$(find /mnt/opt/mrrc_modern -name __pycache__ -print -quit)" ] && ok "no __pycache__" || fail "__pycache__ shipped"
grep -q "^MRRC_WEB_PASSWORD=" /mnt/opt/mrrc_modern/env/mrrc.env \
	&& fail "a web password was baked in" || ok "no baked web password"
grep -q "^MRRC_SERIAL_PORT=" /mnt/opt/mrrc_modern/env/mrrc.env \
	&& fail "a serial port was baked in" || ok "no baked serial port"

echo "== step 3: D-11 wiring and the platform parameters"
unit=/mnt/etc/systemd/system/mrrc-modern.service
grep -q "^User=mrrc$" "$unit" && ok "User=mrrc" || fail "User line"
a=$(grep "^EnvironmentFile=" "$unit" | cut -d= -f2)
b=$(grep "^Environment=MRRC_CONFIG_FILE=" "$unit" | cut -d= -f3)
[ -n "$a" ] && [ "$a" = "$b" ] && ok "EnvironmentFile == MRRC_CONFIG_FILE ($a)" \
	|| fail "split config file: $a vs $b"
fb=/mnt/etc/systemd/system/mrrc-firstboot.service
grep -q "^Environment=MRRC_BOOT_DIR=/boot/firmware$" "$fb" && ok "MRRC_BOOT_DIR=/boot/firmware (Pi value)" || fail "boot dir"
grep -q "^Environment=MRRC_WEB_HOST=::$" "$fb" && ok "MRRC_WEB_HOST=:: (Pi value)" || fail "web host"
grep -q "^MRRC_WEB_HOST=::$" /mnt/opt/mrrc_modern/env/mrrc.env && ok "env file binds ::" || fail "env bind"
# 启动分区那条有意差异要一路走到单元里：写死 /boot 的话 ConditionPathExists 永不成立，
# Wi-Fi 预置在这台机器上静默哑掉（2026-10-12 抓到并修掉的那个）。
grep -q "^ConditionPathExists=/boot/firmware/mrrc-wifi.txt$" \
	/mnt/etc/systemd/system/mrrc-wifi-preseed.service \
	&& ok "Wi-Fi preseed looks at /boot/firmware" \
	|| fail "preseed unit does not point at the Pi boot partition"
grep -q "^Environment=MRRC_BOOT_DIR=/boot/firmware$" \
	/mnt/etc/systemd/system/mrrc-wifi-preseed.service \
	&& ok "preseed script gets the same boot dir" \
	|| fail "preseed script boot dir"
grep -q "^MRRC_RX_BUFFER_MS=100$" /mnt/opt/mrrc_modern/env/mrrc.env && ok "RX buffer 100ms" || fail "RX buffer"
grep -q "^MRRC_PTT_MAX_TX_SECONDS=120$" /mnt/opt/mrrc_modern/env/mrrc.env && ok "PTT ceiling 120s" || fail "PTT ceiling"

echo "== step 4: it runs (native arm64 chroot)"
for d in dev proc sys; do mount --bind /$d /mnt/$d; done
chroot /mnt /opt/mrrc_modern/venv/bin/python -c "import fastapi, numpy, pyaudio, lameenc, cryptography, serial; print('     deps import OK')" || fail "dependency import"
chroot /mnt /opt/mrrc_modern/venv/bin/python -c "import ctypes; ctypes.CDLL('libopus.so.0'); print('     libopus loads OK')" || fail "libopus"
# FT8 mode: the native DSP library the image builds for itself (design
# docs/superpowers/specs/2026-10-13-ft8-packaging-design.md). Presence is not
# enough — a library built against a different ABI loads fine and then refuses
# every call, so this asks it for its ABI and capability mask.
chroot /mnt /opt/mrrc_modern/venv/bin/python -c "
import ctypes
class Abi(ctypes.Structure):
    _fields_ = [(n, ctypes.c_int32) for n in
                ('abi_version', 'struct_size', 'result_size', 'result_capacity',
                 'ft8_rx_rate', 'ft8_rx_samples', 'ft8_tx_rate', 'ft8_tx_samples',
                 'improved_profiles', 'max_threads', 'max_cycles', 'decode_paths')]
lib = ctypes.CDLL('/opt/mrrc_modern/ft8/dsp/build/libwsjt_core.so')
info = Abi()
assert lib.wsjt_get_abi_info(ctypes.byref(info)) == 0
assert info.abi_version == 2, info.abi_version
assert info.decode_paths == 0x7, hex(info.decode_paths)
print('     FT8 DSP loads OK (abi %d, paths 0x%x)' % (info.abi_version, info.decode_paths))" \
	|| fail "FT8 DSP library"
chroot /mnt /opt/mrrc_modern/venv/bin/python -c "
import sys
sys.path.insert(0, '/opt/mrrc_modern')
sys.path.insert(0, '/opt/mrrc_modern/linux')
import server, net_wifi, box_ops, first_run, mrrc_radio
print('     entry points import OK')" || fail "entry point import"
chroot /mnt /opt/mrrc_modern/venv/bin/python -c "
import sys
sys.path.insert(0, '/opt/mrrc_modern')
sys.path.insert(0, '/opt/mrrc_modern/linux')
import box_update, box_helper
cmd = box_update.watchdog_command('/opt/mrrc_modern', '/tmp/rollback.tar.gz')
assert 'curl' in cmd, cmd
print('     box_update + box_helper import OK (watchdog probes with curl)')" \
	|| fail "box_update/box_helper import"
[ -x /mnt/usr/bin/curl ] && ok "curl present (the rollback probe)" \
	|| fail "curl missing — the rollback probe can never report a dead tree"

umount /mnt/dev /mnt/proc /mnt/sys || true
umount /mnt
losetup -d "$loop"
[ "$bad" = 0 ] && echo "== VERIFY_OK" || { echo "== VERIFY_FAILED"; exit 1; }
INNER_SCRIPT

docker run --rm --privileged --platform linux/arm64 \
	-v "$WORK_DIR":/image:ro -v "$REPO":/src:ro \
	-v "$REPO/packaging/common":/common:ro \
	-v "$INNER":/verify.sh:ro \
	debian:bookworm bash /verify.sh
