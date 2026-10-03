"""Cross-platform first-launch auto-configuration for MRRC Modern.

Runs in the launcher (macOS menu-bar app, Windows tray app) BEFORE the server
starts. Generates a random web password, scans for a serial port (macOS
/dev/cu.*, Windows COM ports, Linux /dev/tty*), and probes each candidate
port to identify the radio (FT-710 ASCII CAT vs Icom CI-V). Results are
written back to the user env file so the server always sees final config.

Imports only stdlib + pyserial on purpose: tests (and the Windows VM) do
not have rumps/PyObjC.
"""
from __future__ import annotations

import codecs
import locale
import os
import secrets
import serial
import serial.tools.list_ports
import sys
from pathlib import Path
from typing import Any, Callable

from config import default_baud_for

DEFAULT_WEB_PASSWORD = "changeme_please_use_strong_password!"
if sys.platform == "darwin":
    DEFAULT_SERIAL_PORTS = ("/dev/cu.SLAB_USBtoUART",)
elif os.name == "nt":
    DEFAULT_SERIAL_PORTS = ("COM3",)
else:
    DEFAULT_SERIAL_PORTS = ("/dev/ttyUSB0",)
RADIO_MODEL_FALLBACK = "ft710"
VALID_MODELS = frozenset({"ft710", "ic7300", "ic7300mk2"})


def needs_first_run(env: dict[str, str]) -> bool:
    """True when first-launch auto-config should run.

    Returns False (no re-probe) only when the config is genuinely settled:
    a non-default password, a valid radio model, and either the port was
    confirmed by a live probe (``MRRC_PORT_CONFIRMED=1``) or the port is a
    real non-default device name. Otherwise the launcher re-runs auto-config.
    """
    pwd = env.get("MRRC_WEB_PASSWORD", "")
    port = env.get("MRRC_SERIAL_PORT", "").strip()
    model = env.get("MRRC_RADIO_MODEL", "").strip().lower()
    configured = (
        pwd and pwd != DEFAULT_WEB_PASSWORD
        and model in VALID_MODELS
        and (
            env.get("MRRC_PORT_CONFIRMED") == "1"
            or (port and port not in DEFAULT_SERIAL_PORTS)
        )
    )
    return not configured


def generate_password() -> str:
    return secrets.token_urlsafe(16)


# 会“应答 open() 然后永远吞掉写入”的口。2026-10-03 实测："Intel(R) Active Management
# Technology - SOL" 把一个 launcher 卡在 probe_ft710() 的 write 里数小时。
# 它本身是 PCI 设备，hwid 那一关也会拦住——这里当兼保险，因为卡在这里的代价是整次安装
# （没有服务、没有证书、没有日志、控制台全黑）。
NEVER_PROBE_KEYWORDS = (
    "active management technology",
    "amt - sol",
    "intel(r) active management",
)


def _candidates(ports: list) -> list:
    """平台过滤：macOS 只取 /dev/cu.*，其他平台取“值得探测”的串口。"""
    if sys.platform == "darwin":
        bogus = ("/dev/cu.Bluetooth", "/dev/cu.IRComm", "/dev/cu.debug-console")
        return [
            p for p in ports
            if p.device.startswith("/dev/cu.") and not p.device.startswith(bogus)
        ]
    # Windows (COM*) / Linux (/dev/tty*)：电台一定是 USB 转串口，所以系统描述成 PCI 设备的、
    # 或命中上面那几个“永不探测”名字的，都不是它。
    out = []
    for p in ports:
        if not p.device:
            continue
        text = f"{p.description or ''} {p.hwid or ''}".lower()
        if any(s in text for s in NEVER_PROBE_KEYWORDS):
            continue
        if (p.hwid or "").upper().startswith("PCI"):
            continue
        out.append(p)
    return out


def detect_serial_ports(comports: list | None = None) -> list[str]:
    """Return candidate serial ports, radio-ish ones (CP210x/SLAB/usbserial) first."""
    ports = list(serial.tools.list_ports.comports()) if comports is None else list(comports)
    candidates = _candidates(ports)

    def _key(p) -> tuple[int, str]:
        text = ((p.description or "") + " " + (p.hwid or "")).lower()
        # 2026-10-03 实测：电台自报的描述是 "Silicon Labs Dual CP2105 USB to UART Bridge" ——
        # 它既不包含 "cp210x"（型号是 cp2105，末位不是 x），也不包含 "slab"
        # （"silicon labs" 里没有 slab 这个子串），于是这里想要的“电台口优先”静默失效，
        # 候选保持注册表顺序，**第一个被探测的就是那台机器的 Intel AMT SOL 口**。
        radio_ish = ("cp210", "silicon labs", "slab", "usbserial", "usb serial", "usb-serial",
                     "ftdi", "prolific", "ch340", "usb vid:pid", "usb\\vid_")
        if any(s in text for s in radio_ish):
            return (0, p.device)
        return (1, p.device)

    candidates.sort(key=_key)
    return [p.device for p in candidates]


def probe_ft710(ser) -> bool:
    """True if the device answers the Yaesu ASCII ``ID;`` query."""
    ser.reset_input_buffer()
    ser.write(b"AI0;")          # stop any Auto-Information streaming
    ser.timeout = 0.3
    ser.read(256)               # drain stale stream
    ser.reset_input_buffer()
    ser.write(b"ID;")
    ser.timeout = 1.0
    return ser.read(64).startswith(b"ID")


def probe_ic7300(ser) -> bool:
    """True if the device answers the Icom CI-V 0x19 model query."""
    ser.reset_input_buffer()
    # FE FE <to=0x00 broadcast> <from=0xE0 PC> 19 <cksum=0x00^0xE0^0x19=0xF9> FD
    frame = bytes([0xFE, 0xFE, 0x00, 0xE0, 0x19, 0xF9, 0xFD])
    ser.write(frame)
    ser.timeout = 1.0
    resp = ser.read(64)
    return resp.startswith(b"\xfe\xfe") and len(resp) >= 6 and resp[4] == 0x19


def probe_radio_model(
    port: str,
    open_func: Callable[..., Any] = serial.Serial,
    timeout: float = 1.0,
    write_timeout: float = 1.0,
) -> str | None:
    """Try FT-710 then IC-7300 on ``port``; return model or None.

    ``write_timeout`` 不是装饰：pyserial 的默认值是 None，意思是**写入永远阻塞**。
    2026-10-03 实测 — 一台机器的 "Intel(R) Active Management Technology - SOL" 口 open 正常、
    但一个字节也写不进去；launcher 的首次探测就坐在那次 write 里数小时，控制台一行输出都没有
    （所有 print 都在探测之后），launcher.log 也没有（卡住不是异常，report_fatal 不会触发）。
    给了超时之后它会变成 SerialTimeoutException，而调用方本来就把它当“这个口不行”跳过。
    """
    for baud, probe in ((38400, probe_ft710), (115200, probe_ic7300)):
        try:
            ser = open_func(port=port, baudrate=baud, timeout=timeout,
                            write_timeout=write_timeout)
        except Exception:
            continue
        try:
            if probe(ser):
                return "ft710" if probe is probe_ft710 else "ic7300"
        except Exception:
            continue
        finally:
            try:
                ser.close()
            except Exception:
                pass
    return None


def _bom_encodings() -> tuple[tuple[bytes, str], ...]:
    """BOMs a text editor may have written (UTF-32 first: its BOM prefixes UTF-16's)."""
    return (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    )


def read_env_text(path: Path) -> tuple[str, str]:
    """Read a user-editable text file without ever raising on its encoding.

    Returns ``(text, encoding)`` where ``encoding`` is ``"utf-8"`` for a clean
    file and the fallback that was used otherwise, so callers can say so.

    This file is edited by hand, on machines whose ANSI code page is not UTF-8.
    A zh-CN editor turned the shipped template's em-dash (``e2 80 94``) into
    ``e2 80 3f``; the strict UTF-8 read then raised before the launcher could
    print anything, the console window closed instantly, and the operator could
    only report "I installed it and it will not run" (field report 2026-09-12,
    Win11 VM).  A config file must never be able to do that.
    """
    raw = path.read_bytes()
    for bom, encoding in _bom_encodings():
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace"), encoding
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    # Not UTF-8: the user's editor re-encoded it.  Prefer the local code page
    # (so Chinese device names survive), then GBK explicitly, then a codec that
    # cannot fail — KEY=VALUE lines are ASCII and still parse.
    for encoding in ("cp936", locale.getpreferredencoding(False), "latin-1"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        print(f"Warning: {path} is not valid UTF-8; reading it as {encoding}. "
              f"Re-save it as UTF-8 (any edit from the launcher does that).",
              file=sys.stderr)
        return text, encoding
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Rewrite KEY=VALUE lines in place, preserving comments and appending new keys.

    Always writes UTF-8: a config that arrived from an ANSI (GBK) editor is
    normalised by the first write (see read_env_text for why that matters).
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    lines = read_env_text(path)[0].splitlines()
    pending = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in pending:
                out.append(f"{key}={pending.pop(key)}")
                continue
        out.append(line)
    for key, value in pending.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def apply_first_run(
    env: dict[str, str],
    config_path: Path,
    *,
    open_func: Callable[..., Any] = serial.Serial,
) -> dict[str, str]:
    """Fill password/serial/model, persist, mark done. Returns updated env."""
    updates: dict[str, str] = {}

    pwd = env.get("MRRC_WEB_PASSWORD", "")
    if not pwd or pwd == DEFAULT_WEB_PASSWORD:
        pwd = generate_password()
        env["MRRC_WEB_PASSWORD"] = pwd
        updates["MRRC_WEB_PASSWORD"] = pwd
        env["MRRC_AUTO_PASSWORD"] = "1"
        updates["MRRC_AUTO_PASSWORD"] = "1"
        # 立刻落盘，而不是等最后那次批量写入。2026-10-03 实测：串口探测再也没返回，
        # 留给运维的配置是原封不动的模板 —— 口令是空的、也没有 MRRC_FIRST_RUN_DONE，
        # 因为那个口令只存在于内存里。
        update_env_file(config_path, {"MRRC_WEB_PASSWORD": pwd, "MRRC_AUTO_PASSWORD": "1"})

    port = env.get("MRRC_SERIAL_PORT", "").strip()
    model = env.get("MRRC_RADIO_MODEL", "").strip().lower()
    port_unset = not port or port in DEFAULT_SERIAL_PORTS
    model_unset = model not in VALID_MODELS

    if port_unset or model_unset:
        ports = detect_serial_ports()
        # 探测之前先出声：这个循环要花几秒到几十秒，而 launcher 里其他 print 全在它后面，
        # 所以这里一卡住就是“全黑窗口、毫无线索”（现场报障的原话就是黑屏）。
        print("Detecting the radio on %d serial port(s): %s"
              % (len(ports), ", ".join(ports) or "(none found)"), flush=True)
        found: tuple[str, str] | None = None
        for candidate in ports:
            found_model = probe_radio_model(candidate, open_func=open_func)
            if found_model:
                found = (candidate, found_model)
                break
        if found is not None:
            port, model = found
            env["MRRC_PORT_CONFIRMED"] = "1"
            updates["MRRC_PORT_CONFIRMED"] = "1"
        else:
            if port_unset:
                port = ports[0] if ports else port
            if model_unset:
                model = RADIO_MODEL_FALLBACK

    env["MRRC_SERIAL_PORT"] = port
    env["MRRC_RADIO_MODEL"] = model
    updates["MRRC_SERIAL_PORT"] = port
    updates["MRRC_RADIO_MODEL"] = model
    env["MRRC_FIRST_RUN_DONE"] = "1"
    updates["MRRC_FIRST_RUN_DONE"] = "1"

    # V2.33: legacy installer templates pre-filled MRRC_BAUD_RATE=38400
    # (the FT-710 value) regardless of model, so a discovered IC-7300 kept
    # the stale value and its CI-V scope stream (which requires 115200)
    # never came up (field log 2026-09-10: scope stalled → S-meter
    # fallback). Align the baud with the discovered model, but never
    # override an explicitly customized value (anything other than the
    # template default is treated as operator intent).
    stored_baud = str(env.get("MRRC_BAUD_RATE", "")).strip()
    if stored_baud in ("", "38400"):
        env["MRRC_BAUD_RATE"] = str(default_baud_for(model))
        updates["MRRC_BAUD_RATE"] = env["MRRC_BAUD_RATE"]

    update_env_file(config_path, updates)
    return env
