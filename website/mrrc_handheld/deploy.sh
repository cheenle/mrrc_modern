#!/bin/bash
# MRRC Handheld website — deploy to www.vlsc.net/mrrc_handheld/
#
# The MRRC Modern deploy.sh only publishes /mrrc_modern, so this site has its
# own, deliberately narrower script: it touches ONLY the two paths it owns —
#   /var/www/vlsc.net/mrrc_handheld/        (the site)
#   /var/www/vlsc.net/js/global-nav.js      (the shared nav, if -n is given)
# Everything else on that host is shared with the portal and the other project
# sites, so nothing here ever deletes a tree.
#
#   ./deploy.sh              # upload the site
#   ./deploy.sh -n           # … and push the local nav file too
#   ./deploy.sh -n -y        # non-interactive (no confirmation prompt)
#
# The nav is NOT deployed by default: the copy served from www.vlsc.net carries
# runtime tweaks that are not in this repo, so overwriting it casually would
# drop them.
set -euo pipefail

REMOTE_HOST=${REMOTE_HOST:-www.vlsc.net}
REMOTE_USER=${REMOTE_USER:-cheenle}
WEBROOT=/var/www/vlsc.net/mrrc_handheld
NAV=/var/www/vlsc.net/js/global-nav.js
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE_DIR="$(basename "$HERE")"

WITH_NAV=0
ASSUME_YES=0
for a in "$@"; do
    case "$a" in
        -n|--nav) WITH_NAV=1 ;;
        -y|--yes) ASSUME_YES=1 ;;
        *) echo "unknown option: $a"; exit 2 ;;
    esac
done

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; NC=$'\033[0m'

echo "=========================================="
echo "MRRC Handheld website deployment"
echo "=========================================="
echo " local:  $HERE"
echo " remote: $REMOTE_USER@$REMOTE_HOST:$WEBROOT"
[ "$WITH_NAV" = 1 ] && echo " nav:    $NAV"
echo

# ── sanity: the files the page references must exist ──
REQUIRED=(index.html css/handheld.css css/octen.css images/ui_rx.png images/ui_tx.png images/ui_rx_vol.png)
for f in "${REQUIRED[@]}"; do
    if [ ! -f "$HERE/$f" ]; then
        echo "${RED}missing required file: $f${NC}"
        exit 1
    fi
    echo "  ${GREEN}ok${NC} $f"
done
if [ "$WITH_NAV" = 1 ]; then
    NAV_SRC="$(cd "$HERE/../js" && pwd)/global-nav.js"
    if [ ! -f "$NAV_SRC" ]; then
        echo "${RED}nav source not found: $NAV_SRC${NC}"
        exit 1
    fi
    grep -q "mrrc_handheld" "$NAV_SRC" || {
        echo "${RED}nav source has no mrrc_handheld entry — refusing to publish it${NC}"
        exit 1
    }
    echo "  ${GREEN}ok${NC} js/global-nav.js (contains mrrc_handheld)"
fi
echo

if [ "$ASSUME_YES" = 0 ]; then
    read -r -p "Deploy now? (y/N): " confirm
    [[ $confirm == [yY] ]] || { echo "cancelled"; exit 0; }
fi

PKG=/tmp/${SITE_DIR}_site_$(date +%Y%m%d_%H%M%S).tar.gz
tar -czf "$PKG" --exclude='deploy.sh' --exclude='.DS_Store' -C "$HERE" .
echo "package: $PKG ($(du -h "$PKG" | cut -f1))"

scp -q "$PKG" "$REMOTE_USER@$REMOTE_HOST:/tmp/"

ssh "$REMOTE_USER@$REMOTE_HOST" "
set -e
sudo mkdir -p $WEBROOT
# tar only overwrites: files removed from the repo would stay published, so
# take a cheap backup of the small site before extracting.
sudo cp -a $WEBROOT /var/tmp/${SITE_DIR}_backup_\$(date +%Y%m%d_%H%M%S) 2>/dev/null || true
sudo tar -xzf /tmp/$(basename "$PKG") -C $WEBROOT --overwrite
sudo chown -R www-data:www-data $WEBROOT
sudo chmod -R 755 $WEBROOT
sudo find $WEBROOT -type f \( -name '*.html' -o -name '*.css' -o -name '*.js' -o -name '*.png' -o -name '*.svg' \) -exec chmod 644 {} \;
rm -f /tmp/$(basename "$PKG")
# keep the three newest backups only
ls -1d /var/tmp/${SITE_DIR}_backup_* 2>/dev/null | sort -r | tail -n +4 | xargs -r -d '\n' sudo rm -rf || true
echo 'site files installed'
"

if [ "$WITH_NAV" = 1 ]; then
    scp -q "$HERE/../js/global-nav.js" "$REMOTE_USER@$REMOTE_HOST:/tmp/global-nav.js"
    ssh "$REMOTE_USER@$REMOTE_HOST" "
    set -e
    sudo cp $NAV $NAV.bak.\$(date +%Y%m%d_%H%M%S)
    sudo mv /tmp/global-nav.js $NAV
    sudo chown www-data:www-data $NAV
    sudo chmod 644 $NAV
    echo 'nav updated'
    "
fi

rm -f "$PKG"
echo
echo "${GREEN}done${NC}"
echo "https://www.vlsc.net/${SITE_DIR}/"
