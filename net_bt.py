"""Bluetooth pairing for the box's remote — the only code that talks to bluez.

The settings page needs three things an operator can do without a shell: see
what is in range, pair a remote, and know whether it actually *works*.  That
last one is not the same as "paired": this project spent an evening on a remote
that reported ``Paired: yes`` while the link stayed unencrypted, so bluez built
no input device and every key was dead (see ``docs/W103D_REMOTE_GUIDE.md``).

So the verdict this module returns is the one that matters to the operator —
**a keyboard input device from that address exists** — with the pairing flags
alongside it for diagnosis.

Every call takes an injectable ``runner`` (the same shape ``net_wifi`` uses),
so the whole module is testable without bluez.
"""

from __future__ import annotations

import re
import subprocess
from typing import Callable, Optional

Runner = Callable[[list], tuple]

#: How long a scan listens.  A remote in pairing mode advertises continuously,
#: so this only needs to outlast the inquiry cycle (~10 s).
SCAN_SECONDS = 12

#: ``bluetoothctl info`` lines we surface, lower-cased keys.
_INFO_FIELDS = {
    "name": "Name",
    "alias": "Alias",
    "paired": "Paired",
    "bonded": "Bonded",
    "connected": "Connected",
    "trusted": "Trusted",
}


def _default_runner(argv: list) -> tuple:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return done.returncode, done.stdout or "", done.stderr or ""


def _run(argv: list, runner: Optional[Runner] = None) -> tuple:
    out = (runner or _default_runner)(argv)
    if len(out) == 2:                       # (rc, stdout) from a simple stub
        return out[0], out[1], ""
    return out[0], out[1], out[2]


def _clean(text: str) -> str:
    """bluetoothctl colours its output; a terminal is not always there."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text or "")


# ── scanning ────────────────────────────────────────────────────────────────
def parse_devices(text: str) -> list[dict]:
    """``[NEW] Device AA:BB:CC:DD:EE:FF Name`` lines → [{address, name}]."""
    seen: dict[str, dict] = {}
    for line in _clean(text).splitlines():
        match = re.search(r"Device\s+([0-9A-F]{2}(?::[0-9A-F]{2}){5})\s*(.*)$", line)
        if not match:
            continue
        address = match.group(1).upper()
        name = match.group(2).strip()
        # bluez prints the address with dashes when it has no name
        if not name or name.replace("-", ":") == address:
            name = ""
        entry = seen.setdefault(address, {"address": address, "name": ""})
        if name and not entry["name"]:
            entry["name"] = name
    return sorted(seen.values(), key=lambda d: (not d["name"], d["name"] or d["address"]))


def scan(seconds: int = SCAN_SECONDS, runner: Optional[Runner] = None) -> list[dict]:
    """Ask bluez for nearby BR/EDR devices (remotes are classic, not LE)."""
    rc, out, err = _run(["bluetoothctl", "--timeout", str(seconds), "scan", "bredr"], runner)
    if rc != 0 and not out:
        return []
    return parse_devices(out)


# ── pairing ─────────────────────────────────────────────────────────────────
def parse_info(text: str) -> dict:
    info: dict = {}
    for raw in _clean(text).splitlines():
        match = re.match(r"\s*([A-Za-z][A-Za-z ]*):\s*(.+?)\s*$", raw)
        if not match:
            continue
        key, value = match.group(1).strip().lower(), match.group(2).strip()
        for field, label in _INFO_FIELDS.items():
            if key == label.lower():
                info[field] = value if field in ("name", "alias") else value.lower() == "yes"
    return info


def info(address: str, runner: Optional[Runner] = None) -> dict:
    _rc, out, _err = _run(["bluetoothctl", "info", address], runner)
    return parse_info(out)


def pair(address: str, seconds: int = 25, runner: Optional[Runner] = None) -> dict:
    """Pair + trust.  Returns the resulting ``info()`` (empty on failure)."""
    _run(["bluetoothctl", "--timeout", str(seconds), "pair", address], runner)
    _run(["bluetoothctl", "trust", address], runner)
    return info(address, runner)


def remove(address: str, runner: Optional[Runner] = None) -> None:
    """Drop a stale bonding — the documented cure for an unencrypted link."""
    _run(["bluetoothctl", "remove", address], runner)


def power_cycle(runner: Optional[Runner] = None) -> None:
    """Bring the controller down and up: clears bluez's cached link keys."""
    _run(["bluetoothctl", "power", "off"], runner)
    _run(["bluetoothctl", "power", "on"], runner)


# ── the verdict that matters ────────────────────────────────────────────────
def keyboard_device_present(address: str,
                            proc_path: str = "/proc/bus/input/devices") -> bool:
    """Is there a KEYBOARD input device whose hardware address is ``address``?

    bluez builds one when the remote's HID service was readable — which is
    exactly what fails when the link is unencrypted.  Checking the outcome
    instead of the flags is what turns "paired" into "the keys work".
    """
    want = address.replace(":", "").lower()
    try:
        with open(proc_path, "r", encoding="utf-8", errors="replace") as fh:
            blocks = fh.read().split("\n\n")
    except OSError:
        return False
    for block in blocks:
        low = block.lower()
        if want not in low.replace(":", ""):
            continue
        if "kbd" in low or "keyboard" in low:
            return True
    return False


def state(address: str, runner: Optional[Runner] = None) -> dict:
    """What the settings page shows for one address."""
    flags = info(address, runner)
    return {
        "address": address,
        "name": flags.get("name") or flags.get("alias") or "",
        "paired": bool(flags.get("paired")),
        "bonded": bool(flags.get("bonded")),
        "connected": bool(flags.get("connected")),
        "trusted": bool(flags.get("trusted")),
        "keys_work": keyboard_device_present(address),
    }
