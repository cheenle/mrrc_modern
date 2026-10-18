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

import os
import re
import subprocess
import time
from typing import Callable, Optional

Runner = Callable[[list], tuple]

#: How long a scan listens.  A remote in pairing mode advertises continuously,
#: so this only needs to outlast the inquiry cycle (~10 s).
SCAN_SECONDS = 12

#: 遥控器的**名字**（子串匹配，大小写不敏感），逗号分隔可用
#: ``MRRC_BT_REMOTE_NAME`` 覆盖。按名字认，不按 MAC：来料批次会换地址，而
#: 贴在盒子上的那台遥控器叫什么，是能一眼看见的事实（2026-10-18 现场要求）。
#: 名字对上几台就配几台 —— 多配一台的代价远小于"这台不认"。
DEFAULT_REMOTE_NAMES = ("CMCC_Voice_Remote",)


def remote_name_patterns() -> tuple[str, ...]:
    """The name patterns an auto-pair will accept, from the environment."""
    raw = (os.environ.get("MRRC_BT_REMOTE_NAME") or "").strip()
    if not raw:
        return DEFAULT_REMOTE_NAMES
    parts = tuple(part.strip() for part in raw.split(",") if part.strip())
    return parts or DEFAULT_REMOTE_NAMES


def matches_remote(name: str, patterns: Optional[tuple] = None) -> bool:
    """Is this the remote we are supposed to bond? Substring, case-insensitive."""
    if not name:
        return False
    lowered = name.casefold()
    return any(p.casefold() in lowered for p in (patterns or remote_name_patterns()))


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
    # `-l` is LE and `-b` is BR/EDR; they are **mutually exclusive** (`find [-l|-b]`),
    # and neither means "both".  `-l` alone is how a classic remote stays invisible
    # (measured 2026-10-17: `find -l` → 58 LE findings, 0 BR/EDR), so the two scans
    # run one after the other and are merged.  A cheap remote can be either kind —
    # the measured one is a BLE HID device that only shows up in an LE scan.
    half = max(4, int(seconds) // 2)
    devices: list[dict] = []
    for flag in ("-b", "-l"):
        _rc, out, _err = _run(["timeout", str(half), "btmgmt", "find", flag], runner)
        devices.extend(parse_btmgmt(out))
    if devices:
        return sorted(devices, key=lambda d: (-(d["rssi"] or -999), not d["name"],
                                              d["name"] or d["address"]))

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


#: What bluez says when a pair attempt fails, and what the operator needs to see.
#: Swallowing it left the page saying "没有配上，确认遥控器在配对模式" for every
#: possible cause — including "Device not available" (the remote stopped
#: advertising) and "AuthenticationFailed" (it left pairing mode mid-handshake).
_PAIR_ERROR = re.compile(
    r"(Failed to pair[^\n]*"
    r"|Device .* not available"
    r"|Authentication(Canceled|Failed|Timeout|Rejected)"
    r"|org\.bluez\.Error\.\w+"
    r"|Connection attempt failed[^\n]*)")


def pair_error(text: str) -> str:
    """The one line worth showing an operator, or ``""`` when it worked."""
    for raw in _clean(text).splitlines():
        match = _PAIR_ERROR.search(raw)
        if match:
            return match.group(1).strip()
    return ""


def pair_script(address: str, seconds: int = 25) -> list[tuple[str, float]]:
    """The lines to type at an interactive ``bluetoothctl``, and how long to wait.

    Order matters: discovery first (a remote only starts advertising when it is
    put in pairing mode, and it may take a few seconds), then the pairing itself.
    """
    scan = min(12, max(6, seconds // 2))
    return [
        ("power on", 1.0),
        ("scan on", float(scan)),
        (f"pair {address}", float(seconds)),
        (f"trust {address}", 3.0),
        ("scan off", 0.5),
        ("quit", 1.0),
    ]


def _pty_session(lines: list[tuple[str, float]], argv: Optional[list] = None) -> str:
    """Run ``bluetoothctl`` on a real pty and type `lines` into it.

    The pty is not cosmetic: **without an interactive session bluez registers no
    pairing agent**, so it sends a Pairing Request whose bonding bit is 0 — and a
    BLE HID remote answers SMP ``Invalid parameters``, because a remote must store
    a key to reconnect at all.  Measured on the box: one-shot
    ``bluetoothctl --timeout 25 pair X`` →
    ``Authentication requirement: No bonding … (0x28)`` → ``Pairing Failed:
    Invalid parameters (0x0a)``; the same command typed into a pty →
    ``Bonding … (0x29)`` → ``Pairing successful``, and bluez then builds
    ``CMCC_Voice_Remote Keyboard``.
    """
    import os
    import pty
    import select
    import subprocess
    import time

    master, slave = pty.openpty()
    proc = subprocess.Popen(list(argv or ["bluetoothctl", "--agent", "NoInputNoOutput"]),
                            stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    chunks: list[str] = []
    try:
        for line, wait in lines:
            try:
                os.write(master, (line + "\n").encode())
            except OSError:
                break
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if master not in ready:
                    continue
                try:
                    data = os.read(master, 4096)
                except OSError:
                    data = b""
                if not data:
                    break
                chunks.append(data.decode(errors="replace"))
    finally:
        for closer in (proc.terminate, proc.wait, proc.kill):
            try:
                closer()
            except Exception:
                pass
        try:
            os.close(master)
        except OSError:
            pass
    return "".join(chunks)


def pair(address: str, seconds: int = 25, runner: Optional[Runner] = None, *,
         keyboard: Optional[Callable[[str], bool]] = None,
         cure: bool = True,
         attempts: int = 6, retry_delay: float = 5.0) -> dict:
    """Pair + trust.  Returns the resulting ``info()``, plus ``error`` on failure.

    The error is surfaced on purpose: "没有配上" with no reason is the message an
    operator can do nothing with (2026-10-17: three failed attempts on a box, and
    the reason — the remote had left pairing mode — was only in the terminal).

    A ``runner`` (tests) keeps the one-shot shape; the real path drives an
    interactive session because that is the only way bluez requests bonding.
    """
    if runner is not None:
        _rc, out, err = _run(["bluetoothctl", "--timeout", str(seconds),
                              "pair", address], runner)
        _run(["bluetoothctl", "trust", address], runner)
        state = info(address, runner)
        state["error"] = "" if state.get("paired") else pair_error(out + "\n" + err)
        return state

    keys = keyboard or _keyboard_stable
    out = ""
    state = info(address)
    # 不用"等广播"当前置条件：这台遥控器的广播里**没有 RSSI**（2026-10-19 实测：
    # 它只出 `[NEW] Device … CMCC_Voice_Remote`，别的设备才有 `RSSI:` 行），所以
    # 按 RSSI 判"在不在广播"会永远判成不在——操作员把键按烂也走不到配对。
    # 改成"直接配，bluez 说不可用就隔几秒再试"：配对窗口由遥控器决定，重试把
    # 时序问题变成概率问题，而且失败时拿到的是 bluez 的原话。
    for attempt in range(1, max(1, attempts) + 1):
        try:
            out += _pty_session(pair_script(address, seconds))
        except (ImportError, OSError):
            _rc, out2, _err = _run(["bluetoothctl", "--timeout", str(seconds),
                                    "pair", address])
            _run(["bluetoothctl", "trust", address])
            out += out2
        state = info(address)
        if state.get("paired"):
            break
        if attempt < attempts:
            time.sleep(max(0.0, retry_delay))
    if state.get("paired") and not keys(address) and cure:
        # 文档 §2 那条实测的陷阱：Paired/Bonded 都为真，链路却没加密，bluez 的
        # hog 拼不出键盘设备，按键一个都不来。解法只有一条：清掉陈旧 bond、
        # 控制器断电重启、再配一次。
        remove()
        power_cycle()
        time.sleep(2)
        try:
            out += _pty_session(pair_script(address, seconds))
        except (ImportError, OSError):
            pass
        state = info(address)

    ok = bool(state.get("paired")) and keys(address)
    if ok:
        state["error"] = ""
    elif pair_error(out):
        raw = pair_error(out)
        if "not available" in raw.lower() or "not found" in raw.lower():
            raw += " —— 遥控器没有进入配对模式：按住「OK + 返回」约 5 秒（指示灯快闪）再点一次"
        state["error"] = raw
    elif state.get("paired"):
        why = "链路没加密（bluez 报 ATT 0x0E）" if _att_errors_recent() else "按键没有响应"
        state["error"] = "已配对，但%s——点「重配」再试一次" % why
    else:
        state["error"] = ("配对失败：遥控器没有进入配对模式，或者已经超时退出。"
                          "按住「OK + 返回」约 5 秒（指示灯快闪）再点一次。")
    return state


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


def _keyboard_stable(address: str, samples: int = 3, interval: float = 1.0) -> bool:
    """键盘设备**连续存在**才算数。

    单点采样骗过一次现场（2026-10-18 深夜）：链路没加密时 bluez 的 `hog` 会先把
    键盘设备建出来、读属性失败（ATT 0x0E `unlikely error`）再拆掉，如此循环。
    正好在"存在"的那一刻采样，页面就会写「已配对，按键可用 ✓」——而所有按键都是死的。
    """
    import time

    for _ in range(max(1, samples)):
        if not keyboard_device_present(address):
            return False
        time.sleep(interval)
    return keyboard_device_present(address)


def _att_errors_recent(seconds: int = 90) -> bool:
    """最近 bluez 有没有报 ATT 0x0E —— 那是"链路没加密"的直接证据。

    `hog-lib.c: … Request attribute has encountered an unlikely error` 只会在
    GATT 读被设备拒绝时出现，而实测这种拒绝的唯一原因是链路处明文。
    """
    import subprocess

    try:
        out = subprocess.run(["journalctl", "-u", "bluetooth",
                              "--since", "%d sec ago" % max(10, seconds), "--no-pager"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "unlikely error" in out


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
