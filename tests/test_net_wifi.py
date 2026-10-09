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

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        cfg = net_wifi.ap_settings({})
        self.assertEqual(cfg["ssid"], net_wifi.DEFAULT_SSID)
        self.assertEqual(cfg["ifname"], net_wifi.DEFAULT_IFNAME)
        self.assertEqual(cfg["web_port"], net_wifi.DEFAULT_WEB_PORT)
        self.assertEqual(cfg["state_dir"], net_wifi.DEFAULT_STATE_DIR)

    def test_environment_overrides(self):
        cfg = net_wifi.ap_settings({
            "MRRC_SETUP_AP_SSID": "My-Box",
            "MRRC_SETUP_AP_IFNAME": "wlan1",
            "MRRC_SETUP_AP_STATE_DIR": "/tmp/x",
            "MRRC_WEB_PORT": "9999",
        })
        self.assertEqual(cfg["ssid"], "My-Box")
        self.assertEqual(cfg["ifname"], "wlan1")
        self.assertEqual(cfg["state_dir"], Path("/tmp/x"))
        self.assertEqual(cfg["web_port"], 9999)

    def test_the_web_port_can_be_overridden_on_its_own(self):
        cfg = net_wifi.ap_settings({"MRRC_WEB_PORT": "8888",
                                    "MRRC_SETUP_AP_WEB_PORT": "9001"})
        self.assertEqual(cfg["web_port"], 9001)

    def test_an_empty_value_falls_back_to_the_default(self):
        cfg = net_wifi.ap_settings({"MRRC_SETUP_AP_SSID": "   ",
                                    "MRRC_SETUP_AP_STATE_DIR": ""})
        self.assertEqual(cfg["ssid"], net_wifi.DEFAULT_SSID)
        self.assertEqual(cfg["state_dir"], net_wifi.DEFAULT_STATE_DIR)

    def test_a_non_numeric_port_falls_back(self):
        self.assertEqual(net_wifi.ap_settings({"MRRC_WEB_PORT": "abc"})["web_port"],
                         net_wifi.DEFAULT_WEB_PORT)

    def test_it_reads_the_process_environment_by_default(self):
        """The server and the supervisor both call this with no argument, so the
        default has to be os.environ and not an empty mapping."""
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {"MRRC_SETUP_AP_SSID": "FromEnv"},
                             clear=False):
            self.assertEqual(net_wifi.ap_settings()["ssid"], "FromEnv")


class StateFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_round_trip(self):
        state = net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT, ssid="MRRC-Setup",
                                 gateway="10.42.0.1/24", network="10.42.0.0/24",
                                 url="https://10.42.0.1:8888/setup",
                                 reason="", since=100.0, heartbeat=200.0,
                                 deadline=300.0)
        net_wifi.write_state(state, self.dir)
        self.assertEqual(net_wifi.read_state(self.dir), state)

    def test_a_missing_file_reads_as_off(self):
        """No supervisor, no file ⇒ the gate must be closed, not an exception.
        This is the state of every desktop install."""
        state = net_wifi.read_state(self.dir / "nowhere")
        self.assertEqual(state.mode, net_wifi.MODE_OFF)
        self.assertFalse(net_wifi.state_is_live(state, 1.0))

    def test_garbage_reads_as_off(self):
        (self.dir / net_wifi.STATE_NAME).write_text("not json at all",
                                                    encoding="utf-8")
        self.assertEqual(net_wifi.read_state(self.dir).mode, net_wifi.MODE_OFF)

    def test_a_list_instead_of_an_object_reads_as_off(self):
        (self.dir / net_wifi.STATE_NAME).write_text("[1,2,3]", encoding="utf-8")
        self.assertEqual(net_wifi.read_state(self.dir).mode, net_wifi.MODE_OFF)

    def test_unknown_keys_are_ignored_and_missing_keys_defaulted(self):
        """A newer supervisor must not break an older server (or the reverse)."""
        (self.dir / net_wifi.STATE_NAME).write_text(
            json.dumps({"mode": "hotspot", "a_field_from_the_future": 1}),
            encoding="utf-8")
        state = net_wifi.read_state(self.dir)
        self.assertEqual(state.mode, net_wifi.MODE_HOTSPOT)
        self.assertEqual(state.ssid, "")

    def test_a_wrong_type_is_defaulted_not_raised(self):
        (self.dir / net_wifi.STATE_NAME).write_text(
            json.dumps({"mode": "hotspot", "heartbeat": "soon", "deadline": None}),
            encoding="utf-8")
        state = net_wifi.read_state(self.dir)
        self.assertEqual(state.heartbeat, 0.0)
        self.assertEqual(state.deadline, 0.0)

    def test_the_file_is_world_readable(self):
        """root writes it, `mrrc` reads it. A 0600 file from root's umask would
        make the gate silently unreadable — i.e. permanently closed, with
        nothing anywhere reporting why."""
        net_wifi.write_state(net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT), self.dir)
        mode = os.stat(self.dir / net_wifi.STATE_NAME).st_mode & 0o777
        self.assertEqual(mode, 0o644)

    def test_the_write_is_atomic(self):
        """A half-written state.json read by a concurrent request would be
        garbage, and garbage reads as `off` — the gate would flicker shut."""
        net_wifi.write_state(net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT), self.dir)
        self.assertFalse(list(self.dir.glob("*.tmp")),
                         "the temp file must be renamed away, not left behind")

    def test_an_unwritable_directory_does_not_raise(self):
        net_wifi.write_state(net_wifi.ApState(), Path("/definitely/not/writable"))

    def test_liveness_follows_the_heartbeat(self):
        state = net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT, heartbeat=100.0)
        self.assertTrue(net_wifi.state_is_live(state, 120.0))
        self.assertFalse(net_wifi.state_is_live(state, 100.0 + 46.0))

    def test_a_dead_supervisor_is_not_live_even_though_it_says_hotspot(self):
        state = net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT, heartbeat=0.0)
        self.assertFalse(net_wifi.state_is_live(state, 1.0))

    def test_any_other_mode_is_not_live(self):
        for mode in (net_wifi.MODE_OFF, net_wifi.MODE_STA, "nonsense", ""):
            with self.subTest(mode=mode):
                state = net_wifi.ApState(mode=mode, heartbeat=100.0)
                self.assertFalse(net_wifi.state_is_live(state, 100.0))


class WizardFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_round_trip(self):
        claim = net_wifi.WizardClaim(action="connect", ssid="Home",
                                     state=net_wifi.WIZARD_FAILED,
                                     error="Secrets were required",
                                     address="", nonce="abc123", heartbeat=50.0)
        net_wifi.write_wizard(claim, self.dir)
        self.assertEqual(net_wifi.read_wizard(self.dir), claim)

    def test_the_claim_has_no_password_field(self):
        """The PSK must have nowhere to be written, so "never on disk" is a
        property of the schema rather than a promise every caller keeps."""
        fields = set(net_wifi.WizardClaim.__dataclass_fields__)
        self.assertNotIn("password", fields)
        self.assertNotIn("psk", fields)
        self.assertNotIn("secret", fields)
        self.assertEqual(fields, {"action", "ssid", "state", "error", "address",
                                  "nonce", "heartbeat"})

    def test_a_missing_file_reads_as_no_claim(self):
        claim = net_wifi.read_wizard(self.dir)
        self.assertEqual(claim.action, "")
        self.assertFalse(net_wifi.claim_is_fresh(claim, 1.0))

    def test_garbage_reads_as_no_claim(self):
        (self.dir / net_wifi.WIZARD_NAME).write_text("{oops", encoding="utf-8")
        self.assertEqual(net_wifi.read_wizard(self.dir).action, "")

    def test_a_switch_in_flight_is_fresh(self):
        claim = net_wifi.WizardClaim(action="connect",
                                     state=net_wifi.WIZARD_SWITCHING,
                                     heartbeat=100.0)
        self.assertTrue(net_wifi.claim_is_fresh(claim, 130.0))

    def test_a_switch_that_stopped_heartbeating_goes_stale(self):
        """The server died mid-switch; the supervisor must take the radio back
        or the box is left with neither an AP nor an uplink, forever."""
        claim = net_wifi.WizardClaim(action="connect",
                                     state=net_wifi.WIZARD_SWITCHING,
                                     heartbeat=100.0)
        self.assertFalse(net_wifi.claim_is_fresh(claim, 100.0 + 46.0))

    def test_a_finished_claim_stays_actionable_longer(self):
        """It needs no heartbeat, and the supervisor must still see the failure
        in order to reopen the window."""
        claim = net_wifi.WizardClaim(action="connect",
                                     state=net_wifi.WIZARD_FAILED,
                                     heartbeat=100.0)
        self.assertTrue(net_wifi.claim_is_fresh(claim, 400.0))
        self.assertFalse(net_wifi.claim_is_fresh(claim, 100.0 + 601.0))

    def test_an_empty_action_is_not_a_claim(self):
        claim = net_wifi.WizardClaim(action="", state=net_wifi.WIZARD_OK,
                                     heartbeat=100.0)
        self.assertFalse(net_wifi.claim_is_fresh(claim, 100.0))


class ClientIpTests(unittest.TestCase):
    def test_a_plain_v4_address(self):
        import ipaddress
        self.assertEqual(net_wifi.client_ip("10.42.0.57"),
                         ipaddress.ip_address("10.42.0.57"))

    def test_an_ipv4_mapped_address_is_unwrapped(self):
        """uvicorn on a dual-stack socket reports an IPv4 client as
        `::ffff:10.42.0.57`. Compared against a v4 network that is False, so on
        a box bound to `::` the gate would never open — and nothing would log
        why, because every check "correctly" returned False."""
        import ipaddress
        self.assertEqual(net_wifi.client_ip("::ffff:10.42.0.57"),
                         ipaddress.ip_address("10.42.0.57"))

    def test_a_real_v6_address_is_preserved(self):
        import ipaddress
        self.assertEqual(net_wifi.client_ip("2001:db8::1"),
                         ipaddress.ip_address("2001:db8::1"))

    def test_nonsense_is_none(self):
        for host in ("", None, "   ", "not-an-ip", "10.42.0.57:8888"):
            with self.subTest(host=host):
                self.assertIsNone(net_wifi.client_ip(host))


class GateTests(unittest.TestCase):
    """Design D-6: passwordless ⟺ live hotspot AND the request came from it."""

    NOW = 1_000.0

    def live_state(self, network="10.42.0.0/24"):
        return net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT, ssid="MRRC-Setup",
                                gateway="10.42.0.1/24", network=network,
                                heartbeat=self.NOW - 5.0)

    def test_open_from_inside_the_hotspot_subnet(self):
        self.assertTrue(net_wifi.gate_is_open(self.live_state(), "10.42.0.57",
                                              self.NOW))

    def test_open_from_the_gateway_itself(self):
        self.assertTrue(net_wifi.gate_is_open(self.live_state(), "10.42.0.1",
                                              self.NOW))

    def test_closed_from_another_subnet(self):
        """The case that matters: the box gets a cable plugged in while a LAN
        client happens to be addressed 10.42.0.x."""
        self.assertFalse(net_wifi.gate_is_open(self.live_state(), "192.168.1.50",
                                               self.NOW))

    def test_closed_from_a_sibling_subnet_of_the_same_class(self):
        self.assertFalse(net_wifi.gate_is_open(self.live_state(), "10.43.0.57",
                                               self.NOW))

    def test_closed_when_the_supervisor_stopped_heartbeating(self):
        state = net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT,
                                 network="10.42.0.0/24", heartbeat=self.NOW - 99.0)
        self.assertFalse(net_wifi.gate_is_open(state, "10.42.0.57", self.NOW))

    def test_closed_when_the_mode_is_not_hotspot(self):
        for mode in (net_wifi.MODE_OFF, net_wifi.MODE_STA, ""):
            with self.subTest(mode=mode):
                state = net_wifi.ApState(mode=mode, network="10.42.0.0/24",
                                         heartbeat=self.NOW)
                self.assertFalse(net_wifi.gate_is_open(state, "10.42.0.57",
                                                       self.NOW))

    def test_closed_for_an_unknown_client(self):
        self.assertFalse(net_wifi.gate_is_open(self.live_state(), "", self.NOW))
        self.assertFalse(net_wifi.gate_is_open(self.live_state(), None, self.NOW))

    def test_closed_for_an_ipv6_client_against_a_v4_hotspot(self):
        """Must be False, and must not raise: `in` across versions is the kind
        of thing that turns a gate into a 500."""
        self.assertFalse(net_wifi.gate_is_open(self.live_state(), "2001:db8::1",
                                               self.NOW))

    def test_open_for_an_ipv4_mapped_client(self):
        self.assertTrue(net_wifi.gate_is_open(self.live_state(),
                                              "::ffff:10.42.0.57", self.NOW))

    def test_the_network_comes_from_the_state_not_from_a_constant(self):
        """NM hands out 10.42.0.0/24 by default but not by contract; a state
        that says otherwise must be believed."""
        state = self.live_state(network="10.99.0.0/24")
        self.assertFalse(net_wifi.gate_is_open(state, "10.42.0.57", self.NOW))
        self.assertTrue(net_wifi.gate_is_open(state, "10.99.0.7", self.NOW))

    def test_a_garbage_network_closes_the_gate(self):
        """Fail closed, not open.

        `ap_network()` normalises on the write side, so a garbage `network` can
        only mean a corrupt or hand-edited file — and malformed evidence must
        never be the thing that opens a passwordless door. (An *empty* one is
        different: that is a supervisor that has not read its gateway back yet,
        and the next test covers it.)
        """
        state = self.live_state(network="not-a-network")
        self.assertFalse(net_wifi.gate_is_open(state, "10.42.0.57", self.NOW))
        self.assertFalse(net_wifi.gate_is_open(state, "10.99.0.7", self.NOW))

    def test_an_unparseable_prefix_closes_the_gate(self):
        state = self.live_state(network="10.42.0.1/99")
        self.assertFalse(net_wifi.gate_is_open(state, "10.42.0.57", self.NOW))

    def test_an_empty_network_falls_back_to_the_nm_default(self):
        state = self.live_state(network="")
        self.assertTrue(net_wifi.gate_is_open(state, "10.42.0.57", self.NOW))

    def test_a_missing_state_file_closes_the_gate(self):
        self.assertFalse(net_wifi.gate_is_open(net_wifi.read_state(Path("/nope")),
                                               "10.42.0.57", self.NOW))

    def test_the_max_age_can_be_tightened(self):
        state = net_wifi.ApState(mode=net_wifi.MODE_HOTSPOT,
                                 network="10.42.0.0/24", heartbeat=self.NOW - 10.0)
        self.assertFalse(net_wifi.gate_is_open(state, "10.42.0.57", self.NOW,
                                               max_age=5.0))


class EnsureStateDirTests(unittest.TestCase):
    def test_creates_the_directory_group_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "setup-ap"
            net_wifi.ensure_state_dir(target)
            self.assertTrue(target.is_dir())
            mode = os.stat(target).st_mode & 0o777
            self.assertEqual(mode, 0o775,
                             "root writes state.json, `mrrc` writes wizard.json")

    def test_an_existing_directory_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            net_wifi.ensure_state_dir(Path(tmp))
            net_wifi.ensure_state_dir(Path(tmp))

    def test_a_directory_it_cannot_create_does_not_raise(self):
        """A desktop install has no /run/mrrc and no `mrrc` group; the gate just
        stays closed there rather than taking the server down."""
        net_wifi.ensure_state_dir(Path("/definitely/not/writable"))


class SubprocessRunnerTests(unittest.TestCase):
    def test_a_missing_nmcli_is_a_result_not_an_exception(self):
        """macOS and Windows have no nmcli, and this module is frozen into both
        builds, so a call must degrade instead of raising inside a handler."""
        result = net_wifi.subprocess_runner(["definitely-not-a-real-binary-xyz"])
        self.assertFalse(result.ok)
        self.assertIn("not found", result.detail.lower())


if __name__ == "__main__":
    unittest.main()
