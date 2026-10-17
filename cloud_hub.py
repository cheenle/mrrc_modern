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
import re
import secrets
import socket
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import env_store

import net_tls
import ssl_bootstrap

logger = logging.getLogger(__name__)

#: The hub's own portal entry, and the app's default. Measured from a mainland home line
#: (2026-10-01): 3/3 answered in 0.30-1.40 s with the wildcard certificate in place. An earlier note
#: blamed the hub's TLS for every port at once - that measurement was against the bare IP and this
#: name (which is what the app uses, and what the hub's vhost has a certificate for) was never it.
PORTAL_DEFAULT = "https://portal.mrrc.vlsc.net"

#: The overseas path proxy, which exists for networks that only allow 80/443 outbound (R-H12).
#: It is a hop away and was measured intermittent on that same evening - 3 of 6 requests hung
#: until the client gave up - so it is the **fallback**, never the default.
#: The hub used to be a separate box the instances reached on :8899, with www.vlsc.net as a second
#: path for networks that only permit 80/443. Both now live on one machine, one port, one path, so
#: the second path is gone: measured, the old :8899 was never actually open to the internet.
PORTAL_EDGE = ""

TIMEOUT = 25
#: Timeout for the first path
#: Budget for the first attempt when more than one path is configured (kept for the case where a
#: deployment adds its own second path): a network that blocks the first one must not spend the
#: whole timeout
#: before the fallback gets its turn (the portal answers in ~1.4 s when it is reachable).


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
        with net_tls.urlopen(req, timeout=timeout) as reply:
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
    """POST form-encoded fields to the portal on the one address it now has.

    The hub used to be a box of its own reached on :8899, with www.vlsc.net as a second path for
    networks that only allow 80/443. Both are one machine now, on one port, behind one name - and
    :8899 turned out never to have been open to the internet at all. A config written before the
    merge still names an old address; those are sent to the current one instead, because each of
    them now redirects there anyway.
    """
    legacy = ("https://portal.mrrc.vlsc.net:8899",
              "https://www.vlsc.net/mrrc_portal",
              "https://portal.mrrc.vlsc.net/mrrc_portal")
    base = PORTAL_DEFAULT if portal.rstrip("/") in legacy else portal
    try:
        return _post_once(base, route, payload, timeout)
    except _Unreachable as exc:
        raise CloudHubError(f"portal did not answer ({base}): {exc}") from exc


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
    # Delegated to the one writer (design D-8). What this replaces did three
    # things wrong at once: it dropped every comment in the file, it did not
    # hold a lock across its own read and write, and ``tmp.replace`` does not
    # inherit the mode of the file it replaces — so on the box, where the env
    # file holds the web password at 0640, the first Cloud Hub connect widened
    # it to 0644 and nothing about the file looked different afterwards.
    env_store.update_env_file(Path(config_path), updates)


def frpc_config_text(name: str, token: str, local_port: int, remote_port: int,
                     hub_host: str = "tunnel.mrrc.vlsc.net", control_port: int = 8989,
                     log_file: str = "", *, admin_port: int = 0,
                     admin_password: str = "", help_local_port: int = 0,
                     help_remote_port: int = 0) -> str:
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
        # Forward slashes: a backslash inside a TOML basic string is an escape, so a raw Windows
        # path makes the file unparseable - frpc reports "non-hex character" at the \U of
        # "C:\Users" and refuses to start, which is a silent 502 for the entry. Measured, at byte
        # level, after chasing it for a while. frpc accepts forward slashes on Windows.
        lines += [f'log.to = "{str(log_file).replace(chr(92), "/")}"', 'log.level = "info"', ""]
    if admin_port:
        # The admin API is where `frpc reload` lands. Loopback and Basic auth, both
        # always: an unauthenticated frpc admin API is a local port that can add
        # proxies. The password lives in this file, which is why the file is 0600
        # (see _write_frpc_config).
        lines += [
            'webServer.addr = "127.0.0.1"',
            f"webServer.port = {admin_port}",
            'webServer.user = "mrrc"',
            f'webServer.password = "{admin_password}"',
            "",
        ]
    lines += [
        "[[proxies]]",
        f'name = "{name}"',
        'type = "tcp"',
        'localIP = "127.0.0.1"',
        f"localPort = {local_port}",
        f"remotePort = {remote_port}",
    ]
    if help_local_port and help_remote_port:
        # Named per instance: two instances behind one frps claiming the same proxy
        # name is how the tunnel ends up holding a name somebody else needs
        # (measured 2026-10-11, see docs/2026-10-11-instance-lifecycle-and-port-squatting.md).
        lines += [
            "",
            "[[proxies]]",
            f'name = "mrrc-help-{name}"',
            'type = "tcp"',
            'localIP = "127.0.0.1"',
            f"localPort = {help_local_port}",
            f"remotePort = {help_remote_port}",
        ]
    return "\n".join(lines) + "\n"


def _write_frpc_config(conf: Path, text: str) -> None:
    """Write the tunnel config: ASCII, and 0600.

    The file carries the frps auth token and the admin password, so the default
    umask (usually 0644) left both readable by every local user — a tunnel can be
    hijacked with that token. Tightened on 2026-10-12 while adding the admin block;
    on Windows the mode is mostly decorative, but the call itself never fails the
    build.
    """
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(text, encoding="ascii")
    try:
        os.chmod(conf, 0o600)
    except OSError as exc:                      # pragma: no cover - platform dependent
        logger.warning("cloud hub: cannot tighten %s: %s", conf, exc)


def read_admin_settings(conf: Path) -> tuple[int, str, str]:
    """(port, user, password) of the frpc admin API as recorded in the config.

    Read back from the file rather than remembered in a second place: the file is
    the config, it is written 0600, and a second copy of a password is a second
    thing to leak. Returns (0, "", "") when the config predates the admin block.
    """
    try:
        text = conf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, "", ""
    port = re.search(r"^webServer\.port\s*=\s*(\d+)\s*$", text, re.MULTILINE)
    user = re.search(r'^webServer\.user\s*=\s*"([^"]*)"', text, re.MULTILINE)
    pw = re.search(r'^webServer\.password\s*=\s*"([^"]*)"', text, re.MULTILINE)
    return (int(port.group(1)) if port else 0,
            user.group(1) if user else "",
            pw.group(1) if pw else "")


def _free_loopback_port() -> int:
    """A port nothing is using right now, for the admin API."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def reload_frpc(admin_port: int, user: str, password: str, *, timeout: float = 5.0,
                wait_s: float = 3.0) -> None:
    """Ask the running frpc to re-read its config, over its admin API.

    **GET, not POST** — frp 0.71 answers `POST /api/reload` with 405 (measured against
    the real binary on 2026-10-12; the regression test here had a fake server that
    accepted POST, i.e. a mock that agrees with whatever it is asked. The test now
    speaks the real shape and pins the 405).

    Not `frpc reload -c …`: that needs the binary on PATH and a shell, and this runs
    inside a frozen app. Not a restart either — a restart drops the web tunnel for a
    few seconds, and "the customer pressed a button and their own remote session
    blipped" is not something to make routine. A failure here is raised, never
    silently escalated to a restart (design §4.2).

    ``wait_s`` covers the one timing fact worth waiting for: the admin listener is a
    *local* service that may be a moment behind the config write, and a connection
    refused right after a start is not yet a verdict. A protocol error (405 and
    friends) is a verdict and fails immediately — retrying that would only turn a
    wrong method into a slow wrong method.
    """
    import base64
    import urllib.error
    import urllib.request

    cred = base64.b64encode(f"{user}:{password}".encode()).decode()
    url = f"http://127.0.0.1:{int(admin_port)}/api/reload"
    deadline = time.monotonic() + max(0.0, wait_s)
    last: Exception | None = None
    while True:
        req = urllib.request.Request(url, method="GET",
                                     headers={"Authorization": f"Basic {cred}"})
        try:
            # net_tls.urlopen, not urllib's: it is this repo's single outbound entry
            # point (the AST guard in tests/test_tls_trust_store.py enforces it, and it
            # caught this call). For a plain http:// loopback URL the TLS context is
            # unused anyway — but a second, unguarded door is what that guard is for.
            with net_tls.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise CloudHubError(f"frpc reload returned HTTP {resp.status}")
                return
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200]
            raise CloudHubError(
                f"frpc reload returned HTTP {exc.code}: {detail!r}") from exc
        except OSError as exc:
            last = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
    raise CloudHubError(
        f"frpc admin API unreachable on 127.0.0.1:{admin_port} after {wait_s:.1f}s: "
        f"{last}")


def help_request(portal: str, callsign: str, token: str, minutes: int,
                 username: str = "", *, timeout: int = 15) -> dict:
    """Ask the portal for a help port. Returns its reply (``port``, ``expires_at``).

    The body carries the username and never the password: the portal has no field
    for one, which is the point (design §4.3).
    """
    return _post(portal, "/help/request",
                 {"callsign": callsign, "token": token, "minutes": int(minutes),
                  "username": username}, timeout)


def help_heartbeat(portal: str, callsign: str, token: str, port: int,
                   connections: int = 0, *, timeout: int = 10) -> dict:
    """20 s liveness, and the way a portal-side stop arrives: the reply's ``stop``."""
    return _post(portal, "/help/heartbeat",
                 {"callsign": callsign, "token": token, "port": int(port),
                  "connections": int(connections)}, timeout)


def help_close(portal: str, callsign: str, token: str, *, timeout: int = 10) -> dict:
    """Tell the portal the window is over, whatever ended it."""
    return _post(portal, "/help/close", {"callsign": callsign, "token": token},
                 timeout)


def read_tunnel_settings(conf: Path) -> dict:
    """Everything the generator needs to rewrite this config, read back from it.

    Parsed rather than remembered: the caller holds a path, not a set of
    credentials, and a second copy of these values is a second thing that can go
    stale (or leak). Returns zeros/empties for anything missing.
    """
    out: dict = {"server_addr": "", "server_port": 0, "token": "", "log_file": "",
                 "admin_port": 0, "admin_user": "", "admin_password": "",
                 "proxies": []}
    try:
        text = conf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    current: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line == "[[proxies]]":
            current = {}
            out["proxies"].append(current)
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip().strip('"')
        target = current if current is not None else out
        if key == "serverAddr":
            out["server_addr"] = value
        elif key == "serverPort":
            out["server_port"] = int(value or 0)
        elif key == "auth.token":
            out["token"] = value
        elif key == "log.to":
            out["log_file"] = value
        elif key == "webServer.port":
            out["admin_port"] = int(value or 0)
        elif key == "webServer.user":
            out["admin_user"] = value
        elif key == "webServer.password":
            out["admin_password"] = value
        elif current is not None and key in ("name", "localPort", "remotePort"):
            if key == "name":
                current["name"] = value
            else:
                current[key] = int(value or 0)
    return out


def ensure_admin_segment(conf: Path) -> int:
    """Make sure the tunnel config has an admin API; return its port (0 = cannot).

    frpc reads its config **once, at startup**, so this may only run on the way to
    starting a tunnel — never while one is live (a minted port would be a port the
    running frpc has never heard of; that is the 2026-10-12 defect this whole
    segment exists to avoid).

    Why an instance can be missing it at all: the admin segment entered the
    generator on 2026-10-12, and nothing rewrites the config of an instance that
    is already enrolled — `connect()` is not called again, and `set_help_proxy()`
    refuses to invent one because the live frpc would not know it. The measured
    result (2026-10-17, box .217, v1.25.5): every 「开启协助」 answered
    "frpc admin API unreachable on 127.0.0.1:0", and the 0 is the tell — it is the
    parser reporting that the file has no `webServer.port`.
    """
    admin_port, _user, _pw = read_admin_settings(conf)
    if admin_port:
        return admin_port
    settings = read_tunnel_settings(conf)
    web = [p for p in settings["proxies"]
           if not str(p.get("name", "")).startswith("mrrc-help-")]
    if not web:
        return 0                     # 不是一条能重建的隧道配置，别去改它
    main = web[0]
    admin_port = _free_loopback_port()
    password = secrets.token_urlsafe(18)
    _write_frpc_config(conf, frpc_config_text(
        str(main.get("name") or ""), settings["token"],
        int(main.get("localPort") or 0), int(main.get("remotePort") or 0),
        hub_host=settings["server_addr"] or "tunnel.mrrc.vlsc.net",
        control_port=settings["server_port"] or 8989,
        log_file=settings["log_file"],
        admin_port=admin_port, admin_password=password))
    logger.warning(
        "cloud hub: %s 没有管理段（早于 2026-10-12 的配置）—— 在 127.0.0.1:%d 补一个，"
        "否则「开启协助」无法热加载", conf.name, admin_port)
    return admin_port


def set_help_proxy(conf: Path, *, help_local_port: int = 0, help_remote_port: int = 0,
                   remove: bool = False) -> None:
    """Add (or drop) the help proxy in the tunnel config and hot-reload frpc.

    Rewrites the whole file from the same generator that wrote it, so the web proxy
    cannot be lost by an edit that only meant to add a second one. Everything else
    (token, hub address, the web proxy's ports) comes back out of the file.
    """
    settings = read_tunnel_settings(conf)
    web = [p for p in settings["proxies"]
           if not str(p.get("name", "")).startswith("mrrc-help-")]
    if not web:
        raise CloudHubError(f"no tunnel proxy to preserve in {conf}")
    if not settings["admin_port"]:
        # 0 就是“这份配置里没有 webServer 段”的标记（`read_admin_settings` 的文档说的）。
        # 不先拦下来的话，`reload_frpc(0, …)` 会去连 127.0.0.1:0 并重试 3 秒，最后报回一句
        # “admin API unreachable on 127.0.0.1:0”—— 端口 0 本身才是线索，却不是能行动的话。
        # 这个状态是真实的：2026-10-17 盒子 .217（v1.25.5，入网早于管理段进生成器）
        # 上每一次「开启协助」都倒在这里。修法是重启一次应用（启动时会补上），见
        # `ensure_admin_segment`。
        raise CloudHubError(
            "这条隧道的配置里没有管理段（webServer）—— 远程协助无法热加载。"
            "重启一次应用就会自动补上，再试一次即可。")
    main = web[0]
    text = frpc_config_text(
        str(main.get("name") or ""), settings["token"],
        int(main.get("localPort") or 0), int(main.get("remotePort") or 0),
        hub_host=settings["server_addr"] or "tunnel.mrrc.vlsc.net",
        control_port=settings["server_port"] or 8989,
        log_file=settings["log_file"],
        admin_port=settings["admin_port"], admin_password=settings["admin_password"],
        help_local_port=0 if remove else help_local_port,
        help_remote_port=0 if remove else help_remote_port)
    _write_frpc_config(conf, text)
    reload_frpc(settings["admin_port"], settings["admin_user"] or "mrrc",
                settings["admin_password"])


#: Windows: keep the helper processes this module runs (PowerShell, taskkill) from flashing a
#: console window in the operator's face.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _windows_norm(text: str) -> str:
    """Compare two paths the way the platforms this runs on spell them: case-insensitive, either
    slash.

    Still named for Windows, where that case difference is what bit; macOS is case-insensitive by
    default and takes the same rule, so the name is now narrower than the job. Deliberately not
    ``os.path.normcase``: that applies the *running* platform's rules, which would make the
    comparison mean something different in a test than in the field, and the case difference is
    exactly what bit. Linux is the one place it over-matches - two instances whose labels differ
    only in case would sweep each other - which is the shape Windows already has.
    """
    return text.replace("/", "\\").lower()


def _stale_frpc_pids(output: str, config_path: Path) -> list[int]:
    """The PIDs in ``output`` whose command line names ``config_path``.

    ``output`` is one ``pid<TAB>command line`` per process, the shape ``_enumerate_frpc`` returns.
    Only this instance's own config counts, so a second instance on the same machine keeps its
    tunnel. The match is case-insensitive because Windows paths are: the substring test that used
    to be here was not, so a path differing only in case matched nothing and the stale process
    survived to fight the next start for its own proxy name.
    """
    wanted = _windows_norm(str(config_path))
    pids: list[int] = []
    for line in output.splitlines():
        pid, _, command = line.partition("\t")
        pid = pid.strip()
        if pid.isascii() and pid.isdigit() and wanted in _windows_norm(command):
            pids.append(int(pid))
    return pids


def _enumerate_frpc() -> str:
    """Every frpc on this machine, as ``pid<TAB>command line`` lines.

    Windows: PowerShell's CIM cmdlets, not ``wmic``: wmic is gone from Windows 11 - measured
    2026-10-04 on a field Windows box, where ``Get-Command wmic`` finds nothing - and the bare
    ``except`` that used to wrap it turned that into a silent no-op, so every app start left
    another frpc behind. Six were still running after two days, all claiming one proxy name; the
    hub logged 7143 "proxy already exists" warnings in a single day because of it, and none of it
    was visible from this machine. Output encoding is forced to UTF-8 so a non-ASCII path survives
    the pipe.

    Everywhere else: ``ps``. macOS is where the same leak was measured again on 2026-10-06 - six
    orphans from six app starts in half an hour - because the sweep returned before it ever
    reached this function. Only processes whose own name is ``frpc`` are listed: an editor holding
    the config open names that path on its command line too, and the sweep must not take it for
    an frpc and terminate it.
    """
    if os.name != "nt":
        done = subprocess.run(["ps", "-eo", "pid=,command="],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=20)
        if done.returncode != 0:
            raise OSError(f"ps exited {done.returncode}: {(done.stderr or '').strip()[:200]}")
        lines = []
        for line in done.stdout.splitlines():
            pid, _, command = line.strip().partition(" ")
            command = command.strip()
            if pid.isdigit() and os.path.basename(command.split(" ", 1)[0]) == "frpc":
                lines.append(f"{pid}\t{command}")
        return "\n".join(lines) + "\n" if lines else ""

    script = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
              "Get-CimInstance Win32_Process -Filter \"Name='frpc.exe'\" | "
              "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
    done = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=20, creationflags=_NO_WINDOW)
    if done.returncode != 0:
        raise OSError(f"powershell exited {done.returncode}: {(done.stderr or '').strip()[:200]}")
    return done.stdout


def _kill_stale_frpc(config_path: Path) -> None:
    """Kill an frpc left behind by a previous run of this instance, if there is one.

    Measured: killing the app can leave its frpc running, and the next start then cannot register
    the same proxy name - the tunnel stays down with nothing in the UI to say why. Only a process
    whose command line names this instance's own config is touched, so a second instance on the
    same machine is not disturbed.

    Best effort, but never silent. If the look-up itself fails the operator gets a warning: this
    failure is invisible from here (the symptom shows up in the hub's log, not in this app), which
    is how it went unnoticed for two days while the old code swallowed it.
    """
    try:
        output = _enumerate_frpc()
    except Exception as exc:                                     # noqa: BLE001 - best effort
        logger.warning("cloud hub: cannot list frpc processes (%s: %s); a stale one would hold "
                       "this instance's proxy name and the tunnel would stay down",
                       exc.__class__.__name__, exc)
        return
    for pid in _stale_frpc_pids(output, config_path):
        logger.info("cloud hub: clearing a stale frpc (pid %s) holding this instance's tunnel", pid)
        _terminate(pid)


def _terminate(pid: int) -> None:
    """End one stale frpc. Never raises.

    SIGTERM off Windows so frpc can close its control connection and the hub drops the proxy name
    there and then; ``taskkill /F`` is all Windows offers.

    The listing is a snapshot, so the pid can be gone by the time we get here - a stale process is
    the one thing on this machine most likely to exit on its own. That case is the sweep having
    nothing left to do, not a failure. It matters that this never raises: the caller is
    ``TunnelProcess.__init__``, and an exception there takes the tunnel setup, and the endpoint
    that triggered it, down with it. Anything else is worth the operator's attention.
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10, creationflags=_NO_WINDOW)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("cloud hub: cannot terminate a stale frpc (pid %s): %s", pid, exc)


class TunnelProcess:
    """Keep one frpc alive, restarting it if it exits.

    The app runs it as a child rather than a service: the tunnel is only useful while the app is
    up, and a child needs no elevation and no scheduler entry to manage.
    """

    def __init__(self, frpc: Path, conf: Path):
        self.frpc = Path(frpc)
        self.conf = Path(conf)
        _kill_stale_frpc(self.conf)
        self.proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        # Sweep here as well as in __init__, because the config this supervisor was
        # *constructed* with is not always the one it runs with: the lazy creator uses
        # a placeholder (``pending.toml``) and connect() assigns the real path just
        # before calling this. A sweep keyed to the placeholder finds nothing, so an
        # frpc left behind by a previous run of this instance survives, keeps the proxy
        # name, and the next one logs "proxy [bg1sb] already exists" while the hub sees
        # only the old process. Measured on 2026-10-12: two frpc processes, two admin
        # ports, one working tunnel, and `call help` failing because the live frpc was
        # not the one the config described.
        _kill_stale_frpc(self.conf)
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
    # The admin API exists so `call help` can add a proxy without restarting the
    # tunnel (which would drop the operator's own web session for a few seconds).
    #
    # Minted once, then reused from the file — because frpc reads its config exactly
    # once, at startup. Picking a fresh port on every connect() rewrote the file with
    # a port the *running* frpc had never heard of, so the reload went to a closed
    # port and `call help` failed with "admin API unreachable" right after a
    # successful reconnect (measured on the first real run, 2026-10-12: config said
    # 53235 while the live frpc listened on 53189).
    admin_port, _, admin_password = read_admin_settings(conf)
    if not admin_port:
        admin_port = _free_loopback_port()
        admin_password = secrets.token_urlsafe(18)
    _write_frpc_config(
        conf,
        frpc_config_text(label, hub_token, local_port, port, log_file=log_file,
                         admin_port=admin_port, admin_password=admin_password),
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
        "entry": f"https://{fqdn}/",
        "cert": str(cert_path),
        "tunnel_config": str(conf),
        "tunnel_started": started,
    }
