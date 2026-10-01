"""Cloud Hub onboarding, from inside the app.

The tenant-side story used to be a shell script: paste a token and a one-time secret into a
terminal, then hand the operator a root command. This module turns it into three calls the
settings UI can make:

    apply()    ask the portal for an entry (callsign + contact), keep the request token
    status()   ask whether it has been approved yet
    connect()  once approved: sign the certificate the hub will verify, enroll it, write the
               app's own configuration so the app serves it from then on, and run the tunnel

It is deliberately stdlib-only and free of FastAPI imports so it can be unit-tested without
starting the server. The one thing it needs from the app is where the config file lives - the same
file the launcher reads on every start, which is why writing it here is enough for the setting to
survive a reboot.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import ssl_bootstrap

logger = logging.getLogger(__name__)

#: The hub's own portal entry, and the app's default. Measured from a mainland home line
#: (2026-10-01): 3/3 answered in 0.30-1.40 s with the wildcard certificate in place. An earlier note
#: blamed the hub's TLS for every port at once - that measurement was against the bare IP and this
#: name (which is what the app uses, and what the hub's vhost has a certificate for) was never it.
PORTAL_DEFAULT = "https://portal.mrrc.vlsc.net:8899"

#: The overseas path proxy, which exists for networks that only allow 80/443 outbound (R-H12).
#: It is a hop away and was measured intermittent on that same evening - 3 of 6 requests hung
#: until the client gave up - so it is the **fallback**, never the default.
PORTAL_EDGE = "https://www.vlsc.net/mrrc_portal"

TIMEOUT = 25
#: Budget for the first of two paths: a network that blocks 8899 must not spend the whole timeout
#: before the fallback gets its turn (the portal answers in ~1.4 s when it is reachable).
FALLBACK_FIRST_TIMEOUT = 8


class CloudHubError(RuntimeError):
    """Anything the UI should show as a sentence rather than a traceback."""


class _Unreachable(CloudHubError):
    """The request never reached a portal: DNS/TCP/TLS failure, a timeout, or a hop's 502/504.

    Kept apart from a portal that *answered* (even with a rejection), because only the former is
    worth retrying on the other path - repeating an application the portal already recorded would
    just trade one failure for a duplicate.
    """


def _post_once(portal: str, route: str, payload: dict, timeout: int) -> dict:
    """POST form-encoded fields on one path - the shape the portal speaks - and return its JSON."""
    url = portal.rstrip("/") + route
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request(url, data=data,
                                headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as reply:
            return json.loads(reply.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read() or b"{}").get("error", "")
        except Exception:                       # noqa: BLE001 - the body is best effort
            pass
        message = detail
        if not message:
            message = f"portal returned HTTP {exc.code}"
        if exc.code in (502, 504):              # a proxy hop died; the portal never spoke
            raise _Unreachable(message) from exc
        raise CloudHubError(message) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _Unreachable(f"cannot reach the portal ({exc.__class__.__name__}: {exc})") from exc


def _post(portal: str, route: str, payload: dict, timeout: int = TIMEOUT) -> dict:
    """POST to the portal, asking the overseas edge only when the first path never answered.

    The default entry is the hub's own; the edge exists for networks that permit just 80/443
    outbound (R-H12). A portal that answered - even with a bad callsign or a duplicate - is never
    asked twice, so a retry can not turn a rejection into a second application.
    """
    paths = [portal]
    if portal.rstrip("/") == PORTAL_DEFAULT:
        paths.append(PORTAL_EDGE)
    last_error: CloudHubError = CloudHubError("no portal path to try")
    for index, base in enumerate(paths):
        budget = FALLBACK_FIRST_TIMEOUT if len(paths) > 1 and index == 0 else timeout
        try:
            return _post_once(base, route, payload, budget)
        except _Unreachable as exc:
            logger.info("cloud hub: %s did not answer (%s); trying the next path", base, exc)
            last_error = exc
    raise last_error


def apply(portal: str, callsign: str, contact: str = "", product: str = "") -> dict:
    """Submit an application. The reply carries the request token used by status()."""
    reply = _post(portal, "/apply", {"callsign": callsign, "contact": contact, "product": product})
    if not reply.get("request_token"):
        raise CloudHubError("portal did not return a request token")
    return reply


def claim(portal: str, callsign: str, secret: str) -> dict:
    """Adopt an application the operator already approved, using the secret they handed over.

    Without this, an app can only see an application it submitted itself - so an approval made
    against a web-submitted application would be invisible to the app that has to use it.
    """
    reply = _post(portal, "/claim", {"callsign": callsign, "secret": secret})
    if not reply.get("request_token"):
        raise CloudHubError("portal did not return a request token")
    return reply


def status(portal: str, callsign: str, token: str) -> dict:
    """Ask whether this application has been approved, and for its connection details once it is."""
    return _post(portal, "/status", {"callsign": callsign, "token": token})


def _write_config(config_path: Path, updates: dict[str, str]) -> None:
    """Merge ``k=v`` lines into the app's env file, preserving everything else.

    This file is what the launcher merges over os.environ on every start, so values written here
    are honoured however the app is launched - which user-scope variables are not.
    """
    existing: list[str] = []
    if config_path.exists():
        existing = [ln for ln in config_path.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.lstrip().startswith("#")
                    and "=" in ln and ln.split("=", 1)[0].strip() not in updates]
    lines = existing + [f"{k}={v}" for k, v in sorted(updates.items())]
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(config_path.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(config_path)


def frpc_config_text(name: str, token: str, local_port: int, remote_port: int,
                     hub_host: str = "tunnel.mrrc.vlsc.net", control_port: int = 8989,
                     log_file: str = "") -> str:
    """The tunnel's TOML. Written as ASCII with no BOM: frpc's parser rejects a BOM outright."""
    lines = [
        f'serverAddr = "{hub_host}"',
        f"serverPort = {control_port}",
        "",
        'auth.method = "token"',
        f'auth.token = "{token}"',
        "",
    ]
    if log_file:
        lines += [f'log.to = "{log_file}"', 'log.level = "info"', ""]
    lines += [
        "[[proxies]]",
        f'name = "{name}"',
        'type = "tcp"',
        'localIP = "127.0.0.1"',
        f"localPort = {local_port}",
        f"remotePort = {remote_port}",
    ]
    return "\n".join(lines) + "\n"


class TunnelProcess:
    """Keep one frpc alive, restarting it if it exits.

    The app runs it as a child rather than a service: the tunnel is only useful while the app is
    up, and a child needs no elevation and no scheduler entry to manage.
    """

    def __init__(self, frpc: Path, conf: Path):
        self.frpc = Path(frpc)
        self.conf = Path(conf)
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cloud-hub-frpc", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        delay = 2
        while not self._stop.is_set():
            try:
                self.proc = subprocess.Popen(
                    [str(self.frpc), "-c", str(self.conf)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                )
                self.last_error = ""
                delay = 2
                while self.proc.poll() is None and not self._stop.is_set():
                    time.sleep(1)
                if self._stop.is_set():
                    break
                self.last_error = f"frpc exited with {self.proc.returncode}"
                logger.warning("cloud hub: %s; restarting in %ss", self.last_error, delay)
            except OSError as exc:
                self.last_error = f"cannot run frpc: {exc}"
                logger.warning("cloud hub: %s", self.last_error)
            self._stop.wait(delay)
            delay = min(delay * 2, 60)

    def stop(self) -> None:
        self._stop.set()
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)


def connect(portal: str, callsign: str, token: str, *, config_path: Path, cert_dir: Path,
            fleet_dir: Path, data_dir: Path, local_port: int = 8888,
            tunnel: TunnelProcess | None = None) -> dict:
    """Do everything that follows approval, and report what happened.

    Steps, in order: confirm approval, sign the certificate for the entry name the hub will check,
    enroll its public half, write the app's configuration so it serves that certificate from now on,
    then write the tunnel config and start frpc.
    """
    state = status(portal, callsign, token)
    if state.get("status") != "granted":
        return {"connected": False, "status": state.get("status", "unknown"), "reason": "尚未批准"}

    label = str(state.get("label") or "")
    # The portal is remote input: a malformed port must read as a sentence in the UI, not as an
    # unhandled ValueError that the endpoint turns into a 500.
    try:
        port = int(state.get("port") or 0)
    except (TypeError, ValueError) as exc:
        raise CloudHubError(f"portal sent a non-numeric port: {state.get('port')!r}") from exc
    secret = str(state.get("enroll_secret") or "")
    hub_token = str(state.get("hub_token") or "") or os.environ.get("MRRC_HUB_TOKEN", "")
    if not (label and port and secret):
        raise CloudHubError("portal says granted but sent no label/port/secret")

    fqdn = f"{label}.mrrc.vlsc.net"
    pair = ssl_bootstrap.sign_for(fqdn, cert_dir)
    if pair is None:
        raise CloudHubError("cannot sign a certificate (cryptography unavailable)")
    cert_path, key_path = pair

    _post(portal, "/enroll", {"callsign": callsign, "secret": secret,
                              "cert": cert_path.read_text(encoding="utf-8")})

    frpc = Path(fleet_dir) / "frpc.exe" if os.name == "nt" else Path(fleet_dir) / "frpc"
    conf = Path(data_dir) / f"frpc-{label}.toml"
    conf.parent.mkdir(parents=True, exist_ok=True)
    log_file = str(Path(data_dir) / f"frpc-{label}.log")
    conf.write_text(
        frpc_config_text(label, hub_token, local_port, port, log_file=log_file),
        encoding="ascii",
    )

    _write_config(Path(config_path), {
        "MRRC_SSL_CERT": str(cert_path),
        "MRRC_SSL_KEY": str(key_path),
        "MRRC_WEB_PORT": str(local_port),
        "MRRC_REMOTE_SESSION_TX_HEARTBEAT_S": "5",
    })

    started = False
    if tunnel is not None and frpc.exists():
        tunnel.frpc = frpc
        tunnel.conf = conf
        tunnel.start()
        started = True

    return {
        "connected": True,
        "status": "granted",
        "label": label,
        "port": port,
        "fqdn": fqdn,
        "entry": f"https://{fqdn}:9988/",
        "cert": str(cert_path),
        "tunnel_config": str(conf),
        "tunnel_started": started,
    }
