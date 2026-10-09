"""The rule that decides whether the box should be broadcasting.

Pure function first, because the rule is the part worth arguing about and the
part the design states exactly: a hotspot appears when there is no usable
uplink. Everything that talks to NetworkManager comes later, and sits on top of
this.
"""
from __future__ import annotations

import unittest

from linux.mrrc_hotspot import (
    HOTSPOT_CONNECTION,
    Network,
    NmcliRunnerResult,
    Uplink,
    needs_hotspot,
)


class NeedsHotspotTests(unittest.TestCase):
    def test_no_cable_and_no_wifi_means_hotspot(self):
        self.assertTrue(needs_hotspot(Uplink(ethernet_link=False, wifi_connected=False)))

    def test_a_cable_switches_it_off(self):
        self.assertFalse(needs_hotspot(Uplink(ethernet_link=True, wifi_connected=False)))

    def test_joined_wifi_switches_it_off(self):
        self.assertFalse(needs_hotspot(Uplink(ethernet_link=False, wifi_connected=True)))

    def test_having_both_is_still_off(self):
        self.assertFalse(needs_hotspot(Uplink(ethernet_link=True, wifi_connected=True)))

    def test_a_cable_without_a_link_is_not_an_uplink(self):
        """Interface state matters, not the presence of a cable: a plugged-in
        cable on a dead switch is exactly the case that would otherwise leave
        someone with no way in."""
        self.assertTrue(needs_hotspot(Uplink(ethernet_link=False, wifi_connected=False)))


if __name__ == "__main__":
    unittest.main()


class NmcliTests(unittest.TestCase):
    """Every call into NetworkManager goes through an injected runner.

    Tests never touch the real NetworkManager, and the assertions are on the
    argv, because the argv is what decides whether the AP comes up encrypted.
    """

    def _recording(self, stdout=""):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return NmcliRunnerResult(stdout=stdout, returncode=0)

        return calls, runner

    def test_a_connected_cable_counts_as_an_uplink(self):
        _calls, runner = self._recording("eth0:ethernet:connected\n")
        self.assertEqual(Network(runner).uplink(), Uplink(ethernet_link=True, wifi_connected=False))

    def test_a_joined_network_counts_as_an_uplink(self):
        _calls, runner = self._recording("wlan0:wifi:connected\n")
        self.assertEqual(Network(runner).uplink(), Uplink(ethernet_link=False, wifi_connected=True))

    def test_a_cable_with_no_carrier_is_not_an_uplink(self):
        """The interface is present and unplugged, which is exactly the case
        that would otherwise leave someone with no way in."""
        _calls, runner = self._recording("eth0:ethernet:unavailable\n")
        self.assertEqual(Network(runner).uplink(), Uplink(ethernet_link=False, wifi_connected=False))

    def test_nothing_connected_is_no_uplink(self):
        _calls, runner = self._recording("eth0:ethernet:disconnected\nwlan0:wifi:disconnected\n")
        self.assertEqual(Network(runner).uplink(), Uplink(ethernet_link=False, wifi_connected=False))

    def test_hotspot_is_started_open(self):
        """No password argument, deliberately: the operator is not asked to
        guess one before they can get in, and the wizard is the gate instead
        (design D-6)."""
        calls, runner = self._recording()
        Network(runner).start_hotspot()
        argv = " ".join(calls[0])
        self.assertIn("hotspot", argv)
        self.assertNotIn("password", argv)
        self.assertNotIn("wifi-sec", argv)

    def test_the_hotspot_has_a_stable_name_so_it_can_be_stopped(self):
        calls, runner = self._recording()
        net = Network(runner)
        net.start_hotspot()
        net.stop_hotspot()
        self.assertIn(HOTSPOT_CONNECTION, " ".join(calls[0]))
        self.assertIn(HOTSPOT_CONNECTION, " ".join(calls[1]))

    def test_hotspot_can_be_stopped(self):
        calls, runner = self._recording()
        Network(runner).stop_hotspot()
        self.assertIn("down", " ".join(calls[0]))
