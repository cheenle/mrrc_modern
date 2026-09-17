"""Support diagnostics bundle: collect, redact, summarise, package.

Spec: docs/superpowers/specs/2026-09-17-support-bundle-design.md (§4–§6).
Stdlib only and no application imports on purpose: the module stays unit
testable in isolation and can be hot-fixed later without a release.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

REDACTED = "<redacted>"

# Two independent passes (spec §6).  The key allow-list is the security
# boundary; the value pass only cleans text that is collected anyway (a
# password pasted into a log line, a `?token=` inside a URL).
#
# The leading `(?<![A-Za-z0-9])` (not `\b`) is deliberate: `_` is a word
# character, so `\b` FAILS to match our own `MRRC_WEB_PASSWORD=` (the case this
# pass exists for) while the lookbehind catches it and still ignores words that
# merely end in one of these tokens (`compass=`).
SECRET_VALUE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(cookie[_-]?secret|pass(?:word|wd)?|secret|token|api[_-]?key|credential)\b"
    r"(\s*[=:]\s*)(\S+)"
)

CONFIG_KEY_ALLOWLIST = frozenset({
    # Radio / serial
    "MRRC_RADIO_MODEL", "MRRC_SERIAL_PORT", "MRRC_BAUD_RATE",
    # Web
    "MRRC_WEB_HOST", "MRRC_WEB_PORT",
    # Spectrum
    "MRRC_SCOPE_PORT", "MRRC_SCOPE_BAUD", "MRRC_FTDI_LIB_DIR",
    # Audio / recording
    "MRRC_AUDIO_RX_DEVICE", "MRRC_AUDIO_TX_DEVICE",
    "MRRC_RECORDINGS_BITRATE", "MRRC_RECORDINGS_MAX_SESSION_MIN", "MRRC_CQ_FILE",
    # Tuner / TLS mode / model safety
    "MRRC_ATR1000_HOST", "MRRC_ATR1000_PORT",
    "MRRC_SSL_CERT", "MRRC_SSL", "MRRC_ALLOW_UNVERIFIED_TX",
})

# Path fragments that never enter a bundle (spec §6).
FORBIDDEN_SUBSTRINGS = (
    "certs", "cert", ".pem", ".key", ".crt", ".p12",
    "recordings", "mem_channels.json", "atr1000_tuner.json",
)


def redact_text(text: str) -> tuple[str, int]:
    """Replace secret-looking values; return (new_text, hits)."""
    return SECRET_VALUE_RE.subn(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text or "")


def redact_env_text(text: str) -> tuple[str, int, int]:
    """Allow-list an env file; return (new_text, dropped_keys, value_hits)."""
    out: list[str] = []
    dropped = hits = 0
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            cleaned, n = redact_text(line)
            hits += n
            out.append(cleaned)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key not in CONFIG_KEY_ALLOWLIST:
            dropped += 1
            continue
        cleaned, n = redact_text(line)
        hits += n
        out.append(cleaned)
    return "\n".join(out) + "\n", dropped, hits


def is_collectable(relative_path: str) -> bool:
    """Whether a path may enter the bundle (spec §6)."""
    lowered = str(relative_path).lower().replace("\\", "/")
    return not any(token in lowered for token in FORBIDDEN_SUBSTRINGS)


# ── log discovery and bounded tails (spec §5/§6) ────────────────────────────
DEFAULT_TAIL_BYTES = 2 * 1024 * 1024


def tail_lines(path, max_bytes: int = DEFAULT_TAIL_BYTES) -> str:
    """Read at most `max_bytes` from the end of a file, on a line boundary."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()                      # drop the half line
            data = fh.read()
    except OSError:
        return ""
    return data.decode("utf-8", "replace")


def resolve_log_files(log_dir, data_dir="", install_dir="") -> dict:
    """Map bundle role -> existing log file (spec §5).

    Roles: server / server-prev / stdout / stdout-prev / launcher / legacy.
    The desktop launcher puts logs under the user data dir; systemd and
    `start.sh` write into the install dir, which is also where a hand-started
    server writes when MRRC_LOG_DIR is unset.
    """
    log_dir = Path(log_dir)
    candidates = [
        ("server", log_dir / "server.log"),
        ("server-prev", log_dir / "server.log.1"),
        ("stdout", log_dir / "server-stdout.log"),
        ("stdout-prev", log_dir / "server-stdout.log.1"),
    ]
    if data_dir:
        candidates.append(("launcher", Path(data_dir) / "launcher.log"))
    if install_dir:
        candidates.append(("legacy", Path(install_dir) / "logs" / "ft710-server.log"))
        candidates.append(("legacy-server", Path(install_dir) / "logs" / "server.log"))

    found: dict = {}
    seen: set = set()
    for role, path in candidates:
        try:
            key = os.path.realpath(path)
        except OSError:
            continue
        if key in seen or not os.path.isfile(path):
            continue
        seen.add(key)
        found[role] = str(path)
    return found
