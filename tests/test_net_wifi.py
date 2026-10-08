"""The nmcli adapter: parsing, the uplink decision, and what must not count.

Everything here runs without NetworkManager, without Linux and without a radio:
a canned runner answers every call and records the argv it was handed.

The single most important assertion in this file is
`UplinkTests.test_our_own_hotspot_is_not_an_uplink`. In `nmcli device status`
an AP-mode wlan0 reports state `connected` exactly like a station does, so a
supervisor that trusts that column raises the hotspot and tears it down again
on the very next tick — a box that broadcasts an open network for a few seconds
every five seconds, forever.
"""
from __future__ import annotations

import unittest

import net_wifi
from net_wifi import NmResult


class FakeNmcli:
    """Canned nmcli.

    `table` maps a *substring of the joined argv* to a result and is scanned in
    insertion order, so put the more specific key first
    ("-f NAME,TYPE connection show --active" before "connection show").
    """

    def __init__(self, table=None, default=None):
        self.calls: list = []
        self.table = list((table or {}).items())
        self.default = default if default is not None else NmResult(0, "", "")

    def __call__(self, argv):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for key, value in self.table:
            if key in joined:
                return value
        return self.default

    def count(self, key: str) -> int:
        return sum(1 for call in self.calls if key in " ".join(call))

    def joined(self) -> str:
        return "\n".join(" ".join(call) for call in self.calls)


def device_rows(*rows) -> str:
    """Terse `nmcli device status` output from (device, type, state, conn)."""
    return "\n".join(":".join(row) for row in rows) + "\n"


class ParseTests(unittest.TestCase):
    def test_splits_on_unescaped_colons(self):
        self.assertEqual(net_wifi.parse_row("wlan0:wifi:connected:Home"),
                         ["wlan0", "wifi", "connected", "Home"])

    def test_keeps_an_escaped_colon_inside_a_field(self):
        """An SSID may contain ':' — nmcli escapes it, a naive split loses it
        and shifts every following column into the wrong field."""
        self.assertEqual(net_wifi.parse_row(r"Cafe\:Net:72:WPA2"),
                         ["Cafe:Net", "72", "WPA2"])

    def test_keeps_an_escaped_backslash(self):
        self.assertEqual(net_wifi.parse_row(r"back\\slash:1"), ["back\\slash", "1"])

    def test_empty_trailing_field_survives(self):
        """A device with no connection prints an empty last column."""
        self.assertEqual(net_wifi.parse_row("eth0:ethernet:unavailable:"),
                         ["eth0", "ethernet", "unavailable", ""])

    def test_rows_skips_blank_lines(self):
        self.assertEqual(net_wifi.parse_rows("a:b\n\n   \nc:d\n"),
                         [["a", "b"], ["c", "d"]])

    def test_rows_of_nothing_is_nothing(self):
        self.assertEqual(net_wifi.parse_rows(""), [])
        self.assertEqual(net_wifi.parse_rows(None), [])


class UsableUplinkTests(unittest.TestCase):
    def test_the_pretty_vocabulary_counts(self):
        self.assertTrue(net_wifi._is_usable_uplink("connected"))

    def test_the_terse_vocabulary_also_counts(self):
        """Some NM builds print `activated` where others print `connected`."""
        self.assertTrue(net_wifi._is_usable_uplink("activated"))

    def test_site_only_is_not_an_uplink(self):
        """Link-local only — a cable into a dead switch. No gateway, no DNS,
        so this is precisely the situation the hotspot exists for."""
        self.assertFalse(net_wifi._is_usable_uplink("connected (site only)"))

    def test_the_states_that_mean_no_network(self):
        for state in ("unavailable", "disconnected", "failed", "unmanaged",
                      "unknown", "prepare", "ip-config", "need-auth", ""):
            with self.subTest(state=state):
                self.assertFalse(net_wifi._is_usable_uplink(state))

    def test_case_and_padding_do_not_matter(self):
        self.assertTrue(net_wifi._is_usable_uplink("  Connected  "))

    def test_none_does_not_raise(self):
        self.assertFalse(net_wifi._is_usable_uplink(None))


class UplinkTests(unittest.TestCase):
    def test_wired_link_is_an_uplink(self):
        run = FakeNmcli({
            "connection show --active": NmResult(0, ""),
            "device status": NmResult(0, device_rows(
                ("eth0", "ethernet", "connected", "Wired connection 1"),
                ("wlan0", "wifi", "disconnected", ""))),
        })
        self.assertTrue(net_wifi.has_uplink(run))

    def test_nothing_connected_is_not_an_uplink(self):
        run = FakeNmcli({
            "connection show --active": NmResult(0, ""),
            "device status": NmResult(0, device_rows(
                ("eth0", "ethernet", "unavailable", ""),
                ("wlan0", "wifi", "disconnected", ""))),
        })
        self.assertFalse(net_wifi.has_uplink(run))

    def test_our_own_hotspot_is_not_an_uplink(self):
        """The bug this module exists to avoid (see the file docstring)."""
        run = FakeNmcli({
            "connection show --active": NmResult(0, "Hotspot:802-11-wireless\n"),
            "802-11-wireless.mode": NmResult(0, "ap\n"),
            "device status": NmResult(0, device_rows(
                ("eth0", "ethernet", "unavailable", ""),
                ("wlan0", "wifi", "connected", "Hotspot"))),
        })
        self.assertFalse(net_wifi.has_uplink(run))

    def test_a_station_wifi_is_an_uplink(self):
        run = FakeNmcli({
            "connection show --active": NmResult(0, "Home:802-11-wireless\n"),
            "802-11-wireless.mode": NmResult(0, "infrastructure\n"),
            "device status": NmResult(0, device_rows(
                ("eth0", "ethernet", "unavailable", ""),
                ("wlan0", "wifi", "connected", "Home"))),
        })
        self.assertTrue(net_wifi.has_uplink(run))

    def test_a_cable_with_no_dhcp_is_not_an_uplink(self):
        run = FakeNmcli({
            "connection show --active": NmResult(0, ""),
            "device status": NmResult(0, device_rows(
                ("eth0", "ethernet", "connected (site only)",
                 "Wired connection 1"))),
        })
        self.assertFalse(net_wifi.has_uplink(run))

    def test_nmcli_failing_means_no_uplink_not_a_crash(self):
        run = FakeNmcli(default=NmResult(7, "", "nmcli broke"))
        self.assertFalse(net_wifi.has_uplink(run))

    def test_non_network_interfaces_are_ignored(self):
        """A loopback or a docker bridge being 'connected' says nothing about
        whether a phone can reach the box."""
        run = FakeNmcli({
            "connection show --active": NmResult(0, ""),
            "device status": NmResult(0, device_rows(
                ("lo", "loopback", "connected", "lo"),
                ("docker0", "bridge", "connected", "docker0"))),
        })
        self.assertFalse(net_wifi.has_uplink(run))


class TransitionalTests(unittest.TestCase):
    def test_still_working_counts(self):
        for state in ("prepare", "config", "need-auth", "ip-config",
                      "connecting", "deactivating"):
            with self.subTest(state=state):
                run = FakeNmcli({"device status": NmResult(
                    0, device_rows(("wlan0", "wifi", state, "")))})
                self.assertTrue(net_wifi.any_device_connecting(run))

    def test_settled_does_not_count(self):
        run = FakeNmcli({"device status": NmResult(0, device_rows(
            ("eth0", "ethernet", "unavailable", ""),
            ("wlan0", "wifi", "disconnected", "")))})
        self.assertFalse(net_wifi.any_device_connecting(run))


class GeneralStatusTests(unittest.TestCase):
    def test_running(self):
        run = FakeNmcli({"general": NmResult(0, "running\n")})
        self.assertTrue(net_wifi.nm_running(run))

    def test_not_running(self):
        run = FakeNmcli({"general": NmResult(0, "starting\n")})
        self.assertFalse(net_wifi.nm_running(run))

    def test_a_failure_is_not_running(self):
        run = FakeNmcli({"general": NmResult(7, "", "no daemon")})
        self.assertFalse(net_wifi.nm_running(run))


class DeviceQueryTests(unittest.TestCase):
    def test_the_wifi_interface_is_found_by_type_not_by_name(self):
        """A board may call it wlan1; the TYPE column is the authority."""
        run = FakeNmcli({"device status": NmResult(0, device_rows(
            ("eth0", "ethernet", "unavailable", ""),
            ("wlan1", "wifi", "disconnected", "")))})
        self.assertEqual(net_wifi.wifi_interface(run), "wlan1")

    def test_no_wifi_device_returns_empty(self):
        run = FakeNmcli({"device status": NmResult(0, device_rows(
            ("eth0", "ethernet", "connected", "wired")))})
        self.assertEqual(net_wifi.wifi_interface(run), "")


class NmResultTests(unittest.TestCase):
    def test_ok_follows_the_return_code(self):
        self.assertTrue(NmResult(0, "", "").ok)
        self.assertFalse(NmResult(1, "", "").ok)

    def test_detail_prefers_stderr_then_stdout_then_the_code(self):
        self.assertEqual(NmResult(1, "out", "err").detail, "err")
        self.assertEqual(NmResult(1, "  out\n", "").detail, "out")
        self.assertEqual(NmResult(3, "", "").detail, "exit 3")


class SubprocessRunnerTests(unittest.TestCase):
    def test_a_missing_nmcli_is_a_result_not_an_exception(self):
        """macOS and Windows have no nmcli, and this module is frozen into both
        builds, so a call must degrade instead of raising inside a handler."""
        result = net_wifi.subprocess_runner(["definitely-not-a-real-binary-xyz"])
        self.assertFalse(result.ok)
        self.assertIn("not found", result.detail.lower())


if __name__ == "__main__":
    unittest.main()
