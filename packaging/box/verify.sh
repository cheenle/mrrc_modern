#!/bin/bash -e
# Acceptance for a flashed box. Run on the box, as root:
#
#   scp packaging/box/verify.sh box:/tmp/ && ssh box 'sudo bash /tmp/verify.sh'
#
# Every check reports independent of the others, and a failure prints the
# command that would explain it. The summary at the end is the verdict.
set -uo pipefail

PASS=0
FAIL=0
ok() {
	printf '  ✓ %s\n' "$*"
	PASS=$((PASS + 1))
}
bad() {
	printf '  ✗ %s\n      → %s\n' "$1" "$2"
	FAIL=$((FAIL + 1))
}
head_() { printf '\n%s\n' "$*"; }

ENV_FILE=/opt/mrrc_modern/env/mrrc.env
UNIT=mrrc-modern

head_ "1/12 wired network"
# `cmd | grep -q` inside a condition is a trap in this script: grep exits the
# moment it matches, the writer takes SIGPIPE (141), and `set -o pipefail` turns
# the whole pipeline into a failure — so a check that found what it looked for
# reports the opposite. Capture first, match second.
if grep -q 'inet ' <<<"$(ip -4 addr show scope global 2>/dev/null)"; then
	ok "$(ip -4 addr show scope global | awk '/inet /{print $2; exit}')"
else
	bad "no global IPv4 address" "check the cable and 'nmcli device status'"
fi

head_ "2/12 radio serial"
if compgen -G "/dev/ttyUSB*" >/dev/null || compgen -G "/dev/ttyACM*" >/dev/null; then
	ok "$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | tr '\n' ' ')"
else
	bad "no /dev/ttyUSB* or /dev/ttyACM*" "check the USB cable and the radio's own USB setting"
fi

head_ "3/12 radio USB audio"
if grep -qi 'card' <<<"$(arecord -l 2>/dev/null)"; then
	ok "$(arecord -l | sed -n 's/^card \([0-9]*\).*\[\(.*\)\]/\1:\2/p' | tr '\n' ' ')"
else
	bad "arecord lists no capture card" "the radio's USB sound card is not enumerated"
fi

head_ "4/12 python"
py="$(/opt/mrrc_modern/venv/bin/python -V 2>/dev/null || true)"
case "$py" in
*3.11*) ok "$py" ;;
"") bad "venv python missing" "ls -l /opt/mrrc_modern/venv/bin/python" ;;
*) bad "$py (expected 3.11)" "the image is not the bookworm base" ;;
esac

head_ "5/12 service"
if ! command -v systemctl >/dev/null 2>&1; then
	bad "systemctl not found" "this is not a systemd host — you are on the wrong machine"
elif systemctl is-active --quiet "$UNIT"; then
	ok "active (running)"
else
	bad "$UNIT is not active" "journalctl -u $UNIT -n 50 --no-pager"
fi

head_ "6/12 HTTPS on 8888"
# Any HTTP status means the endpoint answered — the same rule launcher_net.py
# uses (`answers()` counts a 401 as served). What must fail is no answer at all,
# or a 5xx, which is a server that is up but broken.
code="$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1:8888/" || true)"
case "$code" in
[1234][0-9][0-9]) ok "HTTP $code (the endpoint answered)" ;;
*) bad "https://127.0.0.1:8888 returned '${code:-no answer}'" \
	"journalctl -u $UNIT -n 50 --no-pager; ss -ltnp | grep 8888" ;;
esac

head_ "7/12 config identity (design D-11)"
if command -v systemctl >/dev/null 2>&1; then
	# systemd 252 names this property EnvironmentFiles (plural) and prints
	# `/path (ignore_errors=no)`. The singular spelling returns an empty string, so
	# this check reported "EnvironmentFile=''" on every healthy box — nobody had run
	# it against a real systemd until 2026-10-11.
	env_file="$(systemctl show -p EnvironmentFiles --value "$UNIT" |
		sed -e 's/ (ignore_errors=[a-z]*)$//' -e 's/^"//' -e 's/"$//')"
	cfg="$(systemctl show -p Environment --value "$UNIT" | tr ' ' '\n' | sed -n 's/^MRRC_CONFIG_FILE=//p')"
	if [ -n "$cfg" ] && [ "$env_file" = "$cfg" ]; then
		ok "EnvironmentFile == MRRC_CONFIG_FILE == $env_file"
	else
		bad "EnvironmentFile='$env_file' but MRRC_CONFIG_FILE='$cfg'" \
			"Cloud Hub's connect() writes the second path and the service reads the first; fix the unit"
	fi
else
	bad "cannot read the unit (no systemctl)" "run this on the box"
fi

head_ "8/12 Cloud Hub tunnel client"
if [ -x /opt/mrrc_modern/fleet/frpc ]; then
	ok "/opt/mrrc_modern/fleet/frpc ($(/opt/mrrc_modern/fleet/frpc --version 2>&1 | head -1))"
else
	bad "frpc missing or not executable" \
		"without it connect() reports 已连接 and the public entry serves 502"
fi

head_ "9/12 Cloud Hub entry (skipped until connected)"
if grep -q '^MRRC_CLOUD_ENTRY=' "$ENV_FILE" 2>/dev/null; then
	entry="$(sed -n 's/^MRRC_CLOUD_ENTRY=//p' "$ENV_FILE" | head -1)"
	code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$entry" || true)"
	case "$code" in
	200 | 401) ok "$entry → HTTP $code" ;;
	*) bad "$entry → HTTP ${code:-no answer}" \
		"journalctl -u $UNIT | grep -i cloud; is the tunnel up?" ;;
	esac
else
	printf '  – not connected yet; open the settings page and run Cloud Hub apply/connect\n'
fi

head_ "10/12 PTT safety ceiling"
if grep -q '^MRRC_PTT_MAX_TX_SECONDS=' "$ENV_FILE" 2>/dev/null; then
	ok "$(sed -n 's/^MRRC_PTT_MAX_TX_SECONDS=//p' "$ENV_FILE" | head -1) s"
else
	bad "MRRC_PTT_MAX_TX_SECONDS is unset (0 = no ceiling)" \
		"a public entry needs the second PTT defence, see design D-4"
fi

head_ "11/12 box identity and the privilege channel"
# The onboarding path is a LAN page now (design §9), so what has to be true
# post-flash is not "a hotspot exists" but "the operator can find this box and
# the helpers it needs are installed".
if [ ! -e /etc/polkit-1/rules.d/49-mrrc-nm.rules ]; then
	bad "the NetworkManager polkit rule is missing" \
		"the web pages cannot write NM without it; add it to box-overlay.sh"
else
	ok "polkit rule present (mrrc may reconfigure NetworkManager)"
fi
if ! systemctl is-enabled --quiet mrrc-wifi-preseed.service 2>/dev/null; then
	bad "mrrc-wifi-preseed.service is not enabled" \
		"a card preseeded with /boot/mrrc-wifi.txt would be ignored; add it to box-overlay.sh"
else
	ok "Wi-Fi preseed unit is installed (inactive after a successful join)"
fi
if ! systemctl is-active --quiet avahi-daemon; then
	bad "avahi-daemon is not running" \
		"<hostname>.local will not resolve; systemctl status avahi-daemon"
elif ! command -v avahi-resolve >/dev/null 2>&1; then
	printf '  – avahi-resolve not available; skipped the name check\n'
elif avahi-resolve -4 -n "$(hostname).local" >/dev/null 2>&1; then
	ok "$(hostname).local resolves"
else
	bad "$(hostname).local does not resolve" \
		"avahi-daemon is up but not answering; check for a name collision in journalctl -u avahi-daemon"
fi
if [ ! -S /run/mrrc/box-helper.sock ]; then
	bad "/run/mrrc/box-helper.sock is missing or not a socket" \
		"reboot / change-name / change-timezone will be greyed out; journalctl -u mrrc-box-helper"
else
	mode="$(stat -c '%a %U %G' /run/mrrc/box-helper.sock 2>/dev/null)"
	ok "box helper socket ${mode:-present}"
fi

head_ "12/12 WiFi N9 firmware (the 2020 build wedges this board)"
# Field report 2026-10-11: the linux-firmware N9 (3.1.1 / 20200604) associates
# and then progressively dies (drop loop, wedged scans, MCU mailbox timeouts);
# the factory 2021 build is the validated one. See packaging/box/vendor-mt7663.md.
N9=/lib/firmware/mediatek/mt7663_n9_v3.bin
N9_SHA=223f73f17f0f986dc4e7167daa6eef14ffb41c713f22d70f9645eb049bdec80a
if [ ! -f "$N9" ]; then
	bad "$N9 is missing" "the WiFi driver cannot come up at all"
elif ! command -v sha256sum >/dev/null 2>&1; then
	printf '  – sha256sum not available; skipped\n'
elif [ "$(sha256sum "$N9" | cut -d' ' -f1)" = "$N9_SHA" ]; then
	ok "factory 2021 build (7663mp1827)"
else
	bad "N9 is NOT the pinned factory build" \
		"copy vendor/mediatek/mt7663_n9_v3.bin over $N9, dpkg-divert it, then POWER-CYCLE (a soft reboot does not reset the chip)"
fi

printf '\n────────────────────────────\n'
printf '  %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
