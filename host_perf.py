"""Host load, IO and network — read from /proc and /sys, nothing else.

The point of this module is a question an operator actually asks: *the page is
slow — is the box out of CPU, out of IO, or is the server itself stuck?* Those
have different fixes, so the numbers have to be attributable:

  * host-wide CPU, per core, and the busy share spent in iowait;
  * **which processes** burned the CPU during the interval (the server tree,
    the scope pipe, the SDR pipe and the FT8 worker are the usual suspects);
  * disk and network **rates**, not totals — a total tells you nothing about now;
  * thermals and the current clock, because this box throttles;
  * and a one-line verdict, so nobody has to do arithmetic on a phone.

Design notes, all of them learned from what makes such a page wrong:

  * Everything is a **delta between two samples**. Rates need a previous
    reading and a clock, so `Sampler` keeps one and refuses to invent a number
    before it has a second sample (`available: False` beats a made-up 0.0).
  * `/proc` readers are injectable (`read=`, `listdir=`, `clock=`): the tests
    feed literal kernel text, so the parsing is testable on a laptop with no
    Raspberry Pi in sight.
  * Fields the kernel does not provide are **absent**, never zero. A missing
    thermal zone must not look like 0 °C, which on this box would mean "cool,
    keep going" — the opposite of the truth.
  * The verdict is deliberately conservative: it only names a bottleneck when
    the number is unambiguous, and it says "看不出来" otherwise. A wrong
    diagnosis here sends the operator into the wrong rabbit hole.

Stdlib only and no app imports: unit-testable, and safe to import from the
host helper side or a hot fix.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Optional

#: Whole block devices only. Partitions are children of these and their counters
#: are *already included* in the parent's, so summing both would double the disk
#: rate — the classic way a "disk busy 140 %" appears.
_BLOCK_WHOLE = ("sd", "vd", "hd", "mmcblk", "nvme", "dm-", "md")
_PARTITION_MARKERS = ("p1", "p2", "p3", "p4", "p5", "p6", "p7")

#: Interfaces that are never the operator's answer. `lo` is the radio's own
#: traffic (spectrum + audio over a tunnel), which would otherwise look like the
#: network being busy.
_HIDDEN_IFACES = ("lo", "sit0", "ip6tnl0", "ip6gre0", "tunl0", "gre0", "gretap0")
#: 真正承载业务的接口前缀（`end0`/`eth0`/`wlan0` 在这台盒子上都见过）。
_WANTED_IFACE_PREFIXES = ("eth", "end", "enp", "eno", "wlan", "wl", "usb", "ppp",
                          "tun", "tap", "br-")

TOP_PROCESSES = 5


def _split_ws(text: str) -> list:
    return text.split()


def _read_int(path: str, read: Callable[[str], str]) -> Optional[int]:
    try:
        return int(read(path).strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def read_cpu(read: Callable[[str], str]) -> dict:
    """`/proc/stat` first line plus one entry per core, as raw jiffies."""
    try:
        text = read("/proc/stat")
    except OSError:
        return {}
    out = {"total": None, "cores": []}
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        parts = _split_ws(line)
        if len(parts) < 5:
            continue
        values = []
        for token in parts[1:9]:
            try:
                values.append(int(token))
            except ValueError:
                values.append(0)
        while len(values) < 8:
            values.append(0)
        entry = {"user": values[0], "nice": values[1], "system": values[2],
                 "idle": values[3], "iowait": values[4], "irq": values[5],
                 "softirq": values[6], "steal": values[7]}
        if parts[0] == "cpu":
            out["total"] = entry
        else:
            out["cores"].append(entry)
    return out


def read_load(read: Callable[[str], str]) -> dict:
    try:
        parts = _split_ws(read("/proc/loadavg"))
    except OSError:
        return {}
    try:
        running, _, total = parts[3].partition("/")
        return {"load1": float(parts[0]), "load5": float(parts[1]),
                "load15": float(parts[2]), "running": int(running),
                "threads": int(total)}
    except (ValueError, IndexError):
        return {}


_MEM_KEYS = {"MemTotal": "total_kb", "MemAvailable": "available_kb",
             "MemFree": "free_kb", "SwapTotal": "swap_total_kb",
             "SwapFree": "swap_free_kb", "Buffers": "buffers_kb",
             "Cached": "cached_kb"}


def read_mem(read: Callable[[str], str]) -> dict:
    try:
        text = read("/proc/meminfo")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        key = _MEM_KEYS.get(name.strip())
        if not key:
            continue
        try:
            out[key] = int(rest.strip().split()[0])
        except (ValueError, IndexError):
            continue
    return out


def _is_whole_device(name: str) -> bool:
    if not name.startswith(_BLOCK_WHOLE):
        return False
    if name.startswith("mmcblk") or name.startswith("nvme"):
        return "p" not in name.split("n")[-1].split("k")[-1]
    if name.startswith("dm-") or name.startswith("md"):
        return True
    return not name[-1].isdigit()


def read_disk(read: Callable[[str], str]) -> dict:
    """Sectors + milliseconds spent doing IO, summed over whole devices.

    `mmcblk2` (the eMMC) and `mmcblk2p1/p2` (its partitions) both appear in
    `/proc/diskstats`; only the whole device is counted here.
    """
    try:
        text = read("/proc/diskstats")
    except OSError:
        return {}
    sectors_read = sectors_written = busy_ms = 0
    devices = []
    for line in text.splitlines():
        parts = _split_ws(line)
        if len(parts) < 14:
            continue
        name = parts[2]
        if not _is_whole_device(name):
            continue
        try:
            read_sectors = int(parts[5])
            written_sectors = int(parts[9])
            busy = int(parts[12])
        except (ValueError, IndexError):
            continue
        sectors_read += read_sectors
        sectors_written += written_sectors
        busy_ms += busy
        devices.append(name)
    return {"sectors_read": sectors_read, "sectors_written": sectors_written,
            "busy_ms": busy_ms, "devices": devices}


def read_net(read: Callable[[str], str]) -> dict:
    """Per-interface byte counters from `/proc/net/dev`."""
    try:
        text = read("/proc/net/dev")
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        name, sep, rest = line.partition(":")
        if not sep:
            continue
        name = name.strip()
        if not name or name in _HIDDEN_IFACES:
            continue
        if not name.startswith(_WANTED_IFACE_PREFIXES):
            continue
        fields = _split_ws(rest)
        if len(fields) < 9:
            continue
        try:
            out[name] = {"rx": int(fields[0]), "tx": int(fields[8])}
        except ValueError:
            continue
    return out


def read_thermal(read: Callable[[str], str], listdir: Callable[[str], list]) -> dict:
    """Max thermal zone (°C) and the average current clock (MHz)."""
    out = {}
    try:
        zones = listdir("/sys/class/thermal")
    except OSError:
        zones = []
    temps = []
    for zone in zones:
        if not zone.startswith("thermal_zone"):
            continue
        value = _read_int(f"/sys/class/thermal/{zone}/temp", read)
        if value is not None and -40000 < value < 200000:
            temps.append(value / 1000.0)
    if temps:
        out["max_c"] = round(max(temps), 1)
    try:
        cpus = [cpu for cpu in listdir("/sys/devices/system/cpu")
                if cpu.startswith("cpu") and cpu[3:].isdigit()]
    except OSError:
        cpus = []
    freqs = []
    for cpu in cpus:
        value = _read_int(f"/sys/devices/system/cpu/{cpu}/cpufreq/scaling_cur_freq", read)
        if value:
            freqs.append(value / 1000.0)
    if freqs:
        out["freq_mhz"] = round(sum(freqs) / len(freqs), 1)
        out["cores_online"] = len(freqs)
    return out


def _proc_name(pid: str, read: Callable[[str], str]) -> str:
    """A name an operator recognises: `server.py`, `scope_pipe`, `ft8-worker`.

    `comm` is useless here — every one of our processes is called `python`, and
    the whole question "which one is burning the CPU" needs them told apart. So
    `cmdline` wins, reduced to the last meaningful path component.
    """
    try:
        raw = read(f"/proc/{pid}/cmdline")
    except OSError:
        raw = ""
    argv = [part for part in raw.split("\x00") if part]
    if argv:
        name = _compact_argv(argv)
        if name:
            return name
    try:
        raw = read(f"/proc/{pid}/comm").strip()
        if raw:
            return raw
    except OSError:
        pass
    try:
        stat = read(f"/proc/{pid}/stat")
        # 名字在第一个 "(" 与最后一个 ")" 之间，可以含空格与括号；")" 之后第一个字段
        # 是进程状态（S/R/Z），不是名字。
        return stat[stat.index("(") + 1:stat.rindex(")")][:24]
    except (OSError, ValueError):
        return pid


def _compact_argv(argv: list) -> str:
    """`["/opt/x/venv/bin/python", "-m", "backends.ft710.scope_pipe"]` → `scope_pipe`.

    A plain binary keeps its basename (`frpc`, `sshd`). Anything that looks like a
    script path or a `-m` module is reduced to the last component, which is the
    name that appears in our own logs.
    """
    head = os.path.basename(argv[0])
    if not head.startswith(("python", "node", "sh", "bash")):
        return head[:24]
    for part in argv[1:]:
        if part in ("-c", "-e"):
            # 内联代码不是名字：不截它，否则进程表里会出现半句 Python。
            return head[:24]
        if part.startswith("-"):
            continue
        token = part.replace(":", "/")
        if "/" in token or token.endswith((".py", ".sh", ".js", ".mjs")):
            return os.path.basename(token)[:24] or head[:24]
        # `-m backends.ft710.scope_pipe` 是模块路径：取最后一段，日志里就是这么叫的。
        return token.split(".")[-1][:24] or head[:24]
    return head[:24]


def read_procs(read: Callable[[str], str], listdir: Callable[[str], list]) -> dict:
    """utime+stime per pid, with a display name (cheap: `comm`, not cmdline)."""
    try:
        pids = [p for p in listdir("/proc") if p.isdigit()]
    except OSError:
        return {}
    out = {}
    for pid in pids:
        try:
            stat = read(f"/proc/{pid}/stat")
        except OSError:
            continue
        # The comm field is parenthesised and may contain spaces: split after it.
        try:
            rest = stat[stat.rindex(")") + 2:].split()
            ticks = int(rest[11]) + int(rest[12])
        except (ValueError, IndexError):
            continue
        out[pid] = {"ticks": ticks, "name": _proc_name(pid, read)}
    return out


def read_sample(read: Optional[Callable[[str], str]] = None,
                listdir: Optional[Callable[[str], list]] = None) -> dict:
    """One raw reading. All counters, no rates — rates need two readings."""
    read = read or _default_read
    listdir = listdir or _default_listdir
    return {"cpu": read_cpu(read), "load": read_load(read), "mem": read_mem(read),
            "disk": read_disk(read), "net": read_net(read),
            "thermal": read_thermal(read, listdir),
            "procs": read_procs(read, listdir)}


def _default_read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _default_listdir(path: str) -> list:
    return os.listdir(path)


def _cpu_busy(entry: dict) -> tuple:
    """(busy jiffies, total jiffies) for one /proc/stat row."""
    idle = entry.get("idle", 0) + entry.get("iowait", 0)
    total = sum(entry.get(key, 0) for key in
                ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal"))
    return total - idle, total


def _pct(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    return round(100.0 * part / whole, 1)


class Sampler:
    """Turns two raw readings into the rates a person can act on.

    One instance per server. `tick()` is cheap (a few /proc reads) and the first
    call only seeds the baseline — it returns `available: False` rather than
    zeroes, because "0 % CPU" and "no idea yet" must not look the same.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 read: Optional[Callable[[str], str]] = None,
                 listdir: Optional[Callable[[str], list]] = None,
                 read_sample_fn: Optional[Callable] = None):
        self._clock = clock
        self._read = read
        self._listdir = listdir
        self._read_sample = read_sample_fn
        self._prev: Optional[dict] = None
        self._prev_at: Optional[float] = None

    def _sample(self) -> dict:
        if self._read_sample is not None:
            return self._read_sample()
        return read_sample(self._read, self._listdir)

    def tick(self) -> dict:
        now = self._clock()
        current = self._sample()
        previous, previous_at = self._prev, self._prev_at
        self._prev, self._prev_at = current, now
        if previous is None or previous_at is None:
            seeded = self._instant(current)
            seeded["available"] = False
            seeded["reason"] = "第一次采样：速率需要两个读数，2 秒后再看"
            return seeded
        interval = max(0.001, now - previous_at)
        snap = self._instant(current)
        snap.update(self._rates(previous, current, interval))
        snap["available"] = True
        snap["interval"] = round(interval, 2)
        return snap

    # ── things that need no previous sample ────────────────────────────────
    def _instant(self, sample: dict) -> dict:
        mem = sample.get("mem") or {}
        total_kb = mem.get("total_kb") or 0
        available_kb = mem.get("available_kb")
        swap_total = mem.get("swap_total_kb") or 0
        swap_free = mem.get("swap_free_kb") or 0
        load = sample.get("load") or {}
        cores = len((sample.get("cpu") or {}).get("cores") or [])
        out = {
            "load": dict(load),
            "cpus": (sample.get("load") or {}).get("cores") or cores or 0,
            "mem": {
                "total_mb": round(total_kb / 1024.0) if total_kb else None,
                "available_mb": round(available_kb / 1024.0) if available_kb is not None else None,
                "used_pct": _pct(total_kb - available_kb, total_kb) if available_kb is not None else None,
                "swap_used_mb": (round((swap_total - swap_free) / 1024.0)
                                 if swap_total else 0),
            },
            "thermal": dict(sample.get("thermal") or {}),
        }
        # 每核负载：键始终在，值未知时是 None（0.0 会被读成「一点压力都没有」）。
        out["load_per_core"] = (round((load.get("load1") or 0.0) / cores, 2)
                                if cores else None)
        return out

    # ── rates ─────────────────────────────────────────────────────────────
    def _rates(self, previous: dict, current: dict, interval: float) -> dict:
        out = {}
        prev_cpu = previous.get("cpu") or {}
        cur_cpu = current.get("cpu") or {}
        busy, total = _cpu_busy(cur_cpu.get("total") or {})
        pbusy, ptotal = _cpu_busy(prev_cpu.get("total") or {})
        if total > ptotal:
            out["cpu"] = {
                "busy_pct": _pct(busy - pbusy, total - ptotal),
                "iowait_pct": _pct((cur_cpu.get("total") or {}).get("iowait", 0)
                                   - (prev_cpu.get("total") or {}).get("iowait", 0),
                                   total - ptotal),
                "steal_pct": _pct((cur_cpu.get("total") or {}).get("steal", 0)
                                  - (prev_cpu.get("total") or {}).get("steal", 0),
                                  total - ptotal),
            }
            cores = []
            for index, entry in enumerate(cur_cpu.get("cores") or []):
                if index >= len(prev_cpu.get("cores") or []):
                    break
                cbusy, ctotal = _cpu_busy(entry)
                pbusy_c, ptotal_c = _cpu_busy(prev_cpu["cores"][index])
                if ctotal > ptotal_c:
                    cores.append(_pct(cbusy - pbusy_c, ctotal - ptotal_c))
            out["cpu"]["cores"] = cores

        prev_disk = previous.get("disk") or {}
        cur_disk = current.get("disk") or {}
        if cur_disk:
            out["disk"] = {
                # 512-byte sectors, always: /proc/diskstats is defined that way.
                "read_bps": int(max(0, cur_disk.get("sectors_read", 0)
                                    - prev_disk.get("sectors_read", 0)) * 512 / interval),
                "write_bps": int(max(0, cur_disk.get("sectors_written", 0)
                                     - prev_disk.get("sectors_written", 0)) * 512 / interval),
                "busy_pct": _pct(max(0, cur_disk.get("busy_ms", 0)
                                     - prev_disk.get("busy_ms", 0)),
                                 1000.0 * interval),
                "devices": cur_disk.get("devices") or [],
            }

        prev_net = previous.get("net") or {}
        cur_net = current.get("net") or {}
        ifaces = []
        for name, counters in sorted(cur_net.items()):
            before = prev_net.get(name)
            if not before:
                continue
            ifaces.append({
                "name": name,
                "rx_bps": int(max(0, counters["rx"] - before["rx"]) / interval),
                "tx_bps": int(max(0, counters["tx"] - before["tx"]) / interval),
            })
        if ifaces:
            out["net"] = ifaces

        prev_procs = previous.get("procs") or {}
        cur_procs = current.get("procs") or {}
        ticks_per_second = 100.0          # CONFIG_HZ on arm64 images
        burned = []
        for pid, info in cur_procs.items():
            before = prev_procs.get(pid)
            if not before:
                continue
            delta = info["ticks"] - before["ticks"]
            if delta <= 0:
                continue
            burned.append({"pid": int(pid), "name": info["name"],
                           "cpu_pct": _pct(delta, ticks_per_second * interval)})
        burned.sort(key=lambda item: item["cpu_pct"], reverse=True)
        out["processes"] = burned[:TOP_PROCESSES]
        return out


def verdict(snapshot: dict, service: Optional[dict] = None) -> dict:
    """One sentence: is this a bottleneck, and where?

    Deliberately narrow. Each branch names a bottleneck only when the number is
    unambiguous AND the alternative explanation is excluded — a load of 3.0 on
    four cores is not a problem, and a high CPU figure with a healthy event loop
    means the box is simply working, not stuck.
    """
    service = service or {}
    cpu = (snapshot.get("cpu") or {}).get("busy_pct")
    iowait = (snapshot.get("cpu") or {}).get("iowait_pct")
    disk_busy = (snapshot.get("disk") or {}).get("busy_pct")
    mem = (snapshot.get("mem") or {}).get("used_pct")
    load_per_core = snapshot.get("load_per_core")
    lag_ms = service.get("event_loop_lag_ms")
    cores = snapshot.get("cpus") or 0

    if not snapshot.get("available", True):
        return {"level": "unknown", "text": "正在采样，2 秒后出判断"}

    if lag_ms is not None and lag_ms >= 500:
        return {"level": "bad",
                "text": ("事件循环卡了 %d ms —— 服务端自己被堵住，不是 CPU 不够"
                         % round(lag_ms))}
    if cpu is not None and cpu >= 85:
        if iowait is not None and iowait >= 30:
            return {"level": "bad",
                    "text": ("CPU %d%%，其中 %d%% 在等磁盘 —— 瓶颈是 IO，不是 CPU"
                             % (round(cpu), round(iowait)))}
        return {"level": "bad", "text": "CPU %d%%（%d 核）—— 计算是瓶颈"
                % (round(cpu), cores)}
    if disk_busy is not None and disk_busy >= 90:
        return {"level": "warn",
                "text": "磁盘忙 %d%% —— 写入或交换把它占满了" % round(disk_busy)}
    if mem is not None and mem >= 92:
        return {"level": "warn", "text": "内存用了 %d%% —— 开始换页就会卡" % round(mem)}
    if load_per_core is not None and load_per_core >= 1.5:
        return {"level": "warn",
                "text": "每核负载 %.1f（%d 核）—— 有排队，但 CPU 还没满"
                % (load_per_core, cores)}
    if cpu is None:
        return {"level": "unknown", "text": "拿不到 CPU 数据（没有 /proc/stat？）"}
    return {"level": "ok",
            "text": "正常：CPU %d%%、负载 %.1f/核、内存 %s%%"
                    % (round(cpu),
                       load_per_core if load_per_core is not None else 0.0,
                       round(mem) if mem is not None else "?")}


def human_bytes(value: Optional[float]) -> str:
    """Compact units for a phone screen: 0 B, 812 B, 42.1 kB, 3.4 MB."""
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "kB", "MB", "GB"):
        if abs(size) < 1000 or unit == "GB":
            return ("%d %s" % (round(size), unit) if unit == "B"
                    else "%.1f %s" % (size, unit))
        size /= 1000.0
    return "%.1f GB" % size
