#!/bin/bash -e
# Acceptance for a flashed box. Run on the box, as root:
#
#   scp packaging/box/verify.sh box:/tmp/ && ssh box 'sudo bash /tmp/verify.sh'
#
# Every check reports independent of the others, and a failure prints the
# command that would explain it. The summary at the end is the verdict.
set -uo pipefail

PASS=0; FAIL=0
ok()   { printf '  ✓ %s\n' "$*"; PASS=$((PASS+1)); }
bad()  { printf '  ✗ %s\n      → %s\n' "$1" "$2"; FAIL=$((FAIL+1)); }
head_() { printf '\n%s\n' "$*"; }

ENV_FILE=/opt/mrrc_modern/env/mrrc.env
UNIT=mrrc-modern
SETUP_UNIT=mrrc-setup-ap.service
AP_STATE=/run/mrrc/setup-ap/state.json

head_ "1/11 wired network"
if ip -4 addr show scope global 2>/dev/null | grep -q 'inet '; then
  ok "$(ip -4 addr show scope global | awk '/inet /{print $2; exit}')"
else
  bad "no global IPv4 address" "check the cable and 'nmcli device status'"
fi

head_ "2/11 radio serial"
if compgen -G "/dev/ttyUSB*" >/dev/null || compgen -G "/dev/ttyACM*" >/dev/null; then
  ok "$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | tr '\n' ' ')"
else
  bad "no /dev/ttyUSB* or /dev/ttyACM*" "check the USB cable and the radio's own USB setting"
fi

head_ "3/11 radio USB audio"
if arecord -l 2>/dev/null | grep -qi 'card'; then
  ok "$(arecord -l | sed -n 's/^card \([0-9]*\).*\[\(.*\)\]/\1:\2/p' | tr '\n' ' ')"
else
  bad "arecord lists no capture card" "the radio's USB sound card is not enumerated"
fi

head_ "4/11 python"
py="$(/opt/mrrc_modern/venv/bin/python -V 2>/dev/null || true)"
case "$py" in
  *3.11*) ok "$py" ;;
  "")     bad "venv python missing" "ls -l /opt/mrrc_modern/venv/bin/python" ;;
  *)      bad "$py (expected 3.11)" "the image is not the bookworm base" ;;
esac

head_ "5/11 service"
if ! command -v systemctl >/dev/null 2>&1; then
  bad "systemctl not found" "this is not a systemd host — you are on the wrong machine"
elif systemctl is-active --quiet "$UNIT"; then
  ok "active (running)"
else
  bad "$UNIT is not active" "journalctl -u $UNIT -n 50 --no-pager"
fi

head_ "6/11 HTTPS on 8888"
# Any HTTP status means the endpoint answered — the same rule launcher_net.py
# uses (`answers()` counts a 401 as served). What must fail is no answer at all,
# or a 5xx, which is a server that is up but broken.
code="$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1:8888/" || true)"
case "$code" in
  [1234][0-9][0-9]) ok "HTTP $code (the endpoint answered)" ;;
  *) bad "https://127.0.0.1:8888 returned '${code:-no answer}'" \
         "journalctl -u $UNIT -n 50 --no-pager; ss -ltnp | grep 8888" ;;
esac

head_ "7/11 config identity (design D-11)"
if command -v systemctl >/dev/null 2>&1; then
  env_file="$(systemctl show -p EnvironmentFile --value "$UNIT" | tr -d '"')"
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

head_ "8/11 Cloud Hub tunnel client"
if [ -x /opt/mrrc_modern/fleet/frpc ]; then
  ok "/opt/mrrc_modern/fleet/frpc ($( /opt/mrrc_modern/fleet/frpc --version 2>&1 | head -1 ))"
else
  bad "frpc missing or not executable" \
      "without it connect() reports 已连接 and the public entry serves 502"
fi

head_ "9/11 Cloud Hub entry (skipped until connected)"
if grep -q '^MRRC_CLOUD_ENTRY=' "$ENV_FILE" 2>/dev/null; then
  entry="$(sed -n 's/^MRRC_CLOUD_ENTRY=//p' "$ENV_FILE" | head -1)"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$entry" || true)"
  case "$code" in
    200|401) ok "$entry → HTTP $code" ;;
    *)       bad "$entry → HTTP ${code:-no answer}" \
                  "journalctl -u $UNIT | grep -i cloud; is the tunnel up?" ;;
  esac
else
  printf '  – not connected yet; open the settings page and run Cloud Hub apply/connect\n'
fi

head_ "10/11 PTT safety ceiling"
if grep -q '^MRRC_PTT_MAX_TX_SECONDS=' "$ENV_FILE" 2>/dev/null; then
  ok "$(sed -n 's/^MRRC_PTT_MAX_TX_SECONDS=//p' "$ENV_FILE" | head -1) s"
else
  bad "MRRC_PTT_MAX_TX_SECONDS is unset (0 = no ceiling)" \
      "a public entry needs the second PTT defence, see design D-4"
fi

head_ "11/11 setup hotspot (onboarding with no cable and no keyboard)"
# This runs *after* onboarding, over the network the wizard just configured, so a
# healthy box reports mode 'off' or 'sta' — the hotspot being down is design D-6
# fence 1, not a failure. What must be true is that the machinery is installed and
# the daemon is alive, because the alternative is a box that can never be
# onboarded without a keyboard.
if ! command -v systemctl >/dev/null 2>&1; then
  bad "systemctl not found" "this is not a systemd host"
elif ! systemctl list-unit-files --no-legend 2>/dev/null | grep -q "^${SETUP_UNIT}[[:space:]]"; then
  bad "${SETUP_UNIT} is not installed" \
      "this image predates the setup hotspot; rebuild it (design 2026-10-08)"
elif ! systemctl is-enabled --quiet "$SETUP_UNIT"; then
  bad "${SETUP_UNIT} is installed but not enabled" \
      "systemctl enable ${SETUP_UNIT}"
elif ! command -v dnsmasq >/dev/null 2>&1; then
  bad "dnsmasq is missing" \
      "the hotspot comes up but hands out no addresses (design D-2); add it to box-overlay.sh"
elif systemctl is-active --quiet "$SETUP_UNIT"; then
  mode="$(sed -n 's/^ *"mode": *"\([^"]*\)".*/\1/p' "$AP_STATE" 2>/dev/null | head -1)"
  ok "service active; published mode='${mode:-none}' (off/sta is correct once the box has a network)"
  opened="$(journalctl -u "$SETUP_UNIT" --no-pager 2>/dev/null | grep -c 'SETUP HOTSPOT OPEN' || true)"
  printf '      (the hotspot has been opened %s time(s) in the current journal)\n' "${opened:-0}"
else
  bad "${SETUP_UNIT} is not running" \
      "journalctl -u ${SETUP_UNIT} -n 50 --no-pager"
fi

printf '\n────────────────────────────\n'
printf '  %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
