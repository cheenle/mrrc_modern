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


# `FakeNmcli` matches by substring, and both of these queries contain
# "connection show --active", so the keys carry the `-f` column list to keep
# them apart: `_active_uuids` asks for UUID, `active_ap_connections` for NAME,TYPE.
ACTIVE_AP_TABLE = {
    "-f NAME,TYPE connection show --active": NmResult(0, "Hotspot:802-11-wireless\n"),
    "-f UUID connection show --active": NmResult(0, "aaaa-1111\n"),
    "802-11-wireless.mode": NmResult(0, "ap\n"),
}
ACTIVE_STA_TABLE = {
    "-f NAME,TYPE connection show --active": NmResult(0, "Home:802-11-wireless\n"),
    "-f UUID connection show --active": NmResult(0, "bbbb-2222\n"),
    "802-11-wireless.mode": NmResult(0, "infrastructure\n"),
}


class ApModeTests(unittest.TestCase):
    def test_an_ap_connection_is_recognised(self):
        run = FakeNmcli(ACTIVE_AP_TABLE)
        self.assertEqual(net_wifi.active_ap_connections(run), ["Hotspot"])
        self.assertTrue(net_wifi.hotspot_active(run))

    def test_a_station_connection_is_not_an_ap(self):
        """TYPE is 802-11-wireless for both; only the mode tells them apart."""
        run = FakeNmcli(ACTIVE_STA_TABLE)
        self.assertEqual(net_wifi.active_ap_connections(run), [])
        self.assertFalse(net_wifi.hotspot_active(run))

    def test_nothing_active(self):
        run = FakeNmcli({"-f NAME,TYPE connection show --active": NmResult(0, "")})
        self.assertFalse(net_wifi.hotspot_active(run))

    def test_an_empty_connection_name_is_not_queried(self):
        """A row whose NAME column is blank must not trigger a mode lookup with
        an empty name — that asks NM for a connection called "" and fails."""
        run = FakeNmcli({
            "-f NAME,TYPE connection show --active": NmResult(
                0, ":802-11-wireless\n"),
        })
        self.assertEqual(net_wifi.active_ap_connections(run), [])
        self.assertEqual(run.count("802-11-wireless.mode"), 0)


class StartHotspotTests(unittest.TestCase):
    def test_asks_nmcli_for_an_open_hotspot(self):
        """Design D-2: `nmcli device wifi hotspot`, and *no* password argument.

        Omitting `password` is what makes the network open; passing one would
        create a WPA network whose key nobody has been told.
        """
        run = FakeNmcli({
            "-f NAME,TYPE connection show --active": NmResult(0, ""),
            "-f UUID connection show --active": NmResult(0, ""),
            "-f UUID,NAME connection show": NmResult(0, ""),
            "device wifi hotspot": NmResult(0, "ok", ""),
        })
        result = net_wifi.start_hotspot("MRRC-Setup", "wlan0", run)
        self.assertTrue(result.ok)
        self.assertEqual(run.count("device wifi hotspot"), 1)
        self.assertIn("ifname wlan0 ssid MRRC-Setup", run.joined())
        self.assertNotIn("password", run.joined())

    def test_an_already_running_hotspot_is_left_alone(self):
        """Restarting a live AP would drop the operator mid-wizard."""
        run = FakeNmcli(dict(ACTIVE_AP_TABLE))
        result = net_wifi.start_hotspot("MRRC-Setup", "wlan0", run)
        self.assertTrue(result.ok)
        self.assertEqual(run.count("device wifi hotspot"), 0)

    def test_a_stale_profile_of_the_same_name_is_removed_first(self):
        """NM ids are not unique: without this, every raise leaves a twin behind
        and `nmcli connection show` fills up with dead Hotspot entries."""
        run = FakeNmcli({
            "-f NAME,TYPE connection show --active": NmResult(0, ""),
            "-f UUID connection show --active": NmResult(0, "live-9999\n"),
            "-f UUID,NAME connection show": NmResult(
                0, "aaaa-1111:Hotspot\nbbbb-2222:Hotspot\ncccc-3333:Home\n"),
            "device wifi hotspot": NmResult(0, "ok", ""),
        })
        net_wifi.start_hotspot("MRRC-Setup", "wlan0", run)
        self.assertEqual(run.count("connection delete aaaa-1111"), 1)
        self.assertEqual(run.count("connection delete bbbb-2222"), 1)
        self.assertEqual(run.count("connection delete cccc-3333"), 0,
                         "only the hotspot's own id may be touched")

    def test_an_active_profile_is_never_deleted(self):
        """Deleting the profile that carries the live AP cuts the operator off.
        The early return has to come first — and it has to come before the
        profile listing, not merely before the delete."""
        run = FakeNmcli(dict(ACTIVE_AP_TABLE, **{
            "-f UUID,NAME connection show": NmResult(0, "aaaa-1111:Hotspot\n"),
            "device wifi hotspot": NmResult(0, "ok", ""),
        }))
        net_wifi.start_hotspot("MRRC-Setup", "wlan0", run)
        self.assertEqual(run.count("connection delete"), 0)
        self.assertEqual(run.count("-f UUID,NAME connection show"), 0)

    def test_a_profile_that_is_up_under_a_different_query_is_still_spared(self):
        """Belt and braces: even if the AP check somehow missed it, a uuid that
        `_active_uuids` reports as live is never deleted."""
        run = FakeNmcli({
            "-f NAME,TYPE connection show --active": NmResult(0, ""),
            "-f UUID connection show --active": NmResult(0, "aaaa-1111\n"),
            "-f UUID,NAME connection show": NmResult(
                0, "aaaa-1111:Hotspot\nbbbb-2222:Hotspot\n"),
            "device wifi hotspot": NmResult(0, "ok", ""),
        })
        net_wifi.start_hotspot("MRRC-Setup", "wlan0", run)
        self.assertEqual(run.count("connection delete aaaa-1111"), 0)
        self.assertEqual(run.count("connection delete bbbb-2222"), 1)

    def test_a_failure_is_reported_not_raised(self):
        run = FakeNmcli({
            "-f NAME,TYPE connection show --active": NmResult(0, ""),
            "-f UUID connection show --active": NmResult(0, ""),
            "-f UUID,NAME connection show": NmResult(0, ""),
            "device wifi hotspot": NmResult(
                1, "", "Error: Device not suitable for hotspot mode"),
        })
        result = net_wifi.start_hotspot("MRRC-Setup", "wlan0", run)
        self.assertFalse(result.ok)
        self.assertIn("not suitable", result.detail)

    def test_the_defaults_are_used_when_nothing_is_passed(self):
        run = FakeNmcli({
            "-f NAME,TYPE connection show --active": NmResult(0, ""),
            "-f UUID connection show --active": NmResult(0, ""),
            "-f UUID,NAME connection show": NmResult(0, ""),
            "device wifi hotspot": NmResult(0, "ok", ""),
        })
        net_wifi.start_hotspot(runner=run)
        self.assertIn(f"ifname {net_wifi.DEFAULT_IFNAME} ssid {net_wifi.DEFAULT_SSID}",
                      run.joined())


class StopHotspotTests(unittest.TestCase):
    def test_takes_the_ap_down_by_name(self):
        run = FakeNmcli(dict(ACTIVE_AP_TABLE))
        result = net_wifi.stop_hotspot(run)
        self.assertTrue(result.ok)
        self.assertEqual(run.count("connection down Hotspot"), 1)

    def test_nothing_up_is_success(self):
        """Idempotent: a stop on an already-stopped AP must not look like an
        error, or the supervisor would log a failure on every tick."""
        run = FakeNmcli({"-f NAME,TYPE connection show --active": NmResult(0, "")})
        self.assertTrue(net_wifi.stop_hotspot(run).ok)
        self.assertEqual(run.count("connection down"), 0)

    def test_a_station_connection_is_not_torn_down(self):
        """Stopping the hotspot must never drop the operator's real WiFi."""
        run = FakeNmcli(dict(ACTIVE_STA_TABLE))
        net_wifi.stop_hotspot(run)
        self.assertEqual(run.count("connection down"), 0)


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
        """The wizard cannot offer a network it cannot name; an empty row would
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
        """The AP just came up, so NM's cache is empty; without --rescan the
        operator sees a blank list and concludes the radio is broken."""
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

    def test_network_of_an_address(self):
        self.assertEqual(net_wifi.ap_network("10.42.0.1/24"), "10.42.0.0/24")

    def test_garbage_falls_back_to_the_nm_shared_subnet(self):
        """The gate compares against this; an unparseable address must not turn
        into an exception inside a request handler."""
        self.assertEqual(net_wifi.ap_network(""), net_wifi.NM_SHARED_SUBNET)
        self.assertEqual(net_wifi.ap_network("not-an-address"),
                         net_wifi.NM_SHARED_SUBNET)
        self.assertEqual(net_wifi.ap_network(None), net_wifi.NM_SHARED_SUBNET)

    def test_setup_url_strips_the_prefix_and_defaults_the_gateway(self):
        self.assertEqual(net_wifi.setup_url("10.42.0.1/24", 8888),
                         "https://10.42.0.1:8888/setup")
        self.assertEqual(net_wifi.setup_url("", 8888),
                         f"https://{net_wifi.DEFAULT_GATEWAY}:8888/setup")
        self.assertEqual(net_wifi.setup_url("192.168.9.1/24"),
                         "https://192.168.9.1:8888/setup")


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
