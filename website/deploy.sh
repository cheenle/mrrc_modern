#!/bin/bash
# MRRC Modern Website — Deploy to www.vlsc.net/mrrc_modern/
set -e

# Configuration
LOCAL_DIR="/Users/cheenle/HAM/mrrc_modern/website"
REMOTE_HOST="www.vlsc.net"
REMOTE_USER="cheenle"
REMOTE_WEBROOT="/var/www/vlsc.net/mrrc_modern"
BACKUP_DIR="/var/tmp/mrrc_modern_backup_$(date +%Y%m%d_%H%M%S)"

echo "=========================================="
echo "MRRC Modern Website Deployment"
echo "=========================================="
echo ""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

if [ ! -d "$LOCAL_DIR" ]; then
    echo -e "${RED}Error: Local directory not found: $LOCAL_DIR${NC}"
    exit 1
fi

echo "Local directory: $LOCAL_DIR"
echo "Remote host: $REMOTE_HOST"
echo "Remote path: $REMOTE_WEBROOT"
echo ""

cd "$LOCAL_DIR"

echo "Checking required files..."
REQUIRED_FILES=(
    "index.html"
    "zh/index.html"
    "css/octen.css"
    "css/ft710.css"
    "sdd.html"
    "zh/sdd.html"
    "sdd/index.html"
    "sdd/01-executive-summary.html"
    "sdd/15-ptt-safety-architecture.html"
    "images/IMG_8888.PNG"
    "downloads/MRRC-Modern-Setup.exe"
    "downloads/MRRC-Modern-v1.9.0-Windows-x64-Setup.exe"
)
for file in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$file" ]; then
        echo -e "${RED}Error: Required file missing: $file${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓${NC} $file"
done
echo ""

DEPLOY_PACKAGE="/tmp/mrrc_modern_website_$(date +%Y%m%d_%H%M%S).tar.gz"
tar -czf "$DEPLOY_PACKAGE" --exclude='deploy.sh' --exclude='.DS_Store' -C "$LOCAL_DIR" .
echo -e "${GREEN}✓${NC} Package created: $DEPLOY_PACKAGE"
echo ""

echo "This will:"
echo "  1. Backup current site"
echo "  2. Ensure nginx has a /mrrc_modern/ location"
echo "  3. Upload new files to $REMOTE_WEBROOT"
echo "  4. Set permissions and reload nginx"
echo ""

read -p "Continue with deployment? (y/N): " confirm
if [[ $confirm != [yY] ]]; then
    echo "Deployment cancelled."
    rm "$DEPLOY_PACKAGE"
    exit 0
fi

ssh "$REMOTE_USER@$REMOTE_HOST" << 'EOF'
    set -e
    if [ -d "/var/www/vlsc.net/mrrc_modern" ] && [ "$(ls -A /var/www/vlsc.net/mrrc_modern 2>/dev/null)" ]; then
        sudo mkdir -p /var/tmp
        sudo cp -r /var/www/vlsc.net/mrrc_modern /var/tmp/mrrc_modern_backup_$(date +%Y%m%d_%H%M%S)
        echo "Backup created."
    fi
    # Clear any stale/partial deploy packages before the fresh one is scp'd,
    # so the extract glob below always matches exactly one archive (a leftover
    # truncated package once made tar abort with "unexpected end of file").
    sudo rm -f /var/tmp/mrrc_modern_website_*.tar.gz

    # Idempotently add a /mrrc_modern/ location to the nginx site config.
    NGINX_SITE=/etc/nginx/sites-available/vlsc.net
    if [ -f "$NGINX_SITE" ]; then
        sudo python3 - "$NGINX_SITE" << 'PYEOF'
import sys, re
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    text = f.read()
if "location /mrrc_modern/" in text:
    print("nginx: /mrrc_modern/ location already present")
else:
    block = """    # ── MRRC Modern website (/mrrc_modern/) ──
    location /mrrc_modern/ {
        try_files $uri $uri/ =404;
        autoindex off;
    }
    location = /mrrc_modern {
        return 301 /mrrc_modern/;
    }
"""
    # Insert right before the FT-710 block's closing, keeping /mrrc_ft710/ order.
    marker = "    # ── MRRC FT-710 website (/mrrc_ft710/) ──"
    if marker in text:
        text = text.replace(marker, block + "\n" + marker)
    else:
        text = text.rstrip() + "\n\n" + block
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print("nginx: added /mrrc_modern/ location")
PYEOF
        sudo nginx -t
        sudo systemctl reload nginx
        echo "nginx reloaded."
    else
        echo "WARNING: $NGINX_SITE not found; skipping nginx location setup."
    fi

    sudo mkdir -p /var/www/vlsc.net/mrrc_modern
    sudo chown -R www-data:www-data /var/www/vlsc.net/mrrc_modern
    sudo chmod -R 755 /var/www/vlsc.net/mrrc_modern
EOF

scp "$DEPLOY_PACKAGE" "$REMOTE_USER@$REMOTE_HOST:/var/tmp/"

ssh "$REMOTE_USER@$REMOTE_HOST" << 'EOF'
    set -e
    sudo tar -xzf "/var/tmp/mrrc_modern_website_"*.tar.gz -C /var/www/vlsc.net/mrrc_modern --overwrite
    sudo chown -R www-data:www-data /var/www/vlsc.net/mrrc_modern
    sudo chmod -R 755 /var/www/vlsc.net/mrrc_modern
    find /var/www/vlsc.net/mrrc_modern -type f -name "*.html" -exec sudo chmod 644 {} \;
    find /var/www/vlsc.net/mrrc_modern -type f -name "*.css" -exec sudo chmod 644 {} \;
    find /var/www/vlsc.net/mrrc_modern -type f -name "*.js" -exec sudo chmod 644 {} \; 2>/dev/null || true
    find /var/www/vlsc.net/mrrc_modern/downloads -type f -name "*.exe" -exec sudo chmod 644 {} \;
    rm -f "/var/tmp/mrrc_modern_website_"*.tar.gz
    sudo nginx -t
    sudo systemctl reload nginx
    echo ""
    echo "MRRC Modern deployment complete!"
    echo "URL: https://www.vlsc.net/mrrc_modern/"
EOF

rm -f "$DEPLOY_PACKAGE"

echo ""
echo -e "${GREEN}Deployment Complete!${NC}"
echo "https://www.vlsc.net/mrrc_modern/"
echo ""
echo "Rollback: ssh $REMOTE_USER@$REMOTE_HOST"
echo "  sudo rm -rf $REMOTE_WEBROOT && sudo cp -r /var/tmp/mrrc_modern_backup_* $REMOTE_WEBROOT"
