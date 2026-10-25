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

#: How long `connection up` may block. A network that is in range activates in a
#: couple of seconds; an SSID that is *not* in range makes nmcli sit on its own
#: 90 s default, and `box_ops.perform_wifi_switch` has a 30 s DHCP budget behind
#: this call — so this bound is what keeps a typo'd SSID from holding the page for
#: minutes.
NM_CONNECT_WAIT_S = 25

#: 2.4 GHz only, deliberately. This board's MT7663S associates on 5 GHz and then
#: fails to move data, and ~100 s later its firmware stops answering scans
#: altogether (`w103d-box` §陷阱, measured twice). The LAN page has always said
#: "only 2.4 GHz"; pinning the band here is what makes that statement true
#: instead of a hope.
WIFI_BAND = "bg"

#: Fallback resolvers every saved profile carries, in this order.
#:
#: Not a preference — a repair. Measured 2026-10-25 on a shipped box (BG8GGG): the
#: Wi-Fi came up before DHCP finished, the resolver was empty, `chronyc` could not
#: resolve its NTP pool name, the clock stayed **24 days behind** the image date, and
#: every outbound HTTPS call then failed with `certificate is not yet valid`. The
#: page showed "cannot reach the portal" and the operator went looking for a network
#: fault. A resolver that answers even when the router hands out none removes that
#: whole class of silent failure.
#:
#: 114.114.114.114 is reachable from mainland consumer lines; 8.8.8.8 is the one that
#: works when a local resolver is broken or poisoned. Both are appended to whatever
#: the profile already has, so an operator's own DNS is never removed.
FALLBACK_DNS = ("114.114.114.114", "8.8.8.8")


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

#: How nmcli words "the access point is not here", as opposed to "the credential
#: is wrong". A switch that fails this way is not a bad configuration — it is a
#: network nobody has reached yet, which is exactly the case of a box being
#: prepared for someone else: their AP only exists at *their* home.
#: Lower-cased, because `access_point_absent` lower-cases the text it is given —
#: "未发现 SSID" would otherwise stop matching its own string.
ABSENT_MARKERS = (
    "未发现 ssid",           # `device wifi connect` with nothing in the scan list
    "no network with ssid",   # the same, in an English build
    "超时",                   # `-w` expiry: nothing answered during the whole wait
    "timeout",
)


def access_point_absent(text: Optional[str]) -> bool:
    """Whether an nmcli failure means "not in range" rather than "wrong key".

    Read by `box_ops._roll_back`: a failed switch normally disables the profile it
    just wrote (`autoconnect no`, so a reboot does not keep retrying a network that
    failed), but doing that to a network that simply was not around yet throws
    away the one thing the operator was trying to set up.
    """
    lowered = (text or "").lower()
    return any(marker in lowered for marker in ABSENT_MARKERS)


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
    """Join an infrastructure network — including one that is not in range yet.

    Single radio: this replaces whatever the interface was doing, it does not run
    beside it. The caller owns the ordering — see ``box_ops.perform_wifi_switch``,
    which is also where rollback lives.

    This used to be ``nmcli device wifi connect <SSID>``, which is a *scan-based*
    shortcut: nmcli looks the SSID up in the scan list and refuses with "no
    network with SSID …" when it is not there, writing no profile at all. Two
    things the box is used for break on that (2026-10-17, found while preparing a
    box for a customer): pre-configuring the Wi-Fi of someone whose AP only
    exists at *their* home — the one case where the AP is never visible on the
    workbench — and joining a hidden AP, which is never in a scan list even when
    it is the local network. ``linux/wifi_preseed.py`` has the same shape: its
    whole purpose is to join a network before anyone can see it.

    Writing the profile and bringing it up needs no scan. ``hidden yes`` costs
    nothing when the network broadcasts — measured on a W103D: a hidden=yes
    profile still joins a visible AP within seconds — and it is what makes a
    non-broadcasting AP (or one that only appears later, at the customer's site)
    connectable. ``band bg`` keeps the radio off 5 GHz, where this chip wedges.

    The profile is named after the SSID because that is what the callers already
    assume: ``box_ops._roll_back`` turns ``autoconnect`` off by that name, and
    ``linux/wifi_preseed.py`` sets it by that name. A pre-existing profile with
    that name is deleted first, so a retry cannot inherit half-written fields.

    ``password`` travels in argv and nowhere else. It is never logged, never
    written to the env file (WiFi is NetworkManager's domain, design D-4) and is
    removed from any error text that goes back to a page, by ``scrub()``.
    """
    written = write_profile(ssid, password, runner=runner, ifname=ifname)
    if not written.ok:
        return written
    return nmcli(["-w", str(NM_CONNECT_WAIT_S), "connection", "up", ssid], runner)


def write_profile(ssid: str, password: str = "",
                  runner: Optional[Runner] = None,
                  ifname: Optional[str] = None) -> NmResult:
    """Write (or replace) the saved profile for `ssid` — without activating it.

    Split out of `connect_wifi` so the box can *pre-configure* a network instead
    of switching to it: the settings page lets an operator save the Wi-Fi of a
    customer whose AP only exists at their home, and that must not depend on
    anything being in range right now (2026-10-17).
    """
    target_if = ifname or DEFAULT_IFNAME
    # A profile of this name may already exist (an edit, or a retry). Deleting
    # first makes the write idempotent; "there was nothing to delete" is the
    # normal case and is not an error.
    nmcli(["connection", "delete", ssid], runner)
    created = nmcli(["connection", "add", "type", "wifi", "con-name", ssid,
                     "ifname", target_if, "ssid", ssid], runner)
    if not created.ok:
        return created
    args = ["connection", "modify", ssid,
            "802-11-wireless.hidden", "yes",
            "802-11-wireless.band", WIFI_BAND,
            # The profile carries the fallback resolvers from birth, so a network that
            # wakes up before DHCP can still resolve names (see FALLBACK_DNS).
            "ipv4.dns", " ".join(FALLBACK_DNS),
            # Stated explicitly rather than left to the default: the retired
            # hotspot's profile defaulted to yes *by accident*, and "by default"
            # is not a design.
            "connection.autoconnect", "yes",
            # 0 is nmcli's "unlimited": a network that is not here yet must keep
            # being retried, not give up after four scans (its default).
            "connection.autoconnect-retries", "0"]
    if password:
        args += ["802-11-wireless-security.key-mgmt", "wpa-psk",
                 "802-11-wireless-security.psk", password]
    return nmcli(args, runner)


def forget(ssid: str, runner: Optional[Runner] = None) -> NmResult:
    """Delete a saved profile. Nothing else about the box changes."""
    return nmcli(["connection", "delete", ssid], runner)


def _profile_dns(name: str, runner: Optional[Runner] = None) -> list:
    """The ``ipv4.dns`` a saved profile carries, however nmcli spells the list."""
    result = nmcli(["-g", "ipv4.dns", "connection", "show", name], runner)
    if not result.ok:
        return []
    text = (result.stdout or "").replace("|", " ").replace(",", " ")
    return [token for token in text.split() if token]


#: Which saved profiles a resolver belongs on. ``loopback``/``bridge``/... are
#: left alone: adding a public resolver to them changes nothing real and is noise
#: in the operator's own `nmcli connection show`.
_DNS_PROFILE_TYPES = ("802-3-ethernet", "802-11-wireless", "wifi", "ethernet",
                      "gsm", "cdma", "wwan")


def ensure_fallback_dns(runner: Optional[Runner] = None) -> list:
    """Put the fallback resolvers on every saved profile that lacks them.

    Returns the profile names it changed, so the caller can log a sentence instead
    of the box silently reconfiguring itself. Idempotent: a second run finds
    nothing missing and touches nothing — which is what makes it safe to call at
    every start-up (the upgrade path for boxes flashed before FALLBACK_DNS
    existed).

    Existing servers are kept, in their order, and the fallback is appended: an
    operator who typed their own DNS must not have it silently replaced. The
    DHCP-provided server is a separate list (``ignore-auto-dns`` stays no), so it
    keeps working too; NetworkManager lists the profile's own servers first.
    """
    result = nmcli(["-t", "-f", "NAME,TYPE", "connection", "show"], runner)
    if not result.ok:
        return []
    changed = []
    for row in parse_rows(result.stdout):
        if len(row) < 2 or row[1] not in _DNS_PROFILE_TYPES:
            continue
        name = row[0]
        have = _profile_dns(name, runner)
        missing = [server for server in FALLBACK_DNS if server not in have]
        if not missing:
            continue
        wanted = " ".join(dict.fromkeys(have + missing))
        if nmcli(["connection", "modify", name, "ipv4.dns", wanted], runner).ok:
            changed.append(name)
    return changed


def set_autoconnect(ssid: str, enabled: bool,
                    runner: Optional[Runner] = None) -> NmResult:
    """Arm or disarm a saved profile for the next scan (and the next boot)."""
    return nmcli(["connection", "modify", ssid,
                  "connection.autoconnect", "yes" if enabled else "no"], runner)


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
