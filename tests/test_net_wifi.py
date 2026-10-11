"""The nmcli adapter: terse parsing and the station half.

Everything here runs without NetworkManager, without Linux and without a radio:
a canned runner answers every call and records the argv it was handed.

The half that is missing here — the uplink decision and the access-point
lifecycle — was retired with the setup hotspot on 2026-10-11; see
`test_setup_retired.py`. What remains is the adapter the LAN Wi-Fi page needs:
read the visible networks, join one, and read the address that resulted.
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


class ScanTests(unittest.TestCase):
    LISTING = "\n".join([
        r"Home\:5G:82:WPA2",     # an SSID containing a colon, escaped by nmcli
        "Office:41:WPA2",
        "Guest::",               # open network: empty SECURITY
        ":60:WPA2",              # hidden network: empty SSID
        r"Home\:5G:55:WPA2",    # the same SSID again, weaker
    ]) + "\n"

    def test_parses_dedupes_and_sorts_by_strength(self):
        run = FakeNmcli({"device wifi list": NmResult(0, self.LISTING)})
        found = net_wifi.scan_wifi(run)
        self.assertEqual([n["ssid"] for n in found], ["Home:5G", "Office", "Guest"])
        self.assertEqual(found[0]["signal"], 82,
                         "the stronger of two beacons for one SSID wins")

    def test_hidden_networks_are_dropped(self):
        """The page cannot offer a network it cannot name; an empty row would
        be a button that does nothing."""
        run = FakeNmcli({"device wifi list": NmResult(0, self.LISTING)})
        self.assertTrue(all(n["ssid"] for n in net_wifi.scan_wifi(run)))
        self.assertEqual(len(net_wifi.scan_wifi(run)), 3)

    def test_an_open_network_is_marked_unprotected(self):
        run = FakeNmcli({"device wifi list": NmResult(0, self.LISTING)})
        by_ssid = {n["ssid"]: n for n in net_wifi.scan_wifi(run)}
        self.assertFalse(by_ssid["Guest"]["protected"])
        self.assertTrue(by_ssid["Office"]["protected"])
        self.assertEqual(by_ssid["Office"]["security"], "WPA2")

    def test_a_scan_asks_for_a_rescan(self):
        """NM's cache is stale until a rescan; without --rescan the operator
        sees a blank list and concludes the radio is broken."""
        run = FakeNmcli({"device wifi list": NmResult(0, "")})
        net_wifi.scan_wifi(run)
        self.assertIn("--rescan yes", run.joined())

    def test_a_rescan_can_be_suppressed(self):
        run = FakeNmcli({"device wifi list": NmResult(0, "")})
        net_wifi.scan_wifi(run, rescan=False)
        self.assertNotIn("--rescan", run.joined())

    def test_a_failure_yields_an_empty_list(self):
        run = FakeNmcli({"device wifi list": NmResult(1, "", "no device")})
        self.assertEqual(net_wifi.scan_wifi(run), [])


class ConnectTests(unittest.TestCase):
    def test_argv_carries_the_ssid_and_the_psk_exactly_once(self):
        run = FakeNmcli({"device wifi connect": NmResult(0, "", "")})
        net_wifi.connect_wifi("Home", "hunter2hunter2", run, ifname="wlan0")
        joined = run.joined()
        self.assertEqual(joined.count("hunter2hunter2"), 1)
        self.assertIn("device wifi connect Home password hunter2hunter2", joined)
        self.assertIn("ifname wlan0", joined)

    def test_an_open_network_passes_no_password_argument(self):
        run = FakeNmcli({"device wifi connect": NmResult(0, "", "")})
        net_wifi.connect_wifi("Guest", "", run, ifname="wlan0")
        self.assertNotIn("password", run.joined())

    def test_a_wrong_psk_comes_back_as_a_failure_with_a_reason(self):
        run = FakeNmcli({"device wifi connect": NmResult(
            1, "", "Error: Connection activation failed: (7) Secrets were required")})
        result = net_wifi.connect_wifi("Home", "wrongwrong", run)
        self.assertFalse(result.ok)
        self.assertIn("Secrets were required", result.detail)

    def test_the_default_interface_is_used_when_none_is_given(self):
        run = FakeNmcli({"device wifi connect": NmResult(0, "", "")})
        net_wifi.connect_wifi("Home", "", run)
        self.assertIn(f"ifname {net_wifi.DEFAULT_IFNAME}", run.joined())


class AddressTests(unittest.TestCase):
    def test_reads_the_interface_address_with_its_prefix(self):
        run = FakeNmcli({"IP4.ADDRESS": NmResult(0, "10.42.0.1/24\n")})
        self.assertEqual(net_wifi.ipv4_address("wlan0", run), "10.42.0.1/24")

    def test_takes_the_first_line_when_several_come_back(self):
        run = FakeNmcli({"IP4.ADDRESS": NmResult(0, "10.0.0.7/24\n10.0.0.8/24\n")})
        self.assertEqual(net_wifi.ipv4_address("wlan0", run), "10.0.0.7/24")

    def test_no_address_yet(self):
        run = FakeNmcli({"IP4.ADDRESS": NmResult(0, "\n")})
        self.assertEqual(net_wifi.ipv4_address("wlan0", run), "")

    def test_a_failure_is_an_empty_address(self):
        run = FakeNmcli({"IP4.ADDRESS": NmResult(1, "", "no such device")})
        self.assertEqual(net_wifi.ipv4_address("wlan9", run), "")


class ScrubTests(unittest.TestCase):
    def test_removes_every_occurrence_of_every_secret(self):
        text = "tried hunter2hunter2 then failed (psk=hunter2hunter2)"
        cleaned = net_wifi.scrub(text, "hunter2hunter2")
        self.assertNotIn("hunter2hunter2", cleaned)
        self.assertEqual(cleaned.count(net_wifi.REDACTED), 2)

    def test_an_empty_secret_cannot_blank_the_whole_string(self):
        """str.replace(x, "") with x == "" would rebuild the string between
        every character; the guard is the `if secret`."""
        self.assertEqual(net_wifi.scrub("keep me", ""), "keep me")

    def test_none_is_tolerated(self):
        self.assertEqual(net_wifi.scrub(None, "x"), "")

    def test_several_secrets(self):
        cleaned = net_wifi.scrub("a=one b=two", "one", "two")
        self.assertNotIn("one", cleaned)
        self.assertNotIn("two", cleaned)


class SubprocessRunnerTests(unittest.TestCase):
    def test_a_missing_nmcli_is_a_result_not_an_exception(self):
        """macOS and Windows have no nmcli, and this module is frozen into both
        builds, so a call must degrade instead of raising inside a handler."""
        result = net_wifi.subprocess_runner(["definitely-not-a-real-binary-xyz"])
        self.assertFalse(result.ok)
        self.assertIn("not found", result.detail.lower())


if __name__ == "__main__":
    unittest.main()
