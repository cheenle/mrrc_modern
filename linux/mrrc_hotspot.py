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


DEFAULT_WINDOW_SECONDS = 30 * 60


class HotspotService:
    """One tick does one decision.

    Time is injected so the window can be tested without waiting for it, and
    the decisions are kept apart from the loop so that a failure to broadcast
    is a state this class reports rather than an exception nobody reads.
    """

    def __init__(self, net: "Network", now=time.monotonic,
                 window_seconds: int = DEFAULT_WINDOW_SECONDS):
        self._net = net
        self._now = now
        self._window = window_seconds
        self._up = False
        self._deadline = 0.0
        self._error: str | None = None

    @property
    def broadcasting(self) -> bool:
        return self._up

    def tick(self) -> None:
        uplink = self._net.uplink()
        if self._up:
            if not needs_hotspot(uplink) or self._now() >= self._deadline:
                self._net.stop_hotspot()
                self._up = False
            return
        if not needs_hotspot(uplink):
            return
        try:
            self._net.start_hotspot()
        except Exception as exc:
            # Report rather than retry invisibly. The AP mode of this radio is
            # the one premise that could not be checked without the hardware,
            # and the operator's next move is the HDMI console -- which they
            # can only make if the box says the radio refused.
            self._error = str(exc)
            return
        self._error = None
        self._up = True
        self._deadline = self._now() + self._window

    def status(self) -> dict:
        """What the management page shows: seconds left, not a timestamp, and
        the radio's own words if it refused."""
        return {
            "broadcasting": self._up,
            "error": self._error,
            "seconds_left": max(0, int(self._deadline - self._now())) if self._up else 0,
            "ssid": HOTSPOT_SSID if self._up else None,
        }
