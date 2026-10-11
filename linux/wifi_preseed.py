#!/usr/bin/env python3
"""Join the Wi-Fi named in /boot/mrrc-wifi.txt, then delete the file.

Flashing a card is the one moment a user can put something on this box before it
boots, and /boot is a vfat partition they can open on their own computer — so
that is where the credentials go:

    SSID=我的WiFi
    PSK=密码

Kept separate from /boot/mrrc.env on purpose (design D-4). That file carries
MRRC_* config keys and is copied into the 0640 env that support bundles may carry;
a Wi-Fi credential must never meet a config key. This file is read once, applied,
and deleted — vfat has no permission bits, so a plaintext PSK must not be left
lying there.

Its own unit rather than part of mrrc-firstboot.service (spec R-8): that unit is
ordered After=network-online.target, and joining this Wi-Fi is the thing that
brings the network online. Folding them together is a circle.

Runs as root under mrrc-wifi-preseed.service, but only ever calls `nmcli device
wifi connect` through net_wifi — the same argv the interactive path uses, so a
PSK here has the same one channel out. This is the only place it is ever written
down.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# The repo root is net_wifi's home; this script lives one level down in linux/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import net_wifi  # noqa: E402  # type: ignore[import-not-found]

PRESEED_FILE = "/boot/mrrc-wifi.txt"


def log(message: str) -> None:
    """journald, and console: on a headless box the console is where an operator
    with an HDMI monitor is watching for exactly this line."""
    print(f"mrrc-wifi-preseed: {message}", flush=True)


def _decode(raw: bytes) -> str:
    """The user wrote this on their own computer, with whatever that computer's
    editor defaults to. utf-8-sig eats a BOM; cp936 catches a Chinese Windows
    Notepad from before it defaulted to UTF-8. Same ladder first_run uses."""
    for encoding in ("utf-8-sig", "cp936", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace")


def read_preseed(path: str = PRESEED_FILE) -> dict:
    """SSID and PSK from the file. Comments and blanks skipped.

    The *key* is matched case-insensitively — `ssid=` is the same request. The
    value keeps its case, but its surrounding whitespace is stripped: the `\r` a
    Windows editor leaves at the end of the line, or a stray pad of spaces, is
    never part of the network the user named.
    """
    try:
        with open(path, "rb") as handle:
            text = _decode(handle.read())
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip().upper()] = value.strip()
    return out


def main(argv=None) -> int:
    settings = read_preseed(PRESEED_FILE)
    ssid = settings.get("SSID", "")
    if not ssid:
        log("no SSID in the preseed file; nothing to do")
        return 0
    psk = settings.get("PSK", "")

    result = net_wifi.connect_wifi(ssid, psk)
    if not result.ok:
        # Keep the file: a typo should cost a reboot, not a reflash. The reason
        # is scrubbed because nmcli's own error text is the one place a PSK can
        # ride along into a journal.
        detail = net_wifi.scrub(result.stderr or result.stdout, psk).strip()
        log(f"could not join {ssid!r}: {detail or 'nmcli failed'}")
        return 1

    # Stated explicitly, not left to nmcli's default. A Wi-Fi the user asked for
    # should come back after a reboot — and the retired hotspot's profile is the
    # lesson in what an unexamined default costs.
    net_wifi.nmcli(["connection", "modify", ssid,
                    "connection.autoconnect", "yes"])

    try:
        os.unlink(PRESEED_FILE)
    except OSError as exc:
        log(f"joined {ssid!r}, but could not remove {PRESEED_FILE}: {exc}")
        return 0
    log(f"joined {ssid!r}; the preseed file has been removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
