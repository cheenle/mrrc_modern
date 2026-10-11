"""Changing the wired profile, which can take the operator's own path away.

Same hazard as the Wi-Fi switch (design §6.4) and the same safety net: save the
old values, apply, wait for the address to actually change, and put the old
values back if it does not. The `apply` half differs; nothing else does.

The `auto` half is the one that must keep the box reachable: `nmcli modify` has
already rewritten the profile to DHCP, and the device still reports its
pre-change static address until reactivation lands. On a segment with no DHCP
server that is forever — and "it still has the old address" is exactly what must
not read as "DHCP answered".
"""
from __future__ import annotations

import unittest

import box_ops
import net_wifi
from tests.test_net_wifi import FakeNmcli


def clock():
    state = {"t": 0.0}

    def now():
        return state["t"]

    def sleep(seconds):
        state["t"] += seconds

    return now, sleep


class Fake:
    """A runner plus the interface address nmcli reports, changing over time."""

    def __init__(self, table=None, default=None, addresses=None):
        self.inner = FakeNmcli(table, default)
        #: One entry per `IP4.ADDRESS` poll. The last entry repeats.
        self.addresses = list(addresses or [""])
        self.polls = 0

    def __call__(self, argv):
        argv = list(argv)
        if "IP4.ADDRESS" in argv:
            index = min(self.polls, len(self.addresses) - 1)
            self.polls += 1
            return net_wifi.NmResult(0, self.addresses[index], "")
        return self.inner(argv)

    def count(self, key):
        return self.inner.count(key)

    def joined(self):
        return self.inner.joined()


class ValidationTests(unittest.TestCase):
    def test_the_two_methods_are_auto_and_manual(self):
        self.assertEqual(box_ops.LINK_METHODS, ("auto", "manual"))

    def test_a_manual_method_needs_an_address_with_a_prefix(self):
        self.assertEqual(
            box_ops.link_problem("manual", "192.168.16.217/24"), "")
        for address in ("", "192.168.16.217", "not-an-address",
                        "192.168.16.217/33", "192.168.16.217/0"):
            with self.subTest(address=address):
                self.assertTrue(box_ops.link_problem("manual", address))

    def test_an_auto_method_needs_no_address(self):
        self.assertEqual(box_ops.link_problem("auto", ""), "")

    def test_an_unknown_method_is_refused(self):
        for method in ("", "static", "dhcp", "AUTO", "disabled"):
            with self.subTest(method=method):
                self.assertTrue(box_ops.link_problem(method, ""))


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.now, self.sleep = clock()

    def change(self, runner, **kwargs):
        return box_ops.perform_link_change(
            "有线连接 2", "manual", "192.168.16.217/24", runner=runner,
            state=box_ops.SwitchState(), sleep=self.sleep, now=self.now,
            **kwargs)

    def auto_change(self, runner, **kwargs):
        return box_ops.perform_link_change(
            "有线连接 2", "auto", runner=runner, state=box_ops.SwitchState(),
            sleep=self.sleep, now=self.now, **kwargs)

    # ── the manual form, the brief's five ───────────────────────────
    def test_a_successful_change_reports_the_new_address(self):
        runner = FakeNmcli({
            "ipv4.method": net_wifi.NmResult(0, "auto\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "\n", ""),
            "connection.interface-name": net_wifi.NmResult(0, "eth0\n", ""),
            "IP4.ADDRESS": net_wifi.NmResult(0, "192.168.16.217/24\n", ""),
            "connection up": net_wifi.NmResult(0, "", ""),
        })
        result = self.change(runner)
        self.assertTrue(result["ok"])
        self.assertEqual(result["address"], "192.168.16.217/24")

    def test_it_writes_the_method_and_the_address(self):
        runner = FakeNmcli({
            "ipv4.method": net_wifi.NmResult(0, "auto\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "\n", ""),
            "connection.interface-name": net_wifi.NmResult(0, "eth0\n", ""),
            "IP4.ADDRESS": net_wifi.NmResult(0, "192.168.16.217/24\n", ""),
            "connection up": net_wifi.NmResult(0, "", ""),
        })
        self.change(runner)
        self.assertIn("ipv4.method manual", runner.joined())
        self.assertIn("192.168.16.217/24", runner.joined())

    def test_the_old_values_are_read_before_they_are_overwritten(self):
        """Without this the rollback has nothing to restore, and the box is left
        on settings nobody chose."""
        runner = FakeNmcli({
            "ipv4.method": net_wifi.NmResult(0, "auto\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "\n", ""),
            "connection.interface-name": net_wifi.NmResult(0, "eth0\n", ""),
            "IP4.ADDRESS": net_wifi.NmResult(0, "", ""),
            "connection up": net_wifi.NmResult(0, "", ""),
        })
        self.change(runner)
        self.assertIn("ipv4.method auto", runner.joined())

    def test_a_change_that_never_takes_is_rolled_back(self):
        runner = FakeNmcli({
            "ipv4.method": net_wifi.NmResult(0, "auto\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "\n", ""),
            "connection.interface-name": net_wifi.NmResult(0, "eth0\n", ""),
            "IP4.ADDRESS": net_wifi.NmResult(0, "", ""),
            "connection up": net_wifi.NmResult(0, "", ""),
        }, default=net_wifi.NmResult(0, "", ""))
        result = self.change(runner)
        self.assertFalse(result["ok"])
        self.assertTrue(result["reason"])

    def test_a_failed_modify_does_not_leave_the_state_running(self):
        # "connection modify" is listed first: it is the more specific key, and
        # the modify argv also contains "ipv4.method", so a later key would let
        # the modify command be answered by the wrong result and pass by
        # accident rather than by exercising the failure path.
        runner = FakeNmcli({
            "connection modify": net_wifi.NmResult(
                1, "", "Error: unknown connection."),
            "ipv4.method": net_wifi.NmResult(0, "auto\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "\n", ""),
        })
        state = box_ops.SwitchState()
        result = box_ops.perform_link_change(
            "有线连接 2", "manual", "192.168.16.217/24", runner=runner,
            state=state, sleep=self.sleep, now=self.now)
        self.assertFalse(result["ok"])
        self.assertNotEqual(box_ops.switch_state(state)["phase"],
                            box_ops.PHASE_SWITCHING)

    def test_an_invalid_request_never_reaches_nmcli(self):
        runner = FakeNmcli({})
        result = box_ops.perform_link_change(
            "有线连接 2", "manual", "nonsense", runner=runner,
            state=box_ops.SwitchState(), sleep=self.sleep, now=self.now)
        self.assertFalse(result["ok"])
        self.assertEqual(runner.calls, [])

    # ── the address read is scoped to the profile's interface ──────
    def test_the_address_read_names_the_interface(self):
        """R-an item 2: the check reads `device show <ifname>`, never every
        device. The interface name comes from the profile itself."""
        runner = FakeNmcli({
            "ipv4.method": net_wifi.NmResult(0, "auto\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "\n", ""),
            "connection.interface-name": net_wifi.NmResult(0, "eth0\n", ""),
            "IP4.ADDRESS": net_wifi.NmResult(0, "192.168.16.217/24\n", ""),
            "connection up": net_wifi.NmResult(0, "", ""),
        })
        self.change(runner)
        self.assertIn("nmcli -t -g IP4.ADDRESS device show eth0",
                      runner.joined())

    def test_a_sibling_address_does_not_look_like_this_interface_landed(self):
        """R-an item 2: the brief read every device, so an address on another
        device satisfied the manual check. Scoped, the named interface's
        silence is a timeout and a rollback."""
        inner = FakeNmcli({
            "ipv4.method": net_wifi.NmResult(0, "auto\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "\n", ""),
            "connection.interface-name": net_wifi.NmResult(0, "eth0\n", ""),
        })

        def runner(argv):
            if "IP4.ADDRESS" in argv:
                # Only the unscoped read (no ifname) sees the sibling address;
                # the scoped read of eth0 sees nothing.
                return net_wifi.NmResult(
                    0, "" if "eth0" in argv else "192.168.16.217/24\n", "")
            return inner(argv)

        result = self.change(runner)
        self.assertFalse(result["ok"])

    # ── the auto / DHCP branch ──────────────────────────────────────
    def test_auto_does_not_accept_the_address_it_already_held(self):
        """R-an item 1: after `modify ipv4.method auto`, the device still
        reports the pre-change static address until reactivation lands, and on
        a segment with no DHCP server it never does. That stale address is not
        a DHCP answer. The brief's `elif addresses: return addresses[0]` calls
        it one and reports ok:True; the required behaviour is the same rollback
        the timeout path performs."""
        runner = Fake({
            "ipv4.method": net_wifi.NmResult(0, "manual\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "192.168.16.217/24\n", ""),
            "connection.interface-name": net_wifi.NmResult(0, "eth0\n", ""),
        }, addresses=["192.168.16.217/24"])
        result = self.auto_change(runner)
        self.assertFalse(result["ok"])
        self.assertTrue(result["reason"])
        self.assertIn("ipv4.method manual", runner.joined())

    def test_auto_accepts_an_address_it_did_not_hold_before(self):
        """The stale address is rejected while it lasts, then the DHCP address
        is accepted when it actually lands."""
        runner = Fake({
            "ipv4.method": net_wifi.NmResult(0, "manual\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "192.168.16.217/24\n", ""),
            "connection.interface-name": net_wifi.NmResult(0, "eth0\n", ""),
        }, addresses=["192.168.16.217/24", "192.168.16.217/24",
                      "192.168.16.50/24"])
        result = self.auto_change(runner)
        self.assertTrue(result["ok"])
        self.assertEqual(result["address"], "192.168.16.50/24")
        self.assertEqual(runner.polls, 3)

    def test_auto_to_auto_is_a_no_op_that_keeps_the_held_address(self):
        """Auto→auto reaches here (nothing refuses it), and it is a no-op: the
        profile already says DHCP and already holds a DHCP address. That address
        was never in the (empty) saved `ipv4.addresses`, so it reads as landed
        and is reported — success is the honest answer for a box already on
        DHCP."""
        runner = Fake({
            "ipv4.method": net_wifi.NmResult(0, "auto\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "\n", ""),
            "connection.interface-name": net_wifi.NmResult(0, "eth0\n", ""),
        }, addresses=["192.168.16.50/24"])
        result = self.auto_change(runner)
        self.assertTrue(result["ok"])
        self.assertEqual(result["address"], "192.168.16.50/24")

    # ── the rollback clears what the attempt wrote ─────────────────
    def test_a_failed_static_attempt_restores_auto_and_clears_the_address(self):
        """FIX-5. The attempt to go static failed, so the rollback restores the
        DHCP method — and must clear the address the attempt wrote. NM ignores
        `ipv4.addresses` while the method is auto, so leaving it was latent, not
        live: it resurfaced the moment the operator went back to manual, handing
        them an address nobody chose."""
        runner = FakeNmcli({
            "ipv4.method": net_wifi.NmResult(0, "auto\n", ""),
            "ipv4.addresses": net_wifi.NmResult(0, "\n", ""),
            "connection.interface-name": net_wifi.NmResult(0, "eth0\n", ""),
            "IP4.ADDRESS": net_wifi.NmResult(0, "", ""),
            "connection up": net_wifi.NmResult(0, "", ""),
        })
        result = self.change(runner)   # manual, which never lands
        self.assertFalse(result["ok"])
        rollback = [call for call in runner.calls
                    if "ipv4.method" in call
                    and call[call.index("ipv4.method") + 1] == "auto"]
        self.assertTrue(rollback, "no rollback-to-auto command was issued")
        last = rollback[-1]
        self.assertIn("ipv4.addresses", last)
        self.assertEqual(last[last.index("ipv4.addresses") + 1], "")

    # ── F2: numeric parameters are refused, not spun on ─────────────
    def test_a_zero_poll_returns_instead_of_spinning(self):
        runner = Fake({"ipv4.method": net_wifi.NmResult(0, "auto\n", "")},
                      addresses=[""])
        result = self.auto_change(runner, poll=0)
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])
        self.assertEqual(runner.polls, 0)

    def test_a_negative_poll_is_a_dict_not_a_raise(self):
        runner = Fake({"ipv4.method": net_wifi.NmResult(0, "auto\n", "")},
                      addresses=[""])
        result = self.auto_change(runner, poll=-1.0)
        self.assertIsInstance(result, dict)
        self.assertFalse(result["ok"])


class RestoreProfileTests(unittest.TestCase):
    """`_restore_profile` writes the saved values back. The subtlety is the auto
    half: putting the method back to DHCP without clearing the address a failed
    attempt wrote leaves stale config behind (FIX-5)."""

    def test_restoring_to_manual_writes_the_saved_address(self):
        runner = FakeNmcli({})
        box_ops._restore_profile("有线连接 2", "manual",
                                 "192.168.16.217/24", runner)
        self.assertIn("ipv4.method manual", runner.joined())
        self.assertIn("ipv4.addresses 192.168.16.217/24", runner.joined())

    def test_restoring_to_auto_clears_the_address(self):
        runner = FakeNmcli({})
        box_ops._restore_profile("有线连接 2", "auto", "", runner)
        self.assertIn("ipv4.method auto", runner.joined())
        modify = runner.calls[0]
        self.assertIn("ipv4.addresses", modify)
        self.assertEqual(modify[modify.index("ipv4.addresses") + 1], "")


if __name__ == "__main__":
    unittest.main()
