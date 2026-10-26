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

# ``env_store`` is the one writer of the env file (design D-8) and lives at the
# tree root. On the device this file is executed by path (cwd=/opt/mrrc_modern),
# so sys.path[0] is this directory, not the tree — hence the explicit insert,
# the same way linux/mrrc_radio.py resolves the tree it imports from.
_TREE = Path(__file__).resolve().parents[1]
if (_TREE / "env_store.py").is_file() and str(_TREE) not in sys.path:
    sys.path.insert(0, str(_TREE))
import env_store  # noqa: E402  (path assembled above)
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


def registered_models() -> frozenset[str]:
    """Every radio model this build can actually drive (the backend registry).

    这个白名单以前是手写的三个机型。后果不是“少识别一个型号”那么轻：
    `needs_first_run()` 把白名单当成“已配置”的判据，于是任何后来加入的机型
    （FT-891 / FTX-1 / FTDX10 / IC-705 / 7610 / 7760）每次启动都被当成“未配置”，
    重新跑一次串口探测；而 `probe_ft710()` 只验应答前缀（`ID0680;` 也以 `ID` 开头），
    于是任何 Yaesu 电台都被写成 ft710，用户自己选的机型被静默改掉。

    2026-10-21 现场（包 20261021-123324-81ab）：一台 FT-891 在 12:27 保存了
    `model=ft891`，12:30 启动又是 `Radio model: ft710`，一直用 FT-710 后端驱动。
    FTX-1 早在 2026-10-17 就报过三次同一个缺陷。

    注册表是唯一真相源；静态集合只在启动器被打包成“没有 backends 树”时兜底。
    """
    try:
        from backends import known_models
        models = {str(m).strip().lower() for m in known_models()}
        models.discard("")
        if models:
            return frozenset(models)
    except Exception:            # noqa: BLE001 - 启动器绝不因为注册表导入失败而不启动
        pass
    return frozenset({"ft710", "ic7300", "ic7300mk2"})


VALID_MODELS = registered_models()


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


# One writer, not two (design D-8). This module used to carry its own copy, and
# the two disagreed exactly where it hurt: both rewrote only the **first** match
# for a key while every reader (`env_store.parse_env_text`, systemd's
# `EnvironmentFile`) takes the **last** — so a file carrying a duplicate
# `MRRC_SERIAL_PORT` (the field file of 2026-10-26, W103D + IC-7300) accepted six
# saves of `/dev/ttyUSB0` from the connection dialog and still started with an
# empty port; 26 days of logs, not one CI-V port opened. Aliasing also gets the
# file lock and the preserved `0640` mode without a second implementation to
# keep in step. Keep the name exported: server.py patches it by name.
update_env_file = env_store.update_env_file


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
            port, probed_model = found
            env["MRRC_PORT_CONFIRMED"] = "1"
            updates["MRRC_PORT_CONFIRMED"] = "1"
            # 只有“用户还没选过机型”时才采纳探测结果。探测本身分不出机型：
            # `probe_ft710()` 只看应答前缀，FT-891 回的 `ID0680;` 也匹配，
            # 已选 ft891 的机器会被改回 ft710（2026-10-21 现场）。
            if model_unset:
                model = probed_model
            else:
                print("Radio answers like %s on %s — keeping the configured model %s"
                      % (probed_model, port, model), flush=True)
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
