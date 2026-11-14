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
import threading
import time
from typing import Callable, Optional

Runner = Callable[[list], tuple]

#: 同一时刻只允许一个蓝牙操作（扫描**或**配对）。
#:
#: 2026-11-14 现场（W103D，小米 `MI RC`）：页面没有互斥，操作员连点两次「配对」就
#: 得到两个并行的 3 分钟重试循环，各自在真 pty 里 `power on` + `scan on`。内核被顶到
#: ```
#: Bluetooth: hci0: Opcode 0x2042 failed: -16
#: Bluetooth: hci0: Unable to disable scanning: -16
#: ```
#: `0x2042` 是 `LE_Set_Scan_Enable`，`-16` = EBUSY。此后**每一次** start discovery
#: 都是 `Busy (0x0a)` / `InProgress`：页面扫不到任何设备（实测连旁边 9 台手机都没有），
#: 配对请求直接弹回 `InProgress`。重启 bluetoothd、`bluetoothctl power off`（也报
#: `Failed`）都无效，只有 `hciconfig hci0 reset` 能救。这不是「配不上」，是控制器被
#: 自己的两个会话卡死 —— 所以第二个人必须被**拒**，而不是排队再来一遍。
_OPERATION = threading.Lock()
_OPERATION_KIND: Optional[str] = None


class Busy(RuntimeError):
    """另一个蓝牙操作正在跑；调用方应当告诉操作员等这一轮结束。"""

    def __init__(self, kind: str = ""):
        self.kind = kind or "蓝牙操作"
        super().__init__(self.kind)


def _enter(kind: str) -> None:
    global _OPERATION_KIND
    if not _OPERATION.acquire(blocking=False):
        raise Busy(_OPERATION_KIND or "蓝牙操作")
    _OPERATION_KIND = kind


def _leave() -> None:
    global _OPERATION_KIND
    _OPERATION_KIND = None
    _OPERATION.release()

#: How long a scan listens.  A remote in pairing mode advertises continuously,
#: so this only needs to outlast the inquiry cycle (~10 s).
SCAN_SECONDS = 12

#: 遥控器的**名字**（子串匹配，大小写不敏感），逗号分隔可用
#: ``MRRC_BT_REMOTE_NAME`` 覆盖。按名字认，不按 MAC：来料批次会换地址，而
#: 贴在盒子上的那台遥控器叫什么，是能一眼看见的事实（2026-10-18 现场要求）。
#: 名字对上几台就配几台 —— 多配一台的代价远小于"这台不认"。
#: `MI RC` 是小米蓝牙遥控器 2 的**广播短名**（2026-11-14 实测：EIR 里 Name="MI RC"，
#: 配对后内核输入设备名才是「小米蓝牙语音遥控器」，PID 2717:32b8）。它不在名单里时
#: 页面只是不自动配、仍可手点 —— 但「遥控器在闪灯，页面却一句话不说」正是操作员无法
#: 行动的那种沉默，所以把它加进来。
DEFAULT_REMOTE_NAMES = ("CMCC_Voice_Remote", "MI RC", "Xiaomi", "小米")


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


def _int(text, default: Optional[int] = None) -> Optional[int]:
    """Parse an int out of a ``bluetoothctl`` line without raising on garbage.

    Everything this module parses comes from a CLI we do not control: one
    malformed line must cost that line, not the whole scan or the whole pair.
    """
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


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
                value = _int(rssi.group(1))
                if value is not None:
                    entry = entry_for(address)
                    entry["rssi"] = (value if entry["rssi"] is None
                                     else max(entry["rssi"], value))
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
            parsed = _int(match.group(3))
            rssi = -999 if parsed is None else parsed
            current = seen.get(address)
            if current is None:
                current = seen[address] = {"address": address, "name": "",
                                           "rssi": rssi, "type": kind}
            else:
                current["rssi"] = max(current["rssi"], rssi)
                current["type"] = kind
            continue
        name = _BTMGMT_NAME.match(raw.strip()) if current is not None else None
        if name and current is not None and not current["name"]:
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


def scan(seconds: int = SCAN_SECONDS, runner: Optional[Runner] = None,
         reset: Optional[Callable] = None) -> list[dict]:
    """Nearby devices with signal strength; refuses to run two at once.

    The discovery notes live on :func:`_scan_locked` (they are about bluez, not
    about the lock).  Raises :class:`Busy` when a pair is already running.
    """
    _enter("扫描")
    try:
        return _scan_locked(seconds, runner, reset)
    finally:
        _leave()


def _scan_locked(seconds: int, runner: Optional[Runner],
                 reset: Optional[Callable] = None) -> list[dict]:
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

    退回 bluetoothctl 时必须**两种无线都扫**：BLE-only 的遥控器（小米蓝牙遥控器 2、
    以及实测那支 CMCC 语音遥控器）在 `scan bredr` 里永远不出现。这条以前“知道但没写”：
    主路径 `btmgmt find -b/-l` 是给 root 的，服务跑在 `mrrc` 上实测
    `Unable to start discovery. status 0x14 (Permission Denied)`（2026-11-10，W103D），
    于是每次扫码都走退路，而退路只扫经典蓝牙 —— 页面上的“扫描”对 BLE 遥控器等于不存在。
    `bluetoothctl` 走 bluetoothd 的 DBus，普通用户即可（同机实测 `scan le` 正常）。
    """
    # `-l` is LE and `-b` is BR/EDR; they are **mutually exclusive** (`find [-l|-b]`),
    # and neither means "both".  `-l` alone is how a classic remote stays invisible
    # (measured 2026-10-17: `find -l` → 58 LE findings, 0 BR/EDR), so the two scans
    # run one after the other and are merged.  A cheap remote can be either kind —
    # the measured one is a BLE HID device that only shows up in an LE scan.
    half = max(4, seconds // 2)
    devices: list[dict] = []
    for flag in ("-b", "-l"):
        _rc, out, _err = _run(["timeout", str(half), "btmgmt", "find", flag], runner)
        devices.extend(parse_btmgmt(out))
    if devices:
        return sorted(devices, key=lambda d: (-(d["rssi"] or -999), not d["name"],
                                              d["name"] or d["address"]))

    live: dict[str, dict] = {}
    for transport in ("bredr", "le"):
        out = _scan_transport(transport, half, runner, reset)
        for address, device in {d["address"]: d for d in parse_devices(out)}.items():
            entry = live.setdefault(address, device)
            if not entry["name"]:
                entry["name"] = device["name"]
            if device["rssi"] is not None:
                seen = entry["rssi"]
                entry["rssi"] = device["rssi"] if seen is None else max(seen, device["rssi"])
            if device["type"] and not entry["type"]:
                entry["type"] = device["type"]
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
    # 同一次 `info` 顺带带出 `paired`：页面要靠它决定这一行给「配对」还是「忘记」，
    # 而多跑一遍 info 就是二十几个子进程（2026-11-10 现场：没有「忘记」入口，已配好的
    # 小米被反复重配，顺手把控制器上下电，另一支遥控器就再也配不上了）。
    for entry in merged[:16]:
        detail = info(entry["address"], runner)
        entry["icon"] = detail.get("icon", "")
        entry["paired"] = bool(detail.get("paired"))
    # 像遥控器的先来，然后当场扫到的（强的在前），最后只剩缓存名字的。
    return sorted(merged, key=lambda d: (
        not str(d.get("icon", "")).startswith("input-"),
        not (d["name"] and d["rssi"] is not None),
        not d["rssi"] is not None,
        -(d["rssi"] if d["rssi"] is not None else -999),
        d["name"] or d["address"],
    ))


# ── pairing ─────────────────────────────────────────────────────────────────
#: 「控制器卡在发现里」的三种说法，全部来自实测：
#: `Failed to start discovery: org.bluez.Error.InProgress`（bluez 层）、
#: `Unable to start discovery. status 0x0a (Busy)`（mgmt 层）、
#: `Unable to disable scanning: -16`（内核日志，扫描关不掉）。
_DISCOVERY_BLOCKED = re.compile(
    r"(Failed to start discovery|Unable to start discovery|InProgress"
    r"|Busy \(0x0a\)|Unable to disable scanning)")


def discovery_blocked(text: str) -> bool:
    """bluez/内核是不是在说「扫描起不来 / 关不掉」。"""
    return bool(_DISCOVERY_BLOCKED.search(text or ""))


def reset_adapter(runner: Optional[Callable] = None) -> bool:
    """把适配器从「扫描卡死」里拽出来。True 表示这条动词真的跑了。

    `hciconfig hci0 reset` 是 2026-11-14 现场唯一有效的动作：bluetoothd 重启和
    `power off`（它自己也报 `org.bluez.Error.Failed`）都撼不动内核里那个 EBUSY。
    服务用户没有 root，所以走的是 helper 的固定动词（`reset-bluetooth`）；没有
    helper（桌面 / 源码树）就表示这条补救不可用，不是异常。
    """
    run = runner or _default_reset_adapter
    try:
        return bool(run())
    except Exception:
        return False


def _default_reset_adapter() -> bool:
    import box_ops                      # imported lazily: desktop runs lack it
    return bool(box_ops.reset_bluetooth().get("ok"))


def _scan_transport(transport: str, half: int, runner: Optional[Runner],
                    reset: Optional[Callable]) -> str:
    """一次 `bluetoothctl scan <transport>`；发现卡死就先 reset 再试一次。

    重试一次就够：reset 之后仍然 InProgress 说明适配器真的坏了（或者没有 root
    helper），再多试几次只是把 12 秒的扫描拉成两分钟。
    """
    argv = ["bluetoothctl", "--timeout", str(half), "scan", transport]
    _rc, out, err = _run(argv, runner)
    if discovery_blocked(out + "\n" + err) and reset_adapter(reset):
        time.sleep(2)
        _rc, out, err = _run(argv, runner)
    return out


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
        ("scan on", scan),
        # 发现之后**先关扫描再配**：2026-11-14 实测这颗 MT7663S 在 LE 扫描还开着时
        # `LE Extended Create Connection` 会以 0x3e（Connection Failed to be
        # Established）失败；同一分钟里先 `scan off` 再连就能建链。
        ("scan off", 0.5),
        (f"pair {address}", seconds),
        (f"trust {address}", 3.0),
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
        # `quit` 是礼貌，KILL 是保证。2026-11-14 实测：`_pty_session()` 返回后仍有
        # `bluetoothctl --agent NoInputNoOutput` 活着（`terminate()` 之后 `wait()`
        # 立刻返回、子进程却还在），而残留的交互会话会一直占着 agent 和发现状态 ——
        # 连着两次现场配对失败之后，适配器就是这么被顶到 Busy 的。
        try:
            os.write(master, b"quit\n")
        except OSError:
            pass
        for closer in (lambda: proc.wait(timeout=2),
                       lambda: (proc.terminate(), proc.wait(timeout=3)),
                       lambda: (proc.kill(), proc.wait(timeout=3))):
            if proc.poll() is not None:
                break
            try:
                closer()
            except Exception:
                pass
        try:
            os.close(master)
        except OSError:
            pass
    return "".join(chunks)


def restart_bluetoothd(runner: Optional[Callable] = None) -> bool:
    """Restart bluetoothd through the root helper. True when the verb ran.

    The cure for a half-built HOG session lives in the daemon's memory, so only a
    restart clears it (see `pair()`). The service user has no root: the helper
    socket is the one path, and it stays a closed verb table — this function can
    ask for exactly `restart-bluetooth`, nothing else. No helper (a desktop or a
    source-tree run) simply means the cure is unavailable, not an exception.
    """
    run = runner or _default_restart_bluetooth
    try:
        return bool(run())
    except Exception:
        return False


def _default_restart_bluetooth() -> bool:
    import box_ops                      # imported lazily: desktop runs lack it
    return bool(box_ops.restart_bluetooth().get("ok"))


def pair(address: str, seconds: int = 25, runner: Optional[Runner] = None, *,
         keyboard: Optional[Callable[[str], bool]] = None,
         cure: bool = True,
         attempts: int = 6, retry_delay: float = 5.0) -> dict:
    """Pair + trust, one at a time (raises :class:`Busy` if a scan is running)."""
    _enter("配对")
    try:
        return _pair_locked(address, seconds, runner, keyboard=keyboard,
                            cure=cure, attempts=attempts, retry_delay=retry_delay)
    finally:
        _leave()


def _pair_locked(address: str, seconds: int = 25, runner: Optional[Runner] = None, *,
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
    if (state.get("paired") and state.get("connected") is False
            and not keys(address)):
        # 已经绑过、只是**链路空闲**（遥控器为了省电，没人按键时不保持连接）。
        # 这条路绝不能再往下走：下面是「配上但按键不来」的补救，它先重启 bluetoothd、
        # 再 remove bond + 控制器上下电 —— 对一支睡着的遥控器就是把它拆掉。
        # 2026-11-14 实测：这条误判在遥控器睡着时**必然**触发，而且看起来像
        # 「配对又失败了」，实际只是没唤醒。
        state["error"] = ("已经配对过，只是链路空闲 —— 按一下遥控器的键把它唤醒即可；"
                          "按键仍然没反应再点「重配」。")
        return state
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
        # Paired/Bonded 都为真、链路也通，但 hog 拼不出键盘设备（`keys(address)`
        # 为假），按键一个都不来。2026-10-19 现场抓到两种表现，原因不同：
        #   ① bluez 手里的 GATT 数据库残缺 —— 它自己报
        #      `No attribute found within the given range`，每次重连都拿这份坏库
        #      去读，读不到就拆设备（dmesg 里实例号一路涨）。**重配 bond 治不了**
        #      （试过三次），重启 bluetoothd 治得了：内存态扔掉，磁盘上的 bond
        #      原样重读，一次就建成。
        #   ② 陈旧的 bond 本身对不上 —— 清 bond + 控制器上下电 + 再配。
        # 所以先走 ① 这条无破坏性的路，仍然不行才走 ②。
        restarted = restart_bluetoothd()
        time.sleep(max(2.0, retry_delay))
        if restarted and keys(address):
            state = info(address)
        else:
            remove(address)
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
