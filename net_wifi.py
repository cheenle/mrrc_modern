#!/usr/bin/env python3
"""NetworkManager adapter: the station half.

Why this module exists
----------------------
The box has one radio, and the page that configures it needs three things from
NetworkManager: what networks are visible, join one, and read back the address
that resulted. This module is the only code in the repository that talks to
``nmcli`` (design D-4: the WiFi domain has exactly one writer, and it is *not*
the ``MRRC_*`` env file, so a WiFi credential can never meet a config key).

It used to be twice this size. The access-point half — raising an open hotspot
for onboarding — was retired on 2026-10-11 after failing its own hardware test:
this board's MT7663S brings the hotspot up, reports it correctly through ``iw``,
and never beacons it (spec §1). Onboarding is a LAN page now, so the AP
lifecycle, the passwordless gate it published and the two JSON mailboxes that
carried the wizard's requests went with it. ``box_ops.perform_wifi_switch`` owns
the ordering and the rollback that used to live in a root supervisor.

Three rules, all load-bearing
-----------------------------
1. **Stdlib only, no application imports.** ``box_ops`` imports this, and
   PyInstaller's root-module analysis therefore freezes it into *every* desktop
   build. Windows and macOS have no ``nmcli``, so the import has to succeed and
   every call has to degrade to a plain failure result rather than raise inside
   a request handler.
2. **A WiFi password never leaves this module's argv.** Not into a log line,
   not into the env file, not into a status dict (SDD ``support-bundle-privacy``,
   AD-021 / NFR-068). ``scrub()`` exists for the one place a secret could still
   ride along: nmcli's own error text.
3. **Everything that shells out takes an injectable ``runner``**, so the whole
   module is testable without NetworkManager, without Linux and without a
   radio.

Two nmcli facts that cost real debugging time
---------------------------------------------
* NM prints a state as ``connected`` in some builds and ``activated`` in others,
  and as ``connected (site only)`` when there is link-local only (a cable into a
  dead switch). Anything parsing device state has to accept all of them; the
  retired supervisor learned this the expensive way.
* ``nmcli -t`` escapes a colon inside a value as ``\\:``. SSIDs contain colons.
  ``parse_row`` is the only correct way to read a terse row back.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

# ── constants ───────────────────────────────────────────────────────
DEFAULT_IFNAME = "wlan0"
NMCLI_TIMEOUT_S = 60.0
REDACTED = "<redacted>"


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
    SECURITY column makes an open network look protected, and the page then asks
    for a password the network does not have.
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


# ── the device listing ──────────────────────────────────────────────
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


# ── station side ────────────────────────────────────────────────────
def scan_wifi(runner: Optional[Runner] = None, rescan: bool = True) -> list:
    """Visible networks, strongest first, one entry per SSID.

    Hidden networks arrive with an empty SSID and are dropped. ``rescan`` is on
    by default because NM's cache predates whatever the operator is looking at —
    empty after a boot, stale after a switch — and an empty list reads as "the
    radio is broken".
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

    Single radio: this replaces whatever the interface was doing, it does not run
    beside it. The caller owns the ordering — see ``box_ops.perform_wifi_switch``,
    which is also where rollback lives.

    ``password`` travels in argv and nowhere else. It is never logged, never
    written to the env file (WiFi is NetworkManager's domain, design D-4) and is
    removed from any error text that goes back to a page, by ``scrub()``.
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


def scrub(text: Optional[str], *secrets: str) -> str:
    """Remove every literal secret from a string that is about to be stored.

    nmcli does not echo a PSK back today, but its error text is the one place a
    credential could ride along into a support bundle — so the removal is
    unconditional rather than trusted (SDD ``support-bundle-privacy``).
    """
    out = text or ""
    for secret in secrets:
        if secret:
            out = out.replace(secret, REDACTED)
    return out
