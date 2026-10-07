#!/usr/bin/env python3
"""Measure what one spectrum bandwidth tier actually costs (SDD AD-025).

Connects to /WSspectrum with a real token, optionally declares a profile, and
counts the frames and bytes that arrive.  Run it once per tier to reproduce the
design's acceptance table against a live server instead of arithmetic:

    python3 dev_tools/spectrum_profile_probe.py --profile high --seconds 10
    python3 dev_tools/spectrum_profile_probe.py --profile mid  --seconds 10
    python3 dev_tools/spectrum_profile_probe.py --profile low  --seconds 10
    python3 dev_tools/spectrum_profile_probe.py --no-caps --seconds 10   # legacy

Token: `--token` (or `$MRRC_TOKEN`), else `--password` (or `$MRRC_PASSWORD`) is
exchanged against `POST /api/auth/login`, which returns `{"ok":true,"token":...}`
(server.py:3276).  Sent as `Authorization: Bearer` (server.py:893).  The dev
server's cert is self-signed `CN=localhost`, so loopback URLs skip verification.

--no-deflate turns off permessage-deflate negotiation.  Note what this tool can
and cannot see: it counts the *decoded* message length, because the websockets
library decompresses transparently — so every figure it prints is a **payload**
figure, with or without the flag.  The browser's on-the-wire cost (measured
elsewhere with a raw socket: 1701 B -> ~441 B per full frame, and the 850 zero
bytes of wf2 -> ~1.4 B) is not observable from here.  Quote the payload axis for
Android (OkHttp 4.12 does not offer the extension, so payload == wire) and the
raw-socket figure for browsers.
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.request
from urllib.parse import urlparse

try:
    from websockets.sync.client import connect
except ImportError:  # pragma: no cover - dev tool, not part of the suite
    sys.exit("needs the 'websockets' package: .venv/bin/pip install websockets")

DEFAULT_URL = "wss://127.0.0.1:8888/WSspectrum"
DEFAULT_HTTP = "https://127.0.0.1:8888"
FULL_FRAME_BYTES = 1701
SHORT_FRAME_BYTES = 851
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def ssl_context_for(host: str) -> ssl.SSLContext:
    """The dev server presents a self-signed CN=localhost cert (SDD V2.69).

    Only loopback skips verification; anything else gets a real check.  A context
    is always passed explicitly — the repo's AST guard forbids a bare urlopen
    (net_tls, SDD V2.68).
    """
    if host in LOOPBACK:
        return ssl._create_unverified_context()
    return ssl.create_default_context()


def read_token(args) -> str:
    explicit = args.token or os.environ.get("MRRC_TOKEN", "").strip()
    if explicit:
        return explicit
    password = args.password or os.environ.get("MRRC_PASSWORD", "")
    if not password:
        sys.exit("no credentials: pass --token / set MRRC_TOKEN, "
                 "or pass --password / set MRRC_PASSWORD to log in")
    base = args.http_url or DEFAULT_HTTP
    ctx = ssl_context_for(urlparse(base).hostname or "")
    req = urllib.request.Request(
        base.rstrip("/") + "/api/auth/login",   # the only login path the auth
        data=json.dumps({"password": password}).encode("utf-8"),   # middleware
        headers={"Content-Type": "application/json"})              # exempts
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            return str(json.loads(resp.read())["token"])
    except Exception as e:
        sys.exit(f"login failed against {base}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--http-url", default=DEFAULT_HTTP,
                    help="base URL for the /api/auth/login exchange")
    ap.add_argument("--token")
    ap.add_argument("--password", help="exchanged for a token via /api/login")
    ap.add_argument("--profile", choices=("high", "mid", "low"), default="high")
    ap.add_argument("--no-caps", action="store_true",
                    help="send no spectrumCaps: proves an undeclared socket "
                         "still gets the legacy 1701 B stream")
    ap.add_argument("--no-deflate", action="store_true",
                    help="do not negotiate permessage-deflate.  The byte counts "
                         "below are payload either way (the library "
                         "decompresses transparently); this only changes what "
                         "the server would put on the wire")
    ap.add_argument("--seconds", type=float, default=10.0)
    args = ap.parse_args()

    token = read_token(args)
    kwargs = {"additional_headers": {"Authorization": f"Bearer {token}"},
              "max_size": None,
              "open_timeout": 10}
    if args.url.startswith("wss://"):
        kwargs["ssl"] = ssl_context_for(urlparse(args.url).hostname or "")
    if args.no_deflate:
        kwargs["compression"] = None

    lengths: dict[int, int] = {}
    total = 0
    started = time.monotonic()
    deadline = started + args.seconds
    with connect(args.url, **kwargs) as ws:
        if not args.no_caps:
            ws.send(json.dumps({"type": "spectrumCaps", "profile": args.profile}))
        while time.monotonic() < deadline:
            try:
                msg = ws.recv(timeout=max(deadline - time.monotonic(), 0.1))
            except TimeoutError:
                break
            if not isinstance(msg, (bytes, bytearray)):
                continue
            lengths[len(msg)] = lengths.get(len(msg), 0) + 1
            total += len(msg)
    elapsed = max(time.monotonic() - started, 1e-6)

    label = "no caps (legacy)" if args.no_caps else args.profile
    axis = ("payload; deflate not negotiated (Android's OkHttp does not offer "
            "it, so payload == wire)" if args.no_deflate else
            "payload; deflate negotiated (wire cost is lower and invisible here)")
    frames = sum(lengths.values())
    want_full = args.no_caps or args.profile == "high"
    print(f"profile      : {label}")
    print(f"axis         : {axis}")
    print(f"window       : {elapsed:.1f}s  url {args.url}")
    print(f"frames       : {frames}  ({frames / elapsed:.1f} fps)")
    print("frame lengths: " + (", ".join(
        f"{n}B x{c}" for n, c in sorted(lengths.items())) or "none"))
    print(f"bytes        : {total}  ({total * 8 / elapsed / 1000:.1f} kbps)")

    # The acceptance gates, stated where they cannot be misread.
    ok = frames > 0 and set(lengths) <= ({FULL_FRAME_BYTES} if want_full
                                        else {SHORT_FRAME_BYTES})
    print(f"gate         : {'PASS' if ok else 'FAIL'} "
          f"(expected {'1701B only' if want_full else '851B only'})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
