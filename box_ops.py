#!/usr/bin/env python3
"""The box's own operating system, as the web pages need to talk about it.

Why this module exists
----------------------
`/setup` (the onboarding page) and `/settings` (the settings page) show the same
facts and drive the same actions. One backend, two front ends: a second
implementation of "switch the Wi-Fi" is how the two pages start disagreeing about
what happened.

Two rules, both inherited from `net_wifi.py` and load-bearing for the same
reasons:

1. **Stdlib only, no application imports.** `server.py` imports this, and
   PyInstaller freezes its root modules into every desktop build. Windows and
   macOS have no NetworkManager and no helper, so the import must succeed and
   every call must degrade to a plain failure (spec R-6) — the pages grey their
   buttons out instead of returning a 500.
2. **A Wi-Fi password never leaves a function's locals.** It reaches an argv
   that `net_wifi` builds and nothing else: not a status dict, not a log line,
   not an exception message, and not the helper socket. `switch_state()` is what
   the pages read, and it has no field a PSK could occupy.

Everything that shells out takes an injectable `runner` with `net_wifi.NmResult`'s
shape, and the Wi-Fi switch additionally takes an injectable `sleep` and clock —
the same bet `net_wifi.py` made, and the reason its state machine can be tested
here without NetworkManager, a radio or a network.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import time
from typing import Any, Callable, Optional

import net_wifi

# ── constants ───────────────────────────────────────────────────────
#: The name the box ships with (design §8). Not `armbian`. Case is deliberate
#: and is why the hostname validator allows uppercase.
DEFAULT_HOSTNAME = "MRRC-Modern"

#: The root helper's socket. Its absence is a normal state, not an error: the
#: desktop builds have no helper and the box's helper may be restarting.
HELPER_SOCKET = "/run/mrrc/box-helper.sock"

#: The units the settings page reports. `mrrc-modern` is this server; the other
#: two are the ones a broken network is usually hiding behind.
SERVICES = ("mrrc-modern.service", "NetworkManager.service",
            "avahi-daemon.service")

#: nmcli prints a boolean as yes/no in the C locale and 是/否 in a Chinese one.
#: The box's locale is not ours to pin, and the symptom of getting this wrong is
#: a user's own autoconnect switch rendering as off.
YES_WORDS = ("yes", "是")
NO_WORDS = ("no", "否")

#: The safety invariants shown on the settings page. Every one is read-only:
#: that page reports, it does not set. The notes travel with the values because a
#: number without its consequence beside it is a number nobody respects.
SAFETY_FIELDS = (
    ("MRRC_ALLOW_UNVERIFIED_TX", "0",
     "发射门禁。为 1 时允许对未验证机型发射，指令表来自 Hamlib/手册而非真机实测；"
     "8 个未验证机型必须为 0（AD-019 / NFR-067）。改它要两步显式确认。"),
    ("MRRC_PTT_MAX_TX_SECONDS", "0",
     "PTT 最长连续发射秒数，0 = 关闭看门狗。调高或清空会削弱防线②。"),
    ("MRRC_REMOTE_SESSION_TX_HEARTBEAT_S", "",
     "远程会话的发射心跳超时，由 Cloud Hub 写入（防线①）。本页不改。"),
)


# ── transport ───────────────────────────────────────────────────────
def _run(argv: list, runner: Optional[Callable] = None) -> net_wifi.NmResult:
    """Run argv, or hand it to the injected runner. Same shape either way.

    Everything here that is not `nmcli` (systemctl, hostnamectl, timedatectl)
    goes through this too, so a test can answer all of it from one fake.
    """
    if runner is not None:
        return runner(argv)
    return net_wifi.subprocess_runner(argv)


def _values(field: str, runner: Optional[Callable] = None) -> list:
    """One nmcli `-g` field across every device, blanks dropped."""
    result = net_wifi.nmcli(["-t", "-g", field, "device", "show"], runner)
    if not result.ok:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# ── the box's own networks ──────────────────────────────────────────
def local_subnets(runner: Optional[Callable] = None) -> list:
    """Every IPv4 network this box holds an address on.

    The onboarding window asks "is the client on one of my own subnets". The
    retired hotspot design could ask that about a single known network because it
    had created it; a box that arrives over a cable or a Wi-Fi somebody else
    configured has to ask itself.

    An empty list is the failure-safe answer, not an error: no addresses means no
    window (spec §5.1).
    """
    out = []
    for text in _values("IP4.ADDRESS", runner):
        try:
            out.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            continue
    return out


def client_in_local_subnet(host: Optional[str],
                           runner: Optional[Callable] = None) -> bool:
    """Whether `host` is on a subnet this box itself holds an address on.

    Accepts the IPv4-mapped form Starlette produces for a v4 client on a v6
    socket (`::ffff:192.168.16.50`) — refusing that form would close the window
    on exactly the client it was opened for.
    """
    text = (host or "").strip()
    if not text:
        return False
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.version != 4:
        return False
    return any(address in network for network in local_subnets(runner))


# ── read-only network view ──────────────────────────────────────────
def scan_networks(runner: Optional[Callable] = None) -> list:
    """Visible Wi-Fi, strongest first. The retired wizard's scan, unchanged."""
    return net_wifi.scan_wifi(runner)


def wifi_status(runner: Optional[Callable] = None) -> dict:
    """The station link, if there is one: SSID and signal."""
    result = net_wifi.nmcli(
        ["-t", "-f", "ACTIVE,SSID,SIGNAL", "device", "wifi", "list"], runner)
    if result.ok:
        for row in net_wifi.parse_rows(result.stdout):
            if len(row) >= 3 and row[0].strip() in YES_WORDS:
                return {"ssid": row[1], "signal": net_wifi._int_or(row[2])}
    return {"ssid": "", "signal": 0}


def connections_report(runner: Optional[Callable] = None) -> list:
    """Every saved connection, with the one field a user has to be able to see.

    `autoconnect` is here because its mishandling is a field-verified failure
    mode: a profile that brings itself up at boot is how a box takes its own
    Wi-Fi down — the retired hotspot's `Hotspot` profile did exactly that, and
    every profile this module writes states the value explicitly.
    """
    result = net_wifi.nmcli(
        ["-t", "-f", "NAME,TYPE,AUTOCONNECT", "connection", "show"], runner)
    if not result.ok:
        return []
    out = []
    for row in net_wifi.parse_rows(result.stdout):
        if len(row) < 3:
            continue
        out.append({"name": row[0], "type": row[1],
                    "autoconnect": row[2].strip() in YES_WORDS})
    return out


def network_report(runner: Optional[Callable] = None) -> dict:
    """The network health card: where it broke, in one glance.

    Every call is unprivileged — this is the half of the retired `/api/manage`
    worth keeping, plus the two facts (default route holder, per-profile
    autoconnect) that a user actually needs when the box is unreachable.
    """
    return {
        "addresses": _values("IP4.ADDRESS", runner),
        "gateways": _values("IP4.GATEWAY", runner),
        "dns": _values("IP4.DNS", runner),
        "devices": net_wifi.devices(runner),
        "connections": connections_report(runner),
        "wifi": wifi_status(runner),
        "nm_running": net_wifi.nm_running(runner),
    }


# ── read-only system view ───────────────────────────────────────────
def _unit_active(unit: str, runner: Optional[Callable] = None) -> bool:
    result = _run(["systemctl", "is-active", unit], runner)
    return result.ok and result.stdout.strip() == "active"


def services_report(runner: Optional[Callable] = None) -> list:
    """Read-only: `mrrc` may ask systemd about a unit without any helper."""
    return [{"unit": unit, "active": _unit_active(unit, runner)}
            for unit in SERVICES]


def timezone() -> str:
    """The IANA zone name, or "" when it cannot be read.

    /etc/localtime is a symlink into /usr/share/zoneinfo everywhere Debian-ish;
    when it is a plain file (somebody copied it) the name is simply unknown, and
    an empty string is the honest answer.
    """
    marker = "/usr/share/zoneinfo/"
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return ""
    index = target.find(marker)
    return target[index + len(marker):] if index >= 0 else ""


def uptime_seconds() -> float:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def system_report(runner: Optional[Callable] = None) -> dict:
    return {
        "timezone": timezone(),
        "uptime_s": uptime_seconds(),
        "services": services_report(runner),
    }


def identity_report(runner: Optional[Callable] = None) -> dict:
    """The box's name, and both ways a client can reach it afterwards.

    `<name>.local` is perfect on iOS, needs Bonjour installed on Windows and is a
    lottery on Android, so the addresses travel with it — after a network switch
    they are the fallback that always works (design §8).
    """
    name = socket.gethostname() or ""
    return {
        "hostname": name,
        "mdns": f"{name}.local" if name else "",
        "addresses": [value.split("/")[0]
                      for value in _values("IP4.ADDRESS", runner)],
        "suggested_name": DEFAULT_HOSTNAME,
    }


def safety_report(environ: Optional[Any] = None) -> list:
    """The transmit gates, reported and never set.

    A web switch is the shape that turns an invariant into an incident, so these
    are read-only here; changing one belongs with the config layer and its two
    confirmations.
    """
    env = os.environ if environ is None else environ
    return [{"key": key, "value": env.get(key, default),
             "read_only": True, "note": note}
            for key, default, note in SAFETY_FIELDS]


# ── the root helper ─────────────────────────────────────────────────
def helper_available(path: str = HELPER_SOCKET) -> bool:
    """Whether the helper's socket is there. Deliberately just a path check.

    It is allowed to be wrong in one direction only: it may say False when the
    helper would in fact have answered. Saying True with no socket behind it puts
    a button on a page that cannot work.
    """
    return bool(path) and os.path.exists(path)
