#!/usr/bin/env python3
"""NetworkManager adapter for the W103D setup access point (design D-2/D-3/D-4).

Why this module exists
----------------------
A freshly flashed box may have no cable, no keyboard and no saved WiFi, which
makes it unreachable — while configuring it needs it to be reachable. The box
answers by opening its own hotspot. This module is the only code in the
repository that talks to ``nmcli`` about that (design D-4: the WiFi domain has
exactly one writer, and it is *not* the ``MRRC_*`` env file, so a WiFi
credential can never meet a config key).

Three rules, all load-bearing
-----------------------------
1. **Stdlib only, no application imports.** ``server.py`` imports this, and
   PyInstaller's root-module analysis therefore freezes it into *every*
   desktop build. Windows and macOS have no ``nmcli``, so the import has to
   succeed and every call has to degrade to a plain failure result rather than
   raise inside a request handler. For the same reason ``grp`` and
   ``os.chown`` (POSIX-only) are imported lazily inside the one function that
   uses them.
2. **A WiFi password never leaves this module's argv.** Not into a log line,
   not into the env file, not into either JSON mailbox (SDD
   ``support-bundle-privacy``, AD-021 / NFR-068). ``scrub()`` exists for the
   one place a secret could still ride along: nmcli's own error text.
3. **Everything that shells out takes an injectable ``runner``**, so the whole
   module is testable without NetworkManager, without Linux and without a
   radio.

Two nmcli facts that cost real debugging time
---------------------------------------------
* In ``nmcli device status`` an **AP-mode** wlan0 reports state ``connected``
  exactly like a station does, and ``connection show --active`` reports TYPE
  ``802-11-wireless`` for both. Our own hotspot is therefore *not* an uplink,
  and ``has_uplink()`` subtracts it — otherwise the supervisor raises the
  hotspot and tears it down again on the next tick, forever.
* NM prints the state as ``connected`` in some builds and ``activated`` in
  others, and as ``connected (site only)`` when there is link-local only (a
  cable into a dead switch) — which is *not* a usable uplink. Guessing one
  spelling is how a box ends up with no hotspot when it should have one, or
  with an open hotspot when it should not.
"""
from __future__ import annotations

import ipaddress
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

# ── constants ───────────────────────────────────────────────────────
DEFAULT_IFNAME = "wlan0"
DEFAULT_SSID = "MRRC-Setup"
DEFAULT_WEB_PORT = 8888
DEFAULT_STATE_DIR = Path("/run/mrrc/setup-ap")

STATE_NAME = "state.json"       # written by linux/setup_ap.py (root) only
WIZARD_NAME = "wizard.json"     # written by server.py (user mrrc) only

# The connection id `nmcli device wifi hotspot` gives the profile it creates.
# NM ids are not unique (uuids are), so every raise would leave one more twin
# behind; start_hotspot() removes an *inactive* profile of this name first.
HOTSPOT_PROFILE = "Hotspot"

# NetworkManager's `ipv4.method=shared` default subnet. The gate trusts the
# value read back from the running device and falls back to this one.
NM_SHARED_SUBNET = "10.42.0.0/24"
DEFAULT_GATEWAY = "10.42.0.1"

MODE_OFF = "off"
MODE_HOTSPOT = "hotspot"
MODE_STA = "sta"

WIZARD_SWITCHING = "switching"
WIZARD_OK = "ok"
WIZARD_FAILED = "failed"

# A supervisor that stopped heartbeating must not keep the passwordless window
# open (design D-6: the gate is a *live* network path, not a stored flag).
HEARTBEAT_MAX_AGE_S = 45.0
# A finished claim ("ok"/"failed") stays actionable long enough for the
# supervisor to react to it, then expires so normal evaluation resumes.
CLAIM_MAX_AGE_S = 600.0

NMCLI_TIMEOUT_S = 60.0
REDACTED = "<redacted>"
STATE_GROUP = "mrrc"

# "Has a network", in NM's two vocabularies.
CONNECTED_WORDS = ("connected", "activated")
# "Still trying" — i.e. a `has_uplink() == False` that is not yet a final answer.
TRANSITIONAL_STATES = (
    "prepare", "config", "need-auth", "need authentication", "ip-config",
    "ip-check", "secondaries", "connecting", "activating", "deactivating",
)
# Which interface types can carry the box's uplink.
UPLINK_TYPES = ("ethernet", "wifi")


# ── the nmcli transport ─────────────────────────────────────────────
@dataclass(frozen=True)
class NmResult:
    """One nmcli invocation's outcome. Never an exception."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def detail(self) -> str:
        """The most informative single-line reason, for logs and for the page."""
        for text in (self.stderr, self.stdout):
            cleaned = " ".join((text or "").split())
            if cleaned:
                return cleaned
        return f"exit {self.returncode}"


Runner = Callable[[list], NmResult]


def subprocess_runner(argv: list) -> NmResult:
    """The real runner. A missing nmcli or a timeout is a result, not a raise."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=NMCLI_TIMEOUT_S)
    except FileNotFoundError:
        return NmResult(127, "", "nmcli not found")
    except OSError as exc:
        return NmResult(126, "", f"could not run nmcli: {exc}")
    except subprocess.TimeoutExpired:
        return NmResult(124, "", f"nmcli timed out after {NMCLI_TIMEOUT_S:.0f}s")
    return NmResult(proc.returncode, proc.stdout or "", proc.stderr or "")


def nmcli(args: list, runner: Optional[Runner] = None) -> NmResult:
    """Run one nmcli command through the injectable runner."""
    return (runner or subprocess_runner)(["nmcli", *[str(a) for a in args]])


# ── `-t` output parsing ─────────────────────────────────────────────
def parse_row(line: str) -> list:
    """Split one ``nmcli -t`` row on *unescaped* colons.

    NM escapes ``:`` and ``\\`` with a backslash in terse mode, so an SSID
    containing a colon survives. A plain ``line.split(":")`` would shift every
    following column into the wrong field — a wrong SIGNAL is cosmetic, a wrong
    SECURITY column tells the wizard to ask for a password an open network does
    not have.
    """
    out: list = []
    buf: list = []
    escaped = False
    for char in (line or ""):
        if escaped:
            buf.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(char)
    out.append("".join(buf))
    return out


def parse_rows(text: Optional[str]) -> list:
    """Every non-blank row of a terse listing."""
    return [parse_row(line) for line in (text or "").splitlines() if line.strip()]


def _int_or(value, default: int = 0) -> int:
    """Coerce an untyped nmcli/env field to int; never raises."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


# ── what counts as "has a network" ──────────────────────────────────
def _is_usable_uplink(state: Optional[str]) -> bool:
    """Whether an interface state means real reachability (see module docstring).

    ``connected (site only)`` is link-local only — no gateway, no DNS — so a
    cable plugged into a dead switch must NOT be allowed to suppress the
    hotspot; that is exactly the situation the hotspot exists for.
    """
    text = (state or "").strip().lower()
    if "site only" in text:
        return False
    return any(text.startswith(word) for word in CONNECTED_WORDS)


def devices(runner: Optional[Runner] = None) -> list:
    """``nmcli device status`` as a list of dicts; empty when nmcli fails."""
    result = nmcli(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION",
                    "device", "status"], runner)
    if not result.ok:
        return []
    out = []
    for row in parse_rows(result.stdout):
        if len(row) < 3 or not row[0]:
            continue
        out.append({
            "device": row[0],
            "type": row[1].strip().lower(),
            "state": row[2],
            "connection": row[3] if len(row) > 3 else "",
        })
    return out


def any_device_connecting(runner: Optional[Runner] = None) -> bool:
    """Whether NM is still working, so "no uplink" is not yet a final answer."""
    return any((entry["state"] or "").strip().lower() in TRANSITIONAL_STATES
               for entry in devices(runner))


def _connection_is_ap(name: str, runner: Optional[Runner] = None) -> bool:
    """Whether one connection's ``802-11-wireless.mode`` is ``ap``."""
    if not name:
        return False
    result = nmcli(["-t", "-g", "802-11-wireless.mode", "connection", "show", name],
                   runner)
    return result.ok and result.stdout.strip().lower() == "ap"


def active_ap_connections(runner: Optional[Runner] = None) -> list:
    """Names of the active connections that are access points.

    ``connection show --active`` reports TYPE ``802-11-wireless`` for a station
    and for an AP alike, so the mode has to be asked for separately. Getting
    this wrong is what makes the box count its own hotspot as an uplink — which
    is why this lives with the uplink decision rather than with the AP lifecycle.
    """
    result = nmcli(["-t", "-f", "NAME,TYPE", "connection", "show", "--active"],
                   runner)
    if not result.ok:
        return []
    names = [row[0] for row in parse_rows(result.stdout)
             if len(row) >= 2 and row[1].strip() == "802-11-wireless" and row[0]]
    return [name for name in names if _connection_is_ap(name, runner)]


def has_uplink(runner: Optional[Runner] = None) -> bool:
    """Whether the box can already be reached (design D-6's criterion).

    Our own hotspot is subtracted: it reports ``connected`` too.
    """
    ap_names = set(active_ap_connections(runner))
    for entry in devices(runner):
        if entry["type"] not in UPLINK_TYPES:
            continue
        if not _is_usable_uplink(entry["state"]):
            continue
        if entry["connection"] and entry["connection"] in ap_names:
            continue
        return True
    return False


def nm_running(runner: Optional[Runner] = None) -> bool:
    """Whether the NetworkManager daemon answers at all."""
    result = nmcli(["-t", "-f", "RUNNING", "general"], runner)
    return result.ok and result.stdout.strip().lower() == "running"


def wifi_interface(runner: Optional[Runner] = None) -> str:
    """The WiFi device name, by type rather than by an assumed ``wlan0``."""
    for entry in devices(runner):
        if entry["type"] == "wifi" and entry["device"]:
            return entry["device"]
    return ""


# ── AP lifecycle ────────────────────────────────────────────────────
def _active_uuids(runner: Optional[Runner] = None) -> set:
    """UUIDs of the connections that are up right now."""
    result = nmcli(["-t", "-f", "UUID", "connection", "show", "--active"], runner)
    if not result.ok:
        return set()
    return {row[0] for row in parse_rows(result.stdout) if row and row[0]}


def _profiles_named(name: str, runner: Optional[Runner] = None) -> list:
    """``[(uuid, name), …]`` for every profile carrying this id (ids repeat)."""
    result = nmcli(["-t", "-f", "UUID,NAME", "connection", "show"], runner)
    if not result.ok:
        return []
    return [(row[0], row[1]) for row in parse_rows(result.stdout)
            if len(row) >= 2 and row[1] == name]


def hotspot_active(runner: Optional[Runner] = None) -> bool:
    """Whether an AP is up on this box right now."""
    return bool(active_ap_connections(runner))


def start_hotspot(ssid: Optional[str] = None, ifname: Optional[str] = None,
                  runner: Optional[Runner] = None) -> NmResult:
    """Raise an **open** hotspot (design D-2: ``nmcli device wifi hotspot``).

    Idempotent in the two ways that matter:
    * an AP that is already up is left alone — tearing it down and rebuilding
      it would drop the operator in the middle of the wizard;
    * a *stale* profile of the same id is deleted first, because NM ids are not
      unique and every raise would otherwise leave one more twin behind. A
      profile that is currently active is never deleted.

    No ``password`` argument is passed. That omission *is* the open network:
    there is no way to hand the operator a key they have not been told.
    """
    if hotspot_active(runner):
        return NmResult(0, "already up", "")
    live = _active_uuids(runner)
    for uuid, _name in _profiles_named(HOTSPOT_PROFILE, runner):
        if uuid in live:
            continue
        nmcli(["connection", "delete", uuid], runner)
    return nmcli(["device", "wifi", "hotspot",
                  "ifname", ifname or DEFAULT_IFNAME,
                  "ssid", ssid or DEFAULT_SSID], runner)


def stop_hotspot(runner: Optional[Runner] = None) -> NmResult:
    """Take the AP down. Idempotent: nothing up is success, not an error.

    Only AP-mode connections are considered, so this can never drop the
    operator's real WiFi.
    """
    names = active_ap_connections(runner)
    if not names:
        return NmResult(0, "not up", "")
    last = NmResult(0, "not up", "")
    for name in names:
        last = nmcli(["connection", "down", name], runner)
    return last


# ── station side ────────────────────────────────────────────────────
def scan_wifi(runner: Optional[Runner] = None, rescan: bool = True) -> list:
    """Visible networks, strongest first, one entry per SSID.

    Hidden networks arrive with an empty SSID and are dropped. ``rescan`` is on
    by default because the AP has just taken the radio over: NM's cache is
    whatever it saw before, and an empty list reads as "the radio is broken".
    """
    args = ["-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"]
    if rescan:
        args += ["--rescan", "yes"]
    result = nmcli(args, runner)
    if not result.ok:
        return []
    best: dict = {}
    for row in parse_rows(result.stdout):
        if len(row) < 3 or not row[0]:
            continue
        entry = {
            "ssid": row[0],
            "signal": _int_or(row[1]),
            "security": row[2].strip(),
            "protected": bool(row[2].strip()),
        }
        previous = best.get(entry["ssid"])
        if previous is None or entry["signal"] > previous["signal"]:
            best[entry["ssid"]] = entry
    return sorted(best.values(), key=lambda item: -item["signal"])


def connect_wifi(ssid: str, password: str = "",
                 runner: Optional[Runner] = None,
                 ifname: Optional[str] = None) -> NmResult:
    """Join an infrastructure network.

    Single radio (design D-3): this *replaces* the hotspot, it does not run
    beside it. The caller owns the ordering — see
    ``server._perform_wifi_switch``.

    ``password`` travels in argv and nowhere else. It is never logged, never
    written to the env file (WiFi is NetworkManager's domain, design D-4) and
    never written to either JSON mailbox.
    """
    args = ["device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    args += ["ifname", ifname or DEFAULT_IFNAME]
    return nmcli(args, runner)


def ipv4_address(ifname: Optional[str] = None,
                 runner: Optional[Runner] = None) -> str:
    """The interface's IPv4 address with its prefix, or ``""`` (10.42.0.1/24)."""
    result = nmcli(["-t", "-g", "IP4.ADDRESS", "device", "show",
                    ifname or DEFAULT_IFNAME], runner)
    if not result.ok:
        return ""
    for line in result.stdout.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned
    return ""


def ap_network(address: Optional[str]) -> str:
    """``10.42.0.1/24`` → ``10.42.0.0/24``: the subnet the gate trusts."""
    try:
        return str(ipaddress.ip_network((address or "").strip(), strict=False))
    except ValueError:
        return NM_SHARED_SUBNET


def setup_url(gateway: str, port: Optional[int] = None) -> str:
    """The address to print on the HDMI console and to show on the page."""
    host = (gateway or "").strip().split("/")[0] or DEFAULT_GATEWAY
    return f"https://{host}:{port or DEFAULT_WEB_PORT}/setup"


def scrub(text: Optional[str], *secrets: str) -> str:
    """Remove every literal secret from a string that is about to be stored.

    nmcli does not echo a PSK back today, but its error text is the one place a
    credential could ride along into ``wizard.json`` and from there into a
    support bundle — so the removal is unconditional rather than trusted
    (SDD ``support-bundle-privacy``).
    """
    out = text or ""
    for secret in secrets:
        if secret:
            out = out.replace(secret, REDACTED)
    return out


# ── configuration both owners must agree on ─────────────────────────
def ap_settings(env: Optional[Mapping[str, Any]] = None) -> dict:
    """The WiFi-domain settings, read in exactly one place.

    ``Any`` rather than ``str`` for the values because both this function and the
    supervisor's only ever *coerce* what they read (``or ""``, ``_int_or``) — an
    absent or None entry is a normal input, not a type error.

    The supervisor and the server both need the SSID and the state directory,
    and a drift between them is silent: the server would read a state file
    nobody writes, so the gate would stay shut and the box would look like it
    never opened a hotspot.
    """
    source = os.environ if env is None else env
    port = _int_or((source.get("MRRC_SETUP_AP_WEB_PORT") or "").strip()
                   or (source.get("MRRC_WEB_PORT") or "").strip(),
                   DEFAULT_WEB_PORT)
    return {
        "ssid": (source.get("MRRC_SETUP_AP_SSID") or "").strip() or DEFAULT_SSID,
        "ifname": (source.get("MRRC_SETUP_AP_IFNAME") or "").strip() or DEFAULT_IFNAME,
        "web_port": port,
        "state_dir": Path((source.get("MRRC_SETUP_AP_STATE_DIR") or "").strip()
                          or str(DEFAULT_STATE_DIR)),
    }


# ── the shared directory and its two mailboxes ──────────────────────
def _resolve_dir(state_dir) -> Path:
    return Path(state_dir) if state_dir is not None else DEFAULT_STATE_DIR


def ensure_state_dir(state_dir=None) -> None:
    """Create the directory so BOTH owners can write their own file.

    The supervisor runs as root and the server as ``mrrc``, so the directory is
    0775 with group ``mrrc``: root writes ``state.json``, ``mrrc`` writes
    ``wizard.json``, and each is the only writer of its own file — which is why
    there is no lock here.

    Every failure is swallowed. A desktop install has no ``/run/mrrc`` and no
    ``mrrc`` group, and the correct behaviour there is a permanently closed
    gate, not a server that will not start.
    """
    directory = _resolve_dir(state_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        os.chmod(directory, 0o775)
    except OSError:
        pass
    try:
        import grp          # POSIX-only. A deferred import is load-bearing:
        os.chown(directory, -1, grp.getgrnam(STATE_GROUP).gr_gid)
    except (ImportError, KeyError, OSError, AttributeError):
        pass


def _write_json(path: Path, payload: dict) -> None:
    """Atomic, world-readable JSON. Never raises.

    World-readable on purpose and safe: neither file carries a secret, and that
    is precisely what lets a root daemon and an ``mrrc`` server share one
    directory. The chmod happens *before* the rename because ``write_text``
    creates the temp file under the process umask — root's umask would leave a
    0600 file the server cannot read, i.e. a gate that is permanently closed
    and reports nothing.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        try:
            os.chmod(tmp, 0o644)
        except OSError:
            pass
        tmp.replace(path)
    except OSError:
        pass


def _read_json(path: Path):
    """The parsed object at ``path``, or ``None`` for anything unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pick(raw: dict, key: str, default, cast):
    """One typed field of a JSON object; anything unparseable falls back.

    A newer writer must not break an older reader (or the reverse), and a
    truncated file must read as "gate closed" rather than raise inside a
    request handler.
    """
    value = raw.get(key, default)
    if value is None:
        return default
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ApState:
    """What the supervisor last published (``state.json``).

    ``mode`` is the gate's first half: only ``MODE_HOTSPOT`` can open the
    passwordless window, so a supervisor that died, a box with a cable in it and
    a box that finished onboarding all close it without anyone having to
    remember to (design D-6: the window disappears *with the network*, so there
    is no "is it configured yet" flag that could fail to be written).
    """

    mode: str = MODE_OFF
    ssid: str = ""
    gateway: str = ""
    network: str = ""
    url: str = ""
    reason: str = ""
    since: float = 0.0
    heartbeat: float = 0.0
    deadline: float = 0.0

    def to_json(self) -> dict:
        return {
            "mode": self.mode, "ssid": self.ssid, "gateway": self.gateway,
            "network": self.network, "url": self.url, "reason": self.reason,
            "since": self.since, "heartbeat": self.heartbeat,
            "deadline": self.deadline,
        }

    @classmethod
    def from_json(cls, raw) -> "ApState":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            mode=_pick(raw, "mode", MODE_OFF, str),
            ssid=_pick(raw, "ssid", "", str),
            gateway=_pick(raw, "gateway", "", str),
            network=_pick(raw, "network", "", str),
            url=_pick(raw, "url", "", str),
            reason=_pick(raw, "reason", "", str),
            since=_pick(raw, "since", 0.0, float),
            heartbeat=_pick(raw, "heartbeat", 0.0, float),
            deadline=_pick(raw, "deadline", 0.0, float),
        )


def read_state(state_dir=None) -> ApState:
    """The supervisor's last word; ``ApState()`` (mode off) when there is none."""
    return ApState.from_json(_read_json(_resolve_dir(state_dir) / STATE_NAME))


def write_state(state: ApState, state_dir=None) -> None:
    """Publish one heartbeat's worth of truth. Called by the supervisor only."""
    _write_json(_resolve_dir(state_dir) / STATE_NAME, state.to_json())


def state_is_live(state: ApState, now: float,
                  max_age: float = HEARTBEAT_MAX_AGE_S) -> bool:
    """Whether the supervisor is demonstrably running the hotspot *now*.

    A stale file means a dead supervisor: trusting it would leave the
    passwordless window open on a box whose hotspot is long gone, which is the
    one failure mode D-6's "no stored flag" rule exists to prevent.
    """
    if state.mode != MODE_HOTSPOT:
        return False
    if state.heartbeat <= 0:
        return False
    return (now - state.heartbeat) <= max_age


@dataclass(frozen=True)
class WizardClaim:
    """The server's mailbox entry (``wizard.json``).

    There is deliberately **no password field**: the WiFi PSK has nowhere to be
    written, which makes "never on disk" a property of the schema rather than a
    promise every future caller has to remember to keep.
    """

    action: str = ""        # "connect" — the only action today
    ssid: str = ""
    state: str = ""         # WIZARD_SWITCHING | WIZARD_OK | WIZARD_FAILED
    error: str = ""
    address: str = ""
    nonce: str = ""
    heartbeat: float = 0.0

    def to_json(self) -> dict:
        return {
            "action": self.action, "ssid": self.ssid, "state": self.state,
            "error": self.error, "address": self.address,
            "nonce": self.nonce, "heartbeat": self.heartbeat,
        }

    @classmethod
    def from_json(cls, raw) -> "WizardClaim":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            action=_pick(raw, "action", "", str),
            ssid=_pick(raw, "ssid", "", str),
            state=_pick(raw, "state", "", str),
            error=_pick(raw, "error", "", str),
            address=_pick(raw, "address", "", str),
            nonce=_pick(raw, "nonce", "", str),
            heartbeat=_pick(raw, "heartbeat", 0.0, float),
        )


def read_wizard(state_dir=None) -> WizardClaim:
    """The server's current claim on the radio; empty when there is none."""
    return WizardClaim.from_json(_read_json(_resolve_dir(state_dir) / WIZARD_NAME))


def write_wizard(claim: WizardClaim, state_dir=None) -> None:
    """Claim or report. Called by the server only."""
    _write_json(_resolve_dir(state_dir) / WIZARD_NAME, claim.to_json())


def claim_is_fresh(claim: WizardClaim, now: float) -> bool:
    """Whether the supervisor should still stand down for this claim.

    Two budgets, because the two situations differ. A switch *in flight*
    heartbeats every few seconds, so 45 s means the server died mid-switch and
    the supervisor must take the radio back — otherwise the box is left with
    neither an AP nor an uplink and nobody to fix it. A *finished* claim needs
    no heartbeat and stays actionable for ten minutes, which is how the
    supervisor learns that a join failed and reopens the window.
    """
    if not claim.action or claim.heartbeat <= 0:
        return False
    age = now - claim.heartbeat
    if claim.state == WIZARD_SWITCHING:
        return age <= HEARTBEAT_MAX_AGE_S
    return age <= CLAIM_MAX_AGE_S


# ── the passwordless gate ───────────────────────────────────────────
def client_ip(host: Optional[str]):
    """A request's client host as an address object, or ``None``.

    The IPv4-mapped unwrap is the load-bearing line. uvicorn on a dual-stack
    socket reports an IPv4 client as ``::ffff:10.42.0.57``, and that compared
    against a v4 network is ``False`` — so on a box bound to ``::`` the gate
    would never open, with nothing logging why, because every check
    "correctly" returned False.
    """
    try:
        addr = ipaddress.ip_address((host or "").strip())
    except ValueError:
        return None
    return getattr(addr, "ipv4_mapped", None) or addr


def gate_is_open(state: ApState, client_host: Optional[str], now: float,
                 max_age: float = HEARTBEAT_MAX_AGE_S) -> bool:
    """Design D-6's rule — the only passwordless door in the product.

    Both halves must hold:

    * the hotspot must be demonstrably up **now** (a live heartbeat, not a
      stored flag), and
    * the request must arrive from **that hotspot's own subnet**.

    The subnet half alone is a collision waiting to happen — 10.42.0.0/24 is a
    perfectly ordinary LAN range. The heartbeat half alone would let any client
    on the box's real network in. Together they give D-6's property: when the
    hotspot goes away the subnet goes away with it, so the branch cannot be
    left open by a flag that failed to be cleared.
    """
    if not state_is_live(state, now, max_age):
        return False
    addr = client_ip(client_host)
    if addr is None:
        return False
    try:
        network = ipaddress.ip_network(state.network or NM_SHARED_SUBNET,
                                       strict=False)
    except ValueError:
        return False
    return addr in network
