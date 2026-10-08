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
from typing import Callable, Optional

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
