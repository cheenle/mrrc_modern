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

#: ``btmgmt find -l`` prints one line per sighting:
#:   hci0 dev_found: 0C:F3:CE:B0:88:75 type BR/EDR rssi -54 flags 0x0000
#: followed by indented/plain detail lines, of which ``name X`` is the one we want.
_BTMGMT_DEV = re.compile(
    r"dev_found:\s+([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\s+type\s+(BR/EDR|LE\s+\w+)\s+rssi\s+(-?\d+)")
_BTMGMT_NAME = re.compile(r"^name\s+(.+?)\s*$")

#: ``bluetoothctl info`` lines we surface, lower-cased keys.
_INFO_FIELDS = {
    "name": "Name",
    "alias": "Alias",
    "icon": "Icon",
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
    """bluetoothctl colours its output; a terminal is not always there.

    It also wraps each coloured token in bold/reset bytes — the real line is
    ``[\x01\x1b[0;93m\x02CHG\x01\x1b[0m\x02] Device … RSSI: -87`` — so stripping
    the ANSI escapes alone leaves ``[\x01\x02CHG\x01\x02]`` and every
    ``^[(NEW|CHG|DEL)]`` match fails (measured on the box, 2026-10-17: 18 RSSI
    lines in the output, 0 parsed).
    """
    text = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
    return text.replace("\x01", "").replace("\x02", "")


# ── scanning ────────────────────────────────────────────────────────────────
def parse_devices(text: str) -> list[dict]:
    """``bluetoothctl`` monitor output → [{address, name, rssi, type}].

    Two things this has to get right, both learned on a real scan (2026-10-17):

    * a name comes **only** from a ``[NEW] Device XX Some Name`` line. ``[CHG]``
      lines carry every property bluez learns, and treating them as names is how
      a device ended up *called* ``TxPower: 7`` / ``ServiceData Key: …``;
    * an ``RSSI`` is the only thing that tells an operator which of 24 devices is
      the remote in their hand, and it only ever arrives as a ``[CHG]`` line.
    """
    seen: dict[str, dict] = {}

    def entry_for(address: str) -> dict:
        return seen.setdefault(address, {"address": address, "name": "",
                                         "rssi": None, "type": ""})

    for raw in _clean(text).splitlines():
        line = raw.strip()
        tag = re.match(r"^\[(NEW|CHG|DEL)\]\s*", line)
        if tag is None:
            continue
        rest = line[tag.end():]
        match = re.match(r"Device\s+([0-9A-F]{2}(?::[0-9A-F]{2}){5})\s*(.*)$", rest)
        if not match:
            continue
        address = match.group(1).upper()
        detail = match.group(2).strip()
        if tag.group(1) != "NEW":
            rssi = re.match(r"RSSI:\s*(-?\d+)\s*$", detail)
            if rssi:
                entry = entry_for(address)
                value = int(rssi.group(1))
                entry["rssi"] = value if entry["rssi"] is None else max(entry["rssi"], value)
            continue
        # bluez prints the address with dashes when it has no name
        name = "" if not detail or detail.replace("-", ":") == address else detail
        entry = entry_for(address)
        if name and not entry["name"]:
            entry["name"] = name
    # 既没名字也没信号强度的条目是上一次扫描的缓存回声，不是"现在就在旁边"。
    fresh = [d for d in seen.values() if d["name"] or d["rssi"] is not None]
    return sorted(fresh, key=lambda d: (not d["name"],
                                        -(d["rssi"] if d["rssi"] is not None else -999),
                                        d["name"] or d["address"]))


def parse_btmgmt(text: str) -> list[dict]:
    """``btmgmt find -l`` records → [{address, name, rssi, type}], strongest first.

    Signal strength is the only thing that lets an operator pick the remote in
    their hand out of a list: measured 2026-10-17 on a W103D in a flat, a BR/EDR
    scan returned **23 devices**, most of them nameless BLE beacons, and the page
    showed address + name only.  RSSI also orders the list, so the remote the
    operator is holding next to the box lands at the top.

    Duplicates are the normal case (one device, many sightings): keep the
    strongest RSSI seen and the first name.
    """
    seen: dict[str, dict] = {}
    current: Optional[dict] = None
    for raw in _clean(text).splitlines():
        match = _BTMGMT_DEV.search(raw)
        if match:
            address = match.group(1).upper()
            kind = " ".join(match.group(2).split())
            rssi = int(match.group(3))
            current = seen.get(address)
            if current is None:
                current = seen[address] = {"address": address, "name": "",
                                           "rssi": rssi, "type": kind}
            else:
                current["rssi"] = max(current["rssi"], rssi)
                current["type"] = kind
            continue
        name = _BTMGMT_NAME.match(raw.strip()) if current is not None else None
        if name and not current["name"]:
            current["name"] = name.group(1).strip()
    return sorted(seen.values(), key=lambda d: (-(d["rssi"] or -999), not d["name"],
                                                d["name"] or d["address"]))


def parse_known(text: str) -> dict:
    """``bluetoothctl devices`` → {address: name} for everything bluez remembers.

    Needed because bluez announces a device with ``[NEW]`` **once**: the second
    scan in a row says nothing about it, so a list built from the scan alone
    shrinks to nothing on a warm cache (measured on the box: 20 devices on the
    first scan, 0 on the one right after).
    """
    out: dict = {}
    for line in _clean(text).splitlines():
        match = re.search(r"Device\s+([0-9A-F]{2}(?::[0-9A-F]{2}){5})\s*(.*)$", line)
        if not match:
            continue
        address = match.group(1).upper()
        name = match.group(2).strip()
        if not name or name.replace("-", ":") == address:
            name = ""
        if name:
            out[address] = name
    return out


def scan(seconds: int = SCAN_SECONDS, runner: Optional[Runner] = None) -> list[dict]:
    """Nearby devices **with signal strength**, the ones in range first.

    ``btmgmt find -l`` is the preferred source (it reports RSSI and the address
    type) but it needs CAP_NET_ADMIN, which the server does not have — so in
    practice the server lands on ``bluetoothctl``, which reports RSSI as
    ``[CHG] … RSSI: -54`` for devices advertising right now.

    The scan alone is not enough: ``[NEW]`` is printed once per device per
    bluetoothd's lifetime, so a second scan returns nothing about a device it
    already knows. Names therefore come from the scan *and* from
    ``bluetoothctl devices``, and an entry with neither a name nor an RSSI is
    dropped — that is the old cache echo that drowned the remote in a 24-item
    list (2026-10-17).
    """
    _rc, out, _err = _run(["timeout", str(seconds), "btmgmt", "find", "-l"], runner)
    devices = parse_btmgmt(out)
    if devices:
        return devices

    _rc, out, _err = _run(["bluetoothctl", "--timeout", str(seconds), "scan", "bredr"], runner)
    live = {d["address"]: d for d in parse_devices(out)}
    _rc, known, _err = _run(["bluetoothctl", "devices"], runner)
    for address, name in parse_known(known).items():
        entry = live.setdefault(address, {"address": address, "name": "",
                                          "rssi": None, "type": ""})
        if not entry["name"]:
            entry["name"] = name
    merged = [d for d in live.values() if d["name"] or d["rssi"] is not None]
    # bluez 已经从上一轮见过这台遥控器的话，它知道那是个键盘（`Icon: input-keyboard`）——
    # 这是列表里最结实的一条「这台就是遥控器」的证据，比名字还硬（名字可以空白）。
    # 只对前 16 个查，`bluetoothctl info` 是一次一个子进程，24 个就慢到能被察觉了。
    for entry in merged[:16]:
        entry["icon"] = info(entry["address"], runner).get("icon", "")
    # 像遥控器的先来，然后当场扫到的（强的在前），最后只剩缓存名字的。
    return sorted(merged, key=lambda d: (
        not str(d.get("icon", "")).startswith("input-"),
        not (d["name"] and d["rssi"] is not None),
        not d["rssi"] is not None,
        -(d["rssi"] if d["rssi"] is not None else -999),
        d["name"] or d["address"],
    ))


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
                info[field] = (value if field in ("name", "alias", "icon")
                               else value.lower() == "yes")
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
