"""Support diagnostics bundle: collect, redact, summarise, package.

Spec: docs/superpowers/specs/2026-09-17-support-bundle-design.md (§4–§6).
Stdlib only and no application imports on purpose: the module stays unit
testable in isolation and can be hot-fixed later without a release.
"""
from __future__ import annotations

import os
import re

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
