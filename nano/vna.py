"""NanoVNA-H 的串口协议与设备对象 —— 全仓唯一碰这台仪器串口的模块。

契约来源：``hugen79/NanoVNA-H`` 的 ``main.c``（``cmd_scan`` / ``cmd_data`` /
``cmd_reset``），并与真机 1.2.14 双向核对（spec §1.2 记了实测值与帧布局）：

* 命令以 ``\\r`` 结尾，提示符 ``ch> ``；
* ``scan_bin <start> <stop> <points> <mask>`` 一次扫完直接吐二进制：
  ``u16 mask, u16 points``，随后每点 ``u32 freq`` + 每个启用数组各 ``f32 re/im``；
* ``data <0..6>`` 里**没有频率**（0=S11、1=S21、2..6=标定数组），频率只在
  ``frequencies`` 与 ``scan`` 的输出里。

帧长必须精确匹配。半截数据会画出一条**看起来正常**的假曲线 —— 那比报错危险得多，
所以长度不对就整帧丢弃重扫，绝不半截解析。
"""
from __future__ import annotations

import glob
import logging
import os
import re
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Sequence

log = logging.getLogger(__name__)

MASK_FREQ = 0x01
MASK_S11 = 0x02
MASK_S21 = 0x04
MASK_BINARY = 0x80

POINTS_ALLOWED = (101, 201, 401)      # 固件 POINTS_SET 的档位
FREQ_MIN_HZ = 50_000
FREQ_MAX_HZ = 900_000_000

NANO_VID_PID = ("0483", "5740")       # ChibiOS CDC-ACM（实测）
DFU_VID_PID = ("0483", "df11")        # ROM bootloader
PROMPT = b"ch> "
VERSION_RE = re.compile(rb"^\s*(\d+\.\d+(?:\.\d+)?)\s*(?:ch>)?\s*$", re.M)

# 允许从页面上改的设置（spec §4.8）。其余一律不在白名单里 —— 尤其是 tcxo：
# 那是整台仪器的频率基准，手滑一下全段偏移，且从网页上看不出错在哪。
SETTING_LIMITS = {
    "smooth": (0, 8),
    "power": (0, 3),
    "offset": (-1_000_000, 1_000_000),
    "s21offset": (-100.0, 100.0),
    "bandwidth": (0, 10),
}
POWER_AUTO = 255

# `config <flag> <0|1>` 改的是设备上的显示/行为开关（固件 VNA_MODE 位）。
# **`connection` 永久排除**：它把 shell 切到串口控制台，USB 侧当场失联，只能
# 到设备上按回来 —— 这是唯一一个"从网页上按一下就再见"的开关。
CONFIG_FLAGS = {
    "auto": "文件自动命名",
    "avg": "平滑取算术平均（否则几何平均）",
    "mode": "标记搜索：最大 / 最小",
    "grid": "网格显示数值",
    "dot": "网格用点线",
    "bk": "记住关机前状态",
    "flip": "翻转屏幕",
    "separator": "小数分隔符（逗号 / 点）",
}
FORBIDDEN_CONFIG_FLAGS = {"connection", "tif"}

# `trace <n> <type>` 接受的格式名（固件 cmd_type_list 原文）。
TRACE_TYPES = (
    "logmag", "phase", "delay", "smith", "polar", "linear", "swr", "real",
    "imag", "r", "x", "z", "zp", "g", "b", "y", "rp", "xp", "cs", "ls",
    "cp", "lp", "q", "rser", "xser", "zser", "rsh", "xsh", "zsh", "q21",
)


class NanoError(Exception):
    """NanoVNA 通信或协议层的失败。"""


class FrameError(NanoError):
    """二进制帧不完整或有多余字节 —— 整帧丢弃。"""


class PortConflict(NanoError):
    """选中的串口正是电台 CAT 的口 —— 宁可不工作，也不抢它。"""


@dataclass
class DeviceInfo:
    board: str = ""
    version: str = ""
    build: str = ""
    platform: str = ""
    points_max: int = 0


@dataclass
class Sweep:
    freqs: list                       # list[int] Hz
    s11: list                         # list[tuple[float, float]]
    s21: Optional[list] = None        # list[tuple[float, float]] | None
    elapsed_ms: int = 0


# ── 编解码（纯函数）────────────────────────────────────────────────

def scan_command(start: int, stop: int, points: int, s21: bool = False) -> str:
    """``scan_bin`` 命令行。掩码**不含** 8/16/32：要标定、要电延迟补偿。"""
    if not (FREQ_MIN_HZ <= int(start) < int(stop) <= FREQ_MAX_HZ):
        raise ValueError("start/stop out of range: %r %r" % (start, stop))
    if int(points) not in POINTS_ALLOWED:
        raise ValueError("points must be one of %r" % (POINTS_ALLOWED,))
    mask = MASK_BINARY | MASK_FREQ | MASK_S11 | (MASK_S21 if s21 else 0)
    return "scan_bin %d %d %d %d" % (start, stop, points, mask)


def parse_info(text: str) -> DeviceInfo:
    """``info`` 的文本 → 结构化。真机样本见 tests/test_nano_vna.py。"""
    info = DeviceInfo()
    m = re.search(r"^Board:\s*(.+)$", text, re.M)
    if m:
        info.board = m.group(1).strip()
    m = re.search(r"^Version:\s*(\S+)\s*\[p:(\d+)", text, re.M)
    if m:
        info.version = m.group(1)
        info.points_max = int(m.group(2))
    else:
        m = re.search(r"^Version:\s*(\S+)", text, re.M)
        if m:
            info.version = m.group(1)
    m = re.search(r"^Build Time:\s*(.+)$", text, re.M)
    if m:
        info.build = m.group(1).strip()
    m = re.search(r"^Platform:\s*(.+)$", text, re.M)
    if m:
        info.platform = m.group(1).strip()
    return info


def parse_version(raw: bytes) -> str:
    """``version`` 的回复正文 → ``1.2.14``。认不出来就当没连上。"""
    m = VERSION_RE.search(raw if isinstance(raw, bytes) else raw.encode())
    if not m:
        raise NanoError("unrecognised version reply: %r" % (raw,))
    return m.group(1).decode()


def parse_vbat(text: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*mV", text)
    return int(m.group(1)) if m else None


def parse_frequencies(text: str) -> list[int]:
    return [int(tok) for tok in re.findall(r"^\s*(\d+)\s*$", text, re.M)]


def drop_echo_line(raw: bytes) -> bytes:
    """去掉命令回显那一行，保留其后的全部正文。

    `data 0` 的回复是 101 行数字；把回显只去掉末尾的 CRLF 会让第一行变成
    ``data 0`` 这种既不是数字也不是空行的东西 —— 现场调试时它被当成"数据"喂给了
    float()。
    """
    nl = raw.find(b"\r\n")
    return raw[nl + 2:] if nl >= 0 else raw


def _strip_frame_wrapping(raw: bytes) -> bytes:
    """去掉命令回显行与提示符，留下正文。

    真机回复形状是 ``<echo>\\r\\n<body>ch> ``；也容忍提示符跑到正文前面的
    固件（两种都在现场见过，宁可都认，也不要因为一个空格误判成坏帧）。
    """
    nl = raw.find(b"\r\n")
    body = raw[nl + 2:] if nl >= 0 else raw
    if body.startswith(PROMPT):
        body = body[len(PROMPT):]
    if body.endswith(PROMPT):
        body = body[:-len(PROMPT)]
    return body


def frame_length(points: int, mask: int) -> int:
    stride = 4
    if mask & MASK_S11:
        stride += 8
    if mask & MASK_S21:
        stride += 8
    return 4 + points * stride


def parse_scan_bin(raw: bytes) -> Sweep:
    """二进制 ``scan_bin`` 回复 → :class:`Sweep`。长度不符即 :class:`FrameError`。"""
    body = _strip_frame_wrapping(raw)
    if len(body) < 4:
        raise FrameError("frame too short: %d bytes" % len(body))
    mask, points = struct.unpack("<HH", body[:4])
    if points <= 0:
        raise FrameError("frame declares %d points" % points)
    need = frame_length(points, mask)
    if len(body) != need:
        raise FrameError("frame length %d != expected %d (points=%d mask=0x%02x)"
                         % (len(body), need, points, mask))
    off = 4
    freqs: list[int] = []
    s11: list[tuple[float, float]] = []
    s21: list[tuple[float, float]] = []
    for _ in range(points):
        if mask & MASK_FREQ:
            (f,) = struct.unpack_from("<I", body, off)
            off += 4
            freqs.append(f)
        if mask & MASK_S11:
            s11.append(struct.unpack_from("<ff", body, off))
            off += 8
        if mask & MASK_S21:
            s21.append(struct.unpack_from("<ff", body, off))
            off += 8
    return Sweep(freqs=freqs, s11=s11, s21=(s21 if mask & MASK_S21 else None))


# ── 传输 ───────────────────────────────────────────────────────────

class Transport(Protocol):
    def request(self, cmd: str, timeout: float = 2.0) -> str: ...
    def request_binary(self, cmd: str, timeout: float = 4.0) -> bytes: ...
    def close(self) -> None: ...


class SerialTransport:
    """pyserial 之上的最小封装：一条命令一次往返。

    CDC-ACM 忽略波特率，仍写 115200（pyserial 要求一个值）。

    两条纪律（2026-10-18 真机上撞出来的，见下）：

    * **发命令前先与提示符同步**。直接 `reset_input_buffer()` 就写，会在设备还在
      吐上一条回复时把一个新命令行塞进去 —— 实测结果是 `scan_bin` 有时**一个字节
      都不回**（不是回错，是不回），重试才成功。
    * **二进制帧按长度读**，不按"静默多久"判断收尾。设备在 401 点时扫 1.26 s，
      数据也是分段到的；用空闲判断会在段间截断，得到一个长度不对的帧 —— 而
      长度不对的帧正是本模块**必须**整帧拒绝的东西。
    """

    def __init__(self, port: str, baudrate: int = 115200, io_timeout: float = 0.15):
        import serial                                     # 延迟导入：测试不需要真的串口

        self.port = port
        self._ser = serial.Serial(port, baudrate=baudrate, timeout=io_timeout)

    def _sync_prompt(self, timeout: float = 0.6) -> None:
        """等到设备空闲（提示符 `ch> ` 收尾）再发下一条命令。

        `in_waiting` 先看一眼：**没人在说话就立刻走**。第一版无条件读到超时，
        每条命令白等 0.6 s —— 一次扫描因此从 0.32 s 变成 1.05 s。
        """
        if not self._ser.in_waiting:
            return
        deadline = time.monotonic() + timeout
        buf = bytearray()
        empty = 0
        while time.monotonic() < deadline:
            chunk = self._ser.read(64)
            if chunk:
                empty = 0
                buf += chunk
                if buf.endswith(PROMPT):
                    return
                continue
            empty += 1
            if empty >= 2:
                break
        if buf:
            log.debug("nano: prompt sync saw %r", bytes(buf[-16:]))

    def _read_until_newline(self, deadline: float) -> bytes:
        """吃掉命令回显：读到第一个 \r\n 为止。"""
        buf = bytearray()
        while b"\r\n" not in buf and time.monotonic() < deadline:
            chunk = self._ser.read(64)
            if chunk:
                buf += chunk
        return bytes(buf)

    def _drain_quiet(self, seconds: float = 0.02) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self._ser.read(256):
                return

    def request(self, cmd: str, timeout: float = 2.0) -> str:
        deadline = time.monotonic() + timeout
        self._sync_prompt()
        self._ser.write(cmd.encode() + b"\r")
        buf = bytearray(drop_echo_line(self._read_until_newline(deadline)))
        while time.monotonic() < deadline:
            chunk = self._ser.read(4096)
            if chunk:
                buf += chunk
                continue
            if buf.endswith(PROMPT) or (len(buf) and time.monotonic() > deadline - 0.05):
                break
            if buf:
                break                                     # 一拍静默即视为收尾
        body = bytes(buf)
        if body.endswith(PROMPT):
            body = body[:-len(PROMPT)]
        return body.decode("utf-8", "replace")

    def request_binary(self, cmd: str, timeout: float = 4.0) -> bytes:
        deadline = time.monotonic() + timeout
        self._sync_prompt()
        self._ser.write(cmd.encode() + b"\r")
        self._read_until_newline(deadline)                # 回显行

        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self._ser.read(4096)
            if chunk:
                buf += chunk
            body = bytes(buf)
            if body.startswith(PROMPT):
                body = body[len(PROMPT):]
            if len(body) >= 4:
                mask, points = struct.unpack("<HH", body[:4])
                if 0 < points <= 1024 and (mask & MASK_BINARY):
                    need = frame_length(points, mask)
                    if len(body) >= need:
                        self._drain_quiet()
                        return body[:need]
        return bytes(buf)

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:                                 # noqa: BLE001
            pass


# ── 设备 ───────────────────────────────────────────────────────────

class NanoVna:
    """一台 NanoVNA。``transport`` 可注入 —— 测试用假传输，不碰硬件。"""

    def __init__(self, port: str, transport: Optional[Transport] = None):
        self.port = port
        self._t = transport
        self.info_cache: Optional[DeviceInfo] = None

    # 生命周期 ----------------------------------------------------
    def open(self) -> None:
        if self._t is None:
            self._t = SerialTransport(self.port)
        self.version()                                    # 验身份：认不出就不是我们等的那台

    def close(self) -> None:
        if self._t is not None:
            self._t.close()
            self._t = None

    def _ask(self, cmd: str, timeout: float = 2.0) -> str:
        if self._t is None:
            raise NanoError("device not open")
        return self._t.request(cmd, timeout)

    # 身份与健康 --------------------------------------------------
    def version(self) -> str:
        return parse_version(self._ask("version").encode())

    def info(self) -> DeviceInfo:
        if self.info_cache is None:
            self.info_cache = parse_info(self._ask("info"))
        return self.info_cache

    def vbat_mv(self) -> Optional[int]:
        return parse_vbat(self._ask("vbat"))

    def cal_state(self) -> str:
        """``cal`` 的正文：哪几项标定是有效的。"""
        return self._ask("cal").strip()

    def frequencies(self) -> list[int]:
        return parse_frequencies(self._ask("frequencies", timeout=3.0))

    # 扫描 --------------------------------------------------------
    def scan(self, start: int, stop: int, points: int, s21: bool = False) -> Sweep:
        cmd = scan_command(start, stop, points, s21)
        timeout = 3.0 + points * 0.01
        last: Optional[FrameError] = None
        for attempt in (1, 2):
            t0 = time.monotonic()
            raw = self._t.request_binary(cmd, timeout)     # type: ignore[union-attr]
            try:
                sweep = parse_scan_bin(raw)
            except FrameError as exc:
                last = exc
                log.warning("nano: bad frame (attempt %d, %d bytes, head=%s): %s",
                            attempt, len(raw), raw[:24].hex(), exc)
                continue
            sweep.elapsed_ms = int((time.monotonic() - t0) * 1000)
            return sweep
        raise FrameError("two bad frames in a row: %s" % last)

    def restore_sweep(self, start: int, stop: int, points: int) -> bool:
        """把设备屏幕上的扫频范围还原成进来时的那一段。

        失败只记日志：本次数据已经拿到了，还原失败不该让用户以为测量没成功。
        """
        try:
            self._ask("sweep %d %d %d" % (start, stop, points), timeout=3.0 + points * 0.01)
            return True
        except Exception as exc:                           # noqa: BLE001
            log.warning("nano: restore sweep failed: %s", exc)
            return False

    # 设置 --------------------------------------------------------
    def read_settings(self) -> dict:
        out: dict = {}
        out["edelay_s11_ps"] = self._read_number("edelay s11")
        out["edelay_s21_ps"] = self._read_number("edelay s21")
        out["smooth"] = self._read_number("smooth")
        out["power"] = self._read_number("power")
        out["offset_hz"] = self._read_number("offset")
        out["s21offset_db"] = self._read_number("s21offset")
        bw = self._ask("bandwidth")
        m = re.search(r"bandwidth\s+(\d+)\s*\((\d+)\s*Hz\)", bw)
        out["bandwidth"] = int(m.group(1)) if m else None
        out["bandwidth_hz"] = int(m.group(2)) if m else None
        return out

    def _read_number(self, cmd: str) -> Optional[float]:
        try:
            text = self._ask(cmd)
        except Exception:                                  # noqa: BLE001
            return None
        nums = re.findall(r"-?\d+(?:\.\d+)?", text)
        return float(nums[-1]) if nums else None

    def set_setting(self, name: str, value) -> None:
        """白名单设置。名字或值不在名单里一律 ``ValueError``。"""
        if name in ("edelay_s11_ps", "edelay_s21_ps"):
            ch = "s11" if name.endswith("s11_ps") else "s21"
            self._ask("edelay %s %d" % (ch, int(float(value))))
            return
        if name == "power":
            v = int(value)
            if v != POWER_AUTO and not (SETTING_LIMITS["power"][0] <= v <= SETTING_LIMITS["power"][1]):
                raise ValueError("power out of range: %r" % (value,))
            self._ask("power %d" % v)
            return
        if name in ("smooth", "offset", "bandwidth", "s21offset"):
            lo, hi = SETTING_LIMITS[name]
            v = float(value) if name in ("offset", "s21offset") else int(value)
            if not (lo <= v <= hi):
                raise ValueError("%s out of range: %r" % (name, value))
            fmt = "%s %d" if name in ("smooth", "offset", "bandwidth") else "%s %f"
            self._ask(fmt % (name, v))
            return
        raise ValueError("setting not allowed: %r" % (name,))

    def set_config_flag(self, name: str, on) -> None:
        """设备显示开关（`config <flag> <0|1>`）。

        这些开关**读不回来**（固件没有对应查询命令），所以页面显示的是我们自己
        记下的意图，实际效果以设备屏幕为准。
        """
        if name in FORBIDDEN_CONFIG_FLAGS:
            raise ValueError("config flag not allowed: %r" % (name,))
        if name not in CONFIG_FLAGS:
            raise ValueError("unknown config flag: %r" % (name,))
        self._ask("config %s %d" % (name, 1 if on else 0))

    # 屏幕上画什么 --------------------------------------------------
    # `trace` 是固件里唯一"可读可写"的显示设置：格式、通道、刻度、参考位置。
    # 30 种格式里常用的那几个在这里就够了，其余照原样接受（页面按名字下发）。
    def read_traces(self) -> list:
        out = []
        for line in self._ask("trace").splitlines():
            parts = line.split()
            if len(parts) >= 3:
                out.append({"id": int(parts[0]), "type": parts[1],
                            "channel": parts[2],
                            "scale": float(parts[3]) if len(parts) > 3 else None,
                            "refpos": float(parts[4]) if len(parts) > 4 else None})
        return out

    def set_trace_type(self, n: int, type_name: str, src: int = 0) -> None:
        if type_name not in TRACE_TYPES:
            raise ValueError("unknown trace type: %r" % (type_name,))
        if src not in (0, 1):
            raise ValueError("src must be 0 (S11) or 1 (S21)")
        self._ask("trace %d %s %d" % (int(n), type_name, int(src)))

    def set_trace_param(self, n: int, param: str, value: float) -> None:
        if param not in ("scale", "refpos"):
            raise ValueError("param must be scale|refpos")
        self._ask("trace %d %s %g" % (int(n), param, float(value)))

    def disable_trace(self, n: int) -> None:
        self._ask("trace %d off" % int(n))

    def read_markers(self) -> list:
        out = []
        for line in self._ask("marker").splitlines():
            parts = line.split()
            if len(parts) >= 3:
                out.append({"id": int(parts[0]), "index": int(parts[1]),
                            "freq_hz": int(parts[2])})
        return out

    def set_marker(self, on: bool) -> None:
        self._ask("marker %s" % ("on" if on else "off"))

    def set_marker_index(self, n: int, index: int) -> None:
        """把第 n 个标记移到扫描点 ``index``（屏幕上立刻可见）。"""
        self._ask("marker %d %d" % (int(n), int(index)))

    def save_config(self) -> None:
        """把当前设置（含标定）写进设备 flash —— 不写就只是 RAM 里的临时状态。"""
        self._ask("saveconfig", timeout=6.0)

    # 标定槽 ------------------------------------------------------
    def save_cal(self, slot: int) -> None:
        if not 0 <= int(slot) <= 4:
            raise ValueError("slot must be 0..4")
        self._ask("save %d" % int(slot))

    def recall_cal(self, slot: int) -> None:
        if not 0 <= int(slot) <= 4:
            raise ValueError("slot must be 0..4")
        self._ask("recall %d" % int(slot))

    # 升级入口（第二期用）----------------------------------------
    def enter_dfu(self) -> None:
        """跳进 STM32 ROM bootloader。设备随后重新枚举成 0483:df11。"""
        self._t.request("reset dfu", timeout=2.0)          # type: ignore[union-attr]


# ── 端口发现 ───────────────────────────────────────────────────────

def _same_device(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return a == b


def discover_port(env: Optional[dict] = None,
                  cat_port: Optional[str] = None) -> Optional[str]:
    """按优先级找这台仪器。

    ``MRRC_NANO_PORT`` → ``by-id`` 里名字带 ChibiOS/NanoVNA 的 → macOS 的
    ``cu.usbmodem*`` → ``/dev/ttyACM*``。**只看名字是不够的**（CH340 克隆板会
    伪装成 ttyUSB，FT-710 的 CAT 也是 usb serial），所以真正验身份的是
    ``NanoVna.open()`` 里那次 ``version``；这里只负责给出候选。
    """
    env = os.environ if env is None else env
    explicit = (env.get("MRRC_NANO_PORT") or "").strip()
    if explicit:
        return explicit

    candidates: list[str] = []
    by_id = Path("/dev/serial/by-id")
    if by_id.is_dir():
        for pattern in ("*ChibiOS*", "*NanoVNA*", "*nanovna*"):
            candidates += sorted(str(p) for p in by_id.glob(pattern))
    candidates += sorted(glob.glob("/dev/cu.usbmodem*"))
    candidates += sorted(glob.glob("/dev/ttyACM*"))

    for cand in candidates:
        if _same_device(cand, cat_port):
            log.warning("nano: %s is the radio CAT port, skipping", cand)
            continue
        return cand
    return None
