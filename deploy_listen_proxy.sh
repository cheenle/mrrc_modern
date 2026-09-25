#!/usr/bin/env bash
# Expose the listen-only UI at https://www.vlsc.net/mrrc_modern/listen
# (idempotent; safe to re-run).
#
#   ./deploy_listen_proxy.sh [user@host]
#
# Backend: the operator's radio server reached directly over public IPv6
# (HTTPS :8888) — no SSH tunnel. If the radio host's IPv6 address changes,
# update LISTEN_BACKEND below and re-run (the nginx block is replaced when
# the backend literal differs).
set -euo pipefail

REMOTE="${1:-cheenle@www.vlsc.net}"
LISTEN_BACKEND="https://[2409:8a00:1894:9940:41:1df2:337d:6e4b]:8888"

echo "==> backend reachability from $REMOTE ($LISTEN_BACKEND)"
ssh "$REMOTE" "curl -6 -sk --max-time 10 -o /dev/null -w '  backend /api/health -> HTTP %{http_code} (want 401 = server reachable, auth required)\n' '$LISTEN_BACKEND/api/health'"

echo "==> nginx locations /mrrc_modern/listen (idempotent)"
# Local quoted heredoc piped into ssh: nothing expands locally, and the remote
# python writes literal $host/$http_upgrade for nginx. The backend URL travels
# via sudo's environment so the heredoc can stay quoted.
ssh "$REMOTE" "sudo LISTEN_BACKEND='$LISTEN_BACKEND' python3 -" <<'NGINX_PY'
import os
import re

backend = os.environ["LISTEN_BACKEND"]
path = "/etc/nginx/sites-available/vlsc.net"
text = open(path, encoding="utf-8").read()

block = f"""    # ── MRRC Modern listen-only UI (/mrrc_modern/listen) ──
    # Backend: operator's radio server over public IPv6 (HTTPS :8888).
    # If the radio host's IPv6 changes, edit deploy_listen_proxy.sh and re-run.
    location = /mrrc_modern/listen {{
        proxy_pass {backend}/listen;
        proxy_ssl_verify off;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect /login /mrrc_modern/login;
    }}
    location = /mrrc_modern/login {{
        proxy_pass {backend}/login;
        proxy_ssl_verify off;
        proxy_set_header Host $host;
    }}
    location = /mrrc_modern/listen.js {{
        proxy_pass {backend}/listen.js;
        proxy_ssl_verify off;
        proxy_set_header Host $host;
    }}
    location = /mrrc_modern/rx_worklet_processor.js {{
        proxy_pass {backend}/rx_worklet_processor.js;
        proxy_ssl_verify off;
        proxy_set_header Host $host;
    }}
    location ^~ /mrrc_modern/modules/ {{
        proxy_pass {backend}/modules/;
        proxy_ssl_verify off;
        proxy_set_header Host $host;
    }}
    location ^~ /mrrc_modern/api/ {{
        proxy_pass {backend}/api/;
        proxy_ssl_verify off;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }}
    # WebSocket channels: /WSradio, /WSspectrum, /WSaudioRX (listen role has
    # no /WSaudioTX — the radio server refuses it before nginx matters).
    # ^~ is load-bearing on this site: the regex locations (like the .js
    # static-asset rule) further up outrank a plain prefix match and would
    # serve local 404s.
    location ^~ /mrrc_modern/WS {{
        rewrite ^/mrrc_modern(/WS.*)$ $1 break;
        proxy_pass {backend};
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 86400;
    }}
"""

start_marker = "    # ── MRRC Modern listen-only UI (/mrrc_modern/listen) ──"
pattern = re.compile(
    re.escape(start_marker)
    + r".*?location (?:\^~ )?/mrrc_modern/WS \{.*?\n    \}\n",
    re.DOTALL)
matches = list(pattern.finditer(text))

if len(matches) == 1 and backend in matches[0].group(0) \
        and "location ^~ /mrrc_modern/WS" in matches[0].group(0):
    print("nginx: already present and current")
else:
    # Remove every stale copy (an older regex once missed the ^~ form and
    # left a duplicate that broke nginx -t), then insert exactly one.
    for m in reversed(matches):
        text = text[:m.start()] + text[m.end():]
    anchor = "    # ── MRRC Modern support receiver (/mrrc_modern/support/) ──"
    text = (text.replace(anchor, block + "\n" + anchor) if anchor in text
            else text.rstrip() + "\n\n" + block)
    open(path, "w", encoding="utf-8").write(text)
    print("nginx: (re)installed listen block (%d old block(s) removed)"
          % len(matches))
NGINX_PY
ssh "$REMOTE" 'sudo nginx -t && sudo systemctl reload nginx'

echo "==> public probes"
curl -s -o /dev/null -w "  /mrrc_modern/listen  -> HTTP %{http_code} (want 302 → login, or 200 with a session)\n" \
	https://www.vlsc.net/mrrc_modern/listen || true
curl -s -o /dev/null -w "  /mrrc_modern/login   -> HTTP %{http_code} (want 200)\n" \
	https://www.vlsc.net/mrrc_modern/login || true
echo "==> done"
