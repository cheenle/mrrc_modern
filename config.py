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
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# ── Environment helpers ─────────────────────────────────────────────
# Prefer the MRRC_* variable name, falling back to the legacy FT710_*
# prefix so existing deployments keep working unchanged.
_LEGACY_ENV_PREFIX = "FT710_"
_NEW_ENV_PREFIX = "MRRC_"


def _env(name: str, default: str | None = None) -> str | None:
    """Read ``MRRC_*`` env var, falling back to the legacy ``FT710_*`` alias."""
    val = os.environ.get(name)
    if val is not None:
        return val
    if name.startswith(_NEW_ENV_PREFIX):
        legacy = _LEGACY_ENV_PREFIX + name[len(_NEW_ENV_PREFIX):]
        val = os.environ.get(legacy)
    return default if val is None else val


def _env_int(name: str, default: int) -> int:
    return int(_env(name) or default)


def _env_float(name: str, default: float) -> float:
    return float(_env(name) or default)


# ── Radio Model Selection ───────────────────────────────────────────
# Backend key registered in backends/__init__.py ("ft710" is currently
# the only backend).  Select with MRRC_RADIO_MODEL.
RADIO_MODEL = os.environ.get("MRRC_RADIO_MODEL", "ft710").strip().lower()

# ── Serial Configuration ────────────────────────────────────────────
# macOS default: /dev/cu.SLAB_USBtoUART  (FT-710 Enhanced COM Port)
# Linux default: /dev/ttyUSB0
SERIAL_PORT = _env("MRRC_SERIAL_PORT", "/dev/cu.SLAB_USBtoUART")
BAUD_RATE = _env_int("MRRC_BAUD_RATE", 38400)
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
WEB_PASSWORD = _env("MRRC_WEB_PASSWORD", "changeme_please_use_strong_password!")
WEB_HOST = _env("MRRC_WEB_HOST", "::")  # IPv6 dual-stack

# SSL (Let's Encrypt certs for radio.vlsc.net)
CERT_DIR = SCRIPT_DIR / "certs"
SSL_CERTFILE = _env("MRRC_SSL_CERT", str(CERT_DIR / "fullchain.pem"))
SSL_KEYFILE = _env("MRRC_SSL_KEY", str(CERT_DIR / "radio.vlsc.net.key"))

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
PTT_SAFETY_TIMEOUT = 2.0        # Seconds to force TX0; after WebSocket disconnect
PTT_VERIFY_DELAY = 0.2          # Delay before verifying TX state change

# ── Memory Channels ──────────────────────────────────────────────────
MEM_CHANNEL_COUNT = 6
