# MRRC Web Control — Security Configuration Guide

## ⚠️ CRITICAL: Change Default Password Before Deployment

The default password is a **placeholder string**. You MUST change it before deploying anywhere accessible over a network.

### Quick Setup

```bash
# Option 1: Environment variable (recommended)
export FT710_WEB_PASSWORD="YourStrongPassword123!"
python3 server.py

# Option 2: Modify config.py (development only)
# Edit config.py line: WEB_PASSWORD = "YourStrongPassword123!"
```

### Password Requirements

| Criteria | Recommendation |
|----------|---------------|
| Length | **16+ characters** (minimum 12) |
| Complexity | Mixed case, numbers, symbols |
| Uniqueness | Not reused from other services |
| Rotation | Change every 90 days |

**Weak examples** (will trigger warnings):
- `ft710`, `password`, `123456`, `admin`
- Anything shorter than 8 characters

**Strong examples**:
- `K9$mP!xR2vLq#nW8`
- `HamRadio2026!Secure`

## Security Features

### 1. Login Rate Limiting

- **Limit**: 5 login attempts per 5 minutes per IP
- **Implementation**: Sliding window in `_check_login_rate_limit()` (server.py)
- **Effect**: Brute-force attacks are automatically throttled

### 2. WebSocket Authentication

- All WebSocket connections require `?token=<auth_token>` query parameter
- Auth tokens are cleared on server restart
- Invalid tokens receive proper close codes

### 3. HTTPS/SSL Support

The server supports TLS encryption for production deployments:

```bash
# With Let's Encrypt certs:
python3 server.py --ssl-cert certs/fullchain.pem --ssl-key certs/radio.vlsc.net.key

# Disable SSL (development only):
python3 server.py --no-ssl
```

**Production recommendation**: Always use SSL, especially when accessing from external networks.

### 4. Host Binding

Control which network interfaces the server listens on:

```bash
# Localhost only (most secure):
export FT710_WEB_HOST="127.0.0.1"

# All interfaces (use with caution):
export FT710_WEB_HOST="0.0.0.0"

# IPv6 dual-stack (default):
export FT710_WEB_HOST="::"
```

### 5. Health Monitoring

```bash
curl http://localhost:8888/api/health
```

Returns:
```json
{
  "status": "healthy",
  "radio_connected": true,
  "uptime_seconds": 3600,
  "clients": 2
}
```

Use this for monitoring and alerting.

## Deployment Checklist

- [ ] Changed default password to a strong unique value
- [ ] Set `FT710_WEB_HOST` to appropriate binding (localhost for single-user)
- [ ] Enabled SSL with valid certificates for external access
- [ ] Verified rate limiting works (try 6 rapid login attempts)
- [ ] Tested WebSocket authentication with invalid token
- [ ] Confirmed health endpoint is accessible
- [ ] Set up firewall rules to restrict access if needed

## Network Security Recommendations

1. **Local access only**: Run behind a reverse proxy (nginx/caddy) with TLS termination
2. **Firewall**: Block port 8888 from external networks if not using SSL
3. **VPN**: Consider running behind WireGuard/OpenVPN for remote access
4. **Monitoring**: Set up alerts on `/api/health` for downtime detection

## Known Limitations

- Auth tokens are **cleared on server restart** — no persistent session storage
- No 2FA/MFA support — rely on strong passwords
- No IP whitelisting — use firewall/proxy for network-level access control
- Rate limiting is **per-process** — doesn't persist across restarts
