"""macOS first-launch auto-configuration for MRRC Modern.

Runs in the menu-bar launcher BEFORE the server starts. Generates a random
web password, scans /dev/cu.* for a serial port, and probes each candidate
port to identify the radio (FT-710 ASCII CAT vs Icom CI-V). Results are
written back to the user env file so the server always sees final config.

Imports only stdlib + pyserial on purpose: tests (and the Windows VM) do
not have rumps/PyObjC.
"""
from __future__ import annotations

import secrets
import serial
import serial.tools.list_ports
from pathlib import Path

DEFAULT_WEB_PASSWORD = "changeme_please_use_strong_password!"
DEFAULT_SERIAL_PORTS = ("/dev/cu.SLAB_USBtoUART",)
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


def detect_serial_ports(comports: list | None = None) -> list[str]:
    """Return /dev/cu.* devices, CP210x/SLAB/usbserial radios first.

    Non-serial bogus ports (Bluetooth, IRComm, debug-console) are excluded so
    they can never be picked as the radio port.
    """
    ports = list(serial.tools.list_ports.comports()) if comports is None else list(comports)
    _bogus = ("/dev/cu.Bluetooth", "/dev/cu.IRComm", "/dev/cu.debug-console")
    candidates = [
        p for p in ports
        if p.device.startswith("/dev/cu.") and not p.device.startswith(_bogus)
    ]

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


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Rewrite KEY=VALUE lines in place, preserving comments and appending new keys."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    lines = path.read_text(encoding="utf-8").splitlines()
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
    open_func=serial.Serial,
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

    port = env.get("MRRC_SERIAL_PORT", "").strip()
    model = env.get("MRRC_RADIO_MODEL", "").strip().lower()
    port_unset = not port or port in DEFAULT_SERIAL_PORTS
    model_unset = model not in VALID_MODELS

    if port_unset or model_unset:
        ports = detect_serial_ports()
        found_port, found_model = None, None
        for candidate in ports:
            found_model = probe_radio_model(candidate, open_func=open_func)
            if found_model:
                found_port = candidate
                break
        if found_model:
            port, model = found_port, found_model
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
