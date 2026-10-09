#!/usr/bin/env python3
"""Keep an open setup hotspot up while the box has no way onto a network.

Why this exists: configuring the box needs a way *into* the box, and every way
in needs a network or a keyboard. Someone holding only a phone has neither, so
the box broadcasts one and the wizard at /setup does the rest (design D-6).

This module owns the network side and nothing else. It never reads or writes
`env/mrrc.env`: the wizard owns that, through the server's existing writer.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Uplink:
    """What the box currently has, as far as getting onto a network goes."""

    ethernet_link: bool
    wifi_connected: bool


def needs_hotspot(uplink: Uplink) -> bool:
    """Broadcast only when there is nothing else.

    Both conditions matter: a cable with no link is not an uplink, and an
    authenticated WiFi association is what makes the radio useless for an AP.
    """
    return not uplink.ethernet_link and not uplink.wifi_connected
