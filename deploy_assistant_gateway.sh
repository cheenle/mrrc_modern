#!/usr/bin/env bash
# Deploy/refresh the MRRC Modern AI assistant gateway (idempotent; safe to re-run).
#
#   ./deploy_assistant_gateway.sh [user@host]
#
# 与 deploy_support_receiver.sh 同一个套路：独立目录、独立单元、独立 env（0600 root）、
# nginx 前缀 /mrrc_modern/assistant/。**API key 从不打印**：它从
# ~/.mrrc-assistant.env 读、直接写进服务器的 0600 文件，只在两处转手。
#
# 为什么网关要连带 net_tls.py 与 vendor/ca/cacert.pem 一起上：它要出站调模型，
# 而这个仓库的规矩是「所有出站只走 net_tls，且只信随包的根」，不许把信任判定交给机器。
set -euo pipefail

REMOTE="${1:-cheenle@www.vlsc.net}"
HERE="$(cd "$(dirname "$0")" && pwd)"
LOCAL_ENV="${MRRC_ASSISTANT_ENV_FILE:-$HOME/.mrrc-assistant.env}"

[ -f "$LOCAL_ENV" ] || { echo "缺 $LOCAL_ENV（MODEL_URL / MODEL / API_KEY）" >&2; exit 1; }

# 实例 token 表：没有就生成一个给「通用盒子」用，写进本地凭据文件（0600）。
CREDS="$HOME/.mrrc-assistant-credentials.txt"
if [ -z "${ASSISTANT_INSTANCES:-}" ]; then
	if [ -f "$CREDS" ]; then
		ASSISTANT_INSTANCES="$(tr -d '\n' <"$CREDS")"
	else
		TOKEN="$(python3 -c 'import secrets;print(secrets.token_hex(16))')"
		ASSISTANT_INSTANCES="mrrc-${TOKEN}:boxes"
		printf '%s' "$ASSISTANT_INSTANCES" >"$CREDS"
		chmod 600 "$CREDS"
		echo "generated an instance token -> ~/.mrrc-assistant-credentials.txt"
	fi
fi
BOX_TOKEN="${ASSISTANT_INSTANCES%%,*}"
BOX_TOKEN="${BOX_TOKEN%%:*}"

echo "==> directories"
ssh "$REMOTE" 'sudo mkdir -p /opt/mrrc-modern-assistant/tools/assistant_gateway /opt/mrrc-modern-assistant/vendor/ca \
  && sudo mkdir -p /var/lib/mrrc-assistant && sudo chown www-data:www-data /var/lib/mrrc-assistant && sudo chmod 750 /var/lib/mrrc-assistant'

echo "==> upload gateway + trust store"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/tools/assistant_gateway" "$TMP/vendor/ca"
cp "$HERE"/tools/assistant_gateway/*.py "$HERE"/tools/assistant_gateway/assistant_kb.json \
	"$TMP/tools/assistant_gateway/"
cp "$HERE/tools/__init__.py" "$TMP/tools/__init__.py"
# net_tls 只从 config 取一个 `_env`；config 又只依赖 env_store。两个文件一起上，
# 比给 net_tls 开一个"没有 config 的旁路"安全 —— 出站出口必须与产品里那份逐字节相同。
cp "$HERE/net_tls.py" "$HERE/config.py" "$HERE/env_store.py" "$TMP/"
cp "$HERE/vendor/ca/cacert.pem" "$TMP/vendor/ca/cacert.pem"
rsync -az "$TMP"/ "$REMOTE:/tmp/assistant-bundle/"
# `rsync -a` 会把源目录的权限也复制过去 —— 源是 mktemp -d 的 700，于是 gateway 目录
# 变成 root:700，www-data 连 chdir 都进不去（2026-10-30 实测：status=200/CHDIR）。
# 所以 rsync 之后必须把权限改回来。
ssh "$REMOTE" 'sudo rsync -a --delete /tmp/assistant-bundle/ /opt/mrrc-modern-assistant/ \
  && sudo chown -R root:root /opt/mrrc-modern-assistant \
  && sudo find /opt/mrrc-modern-assistant -type d -exec chmod 755 {} + \
  && sudo find /opt/mrrc-modern-assistant -type f -exec chmod 644 {} + \
  && sudo rm -rf /tmp/assistant-bundle'

echo "==> systemd unit"
ssh "$REMOTE" 'sudo tee /etc/systemd/system/mrrc-assistant.service >/dev/null <<UNIT
[Unit]
Description=MRRC Modern AI assistant gateway (product knowledge + conversations)
After=network-online.target
Wants=network-online.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/mrrc-modern-assistant
EnvironmentFile=/etc/mrrc-modern-assistant.env
Environment=PYTHONPATH=/opt/mrrc-modern-assistant
ExecStart=/usr/bin/python3 -m tools.assistant_gateway.gateway
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/mrrc-assistant

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload'

echo "==> env file (0600 root): key + instance table"
# 值从本地 env 读；不 echo、不进 shell 历史（用 ssh 的 stdin 传 heredoc）。
{
	printf 'MRRC_ASSISTANT_MODEL_URL=%s\n' "$(sed -n 's/^MRRC_ASSISTANT_MODEL_URL=//p' "$LOCAL_ENV")"
	printf 'MRRC_ASSISTANT_MODEL=%s\n' "$(sed -n 's/^MRRC_ASSISTANT_MODEL=//p' "$LOCAL_ENV")"
	printf 'MRRC_ASSISTANT_API_KEY=%s\n' "$(sed -n 's/^MRRC_ASSISTANT_API_KEY=//p' "$LOCAL_ENV")"
	printf 'MRRC_ASSISTANT_INSTANCES=%s\n' "$ASSISTANT_INSTANCES"
	printf 'MRRC_ASSISTANT_DB=/var/lib/mrrc-assistant/assistant.db\n'
	printf 'MRRC_ASSISTANT_KB=/opt/mrrc-modern-assistant/tools/assistant_gateway/assistant_kb.json\n'
	printf 'MRRC_ASSISTANT_LISTEN=127.0.0.1:8790\n'
	printf 'MRRC_CA_BUNDLE=/opt/mrrc-modern-assistant/vendor/ca/cacert.pem\n'
} | ssh "$REMOTE" 'sudo bash -c "umask 077; cat > /etc/mrrc-modern-assistant.env"'

echo "==> start / restart"
ssh "$REMOTE" 'sudo systemctl enable mrrc-assistant >/dev/null 2>&1 || true; sudo systemctl restart mrrc-assistant; sleep 2; systemctl is-active mrrc-assistant'
ssh "$REMOTE" 'curl -s -o /dev/null -w "  local probe without token -> HTTP %{http_code} (want 401)\n" -X POST http://127.0.0.1:8790/v1/assistant/ask -H "Content-Type: application/json" -d "{\"message\":\"x\"}"'

echo "==> nginx location /mrrc_modern/assistant/ (idempotent)"
ssh "$REMOTE" "sudo python3 - <<'PY'
path = '/etc/nginx/sites-available/vlsc.net'
block = '''
	# MRRC Modern AI assistant gateway (deploy_assistant_gateway.sh).
	# ^~ matters: the site's regex \\.js$ location would otherwise win.
	location ^~ /mrrc_modern/assistant/ {
		proxy_pass http://127.0.0.1:8790/;
		proxy_http_version 1.1;
		proxy_set_header Host \$host;
		proxy_set_header X-Real-IP \$remote_addr;
		proxy_set_header X-Mrrc-Instance \$http_x_mrrc_instance;
		proxy_read_timeout 120s;
		client_max_body_size 32k;
	}
'''
text = open(path, encoding='utf-8').read()
if '/mrrc_modern/assistant/' in text:
    print('  nginx: already present')
else:
    marker = 'server {'
    idx = text.index(marker) + len(marker)
    open(path, 'w', encoding='utf-8').write(text[:idx] + '\n' + block + text[idx:])
    print('  nginx: added /mrrc_modern/assistant/')
PY
sudo nginx -t && sudo systemctl reload nginx"

echo "==> 线上探针（未带 token 必须 401）"
curl -s -o /dev/null -w '  https://www.vlsc.net/mrrc_modern/assistant/v1/assistant/ask -> HTTP %{http_code} (want 401)\n' \
	-X POST "https://www.vlsc.net/mrrc_modern/assistant/v1/assistant/ask" \
	-H 'Content-Type: application/json' -d '{"message":"x"}'

echo
echo "盒子侧要写的三行（token 在 ~/.mrrc-assistant-credentials.txt）："
echo "  MRRC_ASSISTANT=on"
echo "  MRRC_ASSISTANT_URL=https://www.vlsc.net/mrrc_modern/assistant/v1"
echo "  MRRC_ASSISTANT_TOKEN=$BOX_TOKEN"
