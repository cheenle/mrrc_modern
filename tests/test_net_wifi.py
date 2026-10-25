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
    """Joining must not require *seeing* the network.

    The body used to be `nmcli device wifi connect <SSID>`, which is a scan
    shortcut: nmcli looks the SSID up in the scan list, refuses when it is absent
    ("no network with SSID …") and writes **no profile at all**. Both jobs this
    function has fail on that — pre-configuring the Wi-Fi of a box that is going
    to someone else, whose AP is only at their home, and joining a hidden AP — so
    the profile is written and brought up instead (2026-10-17, first real-box
    preparation for a customer).

    Measured on W103D hardware: the old body against an unseen SSID printed
    `未发现 SSID 为 … 的网络` and left the profile count unchanged; the class of
    profile these tests build joined a visible AP in ~5 s with `hidden=yes` set,
    so the flag covers both cases instead of choosing one.
    """

    def join(self, ssid="Home", password="hunter2hunter2", **kwargs):
        run = FakeNmcli()
        return run, net_wifi.connect_wifi(ssid, password, run, **kwargs)

    def test_never_asks_the_scan_for_the_network(self):
        """The regression itself: an SSID that is not in range still configures."""
        run, result = self.join()
        joined = run.joined()
        self.assertNotIn("device wifi connect", joined)
        self.assertNotIn("device wifi list", joined)
        self.assertTrue(result.ok)

    def test_writes_the_profile_then_brings_it_up(self):
        run, _ = self.join(ifname="wlan0")
        joined = run.joined()
        self.assertIn("connection add type wifi con-name Home ifname wlan0 "
                      "ssid Home", joined)
        self.assertIn("connection up Home", joined)

    def test_a_stale_profile_of_the_same_name_is_removed_first(self):
        """Idempotence: a retry must not inherit half-written fields, and
        `connection add` refuses a name that already exists."""
        run, _ = self.join()
        joined = run.joined()
        self.assertIn("connection delete Home", joined)
        self.assertLess(joined.index("connection delete Home"),
                        joined.index("connection add type wifi"))

    def test_the_profile_is_marked_hidden(self):
        """A hidden AP is never in a scan list, and this flag is harmless for one
        that broadcasts — that is what makes a single saved profile work at the
        customer's site either way."""
        run, _ = self.join()
        self.assertIn("802-11-wireless.hidden yes", run.joined())

    def test_the_band_is_pinned_to_2_4_ghz(self):
        """5 GHz associates on this chip and then wedges it; the page already
        promises 2.4 GHz only."""
        run, _ = self.join()
        self.assertIn("802-11-wireless.band bg", run.joined())

    def test_autoconnect_is_stated_and_retries_are_unlimited(self):
        """An explicit `autoconnect yes` (the retired hotspot's profile defaulted
        to yes by accident, and 'by default' is not a design), and 0 = nmcli's
        'unlimited' so a network that appears later is still picked up."""
        run, _ = self.join()
        joined = run.joined()
        self.assertIn("connection.autoconnect yes", joined)
        self.assertIn("connection.autoconnect-retries 0", joined)

    def test_the_psk_rides_in_exactly_one_argument(self):
        run, _ = self.join()
        self.assertEqual(run.joined().count("hunter2hunter2"), 1)
        self.assertIn("802-11-wireless-security.psk hunter2hunter2",
                      run.joined())

    def test_an_open_network_declares_no_security(self):
        run, _ = self.join(password="")
        joined = run.joined()
        self.assertNotIn("psk", joined)
        self.assertNotIn("key-mgmt", joined)

    def test_a_refused_profile_never_reaches_the_device(self):
        run = FakeNmcli({"connection add": NmResult(1, "", "Error: bad ssid")})
        result = net_wifi.connect_wifi("Home", "hunter2hunter2", run)
        self.assertFalse(result.ok)
        self.assertNotIn("connection up", run.joined())

    def test_a_refused_modify_never_reaches_the_device(self):
        run = FakeNmcli({"connection modify": NmResult(
            1, "", "Error: invalid property")})
        result = net_wifi.connect_wifi("Home", "hunter2hunter2", run)
        self.assertFalse(result.ok)
        self.assertNotIn("connection up", run.joined())

    def test_the_up_is_bounded(self):
        """An absent SSID makes `connection up` wait out nmcli's own 90 s default;
        the switch has a 30 s address budget behind this call."""
        run, _ = self.join()
        ups = [call for call in run.calls if call[-2:] == ["up", "Home"]]
        self.assertEqual(len(ups), 1)
        self.assertIn("-w", ups[0])
        self.assertEqual(ups[0][ups[0].index("-w") + 1],
                         str(net_wifi.NM_CONNECT_WAIT_S))

    def test_a_wrong_psk_comes_back_as_a_failure_with_a_reason(self):
        run = FakeNmcli({"connection up": NmResult(
            1, "", "Error: Connection activation failed: (7) Secrets were required")})
        result = net_wifi.connect_wifi("Home", "wrongwrong", run)
        self.assertFalse(result.ok)
        self.assertIn("Secrets were required", result.detail)

    def test_the_default_interface_is_used_when_none_is_given(self):
        run, _ = self.join(password="")
        self.assertIn(f"ifname {net_wifi.DEFAULT_IFNAME}", run.joined())


class AccessPointAbsentTests(unittest.TestCase):
    """「这个 AP 不在范围内」与「密码被拒」必须分得开。

    前者是给客户预置时的**正常结果**——她的 AP 只在她家里；后者重试一万次也不会成功。
    `box_ops._roll_back` 按这个判断要不要把刚落地的 profile 停用（2026-10-17）。
    """

    def test_a_wait_that_expires_means_absent(self):
        self.assertTrue(net_wifi.access_point_absent("错误：超时时间已到（25 秒）"))

    def test_the_english_timeout_means_absent(self):
        self.assertTrue(net_wifi.access_point_absent("Error: Timeout was reached"))

    def test_no_network_with_that_ssid_means_absent(self):
        self.assertTrue(net_wifi.access_point_absent(
            "Error: No network with SSID 'Clinic-WiFi' found."))
        self.assertTrue(net_wifi.access_point_absent(
            '错误：未发现 SSID 为 "Clinic-WiFi" 的网络。'))

    def test_a_rejected_secret_is_not_absent(self):
        self.assertFalse(net_wifi.access_point_absent(
            "Error: Connection activation failed: (7) Secrets were required"))

    def test_nothing_to_read_is_not_absent(self):
        self.assertFalse(net_wifi.access_point_absent(""))
        self.assertFalse(net_wifi.access_point_absent(None))


class FallbackDnsTests(unittest.TestCase):
    """Every saved profile carries the two shipped fallback resolvers.

    2026-10-25, a shipped box (BG8GGG): the Wi-Fi came up before DHCP finished, the
    resolver was empty, `chronyc` could not resolve its NTP pool name, the clock
    stayed 24 days behind the image date, and every outbound HTTPS call failed with
    `certificate is not yet valid` while the page said only "连不上门户". A profile
    that answers names on its own removes the root of that cascade.
    """

    WIFI_ROW = "Home:802-11-wireless\nlo:loopback\n"

    def test_a_new_profile_is_born_with_the_fallback_resolvers(self):
        """Written at profile creation, so a network joined today is covered without
        waiting for the next restart."""
        run = FakeNmcli()
        net_wifi.write_profile("Home", "hunter2hunter2", run)
        self.assertIn("ipv4.dns 114.114.114.114 8.8.8.8", run.joined())

    def test_an_existing_profile_without_dns_gets_them(self):
        run = FakeNmcli({
            "-g ipv4.dns connection show Home": NmResult(0, "\n"),
            "-t -f NAME,TYPE connection show": NmResult(0, self.WIFI_ROW),
        })
        self.assertEqual(net_wifi.ensure_fallback_dns(run), ["Home"])
        self.assertIn("connection modify Home ipv4.dns 114.114.114.114 8.8.8.8",
                      run.joined())

    def test_an_operators_own_dns_is_kept_and_the_fallback_appended(self):
        """Replacing a hand-typed resolver would be a silent behaviour change on a
        customer's box; appending is the only safe direction."""
        run = FakeNmcli({
            "-g ipv4.dns connection show Home": NmResult(0, "192.168.1.1\n"),
            "-t -f NAME,TYPE connection show": NmResult(0, self.WIFI_ROW),
        })
        net_wifi.ensure_fallback_dns(run)
        self.assertIn("ipv4.dns 192.168.1.1 114.114.114.114 8.8.8.8", run.joined())

    def test_a_profile_that_already_has_them_is_not_touched(self):
        """Idempotence is what makes this safe to run at every start-up."""
        run = FakeNmcli({
            "-g ipv4.dns connection show Home": NmResult(0, "114.114.114.114,8.8.8.8\n"),
            "-t -f NAME,TYPE connection show": NmResult(0, self.WIFI_ROW),
        })
        self.assertEqual(net_wifi.ensure_fallback_dns(run), [])
        self.assertEqual(run.count("connection modify"), 0)

    def test_loopback_and_other_non_network_profiles_are_left_alone(self):
        run = FakeNmcli({
            "-g ipv4.dns connection show": NmResult(0, "\n"),
            "-t -f NAME,TYPE connection show": NmResult(0, "lo:loopback\nbr0:bridge\n"),
        })
        self.assertEqual(net_wifi.ensure_fallback_dns(run), [])
        self.assertEqual(run.count("connection modify"), 0)

    def test_a_refused_modify_is_not_reported_as_changed(self):
        """The caller logs the return value; a failure must not read as success."""
        run = FakeNmcli({
            "-g ipv4.dns connection show Home": NmResult(0, "\n"),
            "connection modify": NmResult(1, "", "not authorized"),
            "-t -f NAME,TYPE connection show": NmResult(0, self.WIFI_ROW),
        })
        self.assertEqual(net_wifi.ensure_fallback_dns(run), [])

    def test_no_nmcli_at_all_is_an_empty_result(self):
        """Windows/macOS builds freeze this module; there is no nmcli to ask."""
        run = FakeNmcli({"connection show": NmResult(127, "", "nmcli not found")})
        self.assertEqual(net_wifi.ensure_fallback_dns(run), [])


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
