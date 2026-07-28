#!/usr/bin/env python3
"""HTTP Basic Auth reverse proxy for pi-web.

Sits in front of pi-web (default localhost:30141) and requires
username/password before granting access. Supports WebSocket and SSE.

Usage:
    PI_WEB_PASSWORD=mysecret python3 pi_web_proxy.py
    PI_WEB_PASSWORD=mysecret PI_WEB_PORT=8443 python3 pi_web_proxy.py
    PI_WEB_USER=admin PI_WEB_PASSWORD=mysecret python3 pi_web_proxy.py

Environment:
    PI_WEB_PASSWORD     - required password
    PI_WEB_USER         - username (default: "admin")
    PI_WEB_PORT         - proxy listen port (default: 8443)
    PI_WEB_HOST         - proxy listen host (default: "0.0.0.0")
    PI_WEB_BACKEND      - pi-web backend URL (default: "http://127.0.0.1:30141")
"""

import asyncio
import base64
import os
import secrets
from urllib.parse import urljoin

import aiohttp
from aiohttp import web, ClientSession, WSMsgType


# ── config ──────────────────────────────────────────────────────────────────
USER = os.environ.get("PI_WEB_USER", "admin")
PASSWORD = os.environ.get("PI_WEB_PASSWORD", "")
HOST = os.environ.get("PI_WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("PI_WEB_PORT", "8443"))
BACKEND = os.environ.get("PI_WEB_BACKEND", "http://127.0.0.1:30141")

if not PASSWORD:
    print("ERROR: PI_WEB_PASSWORD environment variable is required.")
    print("  PI_WEB_PASSWORD=mysecret python3 pi_web_proxy.py")
    exit(1)

# Generate a simple auth token for cookie-based session persistence
# (so the user only has to enter the password once per browser session)
AUTH_COOKIE = "pi_web_auth"
VALID_TOKENS = set()  # in-memory token store


# ── auth helpers ────────────────────────────────────────────────────────────
def check_basic_auth(request: web.Request) -> bool:
    """Check HTTP Basic Auth credentials."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        username, _, password = decoded.partition(":")
        return username == USER and password == PASSWORD
    except Exception:
        return False


def check_cookie(request: web.Request) -> bool:
    """Check if the auth cookie contains a valid token."""
    token = request.cookies.get(AUTH_COOKIE, "")
    return token in VALID_TOKENS


def is_authenticated(request: web.Request) -> bool:
    """Return True if the request is authenticated."""
    return check_basic_auth(request) or check_cookie(request)


@web.middleware
async def auth_middleware(request: web.Request, handler) -> web.StreamResponse:
    """Middleware that rejects unauthenticated requests."""
    # Allow login endpoint
    if request.path == "/pi-web-login" and request.method in ("GET", "POST"):
        return await handler(request)

    if is_authenticated(request):
        return await handler(request)

    # API requests → 401 JSON
    if request.path.startswith("/_next/") or request.path.startswith("/api/"):
        return web.json_response(
            {"error": "Unauthorized", "message": "Please login at /pi-web-login"},
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="pi-web"'},
        )

    # Page requests → redirect to login
    raise web.HTTPFound(f"/pi-web-login?next={request.path_qs}")


# ── login page ──────────────────────────────────────────────────────────────
LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>pi-web Login</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: #0a0a0a; color: #e0e0e0;
  display: flex; align-items: center; justify-content: center;
  min-height: 100vh;
}}
.login-box {{
  background: #1a1a1a; border: 1px solid #333;
  border-radius: 12px; padding: 2.5rem; width: 360px; max-width: 90vw;
  text-align: center;
}}
h1 {{ font-size: 1.4rem; margin-bottom: 0.3rem; }}
h1 span {{ color: #f0a030; }}
p.sub {{ color: #888; font-size: 0.85rem; margin-bottom: 1.5rem; }}
input {{
  width: 100%; padding: 0.7rem 1rem; margin-bottom: 1rem;
  background: #0a0a0a; border: 1px solid #333; border-radius: 8px;
  color: #e0e0e0; font-size: 1rem; outline: none;
}}
input:focus {{ border-color: #f0a030; }}
button {{
  width: 100%; padding: 0.7rem; background: #f0a030; color: #0a0a0a;
  border: none; border-radius: 8px; font-size: 1rem; font-weight: 600;
  cursor: pointer; transition: opacity 0.15s;
}}
button:hover {{ opacity: 0.9; }}
.error {{ color: #e74c3c; font-size: 0.85rem; margin-bottom: 1rem; }}
</style>
</head>
<body>
<div class="login-box">
  <h1>pi<span>-web</span></h1>
  <p class="sub">Enter password to continue</p>
  {error_html}
  <form method="POST">
    <input type="password" name="password" placeholder="Password" autofocus required>
    <button type="submit">Login</button>
  </form>
</div>
</body>
</html>"""


async def login_page(request: web.Request) -> web.Response:
    """Serve the login form and handle login POST."""
    error = ""
    if request.method == "POST":
        data = await request.post()
        password = data.get("password", "")
        if password == PASSWORD:
            # Generate a token and set cookie
            token = secrets.token_urlsafe(32)
            VALID_TOKENS.add(token)
            next_path = request.query.get("next", "/")
            resp = web.HTTPFound(next_path)
            resp.set_cookie(AUTH_COOKIE, token, httponly=True, max_age=86400 * 30)
            return resp
        error = '<p class="error">Incorrect password</p>'

    return web.Response(
        text=LOGIN_HTML.format(error_html=error),
        content_type="text/html",
    )


# ── reverse proxy ───────────────────────────────────────────────────────────
async def proxy_handler(request: web.Request) -> web.StreamResponse:
    """Forward the request to the pi-web backend."""
    target_url = urljoin(BACKEND, request.path_qs)

    async with ClientSession() as session:
        try:
            # Check if this is a WebSocket upgrade
            if request.headers.get("Upgrade", "").lower() == "websocket":
                return await _proxy_websocket(request, session, target_url)

            # Regular HTTP proxy
            return await _proxy_http(request, session, target_url)

        except aiohttp.ClientError as e:
            return web.Response(
                text=f"Backend unavailable: {e}", status=502
            )


async def _proxy_http(
    request: web.Request, session: ClientSession, target_url: str
) -> web.StreamResponse:
    """Proxy a regular HTTP request."""
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "transfer-encoding")}
    # Remove auth header before forwarding to backend
    headers.pop("Authorization", None)
    headers.pop("Cookie", None)  # Don't forward auth cookie to backend
    headers["X-Forwarded-For"] = request.remote

    body = await request.read() if request.method in ("POST", "PUT", "PATCH") else None

    async with session.request(
        method=request.method,
        url=target_url,
        headers=headers,
        data=body,
        allow_redirects=False,
    ) as backend_resp:
        resp = web.StreamResponse(
            status=backend_resp.status,
            headers={
                k: v for k, v in backend_resp.headers.items()
                if k.lower() not in ("transfer-encoding",)
            },
        )
        await resp.prepare(request)

        async for chunk in backend_resp.content.iter_chunks():
            data, _ = chunk
            if data:
                await resp.write(data)
        await resp.write_eof()
        return resp


async def _proxy_websocket(
    request: web.Request, session: ClientSession, target_url: str
) -> web.WebSocketResponse:
    """Proxy a WebSocket connection."""
    ws_client = web.WebSocketResponse()
    await ws_client.prepare(request)

    # Connect to backend
    try:
        async with session.ws_connect(target_url) as ws_backend:
            async def forward_to_backend():
                async for msg in ws_client:
                    if msg.type == WSMsgType.TEXT:
                        await ws_backend.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await ws_backend.send_bytes(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        await ws_backend.close()
                        break

            async def forward_to_client():
                async for msg in ws_backend:
                    if msg.type == WSMsgType.TEXT:
                        await ws_client.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await ws_client.send_bytes(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        await ws_client.close()
                        break

            await asyncio.gather(forward_to_backend(), forward_to_client())
    except Exception:
        pass

    return ws_client


# ── main ────────────────────────────────────────────────────────────────────
def main():
    app = web.Application(middlewares=[auth_middleware])

    # Login endpoint (bypasses auth)
    app.router.add_route("GET", "/pi-web-login", login_page)
    app.router.add_route("POST", "/pi-web-login", login_page)

    # Catch-all proxy
    app.router.add_route("*", "/{tail:.*}", proxy_handler)

    print(f"pi-web auth proxy → {BACKEND}")
    print(f"Listening on http{'s' if 'SSL' in os.environ else ''}://{HOST}:{PORT}")
    print(f"Username: {USER}")
    print(f"Password: {'*' * len(PASSWORD)}")
    print()

    web.run_app(app, host=HOST, port=PORT, print=lambda _: None)


if __name__ == "__main__":
    main()
