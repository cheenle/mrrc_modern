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
import json
import os
import re
import socket
import threading
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
    text = str(host or "").strip()
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


# ── the root helper, client side ────────────────────────────────────
def _send_over_helper_socket(payload: dict, timeout: float) -> str:
    """One line of JSON out, one line back, over the helper's unix socket.

    The socket is 0660 root:mrrc, so the kernel refuses anyone else at connect()
    time; the helper checks SO_PEERCRED on top of that, because a mode is a check
    made once and the helper would rather make its own.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(HELPER_SOCKET)
        client.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        chunks = []
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
    return b"".join(chunks).decode("utf-8", "replace").split("\n", 1)[0]


def call_helper(verb: str, *args, sender: Optional[Callable] = None,
                timeout: float = 10.0) -> dict:
    """Ask the root helper to do one of its four verbs. Never raises.

    Absent helper, refused connection, an unreadable reply: all of them are
    `{"ok": False, "error": ...}`. The settings page has to grey a panel out and
    keep the other panels working, and it cannot do that from an exception.
    """
    payload = {"verb": verb, "args": [str(arg) for arg in args]}
    if sender is None:
        sender = _send_over_helper_socket
    try:
        reply_text = sender(payload, timeout)
    except OSError as exc:
        return {"ok": False,
                "error": f"helper unavailable ({exc.__class__.__name__})"}
    try:
        reply = json.loads(reply_text)
    except (TypeError, ValueError):
        return {"ok": False, "error": "helper sent an unreadable reply"}
    if not isinstance(reply, dict) or "ok" not in reply:
        return {"ok": False, "error": "helper sent an unreadable reply"}
    return reply


# ── the four system verbs, validated before they are asked for ──────
#: Case-open on purpose: the shipped default is `MRRC-Modern`. systemd accepts a
#: static hostname in either case and DNS is case-insensitive, so
#: `mrrc-modern.local` still resolves (design §8).
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")

ZONEINFO = "/usr/share/zoneinfo"


def hostname_problem(name: str) -> str:
    """Why this name cannot be the box's hostname, or "" when it can."""
    text = (name or "").strip()
    if not text:
        return "名字不能为空"
    if not HOSTNAME_RE.match(text):
        return ("名字只能用字母、数字和连字符，不能以连字符开头或结尾，"
                "最长 63 个字符")
    return ""


def timezone_problem(zone: str) -> str:
    """Why this zone cannot be set, or "" when it can.

    The helper checks all of this again before running anything as root. This
    copy exists so the page reports its own wording rather than a round trip's,
    not as a substitute for the one that guards the privileged side.
    """
    text = (zone or "").strip()
    if not text or text != zone or text.startswith("/") or ".." in text:
        return "时区名不合法"
    if not os.path.isfile(os.path.join(ZONEINFO, text)):
        return f"没有这个时区：{text}"
    return ""


def set_hostname(name: str, *, sender: Optional[Callable] = None) -> dict:
    problem = hostname_problem(name)
    if problem:
        return {"ok": False, "error": problem}
    return call_helper("set-hostname", (name or "").strip(), sender=sender)


def set_timezone(zone: str, *, sender: Optional[Callable] = None) -> dict:
    problem = timezone_problem(zone)
    if problem:
        return {"ok": False, "error": problem}
    return call_helper("set-timezone", (zone or "").strip(), sender=sender)


def reboot(*, sender: Optional[Callable] = None) -> dict:
    """Restart the box, not the service. `POST /api/restart` is the other one."""
    return call_helper("reboot", sender=sender)


def reload_avahi(*, sender: Optional[Callable] = None) -> dict:
    """Nudge avahi after a rename, so the new name is advertised immediately."""
    return call_helper("avahi-reload", sender=sender)


# ── the password ────────────────────────────────────────────────────
#: The floor the existing POST /api/setup enforces. The onboarding page must not
#: be the softer door into the same key.
MIN_PASSWORD_LEN = 8


def password_problem(password: str, confirm: str) -> str:
    """Why this password cannot be set, or "" when it can.

    Nothing is trimmed: a password is used byte-for-byte, and silently removing a
    leading space would set a password the user did not type — which they would
    discover at the login page.
    """
    if not password:
        return "口令不能为空"
    if len(password) < MIN_PASSWORD_LEN:
        return f"口令至少 {MIN_PASSWORD_LEN} 位"
    if password != confirm:
        return "两次输入的口令不一致"
    return ""


# ── the Wi-Fi switch ────────────────────────────────────────────────
PHASE_IDLE = "idle"
PHASE_SWITCHING = "switching"
PHASE_OK = "ok"
PHASE_FAILED = "failed"

#: How long a new network has to produce an IPv4 before we put the old one back.
#: A first guess, to be calibrated on the real board (spec R-4).
SWITCH_BUDGET_S = 30.0
SWITCH_POLL_S = 1.0

#: What nmcli calls a Wi-Fi connection. Older and newer builds disagree.
WIFI_TYPES = ("802-11-wireless", "wifi")


class SwitchState:
    """The one switch this process is running, or the last one it ran.

    In memory, not on disk. The retired design published state through a JSON
    file because a root supervisor and the server were two processes; the switch
    now happens inside the server, so there is no second reader. The cost is that
    a server restart loses the progress screen, and the page says so rather than
    pretending (design §6.2).

    The field list is closed on purpose: `snapshot()` is what a page renders, and
    a PSK must have no key it could occupy.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.phase = PHASE_IDLE
        self.ssid = ""
        self.reason = ""
        self.address = ""
        self.since = 0.0

    def snapshot(self) -> dict:
        with self.lock:
            return {"phase": self.phase, "ssid": self.ssid,
                    "reason": self.reason, "address": self.address,
                    "since": self.since}

    def reset(self) -> None:
        with self.lock:
            self.phase = PHASE_IDLE
            self.ssid = ""
            self.reason = ""
            self.address = ""
            self.since = 0.0


#: The process-wide instance the endpoints read. Tests build their own.
_state = SwitchState()


def switch_state(state: Optional[SwitchState] = None) -> dict:
    return (state or _state).snapshot()


def _active_wifi_profiles(runner: Optional[Callable] = None) -> list:
    """The Wi-Fi connections that are up right now — the rollback targets."""
    result = net_wifi.nmcli(
        ["-t", "-f", "NAME,TYPE", "connection", "show", "--active"], runner)
    if not result.ok:
        return []
    return [row[0] for row in net_wifi.parse_rows(result.stdout)
            if len(row) >= 2 and row[1] in WIFI_TYPES]


def _finish_switch(state: SwitchState, ok: bool, reason: str,
                   address: str = "") -> dict:
    with state.lock:
        state.phase = PHASE_OK if ok else PHASE_FAILED
        state.reason = reason
        state.address = address
        state.since = time.time()
        ssid = state.ssid
    return {"ok": ok, "ssid": ssid, "reason": reason, "address": address}


def _roll_back(state: SwitchState, ssid: str, previous: list, password: str,
               budget: float, runner: Optional[Callable] = None) -> dict:
    """Put the box back on the network it came from, then say so.

    The failed profile is left in place but with `autoconnect no`, so a reboot
    does not retry a network that just failed. That is the retired hotspot's
    mistake in the other direction — its profile defaulted to yes, came up at
    every boot, and took the board's Wi-Fi down with it.
    """
    net_wifi.nmcli(["connection", "modify", ssid,
                    "connection.autoconnect", "no"], runner)
    for name in previous:
        net_wifi.nmcli(["connection", "up", name], runner)
    back_to = "、".join(previous) if previous else "（没有旧网络可回退）"
    reason = f"「{ssid}」在 {int(budget)} 秒内没有拿到地址，已回退到 {back_to}"
    return _finish_switch(state, False, net_wifi.scrub(reason, password))


def perform_wifi_switch(ssid: str, password: str = "", *,
                        runner: Optional[Callable] = None,
                        state: Optional[SwitchState] = None,
                        ifname: Optional[str] = None,
                        budget: float = SWITCH_BUDGET_S,
                        poll: float = SWITCH_POLL_S,
                        sleep: Callable = time.sleep,
                        now: Callable = time.monotonic) -> dict:
    """Join `ssid`, and fall back to whatever we were on if it does not take.

    The ordering, and why each step is where it is (design §6.2):

    1. Note the profiles that are up *before* touching anything. They are the
       rollback targets, and after `nmcli device wifi connect` they are no longer
       the active ones.
    2. Hand off to `net_wifi.connect_wifi`, which writes the new profile and
       brings it up. The password travels in its argv and nowhere else.
    3. Poll for an IPv4. nmcli returns once it has associated, which is well
       before DHCP has landed — believing that return value is how a switch looks
       successful and leaves the box with no address.
    4. On timeout, bring the old profile back and report why, scrubbed.

    Blocking on purpose: `server.py` runs this on a worker thread via
    `asyncio.to_thread`, so the state machine stays plain synchronous Python and
    this file keeps needing no event loop to be tested.
    """
    target = state or _state
    with target.lock:
        if target.phase == PHASE_SWITCHING:
            return {"ok": False, "ssid": ssid,
                    "reason": "已经有一次切换在进行中", "address": ""}
        target.phase = PHASE_SWITCHING
        target.ssid = ssid
        target.reason = ""
        target.address = ""
        target.since = time.time()

    previous = _active_wifi_profiles(runner)

    result = net_wifi.connect_wifi(ssid, password, runner=runner, ifname=ifname)
    if not result.ok:
        detail = net_wifi.scrub(result.stderr or result.stdout, password)
        return _finish_switch(target, False,
                              detail.strip() or f"无法连接「{ssid}」")

    address = ""
    deadline = now() + budget
    while now() < deadline:
        address = net_wifi.ipv4_address(ifname, runner)
        if address:
            break
        sleep(poll)

    if address:
        return _finish_switch(target, True, "", address)
    return _roll_back(target, ssid, previous, password, budget, runner)
