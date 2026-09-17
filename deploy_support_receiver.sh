#!/usr/bin/env bash
# Deploy/refresh the MRRC Modern support receiver (idempotent; safe to re-run).
#
#   ./deploy_support_receiver.sh [user@host]
#
# This is MRRC Modern's own instance: separate unit, port 8098, separate storage
# (/var/www/support-modern, outside the docroot) — see
# docs/superpowers/specs/2026-09-17-support-bundle-design.md §9.
# Password: $SUPPORT_PASSWORD -> ~/.mrrc-support-credentials.txt -> generated.
# It is written to a 0600 root-only EnvironmentFile, never printed, never in git.
set -euo pipefail

REMOTE="${1:-cheenle@www.vlsc.net}"
HERE="$(cd "$(dirname "$0")" && pwd)"

PW="${SUPPORT_PASSWORD:-}"
if [ -z "$PW" ] && [ -f "$HOME/.mrrc-support-credentials.txt" ]; then
	PW="$(tr -d '\n' <"$HOME/.mrrc-support-credentials.txt")"
fi
if [ -z "$PW" ]; then
	PW="$(python3 -c 'import secrets,string;print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(24)))')"
	printf '%s' "$PW" >"$HOME/.mrrc-support-credentials.txt"
	chmod 600 "$HOME/.mrrc-support-credentials.txt"
	echo "generated a new password -> ~/.mrrc-support-credentials.txt"
fi

echo "==> directories (storage belongs to www-data and lives outside the docroot)"
ssh "$REMOTE" 'sudo mkdir -p /opt/mrrc-modern-support && sudo mkdir -p /var/www/support-modern && sudo chown www-data:www-data /var/www/support-modern && sudo chmod 750 /var/www/support-modern'

echo "==> upload server + unit"
rsync -az "$HERE/tools/support_receiver/server.py" "$REMOTE:/tmp/support-modern-server.py"
rsync -az "$HERE/tools/support_receiver/support-receiver-modern.service" "$REMOTE:/tmp/support-receiver-modern.service"
ssh "$REMOTE" 'sudo install -m 644 /tmp/support-modern-server.py /opt/mrrc-modern-support/server.py && sudo install -m 644 /tmp/support-receiver-modern.service /etc/systemd/system/support-receiver-modern.service && sudo rm -f /tmp/support-modern-server.py /tmp/support-receiver-modern.service'

echo "==> password file (0600, root only)"
ssh "$REMOTE" "sudo bash -c 'umask 077; printf \"SUPPORT_PASSWORD=%s\nSUPPORT_USER=mrrc\nSUPPORT_DIR=/var/www/support-modern\nSUPPORT_PORT=8098\nSUPPORT_PRODUCT=mrrc_modern\n\" \"$PW\" > /etc/mrrc-modern-support.env'"

echo "==> start / restart"
ssh "$REMOTE" 'sudo systemctl daemon-reload && sudo systemctl enable --now support-receiver-modern >/dev/null 2>&1; sudo systemctl restart support-receiver-modern; sleep 1; systemctl is-active support-receiver-modern'
ssh "$REMOTE" 'curl -s -o /dev/null -w "  local probe /api/list without password -> HTTP %{http_code} (want 401)\n" http://127.0.0.1:8098/api/list'

echo "==> nginx location /mrrc_modern/support/ (idempotent)"
# Local quoted heredoc piped into ssh: nothing expands locally, and the remote
# python writes a literal $host for nginx (an unquoted delimiter inside a
# single-quoted ssh command would let the *remote* shell eat it).
ssh "$REMOTE" sudo python3 - <<'NGINX_PY'
path = "/etc/nginx/sites-available/vlsc.net"
text = open(path, encoding="utf-8").read()
if "location /mrrc_modern/support/" in text:
    print("nginx: already present")
else:
    block = """    # \u2500\u2500 MRRC Modern support receiver (/mrrc_modern/support/) \u2500\u2500
    location /mrrc_modern/support/ {
        proxy_pass http://127.0.0.1:8098/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        client_max_body_size 25m;
    }
"""
    marker = "    # \u2500\u2500 MRRC Modern website (/mrrc_modern/) \u2500\u2500"
    text = (text.replace(marker, block + "\n" + marker) if marker in text
            else text.rstrip() + "\n\n" + block)
    open(path, "w", encoding="utf-8").write(text)
    print("nginx: added /mrrc_modern/support/")
NGINX_PY
ssh "$REMOTE" 'sudo nginx -t && sudo systemctl reload nginx'

echo "==> public probe"
curl -s -o /dev/null -w "  https://www.vlsc.net/mrrc_modern/support/api/list -> HTTP %{http_code} (want 401)\n" \
	https://www.vlsc.net/mrrc_modern/support/api/list || true
echo "==> done"
