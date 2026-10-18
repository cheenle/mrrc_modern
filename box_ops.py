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
def _send_over_helper_socket(payload: dict, timeout: float,
                             path: Optional[str] = None) -> str:
    """One line of JSON out, one line back, over the helper's unix socket.

    The socket is 0660 root:mrrc, so the kernel refuses anyone else at connect()
    time; the helper checks SO_PEERCRED on top of that, because a mode is a check
    made once and the helper would rather make its own.

    `path` mirrors `helper_available`'s injected path: omit it for the real
    socket, pass one to send somewhere else. It is resolved against
    `HELPER_SOCKET` at call time (not bound as a default argument) so that
    pointing the module constant elsewhere — as the tests do — moves the
    transport with it. An injected path is then honored by both halves, so the
    availability check and the transport always agree on which socket they mean.
    """
    socket_path = HELPER_SOCKET if path is None else path
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(socket_path)
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


#: The accounts the box page may change a password for: the two a shipping
#: image sets (root and the service user).
SYSTEM_PASSWORD_USERS = ("root", "mrrc")
SYSTEM_PASSWORD_MIN = 8


def system_password_problem(user: str, password: str) -> str:
    """Why this password change cannot be made, or "" when it can.

    The helper validates the same thing and is the authority (it is the one that
    runs `chpasswd`); this mirror exists so the page gets a 400 with a sentence
    instead of a round trip to a root process for a typo.
    """
    if user not in SYSTEM_PASSWORD_USERS:
        return "只能改这几个账号：" + "、".join(SYSTEM_PASSWORD_USERS)
    if len(password or "") < SYSTEM_PASSWORD_MIN:
        return f"口令至少 {SYSTEM_PASSWORD_MIN} 位"
    if any(ch in password for ch in "\r\n\x00"):
        return "口令里不能有换行或 NUL"
    return ""


def set_system_password(user: str, password: str, *,
                        sender: Optional[Callable] = None) -> dict:
    """Change root / mrrc / rescue's password through the root helper.

    The secret travels as a helper argument over a 0660 root:mrrc Unix socket
    (peer-checked by SO_PEERCRED) and into `chpasswd` on stdin — never into argv,
    never into a log line, never into a status dict.
    """
    problem = system_password_problem(user, password)
    if problem:
        return {"ok": False, "error": problem}
    return call_helper("set-password", user, password, sender=sender)


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
        #: A failed switch that nonetheless left a usable profile behind (the
        #: access point was simply out of range). Booleans only: the field list is
        #: closed so a PSK has no key it could occupy.
        self.saved = False

    def snapshot(self) -> dict:
        with self.lock:
            return {"phase": self.phase, "ssid": self.ssid,
                    "reason": self.reason, "address": self.address,
                    "since": self.since, "saved": self.saved}

    def reset(self) -> None:
        with self.lock:
            self.phase = PHASE_IDLE
            self.ssid = ""
            self.reason = ""
            self.address = ""
            self.since = 0.0
            self.saved = False


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
                   address: str = "", saved: bool = False) -> dict:
    with state.lock:
        state.phase = PHASE_OK if ok else PHASE_FAILED
        state.reason = reason
        state.saved = saved
        state.address = address
        state.since = time.time()
        ssid = state.ssid
    return {"ok": ok, "ssid": ssid, "reason": reason, "address": address,
            "saved": saved}


def _roll_back(state: SwitchState, ssid: str, previous: list, reason: str,
               password: str, runner: Optional[Callable] = None,
               keep_autoconnect: bool = False) -> dict:
    """Leave a switch that did not take, then say which old network came back.

    Both failure paths — a `connect_wifi` that returned non-zero and a budget
    that ran out with no address — come through here, so the teardown is written
    once. `reason` is the path-specific lead-in (a connect error, or "no address
    in N seconds"); this function appends what actually happened to the old
    networks, so neither path can state the other's cause.

    A failed profile is normally left in place with `autoconnect no`, so a reboot
    does not retry a network that just failed. That is the retired hotspot's
    mistake in the other direction — its profile defaulted to yes, came up at
    every boot, and took the board's Wi-Fi down with it. `connection modify`
    against a profile that was never created returns non-zero, and — like
    `connection up` — that result is deliberately ignored.

    `keep_autoconnect` is the other half of that lesson (2026-10-17). When the
    switch failed because **the access point is not in range** — measured on a
    box prepared for a customer, whose AP only exists at their home — disabling
    the profile throws away the whole point of the operator's typing: the saved
    network has to be armed so it connects the first time it *is* in range.
    A rejected credential is different and keeps the old behaviour: retrying that
    at every boot can never succeed.

    The reason names only the profiles `connection up` actually reported as up:
    this string is what the operator reads over a dead network, so it may not
    claim a restore it never verified.
    """
    if not keep_autoconnect:
        net_wifi.nmcli(["connection", "modify", ssid,
                        "connection.autoconnect", "no"], runner)
    restored = [name for name in previous
                if net_wifi.nmcli(["connection", "up", name], runner).ok]
    if restored:
        back_to = "、".join(restored)
    elif previous:
        back_to = "（旧网络未能恢复）"
    else:
        back_to = "（没有旧网络可回退）"
    if keep_autoconnect:
        text = f"{reason}；「{ssid}」已保存，这个网络出现在范围内时自动连接"
        if previous:
            # The operator may be reading this over a Wi-Fi link that just went
            # away, so what happened to the old network is not a detail.
            text += f"（旧网络：{back_to}）"
    else:
        text = f"{reason}，已回退到 {back_to}"
    return _finish_switch(state, False, net_wifi.scrub(text, password),
                          saved=keep_autoconnect)


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

    # A non-positive interval would spin forever on an injected clock and would
    # make ``time.sleep`` raise out of an event-loop worker thread. It is refused
    # before any nmcli call, and after the claim above so a bad argument cannot
    # clobber a switch that is already running.
    if poll <= 0:
        return _finish_switch(target, False, "轮询间隔必须为正数")

    previous = _active_wifi_profiles(runner)

    result = net_wifi.connect_wifi(ssid, password, runner=runner, ifname=ifname)
    if not result.ok:
        detail = net_wifi.scrub(result.stderr or result.stdout, password)
        reason = detail.strip() or f"无法连接「{ssid}」"
        # "The AP is not here" is not a bad configuration — it is the normal
        # result of pre-configuring a network for someone else. Keep it armed.
        return _roll_back(target, ssid, previous, reason, password, runner,
                          keep_autoconnect=net_wifi.access_point_absent(detail))

    address = ""
    deadline = now() + budget
    while now() < deadline:
        address = net_wifi.ipv4_address(ifname, runner)
        if address:
            break
        sleep(poll)

    if address:
        return _finish_switch(target, True, "", address)
    return _roll_back(target, ssid, previous,
                      f"「{ssid}」在 {int(budget)} 秒内没有拿到地址",
                      password, runner)


# ── saved Wi-Fi profiles: add / edit / forget / arm ─────────────────
#: 802.11 caps an SSID at 32 octets (bytes, not characters: 中文 counts 3 each).
SSID_MAX_LEN = 32
#: WPA2-PSK is 8..63 characters. An empty password is an open network, which is
#: allowed on purpose — the operator gets exactly what they typed, and the
#: saved profile says so, instead of a silent "no security" surprise.
PSK_MIN_LEN, PSK_MAX_LEN = 8, 63


def ssid_problem(name: str) -> str:
    """Why this SSID cannot be saved, or "" when it can."""
    text = name or ""
    if not text.strip():
        return "请填 Wi-Fi 名称"
    if len(text.encode("utf-8")) > SSID_MAX_LEN:
        return f"Wi-Fi 名称最多 {SSID_MAX_LEN} 个字节（802.11 的规定，中文一个字占 3 个）"
    if any(ch in text for ch in "\r\n\t"):
        return "Wi-Fi 名称里不能有换行或制表符"
    return ""


def psk_problem(password: str) -> str:
    """Why this password cannot be saved, or "" (empty = open network)."""
    if not password:
        return ""
    length = len(password)
    if length < PSK_MIN_LEN or length > PSK_MAX_LEN:
        return (f"密码长度要在 {PSK_MIN_LEN}–{PSK_MAX_LEN} 之间（WPA2-PSK 的规矩），"
                f"现在是 {length}")
    return ""


def save_wifi(ssid: str, password: str = "", autoconnect: bool = True, *,
              runner: Optional[Callable] = None) -> dict:
    """Write (or replace) a saved Wi-Fi profile — **without** switching to it.

    The switch endpoint cannot do this job: it brings the profile up right away,
    which means waiting out the connect bound and rolling back when the AP is not
    in range — exactly the case of preparing a box for someone else's home
    (2026-10-17). Saving therefore never touches the active connection, and the
    PSK only ever reaches nmcli's argv (`scrub` covers its error text).
    """
    problem = ssid_problem(ssid) or psk_problem(password)
    if problem:
        return {"ok": False, "error": problem}
    written = net_wifi.write_profile(ssid, password, runner=runner)
    if not written.ok:
        detail = net_wifi.scrub(written.stderr or written.stdout, password)
        return {"ok": False, "error": detail.strip() or f"无法保存「{ssid}」"}
    if not autoconnect:
        net_wifi.set_autoconnect(ssid, False, runner=runner)
    return {"ok": True, "ssid": ssid, "autoconnect": bool(autoconnect)}


def forget_wifi(ssid: str, *, runner: Optional[Callable] = None) -> dict:
    """Delete one saved profile. The network stays connected until it drops."""
    if not (ssid or "").strip():
        return {"ok": False, "error": "没有指定要删的 Wi-Fi"}
    result = net_wifi.forget(ssid, runner)
    if not result.ok:
        detail = net_wifi.scrub(result.stderr or result.stdout)
        return {"ok": False, "error": detail.strip() or f"没有找到「{ssid}」"}
    return {"ok": True, "ssid": ssid}


def set_wifi_autoconnect(ssid: str, enabled: bool, *,
                         runner: Optional[Callable] = None) -> dict:
    """Arm or disarm a saved profile — the difference between "今天连" and "以后连"."""
    if not (ssid or "").strip():
        return {"ok": False, "error": "没有指定 Wi-Fi"}
    result = net_wifi.set_autoconnect(ssid, bool(enabled), runner)
    if not result.ok:
        detail = net_wifi.scrub(result.stderr or result.stdout)
        return {"ok": False, "error": detail.strip() or f"改不了「{ssid}」"}
    return {"ok": True, "ssid": ssid, "autoconnect": bool(enabled)}


# ── the wired profile: DHCP or static ───────────────────────────────
#: `auto` is NM's name for DHCP; `manual` is its name for a static address.
LINK_METHODS = ("auto", "manual")


def link_problem(method: str, address: str) -> str:
    """Why this profile change cannot be made, or "" when it can."""
    if method not in LINK_METHODS:
        return "方式只能是 auto（自动获取）或 manual（静态地址）"
    if method == "auto":
        return ""
    text = (address or "").strip()
    if "/" not in text:
        return "静态地址要写成 地址/前缀，例如 192.168.16.217/24"
    try:
        network = ipaddress.ip_interface(text)
    except ValueError:
        return "静态地址要写成 地址/前缀，例如 192.168.16.217/24"
    if network.network.prefixlen == 0:
        return "前缀 /0 不是一台设备能用的地址"
    return ""


def _profile_value(profile: str, key: str,
                   runner: Optional[Callable] = None) -> str:
    """One setting of one saved connection, or "" when it is unset."""
    result = net_wifi.nmcli(
        ["-t", "-g", key, "connection", "show", profile], runner)
    if not result.ok:
        return ""
    for line in result.stdout.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _interface_ipv4(ifname: str, runner: Optional[Callable] = None) -> str:
    """The first live IPv4 on one interface, or "".

    Scoped on purpose: the pre-change address still on the device after a
    successful `modify` is exactly what must not read as "it landed", and an
    address on a *sibling* device is no different. A profile with no declared
    interface yields nothing, which turns into the rollback the caller already
    performs for a change that cannot be seen to land.
    """
    if not ifname:
        return ""
    result = net_wifi.nmcli(
        ["-t", "-g", "IP4.ADDRESS", "device", "show", ifname], runner)
    if not result.ok:
        return ""
    for line in result.stdout.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _address_parts(text: str) -> set:
    """The address half of every IPv4 in a saved `ipv4.addresses` value.

    nmcli prints an address as `192.168.16.217/24` and, when a gateway was set,
    `192.168.16.217/24 192.168.16.1` — the comparison that decides "did the
    address change" is against the address, not the prefix or the gateway.
    """
    parts = set()
    for token in re.split(r"[,\s]+", (text or "").strip()):
        if token:
            parts.add(token.split("/")[0])
    return parts


def perform_link_change(profile: str, method: str, address: str = "",
                        gateway: str = "", *,
                        runner: Optional[Callable] = None,
                        state: Optional[SwitchState] = None,
                        budget: float = SWITCH_BUDGET_S,
                        poll: float = SWITCH_POLL_S,
                        sleep: Callable = time.sleep,
                        now: Callable = time.monotonic) -> dict:
    """Switch a saved connection between DHCP and a static address, with rollback.

    Same hazard as the Wi-Fi switch and for the same reason (design §6.4): if the
    operator is reaching the box over the profile being edited, the edit takes
    their own path away. So the shape is shared — read the old values first,
    apply, wait for the address to actually land, and restore on failure. Only
    the middle step differs.

    `nmcli connection up` is what makes the change take effect; `modify` alone
    changes the file and leaves the running device as it was, which is why the
    verify step can fail here even though every command returned 0.
    """
    problem = link_problem(method, address)
    if problem:
        return {"ok": False, "ssid": profile, "reason": problem, "address": ""}

    target = state or _state
    with target.lock:
        if target.phase == PHASE_SWITCHING:
            return {"ok": False, "ssid": profile,
                    "reason": "已经有一次网络改动在进行中", "address": ""}
        target.phase = PHASE_SWITCHING
        target.ssid = profile
        target.reason = ""
        target.address = ""
        target.since = time.time()

    # Same F2 guard as the Wi-Fi switch: a non-positive interval spins forever
    # on an injected clock or makes `time.sleep` raise out of a worker thread.
    if poll <= 0:
        return _finish_switch(target, False, "轮询间隔必须为正数")

    old_method = _profile_value(profile, "ipv4.method", runner) or "auto"
    old_addresses = _profile_value(profile, "ipv4.addresses", runner)
    ifname = _profile_value(profile, "connection.interface-name", runner)

    argv = ["connection", "modify", profile, "ipv4.method", method]
    if method == "manual":
        argv += ["ipv4.addresses", address.strip()]
        if gateway:
            argv += ["ipv4.gateway", gateway.strip()]
    result = net_wifi.nmcli(argv, runner)
    if not result.ok:
        detail = (result.stderr or result.stdout).strip()
        return _finish_switch(target, False,
                              detail or f"改不动「{profile}」")

    net_wifi.nmcli(["connection", "up", profile], runner)

    expected = address.strip().split("/")[0] if method == "manual" else ""
    landed = _expected_address(expected, ifname, old_addresses, runner,
                               budget, poll, sleep, now)
    if landed:
        return _finish_switch(target, True, "", landed)

    _restore_profile(profile, old_method, old_addresses, runner)
    reason = (f"「{profile}」改了之后没拿到地址，已恢复为 {old_method}"
              + (f" {old_addresses}" if old_addresses else ""))
    return _finish_switch(target, False, reason)


def _expected_address(expected: str, ifname: str, old_addresses: str,
                      runner, budget: float, poll: float,
                      sleep: Callable, now: Callable) -> str:
    """Wait for the address we asked for, or a new one when we asked for DHCP.

    DHCP has no expected value to compare against, so "did the interface get an
    address it did not hold before" is the question. The read is scoped to the
    interface the profile names — a sibling device holding the expected address
    must not read as this one landing it (the manual branch's false positive),
    and a device still holding its pre-change static address must not read as
    DHCP having landed one (the auto branch's false success).
    """
    old_parts = _address_parts(old_addresses)
    deadline = now() + budget
    while now() < deadline:
        value = _interface_ipv4(ifname, runner)
        if value:
            ip = value.split("/")[0]
            if expected:
                if ip == expected:
                    return value
            elif ip not in old_parts:
                return value
        sleep(poll)
    return ""


def _restore_profile(profile: str, method: str, addresses: str,
                     runner: Optional[Callable] = None) -> None:
    """Put the saved values back; the wired counterpart of the Wi-Fi rollback.

    Not the Wi-Fi `_roll_back` — that one writes `connection.autoconnect no`,
    the right teardown for a profile that should not come back at boot. Here the
    profile is the box's own and the *values* changed, so the teardown is the
    values: method and addresses, then bring it up again.
    """
    argv = ["connection", "modify", profile, "ipv4.method", method]
    if method == "manual" and addresses:
        argv += ["ipv4.addresses", addresses]
    elif method != "manual":
        # Restoring to auto must clear the address the failed attempt wrote.
        # NM ignores `ipv4.addresses` while the method is auto, so leaving it is
        # latent rather than live — but it resurfaces the moment the operator
        # goes back to manual, handing them an address nobody chose. Clear it
        # with the same clause the manual branch sets it with.
        argv += ["ipv4.addresses", ""]
    net_wifi.nmcli(argv, runner)
    net_wifi.nmcli(["connection", "up", profile], runner)
