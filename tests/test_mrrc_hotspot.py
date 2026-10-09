"""The rule that decides whether the box should be broadcasting.

Pure function first, because the rule is the part worth arguing about and the
part the design states exactly: a hotspot appears when there is no usable
uplink. Everything that talks to NetworkManager comes later, and sits on top of
this.
"""
from __future__ import annotations

import unittest

from linux.mrrc_hotspot import Uplink, needs_hotspot


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
