"""The read-only half of box_ops: everything a page shows and nothing it sets.

Two things this file exists to keep true.

The **locale trap**: nmcli prints AUTOCONNECT as `yes`/`no` in the C locale and
as `是`/`否` under a Chinese one, and the box's locale is not ours to pin. A
parser that only knows `yes` shows a user's own autoconnect switches as off.

The **degradation contract** (spec R-6): this module is frozen into the Windows
and macOS builds, where there is no `nmcli` and no helper. Every function here
has to return an empty/neutral value rather than raise, because the alternative
is a 500 in a request handler.
"""
from __future__ import annotations

import unittest

import box_ops
import net_wifi
from tests.test_net_wifi import FakeNmcli


class LocalSubnetTests(unittest.TestCase):
    def test_it_reads_every_address_the_box_holds(self):
        runner = FakeNmcli({"IP4.ADDRESS": net_wifi.NmResult(
            0, "192.168.16.217/24\n192.168.1.217/24\n", "")})
        self.assertEqual(
            [str(n) for n in box_ops.local_subnets(runner)],
            ["192.168.16.0/24", "192.168.1.0/24"],
        )

    def test_no_addresses_means_no_subnets(self):
        """An empty list closes the onboarding window (see the Review Focus)."""
        runner = FakeNmcli({"IP4.ADDRESS": net_wifi.NmResult(0, "", "")})
        self.assertEqual(box_ops.local_subnets(runner), [])

    def test_a_failed_nmcli_also_means_no_subnets(self):
        runner = FakeNmcli({"IP4.ADDRESS": net_wifi.NmResult(
            1, "", "Error: NetworkManager is not running.")})
        self.assertEqual(box_ops.local_subnets(runner), [])

    def test_a_failing_nmcli_does_not_contribute_its_stdout(self):
        """rc != 0 with output still on stdout is the realistic failure: nmcli
        prints what it knows and then exits non-zero. Reading that stdout anyway
        would open the onboarding window onto a subnet the box may not hold."""
        runner = FakeNmcli({"IP4.ADDRESS": net_wifi.NmResult(
            10, "192.168.16.5/24\n", "Error: not authorised")})
        self.assertEqual(box_ops.local_subnets(runner), [])
        self.assertFalse(box_ops.client_in_local_subnet("192.168.16.9", runner))

    def test_junk_lines_are_skipped_not_fatal(self):
        runner = FakeNmcli({"IP4.ADDRESS": net_wifi.NmResult(
            0, "192.168.1.217/24\n\nnot-an-address\n", "")})
        self.assertEqual(
            [str(n) for n in box_ops.local_subnets(runner)], ["192.168.1.0/24"])


class ClientSubnetTests(unittest.TestCase):
    def setUp(self):
        self.runner = FakeNmcli({"IP4.ADDRESS": net_wifi.NmResult(
            0, "192.168.16.217/24\n", "")})

    def test_a_client_on_my_subnet_is_inside(self):
        self.assertTrue(
            box_ops.client_in_local_subnet("192.168.16.50", self.runner))

    def test_a_client_elsewhere_is_outside(self):
        self.assertFalse(
            box_ops.client_in_local_subnet("10.42.0.57", self.runner))

    def test_the_ipv4_mapped_form_is_the_same_client(self):
        """Starlette hands us `::ffff:192.168.16.50` when the request arrived
        over a v4-mapped socket. Treating that as "not my subnet" closes the
        window on exactly the client it was opened for."""
        self.assertTrue(box_ops.client_in_local_subnet(
            "::ffff:192.168.16.50", self.runner))

    def test_no_client_at_all_is_outside(self):
        # A non-string host (5) is here because "nothing raises" is a contract:
        # a caller that hands us an int must get False, not an AttributeError.
        for host in ("", None, "not-an-ip", "fe80::1", 5):
            with self.subTest(host=host):
                self.assertFalse(
                    box_ops.client_in_local_subnet(host, self.runner))


class ConnectionsReportTests(unittest.TestCase):
    def rows(self, text):
        return FakeNmcli({"connection show": net_wifi.NmResult(0, text, "")})

    def test_it_reads_name_type_and_autoconnect(self):
        report = box_ops.connections_report(self.rows(
            "home:802-11-wireless:yes\n有线连接 2:802-3-ethernet:no\n"))
        self.assertEqual(report, [
            {"name": "home", "type": "802-11-wireless", "autoconnect": True},
            {"name": "有线连接 2", "type": "802-3-ethernet",
             "autoconnect": False},
        ])

    def test_a_chinese_locale_spelling_is_understood(self):
        """See the module docstring. This is the whole reason the check is not
        `== "yes"`."""
        report = box_ops.connections_report(self.rows(
            "home:802-11-wireless:是\n有线连接 2:802-3-ethernet:否\n"))
        self.assertEqual([r["autoconnect"] for r in report], [True, False])

    def test_a_colon_inside_a_name_survives(self):
        """nmcli escapes it as `\\:`; parse_row is the only correct reader."""
        report = box_ops.connections_report(self.rows(
            "my\\:net:802-11-wireless:yes\n"))
        self.assertEqual(report[0]["name"], "my:net")

    def test_a_failed_nmcli_reports_no_connections(self):
        self.assertEqual(box_ops.connections_report(
            FakeNmcli({"connection show": net_wifi.NmResult(1, "", "boom")})),
            [])


class WifiStatusTests(unittest.TestCase):
    def test_the_active_row_is_the_station_link(self):
        runner = FakeNmcli({"device wifi list": net_wifi.NmResult(
            0, "no:邻居家:44\nyes:CMCC-cnham:61\nno:别的:30\n", "")})
        self.assertEqual(box_ops.wifi_status(runner),
                         {"ssid": "CMCC-cnham", "signal": 61})

    def test_no_active_row_is_an_empty_link_not_an_error(self):
        runner = FakeNmcli({"device wifi list": net_wifi.NmResult(
            0, "no:邻居家:44\n", "")})
        self.assertEqual(box_ops.wifi_status(runner), {"ssid": "", "signal": 0})

    def test_a_failure_is_also_an_empty_link(self):
        self.assertEqual(
            box_ops.wifi_status(FakeNmcli(
                {"device wifi list": net_wifi.NmResult(1, "", "boom")})),
            {"ssid": "", "signal": 0})


class ScanTests(unittest.TestCase):
    def test_it_is_the_same_scan_the_wizard_used(self):
        """No second implementation: the AP-era wizard's scan was already the
        strongest-first, colon-safe one."""
        runner = FakeNmcli({"device wifi list": net_wifi.NmResult(
            0, "weak:10:wpa2\nstrong:90:wpa2\nweak:20:wpa2\n", "")})
        self.assertEqual([n["ssid"] for n in box_ops.scan_networks(runner)],
                         ["strong", "weak"])
        self.assertEqual(box_ops.scan_networks(runner)[0]["signal"], 90)


class NetworkReportTests(unittest.TestCase):
    def test_it_collects_the_five_things_that_say_where_it_broke(self):
        runner = FakeNmcli({
            "IP4.ADDRESS": net_wifi.NmResult(0, "192.168.16.217/24\n", ""),
            "IP4.GATEWAY": net_wifi.NmResult(0, "192.168.1.1\n", ""),
            "IP4.DNS": net_wifi.NmResult(0, "192.168.1.1\n", ""),
            "device status": net_wifi.NmResult(
                0, "wlan0:wifi:connected:CMCC-cnham\n", ""),
            "connection show": net_wifi.NmResult(
                0, "home:802-11-wireless:yes\n", ""),
            "device wifi list": net_wifi.NmResult(
                0, "yes:CMCC-cnham:61\n", ""),
        })
        report = box_ops.network_report(runner)
        self.assertEqual(report["addresses"], ["192.168.16.217/24"])
        self.assertEqual(report["gateways"], ["192.168.1.1"])
        self.assertEqual(report["dns"], ["192.168.1.1"])
        self.assertEqual(report["wifi"]["ssid"], "CMCC-cnham")
        self.assertEqual(len(report["devices"]), 1)
        self.assertEqual(len(report["connections"]), 1)

    def test_no_network_manager_is_a_report_not_a_crash(self):
        runner = FakeNmcli(default=net_wifi.NmResult(1, "", "not running"))
        report = box_ops.network_report(runner)
        self.assertEqual(report["addresses"], [])
        self.assertEqual(report["devices"], [])
        self.assertEqual(report["wifi"], {"ssid": "", "signal": 0})


class SystemReportTests(unittest.TestCase):
    def test_it_reports_the_three_units_that_matter(self):
        runner = FakeNmcli({
            "is-active mrrc-modern.service": net_wifi.NmResult(
                0, "active\n", ""),
            "is-active NetworkManager.service": net_wifi.NmResult(
                0, "active\n", ""),
            "is-active avahi-daemon.service": net_wifi.NmResult(
                3, "inactive\n", ""),
        })
        report = box_ops.system_report(runner)
        self.assertEqual(
            [(s["unit"], s["active"]) for s in report["services"]],
            [("mrrc-modern.service", True),
             ("NetworkManager.service", True),
             ("avahi-daemon.service", False)],
        )

    def test_no_systemctl_is_not_fatal(self):
        """The desktop builds have no systemctl either."""
        report = box_ops.system_report(
            FakeNmcli(default=net_wifi.NmResult(127, "", "not found")))
        self.assertEqual([s["active"] for s in report["services"]],
                         [False, False, False])


class IdentityReportTests(unittest.TestCase):
    def test_it_gives_both_ways_back(self):
        """spec §8: `<name>.local` is perfect on iOS, needs Bonjour on Windows
        and is a lottery on Android — so the page shows the address too."""
        runner = FakeNmcli({"IP4.ADDRESS": net_wifi.NmResult(
            0, "192.168.16.217/24\n", "")})
        report = box_ops.identity_report(runner)
        self.assertTrue(report["hostname"])
        self.assertEqual(report["mdns"], report["hostname"] + ".local")
        self.assertEqual(report["addresses"], ["192.168.16.217"])
        self.assertEqual(report["suggested_name"], "MRRC-Modern")


class SafetyReportTests(unittest.TestCase):
    def test_every_field_is_read_only_and_explains_itself(self):
        report = box_ops.safety_report({"MRRC_PTT_MAX_TX_SECONDS": "300"})
        self.assertEqual(
            {f["key"] for f in report},
            {"MRRC_ALLOW_UNVERIFIED_TX", "MRRC_PTT_MAX_TX_SECONDS",
             "MRRC_REMOTE_SESSION_TX_HEARTBEAT_S"},
        )
        for field in report:
            with self.subTest(key=field["key"]):
                self.assertTrue(field["read_only"])
                self.assertTrue(field["note"])

    def test_the_transmit_gate_shows_what_the_radio_would_use(self):
        """AD-019 / NFR-067 — the one number that must never be guessed at."""
        report = {f["key"]: f["value"]
                  for f in box_ops.safety_report({"MRRC_ALLOW_UNVERIFIED_TX": "1"})}
        self.assertEqual(report["MRRC_ALLOW_UNVERIFIED_TX"], "1")

    def test_an_unset_key_falls_back_to_its_documented_default(self):
        report = {f["key"]: f["value"] for f in box_ops.safety_report({})}
        self.assertEqual(report["MRRC_ALLOW_UNVERIFIED_TX"], "0")
        self.assertEqual(report["MRRC_PTT_MAX_TX_SECONDS"], "0")
        self.assertEqual(report["MRRC_REMOTE_SESSION_TX_HEARTBEAT_S"], "")


class HelperAvailableTests(unittest.TestCase):
    def test_it_is_a_path_check_and_nothing_more(self):
        """`helper_available` is allowed to be wrong in one direction only: it
        may say False when the helper would have answered. Saying True when
        there is no socket puts a button on a page that cannot work."""
        self.assertFalse(box_ops.helper_available("/definitely/not/here.sock"))
        self.assertFalse(box_ops.helper_available(""))


class TimezoneTests(unittest.TestCase):
    def test_a_real_zone_resolves_to_its_name(self):
        report = box_ops.timezone()
        self.assertIsInstance(report, str)


if __name__ == "__main__":
    unittest.main()
