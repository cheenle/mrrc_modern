"""Station device configuration (HAMLIB rig + audio) — spec 2026-08-10, AD-008.

``data/device-config.json`` is the operator-editable station device config:
written by the ``/api/v1/devices`` endpoints, read at startup by the server
(audio device + rigctld port) and by ``restart.sh`` (rigctld launch params).
env vars remain the fallback when the file is absent, so existing deployments
keep working unchanged.  ``restart.sh`` stays the rigctld spawner (AD-008:
rigctld is the serial owner); this module never opens the serial device.
"""

from __future__ import annotations

import dataclasses
import glob
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger("mrrc-ft8.device-config")

DEFAULT_CONFIG_PATH = Path("data/device-config.json")

# Py>=3.13 pathlib.Path() is a flavor dispatcher keyed to the *current*
# os.name at call time, so snapshot the host's concrete class. Filesystem
# paths built here stay native even when os.name is faked (tests simulating
# Windows on POSIX with monkeypatched "nt" would otherwise yield WindowsPath
# objects whose .exists()/.parent/.mkdir cannot run on a POSIX host).
_NATIVE_PATH = type(Path("."))

# Curated hamlib rig models offered in the UI (rigctl -l, verified 2026-08-10).
CURATED_RIG_MODELS: list[tuple[int, str]] = [
    (1020, "Yaesu FT-817"),
    (1049, "Yaesu FT-710"),
    (3073, "Icom IC-7300"),
    (30003, "Icom IC-M710"),
]

BAUD_RATES: list[int] = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]

_CONFIG_KEYS = (
    "rig_model", "rig_device", "rig_baud", "rig_stop_bits", "rig_mode", "rigctld_port",
    "audio_device", "audio_in_device", "audio_out_device", "audio_in_channel",
)

# _ENV_KEY: env names restart.sh evals when launching rigctld (launch vars).
# _SOURCE_ENV: env names the server actually reads when connecting — the
# source detection in ``source_of`` must report against these, not the
# restart.sh vars. rigctld_port is the one divergence: the server connects
# via ``MRRC_FT8_RIGCTLD`` (host:port) while restart.sh is launched with
# ``MRRC_FT8_RIGCTLD_PORT`` (port only).
_ENV_KEY = {
    "rig_model": "MRRC_FT8_RIG_MODEL",
    "rig_device": "MRRC_FT8_RIG_DEVICE",
    "rig_baud": "MRRC_FT8_RIG_BAUD",
    "rig_stop_bits": "MRRC_FT8_RIG_STOP_BITS",
    "rig_mode": "MRRC_FT8_RIG_MODE",
    "rigctld_port": "MRRC_FT8_RIGCTLD_PORT",
    "audio_device": "MRRC_FT8_AUDIO_DEVICE",
    "audio_in_device": "MRRC_FT8_AUDIO_IN_DEVICE",
    "audio_out_device": "MRRC_FT8_AUDIO_OUT_DEVICE",
    "audio_in_channel": "MRRC_FT8_AUDIO_IN_CHANNEL",
}

_SOURCE_ENV = {
    "rig_model": "MRRC_FT8_RIG_MODEL",
    "rig_device": "MRRC_FT8_RIG_DEVICE",
    "rig_baud": "MRRC_FT8_RIG_BAUD",
    "rig_stop_bits": "MRRC_FT8_RIG_STOP_BITS",
    "rig_mode": "MRRC_FT8_RIG_MODE",
    "rigctld_port": "MRRC_FT8_RIGCTLD",  # server 实际连接所用 env（host:port）
    "audio_device": "MRRC_FT8_AUDIO_DEVICE",
    "audio_in_device": "MRRC_FT8_AUDIO_IN_DEVICE",
    "audio_out_device": "MRRC_FT8_AUDIO_OUT_DEVICE",
    "audio_in_channel": "MRRC_FT8_AUDIO_IN_CHANNEL",
}


def load_device_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any] | None:
    """Return the saved device config, or None when absent/corrupt."""

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {k: data[k] for k in _CONFIG_KEYS if k in data}


def save_device_config(cfg: dict[str, Any], path: str | Path = DEFAULT_CONFIG_PATH) -> None:
    """Atomic write (tmp + os.replace); only known keys are persisted."""

    payload = {k: cfg[k] for k in _CONFIG_KEYS if k in cfg and cfg[k] not in (None, "")}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="device-config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        log.error("device config write failed: %s", exc)
        raise


def shell_env_lines(cfg: dict[str, Any]) -> list[str]:
    """``KEY=value`` lines for restart.sh to ``eval`` (rigctld launch params)."""

    lines: list[str] = []
    for key, env in _ENV_KEY.items():
        if key in cfg and cfg[key] is not None:
            lines.append(f"{env}={cfg[key]!r}")
    return lines


def _audio_value(raw: str) -> int | str | None:
    """Parse an audio-device env value (numeric index or device name)."""

    return int(raw) if raw.isdigit() else raw


def _resolve_audio(
    file_cfg: dict[str, Any] | None, environ: Any, *, direction: str
) -> Any:
    """Effective audio device for one direction: file > env > legacy audio_device."""

    key = f"audio_{direction}_device"
    if file_cfg and key in file_cfg:
        return file_cfg[key]
    env_val = environ.get(f"MRRC_FT8_AUDIO_{direction.upper()}_DEVICE", "")
    if env_val:
        return _audio_value(env_val)
    legacy = file_cfg.get("audio_device") if file_cfg else None
    if legacy is not None:
        return legacy
    env_legacy = environ.get("MRRC_FT8_AUDIO_DEVICE", "")
    return _audio_value(env_legacy) if env_legacy else None


def effective_config(
    file_cfg: dict[str, Any] | None, environ: Any = os.environ
) -> dict[str, Any]:
    """Effective values the station would use: file > env > default."""

    cfg: dict[str, Any] = {}
    audio_raw = environ.get("MRRC_FT8_AUDIO_DEVICE", "")
    if audio_raw:
        cfg["audio_device"] = _audio_value(audio_raw)
    cfg["audio_in_device"] = _resolve_audio(file_cfg, environ, direction="in")
    cfg["audio_out_device"] = _resolve_audio(file_cfg, environ, direction="out")
    ch_raw = environ.get("MRRC_FT8_AUDIO_IN_CHANNEL", "")
    cfg["audio_in_channel"] = (
        int(ch_raw) if ch_raw.isdigit() else 0
    ) if ch_raw else 0
    rig_mode = environ.get("MRRC_FT8_RIG_MODE", "")
    cfg["rig_mode"] = rig_mode or "USB"
    _, _, rig_port = environ.get("MRRC_FT8_RIGCTLD", "127.0.0.1:4532").partition(":")
    cfg["rigctld_port"] = int(rig_port or 4532)
    for env, key in (
        ("MRRC_FT8_RIG_MODEL", "rig_model"),
        ("MRRC_FT8_RIG_DEVICE", "rig_device"),
        ("MRRC_FT8_RIG_BAUD", "rig_baud"),
        ("MRRC_FT8_RIG_STOP_BITS", "rig_stop_bits"),
    ):
        value = environ.get(env, "")
        if value:
            cfg[key] = int(value) if value.isdigit() else value
    if file_cfg:
        cfg.update(file_cfg)
    return cfg


def merge_into(config: Any, file_cfg: dict[str, Any] | None) -> Any:
    """ServerConfig copy with file overrides (audio in/out + rigctld port)."""

    if not file_cfg:
        return config
    overrides: dict[str, Any] = {}
    for key in ("audio_device", "audio_in_device", "audio_out_device"):
        if key in file_cfg:
            overrides[key] = file_cfg[key]
    if "audio_in_channel" in file_cfg:
        overrides["audio_in_channel"] = int(file_cfg["audio_in_channel"])
    if "rig_mode" in file_cfg:
        overrides["rig_mode"] = file_cfg["rig_mode"]
    if "rigctld_port" in file_cfg:
        overrides["rigctld_port"] = int(file_cfg["rigctld_port"])
    return dataclasses.replace(config, **overrides)


def enumerate_audio_devices(timeout: float = 3.0) -> list[dict[str, Any]]:
    """sounddevice devices → [{index, name, max_input, max_output}]; [] on error.

    Timeout-guarded: a wedged CoreAudio (2026-08-10 field: an aggregate
    referencing a powered-off radio) can block ``import sounddevice`` /
    ``query_devices`` indefinitely — the API must return an empty list
    instead of hanging the endpoint thread.
    """

    import concurrent.futures

    def _query() -> list[Any]:
        import sounddevice

        return list(sounddevice.query_devices())

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(_query)
        devices = future.result(timeout=timeout)
    except Exception:
        executor.shutdown(wait=False)  # 挂起的导入线程留在后台，不阻塞返回
        return []
    executor.shutdown(wait=False)
    rows: list[dict[str, Any]] = []
    for index, dev in enumerate(devices):
        rows.append(
            {
                "index": index,
                "name": str(dev["name"]),
                "max_input": int(dev["max_input_channels"]),
                "max_output": int(dev["max_output_channels"]),
            }
        )
    return rows


def enumerate_serial_devices() -> list[str]:
    """Candidate CAT serial devices (macOS/Linux /dev names, Windows COM)."""

    if os.name == "nt":
        try:
            from serial.tools import list_ports
            return sorted(p.device for p in list_ports.comports())
        except Exception:
            return sorted(glob.glob("COM[0-9]*"))
    return sorted(
        set(glob.glob("/dev/cu.*")) | set(glob.glob("/dev/ttyUSB*")) | set(glob.glob("/dev/ttyACM*"))
    )


def validate(
    cfg: dict[str, Any],
    audio_devices: list[dict[str, Any]],
    *,
    current_effective: Any = None,
) -> str | None:
    """Error message for an invalid config, else None."""

    if "rig_model" in cfg:
        model = cfg["rig_model"]
        if not isinstance(model, int) or isinstance(model, bool) or model <= 0:
            return "rig_model must be a positive integer"
    if "rig_device" in cfg:
        device = cfg["rig_device"]
        # 空/空白串口 = "不改动"（本站串口可能只在 restart.sh 默认值里，UI 表单
        # 总是提交该字段）；非空才要求绝对 /dev/ 路径。
        if isinstance(device, str):
            device = device.strip()
        if device:
            if os.name == "nt":
                if not isinstance(device, str) or not re.fullmatch(r"COM\d{1,3}", device.upper()):
                    return "rig_device must be a COM port (e.g. COM3)"
            elif not isinstance(device, str) or not device.startswith("/dev/"):
                return "rig_device must be an absolute /dev/... path"
    if "rig_baud" in cfg:
        if cfg["rig_baud"] not in BAUD_RATES:
            return "rig_baud must be one of " + ", ".join(str(b) for b in BAUD_RATES)
    if "rig_stop_bits" in cfg:
        stop = cfg["rig_stop_bits"]
        if not isinstance(stop, int) or isinstance(stop, bool) or stop not in (1, 2):
            return "rig_stop_bits must be 1 or 2"
    if "rigctld_port" in cfg:
        port = cfg["rigctld_port"]
        if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535 or port == 8000:
            return "rigctld_port must be an integer in 1024..65535 (not 8000)"
    if "rig_mode" in cfg:
        import re as _re

        if not isinstance(cfg["rig_mode"], str) or not _re.fullmatch(r"[A-Z0-9]{1,16}", cfg["rig_mode"]):
            return "rig_mode must be an uppercase Hamlib mode token (e.g. USB)"
    for key in ("audio_device", "audio_in_device", "audio_out_device"):
        if key in cfg and cfg[key] is not None:
            wanted = cfg[key]
            names = {str(d["name"]) for d in audio_devices}
            indexes = {d["index"] for d in audio_devices}
            ok = (isinstance(wanted, str) and wanted in names) or (
                isinstance(wanted, int) and not isinstance(wanted, bool) and wanted in indexes
            )
            # 未变值放行（设备暂时不在枚举里也能保存，如 USB 热插拔间隙）
            effective = current_effective.get(key) if isinstance(current_effective, dict) else current_effective
            if not ok and effective is not None and wanted == effective:
                ok = True
            if not ok:
                return f"{key} not found in device list"
    if "audio_in_channel" in cfg:
        ch = cfg["audio_in_channel"]
        if not isinstance(ch, int) or isinstance(ch, bool) or ch not in (0, 1):
            return "audio_in_channel must be 0 (left) or 1 (right)"
    return None


def source_of(file_cfg: dict[str, Any] | None, environ: Any = os.environ) -> dict[str, str]:
    """Per-key origin: file | env | default."""

    source: dict[str, str] = {}
    for key in _CONFIG_KEYS:
        if file_cfg and key in file_cfg:
            source[key] = "file"
        elif environ.get(_SOURCE_ENV[key]):
            source[key] = "env"
        else:
            source[key] = "default"
    return source


class DeviceConfigStore:
    """The API's seam: load/save the config file and spawn the restart script."""

    def __init__(
        self, path: str | Path = DEFAULT_CONFIG_PATH, *, script: str | Path | None = None
    ) -> None:
        self.path = _NATIVE_PATH(path)
        if script is not None:
            self.script = _NATIVE_PATH(script)
        elif os.name == "nt":
            from server.core.paths import app_root
            self.script = app_root() / "restart.ps1"
        else:
            self.script = _NATIVE_PATH(__file__).resolve().parents[2] / "restart.sh"

    def load(self) -> dict[str, Any] | None:
        return load_device_config(self.path)

    def save(self, cfg: dict[str, Any]) -> None:
        save_device_config(cfg, self.path)

    def spawn_restart(self) -> None:
        """Detached restart run; survives this process being killed."""

        if not self.script.exists():
            log.error("restart script missing: %s", self.script)
            raise FileNotFoundError(f"restart script missing: {self.script}")
        if os.name == "nt":
            data_dir = _NATIVE_PATH(os.environ.get("LOCALAPPDATA", ".")) / "MRRC-FT8"
            data_dir.mkdir(parents=True, exist_ok=True)
            log_path = data_dir / "restart.log"
            flags = 0
            for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP"):
                flags |= getattr(subprocess, name, 0)
            with log_path.open("ab") as fh:
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-File", str(self.script)],
                    cwd=str(self.script.parent),
                    stdout=fh,
                    stderr=fh,
                    creationflags=flags,
                    close_fds=True,
                )
            return
        log_path = Path("/tmp/mrrc-ft8-restart.log")
        with log_path.open("ab") as fh:
            subprocess.Popen(
                [str(self.script)],
                cwd=str(self.script.parent),
                stdout=fh,
                stderr=fh,
                start_new_session=True,
                close_fds=True,
            )
