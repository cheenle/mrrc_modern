#!/usr/bin/env python3
"""Keep an open setup hotspot up while the box has no way onto a network.

Why this exists: configuring the box needs a way *into* the box, and every way
in needs a network or a keyboard. Someone holding only a phone has neither, so
the box broadcasts one and the wizard at /setup does the rest (design D-6).

This module owns the network side and nothing else. It never reads or writes
`env/mrrc.env`: the wizard owns that, through the server's existing writer.
"""
from __future__ import annotations

import ipaddress
import subprocess
import time
from dataclasses import dataclass

#: The connection profile name. Stable on purpose: stopping the hotspot means
#: naming the same thing the start created.
HOTSPOT_CONNECTION = "mrrc-setup"
HOTSPOT_SSID = "MRRC-Setup"

#: NetworkManager's shared mode hands out this subnet, and the server's setup
#: gate imports this value rather than repeating it. Two literals would be two
#: things to keep in step, and the failure mode of drift is a gate that is
#: wider than the hotspot.
HOTSPOT_CIDR = ipaddress.ip_network("10.42.0.0/24")


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


@dataclass(frozen=True)
class NmcliRunnerResult:
    """What a command produced. A shape, so tests can hand one back."""

    stdout: str
    returncode: int


def _run(argv, **kwargs):  # pragma: no cover - replaced in every test
    """The only place in this repository a real NetworkManager is invoked."""
    done = subprocess.run(argv, capture_output=True, text=True, **kwargs)
    return NmcliRunnerResult(stdout=done.stdout, returncode=done.returncode)


class Network:
    """NetworkManager, narrowed to the three things this service does."""

    def __init__(self, runner=_run):
        self._run = runner

    def uplink(self) -> Uplink:
        """Read interface states; decide nothing.

        ``nmcli -t`` is colon-separated and these three fields never contain a
        colon, so splitting is enough.
        """
        out = self._run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"]).stdout
        link = wifi = False
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) < 3:
                continue
            kind, state = parts[1], parts[2]
            if kind == "ethernet" and state == "connected":
                link = True
            elif kind == "wifi" and state == "connected":
                wifi = True
        return Uplink(ethernet_link=link, wifi_connected=wifi)

    def start_hotspot(self) -> None:
        """Bring up an *open* access point.

        No password is passed and no ``wifi-sec.*`` keys are set, which is what
        makes it open. Leaving that to an omitted argument is the kind of
        default that changes quietly, so the test asserts on both spellings.
        """
        self._run([
            "nmcli", "device", "wifi", "hotspot",
            "con-name", HOTSPOT_CONNECTION,
            "ssid", HOTSPOT_SSID,
        ])

    def stop_hotspot(self) -> None:
        self._run(["nmcli", "connection", "down", HOTSPOT_CONNECTION])
