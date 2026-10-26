"""First-launch auto-configuration for the Raspberry Pi image (systemd mrrc-firstboot).

Ported from macos/first_run.py. Runs as root BEFORE the server starts on first boot; Generates a random web password, scans for a serial port (macOS
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

DEFAULT_WEB_PASSWORD = "changeme_please_use_strong_password!"
# Linux (Raspberry Pi): CP210x/CI-V ports enumerate under /dev/ttyUSB*|/dev/ttyACM*.
DEFAULT_SERIAL_PORTS = ("/dev/ttyUSB0",)
RADIO_MODEL_FALLBACK = "ft710"
VALID_MODELS = frozenset({"ft710", "ic7300", "ic7300mk2"})


def needs_first_run(env: dict[str, str], port_exists=None) -> bool:
    """True when first-launch auto-config should run.

    Returns False (no re-probe) only when the config is genuinely settled:
    a non-default password, a valid radio model, either the port was confirmed
    by a live probe (``MRRC_PORT_CONFIRMED=1``) or it is a real non-default
    device name, **and the device that name points at is on this machine right
    now**. That last clause is what makes a shipped box self-heal: the seller's
    bench and the customer's desk enumerate the FT-710's two CP210x ports in
    their own order, so a recorded name alone is not evidence.

    ``port_exists`` is injectable because this function is otherwise pure and
    the tests must not depend on a /dev node being present on the build host.
    """
    pwd = env.get("MRRC_WEB_PASSWORD", "")
    port = env.get("MRRC_SERIAL_PORT", "").strip()
    model = env.get("MRRC_RADIO_MODEL", "").strip().lower()
    exists = port_exists or port_node_exists
    configured = (
        pwd and pwd != DEFAULT_WEB_PASSWORD
        and model in VALID_MODELS
        and (
            env.get("MRRC_PORT_CONFIRMED") == "1"
            or (port and port not in DEFAULT_SERIAL_PORTS)
        )
        and exists(port)
    )
    return not configured


def port_node_exists(port: str) -> bool:
    """Whether this machine currently has the device the port name points at.

    Only POSIX device paths are ever *claimed absent*. A Windows port name
    ("COM5") is not a filesystem path at all, and "I cannot tell" has to read as
    present here — reading it as absent would make every Windows launch re-probe
    the radio and rewrite the env file.
    """
    if not port:
        return False
    if not port.startswith("/"):
        return True
    return os.path.exists(port)


def generate_password() -> str:
    return secrets.token_urlsafe(16)


def _candidates(ports: list) -> list:
    """Linux filter: USB serial ports only; drop onboard UARTs and pseudo-devices."""
    import re
    return [
        p for p in ports
        if p.device and re.match(r"^/dev/(ttyUSB|ttyACM)\d+$", p.device)
    ]


def detect_serial_ports(comports: list | None = None) -> list[str]:
    """Return candidate serial ports, radio-ish ones (CP210x/SLAB/usbserial) first."""
    ports = list(serial.tools.list_ports.comports()) if comports is None else list(comports)
    candidates = _candidates(ports)

    def _key(p) -> tuple[int, str]:
        text = (p.description + " " + p.hwid).lower()
        if any(s in text for s in ("cp210x", "slab", "usbserial", "usb serial", "usb-serial")):
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


def probe_radio_model(port: str, open_func=serial.Serial, timeout: float = 1.0) -> str | None:
    """Try FT-710 then IC-7300 on ``port``; return model or None."""
    for baud, probe in ((38400, probe_ft710), (115200, probe_ic7300)):
        try:
            ser = open_func(port=port, baudrate=baud, timeout=timeout)
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

    On the Pi this file arrives as the **preseed**: written on the operator's
    own PC and copied into /boot/firmware/ with the SD card, so a non-UTF-8
    save (an ANSI/GBK editor turns the template's em-dash ``e2 80 94`` into
    ``e2 80 3f``) is the likeliest encoding to show up.  A strict UTF-8 read
    made mrrc-firstboot.service fail on the very first boot — the same field
    bug as the Windows/macOS launchers (2026-09-12).  Keep in sync with
    macos/first_run.py.
    """
    raw = path.read_bytes()
    for bom, encoding in _bom_encodings():
        if raw.startswith(bom):
            return raw.decode(encoding, errors="replace"), encoding
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    # Not UTF-8: prefer the local code page (so Chinese device names survive),
    # then GBK explicitly, then a codec that cannot fail — KEY=VALUE lines are
    # ASCII and still parse.
    for encoding in ("cp936", locale.getpreferredencoding(False), "latin-1"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        print(f"Warning: {path} is not valid UTF-8; reading it as {encoding}. "
              f"Re-save it as UTF-8 (any edit from MRRC does that).",
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
    open_func=serial.Serial,
    preseeded: bool = False,
) -> dict[str, str]:
    """Fill password/serial/model, persist, mark done. Returns updated env.

    ``preseeded`` (MRRC_PRESEED=1) keeps the password decision outside this
    function — the operator or the image already made it. Everything else still
    runs: the probe is the one thing only this machine can do.
    """
    updates: dict[str, str] = {}

    pwd = env.get("MRRC_WEB_PASSWORD", "")
    if not preseeded and (not pwd or pwd == DEFAULT_WEB_PASSWORD):
        pwd = generate_password()
        env["MRRC_WEB_PASSWORD"] = pwd
        updates["MRRC_WEB_PASSWORD"] = pwd
        env["MRRC_AUTO_PASSWORD"] = "1"
        updates["MRRC_AUTO_PASSWORD"] = "1"
        # 立刻落盘，而不是等最后那次批量写入。2026-10-03 实测（macOS launcher）：
        # 串口探测再也没返回，留给运维的配置是原封不动的模板 —— 口令只在内存里，
        # 随后一起消失。Linux 这一份是它的移植，当初没带上这一步；2026-10-17 补齐，
        # 因为盒子正是最容易碰上“某个串口桥只吃写、不回话”的那台机器。
        update_env_file(config_path, {"MRRC_WEB_PASSWORD": pwd, "MRRC_AUTO_PASSWORD": "1"})

    port = env.get("MRRC_SERIAL_PORT", "").strip()
    model = env.get("MRRC_RADIO_MODEL", "").strip().lower()
    port_unset = not port or port in DEFAULT_SERIAL_PORTS
    model_unset = model not in VALID_MODELS

    if port_unset or model_unset:
        ports = detect_serial_ports()
        # 探测之前先出声：这个循环要几秒到几十秒，而首启横幅在它后面 —— 卡住时
        # HDMI 上就是全部黑的，现场没有任何线索（macOS 那边的原话就是“黑屏”）。
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

    update_env_file(config_path, updates)
    return env


def main() -> int:
    """Entry point for mrrc-firstboot.service (runs as root).

    ``MRRC_PRESEED=1`` means "this env file is authoritative — do not invent a
    password", and nothing more. It used to return before the probe, and the
    wrapper passes it on every image whose env file exists (i.e. every image
    install.sh built) — so the serial probe never ran on a real box (2026-10-17
    artifact check: env/mrrc.env present, no MRRC_WEB_PASSWORD, no
    MRRC_SERIAL_PORT, no firstboot-done).
    """
    env_path = Path(os.environ.get("MRRC_ENV_FILE", "/opt/mrrc_modern/env/mrrc.env"))
    preseeded = os.environ.get("MRRC_PRESEED") == "1"
    env = dict(
        line.split("=", 1) for line in read_env_text(env_path)[0].splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    )
    if not needs_first_run(env):
        print("[mrrc-firstboot] config settled; skipping auto-config")
        return 0
    before = env.get("MRRC_WEB_PASSWORD", "")
    env = apply_first_run(env, env_path, preseeded=preseeded)
    if env.get("MRRC_WEB_PASSWORD", "") == before:
        # 预置口令的镜像：控制台只报探测结果，不重复口令（它本来就是随镜像公开的，
        # 但那是随镜像公开的，不是这条日志的功劳）。
        print(f"[mrrc-firstboot] radio probe: model {env['MRRC_RADIO_MODEL']} "
              f"port {env['MRRC_SERIAL_PORT'] or '(none found)'}")
        return 0
    print("=" * 56)
    print("  MRRC Modern 首次配置完成")
    print(f"  网页密码: {env['MRRC_WEB_PASSWORD']}")
    print(f"  电台型号: {env['MRRC_RADIO_MODEL']}  串口: {env['MRRC_SERIAL_PORT']}")
    print("  浏览器打开: https://raspberrypi.local:8888")
    print("  (本提示也会出现在 SSH 登录 motd; sudo mrrc-show-password 可随时查看)")
    print("=" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
