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
}
DEFAULT_BAUD_RATE = _DEFAULT_BAUD_BY_MODEL.get(RADIO_MODEL, 38400)


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
