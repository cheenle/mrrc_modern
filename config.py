"""
MRRC Modern — Configuration & Constants
=======================================
Protocol-neutral, environment-based configuration (serial, web, SSL,
auth, polling, reconnect, PTT safety) plus genuinely shared UI tables.

Radio-specific tables (mode registers, bands, filter widths, meter
calibration, scope spans) live with their backend — see
``backends/ft710/config_ft710.py`` for the FT-710 set.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import overload

import env_store

SCRIPT_DIR = Path(__file__).resolve().parent


# ── Which config file a start-up reads ─────────────────────────────
# Kept above the constants on purpose: ``load_user_config_into_environ`` below has to run
# before any ``_env(...)`` call site, and this helper is what resolves the file's directory.
def default_user_dir() -> Path:
    """The per-user directory this app may write to, per platform.

    Never raises: a test that reloads this module with a stripped environment made Path.home() throw
    on Windows, and a path helper is not a place to fail a start-up over.
    """
    try:
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA") or ""
            if not base:
                profile = os.environ.get("USERPROFILE") or ""
                if profile:
                    base = os.path.join(profile, "AppData", "Local")
        elif sys.platform == "darwin":
            base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
        else:
            base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    except Exception:                                                 # noqa: BLE001 - see docstring
        base = ""
    if not base or base in ("~", os.sep):
        return Path(SCRIPT_DIR) / "user-data"
    return Path(base) / "MRRC-Modern"




def boot_config_file() -> Path | None:
    """The user env file this process should read at start-up, or None.

    ``MRRC_CONFIG_FILE`` names it explicitly (the launchers always do); otherwise the
    per-user copy, which is where a packaged install keeps its settings.
    """
    raw = os.environ.get("MRRC_CONFIG_FILE", "").strip()
    path = Path(raw) if raw else default_user_dir() / "mrrc_modern.env"
    try:
        return path if path.is_file() else None
    except OSError:                                       # unreadable path: not our problem
        return None


def load_user_config_into_environ() -> None:
    """Fill ``MRRC_*`` from the user config file for a start-up that has no launcher.

    The launchers read the file and pass it down as the child environment, so a server
    started *by* a launcher sees everything. Starting ``MRRC-Modern-Server.exe`` on its own
    — which the installer ships as the "MRRC Modern Server" shortcut, and which
    ``_ensure_strong_password`` documents as the "the launcher will not start" path — read
    none of it: a field log showed it binding the built-in ``::`` while the file said
    ``127.0.0.1``, and serving the built-in default password while the file had a real one.

    Only variables the process does not already have are filled, so an explicit environment
    (systemd on the Pi, a shell, a test) still wins, and a launcher start-up is a no-op: it
    has already put every file key into the environment. Auto-loading is limited to packaged
    runs (``sys.frozen``) plus an explicit ``MRRC_CONFIG_FILE``, so a stray file in a
    developer's home directory can never change what the suite sees.
    """
    if os.environ.get("MRRC_NO_CONFIG_FILE", "").strip().lower() in ("1", "true", "yes"):
        return
    explicit = os.environ.get("MRRC_CONFIG_FILE", "").strip()
    if not explicit and not getattr(sys, "frozen", False):
        return
    path = boot_config_file()
    if path is None:
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.startswith((_NEW_ENV_PREFIX, _LEGACY_ENV_PREFIX)):
            continue
        os.environ.setdefault(key, value.strip())


# ── Environment helpers ─────────────────────────────────────────────
# Prefer the MRRC_* variable name, falling back to the legacy FT710_*
# prefix so existing deployments keep working unchanged.
_LEGACY_ENV_PREFIX = "FT710_"
_NEW_ENV_PREFIX = "MRRC_"

# Runs here rather than next to the helpers above: it filters on the prefixes, so it
# must not execute before they are bound. Nothing reads an env var until _env below.
load_user_config_into_environ()


# ── Boot repair: one duplicated key silently disables every later write ──────
def repair_duplicated_env_keys() -> dict[str, str]:
    """Collapse duplicated keys in the user env file; return ``{key: kept value}``.

    2026-10-26 field (bundle 20261026-194408-16d0, W103D + IC-7300): the file carried
    ``MRRC_SERIAL_PORT`` twice, the second line empty. systemd's ``EnvironmentFile``
    hands us the **last** value, so the server started with an empty port, while the
    connection dialog's writer rewrote only the **first** line — six saves of
    ``/dev/ttyUSB0``, zero CI-V connections in 26 days, and the operator's report was
    simply "无法控制 IC7300". ``env_store`` now collapses the duplicates on write; this
    is the other half, for a box that receives the fix without anyone touching the UI:
    repair the file at start-up, and make *this* start agree with it.

    Never raises: a broken config repair must not be able to stop a server.
    """
    path = boot_config_file()
    if path is None:
        return {}
    try:
        return env_store.collapse_duplicates(path)
    except Exception:                                     # noqa: BLE001 - see docstring
        return {}


ENV_REPAIRS: dict[str, str] = repair_duplicated_env_keys()


def _apply_env_repairs(repairs: dict[str, str]) -> None:
    """Make this start use the repaired values — but only the shadowed ones.

    Only an *empty* environment value can be the shadowed duplicate (an empty value
    is "unset", never an override), and an explicit non-empty environment still
    wins, exactly as ``load_user_config_into_environ`` promises: the launchers put
    the file's values into the child environment themselves, so overriding a
    non-empty one would mean guessing which of two real values is right.
    """
    for key, value in repairs.items():
        if value and not os.environ.get(key, "").strip():
            os.environ[key] = value


_apply_env_repairs(ENV_REPAIRS)


@overload
def _env(name: str, default: str) -> str: ...


@overload
def _env(name: str, default: str | None = None) -> str | None: ...


def _env(name: str, default: str | None = None) -> str | None:
    """Read ``MRRC_*`` env var, falling back to the legacy ``FT710_*`` alias.

    The overloads matter: with a string default the result is always a string,
    which is what every ``Path(_env(...))`` call site relies on.
    """
    val = os.environ.get(name)
    if val is not None:
        return val
    if name.startswith(_NEW_ENV_PREFIX):
        legacy = _LEGACY_ENV_PREFIX + name[len(_NEW_ENV_PREFIX):]
        val = os.environ.get(legacy)
    return default if val is None else val


def _env_int(name: str, default: int) -> int:
    """Integer env var; a typo falls back to the default instead of killing boot."""
    try:
        return int(_env(name) or default)
    except (TypeError, ValueError):
        print(f"Warning: {name} is not an integer — using {default}", flush=True)
        return default


def _env_port(name: str, default: int) -> int:
    """A valid UDP/TCP port; invalid input falls back with a warning."""
    value = _env_int(name, default)
    if 1 <= value <= 65535:
        return value
    print(f"Warning: {name} is outside 1..65535 — using {default}", flush=True)
    return default


def _env_choice(name: str, default: int, choices: tuple[int, ...]) -> int:
    """An integer from a finite device-supported set, never a clamp."""
    value = _env_int(name, default)
    if value in choices:
        return value
    print(f"Warning: {name} must be one of {choices} — using {default}", flush=True)
    return default


def _env_float(name: str, default: float) -> float:
    """Float env var; a typo falls back to the default instead of killing boot."""
    try:
        return float(_env(name) or default)
    except (TypeError, ValueError):
        print(f"Warning: {name} is not a number — using {default}", flush=True)
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean env var: 1/true/yes/on are true, anything else false."""
    val = _env(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# ── Unverified-Model Transmit Gate ──────────────────────────────────
# Radios whose profile has no hardware evidence refuse PTT/TUNE until the
# operator opts in (spec 2026-09-12 §6.1).  Read at import time, like the
# other safety switches, so the state is visible in the startup log.
ALLOW_UNVERIFIED_TX = _env_bool("MRRC_ALLOW_UNVERIFIED_TX", False)

# ── Shared TX meter curves ──────────────────────────────────────────
# Same shapes as the FT-710 tables (config_ft710.py:166-208); only the
# rated power differs per model, so the profile passes its own maximum.
def _make_raw_to_power(rated_w: int):
    def _raw_to_power(raw: int) -> float:
        return round(max(0, min(raw, 255)) / 255 * rated_w, 1)
    return _raw_to_power


def _raw_to_swr(raw: int) -> float:
    return round(1.0 + max(0, min(raw, 255)) / 255 * 9.0, 2)


def _raw_to_voltage(raw: int) -> float:
    return round(max(0, min(raw, 255)) / 255 * 16.0, 1)


def _raw_to_current(raw: int) -> float:
    return round(max(0, min(raw, 255)) / 255 * 25.0, 1)


RAW_TO_METER_TABLES = {
    "power": _make_raw_to_power,
    "swr": lambda: _raw_to_swr,
    "voltage": lambda: _raw_to_voltage,
    "current": lambda: _raw_to_current,
}


# ── Recording ───────────────────────────────────────────────────────
# 16 kHz mono MP3 written incrementally while recording (AD-017).  The
# session cap is a forgot-to-stop guard, not a retention policy: it stops
# the session and never deletes a recording.
RECORDINGS_BITRATE = _env_int("MRRC_RECORDINGS_BITRATE", 64)
RECORDINGS_MAX_SESSION_MIN = _env_int("MRRC_RECORDINGS_MAX_SESSION_MIN", 240)
#: Free space below which a new recording is refused instead of started. The
#: recorded stream is the one writer a customer controls, and it is unbounded:
#: 16 kHz mono MP3 at 64 kbps is ~29 MB per hour, so a box left recording a few
#: hours a day fills its ~4 GB of free space within months — and a full disk does
#: not stop recording politely, it takes PTT, audio and the config writes with
#: it. Refusing is the non-destructive half of a retention policy: nothing the
#: operator recorded is ever deleted by the product. 0 disables the guard.
RECORDINGS_MIN_FREE_MB = _env_int("MRRC_RECORDINGS_MIN_FREE_MB", 200)

# ── CQ key (spec 2026-09-13) ──────────────────────────────────────
#: CQ recording played by the CQ key.  MRRC_CQ_FILE overrides it (any
#: mono/stereo 16-bit WAV; normalised to 48 kHz mono once at startup).
#: The default lives under static/, so it ships inside every bundle
#: (PyInstaller puts static/ next to the bundled modules as _internal/static).
CQ_ASSET_PATH = Path(_env("MRRC_CQ_FILE", "") or
                     (Path(__file__).resolve().parent / "static" / "audio" / "cq.wav"))


# ── Radio Model Selection ───────────────────────────────────────────
# Backend key registered in backends/__init__.py. Select with
# MRRC_RADIO_MODEL.
RADIO_MODEL = (os.environ.get("MRRC_RADIO_MODEL") or "ft710").strip().lower()
_DEFAULT_BAUD_BY_MODEL = {
    "ft710": 38400,
    "ic7300": 115200,
    "ic7300mk2": 115200,
    "ic705": 115200,
    "ic7610": 115200,
    "ic7760": 115200,
    # Yaesu ASCII-CAT family: Yaesu's documented default is 38400 8N1
    # (Hamlib ftx1/ftx1_readme.txt confirms the FTX-1's default).
    "ftdx10": 38400,
    "ftdx101d": 38400,
    "ftdx101mp": 38400,
    "ftx1": 38400,
    # FT-891: 出厂默认是 **4800**（菜单 05-06 CAT RATE: 4800/9600/19200/38400，
    # Yaesu FT-891 操作手册；西语版、俄语版手册同表）。此前这里写的是 38400，
    # 依据是 2026-09-17 的一份现场记录 —— 那台机器的用户**已经**把菜单改成了 38400
    # （DigiRig 的 FT-891 数字模式指南也这么建议），不能当出厂值。
    # 2026-10-21 现场（包 20261021-123324-81ab）：电台停在出厂 4800、配置是 38400，
    # 日志只有一句 "No ID response from radio"，用户看不出是波特率不对。
    # 现在两头都改了：默认值按手册，控制器在沉默时自动试完四个档位。
    "ft891": 4800,
    # FT-991A: 实测出厂值就是 4800（2026-11-01，BG7MWR 那台盒子）：/api/setup 写过
    # 38400 之后，控制器每次启动都要走一遍静默阶梯才落回 4800，日志里每次都留一条
    # 「no answer at 38400」。手册的 CAT RATE 菜单出厂值与 FT-891 同为 4800，
    # Hamlib ft991.c:186 serial_rate_min=4800 的 "Default rate per manual" 是对的；
    # 之前取 38400 是把家族惯例当成了本机手册。
    "ft991a": 4800,
    # Xiegu G7250: the radio's USB CAT bridge runs at 115200 (community
    # reverse engineering, spec 2026-11-07), not the X6100/X6200's 19200.
    # The backend probes CI-V addresses 0xA4/0x88/0x70, so only the rate is
    # fixed here.
    "g7250": 115200,
    # SunSDR2 DX (spec 2026-11-08): UDP-controlled — there is no serial baud
    # at all.  The entry exists only because this table is registry-derived
    # (tests/test_config.py requires every known model to appear) and the
    # setup save writes a baud for the selected model; the value is inert.
    "sunsdr2dx": 115200,
}
DEFAULT_BAUD_RATE = _DEFAULT_BAUD_BY_MODEL.get(RADIO_MODEL, 38400)


def baud_for_save(model: str, stored_env: dict) -> str:
    """The `MRRC_BAUD_RATE` a **connection-settings save** must write.

    The dialog has no baud field, so the save used to write the model default
    unconditionally.  That is right on a model switch (V2.33: an IC-7300 picked
    up the FT-710's leftover 38400 and its CI-V scope never came up) and wrong
    on every other save: an FT-991A deliberately moved to 38400 (menu 031, the
    rate this product itself recommends because 4800 saturates the link) drops
    back to the factory 4800 the moment the operator picks a different audio
    device — found 2026-11-01 while answering "where does 4800 come from?".

    So: keep what is stored, unless the model itself changed (or nothing is
    stored yet, where the model default is the only sensible answer).
    """
    previous_model = str(stored_env.get("MRRC_RADIO_MODEL", "")).strip().lower()
    stored_baud = str(stored_env.get("MRRC_BAUD_RATE", "")).strip()
    if stored_baud and previous_model == str(model).strip().lower():
        return stored_baud
    return str(default_baud_for(model))


def default_baud_for(model: str) -> int:
    """Backend-aware default CAT/CI-V baud for a radio model key.

    Shared by the connection-dialog save (api_setup_save) and first-run
    probing so the stored MRRC_BAUD_RATE always aligns with the selected
    model. Rationale (V2.33): the legacy installer templates pre-filled
    MRRC_BAUD_RATE=38400 — the FT-710 value — regardless of model; an
    IC-7300 then kept the stale value and its CI-V scope stream, which
    requires 115200, never came up (field log 2026-09-10).
    """
    return _DEFAULT_BAUD_BY_MODEL.get(str(model).strip().lower(), 38400)


# ── Xiegu G7250 LAN IQ (optional) ───────────────────────────────────
# The G7250 has no CI-V 0x27 scope; its spectrum comes from a UDP IQ stream
# (see backends/xiegu/lan_iq.py).  Empty = disabled, and the radio then runs
# like the Yaesu ASCII-CAT models with the synthetic S-meter waterfall.
# MRRC_G7250_CIV_ADDR pins the CI-V address when the auto-probe (0xA4/0x88/
# 0x70) picks the wrong one; both are read at startup only.
G7250_LAN_IP = (_env("MRRC_G7250_LAN_IP", "") or "").strip()
G7250_CIV_ADDR = (_env("MRRC_G7250_CIV_ADDR", "") or "").strip()
# Set MRRC_G7250_SWAP_IQ=1 if the waterfall comes out mirrored (a signal above
# the dial rendering below it): the vendor DLL leaves I/Q order undocumented.
G7250_SWAP_IQ = (_env("MRRC_G7250_SWAP_IQ", "") or "").strip().lower() in (
    "1", "true", "yes", "on")
# RX audio from the LAN IQ stream instead of the radio's built-in USB sound
# card.  On by default **once a LAN address is configured**: that stream is the
# only path this radio documents for both spectrum and audio, and the field
# case that produced this option (BG7YQV, 2026-11-12) had the built-in sound
# card enumerating on Windows while its capture stream sat at -70 dBFS.
# `MRRC_G7250_LAN_AUDIO=0` goes back to the sound card without forgetting the
# IQ address (the waterfall keeps coming from the LAN either way).
G7250_LAN_AUDIO = (_env("MRRC_G7250_LAN_AUDIO", "1") or "").strip().lower() in (
    "1", "true", "yes", "on")


# ── Expert Electronics SunSDR2 DX native UDP backend ────────────────
SUNSDR_HOST = (_env("MRRC_SUNSDR_HOST", "") or "").strip()
SUNSDR_LOCAL_HOST = (_env("MRRC_SUNSDR_LOCAL_HOST", "") or "").strip()
SUNSDR_CONTROL_PORT = _env_port("MRRC_SUNSDR_CONTROL_PORT", 50001)
SUNSDR_IQ_PORT = _env_port("MRRC_SUNSDR_IQ_PORT", 50002)
SUNSDR_SAMPLE_RATE = _env_choice("MRRC_SUNSDR_SAMPLE_RATE", 78125,
                                  (39062, 78125, 156250, 312500))


# ── Plausibility window for a frequency read back from the radio ─────
# Every frequency that arrives from the radio (the IF poll, the VFO poll, a
# CI-V transceive broadcast) used to be filtered against a literal
# ``30000 <= freq <= 75_000_000``.  75 MHz is the FT-710's ceiling, and for
# years that was the whole fleet — so the guard was invisible.  On a model
# that covers 2 m/70 cm it silently threw away every VHF/UHF reading: the
# FT-991A took ``FA144500000``/``FA435000000`` (2026-11-01 field, BG7MWR) and
# the poll then dropped the answer, so the web UI's frequency never came back.
# The ceiling is now derived from the active model's own band table and the
# 75 MHz figure only survives as a *floor* — widening the window must never
# narrow it for the verified HF+6 m models (the FT-710 tops out at 70.5 MHz).
FREQ_MIN_HZ = 30_000                # 30 kHz, the bottom of every RX range
FREQ_HF_MAX_HZ = 75_000_000         # FT-710/IC-7300-class ceiling + margin


def plausible_freq_range(bands=()) -> tuple[int, int]:
    """``(min, max)`` Hz that is a real frequency on *this* radio.

    ``bands`` is a backend's ``bands`` list — the ``{name, start, end}``
    dicts every client receives — but a ``[label, low, high]`` triple is
    tolerated because that is what the Yaesu profiles used to publish.
    No bands (a fake backend in a test) means the HF-only default, which is
    the behaviour every model had before this window existed.
    """
    top = FREQ_HF_MAX_HZ
    for band in bands or ():
        try:
            end = int(band.get("end") or 0)
        except AttributeError:
            try:
                end = int(band[2])
            except (IndexError, KeyError, TypeError, ValueError):
                continue
        except (TypeError, ValueError):
            continue
        if end > top:
            top = end
    return FREQ_MIN_HZ, top

# ── Serial Configuration ────────────────────────────────────────────
# macOS default: /dev/cu.SLAB_USBtoUART  (FT-710 Enhanced COM Port)
# Linux default: /dev/ttyUSB0
# Per platform. The macOS device name used to be the default everywhere, so a Windows install
# started out pointing at a port that cannot exist there and the radio was never found (reported
# from a real machine: the log said /dev/cu.SLAB_USBtoUART while COM5 was sitting right there).
if sys.platform == "darwin":
    _DEFAULT_SERIAL_PORT = "/dev/cu.SLAB_USBtoUART"
else:
    _DEFAULT_SERIAL_PORT = ""            # empty = auto-detect on first run
SERIAL_PORT = _env("MRRC_SERIAL_PORT", _DEFAULT_SERIAL_PORT)
BAUD_RATE = _env_int("MRRC_BAUD_RATE", DEFAULT_BAUD_RATE)
SERIAL_TIMEOUT = _env_float("MRRC_SERIAL_TIMEOUT", 1.0)
# Short per-query timeout for background pollers.  Bounds how long a
# non-responding poll query can hold the serial lock (and thus block a
# user command like PTT).  Normal responses arrive in <50 ms; 0.25 s is
# generous while keeping worst-case PTT latency bounded.
POLL_TIMEOUT = 0.25

# ── Audio Device ──────────────────────────────────────────────────────
# Set a specific device index or substring to match in device name
# (e.g., "4" for device index 4, or "FT-710" to match by name)
AUDIO_RX_DEVICE = _env("MRRC_AUDIO_RX_DEVICE", "")
AUDIO_TX_DEVICE = _env("MRRC_AUDIO_TX_DEVICE", "")

# Capture host buffer in milliseconds of device samples (the ``frames_per_buffer``
# of the RX input stream). RX_CHUNK_MS (20 ms = one Opus frame) is the *read*
# granularity; as the host buffer it leaves the device only the ~2 periods ALSA
# hands a blocking stream, so a read tick that lands late overflows it. On Linux an
# overflow is not merely dropped samples: on the W103D (Amlogic S905L3A, xhci +
# USB-audio, kernel 6.18) the capture interface stops — /proc/asound/card1/stream0
# reads "Status: Stop" — while PyAudio keeps reporting an open, error-free stream,
# so the loop reads nothing forever and the listener hears chopped audio and then
# silence. Measured 2026-10-11: 4% coverage and a permanent stall at 20 ms; 100%
# and stable at 100 ms. CoreAudio/WASAPI give the same code far more slack, which
# is why the desktops keep the tight 20 ms. Override: MRRC_RX_BUFFER_MS.
RX_HOST_BUFFER_MS = _env_int(
    "MRRC_RX_BUFFER_MS", 100 if sys.platform.startswith("linux") else 20)

# ── ATR1000 Antenna Tuner (optional) ───────────────────────────────
# Networked automatic antenna tuner with a built-in WebSocket server.
# Empty host (default) = feature fully disabled: no client, no tasks,
# no linkage hooks — zero impact for users without the hardware.
ATR1000_HOST = _env("MRRC_ATR1000_HOST", "")
ATR1000_PORT = _env_int("MRRC_ATR1000_PORT", 60001)

# ── Web Server Configuration ────────────────────────────────────────
WEB_PORT = _env_int("MRRC_WEB_PORT", 8888)
# SECURITY: Change this password in production! Use a strong, unique password.
# Recommended: 16+ characters with mixed case, numbers, and symbols
DEFAULT_WEB_PASSWORD = "changeme_please_use_strong_password!"
WEB_PASSWORD = _env("MRRC_WEB_PASSWORD", DEFAULT_WEB_PASSWORD)
# Optional second password for the listen-only interface (/listen): frequency
# and mode changes plus RX audio/spectrum, no TX and no device settings.
# Empty (default) disables listen-only logins entirely.
LISTEN_PASSWORD = _env("MRRC_LISTEN_PASSWORD", "")
WEB_HOST = _env("MRRC_WEB_HOST", "::")  # IPv6 dual-stack

# Remote-session metering (hub open issue I-H1): one INFO line per interval with
# listener/operator concurrency and uplink kbps, plus GET /api/session_metrics.
# Counts and bytes only — no identifiers. 0 disables the periodic line (the
# endpoint keeps working).
SESSION_METRICS_INTERVAL_S = _env_float("MRRC_SESSION_METRICS_INTERVAL_S", 300.0)
SESSION_METRICS_WINDOW_S = _env_float("MRRC_SESSION_METRICS_WINDOW_S", 3600.0)

# TX-phase liveness gate (SDD ch15 §15.6, hub AD-H06): while transmitting, the
# session holding the key must keep proving it is alive; when its heartbeat goes
# stale the server unkeys locally instead of waiting for a TCP close that a
# dropped NAT entry never produces. 0 = off (the default) — the cloud hub sets
# it, and only sessions that declare capability by sending `txhb` are gated.
REMOTE_SESSION_TX_HEARTBEAT_S = _env_float("MRRC_REMOTE_SESSION_TX_HEARTBEAT_S", 0.0)

# SSL (Let's Encrypt certs for radio.vlsc.net)
# The certificate lives in the user's data directory, not next to the program: the packaged
# directory is read-only for a normal user, and baking the build machine's path into a default
# shipped a certificate path that existed on nobody's machine. When the file is missing the server
# generates a self-signed one here (see server.py) instead of silently serving plain HTTP, which
# made the browser show a protocol error and the UI look dead.
def _default_cert_dir() -> Path:
    return default_user_dir() / "certs"


CERT_DIR = _default_cert_dir()
SSL_CERTFILE = _env("MRRC_SSL_CERT", str(CERT_DIR / "fullchain.pem"))
SSL_KEYFILE = _env("MRRC_SSL_KEY", str(CERT_DIR / "localhost.key"))

# ── Auth ────────────────────────────────────────────────────────────
AUTH_COOKIE = "mrrc_auth"
AUTH_TOKEN_BYTES = 32

# ── Shared Mode Display Tables ──────────────────────────────────────
# Human-friendly mode names for display
MODE_DISPLAY_NAMES: dict[str, str] = {
    "LSB": "LSB", "USB": "USB",
    "CW-U": "CW", "CW-L": "CWR",
    "AM": "AM", "AM-N": "AM-N",
    "FM": "FM", "FM-N": "FM-N",
    "RTTY-L": "RTTY", "RTTY-U": "RTTY-R",
    "DATA-L": "DATA", "DATA-U": "DATA-R",
    "DATA-FM": "D-FM", "DATA-FM-N": "D-FMN",
    "PSK": "PSK",
}

# Primary modes exposed in the UI cycle button (in order)
UI_MODES = ["LSB", "USB", "CW-U", "AM", "FM", "RTTY-L", "DATA-L"]

# Mode groups for filter width selection
NARROW_MODES = {"CW-U", "CW-L", "RTTY-L", "RTTY-U", "DATA-L", "DATA-U", "PSK"}


# ── Shared Calibration Helper ───────────────────────────────────────
def _interp(raw: int, table: list[tuple[int, float]]) -> float:
    """Piecewise-linear interpolation over a (raw, value) calibration table."""
    if raw <= table[0][0]:
        return table[0][1]
    if raw >= table[-1][0]:
        return table[-1][1]
    for i in range(len(table) - 1):
        r1, v1 = table[i]
        r2, v2 = table[i + 1]
        if r1 <= raw <= r2:
            frac = (raw - r1) / (r2 - r1)
            return v1 + frac * (v2 - v1)
    return table[-1][1]


# ── Polling Intervals (seconds) ──────────────────────────────────────
POLL_IF_INTERVAL = 0.1          # Tier 1: freq+mode+S-meter via IF;
POLL_VFO_INTERVAL = 0.5         # Tier 1b: active VFO (VS) + VFO-B freq
POLL_TX_STATUS_INTERVAL = 0.5   # Tier 2B: PTT status
POLL_TX_METERS_INTERVAL = 0.5   # Tier 2A: ALC/Power/SWR (TX only)
POLL_SETTINGS_INTERVAL = 2.0    # Tier 3: filter, gains, preamp, att, NR, NB, AN, tuner
POLL_SLOW_INTERVAL = 5.0        # Tier 4: drain current/voltage, compressor

# ── Reconnect ────────────────────────────────────────────────────────
RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 30.0

# ── PTT Safety ───────────────────────────────────────────────────────
# Reserved: the dead-man switch fires immediately on disconnect, so this
# grace period is currently unused by server.py.
PTT_SAFETY_TIMEOUT = 2.0        # Seconds to force TX0; after WebSocket disconnect
PTT_VERIFY_DELAY = 0.2          # Delay before verifying TX state change
# Opt-in stuck-keyup watchdog: force RX after this many seconds of
# continuous transmit. 0 (default) disables it — the client-side PTT
# watchdogs and the disconnect dead-man switch stay the primary layers.
# Covers the gap where a client hangs WITHOUT disconnecting (zombie socket),
# which neither of those layers catches.
PTT_MAX_TX_SECONDS = _env_float("MRRC_PTT_MAX_TX_SECONDS", 0.0)

# ── Memory Channels ──────────────────────────────────────────────────
MEM_CHANNEL_COUNT = 6
