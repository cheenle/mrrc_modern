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

## Desktop Installers — Permissions & Privacy

The packaged desktop apps (macOS `.dmg`, Windows `Setup.exe`) run the same server, so every
security property above still applies. Two things are installer-specific:

**macOS asks for exactly one permission: Microphone (audio input).**

- It is required to read the radio's USB sound card so received audio can be streamed to
  the browser. macOS files *all* audio input under the microphone permission class.
- The app requests nothing else — no camera, no screen recording, no contacts, no
  accessibility. The bundle declares only `NSMicrophoneUsageDescription`.
- Denying it fails **silently** (CoreAudio opens the capture stream and fills it with
  zeros), so it looks like a radio problem: control, spectrum and PTT all work, RX is
  silent. Grant it in *System Settings → Privacy & Security → Microphone*.
- The desktop bundles are **ad-hoc signed** (no paid Developer ID), so macOS re-asks after
  each upgrade and Gatekeeper shows a one-time "cannot verify the developer" prompt.
  `spctl` reporting *no Developer ID* is expected; a *damaged / invalid signature* verdict
  is not — that means the package is older than v1.18.1.

**HTTPS uses a self-signed certificate generated on first run.**

- The private key stays on the machine (`~/Library/Application Support/MRRC-Modern/certs/`
  on macOS, `%LOCALAPPDATA%\MRRC-Modern\certs\` on Windows) and is **never** uploaded.
- The browser warning is expected for self-signed certificates; replace the certs with your
  own (or front the app with a reverse proxy) if the LAN is not trusted.

**Data and diagnostics never leave the machine unless you ask.**

- Passwords, private keys and tokens are excluded from diagnostics bundles by an
  allow-list filter plus a secret-value pass; recordings, memory channels and tuner
  learning data are excluded outright. Uploading is an explicit action, and a
  save-locally-only path exists for offline machines.
- Uninstalling removes the app only. User data lives in the user's own directory
  (`~/Library/Application Support/MRRC-Modern/`); delete it explicitly if needed, and
  reset the recorded permission with `tccutil reset Microphone net.vlsc.mrrc-modern`.

## Known Limitations

- Auth tokens are **cleared on server restart** — no persistent session storage
- No 2FA/MFA support — rely on strong passwords
- No IP whitelisting — use firewall/proxy for network-level access control
- Rate limiting is **per-process** — doesn't persist across restarts
